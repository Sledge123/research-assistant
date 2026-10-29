"""验证真实接收函数在独立挂载和落盘失败时的原子发布。"""
import base64
import errno
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from highscore_downloader.archive import ArchiveError, ArchiveStore
from highscore_web.artifacts import accept_zip, checked_zip
import test_archive

ROOT = Path(__file__).resolve().parents[1]


class ArtifactMountTests(unittest.TestCase):
    def setUp(self):
        temporary = ROOT / '.cache/tmp'
        temporary.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary, prefix='artifact-mount-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = self.root / 'synthetic-source'
        source.mkdir()
        rtf = source / 'synthetic.rtf'
        rtf.write_bytes(b'{\\rtf1 synthetic}')
        receipt = ArchiveStore(source).save(test_archive.synthetic_card(), rtf, '09-999-0001')
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as archive:
            for relative in [receipt['receipt_path'], *(item['path'] for item in receipt['files'].values())]:
                archive.write(source / relative, relative)
        self.encoded = base64.b64encode(data.getvalue()).decode('ascii')
        self.expected = [{'code': '09-999-0001'}]
        self.destination = self.root / 'output/web/artifacts/synthetic-job'

    def test_upload_succeeds_with_separate_cache_and_artifact_mounts(self):
        rename = Path.rename
        moves = []

        def separate_mounts(source, destination):
            destination = Path(destination)
            moves.append((source, destination))
            cache = self.root / '.cache'
            artifacts = self.root / 'output/web/artifacts'
            if source.is_relative_to(cache) and destination.is_relative_to(artifacts):
                raise OSError(errno.EXDEV, 'synthetic separate Docker bind mounts')
            return rename(source, destination)

        with patch.object(Path, 'rename', separate_mounts):
            relative, receipts = accept_zip(self.root, 'synthetic-job', self.encoded, self.expected)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0][0].parent, self.destination.parent)
        self.assertEqual(receipts[0]['peak_count'], 2)
        with zipfile.ZipFile(io.BytesIO(checked_zip(self.root, relative, self.expected))) as archive:
            self.assertEqual(len(archive.namelist()), 5)
        self.assertEqual(list(self.destination.parent.glob('upload-*')), [])

    def test_zip_write_failure_leaves_no_published_or_staging_archive(self):
        write_bytes = Path.write_bytes

        def failing_zip_write(path, payload):
            if path.name == 'artifact.zip':
                raise OSError(errno.ENOSPC, 'synthetic disk full')
            return write_bytes(path, payload)

        with patch.object(Path, 'write_bytes', failing_zip_write):
            with self.assertRaises(ArchiveError):
                accept_zip(self.root, 'synthetic-job', self.encoded, self.expected)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list((self.root / 'output/web/artifacts').glob('upload-*')), [])
        self.assertEqual(list((self.root / '.cache/web-upload').glob('upload-*')), [])


if __name__ == '__main__':
    unittest.main()
