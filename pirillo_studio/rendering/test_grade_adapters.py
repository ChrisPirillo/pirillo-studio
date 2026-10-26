"""Grade support cannot bypass adapter or native composition boundaries."""
import copy
import unittest

from ..spine.files import SpineError, filehash
from . import compositor, test_grade, test_raster_binding


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
        with self.assertRaisesRegex(SpineError, 'compositor: native masters/tall unsupported'):
            g.resolve()

    def test_tall_refuses_crops_pushes_and_cards(self):
        g = self.grade
        g.local()
        req = g.resolve()
        req['settings']['shape'] = 'tall'
        for change in ('crop', 'push', 'card'):
            with self.subTest(change=change):
                wrong = copy.deepcopy(req)
                wrong['settings']['video']['shot_fractions'] = {'med': .25, 'close': .2}
                if change == 'card':
                    wrong['cards_data']['cards'] = [{'kind': 'cover'}]
                else:
                    wrong['plan_data']['spans'][0].update(
                        {'shot': 'med'} if change == 'crop' else
                        {'shot': 'med', 'enter': 'push', 'push': {'to': 'close', 's': .2}})
                with self.assertRaisesRegex(SpineError, 'compositor: native masters/tall unsupported'):
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
