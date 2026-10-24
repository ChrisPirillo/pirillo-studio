"""QC verdict machinery (docs/qc.md sections 1–3).

The host supplies folder, resolve_setting(name) and now(). A per-stage file lock
serializes writers across processes. A Verdict retains that adapter
for check(); disk records can be attached with Verdict(record, video). Evaluators
receive only declared evidence, a manifest's members included.
"""
import copy
import contextlib
import fcntl
import datetime as dt
import os
import json
import inspect
from pathlib import Path

from ..spine.files import SpineError, canon, digest, filehash, inside, load, safe, finite
from . import evaluators

HERE = Path(__file__).parent
_RESULTS = {'pass', 'fail', 'warn', 'unavailable', 'manual-pending'}
_catalog_cache = {}


class Verdict(dict):
    """A serialized verdict with its host context kept out of the record."""

    def __init__(self, record, video):
        super().__init__(record)
        self.video = video


def evaluator(entry):
    key = entry['id'] if entry['id'] in ('heard', 'delivered') else (
        'record:' + entry['owner'] if entry['result'] == 'record' else entry['result'])
    try:
        return evaluators.EVALUATORS[key]
    except KeyError:
        raise SpineError(f"{entry['id']}: no evaluator {key}") from None


def _source(function):
    """This evaluator and every core helper it calls, in any core module, so a changed helper restales its verdicts."""
    pending, seen, sources = [function], set(), []
    while pending:
        fn = pending.pop()
        if fn in seen:
            continue
        seen.add(fn)
        sources.append(inspect.getsource(fn))
        names, codes = set(), [fn.__code__]
        while codes:  # nested comprehensions and closures name helpers too
            code = codes.pop()
            names.update(code.co_names)
            codes.extend(c for c in code.co_consts if inspect.iscode(c))
        for name in names:
            value = fn.__globals__.get(name)
            if inspect.ismodule(value) and value.__name__.startswith('pirillo_studio.'):  # e.g. cut.words
                pending.extend(f for n in names if inspect.isfunction(f := getattr(value, n, None)))
            elif inspect.isfunction(value) and value.__module__.startswith('pirillo_studio.'):
                pending.append(value)
    return '\n'.join(sorted(sources)).encode()


def catalog(stage):
    """Load and validate the whole catalog; return independent entries for one stage."""
    raw = (HERE / 'catalog.json').read_bytes()
    source = Path(evaluators.__file__).read_bytes()
    identity = digest(raw + source)
    if identity not in _catalog_cache:
        entries = load(HERE, 'catalog.json')
        if isinstance(entries, dict):
            entries = entries['checks']
        seen = set()
        for e in entries:
            name = e['id']
            for field, known in (('stage', evaluators.STAGES), ('owner', evaluators.OWNERS),
                                 ('checker', evaluators.CHECKERS)):
                if e[field] not in known:
                    raise SpineError(f"{name}: unknown {field} {e[field]}")
            if e['checker'] == evaluators.STAGES[e['stage']]:
                raise SpineError(f'{name}: checker is stage maker')
            if (e['stage'], name) in seen:
                raise SpineError(f'{name}: duplicate check in {e["stage"]}')
            seen.add((e['stage'], name))
            if e['mode'] not in ('auto', 'manual') or e['result'] not in ('measure', 'record', 'judgment'):
                raise SpineError(f'{name}: unknown mode or result')
            if type(e['may_lower']) is not bool or not e['fixer']:
                raise SpineError(f'{name}: invalid may_lower or fixer')
            binds = set()
            for r in e['reads']:
                safe(r['record'], name)
                if evaluators.BINDINGS.get(r['bind']) != r['record'] or r['bind'] in binds:
                    raise SpineError(f"{name}: unknown or duplicate binding {r['bind']} ({r['record']})")
                binds.add(r['bind'])
            if not binds:
                raise SpineError(f'{name}: no reads')
            if ('each' in e or 'always' in e) and not (
                    'manifest' in binds and isinstance(e.get('each'), list) and e['each']
                    and set(e['each']) <= set(evaluators.EACH) and len(set(e['each'])) == len(e['each'])
                    and isinstance(e.get('always', []), list) and set(e.get('always', [])) <= {'long', 'vertical'}):
                raise SpineError(f'{name}: each needs the manifest read and known kinds; always only long or vertical')
            setting = e.get('setting')
            if setting and evaluators.SETTINGS.get(setting['name']) != setting['owner']:
                raise SpineError(f"{name}: unknown setting {setting['name']}")
            evaluator(e)
            if 'version' in e:
                raise SpineError(f'{name}: version must be computed')
            e['version'] = digest(canon(e) + _source(evaluator(e)))
        _catalog_cache.clear()
        _catalog_cache[identity] = entries
    if stage not in {e['stage'] for e in _catalog_cache[identity]}:
        raise SpineError(f'unknown stage {stage}')
    return copy.deepcopy([e for e in _catalog_cache[identity] if e['stage'] == stage])


def _date(value):
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        result = result.replace(tzinfo=dt.timezone.utc)
    return result


def _read(video, path):
    safe(path, 'QC read')
    target = inside(video.folder, path)
    if path.endswith('.json'):
        raw = target.read_bytes()
        return json.loads(raw, parse_constant=finite), digest(raw)
    hashed = filehash(target)
    if hashed is None:
        raise SpineError(f'{path}: missing')
    return hashed, hashed


def _snapshot(video, entry, started):
    reads, hashes, paths, errors = {}, {}, {}, []

    def take(name, path):
        try:
            reads[name], hashes[name] = _read(video, path)
            paths[path] = hashes[name]
        except Exception as exc:
            hashes[name] = None
            errors.append(f'{name} ({path}): {exc}')
    for binding in entry['reads']:
        take(binding['bind'], binding['record'])
    # A per-output entry reads one record of each kind for every output its manifest names (evaluators.outputs).
    reads['__outputs__'] = []
    if entry.get('each') and 'manifest' in reads:
        try:
            reads['__outputs__'] = evaluators.outputs(entry, reads['manifest'])
            for output in reads['__outputs__']:
                for name, path in evaluators.each(entry, output).items():
                    take(name, path)
        except Exception as exc:
            errors.append(f'outputs: {exc}')
    # Owner records declare the inputs that their check certified. These hashes are
    # part of this check's key, so unchanged owner JSON cannot hide a changed file.
    for record in list(reads.values()):
        if isinstance(record, dict) and isinstance(record.get('reads'), dict):
            for path in record['reads']:
                try:
                    safe(path, 'owner reads')
                    paths[path] = filehash(inside(video.folder, path))
                    if paths[path] is None:
                        raise SpineError('missing owner input')
                except Exception as exc:
                    paths[path] = None
                    errors.append(f'{path}: {exc}')
    # A manifest's members are reads too (spine.md): their bytes and link counts key the check; an unsafe or
    # aliased member is a fact the evaluator names, not a read error.
    members = {}
    for record in list(reads.values()):
        for member in record.get('members', []) if isinstance(record, dict) else []:
            path = member.get('path') if isinstance(member, dict) else None
            try:
                safe(path, 'manifest member')
                target = inside(video.folder, path)
                links = target.stat().st_nlink if target.exists() else None
                members[path] = {'sha256': filehash(target), 'links': links}
            except Exception as exc:
                members[str(path)] = {'error': str(exc)}
    setting, rule_mode = None, 'refuse'
    try:
        if entry.get('setting'):
            setting = video.resolve_setting(entry['setting']['name'])
            if setting is None:
                raise SpineError('unresolved setting')
            canon(setting)
    except Exception as exc:
        setting = None
        errors.append(f"{entry['setting']['name']}: {exc}")
    try:
        if entry.get('rule'):
            rules = video.resolve_setting('qc.rules')
            rule = rules[entry['rule']]
            if rule['mode'] not in ('warn', 'refuse'):
                raise SpineError('invalid rule mode')
            rule_mode = 'warn' if _date(rule['from']) > _date(started) else rule['mode']
    except Exception as exc:
        errors.append(f"qc.rules {entry['rule']}: {exc}")
        rule_mode = None
    key = digest(canon({'version': entry['version'], 'outputs': reads['__outputs__'], 'read': hashes, 'paths': paths,
                        'members': members, 'setting': setting, 'rule_mode': rule_mode, 'errors': errors}))
    reads['__hashes__'], reads['__paths__'], reads['__members__'] = hashes, paths, members
    return {'key': key, 'reads': reads, 'setting': setting, 'rule_mode': rule_mode, 'errors': errors}


def _state(rows):
    for result, state in (('fail', 'failed'), ('unavailable', 'unavailable'),
                          ('manual-pending', 'waiting_manual')):
        if any(row['result'] == result for row in rows):
            return state
    return 'passed'


def _key(rows):
    return digest(canon(sorted((row['id'], row['key']) for row in rows)))


def _row(entry, snap):
    row = {k: entry[k] for k in ('id', 'owner', 'fixer')}
    row.update(key=snap['key'], read=[{'bind': b, 'sha256': h} for b, h in snap['reads']['__hashes__'].items()],
               rule_mode=snap['rule_mode'], by='qc')
    if entry.get('setting'):
        row['setting'] = entry['setting']
    try:
        if snap['errors']:
            raise SpineError('; '.join(snap['errors']))
        value = ({'result': 'manual-pending', 'words': 'Awaiting independent judgment'}
                 if entry['mode'] == 'manual' else evaluator(entry)(entry, snap['reads'], snap['setting']))
        if value['result'] not in _RESULTS:
            raise SpineError('invalid evaluator result')
        canon(value)
        for field in ('result', 'words', 'measured', 'evaluation'):
            if field in value:
                row[field] = value[field]
        if not isinstance(row['words'], str):
            raise SpineError('evaluator words must be text')
        if row['result'] == 'fail' and snap['rule_mode'] == 'warn':
            row['result'] = 'warn'
    except Exception as exc:
        row.update(result='unavailable', words=f"{entry['id']}: {exc}")
    if row['result'] == 'manual-pending':
        row['judge'] = entry['checker']
    return row


def _path(stage):
    safe(stage, 'stage')
    return f'records/qc/{stage}.json'


def _prior(video, stage):
    path = _path(stage)
    if not inside(video.folder, path).exists():
        return None
    record = load(video.folder, path)
    if record.get('schema') != 1:
        raise SpineError('unsupported verdict schema')
    return record


@contextlib.contextmanager
def writer(video, stage):
    """One stable lock inode per stage, never removed while another process waits."""
    lock = inside(video.folder, _path(stage) + '.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            temporary = inside(video.folder, _path(stage) + '.tmp')
            if temporary.exists():
                temporary.unlink()
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write(video, stage, record):
    path = _path(stage)
    target = inside(video.folder, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = inside(video.folder, path + '.tmp')
    # One stage writer owns this sibling, including leftovers from a killed write.
    with temporary.open('wb') as handle:
        handle.write(canon(record) + b'\n')
        handle.flush()
        os.fsync(handle.fileno())
    inside(video.folder, path)
    os.replace(temporary, target)


def judge(video, stage):
    """Serialize one stage, rerun changed checks and atomically replace its verdict."""
    entries = catalog(stage)
    with writer(video, stage):
        previous = _prior(video, stage)
        started = previous['video_started'] if previous else load(video.folder, 'records/job.json')['video_started']
        _date(started)
        snapshots = {e['id']: _snapshot(video, e, started) for e in entries}
        old = {r['id']: r for r in previous['checks']} if previous else {}
        rows = [copy.deepcopy(old[e['id']]) if e['id'] in old and old[e['id']].get('key') == snapshots[e['id']]['key']
                else _row(e, snapshots[e['id']]) for e in entries]
        record = {'schema': 1, 'stage': stage, 'key': _key(rows), 'state': _state(rows),
                  'video_started': started, 'checks': rows}
        if previous and all(previous.get(k) == v for k, v in record.items()):
            return Verdict(previous, video)
        # A producer may write while an evaluator runs. Never certify that mixed snapshot.
        if any(_snapshot(video, e, started)['key'] != snapshots[e['id']]['key'] for e in entries):
            raise SpineError('QC inputs changed during judgment; judge again')
        record['judged_at'] = video.now()
        _write(video, stage, record)
        return Verdict(record, video)


def identity(video, stage):
    """The key judge would give the stage now, read-only: no lock, no write, no evaluator (a caller's version)."""
    previous = _prior(video, stage)
    started = previous['video_started'] if previous else load(video.folder, 'records/job.json')['video_started']
    _date(started)
    return _key([{'id': e['id'], 'key': _snapshot(video, e, started)['key']} for e in catalog(stage)])


def check(verdict):
    """Answer the current gate; an old or incomplete identity can never authorize."""
    try:
        entries = catalog(verdict['stage'])
        rows = verdict['checks']
        snapshots = {e['id']: _snapshot(verdict.video, e, verdict['video_started']) for e in entries}
        expected = {name: snap['key'] for name, snap in snapshots.items()}
        for row in rows:
            hashes = snapshots[row['id']]['reads']['__hashes__']
            if row['read'] != [{'bind': b, 'sha256': h} for b, h in hashes.items()]:
                raise SpineError('recorded read hashes changed')
        actual = {r['id']: r['key'] for r in rows}
        if (verdict['schema'] != 1 or actual != expected or len(rows) != len(expected) or verdict['key'] != _key(rows)
                or any(r['result'] not in _RESULTS for r in rows) or verdict['state'] != _state(rows)):
            raise SpineError('verdict keys or rows changed')
    except Exception as exc:
        return {'ok': False, 'state': 'stale', 'next_action': 'judge', 'detail': str(exc)}
    detail = [copy.deepcopy(r) for r in rows if r['result'] != 'pass']
    state = verdict['state']
    first = next((r for r in rows if r['result'] == {'failed': 'fail', 'unavailable': 'unavailable',
                                                  'waiting_manual': 'manual-pending'}.get(state)), None)
    action = first.get('judge', first['fixer']) if first else None
    return {'ok': state == 'passed', 'state': state, 'next_action': action, 'detail': detail}


def record_manual(video, stage, check, result, by, saw):
    """Bind an explicit independent judgment to one current pending check."""
    entries = {e['id']: e for e in catalog(stage)}
    with writer(video, stage):
        prior = _prior(video, stage)
        if prior is None:
            raise SpineError('judge before recording a manual row')
        record = Verdict(prior, video)
        if globals()['check'](record)['state'] == 'stale':
            raise SpineError('stale verdict; judge again')
        if check not in entries:
            raise SpineError(f'unknown check {check}')
        entry = entries[check]
        row = next(r for r in record['checks'] if r['id'] == check)
        if (row['result'] != 'manual-pending' or by != entry['checker']
                or by == evaluators.STAGES[stage] or not isinstance(saw, str) or not saw.strip()):
            raise SpineError('manual row requires its independent judge, pending result and what they saw')
        if result not in ('pass', 'fail', 'warn'):
            raise SpineError('invalid manual result')
        severity = {'pass': 0, 'warn': 1, 'fail': 2}
        baseline = row.get('evaluation', 'pass')
        if baseline in severity and severity[result] < severity[baseline] and not entry['may_lower']:
            raise SpineError('manual row may not lower evaluator result')
        row.update(result=result, by=by, saw=saw, judge=entry['checker'])
        record['state'], record['judged_at'] = _state(record['checks']), video.now()
        if globals()['check'](record)['state'] == 'stale':
            raise SpineError('QC inputs changed during manual judgment')
        _write(video, stage, record)
        return record
