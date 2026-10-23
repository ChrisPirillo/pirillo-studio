"""Pure declared-read evaluators; heard and delivered are evaluator built in seam 2b.

Future domains supply their public check result together with its content-bound reads.
The host stores check envelopes with key = digest(canon({reads, settings}));
QC verifies that identity and artifact hashes, never invokes the owner's checker.
"""
import json
import math
import re
from pathlib import Path

from ..spine.files import canon, digest

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
for _output in ('arc', 'draft', 'long', 'short'):
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


def seam_2b(entry, reads, setting):
    return unavailable(f"{entry['id']}: evaluator built in seam 2b")


EVALUATORS = {'record:cutting': cutting, 'record:rendering': rendering, 'record:captions': captions,
              'record:release': release, 'measure': measure, 'judgment': judgment,
              'heard': seam_2b, 'delivered': seam_2b}
