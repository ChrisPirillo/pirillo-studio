"""Native long delivery measured against independent decoded source frames and samples."""
import subprocess
import unittest
import wave
from fractions import Fraction
from unittest.mock import patch

from ..spine.files import SpineError, filehash
from . import compositor, records, test_compositor
from .render import render
from .test_rendering import PowerLoss


class LongMaster(unittest.TestCase):
    """e2e: YouTube/main.mp4, its complete voice and sidecar after approved native draft."""

    @classmethod
    def setUpClass(cls):
        test_compositor.Compositor.setUpClass()

    def setUp(self):
        self.c = test_compositor.Compositor()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.c.two_spans()
        self.c.settings['shared']['verification']['master_checks'] = ['draft-match']
        self.c.settings['shared']['verification']['render_checks'] = ['frame-map', 'cards', 'captions', 'bed']
        self.c.captioner.align = self.align

    def align(self, voice, words, model):
        with wave.open(str(voice)) as pcm:
            self.assertGreater(pcm.getnframes(), 0)
        self.c.heard.append(filehash(voice))
        return {'words': {w['id']: self.c.truth[w['id']] for w in words}, 'unmatched': []}

    def decode(self, path, kind):
        options = (['-map', '0:v:0', '-an', '-fps_mode', 'passthrough', '-pix_fmt', 'rgb24', '-f', 'rawvideo']
                   if kind == 'rgb' else ['-map', '0:a:0', '-vn', '-acodec', 'pcm_s16le', '-f', 's16le'])
        return subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), *options, 'pipe:1'],
                              check=True, capture_output=True).stdout

    def approved(self):
        self.c.v.stage = 'draft'
        result = self.c.accepted()
        self.c.approve()
        self.c.v.stage = 'masters'
        return result

    def delivered(self):
        result = render(self.c.v, 'long')
        self.assertEqual(result['state'], 'delivered', result)
        readings = self.c.get(result['readings'])
        for name in ('frame-map', 'voice', 'captions', 'bed', 'draft-match'):
            self.assertTrue(readings['checks'][name]['ok'], readings['checks'][name])
        self.assertEqual(filehash(self.c.folder / result['file']), result['file_sha256'])
        self.assertTrue((self.c.folder / 'YouTube/main.srt').is_file())
        return result

    def test_jcut_source_sampling_and_approval_on_integer_and_fractional_grids(self):
        c = self.c
        for fps, source_fps in (('30/1', '60/1'), ('30000/1001', '60000/1001')):
            with self.subTest(fps=fps):
                source = c.folder / 'source.mp4'
                subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    f'nullsrc=size=160x90:rate={source_fps}', '-f', 'lavfi', '-i',
                    'sine=frequency=440:sample_rate=48000', '-t', '1.01', '-vf',
                    "geq=lum='32+mod(N*7,180)':cb=128:cr=128", '-af', 'volume=0.5',
                    '-c:v', 'libx264', '-crf', '0', '-pix_fmt', 'yuv420p',
                    *test_compositor.COLOR_TAGS, '-c:a', 'aac', str(source)], check=True, capture_output=True)
                c.v.preflight['source_sha256'] = filehash(source)
                c.settings['shared']['video']['fps'] = fps
                c.put('records/faces.json', {'frames': [
                    {'frame': i, 'boxes': [[70, 35, 20, 20]]} for i in range(61)]})
                c.save_cut()
                self.approved()
                for choice in ('reject', 'redo', 'not this', 'try again'):
                    c.approve(decision=choice)
                    with patch.object(compositor, 'encode', side_effect=AssertionError('unapproved encode')):
                        result = render(c.v, 'long')
                    self.assertEqual(result['state'], 'waiting_owner', result)
                c.approve()
                result = self.delivered()
                actual = self.decode(c.folder / result['file'], 'rgb')
                original = self.decode(source, 'rgb')
                indices = list(range(0, 30, 2)) + list(range(42, 60, 2))
                frame_bytes = 160 * 90 * 3
                self.assertEqual(len(actual), len(indices) * frame_bytes)
                # Every source frame is uniform gray; crop/scale cannot change its expected RGB.
                peak = max(abs(value - original[n * frame_bytes + channel % 3])
                           for i, n in enumerate(indices)
                           for channel, value in enumerate(actual[i * frame_bytes:(i + 1) * frame_bytes]))
                self.assertLessEqual(peak, 3)
                raw = self.decode(source, 'pcm')
                samples = lambda frame: round(Fraction(frame * 48000, 1) / Fraction(fps))
                expected = raw[:samples(12) * 2] + raw[samples(18) * 2:(samples(18) + samples(24) - samples(12)) * 2]
                with wave.open(str(c.folder / 'records/render/long-voice.wav')) as voice:
                    self.assertEqual(voice.readframes(voice.getnframes()), expected)
                heard = len(c.heard)
                with patch.object(compositor, 'encode', side_effect=AssertionError('cache encoded')):
                    reused = self.delivered()
                self.assertEqual(reused['file_sha256'], result['file_sha256'])
                self.assertEqual(len(c.heard), heard)

    def test_changed_draft_settings_refuse_after_approval(self):
        self.approved()
        c = self.c
        for output in ('long', 'vertical'):
            with self.subTest(output=output):
                c.settings['profiles']['draft']['encoder'] = {
                    **c.settings['shared']['encoder'], 'crf': 1}
                with patch.object(compositor, 'encode', side_effect=AssertionError('stale draft encoded')):
                    result = render(c.v, output)
                self.assertEqual(result['state'], 'waiting_owner', result)
                self.assertIn('current measured draft', result['reason'])

    def test_master_boundaries_and_interrupted_publication_preserve_accepted_artifacts(self):
        c = self.c
        self.approved()
        accepted = self.delivered()
        paths = [accepted['file'], *accepted['request']['generated']]
        c.settings['profiles']['long']['encoder'] = {**c.settings['shared']['encoder'], 'crf': float('nan')}
        with self.assertRaisesRegex(SpineError, 'rendering.settings encoder.crf: expected finite number'):
            render(c.v, 'long')
        c.settings['profiles']['long']['encoder']['crf'] = 1
        path = c.folder / 'records/render/long-voice.wav'
        # Alias validation runs before any generated artifact can overwrite its target.
        original = path.read_bytes()
        path.unlink()
        path.symlink_to(c.folder / 'owner.txt')
        with self.assertRaisesRegex(SpineError, 'symlink'):
            render(c.v, 'long')
        self.assertEqual((c.folder / 'owner.txt').read_bytes(), b'owner original')
        path.unlink()
        path.write_bytes(original)
        write = records.write
        def crash(video, name, value):
            if name == 'records/render/long.json' and value['state'] == 'delivered':
                raise PowerLoss()
            return write(video, name, value)
        with patch.object(records, 'write', side_effect=crash), self.assertRaises(PowerLoss):
            render(c.v, 'long')
        self.assertEqual(c.saved('long')['state'], 'measuring')
        recovered = self.delivered()
        self.assertNotEqual(recovered['key'], accepted['key'])
        # A failed replacement restores every accepted artifact, not only the movie.
        before = {name: (c.folder / name).read_bytes() if (c.folder / name).exists() else None for name in paths}
        c.settings['profiles']['long']['encoder']['crf'] = 2
        with patch.object(compositor.mix, 'build', side_effect=SpineError('bed: unavailable stem')):
            failed = render(c.v, 'long')
        self.assertEqual(failed['state'], 'render_failed', failed)
        self.assertIn('bed: unavailable stem', failed['error'])
        self.assertEqual({name: (c.folder / name).read_bytes() if (c.folder / name).exists() else None for name in paths}, before)
