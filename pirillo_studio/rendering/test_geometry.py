"""Real asset metadata must not silently change the compositor's crop coordinates."""
import subprocess
import unittest

from ..spine.files import SpineError, filehash
from . import decoded, pixels, request, test_compositor


class Geometry(unittest.TestCase):
    """e2e: source.mp4 and cover.mp4 reject unsupported display geometry before encoding."""

    @classmethod
    def setUpClass(cls):
        test_compositor.Compositor.setUpClass()

    def setUp(self):
        self.case = test_compositor.Compositor()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def asset(self, kind, geometry):
        case = self.case
        original = case.folder / 'original.mp4'
        original.write_bytes(case.media)
        path = case.folder / ('source.mp4' if kind == 'source' else 'cover.mp4')
        args = (['-c', 'copy'] if geometry == 'rotation' else
                ['-vf', 'setsar=0' if geometry == 'unknown' else 'setsar=2/1', '-c:v', 'libx264', '-crf', '0',
                 *test_compositor.COLOR_TAGS, '-c:a', 'copy'])
        inputs = ['-display_rotation:v:0', '90'] if geometry == 'rotation' else []
        subprocess.run(['ffmpeg', '-v', 'error', '-y', *inputs, '-i', str(original), *args, str(path)],
                       check=True, capture_output=True)
        stream = decoded.probe(path, ['-select_streams', 'v:0', '-show_streams'])['streams'][0]
        self.assertEqual([stream['width'], stream['height']], [160, 90])
        if geometry == 'rotation':
            self.assertTrue(any(abs(r.get('rotation', 0)) == 90 for r in stream['side_data_list']))
        elif geometry == 'unknown':
            self.assertNotIn('sample_aspect_ratio', stream)
        else:
            self.assertEqual(stream['sample_aspect_ratio'], '2:1')
        if kind == 'source':
            case.v.preflight['source_sha256'] = filehash(path)
        else:
            case.settings['profiles']['draft']['shape'] = 'tall'
            case.settings['shared']['video']['sizes']['tall'] = [160, 90]
            case.cut['spans'][0]['hide_picture'] = True
            case.put('records/overlay_plan.json', {'plan': case.cut['key'], 'cards': [
                {'id': 'cover', 'kind': 'cover', 'covers': 's1', 'file': 'cover.mp4'}]})
        case.save_cut()
        return stream

    def test_source_rotation_refused(self):
        result = request.resolve(self.case.v, 'draft', 'draft')
        self.assertEqual(result['picture_map'][0]['kind'], 'source')
        self.assertEqual(len(result['picture_map'][0]['crop']), 4)
        self.asset('source', 'rotation')
        with self.assertRaisesRegex(SpineError, 'geometry:.*rotation'):
            request.resolve(self.case.v, 'draft', 'draft')

    def test_cover_rotation_refused(self):
        self.asset('cover', 'rotation')
        with self.assertRaisesRegex(SpineError, 'geometry:.*rotation'):
            request.resolve(self.case.v, 'draft', 'draft')

    def test_source_non_square_pixels_refused(self):
        self.asset('source', 'sar')
        with self.assertRaisesRegex(SpineError, 'geometry:.*sample aspect ratio'):
            request.resolve(self.case.v, 'draft', 'draft')

    def test_cover_non_square_pixels_refused(self):
        self.asset('cover', 'sar')
        with self.assertRaisesRegex(SpineError, 'geometry:.*sample aspect ratio'):
            request.resolve(self.case.v, 'draft', 'draft')

    def test_unknown_sample_aspect_ratio_is_square(self):
        for kind in ('source', 'cover'):
            with self.subTest(kind=kind):
                stream = self.asset(kind, 'unknown')
                self.assertEqual(pixels.geometry(stream), [160, 90])
                self.assertEqual(pixels.geometry({**stream, 'sample_aspect_ratio': '0:1'}), [160, 90])
                result = request.resolve(self.case.v, 'draft', 'draft')
                self.assertEqual(result['picture_map'][0]['kind'], kind)
