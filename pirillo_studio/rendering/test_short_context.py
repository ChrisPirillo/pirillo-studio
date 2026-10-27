"""Whole-short context is proved from the delivered pixels, not the adapter's claims."""
import copy
import json
import subprocess
import unittest
from unittest.mock import patch

from ..spine.files import SpineError, filehash
from . import cards, compositor, records, request
from .render import render
from . import test_cards
from . import test_coverage
from .test_rendering import PowerLoss


class ShortContext(unittest.TestCase):
    """e2e: Shorts/one.mp4 with the settled context visible on every decoded frame."""

    @classmethod
    def setUpClass(cls):
        test_coverage.RendererCoverage.setUpClass()

    def setUp(self):
        self.c = test_coverage.RendererCoverage('runTest')
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        c = self.c
        c.settings['profiles']['short'].update(cards_safe=[2, 2, 46, 86],
                                               render_checks=['context', 'word-ends'])
        self.asset = test_cards.Cards.raster(self, 'context.nut', intervals=((0, 30),),
                                  size=(50, 90), box=(4, 4, 42, 12))
        self.standalone = {'context': 'The subject of this complete excerpt.', 'raster': self.asset}
        self.bind(self.standalone)
        c.accepted()
        c.approve()
        c.v.stage = 'masters'
        original = c.prepare
        def prepare(req):
            bound = original(req)
            bound['elements'] = cards.prepare(c.v, {**req, **bound})
            return bound
        c.v.prepare = prepare
        c.v.encode = self.encode

    def bind(self, standalone):
        spec = self.c.get('records/shorts_spec.json')
        spec['shorts'][1]['stands_alone'] = standalone
        self.c.put('records/shorts_spec.json', spec)
        self.c.bind_cut()

    def encode(self, req, path):
        # This fixture's adapter uses fixed source/crop/clock facts, not the production layer graph.
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(self.c.folder / 'source.mp4'),
            '-i', str(self.c.folder / 'context.nut'), '-filter_complex',
            '[0:v]crop=50:90:55:0:exact=1,scale=50:90:flags=bilinear,format=rgb24[base];'
            '[base][1:v]overlay=format=rgb:shortest=1:repeatlast=0[v]',
            '-map', '[v]', '-map', '0:a', '-c:v', 'libx264rgb', '-crf', '0',
            '-pix_fmt', 'gbrp', '-colorspace', 'bt709', '-color_primaries', 'bt709',
            '-color_trc', 'bt709', '-color_range', 'tv', '-c:a', 'copy', str(path)], check=True, capture_output=True)

    def delivered(self):
        result = render(self.c.v, 'short:one')
        self.assertEqual(result['state'], 'delivered', result)
        return result

    def test_complete_context_pixels_identity_and_reuse(self):
        result = self.delivered()
        actual = test_cards.Cards.decode(self, result['file'])
        expected = bytearray(subprocess.run(['ffmpeg', '-v', 'error', '-i', str(self.c.folder / 'source.mp4'),
            '-vf', 'crop=50:90:55:0:exact=1,scale=50:90:flags=bilinear,format=rgb24',
            '-an', '-fps_mode', 'passthrough', '-f', 'rawvideo', 'pipe:1'], check=True, capture_output=True).stdout)
        self.assertEqual(len(actual), 30 * 50 * 90 * 3)
        self.assertEqual(len(expected), len(actual))
        for frame in range(30):
            for y in range(4, 16):
                for x in range(4, 46):
                    start = ((frame * 90 + y) * 50 + x) * 3
                    for channel, value in enumerate((230, 40, 90)):
                        expected[start + channel] = round((value * 128 + expected[start + channel] * 127) / 255)
        peak = max(abs(a - b) for a, b in zip(actual, expected))
        self.assertLessEqual(peak, 2)
        context = self.c.get(result['readings'])['checks']['context']
        self.assertTrue(context['ok'], context)
        self.assertEqual(context['coverage']['frames'], 30)
        self.assertEqual(context['value']['text'], self.standalone['context'])
        self.assertEqual(context['value']['raster']['bounds'], [{'frames': [0, 30], 'box': [4, 4, 42, 12]}])
        with patch.object(self.c.v, 'encode', side_effect=AssertionError('cached encode')):
            self.assertEqual(self.delivered()['file_sha256'], result['file_sha256'])
        changed = {**self.standalone, 'context': 'A revised subject line.'}
        self.bind(changed)
        self.assertNotEqual(request.resolve(self.c.v, 'masters', 'short:one')['key'], result['key'])
        with patch.object(request, 'RENDERER_VERSION', 'rendering-2h-15'):
            prior = request.resolve(self.c.v, 'masters', 'short:one')['key']
        self.assertNotEqual(request.resolve(self.c.v, 'masters', 'short:one')['key'], prior)
        print(json.dumps({'output': result['file'], 'frames': 30, 'rgb_bytes': len(actual),
                          'independent_rgb_peak_error': peak, 'sha256': result['file_sha256']}), flush=True)

    def test_invalid_context_inputs_preserve_accepted_file(self):
        accepted = self.delivered()
        original = (self.c.folder / accepted['file']).read_bytes()
        for defect, diagnostic in [('missing-raster', 'raster'), ('missing-hash', 'hash'),
                ('stale-hash', 'contradicts'), ('symlink', 'symlink'), ('nonfinite', 'finite'),
                ('missing-safe', 'safe'), ('edge', 'edge'), ('contradiction', 'context'), ('empty', 'context')]:
            with self.subTest(defect=defect):
                bad = copy.deepcopy(self.standalone)
                profile = self.c.settings['profiles']['short']
                if defect == 'missing-raster':
                    bad.pop('raster')
                elif defect == 'missing-hash':
                    bad['raster'].pop('sha256')
                elif defect == 'stale-hash':
                    bad['raster']['sha256'] = 'sha256:' + '0' * 64
                elif defect == 'symlink':
                    (self.c.folder / 'alias.nut').symlink_to(self.c.folder / 'context.nut')
                    bad['raster']['file'] = 'alias.nut'
                elif defect == 'nonfinite':
                    profile['cards_safe'] = [float('nan'), 2, 46, 86]
                elif defect == 'missing-safe':
                    profile.pop('cards_safe')
                elif defect == 'edge':
                    bad['raster'] = test_cards.Cards.raster(self, 'early.nut', intervals=((1, 30),),
                                                size=(50, 90), box=(4, 4, 42, 12))
                elif defect == 'contradiction':
                    bad['opens_on_subject'] = True
                else:
                    bad['context'] = ''
                self.bind(bad)
                with self.assertRaisesRegex(SpineError, diagnostic):
                    render(self.c.v, 'short:one')
                self.assertEqual((self.c.folder / accepted['file']).read_bytes(), original)
                profile['cards_safe'] = [2, 2, 46, 86]
        self.bind(self.standalone)
        with self.assertRaisesRegex(SpineError, 'native masters unsupported'):
            compositor.prepare(self.c.v, request.resolve(self.c.v, 'masters', 'short:one'))

    def test_missing_delivered_layer_and_crash_recover_accepted_output(self):
        accepted = self.delivered()
        original = (self.c.folder / accepted['file']).read_bytes()
        self.c.settings['profiles']['short']['encoder'] = {**self.c.settings['shared']['encoder'], 'crf': 1}
        self.c.v.encode = self.c.encode_map
        refused = render(self.c.v, 'short:one')
        self.assertEqual((refused['state'], refused['reading']), ('measure_refused', 'frame-map'), refused)
        self.assertEqual((self.c.folder / accepted['file']).read_bytes(), original)
        self.c.v.encode = self.encode
        self.c.settings['profiles']['short']['encoder']['crf'] = 2
        write = records.write
        def crash(video, name, value):
            if name == 'records/render/short-one.json' and value['state'] == 'delivered':
                raise PowerLoss()
            return write(video, name, value)
        with patch.object(records, 'write', side_effect=crash), self.assertRaises(PowerLoss):
            render(self.c.v, 'short:one')
        self.assertEqual(self.c.saved('short:one')['state'], 'measuring')
        recovered = self.delivered()
        self.assertNotEqual(recovered['key'], accepted['key'])
        self.assertEqual(recovered['file_sha256'], filehash(self.c.folder / recovered['file']))
