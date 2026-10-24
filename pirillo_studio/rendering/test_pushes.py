"""Approved pushes are proved against delivered pixels, never filter text."""
import copy
import json
import subprocess
import unittest

from ..spine.files import SpineError, filehash
from . import clocks, coverage, mapping, pixels, request, test_compositor


class Pushes(unittest.TestCase):
    """e2e: Draft/draft.mp4 carries the approved crop at every picture frame."""

    @classmethod
    def setUpClass(cls):
        test_compositor.Compositor.setUpClass()

    def setUp(self):
        self.case = test_compositor.Compositor('test_jcut_decoded_picture_voice_captions_and_reuse')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.span = self.case.cut['spans'][0]
        self.span.update(enter='push', shot='wide', push={'to': 'close', 's': .2})
        self.case.settings['shared']['video']['shot_fractions'] = {'med': .8, 'close': .5}
        self.case.save_cut()

    def resolve(self):
        return request.resolve(self.case.v, 'draft', 'draft')

    def test_push_pixels_grid_coverage_and_tampering(self):
        result = self.case.accepted()
        req = result['request']
        rows = req['picture_map']
        expanded = [row['crop'] for row in rows for _ in range(*row['frames'])]
        self.assertEqual(expanded[:7], [[0, 0, 160, 90], [7, 4, 147, 83],
            [13, 7, 133, 75], [20, 11, 120, 68], [27, 15, 107, 60],
            [33, 18, 93, 52], [40, 22, 80, 45]])
        self.assertEqual(expanded[7:], [[40, 22, 80, 45]] * 23)
        self.assertEqual(req['frame_map'][0]['frames'], [0, 30])
        mapping.validate(req)
        subject = coverage.subjects(req, True)
        self.assertTrue(subject['ok'], subject)
        measured = self.case.get(result['readings'])
        self.assertEqual([f['source_frame'] for f in measured['mapped_frames']], list(range(30)))
        self.assertTrue(all(f['ok'] for f in measured['mapped_frames']))
        wrong = copy.deepcopy(req)
        wrong['picture_map'][1]['crop'] = expanded[0]
        with self.assertRaisesRegex(SpineError, 'push.*crop'):
            mapping.validate(wrong)
        old = pixels.compare(self.case.v, wrong, self.case.folder / result['file'])
        self.assertFalse(old['checks']['frame-map']['ok'])
        wrong = copy.deepcopy(req)
        wrong['picture_map'][0]['source_size'] = [320, 180]
        with self.assertRaisesRegex(SpineError, 'source_size.*decoded'):
            pixels.compare(self.case.v, wrong, self.case.folder / result['file'])
        print('PUSH_OUTPUT ' + json.dumps({'file_sha256': result['file_sha256'],
            'crops': expanded, 'pixels': measured['checks']['frame-map']['value'],
            'subjects': subject['value'], 'wrong_crop_failed': old['checks']['frame-map']['value']['failed_frames']}), flush=True)

    def test_five_second_push_decodes_every_frame(self):
        path = self.case.folder / 'source.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'testsrc=size=160x90:rate=30', '-f', 'lavfi', '-i',
            'sine=frequency=440:sample_rate=48000', '-t', '5', '-af', 'volume=0.5',
            '-c:v', 'libx264', '-crf', '0', '-pix_fmt', 'yuv420p', '-colorspace', 'bt709',
            '-c:a', 'aac', str(path)], check=True, capture_output=True)
        self.case.v.preflight['source_sha256'] = filehash(path)
        self.span.update(src=[0, 5], push={'to': 'close', 's': 5})
        self.case.put('records/faces.json', {'frames': [
            {'frame': i, 'boxes': [[70, 35, 20, 20]]} for i in range(150)]})
        self.case.save_cut()
        result = self.case.accepted()
        measured = self.case.get(result['readings'])['checks']['frame-map']['value']
        self.assertEqual(measured['frames'], 150)
        self.assertEqual(measured['failed_frames'], [])
        self.assertTrue(coverage.subjects(result['request'], True)['ok'])
        print('LONG_PUSH_OUTPUT ' + json.dumps({'file_sha256': result['file_sha256'],
            'crop_runs': len(result['picture_map']), 'pixels': measured}), flush=True)

    def test_push_jcut_hold_and_selected_phase(self):
        self.case.two_spans()
        span = self.case.cut['spans'][0]
        span.update(enter='push', shot='med', push={'to': 'close', 's': .2})
        self.case.save_cut()
        result = self.case.accepted()
        req = result['request']
        pushed = [r for r in req['picture_map'] if r['span'] == 's1']
        self.assertEqual(pushed[-1]['source_frames'][1], 15)
        self.assertEqual(pushed[-1]['crop'], [40, 22, 80, 45])
        self.assertEqual(req['frame_map'][0]['frames'], [0, 12])
        self.assertTrue(coverage.subjects(req, True)['ok'])
        # The same approved movement retains its source-relative phase in a cold open.
        self.case.cut['cold_open'] = {'span': 's1', 'words': ['w1', 'w1']}
        self.case.truth = {'0:w1': [0, .15], '1:w1': [.2, .35], '2:w2': [.6, .75]}
        self.case.save_cut()
        selected = self.resolve()['picture_map']
        self.assertEqual(selected[0]['source_frames'], [2, 3])
        self.assertNotEqual(selected[0]['crop'], pushed[0]['crop'])

    def test_missing_unknown_or_stray_target_refuses_by_name(self):
        for push in (None, {}, {'to': 'other', 's': .2}, {'s': .2}, {'to': [], 's': .2}):
            with self.subTest(push=push):
                self.span['push'] = push
                self.case.save_cut()
                with self.assertRaisesRegex(SpineError, 'push.*(to|object)'):
                    self.resolve()
        self.span.update(enter='jump', push={'to': 'close', 's': .2})
        self.case.save_cut()
        with self.assertRaisesRegex(SpineError, 'push.*entry'):
            self.resolve()

    def test_duration_refuses_by_name(self):
        req = {'settings': self.case.settings['shared'], 'source': 'source.mp4',
               'source_sha256': self.case.v.preflight['source_sha256']}
        for duration in (None, 0, -.1, float('nan'), float('inf'), -float('inf'), True):
            with self.subTest(duration=duration):
                self.span['push']['s'] = duration
                with self.assertRaisesRegex(SpineError, r'push\.s'):
                    clocks.split(req, [(self.span, self.span['src'])])
        self.span['push'] = {'to': 'close'}
        with self.assertRaisesRegex(SpineError, r'push\.s'):
            clocks.split(req, [(self.span, self.span['src'])])

    def test_unsupported_operations_alone_and_with_push_refuse_by_name(self):
        req = {'settings': self.case.settings['shared'], 'source': 'source.mp4',
               'source_sha256': self.case.v.preflight['source_sha256']}
        for field in ('retime', 'speed', 'grade'):
            for enter in ('jump', 'push'):
                with self.subTest(field=field, enter=enter):
                    span = {**self.span, 'enter': enter, field: {}}
                    if enter != 'push':
                        del span['push']
                    with self.assertRaisesRegex(SpineError, field + ': unsupported'):
                        clocks.split(req, [(span, span['src'])])

    def test_landmarks_and_hidden_picture_refuse(self):
        for target in ('face', 'mouth'):
            with self.subTest(target=target):
                self.span['push']['to'] = target
                self.case.save_cut()
                with self.assertRaisesRegex(SpineError, 'framing: s1: ' + target + ': unsupported'):
                    self.resolve()
        self.span.update(push={'to': 'close', 's': .2}, hide_picture=True)
        self.case.save_cut()
        with self.assertRaisesRegex(SpineError, 'push.*hidden'):
            self.resolve()
