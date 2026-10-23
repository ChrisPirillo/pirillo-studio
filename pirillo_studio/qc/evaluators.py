"""Pure declared-read evaluators, QC's own heard and delivered included (docs/qc.md).

Future domains supply their public check result together with its content-bound reads.
The host stores check envelopes with key = digest(canon({reads, settings}));
QC verifies that identity and artifact hashes, never invokes the owner's checker.

Record shapes these read, each taken from its owning spec:
- records/hear.json (qc.md section 1, "Required spine.md extension"): {"file", "file_sha256", "engine",
  "model" (sha256 of the model loaded), "words": [{"text", "start_s", "end_s"}]} on the delivered file's clock.
- the cue record (captions.md section 1): "aligner" {"model", "version"} and "cues" [{"words", "lit"}], one lit
  interval per shown word ID, on the output clock rendering's frame map gave; heard never remaps a word itself.
- records/words.json (cutting/plan.py words): [{"id", "text", "start", "end"}].
- records/cut_plan2.json (cutting.md section 1): "spans" (a splice is each speech span's first and last word),
  "removed" (dropped word IDs) and "named", the one place a flag is excused: rule "heard.<kind>", an at_src inside
  the flagged source word, and a why.
- the final manifest (spine/files.py hashes): "members" [{"path", "sha256"}]; render records (rendering.md
  section 1) "file", "file_sha256", "state" ("delivered" passes); cue records' "sidecar" {"file", "sha256"} and
  "state" ("captioned" passes). A member's name is the path its own record gives (rendering's paths;
  release.md: delivered files are never renamed).
- qc.heard, resolved by the host: {"match_s", "onset_s", "clip_s"}: how far a heard word may sit from its
  caption and still be that word, how far its onset may sit from the caption, and how much shorter than its
  source a splice word may sound.
"""
import json
import math
import re
from pathlib import Path

from ..cutting import plan as cut
from ..spine.files import SpineError, canon, digest

_SPINE = Path(__file__).parents[1] / 'spine'
_RECIPES = json.loads((_SPINE / 'recipes.json').read_text())
STAGES = {s['id']: s['run']['role'] for recipe in _RECIPES.values() for s in recipe['stages']}
OWNERS = frozenset(('cutting', 'rendering', 'captions', 'release', 'qc'))
CHECKERS = frozenset(('checker-plans', 'checker-renders', 'checker-copy', 'story-editor', 'owner'))
SETTINGS = {name: owner for owner, names in {
    'cutting': ['cutting.settings'], 'rendering': ['rendering.settings', 'rendering.settings.loudness'],
    'captions': ['captions.settings'], 'release': ['release.settings', 'release.naming'],
    'qc': ['qc.rules', 'qc.attention', 'qc.heard'],
}.items() for name in names}
BINDINGS = {
    'cut-check': 'records/checks/cut.json', 'plan:clock': 'records/cut_plan2.json',
    'words': 'records/words.json', 'source': 'source.mp4', 'paper': 'records/paper.md',
    'story-review': 'records/story_review.json', 'overlay': 'records/overlay_plan.json',
    'shorts': 'records/shorts_spec.json', 'manifest': 'records/final_manifest.json',
    'hear': 'records/hear.json', 'file:long': 'YouTube/main.mp4', 'file:short': 'Shorts/short.mp4',
    'file:arc': 'Draft/arc.mp4', 'file:draft': 'Draft/draft.mp4',
    'publish': 'records/publish.json', 'copy': 'records/copy.json',
    'copy-review': 'records/copy_review.md', 'text-plan': 'records/text_plan.md',
    'text': 'records/text_post.md', 'text-review': 'records/text_review.md',
    'release': 'records/release.json', 'release-check': 'records/checks/release.json',
    'story-map': 'records/story_map.json',
}
for _output in ('arc', 'draft', 'long', 'vertical', 'short'):
    BINDINGS.update({f'render:{_output}': f'records/render/{_output}.json',
                     f'render:{_output}-readings': f'records/render/{_output}-readings.json',
                     f'render:{_output}-check': f'records/checks/render-{_output}.json',
                     f'captions:{_output}': f'records/captions/{_output}.json',
                     f'captions:{_output}-check': f'records/checks/captions-{_output}.json'})
_HASH = re.compile(r'(?:sha256:)?[0-9a-f]{64}\Z')


def unavailable(words):
    return {'result': 'unavailable', 'words': words}


def _equal(a, b):
    return (isinstance(a, str) and isinstance(b, str) and _HASH.fullmatch(a)
            and _HASH.fullmatch(b) and a.removeprefix('sha256:') == b.removeprefix('sha256:'))


def owner_result(record, current_hashes):
    """Validate a public check envelope, including all of its declared input bytes."""
    if not isinstance(record, dict) or not all(k in record for k in ('ok', 'state', 'next_action', 'detail', 'key', 'reads')):
        return unavailable('Owner check record is missing its result or content binding.')
    if not isinstance(record['key'], str) or not _HASH.fullmatch(record['key']):
        return unavailable('Owner check record has no valid content key.')
    declared = record['reads']
    if not isinstance(declared, dict) or not declared:
        return unavailable('Owner check record declares no input hashes.')
    if any(not _equal(value, current_hashes.get(path)) for path, value in declared.items()):
        return unavailable('Owner check record is stale: its input bytes changed or are absent.')
    if record['state'] not in ('pass', 'passed', 'fail', 'failed'):
        return unavailable(f"Owner check is {record['state']}: {record['detail']}")
    passed = record['state'] in ('pass', 'passed')
    if record['ok'] is not passed:
        return unavailable('Owner check result contradicts its state.')
    return {'result': 'pass' if passed else 'fail', 'words': str(record['detail'])}


def _record(entry, reads):
    candidates = [reads.get(r['bind']) for r in entry['reads']
                  if r['record'].startswith('records/checks/')]
    return candidates[0] if len(candidates) == 1 else None


def cutting(entry, reads, setting):
    record = _record(entry, reads)
    result = owner_result(record, reads.get('__paths__', {}))
    if result['result'] == 'unavailable':
        return result
    if record['key'] != digest(canon({'reads': record['reads'], 'settings': setting})):
        return unavailable('Cutting check key does not match its reads and current settings.')
    plan = reads.get('plan:clock')
    if not isinstance(plan, dict) or record.get('plan') != plan.get('key'):
        return unavailable('Cutting check belongs to another plan.')
    if not isinstance(record.get('checks'), dict) or not record['checks']:
        return unavailable('Cutting check has no individual check results.')
    return result


def _owner(entry, reads, setting):
    record = _record(entry, reads)
    result = owner_result(record, reads.get('__paths__', {}))
    if result['result'] == 'unavailable':
        return result
    if record['key'] != digest(canon({'reads': record['reads'], 'settings': setting})):
        return unavailable('Owner check key does not match its reads and resolved settings.')
    paths = reads.get('__paths__', {})
    # The owner result must bind every artifact QC declares, not an unrelated passing check.
    for declared in entry['reads']:
        path = declared['record']
        if not path.startswith('records/checks/') and not _equal(record['reads'].get(path), paths.get(path)):
            return unavailable(f'Owner check does not bind {path}.')
    return result


def rendering(entry, reads, setting):
    return _owner(entry, reads, setting)


def captions(entry, reads, setting):
    return _owner(entry, reads, setting)


def release(entry, reads, setting):
    return _owner(entry, reads, setting)


def measure(entry, reads, setting):
    """Read a measured fact and hold it against the adapter's named range."""
    if not isinstance(setting, dict) or not set(setting) & {'min', 'max'}:
        return unavailable('Resolved measurement setting needs min or max.')
    name = setting['name']
    values = []
    for declaration in entry['reads']:
        record = reads.get(declaration['bind'])
        if isinstance(record, dict):
            measured = record.get('readings', record)
            if isinstance(measured, dict) and name in measured:
                values.append(measured[name])
    if len(values) != 1:
        return unavailable(f'Missing or ambiguous reading: {name}.')
    value = values[0]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return unavailable(f'Reading {name} is not a finite number.')
    for bound in ('min', 'max'):
        if bound in setting and (isinstance(setting[bound], bool) or not isinstance(setting[bound], (int, float))
                                 or not math.isfinite(setting[bound])):
            return unavailable(f'Measurement setting {bound} is not finite.')
    passed = ('min' not in setting or value >= setting['min']) and ('max' not in setting or value <= setting['max'])
    return {'result': 'pass' if passed else 'fail',
            'words': f"{name} is {'within' if passed else 'outside'} {entry['setting']['name']}.",
            'measured': {'name': name, 'value': value, 'unit': setting['unit']}}


def judgment(entry, reads, setting):
    return {'result': 'manual-pending', 'words': f"Waiting for {entry['checker']} to judge {entry['id']}."}


def _time(value, at):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise SpineError(f'{at}: not a finite time')
    return value


def _said(text):
    return ''.join(c for c in text.casefold() if c.isalnum())


def heard(entry, reads, setting):
    """Captions against the words an independent engine heard in the delivered file (qc.md contract 14)."""
    bind = {r['bind'].split(':')[0]: r for r in entry['reads']}
    match, onset, clip = (_time((setting or {}).get(k), f'qc.heard {k}') for k in ('match_s', 'onset_s', 'clip_s'))
    hear, cues, file = reads['hear'], reads[bind['captions']['bind']], bind['file']
    current = reads['__hashes__'][file['bind']]
    if hear.get('file') != file['record'] or not _equal(hear.get('file_sha256'), current):
        return unavailable(f"Hear record is stale: it heard {hear.get('file')} {hear.get('file_sha256')}, "
                           f"not {file['record']} {current}.")
    aligner = cues.get('aligner')
    if not isinstance(aligner, dict) or not all(_HASH.fullmatch(str(x)) for x in (aligner.get('version'), hear.get('model'))):
        raise SpineError('hear model or captions aligner version is not a content hash')
    if _equal(hear['model'], aligner['version']):
        return unavailable(f"Hear engine {hear.get('engine')} {hear['model']} is the captions aligner "
                           f"{aligner.get('model')} {aligner['version']}; heard needs an independent engine.")
    rows = cut.words(reads['words'])
    source = {w['id']: w for w in rows}
    said = [(_said(w['text']), _time(w['start_s'], f'hear word {i}'), _time(w['end_s'], f'hear word {i}'))
            for i, w in enumerate(hear['words'])]
    if any(end < start for _, start, end in said):
        raise SpineError('hear word ends before it starts')
    edges, removed, named = set(), {}, []
    if 'plan' in bind:
        plan = reads['plan:clock']
        edges = {wid for span in plan['spans'] for wid in span['words']}
        for cut_words in (r['words'] for r in plan['removed']):
            for w in cut.selected(rows, cut_words) if len(cut_words) == 2 else [source[i] for i in cut_words]:
                removed.setdefault(_said(w['text']), []).append(w)
        named = plan['named']
    flags, used = [], set()
    if not any(cue['words'] for cue in cues['cues']):  # nothing compared is never a pass
        raise SpineError(f"{bind['captions']['record']}: no cues to hear")
    for cue in cues['cues']:
        if len(cue['words']) != len(cue['lit']):
            raise SpineError(f"cue {cue.get('id')}: one lit interval per shown word")
        for wid, lit in zip(cue['words'], cue['lit']):
            w, at = source[wid], _time(lit[0], f'cue {wid}')
            kind = 'clipped' if wid in edges else 'caption-off'
            near = sorted((abs(h[1] - at), i) for i, h in enumerate(said) if h[0] == _said(w['text']) and abs(h[1] - at) <= match)
            if not near:
                flags.append((kind, [w], f"{wid} '{w['text']}' captioned at {at}s is not heard there"))
                continue
            used.add(near[0][1])
            _, start, end = said[near[0][1]]
            if wid in edges and end - start < w['end'] - w['start'] - clip:
                flags.append(('clipped', [w], f"{wid} '{w['text']}' at a splice is heard for {end - start:.2f}s "
                                             f"of its {w['end'] - w['start']:.2f}s"))
            if abs(start - at) > onset:
                flags.append(('caption-off', [w], f"{wid} '{w['text']}' is captioned at {at}s, heard at {start}s"))
    flags += [('removed', removed[h[0]], f"removed '{h[0]}' is heard again at {h[1]}s")
              for i, h in enumerate(said) if i not in used and h[0] in removed]

    def why(kind, words):
        return next((n['why'] for n in named if n['rule'] == f'heard.{kind}' and isinstance(n.get('why'), str)
                     and n['why'].strip() and any(w['start'] <= n['at_src'] <= w['end'] for w in words)), None)
    open_flags = [f'{kind}: {say}' for kind, words, say in flags if why(kind, words) is None]
    excused = [f'{kind}: {say} (named: {why(kind, words)})' for kind, words, say in flags if why(kind, words)]
    if open_flags:
        return {'result': 'fail', 'words': 'Fix each heard flag or name it in the plan with a why: '
                + '; '.join(open_flags + excused)}
    if excused:
        return {'result': 'manual-pending', 'words': f"{entry['checker']} judges each named heard flag: "
                + '; '.join(excused)}
    return {'result': 'pass', 'words': 'Every captioned word is heard where its caption shows it.'}


def delivered(entry, reads, setting):
    """Every manifest member present, named and passed by its own record, and isolated (qc.md contract 15)."""
    members, facts, owned = reads['manifest'].get('members'), reads['__members__'], {}
    if not isinstance(members, list) or not members:
        raise SpineError('final manifest lists no members')
    for r in entry['reads']:
        record = reads[r['bind']]
        if r['bind'].startswith('render:'):
            owned[record['file']] = (r['bind'], record['state'] == 'delivered', record['file_sha256'])
        elif r['bind'].startswith('captions:'):
            sidecar = record['sidecar']
            owned[sidecar['file']] = (r['bind'], record['state'] == 'captioned', sidecar['sha256'])
    problems, listed = [], set()
    for member in members:
        path = member['path']
        fact, (bind, passing, sha) = facts[str(path)], owned.get(path, (None, False, None))
        if path in listed:
            problems.append(f'{path}: listed twice')
        listed.add(path)
        if 'error' in fact:
            problems.append(f"{path}: {fact['error']}")
        elif fact['sha256'] is None:
            problems.append(f'{path}: missing')
        elif not _equal(fact['sha256'], member['sha256']):
            problems.append(f'{path}: its bytes differ from the manifest')
        elif fact['links'] != 1:
            problems.append(f"{path}: {fact['links']} names share its inode; a delivered file is its own copy")
        if bind is None:
            problems.append(f'{path}: no render or cue record names this path')
        elif not passing:
            problems.append(f'{path}: its record {bind} is not passing')
        elif not _equal(sha, fact.get('sha256')):
            problems.append(f'{path}: its record {bind} binds other bytes')
    problems += [f'{path}: {bind} is missing from the manifest' for path, (bind, _, _) in owned.items()
                 if path not in listed]
    if problems:
        return {'result': 'fail', 'words': 'Delivered set: ' + '; '.join(problems)}
    return {'result': 'pass', 'words': f'{len(members)} delivered files present, named, passing and isolated.'}


EVALUATORS = {'record:cutting': cutting, 'record:rendering': rendering, 'record:captions': captions,
              'record:release': release, 'measure': measure, 'judgment': judgment,
              'heard': heard, 'delivered': delivered}
