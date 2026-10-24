"""Rendering seam 2a lifecycle contracts inspect real media and committed records.

Contracts 1, 2, 4-7, 10 and 12 belong to seam 2b's decoded semantic measurements;
contract 13 belongs to the host switch (seam 3). Decode here exercises the adapter
boundary, not picture, sound, caption or profile acceptance for seam 2b.
"""
import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from pirillo_studio.cutting import plan
from pirillo_studio.rendering import records, request
from pirillo_studio.rendering.render import Video, render
from pirillo_studio.spine.files import SpineError, canon, digest, filehash, load


class PowerLoss(BaseException):
    pass


class Fixture(unittest.TestCase):
    """A video folder with real media and a test adapter; its subclasses hold the tests."""

    @classmethod
    def setUpClass(cls):
        cls.ffmpeg, cls.ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        if not cls.ffmpeg or not cls.ffprobe:
            raise RuntimeError('rendering contracts require local ffmpeg and ffprobe')
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'source.mp4'
            subprocess.run([cls.ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc=size=160x90:rate=30', '-f', 'lavfi', '-i',
                            'sine=frequency=440:sample_rate=48000', '-t', '1',
                            '-c:v', 'mpeg4', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                            '-y', str(path)], check=True, capture_output=True)
            cls.media = path.read_bytes()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / 'video'
        self.folder.mkdir()
        (self.folder / 'source.mp4').write_bytes(self.media)
        (self.folder / 'owner.txt').write_bytes(b'owner original')
        self.encodes = 0
        self.states = []
        self.settings = load(Path(request.__file__).parent, 'settings.json')
        self.v = Video(self.folder, 'arc', settings=self.settings,
                       versions={'encoder': 'fixture-mpeg4-1', 'measure': 'fixture-probe-1'},
                       preflight={'ok': True, 'state': 'ready',
                                  'source_sha256': filehash(self.folder / 'source.mp4')},
                       encode=self.encode, measure=self.measure)
        self.put('records/job.json', {'recipe': 'plain', 'voice_over': []})
        self.put('records/words.json', {'words': [
            {'id': 'w1', 'text': 'Ready.', 'start': .1, 'end': .5}]})
        self.cut = {'video': 'records/job.json', 'plan_version': '1',
                    'spans': [{'id': 's1', 'order': 1, 'src': [0, 1],
                               'words': ['w1', 'w1'], 'enter': 'open', 'shot': 'med'}],
                    'cold_open': {'span': 's1', 'words': ['w1', 'w1']},
                    'end_hold_s': 0, 'removed': [], 'named': [],
                    'key': digest(b'fixture settled plan')}
        self.put('records/cut_plan2.json', self.cut)
        for name, value in [('overlay_plan', {'cards': []}),
                            ('bed', {'request': 'quiet', 'tempo_from': 'words_per_s'}),
                            ('callouts_plan', {'callouts': []})]:
            self.put(f'records/{name}.json', {'plan': self.cut['key'], **value})
        self.put('records/faces.json', {'frames': []})
        self.put('records/shorts_plan.json', {'plan': self.cut['key'], 'shorts': []})
        self.put('records/shorts_spec.json', {'plan': self.cut['key'], 'key': digest(b'shorts'), 'shorts': [
            {'id': 'full', 'full': True, 'point': 'complete story'},
            {'id': 'one', 'spans': [['w1', 'w1']], 'opens_on': 'w1',
             'ends_on': 'w1', 'stands_alone': {'opens_on_subject': True},
             'pieces': [[0, 1]], 'point': 'ready'}]})
        self.bind_cut()

    def put(self, name, value):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canon(value))

    def get(self, name):
        return load(self.folder, name)

    def bind_cut(self):
        names = ['cut_plan2', 'words', 'job', 'overlay_plan', 'bed', 'callouts_plan',
                 'shorts_plan', 'shorts_spec']
        reads = {f'records/{name}.json': filehash(self.folder / f'records/{name}.json') for name in names}
        reads['source.mp4'] = filehash(self.folder / 'source.mp4')
        key = digest(canon({'reads': reads, 'settings': plan.settings()}))
        self.put('records/checks/cut.json', {'ok': True, 'state': 'pass',
                                            'plan': self.cut['key'], 'reads': reads, 'key': key})
        blocks = [{'spans': ['s1'], 'why': 'complete story'}]
        self.put('records/arc.json', {'plan': self.cut['key'], 'check': key, 'key': digest(b'arc')})
        self.put('records/arc_decision.json', {'decision': 'approve', 'plan': self.cut['key'],
                                              'check': key, 'arc': digest(b'arc'), 'blocks': blocks})
        self.put('records/story_map.json', {
            'plan': self.cut['key'], 'check': key, 'blocks': blocks, 'tail': 's1',
            'arc_decision': 'records/arc_decision.json#' + filehash(self.folder / 'records/arc_decision.json'),
            'reads': {name: value for name, value in reads.items()
                      if any(part in name for part in ('overlay', 'bed', 'callouts', 'shorts'))}})

    def encode(self, req, path):
        self.encodes += 1
        self.states.append(self.get(f'records/render/{req["output"].replace(":", "-")}.json')['state'])
        path.write_bytes(self.media + f'fixture encode {self.encodes}'.encode())

    def measure(self, req, path):
        self.states.append(self.saved(req['output'])['state'])
        probe = json.loads(subprocess.run([
            self.ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
            check=True, capture_output=True, text=True).stdout)
        decoded = subprocess.run([self.ffmpeg, '-v', 'info', '-i', str(path),
                                  '-filter_complex', 'ebur128=peak=true', '-f', 'null', '-'],
                                 check=True, capture_output=True, text=True)
        video = next(stream for stream in probe['streams'] if stream['codec_type'] == 'video')
        loudness = float(re.findall(r'I:\s+(-?[\d.]+) LUFS', decoded.stderr)[-1])
        peak = float(re.findall(r'Peak:\s+(-?[\d.]+) dBFS', decoded.stderr)[-1])
        return {'file_sha256': filehash(path), 'key': req['key'],
                'checks': {'decode': {'ok': True, 'value': int(video['nb_frames']),
                                      'setting': 'rendering.settings#checks.decode',
                                      'evidence': 'ffprobe frame count and complete ffmpeg decode'}},
                'duration_s': float(probe['format']['duration']), 'fps': video['r_frame_rate'],
                'loudness_lufs': loudness, 'true_peak_dbtp': peak, 'frame_checks': ['decode']}

    def saved(self, output):
        return self.get(f'records/render/{output.replace(":", "-")}.json')

    def assert_media(self, record):
        self.assertEqual(record['file_sha256'], filehash(self.folder / record['file']))
        readings = self.get(record['readings'])
        self.assertEqual(readings['file_sha256'], record['file_sha256'])
        self.assertEqual(readings['key'], record['key'])
        self.assertEqual(readings['fps'], '30/1')
        self.assertGreater(readings['duration_s'], 0)
        self.assertEqual(readings['checks']['decode']['value'], 30)

    def approve(self, **changes):
        self.put('records/draft_decision.json', {
            'decision': 'approve', 'file': 'Draft/draft.mp4',
            'file_sha256': filehash(self.folder / 'Draft/draft.mp4'),
            'plan': self.cut['key'], **changes})


class Rendering(Fixture):
    """e2e: records/render/arc.json and the exact media it identifies."""

    def test_waits_for_exact_owner_choice(self):
        self.v.stage = 'draft'
        draft = render(self.v, 'draft')
        self.assertEqual(draft, self.saved('draft'))
        self.assertEqual(draft['state'], 'waiting_owner')
        self.assert_media(draft)
        self.v.stage = 'masters'
        before = self.encodes
        invalid = [None, {'decision': 'reject'}, {'decision': 'redo'}, {'decision': 'not this'},
                   {'decision': 'try again'}, {'decision': True}, {'plan': digest(b'old plan')},
                   {'file_sha256': digest(b'wrong file')}, {'file': 'Draft/arc.mp4'}]
        for changes in invalid:
            with self.subTest(changes=changes):
                if changes is not None:
                    self.approve(**changes)
                record = render(self.v, 'long')
                self.assertEqual(record, self.saved('long'))
                self.assertEqual(record['state'], 'waiting_owner')
                self.assertFalse(record['ok'])
                self.assertEqual(record['viewed'], {'file': draft['file'],
                                 'file_sha256': draft['file_sha256'], 'plan': self.cut['key']})
                self.assertEqual(self.encodes, before)
                self.assertFalse((self.folder / 'YouTube/main.mp4').exists())
        self.approve()
        delivered = render(self.v, 'long')
        self.assertEqual(delivered['state'], 'delivered')
        self.assertTrue(delivered['ok'])
        self.assert_media(delivered)
        self.approve(decision='reject')
        self.assertEqual(render(self.v, 'long')['state'], 'waiting_owner')
        self.assertEqual(self.encodes, before + 1)

    def test_unchanged_inputs_do_not_render_again(self):
        first = render(self.v, 'arc')
        before = (self.folder / first['file']).read_bytes()
        self.assertEqual(render(self.v, 'arc'), first)
        self.assertEqual(self.encodes, 1)
        self.assertIn('rendering', self.states)
        self.assertIn('measuring', self.states)
        self.assertEqual((self.folder / first['file']).read_bytes(), before)
        self.put('records/job.json', {'recipe': 'plain', 'voice_over': [], 'title': 'New title'})
        self.bind_cut()
        self.assertEqual(request.resolve(self.v, 'arc', 'arc')['key'], first['key'])
        relocated = Path(self.tmp.name) / 'relocated'
        shutil.copytree(self.folder, relocated)
        self.folder = self.v.folder = relocated
        self.assertEqual(render(self.v, 'arc')['key'], first['key'])
        self.assertEqual(self.encodes, 1)
        self.v.versions['encoder'] = 'fixture-mpeg4-2'
        changed = render(self.v, 'arc')
        self.assertNotEqual(changed['key'], first['key'])
        self.assertEqual(self.encodes, 2)
        self.assert_media(changed)

    def test_failed_render_preserves_owner_files(self):
        accepted = render(self.v, 'arc')
        prior = (self.folder / accepted['file']).read_bytes()
        self.v.versions['encoder'] = 'fixture-broken'
        def broken(req, path):
            path.write_bytes(b'partial')
            raise OSError('encoder: injected piece failure')
        self.v.encode = broken
        failed = render(self.v, 'arc')
        self.assertEqual(failed, self.saved('arc'))
        self.assertEqual(failed['state'], 'render_failed')
        self.assertEqual(failed['step'], 'encode')
        self.assertIn('piece failure', failed['error'])
        self.assertEqual((self.folder / accepted['file']).read_bytes(), prior)
        self.assertEqual((self.folder / 'source.mp4').read_bytes(), self.media)
        self.assertEqual((self.folder / 'owner.txt').read_bytes(), b'owner original')
        self.v.encode = self.encode
        recovered = render(self.v, 'arc')
        self.assertEqual(recovered['state'], 'waiting_owner')
        self.assert_media(recovered)
        self.assertEqual(render(self.v, 'arc'), recovered)

    def test_short_import_preserves_finished_media(self):
        fresh = Path(self.tmp.name) / 'import'
        fresh.mkdir()
        (fresh / 'source.mp4').write_bytes(self.media)
        self.folder = self.v.folder = fresh
        self.v.stage = 'short'
        self.put('records/job.json', {'recipe': 'shorts-only', 'source': 'source.mp4', 'short_id': 'one'})
        result = render(self.v, 'short:one')
        self.assertEqual(result, self.saved('short:one'))
        self.assertEqual(result['state'], 'delivered')
        self.assertTrue(result['ok'])
        self.assertEqual(self.encodes, 0)
        self.assertEqual((fresh / result['file']).read_bytes(), self.media)
        self.assertEqual((fresh / 'source.mp4').read_bytes(), self.media)
        self.assertFalse((fresh / 'records/cut_plan2.json').exists())
        self.assertFalse((fresh / 'Draft').exists())
        self.assert_media(result)
        self.assertEqual(render(self.v, 'short:one'), result)

    def test_measure_refused_records_reading_and_setting(self):
        measure = self.v.measure
        def refused(req, path):
            readings = measure(req, path)
            readings['checks']['decode'].update(ok=False, value='partial decode')
            return readings
        self.v.measure = refused
        result = render(self.v, 'arc')
        self.assertEqual(result, self.saved('arc'))
        self.assertEqual(result['state'], 'measure_refused')
        self.assertEqual(result['reading'], 'decode')
        self.assertIn('decode', result['setting'])
        self.assertFalse((self.folder / 'Draft/arc.mp4').exists())
        self.assertEqual((self.folder / 'source.mp4').read_bytes(), self.media)

    def test_incomplete_or_nonfinite_readings_cannot_publish(self):
        measure = self.v.measure
        for mutation in (lambda r: r.pop('key'), lambda r: r.update(key=digest(b'stale')),
                         lambda r: r.pop('file_sha256'),
                         lambda r: r.update(file_sha256=digest(b'stale bytes')),
                         lambda r: r.update(duration_s=float('nan')),
                         lambda r: r.update(checks={})):
            with self.subTest(mutation=mutation):
                def bad(req, path):
                    value = measure(req, path)
                    mutation(value)
                    return value
                self.v.measure = bad
                result = render(self.v, 'arc')
                self.assertEqual(result, self.saved('arc'))
                self.assertEqual(result['state'], 'measure_refused')
                self.assertTrue(result['reading'])
                self.assertTrue(result['setting'])
                self.assertFalse((self.folder / 'Draft/arc.mp4').exists())

    def test_inputs_changed_during_measurement_do_not_publish(self):
        measure = self.v.measure
        def changed(req, path):
            result = measure(req, path)
            (self.folder / 'source.mp4').write_bytes(self.media + b'changed')
            return result
        self.v.measure = changed
        result = render(self.v, 'arc')
        self.assertEqual(result['state'], 'render_failed')
        self.assertEqual(result, self.saved('arc'))
        self.assertFalse((self.folder / 'Draft/arc.mp4').exists())

    def test_an_input_is_never_a_file_the_output_writes(self):
        """guard: a supplied short named as this output's readings, record or work file is refused before any write."""
        self.v.stage = 'short'
        for source in ('records/render/short-one-readings.json', 'records/render/short-one.json',
                       'Shorts/.render-short-one-x.mp4'):
            with self.subTest(source=source):
                fresh = Path(self.tmp.name) / f'import-{len(source)}'
                (fresh / source).parent.mkdir(parents=True)
                (fresh / source).write_bytes(self.media)
                self.folder = self.v.folder = fresh
                self.put('records/job.json', {'recipe': 'shorts-only', 'source': source, 'short_id': 'one'})
                with self.assertRaisesRegex(SpineError, 'an input is a file this output writes'):
                    render(self.v, 'short:one')
                self.assertEqual((fresh / source).read_bytes(), self.media)

    def test_owner_aliases_cannot_be_overwritten(self):
        for kind in ('symlink', 'hardlink'):
            with self.subTest(kind=kind):
                target = self.folder / 'Draft/arc.mp4'
                target.parent.mkdir(exist_ok=True)
                owner = self.folder / 'source.mp4'
                if kind == 'symlink':
                    target.symlink_to(owner)
                else:
                    os.link(owner, target)
                try:
                    with self.assertRaises(SpineError):
                        render(self.v, 'arc')
                    self.assertEqual(owner.read_bytes(), self.media)
                    self.assertEqual(self.encodes, 0)
                finally:
                    target.unlink()
        with self.assertRaises(SpineError):
            render(self.v, 'short:../source')

    def test_restart_after_publish_without_commit_restores_accepted(self):
        for has_previous in (False, True):
            with self.subTest(has_previous=has_previous):
                prior = render(self.v, 'arc') if has_previous else None
                accepted = (self.folder / prior['file']).read_bytes() if prior else None
                self.v.versions['encoder'] = f'fixture-crash-{has_previous}'
                write = records.write
                def lose_power(video, name, value):
                    if name == 'records/render/arc.json' and value.get('state') == 'waiting_owner':
                        raise PowerLoss('after rename, before commit')
                    return write(video, name, value)
                with patch.object(records, 'write', side_effect=lose_power):
                    with self.assertRaises(PowerLoss):
                        render(self.v, 'arc')
                self.assertNotEqual(self.saved('arc')['state'], 'waiting_owner')
                if prior:
                    self.assertIn(accepted, [p.read_bytes() for p in (self.folder / 'Draft').iterdir()
                                             if p.is_file()])
                result = render(self.v, 'arc')
                self.assertEqual(result['state'], 'waiting_owner')
                self.assert_media(result)
                count = self.encodes
                self.assertEqual(render(self.v, 'arc'), result)
                self.assertEqual(self.encodes, count)

    def test_recovery_writes_only_this_outputs_readings(self):
        """guard: a crashed record whose accepted readings name another file never has recovery write over it."""
        render(self.v, 'arc')
        self.v.versions['encoder'] = 'fixture-crash-aliased'
        write = records.write
        def lose_power(video, name, value):
            if name == 'records/render/arc.json' and value.get('state') == 'waiting_owner':
                raise PowerLoss('after rename, before commit')
            return write(video, name, value)
        with patch.object(records, 'write', side_effect=lose_power), self.assertRaises(PowerLoss):
            render(self.v, 'arc')
        crashed = self.saved('arc')
        crashed['previous']['record']['readings'] = 'records/words.json'
        self.put('records/render/arc.json', crashed)
        words = (self.folder / 'records/words.json').read_bytes()
        self.assertEqual(render(self.v, 'arc')['state'], 'render_failed')
        self.assertEqual((self.folder / 'records/words.json').read_bytes(), words)

    def test_a_short_id_never_names_another_outputs_readings(self):
        """guard: short `one-readings` would share records/render/short-one-readings.json with short `one`."""
        with self.assertRaisesRegex(SpineError, 'unsafe short identity'):
            render(self.v, 'short:one-readings')

    def test_same_output_attempts_publish_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: render(self.v, 'arc'), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], self.saved('arc'))
        self.assertEqual(self.encodes, 1)
        self.assert_media(results[0])

    def test_transitive_assets_and_selected_inputs_invalidate_only_dependents(self):
        asset = self.folder / 'card.bin'
        asset.write_bytes(b'card pixels one')
        overlay = self.get('records/overlay_plan.json')
        overlay['cards'] = [{'asset': 'card.bin', 'covers': ['s1']}]
        self.put('records/overlay_plan.json', overlay)
        self.bind_cut()
        first = render(self.v, 'arc')
        asset.write_bytes(b'card pixels two')
        changed = render(self.v, 'arc')
        self.assertNotEqual(changed['key'], first['key'])
        self.assertEqual(self.encodes, 2)
        renamed = self.folder / 'relocated-card.bin'
        asset.rename(renamed)
        overlay['cards'][0]['asset'] = renamed.name
        self.put('records/overlay_plan.json', overlay)
        self.bind_cut()
        self.assertEqual(request.resolve(self.v, 'arc', 'arc')['key'], changed['key'])
        shorts = self.get('records/shorts_spec.json')
        shorts['shorts'][1]['point'] = 'unrelated clip edit'
        self.put('records/shorts_spec.json', shorts)
        self.bind_cut()
        self.assertEqual(render(self.v, 'arc')['key'], changed['key'])
        self.assertEqual(self.encodes, 2)
        self.assert_media(self.saved('arc'))

    def test_missing_identity_nonfinite_plan_and_unready_preflight_refuse(self):
        original = copy.deepcopy(self.cut)
        for change in ({'key': None}, {'key': digest(b'stale')}, {'end_hold_s': float('nan')}):
            with self.subTest(change=change):
                value = {**original, **change}
                path = self.folder / 'records/cut_plan2.json'
                path.write_text(json.dumps(value))
                with self.assertRaises(SpineError):
                    render(self.v, 'arc')
                self.assertEqual(self.encodes, 0)
                self.assertFalse((self.folder / 'Draft/arc.mp4').exists())
        self.put('records/cut_plan2.json', original)
        overlay = self.get('records/overlay_plan.json')
        overlay['cards'] = [{'file': 'source.mp4', 'asset': 'source.mp4', 'sha256': digest(b'wrong')}]
        self.put('records/overlay_plan.json', overlay)
        self.bind_cut()
        with self.assertRaisesRegex(SpineError, 'ambiguous|hash|sha256'):
            request.resolve(self.v, 'arc', 'arc')
        self.v.preflight['ok'] = False
        with self.assertRaisesRegex(SpineError, 'preflight'):
            render(self.v, 'arc')
        self.assertEqual((self.folder / 'source.mp4').read_bytes(), self.media)

    def test_independent_outputs_do_not_share_the_publication_lock(self):
        barrier = threading.Barrier(2)
        encode = self.encode
        def concurrent(req, path):
            barrier.wait(timeout=5)
            encode(req, path)
        self.v.encode = concurrent
        second = Video(self.folder, 'draft', settings=self.settings,
                       versions=self.v.versions, preflight=self.v.preflight,
                       encode=concurrent, measure=self.measure)
        with ThreadPoolExecutor(max_workers=2) as pool:
            arc = pool.submit(render, self.v, 'arc')
            draft = pool.submit(render, second, 'draft')
            results = [arc.result(), draft.result()]
        self.assertEqual(self.encodes, 2)
        for result in results:
            self.assertEqual(result['state'], 'waiting_owner')
            self.assertEqual(result, self.saved(result['output']))
            self.assert_media(result)

    def test_publish_failure_keeps_previous_media(self):
        record = render(self.v, 'arc')
        accepted = (self.folder / record['file']).read_bytes()
        accepted_readings = (self.folder / record['readings']).read_bytes()
        self.v.versions['encoder'] = 'fixture-publish-failure'
        with patch.object(records, 'publish', side_effect=OSError('rename failure')):
            failed = render(self.v, 'arc')
        self.assertEqual(failed, self.saved('arc'))
        self.assertEqual(failed['state'], 'render_failed')
        self.assertEqual(failed['step'], 'publish')
        self.assertIn('rename failure', failed['error'])
        self.assertEqual((self.folder / record['readings']).read_bytes(), accepted_readings)
        self.assert_media(record)
        self.assertEqual((self.folder / record['file']).read_bytes(), accepted)
        self.assertEqual((self.folder / 'source.mp4').read_bytes(), self.media)
        result = render(self.v, 'arc')
        self.assertEqual(result['state'], 'waiting_owner')
        self.assert_media(result)

    def test_restart_discards_interrupted_encoder_work(self):
        accepted = render(self.v, 'arc')
        prior = (self.folder / accepted['file']).read_bytes()
        for step in ('piece', 'join'):
            with self.subTest(step=step):
                self.v.versions['encoder'] = f'fixture-killed-{step}'
                def killed(req, path):
                    path.write_bytes(b'incomplete media')
                    raise PowerLoss(step)
                self.v.encode = killed
                with self.assertRaises(PowerLoss):
                    render(self.v, 'arc')
                transient = self.saved('arc')
                self.assertEqual(transient['state'], 'rendering')
                incomplete = self.folder / transient['work']
                self.assertEqual((self.folder / accepted['file']).read_bytes(), prior)
                self.v.encode = self.encode
                result = render(self.v, 'arc')
                self.assertFalse(incomplete.exists())
                self.assertEqual(result, self.saved('arc'))
                self.assert_media(result)
                prior = (self.folder / result['file']).read_bytes()
        self.assertEqual((self.folder / 'source.mp4').read_bytes(), self.media)
