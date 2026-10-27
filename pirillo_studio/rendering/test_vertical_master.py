"""Native vertical delivery preserves approved clocks with its own measured crop."""
import math
import subprocess
import unittest
import wave
from unittest.mock import patch

from ..captions import cues
from ..spine.files import SpineError, filehash
from . import compositor, records, test_long_master
from .render import render
from .test_rendering import PowerLoss


class VerticalMaster(unittest.TestCase):
    """e2e: Vertical/main.mp4, complete decoded picture, voice, sidecar and recovery."""

    @classmethod
    def setUpClass(cls):
        test_long_master.LongMaster.setUpClass()

    def setUp(self):
        self.long = test_long_master.LongMaster()
        self.long.setUp()
        self.addCleanup(self.long.doCleanups)
        self.c = self.long.c
        self.c.settings['shared']['video']['sizes']['tall'] = [50, 90]
        self.c.settings['shared']['video']['shot_fractions']['med'] = .8
        self.c.settings['shared']['video']['pix_fmt'] = 'gbrp'
        self.c.settings['shared']['encoder']['engine'] = 'libx264rgb'
        self.c.settings['profiles']['vertical'].update(render_checks=['subject'], caption_safe=[0, 0, 50, 90])
        self.c.captioner.width = lambda text, font: len(text)
        caption_settings = cues.settings()
        caption_settings['tall'].update(min_cue_s=.1, size=4, outline=1)
        caption_settings['frame'] = {'width': 50, 'height': 90}
        caption_settings['safe_tall'].update(left=2, right=2, top=2, bottom=2)
        settings_patch = patch.object(cues, 'settings', return_value=caption_settings)
        self.addCleanup(settings_patch.stop)
        settings_patch.start()

    def supply(self):
        # The existing provider boundary: first produce cues, then bind resolved glyph pixels.
        c = self.c
        result = render(c.v, 'vertical')
        self.assertEqual(result['state'], 'render_failed', result)
        self.assertIn('burn requires supplied raster coverage', result['error'])
        caption = c.get('records/captions/vertical.json')
        self.edges = [(math.ceil(cue['start_s'] * 30), math.ceil(cue['end_s'] * 30))
                      for cue in caption['cues']]
        raw = bytearray(24 * 50 * 90 * 4)
        for n in range(24):
            if any(a <= n < b for a, b in self.edges):
                for y in range(2, 6):
                    for x in range(2, 6):
                        p = ((n * 90 + y) * 50 + x) * 4
                        raw[p:p + 4] = bytes((220, 35, 150, 128))
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgba',
            '-s', '50x90', '-r', '30', '-i', 'pipe:0', '-c:v', 'ffv1', str(c.folder / 'caption.nut')],
            input=raw, check=True, capture_output=True)
        caption['raster'] = {'file': 'caption.nut', 'sha256': filehash(c.folder / 'caption.nut'),
                             'burn_sha256': caption['burn']['sha256']}
        c.put('records/captions/vertical.json', caption)
        return raw

    def delivered(self):
        result = render(self.c.v, 'vertical')
        self.assertEqual(result['state'], 'delivered', result)
        checks = self.c.get(result['readings'])['checks']
        for name in ('frame-map', 'voice', 'captions', 'bed', 'subject', 'draft-match'):
            self.assertTrue(checks[name]['ok'], checks[name])
        self.assertTrue((self.c.folder / 'Vertical/main.srt').is_file())
        return result

    def test_decoded_native_vertical_crop_clocks_audio_and_reuse(self):
        c = self.c
        self.long.approved()
        for choice in ('reject', 'redo', 'not this', 'try again'):
            c.approve(decision=choice)
            with patch.object(compositor, 'encode', side_effect=AssertionError('unapproved encode')):
                result = render(c.v, 'vertical')
            self.assertEqual(result['state'], 'waiting_owner', result)
        c.approve()
        raw = self.supply()
        result = self.delivered()
        actual = self.long.decode(c.folder / result['file'], 'rgb')
        # Independent fixture arithmetic: voice 0..12 + 18..30, picture 0..15 + 21..30.
        # Center (80,45), tall base width 50; med .8 => 40x72, close .5 => 25x45.
        expected = b''
        for start, end, crop in ((0, 15, '40:72:60:9'), (21, 30, '25:45:68:22')):
            expected += subprocess.run(['ffmpeg', '-v', 'error', '-i', str(c.folder / 'source.mp4'),
                '-vf', f'trim=start_frame={start}:end_frame={end},crop={crop}:exact=1,scale=50:90:flags=bilinear,format=rgb24',
                '-an', '-fps_mode', 'passthrough', '-f', 'rawvideo', 'pipe:1'],
                check=True, capture_output=True).stdout
        self.assertEqual(len(actual), 24 * 50 * 90 * 3)
        self.assertEqual(len(expected), len(actual))
        expected = bytearray(expected)
        for p in range(len(raw) // 4):
            alpha = raw[p * 4 + 3]
            for channel in range(3):
                expected[p * 3 + channel] = round((raw[p * 4 + channel] * alpha
                    + expected[p * 3 + channel] * (255 - alpha)) / 255)
        peak = max(abs(a - b) for a, b in zip(actual, expected))
        self.assertLessEqual(peak, 3)
        source_pcm = self.long.decode(c.folder / 'source.mp4', 'pcm')
        voice_expected = source_pcm[:19200 * 2] + source_pcm[28800 * 2:48000 * 2]
        with wave.open(str(c.folder / 'records/render/vertical-voice.wav')) as voice:
            self.assertEqual(voice.readframes(voice.getnframes()), voice_expected)
        count = len(c.heard)
        with patch.object(compositor, 'encode', side_effect=AssertionError('cached encode')):
            reused = self.delivered()
        self.assertEqual(reused['file_sha256'], result['file_sha256'])
        self.assertEqual(len(c.heard), count)

    def test_stale_identity_nonfinite_alias_and_crash_boundaries(self):
        c = self.c
        self.long.approved()
        self.supply()
        accepted = self.delivered()
        spec = c.get('records/shorts_spec.json')
        spec['plan'] = 'stale'
        c.put('records/shorts_spec.json', spec)
        with self.assertRaisesRegex(SpineError, 'stale cut check: its inputs changed'):
            render(c.v, 'vertical')
        spec['plan'] = c.cut['key']
        c.put('records/shorts_spec.json', spec)
        settings = c.settings['profiles']['vertical']
        settings['encoder'] = {**c.settings['shared']['encoder'], 'crf': float('nan')}
        with self.assertRaisesRegex(SpineError, 'encoder.crf: expected finite number'):
            render(c.v, 'vertical')
        settings['encoder']['crf'] = 1
        voice = c.folder / 'records/render/vertical-voice.wav'
        original = voice.read_bytes()
        voice.unlink()
        voice.symlink_to(c.folder / 'owner.txt')
        with self.assertRaisesRegex(SpineError, 'symlink'):
            render(c.v, 'vertical')
        self.assertEqual((c.folder / 'owner.txt').read_bytes(), b'owner original')
        voice.unlink()
        voice.write_bytes(original)
        write = records.write
        def crash(video, name, value):
            if name == 'records/render/vertical.json' and value['state'] == 'delivered':
                raise PowerLoss()
            return write(video, name, value)
        with patch.object(records, 'write', side_effect=crash), self.assertRaises(PowerLoss):
            render(c.v, 'vertical')
        self.assertEqual(c.saved('vertical')['state'], 'measuring')
        recovered = self.delivered()
        self.assertNotEqual(accepted['key'], recovered['key'])
        paths = [recovered['file'], *recovered['request']['generated']]
        before = {name: (c.folder / name).read_bytes() if (c.folder / name).exists() else None for name in paths}
        settings['encoder']['crf'] = 2
        with patch.object(compositor.mix, 'build', side_effect=SpineError('bed: unavailable stem')):
            result = render(c.v, 'vertical')
        self.assertEqual(result['state'], 'render_failed', result)
        self.assertIn('bed: unavailable stem', result['error'])
        self.assertEqual({name: (c.folder / name).read_bytes() if (c.folder / name).exists() else None for name in paths}, before)
