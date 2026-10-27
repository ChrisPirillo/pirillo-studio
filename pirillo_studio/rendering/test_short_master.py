"""Native selected shorts, independently decoded on the settled piece clock."""
import copy
import json
import math
import subprocess
import unittest
import wave
from unittest.mock import patch

from ..spine.files import SpineError, filehash
from . import compositor, records, request, test_cards, test_vertical_master
from .render import render
from .test_rendering import PowerLoss


class ShortMaster(unittest.TestCase):
    """e2e: Shorts/one.mp4, trimmed/reordered pixels, voice, captions and recovery."""

    @classmethod
    def setUpClass(cls):
        test_vertical_master.VerticalMaster.setUpClass()

    def setUp(self):
        self.vertical = test_vertical_master.VerticalMaster()
        self.vertical.setUp()
        self.addCleanup(self.vertical.doCleanups)
        self.c = self.vertical.c
        self.c.two_spans('jump')
        self.c.settings['profiles']['short'].update(
            render_checks=['subject', 'context', 'word-ends'],
            cards_safe=[2, 2, 46, 86], caption_safe=[2, 2, 46, 86])
        self.spec = self.c.get('records/shorts_spec.json')
        self.spec['shorts'][1].update(spans=[['w2', 'w2'], ['w1', 'w1']],
            opens_on='w2', ends_on='w1', pieces=[[.6, .9], [0, .3]])
        self.count = 18
        self.c.truth = {'0:w1': [.05, .2], '1:w2': [.45, .6]}

    def approve(self, context=False, jcut=False):
        c = self.c
        if jcut:
            c.two_spans()
            self.spec['shorts'][1].update(spans=[['w1', 'w1'], ['w2', 'w2']],
                opens_on='w1', ends_on='w2', pieces=[[0, .41], [.6, 1]])
            self.count = 24
        if context:
            self.context = test_cards.Cards.raster(self, 'context.nut', intervals=((0, self.count),),
                count=self.count, size=(50, 90), box=(4, 4, 42, 12))
            self.spec['shorts'][1]['stands_alone'] = {'context': 'The complete subject.', 'raster': self.context}
        c.put('records/shorts_spec.json', self.spec)
        c.save_cut()
        self.vertical.long.approved()
        if not jcut:
            c.truth = {'0:w2': [.05, .2], '1:w1': [.35, .5]}

    def supply(self):
        result = render(self.c.v, 'short:one')
        self.assertEqual(result['state'], 'render_failed', result)
        self.assertIn('burn requires supplied raster coverage', result['error'])
        caption = self.c.get('records/captions/short-one.json')
        self.edges = [(math.ceil(c['start_s'] * 30), math.ceil(c['end_s'] * 30)) for c in caption['cues']]
        self.assertEqual(self.edges, [(2, 6), (11, 15)] if self.count == 18 else [(2, 6), (14, 18)])
        asset = test_cards.Cards.raster(self, 'caption.nut', intervals=self.edges,
            count=self.count, size=(50, 90), box=(20, 40, 6, 4), rgba=(220, 35, 150, 128))
        caption['raster'] = {**asset, 'burn_sha256': caption['burn']['sha256']}
        self.c.put('records/captions/short-one.json', caption)

    def delivered(self):
        result = render(self.c.v, 'short:one')
        self.assertEqual(result['state'], 'delivered', result)
        checks = self.c.get(result['readings'])['checks']
        for name in ('frame-map', 'voice', 'cards', 'captions', 'bed', 'subject', 'context', 'word-ends', 'draft-match'):
            self.assertTrue(checks[name]['ok'], checks[name])
        self.assertTrue((self.c.folder / 'Shorts/one.srt').is_file())
        return result

    def compare(self, result, intervals, context):
        c = self.c
        actual = self.vertical.long.decode(c.folder / result['file'], 'rgb')
        expected = bytearray()
        # Fixed source/crop facts, independent of all compositor maps and pixel helpers.
        for a, b, crop in intervals:
            expected.extend(subprocess.run(['ffmpeg', '-v', 'error', '-i', str(c.folder / 'source.mp4'),
                '-vf', f'trim=start_frame={a}:end_frame={b},crop={crop}:exact=1,scale=50:90:flags=bilinear,format=rgb24',
                '-an', '-fps_mode', 'passthrough', '-f', 'rawvideo', 'pipe:1'], check=True, capture_output=True).stdout)
        self.assertEqual(len(actual), self.count * 50 * 90 * 3)
        self.assertEqual(len(expected), len(actual))
        for frame in range(self.count):
            layers = [((4, 4, 42, 12), (230, 40, 90))] if context else []
            if any(a <= frame < b for a, b in self.edges):
                layers.append(((20, 40, 6, 4), (220, 35, 150)))
            for (x, y, w, h), rgb in layers:
                for row in range(y, y + h):
                    for col in range(x, x + w):
                        p = ((frame * 90 + row) * 50 + col) * 3
                        for channel, value in enumerate(rgb):
                            expected[p + channel] = round((value * 128 + expected[p + channel] * 127) / 255)
        peak = max(abs(a - b) for a, b in zip(actual, expected))
        self.assertLessEqual(peak, 2)
        source = self.vertical.long.decode(c.folder / 'source.mp4', 'pcm')
        voice_ranges = [(18, 27), (0, 9)] if self.count == 18 else [(0, 12), (18, 30)]
        expected_voice = b''.join(source[a * 1600 * 2:b * 1600 * 2] for a, b in voice_ranges)
        with wave.open(str(c.folder / 'records/render/short-one-voice.wav')) as voice:
            actual_voice = voice.readframes(voice.getnframes())
        self.assertEqual(actual_voice, expected_voice)
        print(json.dumps({'output': result['file'], 'frames': self.count, 'rgb_bytes': len(actual),
            'independent_rgb_peak_error': peak, 'exact_voice_samples': len(actual_voice) // 2,
            'sha256': result['file_sha256']}), flush=True)

    def test_reordered_trimmed_native_context_pixels_voice_and_reuse(self):
        self.approve(context=True)
        for choice in ('reject', 'redo', 'not this', 'try again'):
            self.c.approve(decision=choice)
            with patch.object(compositor, 'encode', side_effect=AssertionError('unapproved encode')):
                self.assertEqual(render(self.c.v, 'short:one')['state'], 'waiting_owner')
        self.c.approve()
        self.supply()
        result = self.delivered()
        self.compare(result, [(18, 27, '25:45:68:22'), (0, 9, '40:72:60:9')], True)
        with patch.object(compositor, 'encode', side_effect=AssertionError('cache encoded')):
            self.assertEqual(self.delivered()['file_sha256'], result['file_sha256'])
        with patch.object(request, 'RENDERER_VERSION', 'rendering-2h-16'):
            old = request.resolve(self.c.v, 'masters', 'short:one')['key']
        self.assertNotEqual(request.resolve(self.c.v, 'masters', 'short:one')['key'], old)

    def test_native_jcut_subject_opening_and_remaining_refusals(self):
        self.approve(jcut=True)
        self.supply()
        result = self.delivered()
        self.compare(result, [(0, 15, '40:72:60:9'), (21, 30, '25:45:68:22')], False)
        for defect, diagnostic in [('partial-jcut', 'J-cut:.*partial selection'),
                ('partial-patch', 'patch:.*partial patch'), ('retime', 'retiming:.*voice unsupported'),
                ('supplied', 'native masters unsupported')]:
            with self.subTest(defect=defect):
                req = copy.deepcopy(result['request'])
                if defect == 'partial-jcut':
                    req['selected']['pieces'][1][0] = .7
                elif defect == 'partial-patch':
                    req['plan_data']['spans'][0]['patch'] = {'record': 'unused'}
                    req['selected']['pieces'][0][0] = .1
                elif defect == 'retime':
                    req['plan_data']['spans'][0]['speed'] = 2
                else:
                    req['stage'] = 'short'
                with self.assertRaisesRegex(SpineError, diagnostic):
                    compositor.prepare(self.c.v, req)

    def test_native_boundaries_and_interrupted_publication_preserve_artifacts(self):
        self.approve(context=True)
        self.supply()
        accepted = self.delivered()
        c = self.c
        paths = [accepted['file'], *accepted['request']['generated']]
        before = {name: (c.folder / name).read_bytes() if (c.folder / name).exists() else None for name in paths}
        settings = c.settings['profiles']['short']
        settings['cards_safe'][0] = float('nan')
        with self.assertRaisesRegex(SpineError, 'finite'):
            render(c.v, 'short:one')
        settings['cards_safe'][0] = 2
        req = copy.deepcopy(accepted['request'])
        req['inputs'].pop('context.nut')
        with self.assertRaisesRegex(SpineError, 'stale or unbound raster'):
            compositor.prepare(c.v, req)
        voice = c.folder / 'records/render/short-one-voice.wav'
        voice.unlink()
        voice.symlink_to(c.folder / 'owner.txt')
        with self.assertRaisesRegex(SpineError, 'symlink'):
            render(c.v, 'short:one')
        self.assertEqual((c.folder / 'owner.txt').read_bytes(), b'owner original')
        voice.unlink()
        voice.write_bytes(before['records/render/short-one-voice.wav'])
        settings['encoder'] = {**c.settings['shared']['encoder'], 'crf': 1}
        with patch.object(compositor.mix, 'build', side_effect=SpineError('bed: unavailable stem')):
            failed = render(c.v, 'short:one')
        self.assertEqual(failed['state'], 'render_failed', failed)
        self.assertIn('bed: unavailable stem', failed['error'])
        self.assertEqual({name: (c.folder / name).read_bytes() if (c.folder / name).exists() else None for name in paths}, before)
        write = records.write
        def crash(video, name, value):
            if name == 'records/render/short-one.json' and value['state'] == 'delivered':
                raise PowerLoss()
            return write(video, name, value)
        with patch.object(records, 'write', side_effect=crash), self.assertRaises(PowerLoss):
            render(c.v, 'short:one')
        self.assertEqual(c.saved('short:one')['state'], 'measuring')
        recovered = self.delivered()
        self.assertNotEqual(recovered['key'], accepted['key'])
        self.assertEqual(recovered['file_sha256'], filehash(c.folder / recovered['file']))
