"""Contract 10 measures a finished mix, never a reconstructed approval."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pirillo_studio.rendering import decoded, request, sound
from pirillo_studio.rendering.render import MeasurementRefused
from pirillo_studio.spine.files import filehash


class DeliveredMix(unittest.TestCase):
    """e2e: delivered.mkv contains independently decoded speech and bound bed audio."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.video = SimpleNamespace(folder=self.folder)
        self.stem = self.folder / 'stem.wav'
        decoded.run('ffmpeg', ['-v', 'error', '-f', 'lavfi', '-i',
                    'anoisesrc=color=white:amplitude=0.1:sample_rate=8000:seed=73:duration=2',
                    '-c:a', 'pcm_s16le', str(self.stem)], 'fixture')
        self.req = {'file': 'delivered.mkv', 'inputs': {'stem.wav': filehash(self.stem)},
                    'settings': {'measurement': request.settings()['shared']['measurement']},
                    'audio_binding': {'stem': {'file': 'stem.wav', 'sha256': filehash(self.stem)},
                                      'offset_s': 0, 'gain_db': 0, 'tacets': []}}

    def mix(self, bed='anull'):
        path = self.folder / 'delivered.mkv'
        decoded.run('ffmpeg', ['-v', 'error', '-y', '-i', str(self.stem),
                    '-f', 'lavfi', '-i', 'sine=frequency=1200:sample_rate=8000:duration=2',
                    '-f', 'lavfi', '-i', 'color=size=16x16:rate=4:duration=2',
                    '-filter_complex', f'[0:a]{bed}[bed];[bed][1:a]amix=normalize=0[a]',
                    '-map', '2:v', '-map', '[a]', '-t', '2', '-c:v', 'ffv1',
                    '-c:a', 'pcm_s16le', str(path)], 'fixture')
        return path

    def test_delivered_mix_is_the_measured_mix(self):
        result = sound.measure(self.video, self.req, self.mix())['bed']
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['value']['units'], 'dBFS (normalized PCM RMS); gain dB; correlation dimensionless')
        self.assertGreater(result['value']['windows'][0]['stem_dbfs'], -30)
        self.assertLess(result['value']['windows'][0]['stem_dbfs'], -25)
        self.assertEqual(result['value']['files']['stem.wav'], filehash(self.stem))
        for filter_ in ('volume=0', 'adelay=80:all=1', "volume=0:enable='between(t,0.5,1.0)'", 'volume=0.1'):
            with self.subTest(filter=filter_):
                result = sound.measure(self.video, self.req, self.mix(filter_))['bed']
                self.assertFalse(result['ok'], result)
        self.req['audio_binding']['tacets'] = [[0.25, 1.25]]
        result = sound.measure(self.video, self.req,
                               self.mix("volume=0:enable='between(t,0.5,0.75)'"))['bed']
        self.assertTrue(result['ok'], result)

    def test_missing_stale_and_invalid_bindings_refuse(self):
        path = self.mix()
        for mutate in (lambda r: r.pop('audio_binding'),
                       lambda r: r['inputs'].clear(),
                       lambda r: r['audio_binding']['stem'].update(sha256='sha256:stale'),
                       lambda r: r['audio_binding'].update(gain_db=float('nan')),
                       lambda r: r['audio_binding'].update(offset_s='seconds'),
                       lambda r: r['audio_binding'].update(tacets=[[1, 0]])):
            req = copy.deepcopy(self.req)
            mutate(req)
            with self.assertRaises(MeasurementRefused):
                sound.measure(self.video, req, path)
        self.stem.write_bytes(b'changed')
        with self.assertRaises(MeasurementRefused):
            sound.measure(self.video, self.req, path)

    def test_offset_and_changed_bytes_are_measured(self):
        self.req['audio_binding'].update(offset_s=0.25, tacets=[[0, 0.25]])
        path = self.mix('adelay=250:all=1')
        self.assertTrue(sound.measure(self.video, self.req, path)['bed']['ok'])
        original = sound._pcm

        def mutate(source, destination, policy):
            original(source, destination, policy)
            if source == self.stem:
                self.stem.write_bytes(b'changed after decode')

        with patch.object(sound, '_pcm', side_effect=mutate):
            with self.assertRaisesRegex(MeasurementRefused, 'changed during measurement'):
                sound.measure(self.video, self.req, path)

    def compare(self, path, segments):
        return sound.compare_audio(self.video, self.req, path, self.req['audio_binding']['stem'], segments)

    def audio_output(self, filter_):
        path = self.folder / 'output.wav'
        decoded.run('ffmpeg', ['-v', 'error', '-y', '-i', str(self.stem), '-af', filter_,
                    '-c:a', 'pcm_s16le', str(path)], 'fixture')
        return path

    def test_masters_match_approved_draft(self):
        mapping = [{'src': [0, 2], 'out': [0, 2]}]
        self.assertTrue(self.compare(self.audio_output('anull'), mapping)['ok'])
        for filter_ in ('atrim=end=1.75', 'volume=0', 'adelay=100:all=1', 'areverse'):
            with self.subTest(filter=filter_):
                self.assertFalse(self.compare(self.audio_output(filter_), mapping)['ok'])

    def test_each_short_has_its_own_complete_ends(self):
        mapping = [{'src': [0.5, 1.5], 'out': [0, 1]}]
        self.assertTrue(self.compare(self.audio_output('atrim=start=0.5:end=1.5'), mapping)['ok'])
        self.assertFalse(self.compare(self.audio_output('atrim=start=0.5:end=1.4'), mapping)['ok'])
        self.assertFalse(self.compare(self.audio_output('atrim=start=0.6:end=1.6'), mapping)['ok'])
        self.assertFalse(self.compare(self.audio_output('atrim=start=0.5:end=1.5'),
                                     [{'src': [0.5, 1.25], 'out': [0, 0.75]}])['ok'])

    def test_evidence_is_publishable_and_channels_are_not_discarded(self):
        result = sound.measure(self.video, self.req, self.mix())['bed']
        self.assertIsInstance(result['evidence'], str)
        reference = self.folder / 'stereo.wav'
        decoded.run('ffmpeg', ['-v', 'error', '-i', str(self.stem), '-af',
                    'pan=stereo|c0=c0|c1=-1*c0', '-c:a', 'pcm_s16le', str(reference)], 'fixture')
        self.req['inputs']['stereo.wav'] = filehash(reference)
        self.req['audio_binding']['stem'] = {'file': 'stereo.wav', 'sha256': filehash(reference)}
        output = self.folder / 'stereo-output.wav'
        mapping = [{'src': [0, 2], 'out': [0, 2]}]
        for filter_, expected in [('anull', True), ('volume=0', False)]:
            decoded.run('ffmpeg', ['-v', 'error', '-y', '-i', str(reference), '-af', filter_,
                        '-c:a', 'pcm_s16le', str(output)], 'fixture')
            check = self.compare(output, mapping)
            self.assertEqual(check['ok'], expected, check)
            self.assertIsInstance(check['evidence'], str)

    def test_non_tacet_stem_coverage_cannot_be_padded(self):
        path = self.mix()
        shortened = self.folder / 'short-stem.wav'
        decoded.run('ffmpeg', ['-v', 'error', '-i', str(self.stem), '-af', 'atrim=end=1.999',
                    '-c:a', 'pcm_s16le', str(shortened)], 'fixture')
        self.req['audio_binding']['stem'] = {'file': 'short-stem.wav', 'sha256': filehash(shortened)}
        self.req['inputs']['short-stem.wav'] = filehash(shortened)
        with self.assertRaisesRegex(MeasurementRefused, 'bed.*coverage'):
            sound.measure(self.video, self.req, path)
        self.req['audio_binding']['stem'] = {'file': 'stem.wav', 'sha256': filehash(self.stem)}
        self.req['audio_binding']['offset_s'] = 0.000125
        with self.assertRaisesRegex(MeasurementRefused, 'bed.*coverage'):
            sound.measure(self.video, self.req, path)

    def test_approved_draft_audio_must_be_consumed_completely(self):
        path = self.audio_output('atrim=end=1.75')
        mapping = [{'src': [0, 1.75], 'out': [0, 1.75]}]
        asset = self.req['audio_binding']['stem']
        self.assertTrue(sound.compare_audio(self.video, self.req, path, asset, mapping)['ok'])
        self.assertFalse(sound.compare_audio(self.video, self.req, path, asset, mapping,
                                              complete_reference=True)['ok'])
        path = self.audio_output('anull')
        self.assertTrue(sound.compare_audio(self.video, self.req, path, asset,
                                            [{'src': [0, 2], 'out': [0, 2]}], complete_reference=True)['ok'])
