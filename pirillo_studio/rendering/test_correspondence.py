"""Split-clock correspondence through independently encoded master files."""
import copy
import json
import subprocess
import unittest
from unittest.mock import patch

from ..spine.files import SpineError, filehash
from . import compositor, correspondence, coverage, mapping, records
from .render import Video, render
from . import test_compositor
from .test_rendering import PowerLoss


class SplitCorrespondence(unittest.TestCase):
    """e2e: YouTube/main.mp4 and Shorts/one.mp4 compared to the approved split-clock draft."""

    @classmethod
    def setUpClass(cls):
        test_compositor.Compositor.setUpClass()

    def setUp(self):
        self.c = test_compositor.Compositor()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.c.two_spans()
        self.c.settings['shared']['video']['sizes']['tall'] = [50, 90]
        self.c.settings['shared']['video']['shot_fractions']['med'] = .8
        self.c.settings['shared']['verification']['master_checks'] = ['draft-match']
        self.c.settings['shared']['verification']['render_checks'] = ['frame-map', 'cards']
        # Independent fixture clock: 12 voice frames, 15 picture frames before the J-cut.
        self.picture = [(0, 15, [16, 9, 128, 72]), (21, 30, [40, 22, 80, 45])]
        self.voice = [(0, .4), (.6, 1)]
        self.gain = 1
        self.step = 1
        self.gray = False
        self.master = Video(self.c.folder, 'masters', versions={'adapter': 'fixture-1'},
            preflight=self.c.v.preflight, settings=self.c.settings,
            encode=self.encode, prepare=lambda req: compositor.prepare(self.c.v, {**req, 'stage': 'draft'}))

    def encode(self, req, path):
        self.c.encodes += 1
        width, height = req['settings']['video']['sizes'][req['settings']['shape']]
        graph = []
        for i, (a, b, crop) in enumerate(self.picture):
            x, y, w, h = crop
            graph.append(f'[0:v]trim=start_frame={a}:end_frame={b},setpts=PTS-STARTPTS,'
                         f"select='not(mod(n,{self.step}))',setpts=N/(30*TB),crop={w}:{h}:{x}:{y}:exact=1,"
                         f'scale={width}:{height}:flags=bilinear,format=yuv420p,setsar=1[v{i}]')
        for i, (a, b) in enumerate(self.voice):
            graph.append(f'[0:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS,volume={self.gain}[a{i}]')
        graph += [''.join(f'[v{i}]' for i in range(len(self.picture))) + f'concat=n={len(self.picture)}:v=1:a=0,settb=AVTB,setpts=N/(30*TB)[v]',
                  ''.join(f'[a{i}]' for i in range(len(self.voice))) + f'concat=n={len(self.voice)}:v=0:a=1[a]']
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(self.c.folder / 'source.mp4'),
            '-filter_complex', ';'.join(graph), '-map', '[v]', '-map', '[a]', '-c:v', 'libx264', '-crf', '0',
            '-pix_fmt', 'yuv420p', '-r', '30', *test_compositor.COLOR_TAGS, '-c:a', 'aac', str(path)], check=True, capture_output=True)

    def draft(self):
        self.master.stage = 'draft'
        result = render(self.master, 'draft')
        self.assertEqual(result['state'], 'waiting_owner', {k: result[k] for k in ('state', 'error', 'reading') if k in result})
        self.master.stage = 'masters'
        self.c.approve()
        return result

    def accepted(self, output='long'):
        result = render(self.master, output)
        self.assertEqual(result['state'], 'delivered', {k: result[k] for k in ('state', 'error', 'reading', 'reason') if k in result})
        checks = self.c.get(result['readings'])['checks']
        self.assertTrue(checks['draft-match']['ok'], checks['draft-match'])
        self.assertTrue(checks['frame-map']['ok'], checks['frame-map'])
        # The encoded file's decoded frame readings must match this fixture's independent clock.
        actual = self.c.get(result['readings'])['mapped_frames']
        expected = [n for a, b, _ in self.picture for n in range(a, b, self.step)]
        self.assertEqual([r['source_frame'] for r in actual], expected)
        independent_peak = None
        if self.gray:
            def rgb(path):
                return subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:v:0',
                    '-an', '-fps_mode', 'passthrough', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'],
                    check=True, capture_output=True).stdout
            source, delivered = rgb(self.c.folder / 'source.mp4'), rgb(self.c.folder / result['file'])
            width, height = result['request']['settings']['video']['sizes'][result['request']['settings']['shape']]
            length = width * height * 3
            self.assertEqual(len(delivered), len(expected) * length)
            independent_peak = 0
            for frame, index in enumerate(expected):
                pixel = source[index * 160 * 90 * 3:index * 160 * 90 * 3 + 3]
                independent_peak = max(independent_peak, max(abs(value - pixel[n % 3]) for n, value
                    in enumerate(delivered[frame * length:(frame + 1) * length])))
            self.assertLessEqual(independent_peak, 3)
        print('SPLIT_MASTER_OUTPUT ' + json.dumps({'output': output, 'sha256': result['file_sha256'],
            'independent_rgb_peak_error': independent_peak,
            'pixels': checks['frame-map']['value'], 'draft': checks['draft-match']['value']}), flush=True)
        return result

    def test_jcut_long_and_vertical_keep_both_clocks(self):
        spec = self.c.get('records/shorts_spec.json')
        spec['shorts'][1].update(pieces=[[0, .41], [.6, 1]], ends_on='w2')
        self.c.put('records/shorts_spec.json', spec)
        self.c.save_cut()
        self.draft()
        result = self.accepted()
        self.assertEqual(self.c.get(result['readings'])['checks']['draft-match']['value']['picture']['frames'], 24)
        self.picture = [(0, 15, [60, 9, 40, 72]), (21, 30, [68, 22, 25, 45])]
        self.accepted('vertical')
        self.accepted('short:one')
        self.gain = .2
        self.c.settings['profiles']['vertical']['encoder'] = {**self.c.settings['shared']['encoder'], 'crf': 1}
        refused = render(self.master, 'vertical')
        self.assertEqual((refused['state'], refused['reading']), ('measure_refused', 'draft-match'), refused)
        self.gain = 1
        self.picture[0] = (0, 15, [0, 0, 40, 72])
        self.c.settings['profiles']['vertical']['encoder']['crf'] = 2
        refused = render(self.master, 'vertical')
        self.assertEqual((refused['state'], refused.get('reading')), ('measure_refused', 'frame-map'), {k: refused[k] for k in ('state', 'error', 'step') if k in refused})

    def test_short_uses_voice_positions_for_its_picture(self):
        c = self.c
        c.two_spans('jump')
        self.picture = [(0, 12, [16, 9, 128, 72]), (18, 30, [40, 22, 80, 45])]
        spec = c.get('records/shorts_spec.json')
        spec['shorts'][1].update(pieces=[[.1, .3], [.6, .9]], ends_on='w2')
        c.put('records/shorts_spec.json', spec)
        c.save_cut()
        self.draft()
        self.picture = [(3, 9, [60, 9, 40, 72]), (18, 27, [68, 22, 25, 45])]
        self.voice = [(.1, .3), (.6, .9)]
        result = self.accepted('short:one')
        segments = c.get(result['readings'])['checks']['draft-match']['value']['audio']['segments']
        self.assertEqual([s['src'] for s in segments], [[.1, .3], [.4, .7]])
        self.assertEqual([s['out'] for s in segments], [[0, .2], [.2, .5]])
        # The locked-plan map check refuses altered source indices before correspondence.
        req = copy.deepcopy(result['request'])
        req['picture_map'][0]['source_indices'][0] += 1
        with self.assertRaisesRegex(SpineError, 'frame-map: source asset or source frames differ from request'):
            coverage.draft(self.master, req, c.folder / result['file'], True)

    def test_repeated_cold_open_is_unambiguous_only_for_full_output(self):
        c = self.c
        c.two_spans('jump')
        c.cut['cold_open'] = {'span': 's1', 'words': ['w1', 'w1']}
        c.truth = {'0:w1': [0, 4/30], '1:w1': [.2, .35], '2:w2': [.6, .75]}
        c.save_cut()
        self.picture = [(2, 6, [16, 9, 128, 72]), (0, 12, [16, 9, 128, 72]), (18, 30, [40, 22, 80, 45])]
        self.voice = [(2/30, .2), (0, .4), (.6, 1)]
        self.draft()
        self.accepted()
        spec = c.get('records/shorts_spec.json')
        spec['shorts'][1]['pieces'] = [[.1, .2]]
        c.put('records/shorts_spec.json', spec)
        c.save_cut()
        self.picture, self.voice = [(3, 6, [60, 9, 40, 72])], [(.1, .2)]
        refused = render(self.master, 'short:one')
        self.assertEqual(refused['state'], 'measure_refused', refused)
        self.assertIn('unambiguous approved-draft occurrence', refused['error'])

    def test_source_sampling_keeps_exact_approved_indices(self):
        c = self.c
        source = c.folder / 'source.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'nullsrc=size=160x90:rate=60', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
            '-t', '1', '-vf', "geq=lum='32+mod(N*7,180)':cb=128:cr=128",
            '-af', 'volume=0.5', '-c:v', 'libx264', '-crf', '0', '-pix_fmt', 'yuv420p',
            *test_compositor.COLOR_TAGS, '-c:a', 'aac', str(source)], check=True, capture_output=True)
        c.v.preflight['source_sha256'] = filehash(source)
        c.put('records/faces.json', {'frames': [
            {'frame': i, 'boxes': [[70, 35, 20, 20]]} for i in range(60)]})
        c.save_cut()
        self.step = 2
        self.gray = True
        self.picture = [(0, 30, [16, 9, 128, 72]), (42, 60, [40, 22, 80, 45])]
        self.draft()
        self.accepted()
        c.cut['spans'][0].update(enter='push', push={'to': 'close', 's': .2})
        c.save_cut()
        for shape, start, end, ratio in (
                ('wide', [16, 9, 128], [40, 22, 80], 90/160),
                ('tall', [60, 9, 40], [68, 22, 25], 90/50)):
            # Independent fixture interpolation, with a source-rate step of two.
            self.picture = []
            for frame in range(15):
                phase = min(frame / 6, 1)
                x, y, w = [round(a + (b - a) * phase) for a, b in zip(start, end)]
                self.picture.append((frame * 2, frame * 2 + 2, [x, y, w, round(w * ratio)]))
            x, y, w = end
            self.picture.append((42, 60, [x, y, w, round(w * ratio)]))
            if shape == 'wide':
                self.draft()
            self.accepted('long' if shape == 'wide' else 'vertical')

    def test_valid_maps_with_different_frame_rates_refuse_correspondence(self):
        draft = self.draft()
        mapping.validate(draft['request'])
        # A valid full-output crop can differ from the viewed draft without changing its clocks.
        req = copy.deepcopy(draft['request'])
        req['output'] = 'long'
        req['picture_map'][0]['crop'][0] += 1
        mapping.validate(req)
        self.assertFalse(correspondence.split(req, draft)[0])
        for rate in ('24', '30000/1001'):
            with self.subTest(rate=rate):
                req = copy.deepcopy(draft['request'])
                req['settings']['video']['fps'] = rate
                req['output'] = 'long'
                req.update(compositor.prepare(self.c.v, req))
                mapping.validate(req)
                with self.assertRaisesRegex(SpineError, 'draft-match: draft and master frame rates differ'):
                    correspondence.split(req, draft)

    def test_identity_boundaries_and_interrupted_publication(self):
        draft = self.draft()
        accepted = self.accepted()
        req = accepted['request']
        name = 'records/render/draft.json'
        evidence = self.c.get(draft['readings'])
        evidence_path = self.c.folder / draft['readings']
        evidence_bytes = evidence_path.read_bytes()
        refusals = {
            'missing-key': 'draft-match: approved draft record missing key',
            'request-key': 'draft-match: draft request identity differs from its record',
            'stale-readings': 'draft-match: stale decoded draft evidence',
            'failed-reading': 'draft-match: missing or failed draft frame-map reading',
            'map': 'frame-map: frames differs from locked source order/edges/hold',
            'nonfinite': 'non-finite',
            'alias': 'symlink',
        }
        for defect, refusal in refusals.items():
            with self.subTest(defect=defect):
                changed = copy.deepcopy(draft)
                if defect == 'missing-key':
                    del changed['key']
                elif defect == 'request-key':
                    changed['request']['key'] = 'sha256:' + '0' * 64
                elif defect == 'stale-readings':
                    changed['readings_sha256'] = 'sha256:' + '0' * 64
                elif defect == 'failed-reading':
                    bad = copy.deepcopy(evidence)
                    bad['checks']['frame-map']['ok'] = False
                    self.c.put(draft['readings'], bad)
                    changed['readings_sha256'] = filehash(self.c.folder / draft['readings'])
                elif defect == 'map':
                    changed['picture_map'][0]['frames'][1] += 1
                elif defect == 'nonfinite':
                    changed['request']['frame_map'][0]['src'][0] = float('nan')
                else:
                    alias = self.c.folder / 'draft-alias.mp4'
                    alias.symlink_to(self.c.folder / draft['file'])
                    changed['file'] = 'draft-alias.mp4'
                # Non-finite JSON is refused at the same read boundary as corrupt records.
                target = self.c.folder / name
                target.write_text(json.dumps(changed))
                with self.assertRaisesRegex(SpineError, refusal):
                    self.master.measure(req, self.c.folder / accepted['file'])
                self.c.put(name, draft)
                evidence_path.write_bytes(evidence_bytes)
        self.c.settings['profiles']['long']['encoder'] = {**self.c.settings['shared']['encoder'], 'crf': 1}
        write = records.write
        def crash(video, name, value):
            if name == 'records/render/long.json' and value['state'] == 'delivered':
                raise PowerLoss()
            return write(video, name, value)
        with patch.object(records, 'write', side_effect=crash), self.assertRaises(PowerLoss):
            render(self.master, 'long')
        self.accepted()
        encodes = self.c.encodes
        self.accepted()
        self.assertEqual(self.c.encodes, encodes)
