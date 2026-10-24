"""Actual lossless media proves pixel correspondence independently of renderer claims."""
import copy
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ..spine.files import SpineError, filehash
from . import pixels, request
from .render import MeasurementRefused


class PixelCorrespondence(unittest.TestCase):
    """e2e: delivered.mkv compared to the source and opaque-cover frame assets."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.video = SimpleNamespace(folder=self.folder)
        self.source = self.folder / 'source.mkv'
        self.output = self.folder / 'delivered.mkv'
        self.make(self.source, 'color=red:size=32x24:rate=4:duration=1,drawbox=color=blue:t=fill:enable=gte(n\\,2)')
        self.req = {'file': 'delivered.mkv', 'inputs': {'source.mkv': filehash(self.source)},
                    'settings': {'shape': 'wide', 'video': {'fps': '4/1', 'sizes': {'wide': [32, 24]}},
                                 'measurement': request.settings()['shared']['measurement']},
                    'frame_map': [self.row([0, 2], [2, 4]), self.row([2, 4], [0, 2])]}
        self.command('-i', str(self.source), '-filter_complex',
                     '[0:v]split[a][b];[a]trim=start_frame=2:end_frame=4,setpts=PTS-STARTPTS[c];'
                     '[b]trim=start_frame=0:end_frame=2,setpts=PTS-STARTPTS[d];[c][d]concat=n=2:v=1:a=0[v]',
                     '-map', '[v]', '-c:v', 'ffv1', str(self.output))

    def command(self, *args):
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', *args], check=True, capture_output=True)

    def make(self, path, source):
        self.command('-f', 'lavfi', '-i', source, '-c:v', 'ffv1', str(path))

    def row(self, out, source, kind='source', name='source.mkv'):
        return {'span': 'span-' + str(out[0]), 'frames': out, 'source_frames': source,
                'asset': {'file': name, 'sha256': filehash(self.folder / name)}, 'crop': [0, 0, 32, 24], 'kind': kind}

    def compare(self):
        return pixels.compare(self.video, self.req, self.output)

    def test_full_hd_comparison_cost(self):
        # Regression: per-byte Python comparison made a master take hours to measure.
        count = 8
        self.make(self.source, 'testsrc2=size=1920x1080:rate=4:duration=2')
        self.output.write_bytes(self.source.read_bytes())
        self.req['inputs']['source.mkv'] = filehash(self.source)
        self.req['settings']['video']['sizes']['wide'] = [1920, 1080]
        row = self.row([0, count], [0, count])
        row['crop'] = [0, 0, 1920, 1080]
        self.req['frame_map'] = [row]
        start = time.perf_counter()
        result = self.compare()
        per_frame = (time.perf_counter() - start) / count
        print(f'1080p comparison: {per_frame:.6f} s/frame; {count} frames; {filehash(self.source)}', flush=True)
        self.assertEqual(len(result['frame_checks']), count)
        self.assertTrue(result['checks']['frame-map']['ok'])
        self.assertLess(per_frame, 0.35, 'per-frame comparison exceeds regression budget')

    def test_locked_plan_is_the_viewed_draft(self):
        result = self.compare()
        self.assertTrue(result['checks']['frame-map']['ok'])
        self.assertEqual([item['source_frame'] for item in result['frame_checks']], [2, 3, 0, 1])
        self.output.write_bytes(self.source.read_bytes())
        rejected = self.compare()
        self.assertFalse(rejected['checks']['frame-map']['ok'])
        self.assertEqual(rejected['checks']['frame-map']['value']['failed_frames'], [0, 1, 2, 3])

    def test_source_rate_mismatch_refuses(self):
        for source in ('color=red:size=32x24:rate=8:duration=0.5',
                       'color=red:size=32x24:rate=4:duration=1,setpts=PTS+gte(N\\,2)'):
            with self.subTest(source=source):
                self.make(self.source, source + ',drawbox=color=blue:t=fill:enable=gte(n\\,2)')
                sha = filehash(self.source)
                self.req['inputs']['source.mkv'] = sha
                for row in self.req['frame_map']:
                    row['asset']['sha256'] = sha
                with self.assertRaisesRegex(MeasurementRefused, 'source frame rate'):
                    self.compare()

    def test_one_exposed_edge_cannot_pass_opaque_cover(self):
        cover = self.folder / 'cover.mkv'
        self.make(cover, 'color=red:size=32x24:rate=4:duration=0.25')
        self.req['inputs']['cover.mkv'] = filehash(cover)
        self.req['frame_map'] = [self.row([0, 4], [0, 1], 'cover', 'cover.mkv')]
        self.make(self.output, 'color=red:size=32x24:rate=4:duration=1')
        self.assertTrue(self.compare()['checks']['cards']['ok'])
        self.make(self.output, 'color=red:size=32x24:rate=4:duration=1,drawbox=x=0:y=0:w=1:h=24:color=blue:t=fill')
        failed = self.compare()
        self.assertFalse(failed['checks']['cards']['ok'])
        self.assertEqual(failed['checks']['cards']['value']['failed_frames'], [0, 1, 2, 3])

    def test_missing_extra_and_unmapped_frames_refuse(self):
        original = copy.deepcopy(self.req)
        for rows in ([self.row([0, 2], [2, 4])],
                     [self.row([0, 5], [0, 5])],
                     [self.row([1, 3], [2, 4])]):
            with self.subTest(rows=rows):
                self.req = copy.deepcopy(original)
                self.req['frame_map'] = rows
                with self.assertRaises(MeasurementRefused):
                    self.compare()

    def test_unsafe_stale_crop_and_nonfinite_bindings_refuse(self):
        original = copy.deepcopy(self.req)
        for mutation in ('symlink', 'stale', 'crop', 'nan'):
            with self.subTest(mutation=mutation):
                self.req = copy.deepcopy(original)
                if mutation == 'symlink':
                    (self.folder / 'alias.mkv').symlink_to(self.source)
                    row = self.req['frame_map'][0]
                    row['asset']['file'] = 'alias.mkv'
                    self.req['inputs']['alias.mkv'] = row['asset']['sha256']
                elif mutation == 'stale':
                    self.req['inputs']['source.mkv'] = 'sha256:stale'
                elif mutation == 'crop':
                    self.req['frame_map'][0]['crop'][2] += 1
                else:
                    self.req['settings']['measurement']['pixel_mean_error_max'] = float('nan')
                with self.assertRaises((MeasurementRefused, SpineError)):
                    self.compare()

    def test_asset_mutation_during_decode_refuses(self):
        original = pixels.frames
        def mutate(path, size, **kwargs):
            yield from original(path, size, **kwargs)
            if path == self.output:
                with self.source.open('ab') as handle:
                    handle.write(b'changed')
        with patch.object(pixels, 'frames', side_effect=mutate):
            with self.assertRaises(MeasurementRefused):
                self.compare()
