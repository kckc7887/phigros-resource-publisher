"""In-memory S3 publication contract tests; never connects to the network."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError
from phigros_publisher.organizer import organize_release
from phigros_publisher.uploader import CURRENT_KEY, _cleanup, _manifest_identity, retry_cleanup, upload_release
from publish import _write_summary


def s3_error(code, operation="GetObject", status=404):
    return ClientError({"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.operations = []
        self.fail_upload = False
        self.fail_get = {}
        self.corrupt = set()
        self.failed_deletes = set()
        self.fail_current = False
        self.conflict_current = False
        self.corrupt_current_readback = False
        self.on_delete = None
        self.lock = threading.RLock()
        self.active = 0
        self.max_active = 0
        self.transfer_configs = []
        self.delay = 0
        self.meta = SimpleNamespace(service_model=SimpleNamespace(operation_model=lambda _: SimpleNamespace(
            input_shape=SimpleNamespace(members={"IfMatch": {}, "IfNoneMatch": {}}))))

    @staticmethod
    def etag(data):
        return '"' + hashlib.md5(data).hexdigest() + '"'

    def get_object(self, *, Bucket, Key):
        with self.lock:
            self.operations.append(("get", Key))
            if Key in self.fail_get:
                raise self.fail_get[Key]
            if Key not in self.objects:
                raise s3_error("NoSuchKey")
            data = self.objects[Key]
            etag = self.etag(data)
            if Key in self.corrupt:
                data = b"X" + data[1:]
            if Key == CURRENT_KEY and self.corrupt_current_readback and any(op == ("put", CURRENT_KEY) for op in self.operations):
                data = b"{}"
            return {"Body": io.BytesIO(data), "ContentLength": len(data), "ETag": etag}

    def upload_file(self, filename, bucket, key, *, ExtraArgs, Config):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.operations.append(("upload", key))
            self.transfer_configs.append(Config)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.fail_upload:
                raise RuntimeError("upload failed")
            with self.lock:
                self.objects[key] = Path(filename).read_bytes()
        finally:
            with self.lock:
                self.active -= 1

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        with self.lock:
            self.operations.append(("put", Key))
            if Key == CURRENT_KEY:
                if self.fail_current:
                    raise s3_error("NotImplemented", "PutObject", 501)
                if self.conflict_current:
                    raise s3_error("PreconditionFailed", "PutObject", 412)
            if "IfMatch" in kwargs:
                if Key not in self.objects or self.etag(self.objects[Key]) != kwargs["IfMatch"]:
                    raise s3_error("PreconditionFailed", "PutObject", 412)
            elif "IfNoneMatch" in kwargs:
                if kwargs["IfNoneMatch"] != "*" or Key in self.objects:
                    raise s3_error("PreconditionFailed", "PutObject", 412)
            elif Key == CURRENT_KEY:
                raise AssertionError("current requires conditional write")
            self.objects[Key] = Body
            return {"ETag": self.etag(Body)}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, *, Bucket, Prefix):
        with self.lock:
            self.operations.append(("list", Prefix))
            return [{"Contents": [{"Key": key} for key in sorted(self.objects) if key.startswith(Prefix)]}]

    def delete_objects(self, *, Bucket, Delete):
        result = {"Deleted": [], "Errors": []}
        with self.lock:
            for entry in Delete["Objects"]:
                key = entry["Key"]
                self.operations.append(("delete", key))
                if key in self.failed_deletes:
                    result["Errors"].append({"Key": key, "Code": "AccessDenied"})
                else:
                    self.objects.pop(key, None)
                    result["Deleted"].append({"Key": key})
            if self.on_delete:
                self.on_delete()
        return result

    def delete_object(self, *, Bucket, Key):
        with self.lock:
            self.operations.append(("delete_single", Key))
            self.objects.pop(Key, None)
        return {}


class PublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audio_temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.audio_temp.cleanup)
        audio_path = Path(cls.audio_temp.name) / "fixture.ogg"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                        "-t", "0.1", "-c:a", "libvorbis", str(audio_path)], check=True, timeout=30)
        cls.audio = audio_path.read_bytes()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.extracted = self.root / "extracted"
        files = {
            "metadata/info.tsv": b"Song.A\tSong\tArtist\tIllustrator\tCharter\n",
            "metadata/difficulty.tsv": b"Song.A\t1\n",
            "chart/Song.A.0/EZ.json": b'{"judgeLineList":[]}',
            "music/Song.A.ogg": self.audio,
        }
        files.update({f"{directory}/Song.A.png": b"png" for directory in
                      ("illustration", "illustrationBlur", "illustrationLowRes")})
        for filename, data in files.items():
            path = self.extracted / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.s3 = FakeS3()
        self.report = self.root / "report.json"
        self.config = {"endpoint": "https://example.com", "bucket": "test", "access_key": "test",
                       "secret_key": "test", "max_workers": 4, "report_path": self.report}
        self.patcher = patch("phigros_publisher.uploader._make_s3_client", return_value=self.s3)
        self.client_factory = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def release(self):
        return organize_release(self.extracted, self.root / "release", "3.20.0")

    def publish(self, release=None):
        return upload_release(release or self.release(), self.config)

    def old(self):
        result = self.publish()
        self.s3.operations.clear()
        return result

    def changed(self):
        image = self.extracted / "illustration/Song.A.png"
        image.write_bytes(image.read_bytes() + b"changed png")
        return self.release()

    def test_same_content_ignores_revision_time_and_performs_only_two_json_reads(self):
        self.old()
        before = dict(self.s3.objects)
        result = self.publish()
        self.assertTrue(result["unchanged"])
        self.assertEqual(result["uploaded"], 0)
        self.assertEqual(before, self.s3.objects)
        self.assertEqual(len(self.s3.operations), 2)
        self.assertTrue(all(op[0] == "get" and op[1].endswith(".json") for op in self.s3.operations))

    def test_one_asset_change_uploads_every_asset_and_then_deletes_only_old_snapshot(self):
        old = self.old()
        unrelated = "phigros/releases/unrelated/keep"
        self.s3.objects[unrelated] = b"keep"
        release = self.changed()
        result = self.publish(release)
        uploads = [op for op in self.s3.operations if op[0] == "upload"]
        self.assertEqual(len(uploads), release["asset_count"])
        self.assertNotEqual(result["candidate_prefix"], old["candidate_prefix"])
        self.assertIn(unrelated, self.s3.objects)
        self.assertTrue(all(op[1].startswith(old["candidate_prefix"]) for op in self.s3.operations if op[0] == "delete"))
        last_resource_get = max(i for i, op in enumerate(self.s3.operations) if op[0] == "get" and op[1].startswith(result["candidate_prefix"]))
        pointer_put = self.s3.operations.index(("put", CURRENT_KEY))
        first_delete = next(i for i, op in enumerate(self.s3.operations) if op[0] == "delete")
        self.assertLess(last_resource_get, pointer_put)
        self.assertLess(pointer_put, first_delete)

    def test_unchanged_artifact_current_and_manifest_keep_matching_hashes(self):
        self.old()
        release = self.release()
        result = self.publish(release)
        artifacts = self.root / "artifacts"
        _write_summary(artifacts, release, result, "3.20.0", 1.0)
        current = json.loads((artifacts / "current.json").read_text(encoding="utf-8"))
        archived_manifest = (artifacts / "manifest.json").read_bytes()
        self.assertEqual(current["manifestSha256"], hashlib.sha256(archived_manifest).hexdigest())
        self.assertEqual(archived_manifest, self.s3.objects[current["manifest"]])
        self.assertTrue((artifacts / "candidate-manifest.json").is_file())

    def test_missing_sdk_conditions_fail_before_resource_upload(self):
        self.s3.meta.service_model.operation_model = lambda _: SimpleNamespace(
            input_shape=SimpleNamespace(members={}))
        with self.assertRaisesRegex(RuntimeError, "条件写入"):
            self.publish()
        self.assertFalse(any(op[0] in ("upload", "put", "delete") for op in self.s3.operations))

    def test_missing_manifest_or_hash_or_corrupt_manifest_does_full_upload_and_old_snapshot_cleanup(self):
        for failure in ("missing", "hash", "corrupt"):
            with self.subTest(failure=failure):
                self.s3 = FakeS3()
                self.client_factory.return_value = self.s3
                old = self.old()
                current = json.loads(self.s3.objects[CURRENT_KEY])
                if failure == "missing":
                    del self.s3.objects[current["manifest"]]
                elif failure == "hash":
                    del current["manifestSha256"]
                    del current["noteCounts"]
                    self.s3.objects[CURRENT_KEY] = json.dumps(current).encode()
                else:
                    self.s3.objects[current["manifest"]] = b"broken"
                result = self.publish()
                self.assertFalse(result["unchanged"])
                self.assertTrue(any(op[0] == "upload" for op in self.s3.operations))
                self.assertEqual(result["previous_prefix"], old["candidate_prefix"])
                self.assertFalse(any(key.startswith(old["candidate_prefix"]) for key in self.s3.objects))

    def test_forbidden_baseline_is_not_mistaken_for_missing_manifest(self):
        self.s3.fail_get[CURRENT_KEY] = s3_error("AccessDenied", status=403)
        with self.assertRaises(ClientError):
            self.publish()
        self.assertFalse(any(op[0] in ("upload", "put", "delete") for op in self.s3.operations))

    def test_remote_same_size_corruption_never_commits_or_deletes(self):
        old = self.old()
        current_bytes = self.s3.objects[CURRENT_KEY]
        release = self.changed()
        prefix = release["current"]["manifest"].rsplit("/", 1)[0] + "/"
        self.s3.corrupt.add(prefix + "illustrations/Song.A.png")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.publish(release)
        self.assertEqual(self.s3.objects[CURRENT_KEY], current_bytes)
        self.assertFalse(any(op[0] == "delete" for op in self.s3.operations))
        self.assertTrue(any(key.startswith(old["candidate_prefix"]) for key in self.s3.objects))

    def test_current_conflict_or_unsupported_condition_never_falls_back(self):
        for attribute in ("conflict_current", "fail_current"):
            with self.subTest(attribute=attribute):
                self.s3.conflict_current = self.s3.fail_current = False
                self.old()
                before = self.s3.objects[CURRENT_KEY]
                setattr(self.s3, attribute, True)
                with self.assertRaises(ClientError):
                    self.publish(self.changed())
                self.assertEqual(self.s3.objects[CURRENT_KEY], before)
                self.assertEqual(sum(op == ("put", CURRENT_KEY) for op in self.s3.operations), 1)
                self.assertFalse(any(op[0] == "delete" for op in self.s3.operations))

    def test_manifest_readback_failure_stops_pointer(self):
        release = self.release()
        self.s3.corrupt.add(release["current"]["manifest"])
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.publish(release)
        self.assertNotIn(CURRENT_KEY, self.s3.objects)

    def test_pointer_readback_failure_stops_cleanup(self):
        self.old()
        self.s3.corrupt_current_readback = True
        with self.assertRaisesRegex(RuntimeError, "回读"):
            self.publish(self.changed())
        self.assertFalse(any(op[0] == "delete" for op in self.s3.operations))
        self.assertFalse(json.loads(self.report.read_text(encoding="utf-8"))["pointer_verified"])

    def test_cleanup_partial_error_report_can_retry_only_remaining_keys(self):
        old = self.old()
        failed_key = old["candidate_prefix"] + "music/Song.A.ogg"
        self.s3.failed_deletes.add(failed_key)
        with self.assertRaisesRegex(RuntimeError, "清理未完成"):
            self.publish(self.changed())
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertTrue(report["pointer_verified"])
        self.assertEqual(report["cleanup_remaining"], [failed_key])
        self.s3.failed_deletes.clear()
        self.s3.operations.clear()
        result = retry_cleanup(self.report, self.config)
        self.assertEqual(result["status"], "published")
        self.assertEqual([op for op in self.s3.operations if op[0] == "delete"], [("delete", failed_key)])

    def test_cleanup_rechecks_pointer_between_batches(self):
        published = self.publish()
        old_prefix = "phigros/releases/old-snapshot/"
        keys = [old_prefix + str(i) for i in range(1001)]
        self.s3.objects.update({key: b"old" for key in keys})
        report = {**published, "previous_prefix": old_prefix, "previous_keys": keys, "cleanup_remaining": keys}
        self.s3.on_delete = lambda: self.s3.objects.update({CURRENT_KEY: b"{}"})
        with self.assertRaisesRegex(ValueError, "current 已改变"):
            _cleanup(self.s3, "test", report, self.report)
        self.assertEqual(len(report["cleanup_remaining"]), 1)
        self.assertIn(keys[-1], self.s3.objects)

    def test_retry_cleanup_refuses_changed_pointer_or_outside_snapshot(self):
        old = self.old()
        failed_key = old["candidate_prefix"] + "music/Song.A.ogg"
        self.s3.failed_deletes.add(failed_key)
        with self.assertRaises(RuntimeError):
            self.publish(self.changed())
        self.s3.operations.clear()
        self.s3.objects[CURRENT_KEY] = b"{}"
        with self.assertRaisesRegex(ValueError, "current 已改变"):
            retry_cleanup(self.report, self.config)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        report["cleanup_remaining"] = ["rizline/releases/other/music.ogg"]
        self.report.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "快照"):
            retry_cleanup(self.report, self.config)
        self.assertFalse(any(op[0] == "delete" for op in self.s3.operations))

    def test_bounded_upload_workers_and_disabled_nested_transfer_threads(self):
        self.s3.delay = 0.03
        self.publish()
        self.assertGreater(self.s3.max_active, 1)
        self.assertLessEqual(self.s3.max_active, 4)
        self.assertTrue(all(not config.use_threads and config.max_concurrency == 1 for config in self.s3.transfer_configs))

    def test_existing_candidate_namespace_is_never_overwritten(self):
        release = self.release()
        candidate = release["current"]["manifest"].rsplit("/", 1)[0] + "/existing"
        self.s3.objects[candidate] = b"keep"
        with self.assertRaisesRegex(ValueError, "目录已存在"):
            self.publish(release)
        self.assertEqual(self.s3.objects[candidate], b"keep")
        self.assertFalse(any(op[0] in ("upload", "put") for op in self.s3.operations))

    def test_unsafe_manifest_path_rejected(self):
        manifest = {"schemaVersion": 1, "gameVersion": "1", "assetCount": 1, "totalBytes": 1,
                    "assets": [{"path": "../outside", "sha256": "0" * 64, "size": 1, "contentType": "text/plain"}]}
        with self.assertRaises(ValueError):
            _manifest_identity(manifest)

    def test_workflow_schedules_beijing_eight_and_keeps_validation_only_push_pr(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/publish.yml").read_text(encoding="utf-8")
        self.assertIn("cron: '0 0 * * *'", workflow)
        self.assertIn("github.event_name == 'workflow_dispatch' || github.event_name == 'schedule'", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("PHIGROS_PARSE_WORKERS: ${{ vars.PHIGROS_PARSE_WORKERS }}", workflow)


if __name__ == "__main__":
    unittest.main()
