"""Shot geometry must survive real encoding and decoded correspondence checks."""
import copy
import json
import unittest

from ..spine.files import SpineError
from . import coverage, mapping, pixels, request, test_compositor


class Framing(unittest.TestCase):
    """e2e: Draft/draft.mp4 realizes shot intent or refuses the named span."""

    @classmethod
    def setUpClass(cls):
        test_compositor.Compositor.setUpClass()

    def setUp(self):
        self.case = test_compositor.Compositor('test_jcut_decoded_picture_voice_captions_and_reuse')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.video = self.case.settings['shared']['video']
        self.video['shot_fractions'] = {'med': .8, 'close': .5}
        self.observations = [{'frame': i, 'boxes': [[70, 35, 20, 20]]} for i in range(30)]
        self.save_faces()

    def save_faces(self):
        self.case.put('records/faces.json', {'frames': self.observations})

    def resolve(self):
        return request.resolve(self.case.v, 'draft', 'draft')

    def refused(self, reason):
        with self.assertRaisesRegex(SpineError, 'framing: s1: .*' + reason):
            self.resolve()
        self.assertFalse((self.case.folder / 'Draft/draft.mp4').exists())

    def test_med_close_wide_decoded_crops(self):
        self.case.put('records/words.json', {'words': [
            {'id': 'w1', 'text': 'First.', 'start': .05, 'end': .15},
            {'id': 'w2', 'text': 'Next.', 'start': .35, 'end': .45},
            {'id': 'w3', 'text': 'Last.', 'start': .65, 'end': .8}]})
        self.case.cut['spans'] = [
            {'id': 's1', 'order': 1, 'src': [0, .3], 'words': ['w1', 'w1'], 'enter': 'open', 'shot': 'med'},
            {'id': 's2', 'order': 2, 'src': [.3, .6], 'words': ['w2', 'w2'], 'enter': 'jump', 'shot': 'close'},
            {'id': 's3', 'order': 3, 'src': [.6, 1], 'words': ['w3', 'w3'], 'enter': 'jump', 'shot': 'wide'}]
        self.case.truth = {'0:w1': [.05, .15], '1:w2': [.35, .45], '2:w3': [.65, .8]}
        for row in self.observations:
            row['boxes'] = [[2, 3, 24, 18]] if row['frame'] < 9 else [[125, 65, 24, 18]]
        self.save_faces()
        self.case.save_cut()
        result = self.case.accepted()
        req = result['request']
        crops = [r['crop'] for r in req['picture_map']]
        self.assertEqual(crops, [[0, 0, 128, 72], [80, 45, 80, 45], [0, 0, 160, 90]])
        mapping.validate(req)
        subject = coverage.subjects(req, True)
        self.assertTrue(subject['ok'], subject)
        readings = self.case.get(result['readings'])
        measured = readings['checks']['frame-map']
        self.assertEqual(measured['value']['frames'], 30)
        self.assertTrue(all(r['ok'] for r in readings['mapped_frames']))
        wrong = copy.deepcopy(req)
        for row in wrong['picture_map']:
            row['crop'] = [0, 0, 160, 90]
        old = pixels.compare(self.case.v, wrong, self.case.folder / result['file'])
        self.assertFalse(old['checks']['frame-map']['ok'], 'full-frame output must not pass cropped correspondence')
        print('SHOT_OUTPUT ' + json.dumps({'file_sha256': result['file_sha256'], 'crops': crops,
              'pixels': measured['value'], 'subjects': subject['value'],
              'full_frame_failed': old['checks']['frame-map']['value']['failed_frames']}), flush=True)

    def test_jcut_observations_include_outgoing_picture_hold(self):
        self.case.two_spans()
        self.observations[14]['boxes'] = [[110, 45, 20, 20]]
        self.save_faces()
        result = self.case.accepted()
        self.assertEqual(result['picture_map'][0]['crop'], [32, 14, 128, 72])
        subject = coverage.subjects(result['request'], True)
        self.assertTrue(subject['ok'], subject)
        del self.observations[14]
        self.save_faces()
        with self.assertRaisesRegex(SpineError, 'framing: s1: .*14'):
            self.resolve()

    def test_missing_observation_refuses(self):
        del self.observations[15]
        self.save_faces()
        self.refused('missing.*15')

    def test_empty_observation_refuses(self):
        self.observations[15]['boxes'] = []
        self.save_faces()
        self.refused('empty.*15')

    def test_subject_union_not_fitting_refuses(self):
        self.observations[0]['boxes'] = [[0, 35, 20, 20]]
        self.observations[-1]['boxes'] = [[140, 35, 20, 20]]
        self.save_faces()
        self.refused('subject.*fit')

    def test_missing_size_refuses(self):
        del self.video['shot_fractions']['med']
        self.refused('missing.*med')

    def test_nonfinite_size_refuses(self):
        for value in (float('nan'), float('inf'), -float('inf')):
            with self.subTest(value=value):
                self.video['shot_fractions']['med'] = value
                self.refused('size')

    def test_nonpositive_size_refuses(self):
        for value in (0, -.1):
            with self.subTest(value=value):
                self.video['shot_fractions']['med'] = value
                self.refused('size')

    def test_size_larger_than_source_refuses(self):
        self.video['shot_fractions']['med'] = 1.1
        self.refused('size')

    def test_output_aspect_exceeding_source_refuses(self):
        self.video['sizes']['wide'] = [90, 160]
        self.refused('size')

    def test_face_refuses_without_landmark_geometry(self):
        self.case.cut['spans'][0]['shot'] = 'face'
        self.case.save_cut()
        self.refused('face.*unsupported')

    def test_mouth_refuses_without_landmark_geometry(self):
        self.case.cut['spans'][0]['shot'] = 'mouth'
        self.case.save_cut()
        self.refused('mouth.*unsupported')

    def test_wide_without_observations_preserves_whole_frame(self):
        self.case.cut['spans'][0]['shot'] = 'wide'
        self.case.save_cut()
        self.observations = []
        self.save_faces()
        result = self.case.accepted()
        self.assertEqual(result['picture_map'][0]['crop'], [0, 0, 160, 90])
