"""Pure declared-read evaluators, QC's own heard and delivered included (docs/qc.md).

Future domains supply their public check result together with its content-bound reads.
The host stores check envelopes with key = digest(canon({reads, settings}));
QC verifies that identity and artifact hashes, never invokes the owner's checker.

Record shapes these read, each taken from its owning spec:
- records/hear/<output-id>.json (qc.md section 1, "Required spine.md extension"), one per delivered output:
  {"file", "file_sha256", "engine", "model" (sha256 of the model loaded), "words": [{"text", "start_s", "end_s"}]}
  on that file's clock. A catalog entry's `each` reads one record per output (EACH), the outputs being its `always`
  IDs and one short-<id> per Shorts/<id>.mp4 manifest member (rendering.md section 1).
- records/heard_named.json (shorts-only's `short` stage): {"named": [...]}, the plan's named shape plus "output"
  (short-<id>), at_src on that supplied file's own clock (records/words-<id>.json).
- the cue record (captions.md section 1): "aligner" {"model", "version"} and "cues" [{"words", "lit"}], one lit
  interval per shown word ID, on the output clock rendering's frame map gave; heard remaps only a removed range's bounding kept words,
  through the output render record's frame_map [{span, src, out}].
- records/words.json (cutting/plan.py words): [{"id", "text", "start", "end"}].
- records/cut_plan2.json (cutting.md section 1): "spans" (a splice is each speech span's first and last word),
  "removed" (dropped word IDs) and "named", the one place a flag is excused: rule "heard.<kind>", an at_src inside
  the flagged source word, and a why.
- the final manifest (spine/files.py hashes): "members" [{"path", "sha256"}]; render records (rendering.md
  section 1) "file", "file_sha256", "state" ("delivered" passes); cue records' "sidecar" {"file", "sha256"} and
  "state" ("captioned" passes). A member's name is the path its own record gives (rendering's paths;
  release.md: delivered files are never renamed).
- qc.heard, resolved by the host: {"match_s", "onset_s", "clip_s", "removed_run"}: how far a heard word may sit
  from its caption (or removed tape from a kept word that bounds it) and still be that word, how far its onset may
  sit from the caption, how much shorter than its source a splice word may sound, and how many of a removed range's
  words, heard in order, make it heard again.
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
    'heard-named': 'records/heard_named.json', 'file:long': 'YouTube/main.mp4', 'file:short': 'Shorts/short.mp4',
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
EACH = {'render': 'records/render/{output}.json', 'captions': 'records/captions/{output}.json',
        'hear': 'records/hear/{output}.json', 'words': 'records/words-{short}.json'}
_FILES = {'long': 'YouTube/main.mp4', 'vertical': 'Vertical/main.mp4'}  # rendering.md section 1; shorts below
_SHORT = re.compile(r'Shorts/([a-z0-9][a-z0-9_-]*)\.mp4')
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


def outputs(entry, manifest):
    """The output IDs a per-output check judges: its `always` ones, then short-<id> per Shorts/<id>.mp4 member."""
    members = manifest.get('members') if isinstance(manifest, dict) else None
    if not isinstance(members, list) or not members:
        raise SpineError('final manifest lists no members')
    shorts = []
    for member in members:
        path = member.get('path') if isinstance(member, dict) else None
        if isinstance(path, str) and path.startswith('Shorts/') and path.endswith('.mp4'):
            match = _SHORT.fullmatch(path)
            if match is None:
                raise SpineError(f'{path}: not a Shorts/<id>.mp4 short (rendering.md section 1)')
            shorts.append(f'short-{match.group(1)}')
    found = list(dict.fromkeys(entry.get('always', []) + shorts))
    if not found:  # nothing compared is never a pass
        raise SpineError('final manifest lists no output to judge')
    return found


def each(entry, output):
    """bind -> record path for one output, from the entry's `each` kinds."""
    short = output.removeprefix('short-') if output.startswith('short-') else None
    if 'words' in entry['each'] and short is None:
        raise SpineError(f'{output}: only a supplied short has its own words record')
    return {f'{kind}:{output}': EACH[kind].format(output=output, short=short) for kind in entry['each']}


def _out(render, word, at):
    """Every output interval the frame map gives one source word (a cold open gives a word two)."""
    pieces = render.get('frame_map') if isinstance(render, dict) else None
    if not isinstance(pieces, list) or not pieces:
        raise SpineError(f'{at}: frame_map must list pieces')
    found, mid = [], (word['start'] + word['end']) / 2
    for i, piece in enumerate(pieces):
        a, b = cut.interval(piece.get('src') if isinstance(piece, dict) else None, f'{at} frame_map[{i}] src')
        c, _ = cut.interval(piece['out'], f'{at} frame_map[{i}] out')
        if a <= mid < b:
            found.append((c + word['start'] - a, c + word['end'] - a))
    return found


def _heard(output, reads, plan, match, onset, clip, run):
    """One output's flags as (kind, output, source words, words), or the words that make heard unavailable."""
    render, cues, hear = (reads[f'{kind}:{output}'] for kind in ('render', 'captions', 'hear'))
    file, at = render.get('file'), f'records/captions/{output}.json'
    own = _FILES.get(output) or f"Shorts/{output.removeprefix('short-')}.mp4"
    if file != own:  # a record filed under another output never stands in for this one's file
        return f'{output}: its render record names {file}, not {own}.'
    current = reads['__members__'].get(str(file), {}).get('sha256')
    if current is None:
        return f'{output}: its file {file} is not a present member of the final manifest.'
    if hear.get('file') != file or not _equal(hear.get('file_sha256'), current):
        return (f"{output}: hear record is stale: it heard {hear.get('file')} {hear.get('file_sha256')}, "
                f"not {file} {current}.")
    aligner = cues.get('aligner')
    if not isinstance(aligner, dict) or not all(_HASH.fullmatch(str(x)) for x in (aligner.get('version'), hear.get('model'))):
        raise SpineError(f'{output}: hear model or captions aligner version is not a content hash')
    if _equal(hear['model'], aligner['version']):
        return (f"{output}: hear engine {hear.get('engine')} {hear['model']} is the captions aligner "
                f"{aligner.get('model')} {aligner['version']}; heard needs an independent engine.")
    rows = cut.words(reads['words'] if plan else reads[f'words:{output}'])
    source = {w['id']: w for w in rows}
    said = [(_said(w['text']), _time(w['start_s'], f'{output} hear word {i}'), _time(w['end_s'], f'{output} hear word {i}'))
            for i, w in enumerate(hear['words'])]
    if any(end < start for _, start, end in said):
        raise SpineError(f'{output}: hear word ends before it starts')
    if not any(cue['words'] for cue in cues['cues']):  # nothing compared is never a pass
        raise SpineError(f'{at}: no cues to hear')
    edges = {wid for span in plan['spans'] for wid in span['words']} if plan else set()
    flags, used = [], set()
    for cue in cues['cues']:
        if len(cue['words']) != len(cue['lit']):
            raise SpineError(f"{output} cue {cue.get('id')}: one lit interval per shown word")
        for wid, lit in zip(cue['words'], cue['lit']):
            w, t = source[wid], _time(lit[0], f'{output} cue {wid}')
            kind = 'clipped' if wid in edges else 'caption-off'
            near = sorted((abs(h[1] - t), i) for i, h in enumerate(said) if h[0] == _said(w['text']) and abs(h[1] - t) <= match)
            if not near:
                flags.append((kind, output, [w], f"{output}: {wid} '{w['text']}' captioned at {t}s is not heard there"))
                continue
            used.add(near[0][1])
            _, start, end = said[near[0][1]]
            if wid in edges and end - start < w['end'] - w['start'] - clip:
                flags.append(('clipped', output, [w], f"{output}: {wid} '{w['text']}' at a splice is heard for "
                                             f"{end - start:.2f}s of its {w['end'] - w['start']:.2f}s"))
            if abs(start - t) > onset:
                flags.append(('caption-off', output, [w], f"{output}: {wid} '{w['text']}' is captioned at {t}s, heard at {start}s"))
    if plan:  # removed tape heard again: a run of its words beside a kept word that bounds it (qc.md section 1)
        index = {w['id']: i for i, w in enumerate(rows)}
        kept = {w['id'] for span in plan['spans'] for w in cut.selected(rows, span['words'])}
        for removed in plan['removed']:
            gone = cut.selected(rows, removed['words']) if len(removed['words']) == 2 else [source[i] for i in removed['words']]
            if not gone:
                continue
            lo, hi = index[gone[0]['id']], index[gone[-1]['id']]
            sides = [w for w in (next((w for w in reversed(rows[:lo]) if w['id'] in kept), None),
                                 next((w for w in rows[hi + 1:] if w['id'] in kept), None)) if w]
            near = [t for w in sides for t in _out(render, w, f'records/render/{output}.json')]
            texts, n = [_said(w['text']) for w in gone], min(run, len(gone))
            for j in range(len(said) - n + 1):
                heard_run = said[j:j + n]
                if (any(i in used for i in range(j, j + n))
                        or not any([h[0] for h in heard_run] == texts[k:k + n] for k in range(len(texts) - n + 1))
                        or not any(max(0, heard_run[0][1] - b, a - heard_run[-1][2]) <= match for a, b in near)):
                    continue
                flags.append(('removed', output, gone, f"{output}: removed '{' '.join(h[0] for h in heard_run)}' "
                                               f"is heard again at {heard_run[0][1]}s"))
                break
    return flags


def heard(entry, reads, setting):
    """Every delivered output's captions against the words an independent engine heard in that file (qc.md
    contract 14): long, vertical and each short on the plan clock through the output's frame map, or a supplied
    short on its own transcript, excused only by the plan's named or the short's records/heard_named.json."""
    values = setting or {}
    match, onset, clip = (_time(values.get(k), f'qc.heard {k}') for k in ('match_s', 'onset_s', 'clip_s'))
    run = values.get('removed_run')
    if isinstance(run, bool) or not isinstance(run, int) or run < 1:
        raise SpineError('qc.heard removed_run: not a whole number of words')
    plan = reads.get('plan:clock')
    named = plan['named'] if plan else reads['heard-named']['named']
    flags, missing = [], []
    for output in reads['__outputs__']:
        found = _heard(output, reads, plan, match, onset, clip, run)
        if isinstance(found, str):
            missing.append(found)
        else:
            flags += found
    if missing:
        return unavailable(' '.join(missing))

    def why(kind, output, words):  # a supplied short's at_src is on its own clock, so its entry names the output
        return next((n['why'] for n in named if n['rule'] == f'heard.{kind}' and isinstance(n.get('why'), str)
                     and n['why'].strip() and (plan or n.get('output') == output)
                     and any(w['start'] <= n['at_src'] <= w['end'] for w in words)), None)
    open_flags = [f'{flag[0]}: {flag[3]}' for flag in flags if why(*flag[:3]) is None]
    excused = [f'{flag[0]}: {flag[3]} (named: {why(*flag[:3])})' for flag in flags if why(*flag[:3])]
    if open_flags:
        return {'result': 'fail', 'words': 'Fix each heard flag or name it with a why: '
                + '; '.join(open_flags + excused)}
    if excused:
        return {'result': 'manual-pending', 'words': f"{entry['checker']} judges each named heard flag: "
                + '; '.join(excused)}
    return {'result': 'pass', 'words': f"Every captioned word is heard where its caption shows it, in "
                                       f"{len(reads['__outputs__'])} outputs."}


def delivered(entry, reads, setting):
    """Every manifest member present, named and passed by its own output's record, and isolated (qc.md
    contract 15); the outputs are long, vertical and one short per Shorts/<id>.mp4 member."""
    members, facts, owned = reads['manifest'].get('members'), reads['__members__'], {}
    if not isinstance(members, list) or not members:
        raise SpineError('final manifest lists no members')
    for output in reads['__outputs__']:
        render, cues = reads[f'render:{output}'], reads[f'captions:{output}']
        owned[render['file']] = (f'render:{output}', render['state'] == 'delivered', render['file_sha256'])
        sidecar = cues['sidecar']
        owned[sidecar['file']] = (f'captions:{output}', cues['state'] == 'captioned', sidecar['sha256'])
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
