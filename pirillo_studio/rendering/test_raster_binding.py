"""Production burning composition consumes a provider's resolved glyph raster."""
import copy
import math
import unittest

from ..spine.files import filehash
from . import compositor, request, test_compositor, test_raster
from .render import render


class RasterBinding(unittest.TestCase):
    """e2e: Draft/draft.mp4 burned by the production compositor, including cache reuse."""

    def setUp(self):
        self.case = test_compositor.Compositor(methodName='runTest')
        self.case.setUpClass()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        c = self.case
        c.settings['profiles']['draft']['caption_safe'] = [10, 10, 140, 70]
        c.settings['shared']['encoder']['engine'] = 'libx264rgb'
        c.settings['shared']['video']['pix_fmt'] = 'gbrp'
        self.defect = None

    def supply(self):
        c = self.case
        caption = c.get('records/captions/draft.json')
        burn = c.folder / 'records/captions/draft.ass'
        burn.write_text('fixture externally resolved glyphs')
        caption['burn'] = {'file': 'records/captions/draft.ass', 'sha256': filehash(burn)}
        a, b = caption['cues'][0]['start_s'], caption['cues'][0]['end_s']
        test_raster.CaptionRaster.make_raster(self, start=math.ceil(a * 30), end=math.ceil(b * 30))
        caption['raster'] = {'file': 'caption.nut', 'sha256': filehash(c.folder / 'caption.nut'),
                             'burn_sha256': caption['burn']['sha256']}
        if self.defect == 'missing':
            del caption['raster']
        elif self.defect == 'stale':
            caption['raster']['burn_sha256'] = 'sha256:stale'
        elif self.defect == 'missing-hash':
            del caption['raster']['sha256']
        c.put('records/captions/draft.json', caption)
        return caption

    def test_production_burn_binding_pixels_and_reuse(self):
        c = self.case
        c.accepted()
        self.supply()
        result = c.accepted()
        alignments = len(c.heard)
        bound = result['request']['caption_binding']['raster']
        self.assertEqual(bound, c.get('records/captions/draft.json')['raster'])
        self.assertEqual(result['request']['inputs'][bound['file']], bound['sha256'])
        readings = c.get(result['readings'])
        self.assertTrue(readings['checks']['captions']['ok'])
        self.assertEqual(readings['checks']['captions']['value']['raster']['sha256'], bound['sha256'])
        reused = c.accepted()
        self.assertEqual(len(c.heard), alignments, 'cache must retain raster identity without alignment')
        self.assertEqual(reused['request']['caption_binding']['raster'], bound)
        accepted = copy.deepcopy(result)
        (c.folder / bound['file']).write_bytes(b'stale raster')
        req = request.resolve(c.v, 'draft', 'draft')
        self.assertFalse(compositor.reuse(c.v, req, accepted), 'stale raster cannot reuse accepted burn')
        for defect in ('missing', 'stale', 'missing-hash'):
            with self.subTest(defect=defect):
                fixture = type(self)(methodName='runTest')
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                c = fixture.case
                c.accepted()
                fixture.defect = defect
                fixture.supply()
                refused = render(c.v, 'draft')
                self.assertIn(refused['state'], ('render_failed', 'measure_refused'), refused)
                self.assertIn('captions:', refused['error'])
                self.assertIn('raster', refused['error'])

    def test_failed_burn_preserves_provider_artifacts(self):
        # defect: c0dd9c7d4b2f4d5e9b8f763facd96942, rollback deleted provider metadata.
        c = self.case
        accepted = c.accepted()
        self.defect = 'stale'
        caption = self.supply()
        paths = ['records/captions/draft.json', caption['burn']['file']]
        before = {name: (c.folder / name).read_bytes() for name in paths}
        failed = render(c.v, 'draft')
        self.assertEqual(failed['state'], 'render_failed', failed)
        self.assertIn('raster does not bind the current burn', failed['error'])
        self.assertEqual(failed['previous']['artifacts'], accepted['request']['generated'])
        self.assertEqual({name: (c.folder / name).read_bytes() if (c.folder / name).exists()
                          else None for name in paths}, before,
                         'failed burn deleted provider artifacts')
        # defect: review r2 a2c24a75, the next render restored the failed attempt's backup over the provider's fix.
        self.defect = None
        fixed = self.supply()
        fixed_bytes = (c.folder / 'records/captions/draft.json').read_bytes()
        self.assertEqual(render(c.v, 'draft')['state'], 'waiting_owner')
        self.assertEqual(c.get('records/captions/draft.json')['raster'], fixed['raster'])
        self.assertEqual((c.folder / 'records/captions/draft.json').read_bytes(), fixed_bytes)
