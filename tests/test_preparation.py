import base64
import importlib.util
import json
import logging
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from phigros_publisher.chart_notes import build_note_counts_rows
from phigros_publisher.extractor import _LineLogWriter, extract_resources
from phigros_publisher.organizer import organize_release, validate_release
from phigros_publisher.parallel import bounded_map


class BoundedPreparationTests(unittest.TestCase):
    def test_parallel_work_is_bounded_and_returns_input_order(self):
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        active = 0
        maximum = 0

        def work(value):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            barrier.wait(timeout=5)
            with lock:
                active -= 1
            return value * 2

        self.assertEqual(bounded_map(work, range(9), workers=3), list(range(0, 18, 2)))
        self.assertEqual(maximum, 3)

    def test_failure_stops_new_work_and_joins_active_workers(self):
        active_started = threading.Event()
        failed = threading.Event()
        release_active = threading.Event()
        finished = threading.Event()
        started = []
        errors = []

        def work(value):
            started.append(value)
            if value == 0:
                active_started.wait(timeout=5)
                failed.set()
                raise RuntimeError("decode failed")
            active_started.set()
            release_active.wait(timeout=5)

        def run():
            try:
                bounded_map(work, range(20), workers=2)
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(failed.wait(timeout=5))
            self.assertFalse(finished.wait(timeout=0.05))
        finally:
            release_active.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(started, [0, 1])
        self.assertEqual(str(errors[0]), "decode failed")

    def test_invalid_worker_limits_rejected(self):
        for workers in (0, -1, 17, True, "4"):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                bounded_map(lambda value: value, (), workers=workers)

    def test_log_writer_accepts_concurrent_lines_and_binary_writes(self):
        lines = []
        writer = _LineLogWriter(lines.append)
        bounded_map(lambda value: writer.write(f"line-{value}\n"), range(30), workers=4)
        writer.buffer.write(b"last\n")
        writer.flush()
        self.assertEqual(set(lines), {*(f"line-{value}" for value in range(30)), "last"})
        self.assertEqual(len(lines), 31)


class CandidatePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.extracted = self.root / "extracted"
        files = {
            "metadata/info.tsv": "Song.B\tB\tArtist\tIllustrator\tCharter\nSong.A\tA\tArtist\tIllustrator\tCharter\n",
            "metadata/difficulty.tsv": "Song.B\t1\nSong.A\t1\n",
            "chart/Song.A.0/EZ.json": json.dumps({"judgeLineList": [{"notesAbove": [{"type": 1}]}]}),
            "chart/Song.B.0/EZ.json": json.dumps({"judgeLineList": [{"notesBelow": [{"type": 3}]}]}),
        }
        for name, content in files.items():
            path = self.extracted / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        for song in ("Song.A", "Song.B"):
            for folder in ("illustration", "illustrationBlur", "illustrationLowRes", "music"):
                path = self.extracted / folder / (f"{song}.ogg" if folder == "music" else f"{song}.png")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"OggS" + bytes(24) + b"\x01vorbis" if folder == "music" else b"png")

    def test_same_game_version_reuses_local_bundle_directory(self):
        with patch("phigros_publisher.organizer.validate_music", return_value=True):
            first = organize_release(self.extracted, self.root / "release", "3.20.0", workers=2)
            first_root = Path(first["version_dir"])
            (self.extracted / "illustration/Song.A.png").write_bytes(b"new picture")
            second = organize_release(self.extracted, self.root / "release", "3.20.0", workers=2)
            self.assertEqual(validate_release(second, workers=2)["songCount"], 2)
        self.assertEqual(first["version_dir"], second["version_dir"])
        self.assertEqual(first["version"], second["version"])
        self.assertEqual(Path(second["version_dir"]).name, "bundle")
        self.assertEqual((first_root / "illustrations/Song.A.png").read_bytes(), b"new picture")
        for result in (first, second):
            resource_version = Path(result["version_dir"]).name
            self.assertEqual(result["current"]["resourceVersion"], resource_version)
            self.assertEqual(result["current"]["manifest"], f"phigros/releases/{resource_version}/manifest.json")
            self.assertEqual(result["current"]["gameVersion"], "3.20.0")

    def test_parallel_and_serial_manifests_have_identical_sorted_assets(self):
        with patch("phigros_publisher.organizer.validate_music", return_value=True):
            serial = organize_release(self.extracted, self.root / "serial", "3.20.0", workers=1)
            parallel = organize_release(self.extracted, self.root / "parallel", "3.20.0", workers=4)
        def assets(result):
            return json.loads((Path(result["version_dir"]) / "manifest.json").read_text())["assets"]
        self.assertEqual(assets(serial), assets(parallel))
        self.assertEqual([asset["path"] for asset in assets(parallel)], sorted(asset["path"] for asset in assets(parallel)))

    def test_ffprobe_validation_runs_in_parallel(self):
        barrier = threading.Barrier(2)
        def validate(_path):
            barrier.wait(timeout=5)
            return True
        with patch("phigros_publisher.organizer.validate_music", side_effect=validate):
            result = organize_release(self.extracted, self.root / "release", "3.20.0", workers=2)
        self.assertEqual(result["validation"]["musicCount"], 2)

    def test_note_count_order_and_values_are_stable(self):
        charts = self.extracted / "chart"
        metadata = self.extracted / "metadata"
        expected = [["Song.A.0", "[1,0,0,0]"], ["Song.B.0", "[0,1,0,0]"]]
        self.assertEqual(build_note_counts_rows(charts, metadata, workers=1), expected)
        self.assertEqual(build_note_counts_rows(charts, metadata, workers=4), expected)

    def test_corrupt_chart_stops_before_replacing_local_current(self):
        with patch("phigros_publisher.organizer.validate_music", return_value=True):
            original = organize_release(self.extracted, self.root / "release", "3.20.0", workers=2)
            pointer = Path(original["current_path"]).read_bytes()
            (self.extracted / "chart/Song.A.0/EZ.json").write_text("{corrupt", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "谱面物量统计失败"):
                organize_release(self.extracted, self.root / "release", "3.20.0", workers=2)
        self.assertEqual(Path(original["current_path"]).read_bytes(), pointer)

    def test_in_process_extraction_restores_runtime_after_failure(self):
        toolchain = self.root / "toolchain"
        script = toolchain / "script-py"
        script.mkdir(parents=True)
        apk = self.root / "game.apk"
        apk.write_bytes(b"apk")
        previous_cwd = Path.cwd()
        previous_path = sys.path[:]
        def failing_run(script_dir, apk_path, *, music, workers):
            self.assertEqual(workers, 3)
            self.assertTrue(music)
            os.chdir(script_dir)
            sys.path.insert(0, "temporary-entry")
            raise RuntimeError("bundle failure")
        with patch("phigros_publisher.extractor.prepare_toolchain", return_value=toolchain), \
             patch("phigros_publisher.extractor.run_extract", side_effect=failing_run):
            with self.assertRaisesRegex(RuntimeError, "bundle failure"):
                extract_resources(self.root, apk, self.root / "tools", music=True, workers=3)
        self.assertEqual(Path.cwd(), previous_cwd)
        self.assertEqual(sys.path, previous_path)


class BundlePreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = Path(__file__).resolve().parents[1] / "bundled/phiTool/script-py"
        sys.path.insert(0, str(directory))
        try:
            spec = importlib.util.spec_from_file_location("publisher_parallel_resource_test", directory / "resource.py")
            cls.resource = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.resource)
        finally:
            sys.path.remove(str(directory))

    @staticmethod
    def catalog(keys):
        key_data = bytearray()
        positions = []
        names = [f"Assets/Tracks/{key}" for key in keys] + [f"bundle-{index}" for index in range(len(keys))]
        for name in names:
            encoded = name.encode()
            positions.append(len(key_data))
            key_data.extend(b"\0" + struct.pack("<I", len(encoded)) + encoded)
        buckets = bytearray(struct.pack("<I", len(names)))
        entries = bytearray(4)
        for index, position in enumerate(positions):
            buckets.extend(struct.pack("<III", position, 1, index))
            entry = bytearray(28)
            entry[8:10] = struct.pack("<H", index + len(keys) if index < len(keys) else 65535)
            entries.extend(entry)
        return {name: base64.b64encode(value).decode() for name, value in (
            ("m_KeyDataString", key_data), ("m_BucketDataString", buckets), ("m_EntryDataString", entries))}

    def test_each_bundle_parses_and_saves_on_its_own_worker_and_handles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk_path = root / "game.apk"
            keys = [f"Song.{index}.0/Chart_EZ.json" for index in range(6)]
            with ZipFile(apk_path, "w") as apk:
                apk.writestr("assets/aa/catalog.json", json.dumps(self.catalog(keys)))
                for index in range(6):
                    apk.writestr(f"assets/aa/Android/bundle-{index}", str(index))
            metadata = root / "metadata"
            metadata.mkdir()
            (metadata / "tmp.tsv").write_text("")
            (metadata / "difficulty.tsv").write_text("")
            barrier = threading.Barrier(3)
            lock = threading.Lock()
            environment_threads = []
            archive_threads = []
            real_zip = self.resource.ZipFile
            class Environment:
                def __init__(self):
                    self.owner = threading.get_ident()
                    self.files = {}
                    with lock:
                        environment_threads.append(self.owner)
                def load_file(self, _payload, name):
                    barrier.wait(timeout=5)
                    obj = Mock(script=json.dumps({"judgeLineList": []}).encode())
                    self.files[name] = Mock(get_filtered_objects=lambda _: iter([Mock(read=lambda: obj)]))
            def archive(*args, **kwargs):
                with lock:
                    archive_threads.append(threading.get_ident())
                return real_zip(*args, **kwargs)
            config = dict(avatar=False, chart=True, illustrationBlur=False, illustrationLowRes=False,
                          illustration=False, music=False, UPDATE=dict(main_story=0, other_song=0, side_story=0))
            with patch.object(self.resource, "Environment", Environment), patch.object(self.resource, "ZipFile", side_effect=archive):
                self.resource.run(str(apk_path), config, logging.getLogger("prepare-test"), str(metadata),
                                  {"chart": str(root / "charts")}, workers=3)
            self.assertEqual(len(environment_threads), 6)
            self.assertEqual(len(set(environment_threads)), 3)
            self.assertEqual(len(archive_threads), 7)  # one catalog plus one independent handle per bundle
            self.assertEqual(sorted(path.relative_to(root / "charts").as_posix() for path in (root / "charts").rglob("*.json")),
                             [f"Song.{index}.0/EZ.json" for index in range(6)])

    def test_all_asset_saves_use_the_worker_path(self):
        obj = Mock(script=b"chart")
        entry = Mock(get_filtered_objects=lambda _: iter([Mock(read=lambda: obj)]))
        keys = ("avatar.Name", "Song.0/Chart_EZ.json", "Song.0/IllustrationBlur.png",
                "Song.0/IllustrationLowRes.png", "Song.0/Illustration.png", "Song.0/music.wav")
        config = {key: True for key in ("avatar", "chart", "illustrationBlur", "illustrationLowRes", "illustration", "music")}
        with tempfile.TemporaryDirectory() as temporary:
            pool = Mock()
            output = {key: temporary for key in config}
            for key in keys:
                self.resource.save(key, entry, pool, Mock(), output, config)
            self.assertEqual(pool.submit.call_count, 6)

    def test_bundle_failure_cancels_queued_work_and_waits_for_active_saves(self):
        both_active = threading.Barrier(3)
        fail = threading.Event()
        release_save = threading.Event()
        submitted = threading.Event()
        finished = threading.Event()
        started = []
        tasks = []
        errors = []

        def work(index):
            started.append(index)
            both_active.wait(timeout=5)
            if index == 0:
                fail.wait(timeout=5)
                raise ValueError("invalid bundle")
            release_save.wait(timeout=5)

        def run():
            try:
                with self.resource.CheckedThreadPoolExecutor(max_workers=2) as pool:
                    for index in range(4):
                        tasks.append(pool.submit(work, index))
                    submitted.set()
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(submitted.wait(timeout=5))
            both_active.wait(timeout=5)
            fail.set()
            self.assertFalse(finished.wait(timeout=0.05))
        finally:
            fail.set()
            release_save.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(started), [0, 1])
        self.assertTrue(tasks[2].cancelled())
        self.assertTrue(tasks[3].cancelled())
        self.assertIn("资源提取失败", str(errors[0]))


if __name__ == "__main__":
    unittest.main()
