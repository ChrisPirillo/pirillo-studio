"""Grade support cannot bypass adapter or native composition boundaries."""
import copy
import json
import unittest

from ..spine.files import SpineError, filehash
from . import compositor, coverage, framing, mapping, pixels, test_grade, test_raster_binding


class GradeAdapters(unittest.TestCase):
    """defect: 46f6428cc38b41a6ba4b7b6f628773c2 and 1c72fe7477c34840a20babfee8eec9a7, adapter/tall refusals."""

    def setUp(self):
        self.grade = test_grade.Grades(methodName='runTest')
        self.grade.setUpClass()
        self.grade.setUp()
        self.addCleanup(self.grade.doCleanups)

    def test_unprepared_adapter_refuses_local_grade(self):
        g = self.grade
        g.case.v.prepare = None
        g.case.v.native = False
        g.resolve()  # The existing ungraded adapter remains usable.
        g.local()
        with self.assertRaisesRegex(SpineError, 'grade: adapter requires prepare'):
            g.resolve()

    def test_unprepared_adapter_refuses_finish_grade(self):
        g = self.grade
        g.case.v.prepare = None
        g.case.v.native = False
        g.finish()
        with self.assertRaisesRegex(SpineError, 'grade: adapter requires prepare'):
            g.resolve()

    def test_plain_tall_graded_draft_refuses(self):
        g = self.grade
        g.case.settings['profiles']['draft']['shape'] = 'tall'
        g.local()
        with self.assertRaisesRegex(SpineError, 'tall crop source required'):
            g.resolve()

    def test_tall_refuses_cards_and_masters(self):
        g = self.grade
        g.local()
        req = g.resolve()
        req['settings']['shape'] = 'tall'
        for change in ('master', 'card'):
            with self.subTest(change=change):
                wrong = copy.deepcopy(req)
                wrong['plan_data']['spans'][0]['shot'] = 'med'
                if change == 'card':
                    wrong['cards_data']['cards'] = [{'kind': 'cover'}]
                else:
                    wrong['stage'] = 'masters'
                with self.assertRaisesRegex(SpineError, 'native tall cards|native masters unsupported'):
                    compositor.prepare(g.case.v, wrong)

    def test_tall_refuses_caption_burn(self):
        fixture = test_raster_binding.RasterBinding(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        c = fixture.case
        result = c.accepted()
        fixture.supply()
        req = copy.deepcopy(result['request'])
        generated = {p: filehash(c.folder / p) for p in compositor.names(req)}
        compositor.bind(c.v, req, generated)  # Valid provider assets bind in wide.
        req['settings']['shape'] = 'tall'
        with self.assertRaisesRegex(SpineError, 'compositor: native tall caption burns unsupported'):
            compositor.bind(c.v, req, generated)


class TallGrades(unittest.TestCase):
    """e2e: Draft/draft.mp4 proves native tall crop coordinates, grades and every-frame subjects."""

    @classmethod
    def setUpClass(cls):
        test_grade.Grades.setUpClass()

    def setUp(self):
        self.grade = test_grade.Grades(methodName='runTest')
        self.grade.setUp()
        self.addCleanup(self.grade.doCleanups)
        c = self.grade.case
        c.settings['profiles']['draft']['shape'] = 'tall'
        c.settings['shared']['video']['sizes']['tall'] = [90, 160]
        c.settings['shared']['video']['shot_fractions'] = {'med': .8, 'close': .5}
        c.cut['spans'][0]['shot'] = 'med'
        c.save_cut()

    def prove_subjects(self, result):
        c, req = self.grade.case, result['request']
        readings = c.get(result['readings'])
        measured = readings['checks']['frame-map']['value']
        subjects = coverage.subjects(req, readings['checks']['frame-map']['ok'])
        self.assertTrue(subjects['ok'], subjects)
        self.assertEqual(subjects['coverage']['frames'], measured['frames'])
        self.assertEqual(measured['failed_frames'], [])
        self.assertTrue(all(r['ok'] for r in readings['mapped_frames']))
        for row in req['picture_map']:
            x, y, w, h = row['crop']
            for source in row['source_indices']:
                for a, b, width, height in req['faces_data']['frames'][source]['boxes']:
                    self.assertTrue(x <= a and y <= b and a + width <= x + w and b + height <= y + h)
        print('TALL_OUTPUT ' + json.dumps({'sha256': result['file_sha256'],
            'crops': [r['crop'] for r in req['picture_map']],
            'pixels': measured, 'subjects': subjects['value']}), flush=True)

    def test_tall_med_close_distinct_rgb_and_luma_grades(self):
        g, c = self.grade, self.grade.case
        c.two_spans('jump')
        c.put('records/faces.json', {'frames': [{'frame': i, 'boxes': [
            [20, 35, 20, 20] if i < 15 else [120, 35, 20, 20]]} for i in range(30)]})
        g.finish()
        for space in ('rgb', 'luma'):
            with self.subTest(space=space):
                for span in c.cut['spans']:
                    g.local(span, space=space)
                result = c.accepted()
                self.assertEqual([r['crop'] for r in result['picture_map']],
                                 [[10, 10, 40, 71], [118, 23, 25, 44]])
                g.prove(result, finish=True, local=True, source_points={'s1': (30, 45), 's2': (130, 45)})
                self.prove_subjects(result)
        wrong = copy.deepcopy(result['request'])
        wrong['picture_map'][0]['crop'][0] += 1  # Still inside the same flat band and contains the subject.
        with self.assertRaisesRegex(SpineError, 'tall crop.*framing'):
            pixels.compare(c.v, wrong, c.folder / result['file'])

    def test_tall_push_jcut_hold_grade_and_subjects(self):
        g, c = self.grade, self.grade.case
        c.two_spans()
        c.cut['spans'][0].update(enter='push', push={'to': 'close', 's': .2})
        for span in c.cut['spans']:
            g.local(span)
        result = c.accepted()
        rows = result['picture_map']
        self.assertEqual(rows[0]['crop'], [60, 10, 40, 71])
        held = [r for r in rows if r['span'] == 's1'][-1]
        self.assertEqual((held['crop'], held['source_frames'][1]), ([68, 23, 25, 44], 15))
        g.prove(result, local=True)
        self.prove_subjects(result)
        wrong = copy.deepcopy(result['request'])
        wrong['picture_map'][1]['crop'][0] += 1
        with self.assertRaisesRegex(SpineError, 'push.*crop'):
            mapping.validate(wrong)

    def test_tall_fit_missing_crop_and_unsupported_refusals(self):
        g, c = self.grade, self.grade.case
        req = g.resolve()
        span = req['plan_data']['spans'][0]
        req['settings']['video']['shot_fractions']['med'] = 1
        self.assertEqual(framing.crop(req, span, [0, 30], [1920, 1080])[2:], [607, 1079])
        cases = [('wide', 'tall crop source required'), ('face', 'landmark'), ('mouth', 'landmark')]
        for shot, reason in cases:
            with self.subTest(shot=shot):
                c.cut['spans'][0]['shot'] = shot
                c.save_cut()
                with self.assertRaisesRegex(SpineError, reason):
                    g.resolve()
        c.cut['spans'][0]['shot'] = 'close'
        c.save_cut()
        for boxes, reason in (([], 'empty subject'), ([[0, 0, 100, 30]], 'subject.*fit')):
            c.put('records/faces.json', {'frames': [{'frame': i, 'boxes': boxes} for i in range(30)]})
            with self.assertRaisesRegex(SpineError, reason):
                g.resolve()
        c.put('records/faces.json', {'frames': []})
        with self.assertRaisesRegex(SpineError, 'missing subject'):
            g.resolve()
        # The clock owner must still name the unsupported path before any tall encoding.
        wrong = copy.deepcopy(req)
        wrong['plan_data']['spans'][0]['patch'] = {'record': 'patch.json'}
        with self.assertRaisesRegex(SpineError, 'partial patch'):
            from . import clocks
            clocks.split(wrong, [(wrong['plan_data']['spans'][0], [0, .5])])
        c.two_spans()
        wrong = copy.deepcopy(req)
        wrong['plan_data']['spans'] = c.cut['spans']
        with self.assertRaisesRegex(SpineError, 'J-cut:.*partial selection'):
            clocks.split(wrong, [(c.cut['spans'][1], c.cut['spans'][1]['src'])])

    def test_tall_nonfinite_and_stale_grade_refuse(self):
        self.grade.resolve()
        self.grade.test_local_changes_require_new_approval_and_finite_parameters()

    def test_tall_symlink_grade_refuses(self):
        self.grade.resolve()
        self.grade.test_symlink_lut_refuses()

    def test_tall_interrupted_grade_restores_accepted_output(self):
        self.grade.resolve()
        self.grade.test_interrupted_grade_publish_restores_previous_output_before_retry()
