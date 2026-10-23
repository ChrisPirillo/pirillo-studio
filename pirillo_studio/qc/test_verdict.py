"""Remaining QC machinery contracts; heard/delivered are in test_heard.py and the host switch is seam 3."""
import copy
import json
import multiprocessing
import os
import tempfile
from unittest.mock import patch

from pirillo_studio.qc import evaluators, verdict
from pirillo_studio.qc.test_qc import Fixture
from pirillo_studio.spine.files import SpineError, canon, digest, filehash


class Verdicts(Fixture):
    """e2e: records/qc/masters.json stays atomic, current and scoped to its stage."""

    def test_one_setting_decides_warn_or_refuse(self):
        self.answers['delivered-media'] = {'result': 'fail', 'words': 'outside loudness'}
        refused = self.judged('failed')
        self.v.settings['qc.rules']['delivered-loudness']['mode'] = 'warn'
        warned = self.judged('passed')
        self.assertEqual(warned['checks'][0]['result'], 'warn')
        self.assertEqual(warned['checks'][1], refused['checks'][1])
        self.assertNotIn('from', self.entries[0])
        self.assertNotIn('from', warned['checks'][0])
        self.v.settings['qc.rules']['delivered-loudness']['mode'] = 'refuse'
        self.judged('failed')

    def test_rule_after_start_never_refuses_in_flight(self):
        self.answers['delivered-media'] = {'result': 'fail', 'words': 'outside loudness'}
        self.v.settings['qc.rules']['delivered-loudness']['from'] = '2026-02-01'
        first = self.judged('passed')
        self.assertEqual(first['checks'][0]['result'], 'warn')
        self.put('records/job.json', {'video_started': '2026-03-01T00:00:00Z'})
        self.assertEqual(self.judged('passed')['video_started'], first['video_started'])
        original = self.v.folder
        self.v.folder = original.parent / 'later-video'
        self.v.folder.mkdir()
        self.put('records/job.json', {'video_started': '2026-03-01T00:00:00Z'})
        self.put('records/render/long.json', {'value': 1})
        self.put('records/final_manifest.json', {'value': 2})
        self.judged('failed')

    def test_unchanged_inputs_change_nothing(self):
        first = self.judged('passed')
        before, stat = self.path.read_bytes(), self.path.stat()
        self.calls.clear()
        second = self.judged('passed')
        self.assertEqual(first, second)
        self.assertEqual(self.calls, [])
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(stat.st_mtime_ns, self.path.stat().st_mtime_ns)

    def test_crash_leaves_no_passing_verdict(self):
        for after in (False, True):
            with self.subTest(after_rename=after):
                self.put('records/render/long.json', {'value': 1})
                self.answers['delivered-media'] = {'result': 'pass', 'words': 'checked'}
                prior = self.judged('passed')
                before = self.path.read_bytes()
                self.put('records/render/long.json', {'value': 2})
                self.answers['delivered-media'] = {'result': 'fail', 'words': 'changed bytes fail'}
                replace = os.replace
                def die(src, dst):
                    if after:
                        replace(src, dst)
                    os._exit(73)
                def run():
                    with patch.object(verdict.os, 'replace', die):
                        verdict.judge(self.v, 'masters')
                process = multiprocessing.get_context('fork').Process(target=run)
                process.start()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join()
                    self.fail('judge did not reach rename')
                self.assertEqual(process.exitcode, 73)
                disk = self.get()
                if after:
                    self.assertEqual(disk['state'], 'failed')
                else:
                    self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(verdict.check(prior)['state'], 'stale')
                self.judged('failed')
                self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ['masters.json', 'masters.json.lock'])

    def test_checks_never_read_the_recipe(self):
        for recipe in ('plain', 'shorts-only'):
            self.put('records/recipe.json', {'recipe': recipe})
            self.judged('passed')
        short = self.original_catalog('short')
        self.assertEqual({e['id'] for e in short}, {'short-media', 'captions', 'heard'})
        self.assertFalse(any(e['owner'] == 'cutting' or e['id'] in ('attention', 'framing') for e in short))
        self.assertEqual({e['id'] for e in self.original_catalog('masters')},
                         {'delivered-media', 'delivered', 'heard'})

    def test_nonfinite_inputs_and_path_aliases_never_pass(self):
        for value in (float('nan'), float('inf'), float('-inf')):
            self.v.settings['rendering.settings.loudness'] = value
            self.judged('unavailable')
        self.v.settings['rendering.settings.loudness'] = {'name': 'integrated_lufs', 'unit': 'LUFS', 'min': -17, 'max': -15}
        reading = self.v.folder / 'records/render/long.json'
        for value in (float('nan'), float('inf'), float('-inf')):
            reading.write_text(json.dumps({'integrated_lufs': value}))
            self.judged('unavailable')
        self.put('records/render/long.json', {'value': 1})
        original = self.entries[0]['reads'][0]['record']
        for alias in ('../outside.json', './records/render/long.json', 'records//render/long.json'):
            self.entries[0]['reads'][0]['record'] = alias
            self.judged('unavailable')
        self.entries[0]['reads'][0]['record'] = original
        source = self.v.folder / original
        outside = self.v.folder.parent / 'outside.json'
        outside.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(outside)
        self.judged('unavailable')
        self.assertEqual(json.loads(outside.read_text()), {'value': 1})

    def test_missing_verdict_keys_cannot_pass(self):
        result = self.judged('passed')
        for name in ('schema', 'key', 'checks', 'stage', 'video_started'):
            broken = copy.copy(result)
            del broken[name]
            self.assertFalse(verdict.check(broken)['ok'], name)

    def test_unknown_verdict_schema_refuses(self):
        result = self.judged('passed')
        for schema in (None, 2):
            with self.subTest(schema=schema):
                broken = copy.copy(result)
                if schema is None:
                    del broken['schema']
                else:
                    broken['schema'] = schema
                self.assertFalse(verdict.check(broken)['ok'])
                self.put('records/qc/masters.json', broken)
                before = self.path.read_bytes()
                with self.assertRaisesRegex(SpineError, 'schema'):
                    verdict.judge(self.v, 'masters')
                self.assertEqual(self.path.read_bytes(), before)

    def test_media_reads_use_filehash(self):
        from pathlib import Path
        media = 'YouTube/main.mp4'
        target = self.v.folder / media
        target.parent.mkdir()
        target.write_bytes(b'synthetic media')
        expected = filehash(target)
        self.entries[1]['reads'] = [{'bind': 'file:long', 'record': media}]
        original = Path.read_bytes
        def read_bytes(path):
            self.assertNotEqual(path, target, 'media must use streaming filehash')
            return original(path)
        def evaluate(entry, reads, setting):
            self.assertEqual(reads['file:long'], expected)
            return {'result': 'pass', 'words': 'media hash checked'}
        with patch.object(Path, 'read_bytes', read_bytes), patch.dict(evaluators.EVALUATORS, {'delivered': evaluate}):
            result = self.judged('passed')
        self.assertEqual(result['checks'][1]['read'], [{'bind': 'file:long', 'sha256': expected}])
        target.unlink()
        missing = self.judged('unavailable')
        self.assertIn(media + ': missing', missing['checks'][1]['words'])

    def test_reads_other_domains_records_not_rerun(self):
        from pirillo_studio.cutting import checks as cutting_checks
        self.entries.clear()
        self.put('records/cut_plan2.json', {'key': digest(b'plan')})
        source = self.v.folder / 'source.mp4'
        source.write_bytes(b'synthetic delivered bytes')
        for owner in ('cutting', 'rendering', 'captions', 'release'):
            setting_name = owner + '.settings'
            self.v.settings[setting_name] = {'profile': 'test'}
            reads = {'source.mp4': filehash(source)}
            declarations = [{'bind': 'source', 'record': 'source.mp4'}]
            if owner == 'cutting':
                reads['records/cut_plan2.json'] = filehash(self.v.folder / 'records/cut_plan2.json')
                declarations.append({'bind': 'plan:clock', 'record': 'records/cut_plan2.json'})
            record = {'ok': True, 'state': 'pass', 'next_action': 'continue', 'detail': [],
                      'key': digest(canon({'reads': reads, 'settings': self.v.settings[setting_name]})),
                      'reads': reads, 'checks': {'fixture': []}, 'plan': digest(b'plan')}
            path = 'records/checks/' + owner + '.json'
            self.put(path, record)
            declarations.insert(0, {'bind': owner + '-check', 'record': path})
            self.entries.append({'id': owner, 'stage': 'masters', 'owner': owner,
                                 'checker': 'checker-renders', 'mode': 'auto', 'result': 'record',
                                 'reads': declarations, 'setting': {'name': setting_name, 'owner': owner},
                                 'rule': None, 'may_lower': False, 'fixer': 'finisher',
                                 'version': digest(owner.encode())})
        records = {p: p.read_bytes() for p in (self.v.folder / 'records/checks').iterdir()}
        with patch.object(cutting_checks, 'check', side_effect=AssertionError('owner check reran')) as spy:
            result = self.judged('passed')
            spy.assert_not_called()
        self.assertEqual({r['owner'] for r in result['checks']}, {'cutting', 'rendering', 'captions', 'release'})
        self.assertEqual({p: p.read_bytes() for p in records}, records)
        for owner in ('cutting', 'rendering', 'captions', 'release'):
            path = 'records/checks/' + owner + '.json'
            record = json.loads((self.v.folder / path).read_text())
            original = copy.deepcopy(record)
            for key in (None, digest(b'stale-owner-key')):
                if key is None:
                    record.pop('key')
                else:
                    record['key'] = key
                self.put(path, record)
                self.judged('unavailable')
            self.put(path, original)
        source.write_bytes(b'changed delivered bytes')
        stale = self.judged('unavailable')
        self.assertTrue(all(row['result'] == 'unavailable' for row in stale['checks']))

    def test_input_change_during_evaluation_never_commits(self):
        prior = self.judged('passed')
        before = self.path.read_bytes()
        self.entries[0]['version'] += 'change'
        def change(entry, reads, setting):
            self.put('records/render/long.json', {'value': 8})
            return {'result': 'pass', 'words': 'checked old bytes'}
        with patch.dict(evaluators.EVALUATORS, {'measure': change}):
            with self.assertRaisesRegex(SpineError, 'inputs changed'):
                verdict.judge(self.v, 'masters')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(verdict.check(prior)['state'], 'stale')

    def test_stage_writers_serialize(self):
        import multiprocessing
        context = multiprocessing.get_context('fork')
        started, other = context.Event(), context.Event()
        def compete(stage, event):
            with verdict.writer(self.v, stage):
                event.set()
        with verdict.writer(self.v, 'masters'):
            same = context.Process(target=compete, args=('masters', started))
            separate = context.Process(target=compete, args=('short', other))
            same.start()
            separate.start()
            try:
                self.assertTrue(other.wait(3), 'a different stage was blocked')
                self.assertFalse(started.wait(.1), 'same-stage writer bypassed the lock')
            finally:
                separate.join(3)
        same.join(3)
        for process in (same, separate):
            if process.is_alive():
                process.terminate()
                process.join()
            self.assertEqual(process.exitcode, 0)
        self.assertTrue(started.is_set())
        self.judged('passed')

    def test_catalog_rejects_unknown_names(self):
        from pathlib import Path
        original = json.loads((verdict.HERE / 'catalog.json').read_text())
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            for field in ('stage', 'owner', 'checker', 'binding', 'setting', 'evaluator'):
                rows = copy.deepcopy(original)
                name = 'unknown-' + field
                if field == 'binding':
                    rows[0]['reads'][0]['bind'] = name
                elif field == 'setting':
                    rows[0]['setting']['name'] = name
                elif field != 'evaluator':
                    rows[0][field] = name
                (folder / 'catalog.json').write_text(json.dumps(rows))
                verdict._catalog_cache.clear()
                with patch.object(verdict, 'HERE', folder):
                    if field == 'evaluator':
                        with patch.dict(evaluators.EVALUATORS, {}, clear=True):
                            with self.assertRaisesRegex(SpineError, 'no evaluator'):
                                self.original_catalog('plan')
                    else:
                        with self.assertRaisesRegex(SpineError, name):
                            self.original_catalog('plan')
        verdict._catalog_cache.clear()
        with self.assertRaisesRegex(SpineError, 'unknown stage'):
            self.original_catalog('unknown-stage')
        self.judged('passed')
