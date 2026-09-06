import importlib.util
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from phigros_publisher.organizer import organize_release, validate_release
from phigros_publisher.uploader import upload_release


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audio_temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.audio_temp.cleanup)
        audio_path = Path(cls.audio_temp.name) / 'fixture.ogg'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
                        '-t', '0.1', '-c:a', 'libvorbis', str(audio_path)], check=True, timeout=30)
        cls.audio = audio_path.read_bytes()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.extracted = self.root / 'extracted'
        files = {
            'metadata/info.tsv': 'Song.A\tSong\tArtist\tIllustrator\tCharter\n',
            'metadata/difficulty.tsv': 'Song.A\t1\n',
            'chart/Song.A.0/EZ.json': json.dumps({'judgeLineList': []}),
        }
        for name, content in files.items():
            path = self.extracted / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        for folder in ('illustration', 'illustrationBlur', 'illustrationLowRes', 'music'):
            path = self.extracted / folder
            path.mkdir()
            (path / ('Song.A.ogg' if folder == 'music' else 'Song.A.png')).write_bytes(
                self.audio if folder == 'music' else b'png')

    def release(self):
        return organize_release(self.extracted, self.root / 'release', '3.20.0')

    def test_complete_release_and_unique_revision(self):
        first = self.release()
        self.assertEqual(validate_release(first), {'songCount': 1, 'musicCount': 1, 'missingResources': []})
        second = self.release()
        self.assertNotEqual(first['current']['resourceVersion'], second['current']['resourceVersion'])
        self.assertEqual(len(second['current']['manifestSha256']), 64)

    def test_empty_music_directory_cannot_publish(self):
        (self.extracted / 'music/Song.A.ogg').unlink()
        with self.assertRaisesRegex(ValueError, 'music/Song.A.ogg'):
            self.release()

    def test_invalid_music_cannot_publish(self):
        (self.extracted / 'music/Song.A.ogg').write_bytes(b'not audio' * 20)
        with self.assertRaisesRegex(ValueError, 'invalid OGG'):
            self.release()

    def test_missing_difficulty_cannot_publish(self):
        (self.extracted / 'chart/Song.A.0/EZ.json').unlink()
        with self.assertRaisesRegex(ValueError, 'EZ.json'):
            self.release()

    def test_header_only_music_cannot_publish(self):
        (self.extracted / 'music/Song.A.ogg').write_bytes(b'OggS' + bytes(24) + b'\x01vorbis' + bytes(40))
        with self.assertRaisesRegex(ValueError, 'invalid OGG'):
            self.release()

    def test_pointer_only_upload_is_blocked_before_s3(self):
        with patch('phigros_publisher.uploader._load_boto3') as sdk:
            with self.assertRaisesRegex(ValueError, '全量上传'):
                upload_release(self.release(), {**self.config(), 'upload_scope': 'current'})
            sdk.assert_not_called()

    def test_corrupt_file_blocked_before_s3(self):
        release = self.release()
        (Path(release['version_dir']) / 'catalog.json').write_text('{}')
        with patch('phigros_publisher.uploader._load_boto3') as sdk:
            with self.assertRaises((KeyError, ValueError)):
                upload_release(release, self.config())
            sdk.assert_not_called()

    @staticmethod
    def config():
        return dict(endpoint='https://example.com', bucket='test', access_key='test', secret_key='test', max_workers=1)

    def test_upload_failure_does_not_advance_pointer_or_delete(self):
        release = self.release()
        client = Mock()
        client.upload_file.side_effect = RuntimeError('upload failed')
        with patch('phigros_publisher.uploader._load_boto3', return_value=(Mock(), Mock())), \
             patch('phigros_publisher.uploader._make_s3_client', return_value=client), \
             patch('phigros_publisher.uploader.delete_stale_release_objects') as delete:
            with self.assertRaisesRegex(RuntimeError, 'upload failed'):
                upload_release(release, self.config())
            self.assertFalse(any(call.args[2] == 'phigros/current.json' for call in client.upload_file.call_args_list))
            delete.assert_not_called()

    def test_pointer_is_last_upload(self):
        release = self.release()
        client = Mock()
        with patch('phigros_publisher.uploader._load_boto3', return_value=(Mock(), Mock())), \
             patch('phigros_publisher.uploader._make_s3_client', return_value=client), \
             patch('phigros_publisher.uploader.delete_stale_release_objects', return_value=0):
            upload_release(release, self.config())
        self.assertEqual(client.upload_file.call_args_list[-1].args[2], 'phigros/current.json')

    def test_same_size_corruption_rejected(self):
        release = self.release()
        (Path(release['version_dir']) / 'illustrations/Song.A.png').write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError, '校验失败'):
            validate_release(release)


class ExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = Path(__file__).resolve().parents[1] / 'bundled/phiTool/script-py'
        sys.path.insert(0, str(directory))
        try:
            spec = importlib.util.spec_from_file_location('publisher_resource_test', directory / 'resource.py')
            cls.resource = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.resource)
        finally:
            sys.path.remove(str(directory))

    def test_music_worker_failure_reaches_caller(self):
        with self.assertRaisesRegex(RuntimeError, 'broken.ogg'):
            with self.resource.CheckedThreadPoolExecutor(2) as pool:
                pool.submit(Mock(side_effect=ValueError('decode failed')), 'broken.ogg')

    def test_write_failure_reaches_caller(self):
        with patch('builtins.open', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'chart.json'):
                self.resource.write_resource(('chart.json', b'chart'))


if __name__ == '__main__':
    unittest.main()
