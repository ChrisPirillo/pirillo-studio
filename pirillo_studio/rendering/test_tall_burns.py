"""Native tall burns are measured against an independent straight-alpha composite."""
import copy
import json
import math
import subprocess
import unittest
from unittest.mock import patch

from ..spine.files import SpineError, filehash
from . import compositor, raster, records, request, test_grade_adapters
from .render import render
from .test_rendering import PowerLoss


class TallBurns(unittest.TestCase):
    """e2e: Draft/draft.mp4 carries native-size, timed caption pixels over a tall grade."""

    @classmethod
    def setUpClass(cls):
        test_grade_adapters.TallGrades.setUpClass()

    def setUp(self):
        self.fixture = test_grade_adapters.TallGrades(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.case = c = self.fixture.grade.case
        c.settings['profiles']['draft']['caption_safe'] = [5, 110, 80, 40]
        self.fixture.grade.finish()
        self.fixture.grade.local()

    def supply(self, size=(90, 160), shift=0, early=False, alpha=True):
        c = self.case
        caption = c.get('records/captions/draft.json')
        burn = c.folder / 'records/captions/draft.ass'
        burn.write_text('fixture externally resolved glyphs')
        caption['burn'] = {'file': 'records/captions/draft.ass', 'sha256': filehash(burn)}
        width, height = size
        self.edges = [(math.ceil(cue['start_s'] * 30), math.ceil(cue['end_s'] * 30))
                      for cue in caption['cues']]
        raw = bytearray(width * height * 4 * 30)
        for n in range(30):
            if early and n == 0 or any(a <= n < b for a, b in self.edges):
                for x0, y0 in ((5, 110), (81, 146)):
                    for dy in range(4):
                        for dx, opacity in enumerate((1, 64, 128, 255)):
                            x, y = x0 + dx + shift, y0 + dy
                            if x < width and y < height:
                                offset = ((n * height + y) * width + x) * 4
                                raw[offset:offset + 4] = bytes((220, 35, 150, opacity))
        args = ['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgba',
                '-s', f'{width}x{height}', '-r', '30', '-i', 'pipe:0']
        if not alpha:
            args += ['-vf', 'format=bgr0']
        subprocess.run([*args, '-c:v', 'ffv1', str(c.folder / 'caption.nut')],
                       input=raw, check=True, capture_output=True)
        caption['raster'] = {'file': 'caption.nut', 'sha256': filehash(c.folder / 'caption.nut'),
                             'burn_sha256': caption['burn']['sha256']}
        c.put('records/captions/draft.json', caption)
        return raw

    def rgb(self, result):
        return subprocess.run(['ffmpeg', '-v', 'error', '-i', str(self.case.folder / result['file']),
            '-map', '0:v:0', '-an', '-fps_mode', 'passthrough', '-pix_fmt', 'rgb24',
            '-f', 'rawvideo', 'pipe:1'], check=True, capture_output=True).stdout

    def burned(self):
        baseline = self.case.accepted()
        before = self.rgb(baseline)
        raw = self.supply()
        result = self.case.accepted()
        return result, before, raw

    def test_decoded_tall_alpha_edges_grade_and_reuse(self):
        c = self.case
        result, before, raw = self.burned()
        actual = self.rgb(result)
        self.assertEqual(len(actual), 90 * 160 * 3 * 30)
        self.assertEqual(len(before), len(actual))
        # No production filter graph or pixel comparator computes this oracle.
        expected = bytearray(len(before))
        for p in range(len(raw) // 4):
            alpha = raw[p * 4 + 3]
            for channel in range(3):
                expected[p * 3 + channel] = round((raw[p * 4 + channel] * alpha
                    + before[p * 3 + channel] * (255 - alpha)) / 255)
        peak = max(abs(a - b) for a, b in zip(actual, expected))
        self.assertLessEqual(peak, 2)
        self.assertNotEqual(actual, before)
        # The unchanged center also has an independent LUT/exposure oracle.
        self.fixture.grade.prove(result, finish=True, local=True)
        self.fixture.prove_subjects(result)
        reading = c.get(result['readings'])['checks']['captions']['value']['raster']
        a, b = self.edges[0]
        self.assertEqual(reading['bounds'], [{'frames': [0, a], 'box': None},
            {'frames': [a, b], 'box': [5, 110, 80, 40]}, {'frames': [b, 30], 'box': None}])
        self.assertEqual(reading['safe'], [5, 110, 80, 40])
        alignments = len(c.heard)
        reused = c.accepted()
        self.assertEqual(len(c.heard), alignments)
        self.assertEqual(reused['file_sha256'], result['file_sha256'])
        self.assertEqual(reused['request']['caption_binding'], result['request']['caption_binding'])
        (c.folder / 'caption.nut').write_bytes(b'stale raster')
        self.assertFalse(compositor.reuse(c.v, request.resolve(c.v, 'draft', 'draft'), result))
        print('TALL_BURN_OUTPUT ' + json.dumps({'sha256': result['file_sha256'], 'frames': 30,
            'size': [90, 160], 'alpha_peak_error': peak, 'bounds': reading['bounds']}), flush=True)

    def test_invalid_tall_raster_refuses_by_name(self):
        c = self.case
        result, _, _ = self.burned()
        original = c.get('records/captions/draft.json')
        for defect, diagnostic in (('missing', 'supplied raster'), ('stale', 'current burn'),
                ('hash', 'content hash'), ('alias', 'symlink'), ('nonfinite', 'safe area'),
                ('size', 'dimensions'), ('unsafe', 'safe area'), ('early', 'cue edge'),
                ('opaque', 'alpha')):
            with self.subTest(defect=defect):
                c.put('records/captions/draft.json', original)
                self.supply(size=(160, 90) if defect == 'size' else (90, 160),
                            shift=1 if defect == 'unsafe' else 0, early=defect == 'early',
                            alpha=defect != 'opaque')
                caption = c.get('records/captions/draft.json')
                if defect == 'missing':
                    del caption['raster']
                elif defect == 'stale':
                    caption['raster']['burn_sha256'] = 'sha256:stale'
                elif defect == 'hash':
                    del caption['raster']['sha256']
                elif defect == 'alias':
                    (c.folder / 'alias.nut').symlink_to(c.folder / 'caption.nut')
                    caption['raster']['file'] = 'alias.nut'
                c.put('records/captions/draft.json', caption)
                req = copy.deepcopy(result['request'])
                if defect == 'nonfinite':
                    req['settings']['caption_safe'][0] = float('nan')
                with self.assertRaisesRegex(SpineError, diagnostic):
                    compositor.bind(c.v, req, {p: filehash(c.folder / p) for p in compositor.names(req)})
                    raster.inspect(c.v, req, caption)

    def test_failed_and_interrupted_tall_burn_restore_accepted_artifacts(self):
        c = self.case
        accepted, _, _ = self.burned()
        output = c.folder / accepted['file']
        accepted_hash = filehash(output)
        self.supply(shift=1)
        failed = render(c.v, 'draft')
        self.assertEqual(failed['state'], 'render_failed', failed)
        self.assertIn('safe area', failed['error'])
        self.assertEqual(filehash(output), accepted_hash)
        self.supply()
        accepted = c.accepted()
        artifacts = {p: (c.folder / p).read_bytes() if h is not None else None
                     for p, h in accepted['request']['generated'].items()}
        c.v.versions['compositor'] = 'interrupted-tall-burn'
        write = records.write

        def crash(video, name, value):
            if name == 'records/render/draft.json' and value['state'] == 'waiting_owner':
                raise PowerLoss()
            return write(video, name, value)

        with patch.object(records, 'write', side_effect=crash), self.assertRaises(PowerLoss):
            render(c.v, 'draft')
        self.assertEqual(c.saved('draft')['state'], 'measuring')
        recovered = c.accepted()
        self.assertEqual(filehash(output), recovered['file_sha256'])
        self.assertEqual(recovered['request']['caption_binding'], accepted['request']['caption_binding'])
        self.assertEqual({p: (c.folder / p).read_bytes() if (c.folder / p).exists() else None
                          for p in artifacts}, artifacts)
