"""Real output contracts for the processed voice and approved music seam."""
import copy
import unittest
from unittest.mock import patch

from ..spine.files import SpineError, filehash
from . import decoded, records, request, sound, test_compositor
from .render import MeasurementRefused, measured, render
from .test_rendering import PowerLoss


class Mix(unittest.TestCase):
    """e2e: Draft/draft.mp4 retains its processed voice, actual bed and final mix."""

    @classmethod
    def setUpClass(cls):
        test_compositor.Compositor.setUpClass()

    def setUp(self):
        self.case = test_compositor.Compositor('test_jcut_decoded_picture_voice_captions_and_reuse')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.bed = self.case.folder / 'bed.wav'
        self.make_bed(73)
        self.brief = self.case.get('records/bed.json')
        self.brief.update(assets=[{'file': 'bed.wav', 'sha256': filehash(self.bed)}],
                          decision='approve', tacets=[])
        self.save_bed()

    def make_bed(self, seed):
        decoded.run('ffmpeg', ['-v', 'error', '-y', '-f', 'lavfi', '-i',
                    f'anoisesrc=color=white:amplitude=0.1:sample_rate=48000:seed={seed}:duration=1',
                    '-c:a', 'pcm_s16le', str(self.bed)], 'fixture')

    def save_bed(self):
        self.case.put('records/bed.json', self.brief)
        self.case.save_cut()

    def resolve(self):
        return request.resolve(self.case.v, 'draft', 'draft')

    def test_delivered_mix_binds_and_measures_actual_retained_stem(self):
        self.brief['tacets'] = [[.213, .407]]
        self.save_bed()
        result = self.case.accepted()
        req = result['request']
        self.assertTrue('bed_binding' in req, 'output must bind its bed brief')
        self.assertEqual(req['bed_binding']['mode'], 'bed')
        self.assertEqual(req['bed_binding']['brief']['sha256'], filehash(self.case.folder / 'records/bed.json'))
        self.assertEqual(req['bed_binding']['assets'][0]['sha256'], filehash(self.bed))
        for binding in (req['voice_binding'], req['mix_binding'], req['audio_binding']['stem']):
            self.assertEqual(req['generated'][binding['file']], binding['sha256'])
            self.assertEqual(filehash(self.case.folder / binding['file']), binding['sha256'])
        self.assertEqual(req['audio_binding']['gain_db'], 0)
        self.assertEqual(req['audio_binding']['offset_s'], 0)
        reading = sound.measure(self.case.v, req, self.case.folder / result['file'])['bed']
        self.assertTrue(reading['ok'], reading)
        self.assertEqual(reading['value']['files'][req['audio_binding']['stem']['file']],
                         req['audio_binding']['stem']['sha256'])
        missing_mix = copy.deepcopy(req)
        del missing_mix['mix_binding']
        with self.assertRaisesRegex(MeasurementRefused, 'mix_binding'):
            measured(self.case.v, missing_mix, result['file'])
        missing_hash = copy.deepcopy(req)
        del missing_hash['audio_binding']['stem']['sha256']
        with self.assertRaises(MeasurementRefused):
            sound.measure(self.case.v, missing_hash, self.case.folder / result['file'])
        (self.case.folder / req['audio_binding']['stem']['file']).unlink()
        with self.assertRaises(MeasurementRefused):
            sound.measure(self.case.v, req, self.case.folder / result['file'])

    def test_bed_bytes_brief_and_processing_change_identity_and_voice(self):
        first = self.case.accepted()['request']
        self.make_bed(74)
        self.brief['assets'][0]['sha256'] = filehash(self.bed)
        self.save_bed()
        changed = self.resolve()
        self.assertNotEqual(first['key'], changed['key'])
        self.brief['request'] = 'quieter intent'
        self.save_bed()
        revised = self.resolve()
        self.assertNotEqual(changed['key'], revised['key'])
        self.assertTrue('mix' in self.case.settings['shared'], 'versioned mix policy is required')
        policy = self.case.settings['shared']['mix']
        policy['voice_chain'] = ['volume=0.5']
        policy['version'] += '-processed'
        processed = self.case.accepted()['request']
        self.assertNotEqual(revised['key'], processed['key'])
        self.assertNotEqual(first['voice_binding']['sha256'], processed['voice_binding']['sha256'])
        self.assertEqual(processed['voice_binding']['processing_chain'], policy['voice_chain'])
        self.assertEqual(processed['voice_binding']['settings_version'], policy['version'])

    def test_no_asset_is_explicit_voice_only(self):
        del self.brief['assets']
        self.save_bed()
        result = self.case.accepted()
        req = result['request']
        self.assertTrue('bed_binding' in req, 'output must bind its bed brief')
        self.assertEqual(req['bed_binding']['mode'], 'voice_only')
        self.assertNotIn('audio_binding', req)
        self.assertIsNone(req['generated']['records/render/draft-bed.wav'])
        self.assertEqual(req['mix_binding']['sha256'], req['voice_binding']['sha256'])

    def test_named_invalid_bed_and_nonfinite_policy_refuse_before_writing(self):
        original = copy.deepcopy(self.brief)
        for defect in ('missing', 'unapproved', 'stale', 'symlink', 'alias'):
            with self.subTest(defect=defect):
                self.brief = copy.deepcopy(original)
                if defect == 'missing':
                    self.brief['assets'][0]['file'] = 'missing.wav'
                elif defect == 'unapproved':
                    self.brief['decision'] = 'reject'
                elif defect == 'stale':
                    self.brief['assets'][0]['sha256'] = 'sha256:' + '0' * 64
                elif defect == 'symlink':
                    (self.case.folder / 'linked.wav').symlink_to(self.bed)
                    self.brief['assets'][0]['file'] = 'linked.wav'
                else:
                    self.brief['assets'][0]['file'] = './bed.wav'
                self.save_bed()
                with self.assertRaisesRegex(SpineError, 'bed|missing.wav|linked.wav'):
                    self.resolve()
        self.brief = original
        self.save_bed()
        self.assertTrue('mix' in self.case.settings['shared'], 'versioned mix policy is required')
        self.case.settings['shared']['mix']['bed_loudness_lufs'] = float('nan')
        with self.assertRaises(SpineError):
            self.resolve()
        self.assertFalse((self.case.folder / 'records/render/draft-mix.wav').exists())

    def test_crash_after_mix_restores_previous_actual_audio(self):
        self.assertTrue('mix' in self.case.settings['shared'], 'versioned mix policy is required')
        self.case.settings['shared']['mix']['voice_chain'] = ['volume=0']
        self.case.settings['shared']['mix']['bed_loudness_lufs'] -= 6
        accepted = self.case.accepted()
        req = accepted['request']
        self.assertTrue('audio_binding' in req, 'output must bind its retained stem')
        paths = [req['audio_binding']['stem']['file'], req['mix_binding']['file']]
        before = {p: filehash(self.case.folder / p) for p in paths}
        self.assertEqual(before[paths[0]], before[paths[1]], 'silent voice makes identical bed/mix bytes')
        self.make_bed(75)
        self.brief['assets'][0]['sha256'] = filehash(self.bed)
        self.save_bed()
        run = decoded.run

        def interrupt(tool, args, reading):
            if reading == 'encode':
                self.assertTrue(all((self.case.folder / p).is_file() for p in paths))
                self.assertNotEqual(before, {p: filehash(self.case.folder / p) for p in paths})
                raise PowerLoss()
            return run(tool, args, reading)

        with patch.object(decoded, 'run', side_effect=interrupt), self.assertRaises(PowerLoss):
            render(self.case.v, 'draft')
        pending = self.case.saved('draft')
        incomplete = copy.deepcopy(pending['previous'])
        for name in paths:
            del incomplete['current'][name]
        with self.assertRaisesRegex(SpineError, 'current artifacts'):
            records.restore(self.case.v, pending['request'], incomplete)
        records.recover(self.case.v, pending['request'], pending)
        self.assertEqual(before, {p: filehash(self.case.folder / p) for p in paths})
        self.assertEqual(filehash(self.case.folder / accepted['file']), accepted['file_sha256'])
        self.case.accepted()
        self.assertFalse(list(self.case.folder.rglob('.render-*')))
