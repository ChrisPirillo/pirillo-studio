"""Shared short-opening contract at the cutting boundaries."""
import copy
import unittest

from pirillo_studio.cutting import checks, plan, settle
from pirillo_studio.spine.files import SpineError


class Context(unittest.TestCase):
    """defect: part16 review-r1 P3 cutting accepted contradictory opening records."""

    def records(self, opening):
        words = [{'id': 'w1', 'text': 'Subject', 'start': 1, 'end': 30}]
        cut = {'key': 'cut', 'named': [], 'measurements': {'duration_s': 31, 'fps': 30},
               'spans': [{'id': 's1', 'words': ['w1', 'w1'], 'src': [0, 31]}]}
        maker = {'plan': 'cut', 'shorts': [
            {'id': 'full', 'full': True, 'point': 'Whole story'},
            {'id': 'one', 'point': 'Subject', 'spans': [['w1', 'w1']],
             'opens_on': 'w1', 'ends_on': 'w1', 'stands_alone': opening}]}
        spec = copy.deepcopy(maker)
        spec['shorts'][1]['pieces'] = settle.short_pieces([['w1', 'w1']], words, cut['measurements'])
        return maker, spec, cut, words

    def test_contradictions_refused_at_both_cutting_boundaries(self):
        for opening in ({'context': 'Subject', 'opens_on_subject': False},
                        {'opens_on_subject': True, 'context': ''}):
            maker, spec, cut, words = self.records(opening)
            with self.subTest(opening=opening, boundary='settle'):
                with self.assertRaisesRegex(SpineError, 'context: contradictory'):
                    plan.validate_shorts(maker, words, cut)
            with self.subTest(opening=opening, boundary='checks'):
                flags = checks.shorts(maker, spec, cut, words)
                self.assertTrue(any('context: contradictory' in flag['measure']['reason'] for flag in flags), flags)

        for opening in ({'context': 'Subject'},
                        {'context': 'Subject', 'raster': {'file': 'context.png', 'sha256': 'authored'}},
                        {'opens_on_subject': True}):
            with self.subTest(opening=opening):
                maker, spec, cut, words = self.records(opening)
                self.assertEqual(plan.validate_shorts(maker, words, cut), maker)
                self.assertEqual(checks.shorts(maker, spec, cut, words), [])
