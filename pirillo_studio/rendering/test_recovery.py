"""Recovery regressions from rendering seam 2f's accepted-artifact review."""
import copy
import fcntl
import os
import shutil
import unittest
from unittest.mock import patch

from ..captions import cues
from ..spine.files import SpineError, digest, filehash
from . import records, test_compositor
from .render import render
from .test_rendering import PowerLoss


class Recovery(unittest.TestCase):
    """defect: seam 2f items 1, 2, 4 and 8; accepted outputs survive replacement and restart."""

    @classmethod
    def setUpClass(cls):
        test_compositor.Compositor.setUpClass()

    def setUp(self):
        self.case = test_compositor.Compositor('test_jcut_decoded_picture_voice_captions_and_reuse')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.accepted = self.case.accepted()

    def test_cached_generated_artifacts_make_no_backup_copies(self):
        paths = [self.accepted['file'], *self.accepted['request']['generated']]
        before = {p: (filehash(self.case.folder / p), (self.case.folder / p).stat().st_mtime_ns)
                  for p in paths if (self.case.folder / p).exists()}
        with patch.object(records.shutil, 'copyfile', wraps=shutil.copyfile) as copied:
            again = self.case.accepted()
            self.assertEqual(again['file_sha256'], self.accepted['file_sha256'])
        copied.assert_not_called()
        self.assertEqual(before, {p: (filehash(self.case.folder / p), (self.case.folder / p).stat().st_mtime_ns)
                                 for p in before})
        self.assertFalse(list(self.case.folder.rglob('.render-*')))
        self._assert_imported_cache()

    def test_three_artifact_predecessor_can_render(self):
        # defect: c0dd9c7d4b2f4d5e9b8f763facd96942, seam 2e records refused forever.
        predecessor = copy.deepcopy(self.accepted)
        del predecessor['request']['generated'][cues.artifact_names('draft')[3]]
        records.write(self.case.v, records.name(predecessor['request']), predecessor)
        result = self.case.accepted()
        self.assertEqual(set(result['request']['generated']), set(cues.artifact_names('draft')))
        self.assertFalse(list(self.case.folder.rglob('.render-*')))

    def test_missing_and_corrupt_generated_bytes_never_become_accepted_identity(self):
        expected = self.accepted['request']['generated']
        for artifact, raw in [('Draft/draft.srt', None), ('records/render/draft-voice.wav', b'corrupt')]:
            with self.subTest(artifact=artifact):
                target = self.case.folder / artifact
                original = target.read_bytes()
                if raw is None:
                    target.unlink()
                else:
                    target.write_bytes(raw)
                req = {**self.accepted['request'], 'key': 'replacement'}
                saved = records.preserve(self.case.v, req, self.accepted)
                self.assertEqual(saved['artifacts'], expected)
                self.assertEqual(saved['current'][artifact], filehash(target))
                self.assertFalse((self.case.folder / records.backup_name({**req, 'file': artifact},
                                 expected[artifact])).exists())
                target.write_bytes(b'attempt wrote this')
                records.restore(self.case.v, req, saved)
                self.assertEqual(target.read_bytes() if target.exists() else None, raw)
                target.write_bytes(original)
                records.retire(self.case.v, req, saved)
                records.write(self.case.v, records.name(req), self.accepted)

    def test_restart_after_committed_record_retires_without_restoring(self):
        drifted = self.case.folder / 'Draft/draft.srt'
        drifted.write_bytes(b'provider revision before the attempt')
        self.case.two_spans()
        write = records.write
        def crash(video, name, value):
            write(video, name, value)
            if name == 'records/render/draft.json' and 'file_sha256' in value:
                raise PowerLoss()
        with patch.object(records, 'write', side_effect=crash), self.assertRaises(PowerLoss):
            render(self.case.v, 'draft')
        committed = self.case.saved('draft')
        self.assertNotEqual(committed['file_sha256'], self.accepted['file_sha256'])
        names = [committed['file'], committed['readings'], *committed['request']['generated']]
        hashes = {n: filehash(self.case.folder / n) for n in names}
        self.assertTrue(list(self.case.folder.rglob('.render-*')))
        with patch.object(records, 'restore', side_effect=AssertionError('committed output cannot roll back')):
            recovered = records.recover(self.case.v, committed['request'], committed)
        self.assertEqual(recovered['file_sha256'], committed['file_sha256'])
        self.assertEqual(hashes, {n: filehash(self.case.folder / n) for n in names})
        self.assertFalse(list(self.case.folder.rglob('.render-*')))
        records.recover(self.case.v, committed['request'], self.case.saved('draft'))

    def test_caption_rollback_holds_existing_caption_writer_lock(self):
        req = {**self.accepted['request'], 'key': 'replacement'}
        previous = records.preserve(self.case.v, req, self.accepted)
        caption = self.case.folder / cues.artifact_names('draft')[1]
        original = caption.read_bytes()
        caption.write_bytes(b'failed candidate')
        replace = os.replace
        checked = []
        def observe(source, target):
            if target == caption:
                with (self.case.folder / 'records/spine.lock').open('a') as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                checked.append(True)
            return replace(source, target)
        with patch.object(records.os, 'replace', side_effect=observe):
            records.restore(self.case.v, req, previous)
        self.assertEqual(checked, [True])
        self.assertEqual(caption.read_bytes(), original)

    def test_bad_recovery_identity_refuses_before_restore_or_retirement(self):
        req = {**self.accepted['request'], 'key': 'replacement'}
        previous = records.preserve(self.case.v, req, self.accepted)
        target = self.case.folder / req['file']
        target.write_bytes(b'uncommitted candidate')
        before = {p: filehash(p) for p in self.case.folder.rglob('*') if p.is_file()}
        artifact = cues.artifact_names('draft')[1]
        for defect in ('missing', 'stale', 'malformed', 'foreign', 'duplicate',
                       'current-missing', 'current-foreign', 'current-malformed'):
            with self.subTest(defect=defect):
                broken = copy.deepcopy(previous)
                if defect == 'missing':
                    del broken['artifacts'][artifact]
                elif defect == 'stale':
                    broken['artifacts'][artifact] = digest(b'stale')
                elif defect == 'malformed':
                    broken['artifacts'][artifact] = broken['record']['request']['generated'][artifact] = 'bad'
                elif defect == 'current-missing':
                    del broken['current']
                elif defect == 'current-foreign':
                    broken['current']['owner.txt'] = digest(b'owner original')
                elif defect == 'current-malformed':
                    broken['current'][artifact] = float('nan')
                else:
                    broken['unavailable'] = ['owner.txt'] if defect == 'foreign' else [artifact, artifact]
                for operation in (records.restore, records.retire):
                    with self.assertRaises(SpineError):
                        operation(self.case.v, req, broken)
                    self.assertEqual(before, {p: filehash(p) for p in self.case.folder.rglob('*') if p.is_file()})

        records.restore(self.case.v, req, previous)
        records.retire(self.case.v, req, previous)
        self._assert_generated_paths()

    def _assert_generated_paths(self):
        req = {**self.accepted['request'], 'key': 'replacement'}
        target = self.case.folder / cues.artifact_names('draft')[1]
        original = target.read_bytes()
        target.unlink()
        target.symlink_to(self.case.folder / 'owner.txt')
        with self.assertRaisesRegex(SpineError, 'symlink'):
            records.preserve(self.case.v, req, self.accepted)
        target.unlink()
        target.write_bytes(original)
        alias = target.with_name('DRAFT.json')
        target.rename(alias)
        with self.assertRaisesRegex(SpineError, 'path alias'):
            records.preserve(self.case.v, req, self.accepted)
        self.assertEqual((self.case.folder / 'owner.txt').read_bytes(), b'owner original')
        self.assertFalse(list(self.case.folder.rglob('.render-*')))

    def test_post_commit_writer_error_keeps_new_output(self):
        self.case.two_spans()
        write = records.write
        def fail(video, name, value):
            write(video, name, value)
            if name == 'records/render/draft.json' and 'file_sha256' in value:
                raise OSError('writer failed after committing')
        with patch.object(records, 'write', side_effect=fail), self.assertRaises(SpineError):
            render(self.case.v, 'draft')
        committed = self.case.saved('draft')
        self.assertNotEqual(committed['file_sha256'], self.accepted['file_sha256'])
        self.assertEqual(filehash(self.case.folder / committed['file']), committed['file_sha256'])
        records.recover(self.case.v, committed['request'], committed)
        self.assertFalse(list(self.case.folder.rglob('.render-*')))


    def _assert_imported_cache(self):
        case = self.case
        case.ffmpeg, case.ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        case.v.stage, case.v.measure = 'short', case.measure
        case.put('records/job.json', {'recipe': 'shorts-only', 'source': 'source.mp4', 'short_id': 'one'})
        accepted = render(case.v, 'short:one')
        self.assertEqual(accepted['mode'], 'measure_existing')
        self.assertEqual(accepted['state'], 'delivered')
        target = case.folder / accepted['file']
        original = target.stat()
        with patch.object(records.shutil, 'copyfile', wraps=shutil.copyfile) as copied:
            again = render(case.v, 'short:one')
        copied.assert_not_called()
        self.assertEqual(again, accepted)
        self.assertEqual(target.stat().st_ino, original.st_ino)
        self.assertEqual(target.stat().st_mtime_ns, original.st_mtime_ns)
        self.assertEqual(target.read_bytes(), case.media)
        self.assertFalse(list(case.folder.rglob('.render-*')))
