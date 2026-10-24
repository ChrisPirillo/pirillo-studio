"""Synthetic finished files prove decoded measurements; no renderer or semantic observations are fabricated."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pirillo_studio.rendering import records, request
from pirillo_studio.rendering.render import MeasurementRefused, Video, measured, render
from pirillo_studio.spine.files import SpineError, filehash


class Measurements(unittest.TestCase):
    """e2e: Shorts/sample.mp4 and records/render/short-sample-readings.json from decoded lavfi media."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        (self.folder / 'records').mkdir()
        (self.folder / 'records/job.json').write_text(json.dumps({'recipe': 'shorts-only', 'short_id': 'sample', 'source': 'source.mp4'}))
        self.media = self.folder / 'source.mp4'
        self.make_media()
        self.settings = request.settings()
        self.settings['shared']['video']['sizes'] = {'wide': [160, 90], 'tall': [90, 160]}
        self.video = Video(self.folder, 'short', versions={'encoder': 'fixture'},
                           preflight={'ok': True, 'state': 'ready', 'source_sha256': filehash(self.media)},
                           encode=self.no_encode, settings=self.settings)

    def no_encode(self, req, path):
        self.fail('a supplied short must not encode')

    def make_media(self, *, video='testsrc2=size=90x160:rate=30', audio='sine=frequency=440:sample_rate=48000',
                   vf='null', af='volume=2.5', options=(), duration='1', tagged=True):
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i', video,
                        '-f', 'lavfi', '-i', audio, '-t', duration, '-vf',
                        vf + ',setsar=1' + (',setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709' if tagged else ''),
                        '-af', af, '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                        *(['-x264-params', 'colorprim=bt709:transfer=bt709:colormatrix=bt709'] if tagged else []),
                        '-c:a', 'aac', *options, str(self.media)], check=True, capture_output=True)
        if hasattr(self, 'video'):
            self.video.preflight['source_sha256'] = filehash(self.media)

    def req(self):
        return request.resolve(self.video, 'short', 'short:sample')

    def read(self):
        req = self.req()
        return self.video.measure(req, self.media)

    def test_delivered_files_meet_named_profiles(self):
        for profile in self.settings['profiles']:
            with self.subTest(profile=profile):
                shape = '90x160' if profile in ('vertical', 'short') else '160x90'
                self.make_media(video=f'testsrc2=size={shape}:rate=30')
                req = self.req()
                req['settings'] = request._profile(self.video, profile, request.Inputs(self.video))
                value = measured(self.video, req, 'source.mp4')
                self.assertEqual(value['fps'], '30/1')
                self.assertEqual(value['checks']['count']['value']['decoded'], 30)
                self.assertEqual(value['checks']['color']['coverage']['frames'], 30)
                self.assertTrue(all(value['checks'][name]['ok'] for name in req['settings']['checks']))
        before = self.media.read_bytes()
        accepted = render(self.video, 'short:sample')
        self.assertEqual(accepted['state'], 'delivered', accepted)
        stored = json.loads((self.folder / records.readings_name(self.req())).read_text())
        self.assertEqual(stored['file_sha256'], accepted['file_sha256'])
        self.assertEqual((self.folder / accepted['file']).read_bytes(), before)
        self.assertEqual(self.media.read_bytes(), before)
        self.assertEqual(render(self.video, 'short:sample'), accepted)

    def test_measure_refusal_names_reading_and_setting(self):
        self.settings['shared']['video']['max_bytes'] = 1
        refused = render(self.video, 'short:sample')
        self.assertEqual(refused['state'], 'measure_refused', refused)
        self.assertEqual(refused['reading'], 'file_cap')
        self.assertEqual(refused['setting'], 'rendering.settings#video.max_bytes')
        self.assertFalse((self.folder / 'Shorts/sample.mp4').exists())
        for missing in ('captions', 'subject', 'unknown-check'):
            with self.subTest(reading=missing):
                req = self.req()
                req['settings']['checks'] = [missing]
                with self.assertRaises(MeasurementRefused) as caught:
                    measured(self.video, req, 'source.mp4')
                self.assertEqual(caught.exception.reading, missing)

    def test_evidence_and_layers_follow_output_bytes(self):
        original = self.read()
        self.make_media(vf='hflip')
        changed = self.read()
        self.assertNotEqual(original['file_sha256'], changed['file_sha256'])
        self.assertNotEqual(original['key'], changed['key'])
        for name in ('cards', 'context', 'captions', 'subject'):
            row = changed['checks'][name]
            self.assertIsNone(row['value'])
            self.assertEqual(row['result'], 'unavailable')
            self.assertEqual(row['coverage']['frames'], 0)
            self.assertIn(changed['file_sha256'], row['evidence'])
        req = self.req()
        req['tools']['ffmpeg'] = 'stale'
        with self.assertRaises(MeasurementRefused) as caught:
            self.video.measure(req, self.media)
        self.assertEqual(caught.exception.reading, 'tools.ffmpeg')
        del req['key']
        req['tools'] = self.video.versions
        with self.assertRaises(MeasurementRefused) as caught:
            self.video.measure(req, self.media)
        self.assertEqual(caught.exception.reading, 'key')

    def test_locked_plan_is_the_viewed_draft(self):
        # Measurement half: exact decoded clock exists; no fabricated lock/pixel correspondence.
        value = self.read()
        picture = next(s for s in value['streams'] if s['kind'] == 'video')
        self.assertEqual(picture['decoded_frames'], 30)
        self.assertEqual(picture['decoded_start_s'], 0)
        self.assertAlmostEqual(picture['decoded_end_s'], 1)
        self.assertIsNone(value['checks']['frame-map']['value'])
        self.assertFalse(value['checks']['frame-map']['ok'])

    def test_masters_match_approved_draft(self):
        # Measurement half: bytes/measurements follow actual picture and sound; approval is not inferred.
        original = self.read()
        self.make_media(af='volume=1')
        changed = self.read()
        self.assertNotEqual(original['loudness_lufs'], changed['loudness_lufs'])
        self.assertFalse(changed['checks']['loudness_lufs']['ok'])
        self.assertNotEqual(original['file_sha256'], changed['file_sha256'])
        self.assertIsNone(changed['checks']['draft-match']['value'])

    def test_vertical_keeps_subject_every_frame(self):
        value = self.read()
        row = value['checks']['subject']
        self.assertFalse(row['ok'])
        self.assertEqual(row['coverage']['unseen'], [[0, 1]])
        req = self.req()
        req['settings']['checks'] = ['subject']
        with self.assertRaises(MeasurementRefused) as caught:
            measured(self.video, req, 'source.mp4')
        self.assertEqual(caught.exception.reading, 'subject')

    def test_each_short_has_its_own_complete_ends(self):
        complete = self.read()
        self.assertTrue(complete['checks']['av']['ok'])
        self.make_media(af='volume=2.5,atrim=end=0.6')
        short = self.read()
        self.assertFalse(short['checks']['av']['ok'])
        self.assertLess(short['checks']['av']['value']['offsets_s']['end'], -0.3)
        self.assertIsNone(short['checks']['word-ends']['value'])

    def test_delivered_mix_is_the_measured_mix(self):
        normal = self.read()
        self.make_media(af="volume=2.5,volume=0:enable='between(t,0.3,0.7)'")
        hole = self.read()
        self.assertFalse(hole['checks']['silence']['ok'])
        self.assertTrue(hole['checks']['silence']['value'])
        self.assertNotEqual(normal['loudness_lufs'], hole['loudness_lufs'])
        self.assertIsNone(hole['checks']['bed']['value'])
        self.make_media(af='volume=0')
        with self.assertRaises(MeasurementRefused) as caught:
            self.read()
        self.assertEqual(caught.exception.reading, 'true_peak_dbtp')

    def test_decoded_off_grid_gap_hold_and_flash_refuse(self):
        cases = [('clock', {'vf': "setpts='if(gte(N,15),PTS+1,PTS)'", 'options': ('-fps_mode','passthrough') }),
                 ('av', {'af': "volume=2.5,aselect='not(between(t,0.3,0.6))'"}),
                 ('held_tail', {'video': 'color=black:size=90x160:rate=30'}),
                 ('flash', {'video': "color=black:size=90x160:rate=30,drawbox=color=white:t=fill:enable='eq(n,15)'"})]
        for reading, kwargs in cases:
            with self.subTest(reading=reading):
                self.make_media(**kwargs)
                value = self.read()
                self.assertFalse(value['checks'][reading]['ok'], value['checks'][reading])
                req = self.req()
                req['settings']['checks'] = [reading]
                with self.assertRaises(MeasurementRefused) as caught:
                    measured(self.video, req, 'source.mp4')
                self.assertEqual(caught.exception.reading, reading)

    def test_partial_decode_and_unknown_color_refuse(self):
        self.make_media(options=('-movflags', '+faststart'))
        self.media.write_bytes(self.media.read_bytes()[:-900])
        self.video.preflight['source_sha256'] = filehash(self.media)
        with self.assertRaises(MeasurementRefused) as caught:
            self.read()
        self.assertEqual(caught.exception.reading, 'decode')
        self.make_media(tagged=False)
        value = self.read()
        self.assertFalse(value['checks']['color']['ok'])

    def test_nonfinite_missing_tool_and_path_alias_refuse(self):
        req = self.req()
        req['settings']['measurement']['av_tolerance_s'] = float('nan')
        with self.assertRaises(MeasurementRefused):
            self.video.measure(req, self.media)
        req = self.req()
        from pirillo_studio.rendering import decoded
        original_probe = decoded.probe
        for invalid in (None, 'NaN', '0/0'):
            def bad_clock(path, entries):
                value = original_probe(path, entries)
                value['streams'][0]['time_base'] = invalid
                return value
            with self.subTest(time_base=invalid), patch.object(decoded, 'probe', side_effect=bad_clock):
                with self.assertRaises(MeasurementRefused) as caught:
                    self.video.measure(req, self.media)
                self.assertEqual(caught.exception.reading, 'video.time_base')
        with patch('pirillo_studio.rendering.decoded.shutil.which', return_value=None):
            with self.assertRaises(MeasurementRefused) as caught:
                self.video.measure(req, self.media)
        self.assertEqual(caught.exception.reading, 'tools.ffmpeg')
        alias = self.folder / 'alias.mp4'
        alias.symlink_to(self.media)
        with self.assertRaisesRegex(SpineError, 'symlink'):
            self.video.measure(req, alias)

    def test_mutation_during_decode_cannot_publish(self):
        from pirillo_studio.rendering import decoded
        signals = decoded.signals
        def mutate(path, policy):
            result = signals(path, policy)
            with path.open('ab') as handle:
                handle.write(b'changed')
            return result
        with patch.object(decoded, 'signals', side_effect=mutate):
            refused = render(self.video, 'short:sample')
        self.assertEqual(refused['state'], 'measure_refused', refused)
        self.assertEqual(refused['reading'], 'file_sha256')
        self.assertFalse((self.folder / 'Shorts/sample.mp4').exists())
