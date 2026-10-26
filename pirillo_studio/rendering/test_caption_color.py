"""Decoded caption burns preserve the explicitly bound SDR output range."""
import json
import unittest

from . import test_raster_binding, test_source_rate


class CaptionColor(unittest.TestCase):
    """guard: caption RGB round trips must preserve full-range YUV values."""

    def test_full_range_burn_preserves_picture(self):
        fixture = test_raster_binding.RasterBinding(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        c = fixture.case
        c.settings['shared']['encoder']['engine'] = 'libx264'
        c.settings['shared']['video'].update(pix_fmt='yuv444p', color_range='pc')
        source = test_source_rate.ColorConversion(methodName='runTest')
        source.case = c
        source.source('bt709', 'pc')
        baseline = c.accepted()
        original = source.planes(c.folder / baseline['file'])
        fixture.supply()
        burned = c.accepted()
        actual = source.planes(c.folder / burned['file'])
        # The first frame precedes the caption; every plane must retain its values.
        frame_bytes = 160 * 90 * 3
        self.assertEqual(len(actual), len(original))
        peak = max(abs(a - b) for a, b in zip(original[:frame_bytes], actual[:frame_bytes]))
        print('CAPTION_COLOR_OUTPUT ' + json.dumps({'range': 'pc',
            'file_sha256': burned['file_sha256'], 'decoded_yuv_peak_error': peak}), flush=True)
        self.assertLessEqual(peak, 3, f'caption RGB round trip changed picture by {peak}')
