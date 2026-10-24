"""Real caption raster and delivered files exercise bounds, timing and identity."""
import copy
import subprocess
import unittest
from unittest.mock import patch

from ..spine.files import filehash
from . import coverage, raster, request, test_coverage
from .render import MeasurementRefused, measured, render


class CaptionRaster(unittest.TestCase):
    """e2e: Draft/draft.mp4 with a bound alpha caption raster."""

    def setUp(self):
        self.case = test_coverage.RendererCoverage(methodName='runTest')
        self.case.setUpClass()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        c = self.case
        c.captioned()
        c.settings['profiles']['draft']['caption_safe'] = [10, 10, 140, 70]
        (c.folder / 'caption.ass').write_text('fixture resolved glyphs')
        caption = c.get('records/captions/draft.json')
        caption['burn'] = {'file': 'caption.ass', 'sha256': filehash(c.folder / 'caption.ass')}
        c.put('records/captions/draft.json', caption)
        self.make_raster()
        prepare = c.v.prepare
        def bound(req):
            value = prepare(req)
            value['caption_binding']['raster'] = {
                'file': 'caption.nut', 'sha256': filehash(c.folder / 'caption.nut'),
                'burn_sha256': caption['burn']['sha256']}
            return value
        c.v.prepare = bound
        c.v.encode = self.encode

    def accepted(self):
        result = render(self.case.v, 'draft')
        self.assertIn(result['state'], ('waiting_owner', 'delivered'),
                      {key: result.get(key) for key in ('state', 'reading', 'error')})
        return result

    def make_raster(self, x=20, start=3, end=27):
        raw = bytearray(160 * 90 * 4 * 30)
        for n in range(start, end):
            for y in range(20, 30):
                for dx in range(10):
                    if dx in (0, 1, 8, 9) or y in (24, 25):
                        offset = ((n * 90 + y) * 160 + x + dx) * 4
                        raw[offset:offset + 4] = b'\xff' * 4
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'bgra',
                        '-s', '160x90', '-r', '30', '-i', 'pipe:0', '-c:v', 'ffv1',
                        str(self.case.folder / 'caption.nut')], input=raw, check=True, capture_output=True)

    def encode(self, req, path):
        c = self.case
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(c.folder / req['source']),
                        '-i', str(c.folder / 'caption.nut'), '-filter_complex',
                        '[0:v]format=gbrp[b];[b][1:v]overlay=format=rgb:shortest=1[v]',
                        '-map', '[v]', '-map', '0:a', '-c:v', 'libx264rgb', '-crf', '0',
                        '-c:a', 'copy', str(path)], check=True, capture_output=True)

    def test_burn_pixels_and_safe_bounds_are_measured(self):
        c = self.case
        accepted = self.accepted()
        reading = c.get(accepted['readings'])['checks']['captions']
        self.assertTrue(reading['ok'], reading)
        raster = reading['value']['raster']
        self.assertEqual(raster['bounds'], [
            {'frames': [0, 3], 'box': None},
            {'frames': [3, 27], 'box': [20, 20, 10, 10]},
            {'frames': [27, 30], 'box': None}])
        expanded = [row['box'] for row in raster['bounds'] for _ in range(*row['frames'])]
        self.assertEqual(expanded, [None] * 3 + [[20, 20, 10, 10]] * 24 + [None] * 3)
        # A valid bound raster cannot excuse a missing burn in the delivered video.
        c.v.versions['encoder'] = 'omitted-burn'
        c.v.encode = c.encode_map
        refused = render(c.v, 'draft')
        self.assertEqual(refused['state'], 'measure_refused', refused)

    def test_unsafe_and_early_raster_edges_refuse_captions(self):
        c = self.case
        self.accepted()
        for x, start in ((0, 3), (20, 2)):
            with self.subTest(x=x, start=start):
                self.make_raster(x=x, start=start)
                req = request.resolve(c.v, 'draft', 'draft')
                with self.assertRaises(MeasurementRefused) as caught:
                    measured(c.v, req, 'Draft/draft.mp4')
                self.assertEqual(caught.exception.reading, 'captions')
                self.assertRegex(str(caught.exception), 'safe|cue')

    def test_ass_only_unbound_and_invalid_identity_refuse_by_name(self):
        c = self.case
        self.accepted()
        original = request.resolve(c.v, 'draft', 'draft')
        for defect in ('ass-only', 'unbound', 'stale', 'nonfinite', 'alias'):
            with self.subTest(defect=defect):
                req = copy.deepcopy(original)
                raster = req['caption_binding']['raster']
                if defect == 'ass-only':
                    del req['caption_binding']['raster']
                elif defect == 'unbound':
                    del req['inputs']['caption.nut']
                elif defect == 'stale':
                    raster['burn_sha256'] = 'sha256:stale'
                elif defect == 'nonfinite':
                    req['settings']['caption_safe'][0] = float('nan')
                else:
                    (c.folder / 'alias.mkv').symlink_to(c.folder / 'caption.nut')
                    raster['file'] = 'alias.mkv'
                    req['inputs']['alias.mkv'] = raster['sha256']
                with self.assertRaises(MeasurementRefused) as caught:
                    measured(c.v, req, 'Draft/draft.mp4')
                expected = {'nonfinite': 'settings', 'alias': 'measure'}.get(defect, 'captions')
                self.assertEqual(caught.exception.reading, expected)

    def test_raster_change_during_measurement_cannot_publish(self):
        c = self.case
        self.accepted()
        req = request.resolve(c.v, 'draft', 'draft')
        compare = coverage.pixels.compare
        def mutate(*args, **kwargs):
            result = compare(*args, **kwargs)
            with (c.folder / 'caption.nut').open('ab') as handle:
                handle.write(b'changed')
            return result
        with patch.object(coverage.pixels, 'compare', side_effect=mutate):
            with self.assertRaises(MeasurementRefused):
                measured(c.v, req, 'Draft/draft.mp4')

    def test_invalid_cue_times_name_cue_time_not_video_rate(self):
        c = self.case
        req = request.resolve(c.v, 'draft', 'draft')
        original = c.get('records/captions/draft.json')
        for field in ('start_s', 'end_s'):
            for value in (float('nan'), float('inf'), -float('inf'), -1, True):
                with self.subTest(field=field, value=value):
                    caption = copy.deepcopy(original)
                    caption['cues'][0][field] = value
                    with self.assertRaises(MeasurementRefused) as caught:
                        raster.inspect(c.v, req, caption)
                    self.assertEqual(caught.exception.reading, 'captions')
                    self.assertIn('cue', caught.exception.setting)
                    self.assertIn(field, str(caught.exception))
                    self.assertNotIn('video.fps', caught.exception.setting)
        self.make_raster(start=0)
        req = request.resolve(c.v, 'draft', 'draft')
        original['cues'][0]['start_s'] = 0
        self.assertEqual(raster.inspect(c.v, req, original)['bounds'][0]['frames'], [0, 27])
