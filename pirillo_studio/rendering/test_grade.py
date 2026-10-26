"""Approved grades are judged against independently decoded delivered RGB frames."""
import copy
import json
import subprocess
import unittest
from unittest.mock import patch

from ..spine.files import SpineError, canon, digest, filehash
from . import decoded, pixels, records, request, test_compositor
from .render import render
from .test_rendering import PowerLoss


class Grades(unittest.TestCase):
    """e2e: Draft/draft.mp4 carries only the approved finish and source-relative correction."""

    @classmethod
    def setUpClass(cls):
        test_compositor.Compositor.setUpClass()

    def setUp(self):
        self.case = test_compositor.Compositor(methodName='runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        c = self.case
        path = c.folder / 'source.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'testsrc=size=160x90:rate=30', '-f', 'lavfi', '-i',
            'sine=frequency=440:sample_rate=48000', '-t', '1', '-af', 'volume=0.5',
            '-vf', 'geq=lum=100:cb=128:cr=128', '-c:v', 'libx264', '-crf', '0',
            '-pix_fmt', 'yuv444p', *test_compositor.COLOR_TAGS,
            '-c:a', 'aac', str(path)], check=True, capture_output=True)
        c.v.preflight['source_sha256'] = filehash(path)
        c.settings['shared']['video'].update(pix_fmt='yuv444p')
        c.settings['shared']['video']['sizes']['tall'] = [90, 160]
        c.settings['shared']['grade'] = {'skin_target': .6, 'knee': .8, 'max_stop': 1.0, 'n': 2}
        c.cut['spans'][0]['shot'] = 'wide'
        c.save_cut()

    def approved(self, payload):
        return {'decision': 'approve', **payload, 'sha256': digest(canon(payload))}

    def finish(self, name='finish.cube'):
        c = self.case
        # CUBE order is red fastest; this affine transform has an analytic oracle.
        lines = ['LUT_3D_SIZE 2', 'DOMAIN_MIN 0 0 0', 'DOMAIN_MAX 1 1 1']
        for b in (0, 1):
            for g in (0, 1):
                for r in (0, 1):
                    lines.append(f'{.1 + .7*r} {.05 + .8*g} {.15 + .6*b}')
        path = c.folder / name
        path.write_text('\n'.join(lines) + '\n')
        record = self.approved({'source_sha256': c.v.preflight['source_sha256'],
            'lut': {'file': name, 'sha256': filehash(path)},
            'parameters': copy.deepcopy(c.settings['shared']['grade'])})
        job = c.get('records/job.json')
        job['grade'] = record
        c.put('records/job.json', job)
        c.save_cut()
        return record

    def local(self, span=None, exposure=(-.4, .7), contrast=(.9, 1.1), space='rgb'):
        c = self.case
        span = c.cut['spans'][0] if span is None else span
        payload = {'source_sha256': c.v.preflight['source_sha256'], 'src': span['src'],
            'parameters': {'exposure': list(exposure), 'contrast': list(contrast), 'space': space}}
        record = self.approved(payload)
        del record['src']
        span['grade'] = record
        c.save_cut()
        return record

    def resolve(self):
        return request.resolve(self.case.v, 'draft', 'draft')

    def samples(self, path, size):
        raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:v:0',
            '-an', '-fps_mode', 'passthrough', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'],
            check=True, capture_output=True).stdout
        width, height = size
        length, center = width * height * 3, ((height // 2) * width + width // 2) * 3
        self.assertEqual(len(raw) % length, 0)
        return [raw[i + center:i + center + 3] for i in range(0, len(raw), length)]

    def prove(self, result, finish=False, local=False):
        c, req = self.case, result['request']
        size = req['settings']['video']['sizes'][req['settings']['shape']]
        actual = self.samples(c.folder / result['file'], size)
        source = self.samples(c.folder / 'source.mp4', [160, 90])[0]
        expected = []
        for row in req['picture_map']:
            span = next(s for s in c.cut['spans'] if s['id'] == row['span'])
            for index in row['source_indices']:
                rgb = [value / 255 for value in source]
                if finish:
                    rgb = [.1 + .7*rgb[0], .05 + .8*rgb[1], .15 + .6*rgb[2]]
                if local and 'grade' in span:
                    p = span['grade']['parameters']
                    a, b = span['src']
                    phase = min(1, max(0, (index / 30 - a) / (b - a)))
                    exposure, contrast = [p[k][0] + phase*(p[k][1] - p[k][0])
                                          for k in ('exposure', 'contrast')]
                    rgb = [min(1, max(0, contrast*(v - .5) + .5))**(1-exposure/2.4)
                           for v in rgb]
                expected.append([round(v * 255) for v in rgb])
        self.assertEqual(len(actual), len(expected))
        peak = max(abs(a - e) for got, want in zip(actual, expected) for a, e in zip(got, want))
        self.assertLessEqual(peak, 5, f'grade decoded RGB error {peak}')
        tags = decoded.probe(c.folder / result['file'], ['-select_streams', 'v:0', '-show_frames',
            '-show_entries', 'frame=color_range,color_space,color_primaries,color_transfer'])['frames']
        self.assertEqual(len(tags), len(actual))
        for frame in tags:
            for key in ('color_range', 'color_space', 'color_primaries', 'color_transfer'):
                self.assertEqual(frame[key], req['settings']['video'][key])
        print('GRADE_OUTPUT ' + json.dumps({'file_sha256': result['file_sha256'],
            'shape': req['settings']['shape'], 'frames': len(actual), 'rgb_peak_error': peak,
            'finish': finish, 'local': local}), flush=True)

    def test_affine_finish_cube_pixels_and_tags(self):
        self.finish("owner's finish.cube")
        self.prove(self.case.accepted(), finish=True)

    def test_local_ramp_wide_rgb_and_luma(self):
        for shape, space, output_range in (('wide', 'rgb', 'tv'),
                                            ('wide', 'luma', 'tv'), ('wide', 'rgb', 'pc')):
            with self.subTest(shape=shape, space=space, output_range=output_range):
                self.case.settings['profiles']['draft']['shape'] = shape
                self.case.settings['shared']['video'].update(color_range=output_range,
                    pix_fmt='yuvj444p' if output_range == 'pc' else 'yuv444p')
                self.local(space=space)
                self.prove(self.case.accepted(), local=True)

    def test_finish_precedes_local_and_removing_grade_restores_pixels(self):
        c = self.case
        original = c.accepted()
        pixels = self.samples(c.folder / original['file'], [160, 90])
        self.finish()
        self.local()
        self.prove(c.accepted(), finish=True, local=True)
        job = c.get('records/job.json')
        del job['grade']
        c.put('records/job.json', job)
        del c.cut['spans'][0]['grade']
        c.save_cut()
        restored = c.accepted()
        self.assertEqual(self.samples(c.folder / restored['file'], [160, 90]), pixels)

    def test_cold_open_occurrence_preserves_source_relative_phase(self):
        c = self.case
        self.local()
        c.cut['cold_open'] = {'span': 's1', 'words': ['w1', 'w1']}
        c.truth = {'0:w1': [0, .2], '1:w1': [.5, .7]}
        c.save_cut()
        result = c.accepted()
        self.assertGreater(len(result['request']['frame_map']), 1)
        self.prove(result, local=True)

    def test_bad_approval_and_changed_inputs_refuse(self):
        c = self.case
        valid = self.finish()
        first = self.resolve()
        self.assertEqual(first['grade'], valid)
        for label in ('reject', 'redo', 'not this', 'try again', None):
            with self.subTest(decision=label):
                bad = copy.deepcopy(valid)
                bad['decision'] = label
                job = c.get('records/job.json')
                job['grade'] = bad
                c.put('records/job.json', job)
                c.save_cut()
                with self.assertRaisesRegex(SpineError, 'grade|approval'):
                    self.resolve()
        for field in ('sha256', 'source_sha256'):
            with self.subTest(missing=field):
                bad = copy.deepcopy(valid)
                del bad[field]
                job['grade'] = bad
                c.put('records/job.json', job)
                c.save_cut()
                with self.assertRaisesRegex(SpineError, 'grade|hash'):
                    self.resolve()
        job['grade'] = valid
        c.put('records/job.json', job)
        c.save_cut()
        (c.folder / 'finish.cube').write_text('LUT_3D_SIZE 2\n')
        with self.assertRaisesRegex(SpineError, 'grade|hash|contradict'):
            self.resolve()
        valid = self.finish()
        cube = c.folder / 'finish.cube'
        cube.write_text(cube.read_text().replace('0.1 ', '0.2 '))
        valid['lut']['sha256'] = filehash(cube)
        payload = {k: v for k, v in valid.items() if k not in ('decision', 'sha256')}
        job['grade'] = self.approved(payload)
        c.put('records/job.json', job)
        c.save_cut()
        changed_bytes = self.resolve()
        self.assertNotEqual(first['key'], changed_bytes['key'])
        c.settings['shared']['grade']['knee'] = .75
        payload['parameters'] = copy.deepcopy(c.settings['shared']['grade'])
        job['grade'] = self.approved(payload)
        c.put('records/job.json', job)
        c.save_cut()
        self.assertNotEqual(changed_bytes['key'], self.resolve()['key'])

    def test_local_changes_require_new_approval_and_finite_parameters(self):
        c = self.case
        valid = self.local()
        self.assertEqual(self.resolve()['picture_map'][0]['grade']['local']['sha256'], valid['sha256'])
        c.cut['spans'][0]['grade']['parameters']['exposure'][0] = .2
        c.save_cut()
        with self.assertRaisesRegex(SpineError, 'grade|approval'):
            self.resolve()
        c.cut['spans'][0]['grade'] = valid
        for value in ('NaN', 'Infinity', None):
            with self.subTest(value=value):
                self.local(exposure=(value, .4))
                with self.assertRaisesRegex(SpineError, 'grade|exposure|finite'):
                    self.resolve()

        for value in (float('nan'), float('inf'), -float('inf')):
            with self.subTest(nonfinite=value):
                self.local()
                bad = copy.deepcopy(c.cut)
                bad['spans'][0]['grade']['parameters']['exposure'][0] = value
                # Raw malformed JSON must be refused before trusting its approval hash.
                (c.folder / 'records/cut_plan2.json').write_text(json.dumps(bad))
                with self.assertRaises(SpineError):
                    self.resolve()

    def test_symlink_lut_refuses(self):
        c = self.case
        record = self.finish()
        self.assertEqual(self.resolve()['grade'], record)
        (c.folder / 'alias.cube').symlink_to('finish.cube')
        record['lut']['file'] = 'alias.cube'
        record = self.approved({k: v for k, v in record.items() if k not in ('decision', 'sha256')})
        job = c.get('records/job.json')
        job['grade'] = record
        c.put('records/job.json', job)
        c.save_cut()
        with self.assertRaisesRegex(SpineError, 'symlink|grade'):
            self.resolve()

    def test_selected_output_identity_ignores_other_span_grade(self):
        c = self.case
        c.two_spans()
        for span in c.cut['spans']:
            span['shot'] = 'wide'
        spec = c.get('records/shorts_spec.json')
        spec['shorts'][1]['pieces'] = [[0, .4]]
        c.put('records/shorts_spec.json', spec)
        c.save_cut()
        # Exercise selected mapping with the draft compositor; native master remains refused.
        prepare = c.v.prepare
        c.settings['profiles']['short']['shape'] = 'wide'
        c.v.prepare = lambda req: prepare({**req, 'stage': 'draft'})
        first = request.resolve(c.v, 'masters', 'short:one')
        self.local(c.cut['spans'][1])
        other = request.resolve(c.v, 'masters', 'short:one')
        self.assertEqual(first['key'], other['key'])
        self.local(c.cut['spans'][0])
        selected = request.resolve(c.v, 'masters', 'short:one')
        self.assertNotEqual(first['key'], selected['key'])
        self.assertTrue(all(row['span'] == 's1' for row in selected['picture_map']))

    def test_interrupted_grade_publish_restores_previous_output_before_retry(self):
        c = self.case
        first = c.accepted()
        accepted = (c.folder / first['file']).read_bytes()
        self.finish()
        write = records.write
        def lose_power(video, name, value):
            if name == 'records/render/draft.json' and value.get('state') == 'waiting_owner':
                raise PowerLoss('after grade publish before record acceptance')
            return write(video, name, value)
        with patch.object(records, 'write', side_effect=lose_power), self.assertRaises(PowerLoss):
            render(c.v, 'draft')
        self.assertNotEqual((c.folder / first['file']).read_bytes(), accepted)
        encode = c.v.encode
        def inspect_restore(req, path):
            self.assertEqual((c.folder / first['file']).read_bytes(), accepted)
            raise OSError('stop after recovery before replacement')
        c.v.encode = inspect_restore
        failed = render(c.v, 'draft')
        self.assertEqual(failed['state'], 'render_failed')
        self.assertEqual((c.folder / first['file']).read_bytes(), accepted)
        c.v.encode = encode
        self.prove(c.accepted(), finish=True)

    def test_stale_picture_binding_and_unknown_parameters_refuse(self):
        c = self.case
        self.local()
        result = c.accepted()
        wrong = copy.deepcopy(result['request'])
        del wrong['picture_map'][0]['grade']
        with self.assertRaisesRegex(SpineError, 'grade'):
            pixels.compare(c.v, wrong, c.folder / result['file'])
        for change in ({'space': 'hsv'}, {'saturation': [1, 2]}, {'contrast': None}):
            with self.subTest(change=change):
                record = self.local()
                record['parameters'].update(change)
                payload = {k: v for k, v in record.items() if k not in ('decision', 'sha256')}
                record['sha256'] = digest(canon({**payload, 'src': c.cut['spans'][0]['src']}))
                c.save_cut()
                with self.assertRaisesRegex(SpineError, 'grade'):
                    self.resolve()

    def test_rational_retime_grade_uses_exact_selected_source_instant(self):
        from . import grade
        # At output 45, speed 7/5 selects source frame 63 exactly, not floor(62.999...).
        # A legal steep ramp makes this one-frame phase error visible in delivered RGB.
        local = {'src': [0, 64/30], 'parameters': {'exposure': [-100, 2.3],
                 'contrast': [1, 1], 'space': 'rgb'}}
        req = {'source_rate': '30', 'settings': {'video': {'fps': '30'}},
               'plan_data': {'spans': [{'id': 's1', 'speed': '7/5'}]}}
        row = {'span': 's1', 'source_grid_frames': [0, 64],
               'grade': {'finish': None, 'local': local},
               'color_conversion': {'output': {'color_range': 'tv'}}}
        chain = 'format=yuv444p,geq=lum=100:cb=128:cr=128,' + grade.filters(self.case.v, req, row)
        chain += ',scale=in_color_matrix=bt709:in_range=tv:out_range=pc,format=rgb24'
        path = self.case.folder / 'retimed-grade.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'testsrc=size=2x2:rate=30', '-frames:v', '46', '-vf', chain,
            '-c:v', 'libx264rgb', '-crf', '0', '-pix_fmt', 'rgb24',
            '-color_range', 'pc', '-colorspace', 'bt709', '-color_trc', 'bt709',
            '-color_primaries', 'bt709', str(path)], check=True, capture_output=True)
        frames = self.samples(path, [2, 2])
        self.assertEqual(len(frames), 46)
        exposure = -100 + 102.3 * 63 / 64
        expected = round(255 * ((100 - 16) / 219)**(1 - exposure / 2.4))
        actual = list(frames[45])
        peak = max(abs(v - expected) for v in actual)
        print('GRADE_RETIME_PHASE ' + json.dumps({'source_frame': 63, 'output_frame': 45,
            'expected_rgb': expected, 'actual_rgb': actual, 'peak_error': peak}), flush=True)
        self.assertLessEqual(peak, 3)
