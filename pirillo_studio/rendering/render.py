"""Render lifecycle (docs/rendering.md section 3); the host owns encoding; decoded measurement is the default."""
import shutil
from fractions import Fraction
from pathlib import Path

from ..cutting import plan
from ..spine.files import SpineError, canon, filehash
from . import records, request


class Video:
    """Host effects for one stage, with explicit preflight, tool versions and resolved policy.

    encode(request, sibling_path) writes and closes media. measure(request, path) returns
    file_sha256, key, duration_s, fps, loudness_lufs, true_peak_dbtp, frame_checks and checks.
    Each required check has an explicit boolean ok, value, setting reference and evidence.
    The decoded adapter supplies acceptance; an absent reading never counts as a pass here.
    """

    def __init__(self, folder, stage, *, versions, preflight, encode=None, measure=None, settings=None, prepare=None, captioner=None):
        self.folder, self.stage = Path(folder), stage
        self.native, self.captioner = encode is None, captioner
        self.versions, self.preflight = versions, preflight
        if measure is None:
            from . import decoded, measure as readings
            actual = decoded.tools()
            for tool, version in actual.items():
                if tool in versions and versions[tool] != version:
                    raise SpineError(f'{tool}: declared version is stale')
            self.versions = {**versions, **actual}
            measure = lambda req, path: readings.measure(self, req, path)
        if self.native:
            from . import compositor
            if prepare is not None:
                raise SpineError('compositor: custom preparation requires a custom encoder')
            encode = lambda req, path: compositor.encode(self, req, path)
            prepare = lambda req: compositor.prepare(self, req)
        self.encode, self.measure, self.prepare = encode, measure, prepare
        self.settings = request.settings() if settings is None else settings


class MeasurementRefused(SpineError):
    def __init__(self, reading, setting, error):
        self.reading, self.setting = reading, setting
        super().__init__(str(error))


def measured(video, req, name):
    """Validate the adapter envelope and bindings; numerical acceptance belongs to its checks."""
    reading, setting = 'measure', 'rendering.settings#checks'
    try:
        path = records.path(video, name, writable=True)
        before = filehash(path)
        value = video.measure(req, path)
        reading = 'file_sha256'
        if not before or value['file_sha256'] != before or filehash(path) != before:
            raise SpineError(f'{name}: measurement does not bind current bytes')
        reading = 'key'
        if value['key'] != req['key']:
            raise SpineError('measurement request key is stale')
        for reading in ('duration_s', 'loudness_lufs', 'true_peak_dbtp'):
            plan.number(value[reading], reading)
        if value['duration_s'] <= 0:
            raise SpineError('duration_s must be positive')
        reading = 'fps'
        if not isinstance(value['fps'], str) or Fraction(value['fps']) <= 0:
            raise SpineError('fps: expected positive measured fraction')
        reading = 'frame_checks'
        if not isinstance(value['frame_checks'], list) or not value['frame_checks']:
            raise SpineError('frame_checks: missing decoded coverage')
        for reading in req['settings']['checks']:
            setting = f'rendering.settings#checks.{reading}'
            check = value['checks'][reading]
            plan.text(check['setting'], f'{reading} setting')
            setting = check['setting']
            plan.text(check['evidence'], f'{reading} evidence')
            canon(check['value'])
            if check['value'] is None or check['ok'] is not True:
                raise SpineError(f'{reading}: measured {check["value"]!r}; missing or outside {check["setting"]}')
        canon(value)
        return {**value, 'settings': req['settings'], 'tools': req['tools']}
    except MeasurementRefused:
        raise
    except Exception as error:
        raise MeasurementRefused(reading, setting, error) from None


def approval(video, req):
    """Only an explicit choice of the current measured draft and exact plan permits a master."""
    paths = ['records/draft_decision.json', 'records/render/draft.json', 'Draft/draft.mp4',
             'records/render/draft-readings.json']
    hashes = {p: filehash(records.path(video, p)) for p in paths}
    viewed = {'file': 'Draft/draft.mp4', 'file_sha256': hashes['Draft/draft.mp4'], 'plan': req['plan_key']}
    if any(value is None for value in hashes.values()):
        return False, viewed, hashes, 'current measured draft and explicit owner decision required'
    choice = records.read(video, paths[0])
    draft = records.read(video, paths[1])
    readings = records.read(video, paths[3])
    current = request.resolve(video, 'draft', 'draft')
    approved = (choice.get('decision') == 'approve' and choice.get('file') == viewed['file']
                and choice.get('file_sha256') == viewed['file_sha256'] and choice.get('plan') == viewed['plan']
                and draft.get('state') == 'waiting_owner' and draft.get('file') == viewed['file']
                and draft.get('file_sha256') == viewed['file_sha256'] and draft.get('key') == current['key']
                and draft.get('readings_sha256') == hashes[paths[3]]
                and readings.get('file_sha256') == viewed['file_sha256'] and readings.get('key') == current['key'])
    return approved, viewed, hashes, 'owner decision must name the current measured draft and plan'


def fresh(video, req, acceptance):
    current = request.resolve(video, req['stage'], req['output'])
    generated = req.get('generated', {})
    original = {p: h for p, h in req['inputs'].items() if p not in generated}
    if (current['key'] != req['key'] or current['inputs'] != original
            or any(filehash(records.path(video, p)) != h for p, h in generated.items())):
        raise SpineError('render inputs changed before publication')
    if acceptance is not None:
        approved, _, hashes, _ = approval(video, req)
        if not approved or hashes != acceptance:
            raise SpineError('owner approval changed before publication')


def render(video, output):
    """Publish one verified file, import a supplied short unchanged, or persist its precise refusal."""
    try:
        req = request.resolve(video, video.stage, output)
        with records.writer(video, req['output_id']):
            req = request.resolve(video, video.stage, output)
            return _render(video, req)
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as error:
        if isinstance(error, SpineError):
            raise
        raise SpineError(f'render {output}: {error}') from None


def _render(video, req):
    name = records.name(req)
    # Every writable boundary is checked before the first adapter effect.
    owned = {records.path(video, target, writable=True) for target in
             (req['file'], name, records.readings_name(req),
              f"records/render/{req['output_id']}.lock")}
    work = records.path(video, req['file']).parent, f".render-{req['output_id']}-"  # work and backup files
    for source in {req['source'], *req['inputs']}:
        read = records.path(video, source)
        if read in owned or (read.parent, read.name[:len(work[1])]) == work:
            raise SpineError(f"{source}: an input is a file this output writes")
    if video.native and req['mode'] == 'render':
        from . import compositor
        for produced in compositor.names(req):
            records.path(video, produced, writable=True)
            if produced in req['inputs']:
                raise SpineError(f'{produced}: input aliases generated output')
    old = records.read(video, name)
    base = {'output': req['output'], 'key': req['key'], 'file': req['file'], 'mode': req['mode'],
            'request': req, 'ok': False}
    if 'frame_map' in req:
        base.update(frame_map=req['frame_map'], elements=req['elements'],
                    duration_s=req['frame_map'][-1]['out'][1])
    if 'picture_map' in req:
        base['picture_map'] = req['picture_map']
    target = records.path(video, req['file'], writable=True)
    found = filehash(target)
    stored = None
    if old is not None and old.get('step') == 'verify':
        stored, old = old, old['refused']  # judge the refused record again: a restored cause clears
    try:
        accepted = records.recover(video, req, old)
        if accepted is not None and not target.exists():
            accepted = None  # no accepted bytes are left to protect: render again
        previous = records.preserve(video, req, accepted)
    except SpineError as error:
        refused = {**base, 'state': 'render_failed', 'step': 'verify', 'error': str(error), 'found_sha256': found,
                   'refused': old}
        if refused != stored:  # the same refusal is written once, never touched
            records.write(video, name, refused)
        return refused
    acceptance = None
    if req['stage'] == 'masters':
        approved, viewed, acceptance, reason = approval(video, req)
        if not approved:
            waiting = {**base, 'state': 'waiting_owner', 'viewed': viewed, 'reason': reason,
                       'previous': previous}
            records.write(video, name, waiting)
            return waiting
    cached = accepted is not None and accepted['key'] == req['key']
    if cached and video.native and req['mode'] == 'render':
        cached = compositor.reuse(video, req, accepted)
    temporary, step, candidate_sha = None, 'prepare', None
    try:
        if cached:
            candidate = req['file']
            records.write(video, name, {**base, 'state': 'measuring', 'previous': previous})
        else:
            temporary = records.work(video, req)
            candidate = temporary
            state = 'measuring' if req['mode'] == 'measure_existing' else 'rendering'
            pending = {**base, 'state': state, 'work': temporary, 'previous': previous}
            records.write(video, name, pending)
            records.create_work(video, req, temporary)
            if req['mode'] == 'measure_existing':
                step = 'import'
                shutil.copyfile(records.path(video, req['source']), records.path(video, temporary, writable=True))
            else:
                step = 'encode'
                video.encode(req, records.path(video, temporary, writable=True))
            sha = records.sync(video, temporary)
            candidate_sha = sha
            if req['mode'] == 'measure_existing' and sha != req['source_sha256']:
                raise SpineError('supplied short changed while importing')
            pending.update(state='measuring', candidate_sha256=sha)
            records.write(video, name, pending)
        step = 'measure'
        readings = measured(video, req, candidate)
        step = 'inputs'
        fresh(video, req, acceptance)
        if filehash(records.path(video, candidate)) != readings['file_sha256']:
            raise SpineError('measured media changed before publication')
        reading_name = records.readings_name(req)
        result = {**base, 'state': 'waiting_owner' if req['stage'] in {'arc', 'draft'} else 'delivered',
                  'file_sha256': readings['file_sha256'], 'readings': reading_name,
                  'encode_settings': f"rendering.settings#{req['settings']['profile']}"}
        result['ok'] = result['state'] == 'delivered'
        for field in ('duration_s', 'fps', 'loudness_lufs', 'true_peak_dbtp', 'frame_checks'):
            result[field] = readings[field]
        if req['mode'] == 'render' and 'frame_map' in req:
            result.update(frame_map=req['frame_map'], elements=req['elements'])
        if result['state'] == 'waiting_owner':
            result['viewed'] = {'file': req['file'], 'file_sha256': readings['file_sha256'],
                                'plan': req['plan_key']}
        step = 'readings'
        records.write(video, reading_name, readings)
        result['readings_sha256'] = filehash(records.path(video, reading_name))
        step = 'publish'
        if temporary is not None:
            records.publish(video, req, temporary)
        # A crash here leaves measuring, never a delivered record for uncommitted bytes.
        step = 'record'
        records.write(video, name, result)
    except Exception as error:
        records.restore(video, req, previous)
        failure = {**base, 'state': 'render_failed', 'step': step, 'error': str(error), 'previous': previous}
        if previous is None and candidate_sha is not None:
            failure['candidate_sha256'] = candidate_sha
        if isinstance(error, MeasurementRefused):
            failure.update(state='measure_refused', reading=error.reading, setting=error.setting)
        records.write(video, name, failure)
        return failure
    finally:
        if temporary is not None:
            records.remove_work(video, req, temporary)
    records.retire(video, req, previous)
    return result
