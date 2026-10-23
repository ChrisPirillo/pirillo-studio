"""QC contracts 1-13; 14 and 15 belong to seam 2b, 16 (test_switch_off_is_unchanged) to host seam 3."""
import copy
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from pirillo_studio.qc import evaluators, verdict
from pirillo_studio.spine.files import SpineError, canon, filehash


class Video:
    def __init__(self, folder):
        self.folder = folder
        self.settings = {'qc.rules': {'delivered-loudness': {'from': '2025-01-01', 'mode': 'refuse'}},
                         'rendering.settings.loudness': {'name': 'integrated_lufs', 'unit': 'LUFS', 'min': -17, 'max': -15}}
        self.locks = []

    def resolve_setting(self, name):
        return copy.deepcopy(self.settings[name])

    def now(self):
        return '2026-01-01T00:00:00Z'

    @contextmanager
    def writer(self, stage):
        self.locks.append(stage)
        yield

    def recipe(self):
        raise AssertionError('QC read the recipe')


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.v = Video(Path(temp.name) / 'video')
        self.v.folder.mkdir()
        self.put('records/job.json', {'video_started': '2026-01-01T00:00:00Z'})
        self.put('records/render/long.json', {'value': 1})
        self.put('records/final_manifest.json', {'value': 2})
        self.entries = [
            {'id': 'delivered-media', 'stage': 'masters', 'owner': 'rendering', 'checker': 'checker-renders',
             'mode': 'auto', 'result': 'measure', 'reads': [{'bind': 'render:long', 'record': 'records/render/long.json'}],
             'setting': {'name': 'rendering.settings.loudness', 'owner': 'rendering'},
             'rule': 'delivered-loudness', 'may_lower': False, 'fixer': 'finisher', 'version': 'sha256:one'},
            {'id': 'delivered', 'stage': 'masters', 'owner': 'qc', 'checker': 'checker-renders',
             'mode': 'auto', 'result': 'judgment',
             'reads': [{'bind': 'manifest', 'record': 'records/final_manifest.json'}],
             'setting': None, 'rule': None, 'may_lower': False, 'fixer': 'finisher', 'version': 'sha256:two'}]
        self.calls = []
        self.answers = {e['id']: {'result': 'pass', 'words': 'checked'} for e in self.entries}
        self.original_catalog = verdict.catalog
        patcher = patch.object(verdict, 'catalog', side_effect=lambda stage: copy.deepcopy(self.entries))
        patcher.start()
        self.addCleanup(patcher.stop)
        functions = {}
        for entry in self.entries:
            name = entry['id']
            def evaluate(*args, name=name, **kwargs):
                self.calls.append(name)
                answer = self.answers[name]
                if isinstance(answer, Exception):
                    raise answer
                return copy.deepcopy(answer)
            functions['measure' if name == 'delivered-media' else 'delivered'] = evaluate
        patcher = patch.dict(evaluators.EVALUATORS, functions)
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, name, value):
        path = self.v.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canon(value))

    def get(self):
        return json.loads(self.path.read_text())

    @property
    def path(self):
        return self.v.folder / 'records/qc/masters.json'

    def judged(self, state):
        result = verdict.judge(self.v, 'masters')
        self.assertEqual(result['state'], state, result)
        self.assertEqual(self.get(), result)
        self.assertEqual(verdict.check(result)['ok'], state == 'passed')
        return result

    def manual(self, **changes):
        self.entries[1].update(mode='manual', **changes)

    def record(self, by='checker-renders', saw='Saw the delivered file; words complete.', result='pass'):
        return verdict.record_manual(self.v, 'masters', 'delivered', result, by, saw)


class QC(Fixture):
    """e2e: records/qc/masters.json binds every gate to the evidence actually judged."""

    def test_pass_means_every_applicable_check_ran_on_these_bytes(self):
        result = self.judged('passed')
        self.assertEqual(set(self.calls), {e['id'] for e in self.entries})
        for row, entry in zip(result['checks'], self.entries):
            self.assertEqual(row['read'][0]['sha256'], filehash(self.v.folder / entry['reads'][0]['record']))
        for mutation in ('missing', 'not-run', 'other-bytes'):
            broken = copy.copy(result)
            broken['checks'] = copy.deepcopy(result['checks'])
            if mutation == 'missing':
                broken['checks'].pop()
            elif mutation == 'not-run':
                broken['checks'][0]['result'] = 'manual-pending'
            else:
                broken['checks'][0]['read'][0]['sha256'] = 'sha256:other'
            self.assertFalse(verdict.check(broken)['ok'], mutation)

    def test_failure_names_check_owner_and_fix(self):
        self.put('records/render/long.json', {'integrated_lufs': -11.2})
        with patch.dict(evaluators.EVALUATORS, {'measure': evaluators.measure}):
            result = self.judged('failed')
        answer = verdict.check(result)
        detail = json.dumps(answer['detail'])
        for expected in ('delivered-media', 'rendering', '-11.2', 'rendering.settings.loudness', 'finisher'):
            self.assertIn(expected, detail)
        self.assertEqual(answer['next_action'], 'finisher')
        with patch.dict(evaluators.EVALUATORS, {'measure': evaluators.measure}):
            for value, state in ((-17, 'passed'), (-15, 'passed'), (-17.01, 'failed'), (-14.99, 'failed')):
                self.put('records/render/long.json', {'integrated_lufs': value})
                self.judged(state)

    def test_manual_row_waits_on_its_judge(self):
        for mode in ('manual', 'auto'):
            with self.subTest(mode=mode):
                self.entries[1]['mode'] = mode
                self.entries[1]['version'] += 'x'
                self.answers['delivered'] = {'result': 'manual-pending', 'words': 'flag needs its judge'}
                result = self.judged('waiting_manual')
                self.assertIn('checker-renders', json.dumps(verdict.check(result)))
                for by, saw in (('finisher', 'saw file'), ('checker-copy', 'saw file'), ('checker-renders', '')):
                    before = self.path.read_bytes()
                    with self.assertRaises((ValueError, SpineError)):
                        self.record(by=by, saw=saw)
                    self.assertEqual(before, self.path.read_bytes())
                self.assertEqual(self.record()['state'], 'passed')
                with self.assertRaises((ValueError, SpineError)):
                    self.record()

    def test_manual_row_records_who_and_what(self):
        self.manual()
        pending = self.judged('waiting_manual')
        result = self.record()
        row = next(r for r in result['checks'] if r['id'] == 'delivered')
        self.assertEqual(row['by'], 'checker-renders')
        self.assertEqual(row['saw'], 'Saw the delivered file; words complete.')
        self.assertEqual(row['key'], pending['checks'][1]['key'])
        self.put('records/final_manifest.json', {'value': 3})
        with self.assertRaises((ValueError, SpineError)):
            self.record()
        self.judged('waiting_manual')
        self.assertEqual(self.record(result='fail')['state'], 'failed')
        self.entries[1].update(mode='auto', may_lower=False, version='sha256:flag')
        self.answers['delivered'] = {'result': 'manual-pending', 'words': 'flag', 'evaluation': 'fail'}
        self.judged('waiting_manual')
        with self.assertRaises((ValueError, SpineError)):
            self.record()
        self.entries[1].update(may_lower=True, version='sha256:lowerable')
        self.judged('waiting_manual')
        self.assertEqual(self.record()['state'], 'passed')

    def test_unavailable_measure_is_never_pass(self):
        for cause, token in (('missing', 'render:long'), ('exception', 'speech engine absent'),
                             ('rules', 'qc.rules'), ('setting', 'rendering.settings.loudness')):
            with self.subTest(cause=cause):
                self.entries[0]['version'] += 'x'
                self.put('records/render/long.json', {'value': 1})
                self.answers['delivered-media'] = {'result': 'pass', 'words': 'checked'}
                self.v.settings['qc.rules'] = {'delivered-loudness': {'from': '2025-01-01', 'mode': 'refuse'}}
                self.v.settings['rendering.settings.loudness'] = {'name': 'integrated_lufs', 'unit': 'LUFS', 'min': -17, 'max': -15}
                if cause == 'missing':
                    (self.v.folder / 'records/render/long.json').unlink()
                elif cause == 'exception':
                    self.answers['delivered-media'] = OSError('speech engine absent')
                else:
                    self.v.settings.pop('qc.rules' if cause == 'rules' else 'rendering.settings.loudness')
                result = self.judged('unavailable')
                self.assertEqual(result['checks'][0]['result'], 'unavailable')
                self.assertIn(token, result['checks'][0]['words'])

    def test_stale_verdict_is_rejudged(self):
        result = self.judged('passed')
        for change in ('bytes', 'version', 'setting'):
            if change == 'bytes':
                path = self.v.folder / 'records/render/long.json'
                st = path.stat()
                raw = path.read_bytes()
                path.write_bytes(raw.replace(b'1', b'9'))
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
                self.assertEqual(path.stat().st_size, st.st_size)
            elif change == 'version':
                self.entries[0]['version'] += 'x'
            else:
                self.v.settings['rendering.settings.loudness']['min'] = -18
            self.assertEqual(verdict.check(result)['state'], 'stale')
            self.calls.clear()
            result = self.judged('passed')
            self.assertEqual(self.calls, ['delivered-media'])
        removed = self.entries.pop()
        self.assertEqual(verdict.check(result)['state'], 'stale')
        result = self.judged('passed')
        self.entries.append(removed)
        self.assertEqual(verdict.check(result)['state'], 'stale')
        self.judged('passed')

    def test_nothing_shows_passed_on_a_stale_verdict(self):
        result = self.judged('passed')
        before = self.path.read_bytes()
        self.put('records/render/long.json', {'value': 9})
        for consumer in ('spine gate', 'Board', 'approval'):
            with self.subTest(consumer=consumer):
                answer = verdict.check(result)
                self.assertFalse(answer['ok'])
                self.assertEqual(answer['state'], 'stale')
                self.assertEqual(answer['next_action'], 'judge')
        self.assertEqual(self.path.read_bytes(), before)
