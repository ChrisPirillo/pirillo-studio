"""Native tall cards retain the shared clocks, alpha, identity and recovery contract."""
import copy
import json
import subprocess
import unittest
from unittest.mock import patch

from ..spine.files import SpineError, filehash
from . import cards, compositor, records, request, test_cards, test_compositor, test_tall_burns
from .render import render
from .test_rendering import PowerLoss


class TallCards(unittest.TestCase):
    """e2e: Draft/draft.mp4 carries native tall card pixels after crop/grade and below captions."""

    @classmethod
    def setUpClass(cls):
        test_tall_burns.TallBurns.setUpClass()

    def setUp(self):
        self.burns = test_tall_burns.TallBurns('runTest')
        self.burns.setUp()
        self.addCleanup(self.burns.doCleanups)
        self.c = self.burns.case
        self.assets = test_cards.Cards('runTest')
        self.assets.c = self.c
        self.c.settings['profiles']['draft']['cards_safe'] = [5, 110, 80, 40]

    def layer(self, **kwargs):
        return self.assets.card(**{'size': (90, 160), 'box': (5, 110, 80, 40), **kwargs})

    def prove(self, result, before, intervals, colors, caption=None):
        actual = self.burns.rgb(result)
        self.assertEqual(len(actual), len(before))
        self.assertEqual(len(actual), result['frame_map'][-1]['frames'][1] * 90 * 160 * 3)
        peak, changed = 0, 0
        # Reconstruct each output pixel from decoded unlayered pixels and fixture RGBA.
        for p in range(len(actual) // 3):
            n, xy = divmod(p, 90 * 160)
            y, x = divmod(xy, 90)
            expected = list(before[p * 3:p * 3 + 3])
            if any(a <= n < b for a, b in intervals) and 5 <= x < 85 and 110 <= y < 150:
                for rgb, alpha in colors:
                    expected = [round((v * alpha + old * (255 - alpha)) / 255)
                                for v, old in zip(rgb, expected)]
                changed += 1
            if caption is not None:
                alpha = caption[p * 4 + 3]
                expected = [round((v * alpha + old * (255 - alpha)) / 255)
                            for v, old in zip(caption[p * 4:p * 4 + 3], expected)]
            peak = max(peak, *(abs(a - b) for a, b in zip(actual[p * 3:p * 3 + 3], expected)))
        self.assertGreater(changed, 0)
        self.assertLessEqual(peak, 2)
        self.assertNotEqual(actual, before)
        self.burns.fixture.prove_subjects(result)
        print('TALL_CARD_OUTPUT ' + json.dumps({'sha256': result['file_sha256'],
            'frames': len(actual) // (90 * 160 * 3), 'size': [90, 160],
            'independent_rgb_peak_error': peak, 'card_pixels': changed}), flush=True)

    def test_plan_order_after_grade_before_captions_and_reuse(self):
        baseline = self.c.accepted()
        before = self.burns.rgb(baseline)
        baseline_path = self.c.folder / 'baseline.mp4'
        baseline_path.write_bytes((self.c.folder / baseline['file']).read_bytes())
        caption = self.burns.supply()
        self.assets.bind([self.layer(), self.layer(ident='context', name='context.nut',
                                                  rgba=(20, 210, 70, 192))])
        result = self.c.accepted()
        self.prove(result, before, [(3, 15)], [((230, 40, 90), 128), ((20, 210, 70), 192)], caption)
        self.burns.fixture.grade.prove(result, finish=True, local=True)
        reading = self.c.get(result['readings'])['checks']['cards']['value']['rasters']
        self.assertEqual([r['id'] for r in reading], ['instruction', 'context'])
        for row in reading:
            self.assertEqual(row['safe'], [5, 110, 80, 40])
            self.assertEqual(row['bounds'], [{'frames': [0, 3], 'box': None},
                {'frames': [3, 15], 'box': [5, 110, 80, 40]}, {'frames': [15, 30], 'box': None}])
        omitted = self.c.v.measure(result['request'], baseline_path)
        for name in ('cards', 'frame-map', 'captions'):
            self.assertFalse(omitted['checks'][name]['ok'])
        heard = len(self.c.heard)
        self.assertEqual(self.c.accepted()['file_sha256'], result['file_sha256'])
        self.assertEqual(len(self.c.heard), heard)
        old_key = result['key']
        self.assets.bind([self.layer(rgba=(40, 190, 60, 64))])
        changed = self.c.accepted()
        self.assertNotEqual(changed['key'], old_key)
        self.assertNotEqual(changed['file_sha256'], result['file_sha256'])

    def test_voice_clock_jcut_push_and_repeated_cold_open(self):
        self.c.two_spans()
        self.c.cut['spans'][0].update(enter='push', push={'to': 'close', 's': .2})
        self.c.save_cut()
        for cold in (False, True):
            with self.subTest(cold_open=cold):
                if cold:
                    words = self.c.get('records/words.json')
                    words['words'][0].update(start=.1, end=.3)
                    self.c.put('records/words.json', words)
                    self.c.cut['cold_open'] = {'span': 's1', 'words': ['w1', 'w1']}
                    self.c.truth = {'0:w1': [0, .2], '1:w1': [.3, .5], '2:w2': [.65, .8]}
                self.assets.bind([])
                before = self.burns.rgb(self.c.accepted())
                edges, count, word = ([(0, 6), (9, 15)], 30, 'w1') if cold else ([(14, 18)], 24, 'w2')
                self.assets.bind([self.layer(anchor=(word, word), intervals=edges, count=count)])
                result = self.c.accepted()
                self.assertEqual([(e['start_s'], e['end_s']) for e in result['request']['elements']],
                                 [(a / 30, b / 30) for a, b in edges])
                self.prove(result, before, edges, [((230, 40, 90), 128)])

    def test_invalid_inputs_and_interrupted_publication_preserve_output(self):
        good = self.layer()
        self.assets.bind([good])
        accepted = self.c.accepted()
        output = self.c.folder / accepted['file']
        for defect, diagnostic in (('missing', 'hash'), ('stale', 'sha256 contradicts'),
                ('alias', 'symlink'), ('nonfinite', 'finite'), ('safe', 'cards_safe'),
                ('size', 'dimensions'), ('unsafe', 'safe area'), ('edge', 'cue edge'),
                ('alpha', 'alpha|decode'), ('anchor', 'word'), ('context', 'unsupported timing')):
            with self.subTest(defect=defect):
                bad = copy.deepcopy(good)
                if defect == 'missing':
                    del bad['raster']['sha256']
                elif defect == 'stale':
                    bad['raster']['sha256'] = 'sha256:' + '0' * 64
                elif defect == 'alias':
                    (self.c.folder / 'alias.nut').symlink_to(self.c.folder / 'card.nut')
                    bad['raster']['file'] = 'alias.nut'
                elif defect == 'nonfinite':
                    self.c.settings['profiles']['draft']['cards_safe'][0] = float('nan')
                elif defect == 'safe':
                    del self.c.settings['profiles']['draft']['cards_safe']
                elif defect == 'anchor':
                    bad['anchor'] = ['missing', 'missing']
                elif defect == 'context':
                    bad.update(kind='context', whole_short=True)
                else:
                    args = {'size': (160, 90), 'box': (5, 5, 80, 40)} if defect == 'size' else (
                        {'box': (4, 110, 80, 40)} if defect == 'unsafe' else (
                        {'intervals': ((2, 15),)} if defect == 'edge' else {'alpha': False}))
                    bad = self.layer(name=defect + '.nut', **args)
                self.assets.bind([bad])
                with self.assertRaisesRegex(SpineError, diagnostic):
                    request.resolve(self.c.v, 'draft', 'draft')
                self.assertEqual(filehash(output), accepted['file_sha256'])
                self.c.settings['profiles']['draft']['cards_safe'] = [5, 110, 80, 40]
        self.assets.bind([good])
        wrong = copy.deepcopy(accepted['request'])
        wrong['settings']['shape'] = 'square'
        with self.assertRaisesRegex(SpineError, 'cards: unsupported output shape square'):
            cards.inspect(self.c.v, wrong)
        wrong['settings']['shape'] = 'tall'
        wrong['stage'] = 'masters'
        with self.assertRaisesRegex(SpineError, 'native masters unsupported'):
            compositor.prepare(self.c.v, wrong)
        artifacts = {p: (self.c.folder / p).read_bytes() if h is not None else None
                     for p, h in accepted['request']['generated'].items()}
        self.c.v.versions['compositor'] = 'publish-failure'
        with patch.object(records, 'publish', side_effect=OSError('failed publication')):
            self.assertEqual(render(self.c.v, 'draft')['state'], 'render_failed')
        self.assertEqual(filehash(output), accepted['file_sha256'])
        self.c.v.versions['compositor'] = 'crash'
        write = records.write
        def crash(video, name, value):
            if name == 'records/render/draft.json' and value['state'] == 'waiting_owner':
                raise PowerLoss()
            return write(video, name, value)
        with patch.object(records, 'write', side_effect=crash), self.assertRaises(PowerLoss):
            render(self.c.v, 'draft')
        recovered = self.c.accepted()
        self.assertEqual(recovered['file_sha256'], accepted['file_sha256'])
        self.assertEqual({p: (self.c.folder / p).read_bytes() if (self.c.folder / p).exists() else None
                          for p in artifacts}, artifacts)

    def test_native_tall_opaque_cover_hides_every_source_frame(self):
        cover = self.c.folder / 'cover.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=magenta:size=90x160:rate=30',
                        '-frames:v', '1', '-c:v', 'libx264', '-crf', '0', *test_compositor.COLOR_TAGS,
                        str(cover)], check=True, capture_output=True)
        self.c.cut['spans'][0]['hide_picture'] = True
        self.assets.bind([{'id': 'hidden', 'kind': 'cover', 'covers': 's1', 'file': 'cover.mp4'}])
        result = self.c.accepted()
        actual = self.burns.rgb(result)
        expected = self.assets.decode('cover.mp4') * 30
        self.assertEqual(len(actual), len(expected))
        peak = max(abs(a - b) for a, b in zip(actual, expected))
        self.assertLessEqual(peak, 2)
        measured = self.c.get(result['readings'])
        self.assertEqual(measured['checks']['subject']['value']['covered'], list(range(30)))
        self.assertTrue(all(r['kind'] == 'cover' for r in measured['mapped_frames']))
        print('TALL_COVER_OUTPUT ' + json.dumps({'sha256': result['file_sha256'],
            'frames': 30, 'independent_rgb_peak_error': peak}), flush=True)
        wide = self.c.folder / 'wide-cover.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=magenta:size=160x90:rate=30',
                        '-frames:v', '1', '-c:v', 'libx264', '-crf', '0', *test_compositor.COLOR_TAGS,
                        str(wide)], check=True, capture_output=True)
        self.assets.bind([{'id': 'hidden', 'kind': 'cover', 'covers': 's1', 'file': 'wide-cover.mp4'}])
        with self.assertRaisesRegex(SpineError, 'cards: tall cover dimensions must match video.sizes.tall'):
            request.resolve(self.c.v, 'draft', 'draft')
