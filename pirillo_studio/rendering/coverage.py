"""Plan/subject/word/draft coverage, grounded in the decoded frame map and actual sound."""
import math
from fractions import Fraction

from ..cutting import plan
from ..spine.files import SpineError, canon, digest, filehash
from . import decoded, mapping, pixels, raster, records, sound


def entry(req, name, value, ok, evidence, frames):
    return {'ok': ok, 'result': 'unavailable' if value is None else 'pass' if ok else 'fail',
            'value': value, 'setting': 'rendering.settings#verification.render_checks',
            'coverage': {'frames': frames}, 'evidence': evidence, 'file': req['file'], 'actor': 'rendering'}


def subjects(req, pixels_ok):
    """Each source frame needs explicit boxes; opaque covers are the only excluded frames."""
    observations = {}
    for row in req['faces_data']['frames']:
        index = pixels.integer(row['frame'], 'subject frame')
        if index in observations:
            raise SpineError('subject: duplicate source frame observations')
        boxes = row['boxes']
        if not isinstance(boxes, list) or not boxes:
            raise SpineError('subject: frame has no declared subject boxes')
        observations[index] = [mapping.rectangle(box, 'subject box') for box in boxes]
    unseen, outside, covered, checked = [], [], [], 0
    for row in mapping.pictures(req):
        x, y, w, h = row['crop']
        for index in range(*row['frames']):
            if row['kind'] == 'cover':
                covered.append(index)
                continue
            source = row['source_frames'][0] + index - row['frames'][0]
            if source not in observations:
                unseen.append(index)
                continue
            checked += 1
            if any(not (x <= a and y <= b and a + c <= x + w and b + d <= y + h)
                   for a, b, c, d in observations[source]):
                outside.append(index)
    ok = pixels_ok and not unseen and not outside
    return entry(req, 'subject', {'unseen': unseen, 'outside': outside, 'covered': covered}, ok,
                 'every mapped decoded frame/crop compared with source subject boxes', checked)


def words(video, req, path, pixels_ok):
    selected = req['selected']
    rows, rate = req['frame_map'], decoded.fraction(req['settings']['video']['fps'], 'word-ends', 'video.fps')
    endpoints = []
    for key, piece in (('opens_on', rows[0]), ('ends_on', rows[-1])):
        chosen = plan.selected(req['words_data'], [selected[key]] * 2)[0]
        a, b = chosen['start'], chosen['end']
        inside = piece['src'][0] <= a < b <= piece['src'][1]
        offset = piece['frames'][0] - round(Fraction(str(piece['src'][0])) * rate)
        endpoints.append({'word': chosen['id'], 'frames': [math.floor(Fraction(str(a)) * rate) + offset,
                                                           math.ceil(Fraction(str(b)) * rate) + offset], 'inside': inside})
    audio = sound.compare_audio(video, req, path, {'file': req['source'], 'sha256': req['source_sha256']}, rows)
    return entry(req, 'word-ends', {'words': endpoints, 'audio': audio['value']},
                 pixels_ok and all(w['inside'] for w in endpoints) and audio['ok'],
                 audio['evidence'] + '; first/last word occurrences mapped to exact output frames', rows[-1]['frames'][1])


def draft(video, req, path, pixels_ok):
    name = 'records/render/draft.json'
    record = records.read(video, name)
    if record is None:
        raise SpineError('draft-match: approved draft record missing')
    if 'picture_map' in req or 'picture_map' in record:
        raise SpineError('draft-match: split-clock master correspondence unsupported (seam 2g)')
    asset = {'file': record['file'], 'sha256': record['file_sha256']}
    if filehash(records.path(video, asset['file'])) != asset['sha256']:
        raise SpineError('draft-match: viewed draft bytes changed')
    evidence = records.read(video, record['readings'])
    if (filehash(records.path(video, record['readings'])) != record['readings_sha256']
            or evidence['key'] != record['key'] or evidence['file_sha256'] != asset['sha256']):
        raise SpineError('draft-match: stale decoded draft evidence')
    def identity(rows):
        fields = ('span', 'src', 'out', 'frames', 'source_frames', 'kind') + (('crop',) if req['output'] == 'long' else ())
        return [{**{k: r[k] for k in fields},
                 'asset_sha256': r['asset']['sha256']} for r in rows]
    complete = not req['output'].startswith('short:')
    same = record['request']['plan_sha256'] == req['plan_sha256']
    duration = req['frame_map'][-1]['out'][1]
    segments = []
    if complete:
        same = same and identity(record['frame_map']) == identity(req['frame_map']) and record['elements'] == req['elements']
        segments = [{'src': [0, duration], 'out': [0, duration]}]
    else:
        for row in req['frame_map']:
            matches = [r for r in record['frame_map'] if r['span'] == row['span'] and r['kind'] == row['kind']
                       and r['asset']['sha256'] == row['asset']['sha256']
                       and r['src'][0] <= row['src'][0] < row['src'][1] <= r['src'][1]]
            if len(matches) != 1:
                raise SpineError('draft-match: short needs one unambiguous approved-draft occurrence')
            chosen = matches[0]
            offset = Fraction(chosen['frames'][0]) / Fraction(req['settings']['video']['fps']) - Fraction(str(chosen['src'][0]))
            segments.append({'src': [float(Fraction(str(t)) + offset) for t in row['src']], 'out': row['out']})
    bound = {**req, 'inputs': {**req['inputs'], asset['file']: asset['sha256']}}
    count = req['frame_map'][-1]['frames'][1]
    if req['output'] == 'long':
        width, height = pixels.dimensions(records.path(video, asset['file']))
        reference = {**bound, 'frame_map': [{'span': 'viewed-draft', 'frames': [0, count], 'source_frames': [0, count],
                                            'kind': 'source', 'crop': [0, 0, width, height], 'asset': asset}]}
        picture = pixels.compare(video, reference, path)['checks']['frame-map']
    else:
        try:
            picture = evidence['checks']['frame-map']
            if picture['ok'] is not True or picture['value'] is None:
                raise ValueError('not passed')
        except (KeyError, TypeError, ValueError) as error:
            raise SpineError('draft-match: missing or failed draft frame-map reading') from error
    audio = sound.compare_audio(video, bound, path, asset, segments, complete_reference=complete)
    return entry(req, 'draft-match', {'mapping_equal': same, 'picture': picture['value'], 'audio': audio['value']},
                 same and pixels_ok and picture['ok'] and audio['ok'],
                 audio['evidence'] + '; approved source frames/crops and card timing', req['frame_map'][-1]['frames'][1])


def captions(video, req, duration):
    """Read actual sidecar bytes; a burn still requires decoded glyph/layer coverage."""
    from ..captions import layout
    binding = req['caption_binding']
    path = records.path(video, binding['record'])
    if filehash(path) != binding['sha256'] or req['inputs'][binding['record']] != binding['sha256']:
        raise SpineError('captions: missing or stale caption record')
    record = records.read(video, binding['record'])
    clock = [{k: row[k] for k in ('span', 'src', 'out')} for row in req['frame_map']]
    if (record['state'] != 'captioned' or record['output'] != req['output_id']
            or record['plan_sha256'] != req['plan_sha256'] or record['frame_map_sha256'] != digest(canon(clock))):
        raise SpineError('captions: stale output, plan or frame map')
    if (record['words_sha256'] != req['inputs'][record['words']]
            or filehash(records.path(video, record['words'])) != record['words_sha256']):
        raise SpineError('captions: stale word bytes')
    voice = records.path(video, record['voice'])
    if filehash(voice) != record['voice_sha256'] or req['inputs'][record['voice']] != record['voice_sha256']:
        raise SpineError('captions: stale voice binding')
    sidecar = record['sidecar']
    raw = records.path(video, sidecar['file']).read_bytes()
    if digest(raw) != sidecar['sha256'] or req['inputs'][sidecar['file']] != sidecar['sha256']:
        raise SpineError('captions: stale sidecar bytes')
    cues, edges, end = record['cues'], [], 0
    if not cues or raw != layout.srt(cues):
        raise SpineError('captions: sidecar differs from bound cues')
    for cue in cues:
        a, b = plan.interval([cue['start_s'], cue['end_s']], 'caption edges')
        if a < end or b > duration:
            raise SpineError('captions: sidecar edge outside decoded output or overlapping')
        edges.append([a, b])
        end = b
    if filehash(path) != binding['sha256']:
        raise SpineError('captions: changed while measuring')
    value = {'sidecar_sha256': sidecar['sha256'], 'edges_s': edges}
    if record['burn'] is not None:
        value['raster'] = raster.inspect(video, req, record)
    elif 'raster' in binding:
        raise SpineError('captions: raster without a bound burn')
    return entry(req, 'captions', value, True,
                 'actual sidecar bytes, cue edges, voice and frame-map identity', req['frame_map'][-1]['frames'][1])


def measure(video, req, path, result):
    """Missing coverage stays unavailable; present invalid evidence refuses with its own reading."""
    if 'frame_map' not in req:
        return
    label = 'frame-map'
    try:
        caption = None
        if 'caption_binding' in req:
            label = 'captions'
            caption = captions(video, req, result['duration_s'])
            result['checks'][label] = caption
        label = 'frame-map'
        mapping.validate(req)
        burned = caption is not None and 'raster' in caption['value']
        options = {'raster': records.path(video, req['caption_binding']['raster']['file'])} if burned else {}
        compared = pixels.compare(video, req, path, **options)
        result['checks'].update(compared['checks'])
        result['mapped_frames'] = compared['frame_checks']
        ok = compared['checks']['frame-map']['ok']
        if req['elements'] or any(c.get('kind') != 'cover' for c in req['cards_data']['cards']):
            result['checks']['cards'] = entry(req, 'cards', None, False, 'decoded non-cover layer coverage unavailable', 0)
        if req['settings']['shape'] == 'tall':
            label = 'subject'
            result['checks'][label] = subjects(req, ok)
        if req['output'].startswith('short:'):
            label = 'word-ends'
            result['checks'][label] = words(video, req, path, ok)
            context = req['selected']['stands_alone']
            if context.get('opens_on_subject') is True and not context.get('context'):
                result['checks']['context'] = entry(req, 'context', {'opens_on_subject': True}, ok,
                                                    'settled opening uses subject; no context layer declared', 1)
        if 'draft-match' in req['settings']['checks']:
            label = 'draft-match'
            result['checks'][label] = draft(video, req, path, ok)
        if 'voice_binding' in req:
            label = 'voice'
            duration = req['frame_map'][-1]['out'][1]
            result['checks'][label] = sound.compare_audio(video, req, path, req['voice_binding'],
                [{'src': [0, duration], 'out': [0, duration]}], complete_reference=True)
        if 'audio_binding' in req:
            label = 'bed'
            result['checks'].update(sound.measure(video, req, path))
        if burned:
            caption['ok'] = ok
            caption['result'] = 'pass' if ok else 'fail'
            caption['evidence'] += '; every delivered frame compared with bound source plus caption raster'
            if filehash(options['raster']) != req['caption_binding']['raster']['sha256']:
                decoded.refuse('captions', 'verification.render_checks', 'raster changed during comparison')
    except (KeyError, TypeError, IndexError, ValueError, SpineError) as error:
        from .render import MeasurementRefused
        if isinstance(error, MeasurementRefused):
            raise
        decoded.refuse(label, 'verification.render_checks', str(error))
