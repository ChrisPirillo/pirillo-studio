"""Real bound card rasters are checked against delivered RGB, clocks and recovery."""
import copy
import json
import subprocess
import unittest
from unittest.mock import patch

from ..spine.files import SpineError, filehash
from . import compositor, pixels, records, test_compositor
from .render import render
from .test_rendering import PowerLoss


class Cards(unittest.TestCase):
    """e2e: Draft/draft.mp4 with anchored card/context rasters under captions."""

    @classmethod
    def setUpClass(cls):
        test_compositor.Compositor.setUpClass()

    def setUp(self):
        self.c = test_compositor.Compositor('runTest')
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.c.settings['profiles']['draft']['cards_safe'] = [2, 2, 156, 86]
        self.c.settings['profiles']['draft']['caption_safe'] = [2, 2, 156, 86]
        self.c.settings['shared']['encoder']['engine'] = 'libx264rgb'
        self.c.settings['shared']['video']['pix_fmt'] = 'gbrp'

    def raster(self, name='card.nut', intervals=((3, 15),), count=30,
               box=(4, 4, 152, 82), rgba=(230, 40, 90, 128), size=(160, 90), alpha=True):
        width, height = size
        raw = bytearray(width * height * 4 * count)
        x, y, w, h = box
        for a, b in intervals:
            for n in range(a, b):
                for row in range(y, y + h):
                    start = ((n * height + row) * width + x) * 4
                    raw[start:start + w * 4] = bytes(rgba) * w
        path = self.c.folder / name
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgba',
                        '-s', f'{width}x{height}', '-r', '30', '-i', 'pipe:0', '-c:v', 'ffv1',
                        '-pix_fmt', 'bgra' if alpha else 'bgr0', str(path)],
                       input=raw, check=True, capture_output=True)
        return {'file': name, 'sha256': filehash(path)}

    def bind(self, cards):
        self.c.put('records/overlay_plan.json', {'plan': self.c.cut['key'], 'cards': cards})
        self.c.save_cut()

    def card(self, ident='instruction', anchor=('w1', 'w1'), **kwargs):
        return {'id': ident, 'kind': 'context' if ident == 'context' else 'instruction',
                'anchor': list(anchor), 'text': 'A helpful explanation.', 'raster': self.raster(**kwargs)}

    def decode(self, name, fmt='rgb24'):
        return subprocess.run(['ffmpeg', '-v', 'error', '-i', str(self.c.folder / name),
                               '-an', '-fps_mode', 'passthrough', '-f', 'rawvideo',
                               '-pix_fmt', fmt, 'pipe:1'], check=True, capture_output=True).stdout

    def test_plan_order_alpha_and_captions_match_independent_pixels(self):
        self.c.accepted()
        caption = self.c.get('records/captions/draft.json')
        burn = self.c.folder / 'caption.ass'
        burn.write_text('fixture resolved glyphs')
        caption['burn'] = {'file': 'caption.ass', 'sha256': filehash(burn)}
        caption['raster'] = {**self.raster('caption.nut', intervals=((3, 9),),
                                         box=(40, 40, 20, 10), rgba=(255, 255, 255, 255)),
                             'burn_sha256': caption['burn']['sha256']}
        self.c.put('records/captions/draft.json', caption)
        baseline = self.c.accepted()
        baseline_path = self.c.folder / 'baseline.mp4'
        baseline_path.write_bytes((self.c.folder / baseline['file']).read_bytes())
        before = self.decode(baseline['file'])
        first = self.card()
        second = self.card('context', name='context.nut', rgba=(20, 210, 70, 192))
        self.bind([first, second])
        result = self.c.accepted()
        after = self.decode(result['file'])
        caption = self.decode(result['request']['caption_binding']['raster']['file'], 'rgba')
        self.assertEqual(len(after), 30 * 160 * 90 * 3)
        self.assertEqual(len(before), len(after))
        peak, opaque, compared = 0, 0, 0
        for n in range(30):
            for y in range(90):
                for x in range(160):
                    p = (n * 90 + y) * 160 + x
                    alpha = caption[p * 4 + 3]
                    self.assertIn(alpha, (0, 255))
                    expected = list(before[p * 3:p * 3 + 3])
                    if 3 <= n < 15 and 4 <= x < 156 and 4 <= y < 86 and alpha == 0:
                        for rgb, a in (((230, 40, 90), 128), ((20, 210, 70), 192)):
                            expected = [round((v * a + old * (255 - a)) / 255)
                                        for v, old in zip(rgb, expected)]
                        compared += 1
                    if 3 <= n < 15 and alpha == 255:
                        opaque += 1
                    peak = max(peak, *(abs(a - b) for a, b in
                                     zip(after[p * 3:p * 3 + 3], expected)))
        self.assertGreater(compared, 100)
        self.assertGreater(opaque, 0, 'must observe captions above the cards')
        self.assertLessEqual(peak, 5)
        omitted = pixels.compare(self.c.v, result['request'], baseline_path,
            layers=[self.c.folder / c['raster']['file'] for c in (first, second)],
            raster=self.c.folder / result['request']['caption_binding']['raster']['file'])
        for name in ('cards', 'frame-map'):
            self.assertFalse(omitted['checks'][name]['ok'], omitted['checks'][name])
        print(json.dumps({'cards_sha256': result['file_sha256'], 'frames': 30,
                          'independent_rgb_peak_error': peak, 'caption_pixels': opaque}), flush=True)

    def test_voice_clock_fractional_edges_and_repeated_cold_open(self):
        self.c.two_spans()
        self.bind([self.card(anchor=('w2', 'w2'), intervals=((14, 18),), count=24)])
        result = self.c.accepted()
        elements = [e for e in result['request']['elements'] if e['what'] == 'instruction']
        self.assertEqual([(e['start_s'], e['end_s']) for e in elements], [(14 / 30, 18 / 30)])
        self.assertEqual(len(self.decode(result['file'])), 24 * 160 * 90 * 3)
        words = self.c.get('records/words.json')
        words['words'][0].update(start=.1, end=.3)
        self.c.put('records/words.json', words)
        self.c.cut['cold_open'] = {'span': 's1', 'words': ['w1', 'w1']}
        self.c.truth = {'0:w1': [0, .2], '1:w1': [.3, .5], '2:w2': [.65, .8]}
        self.c.save_cut()
        # On-grid cold open repeats the same source word before the ordinary spans.
        self.bind([self.card(intervals=((0, 6), (9, 15)), count=30)])
        result = self.c.accepted()
        elements = [e for e in result['request']['elements'] if e['what'] == 'instruction']
        self.assertEqual([(e['start_s'], e['end_s']) for e in elements], [(0, 6 / 30), (9 / 30, 15 / 30)])
        print(json.dumps({'cold_open_sha256': result['file_sha256'], 'frames': 30}), flush=True)

    def test_bad_bindings_alpha_and_publish_crash_preserve_prior_output(self):
        good = self.card()
        self.bind([good])
        accepted = self.c.accepted()
        original = (self.c.folder / accepted['file']).read_bytes()
        defects = {'missing-hash': 'hash', 'stale-hash': 'sha256 contradicts actual bytes', 'symlink': 'symlink',
                   'nonfinite-safe': 'finite', 'missing-safe': 'safe', 'unsafe': 'safe',
                   'edge': 'edge', 'size': 'dimensions', 'count': 'frame', 'alpha': 'alpha|decode'}
        for defect, diagnostic in defects.items():
            with self.subTest(defect=defect):
                bad = copy.deepcopy(good)
                if defect == 'missing-hash':
                    bad['raster'].pop('sha256')
                elif defect == 'stale-hash':
                    bad['raster']['sha256'] = 'sha256:' + '0' * 64
                elif defect == 'symlink':
                    (self.c.folder / 'alias.nut').symlink_to(self.c.folder / 'card.nut')
                    bad['raster']['file'] = 'alias.nut'
                elif defect == 'nonfinite-safe':
                    self.c.settings['profiles']['draft']['cards_safe'] = [float('nan'), 2, 156, 86]
                elif defect == 'missing-safe':
                    self.c.settings['profiles']['draft'].pop('cards_safe')
                elif defect == 'size':
                    bad['raster'] = self.raster('small.nut', size=(158, 90))
                elif defect == 'count':
                    bad['raster'] = self.raster('short.nut', count=29)
                elif defect == 'alpha':
                    bad['raster'] = self.raster('opaque.nut', alpha=False)
                elif defect == 'unsafe':
                    bad['raster'] = self.raster('unsafe.nut', box=(0, 0, 10, 10))
                else:
                    bad['raster'] = self.raster('early.nut', intervals=((2, 15),))
                self.bind([bad])
                try:
                    refused = render(self.c.v, 'draft')
                except SpineError as error:
                    self.assertRegex(str(error), diagnostic)
                else:
                    self.assertIn(refused['state'], ('render_failed', 'measure_refused'), refused)
                    self.assertRegex(refused['error'], diagnostic)
                self.assertEqual((self.c.folder / accepted['file']).read_bytes(), original)
                self.c.settings['profiles']['draft']['cards_safe'] = [2, 2, 156, 86]
        for stage, shape, diagnostic in (('draft', 'tall', 'tall'), ('master', 'wide', 'masters')):
            unsupported = copy.deepcopy(accepted['request'])
            unsupported['stage'], unsupported['settings']['shape'] = stage, shape
            with self.assertRaisesRegex(SpineError, diagnostic):
                compositor.prepare(self.c.v, unsupported)
        self.bind([good])
        self.c.v.versions['compositor'] = 'publish-failure'
        with patch.object(records, 'publish', side_effect=OSError('failed publication')):
            self.assertEqual(render(self.c.v, 'draft')['state'], 'render_failed')
        self.assertEqual((self.c.folder / accepted['file']).read_bytes(), original)
        self.c.accepted()
        self.c.v.versions['compositor'] = 'crash'
        write = records.write
        def crash(video, name, value):
            if name == 'records/render/draft.json' and value['state'] == 'waiting_owner':
                raise PowerLoss()
            return write(video, name, value)
        with patch.object(records, 'write', side_effect=crash), self.assertRaises(PowerLoss):
            render(self.c.v, 'draft')
        recovered = self.c.accepted()
        self.assertEqual(recovered['request']['elements'], accepted['request']['elements'])
