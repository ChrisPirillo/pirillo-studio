"""Resolved renderer geometry on the settled clock; captions retain span/src/out in seconds.

prepare(request) supplies frame_map crops/assets, elements, and optional audio/caption bindings.
The core derives the edit independently; a renderer cannot change source selection or cover identity.
J-cuts, pushes, patches and retiming need separate picture/voice clocks and refuse until implemented.
"""
import copy
from fractions import Fraction

from ..cutting import plan
from ..spine.files import SpineError, canon


def timeline(req):
    """Materialize the locked intervals, including a repeated cold open, on one frame grid."""
    fps = Fraction(req['settings']['video']['fps'])
    settled, words = req['plan_data'], req['words_data']
    ordered = sorted(settled['spans'], key=lambda row: row['order'])
    selected = req['selected']
    pieces = []
    if selected is not None and selected.get('full') is not True:
        for src in selected['pieces']:
            owners = [s for s in ordered if s['src'][0] <= src[0] < src[1] <= s['src'][1]]
            if len(owners) != 1:
                raise SpineError('frame-map: short piece crosses a kept span or reinstates removed tape')
            pieces.append((owners[0], src))
    else:
        cold = settled['cold_open']
        if cold is not None:
            span = next(s for s in ordered if s['id'] == cold['span'])
            chosen = plan.selected(words, cold['words'])
            src = [chosen[0]['start'], chosen[-1]['end']]
            if not span['src'][0] <= src[0] < src[1] <= span['src'][1]:
                raise SpineError('frame-map: cold open is outside its kept span')
            pieces.append((span, src))
        pieces.extend((s, s['src']) for s in ordered)
    rows, end = [], 0
    for span, src in pieces:
        if span['enter'] in ('jcut', 'push') or 'patch' in span:
            raise SpineError(f"frame-map: {span['id']} needs unresolved picture/voice or moving-crop coverage")
        a, b = [round(Fraction(str(plan.number(t, 'frame-map src'))) * fps) for t in src]
        if a < 0 or b <= a:
            raise SpineError('frame-map: source interval collapses on the output grid')
        rows.append({'span': span['id'], 'src': [float(a / fps), float(b / fps)],
                     'out': [float(end / fps), float((end + b - a) / fps)],
                     'frames': [end, end + b - a], 'source_frames': [a, b]})
        end += b - a
    return rows


def rectangle(value, at):
    if not isinstance(value, list) or len(value) != 4:
        raise SpineError(f'{at}: expected [x,y,width,height]')
    x, y, w, h = [plan.number(n, at) for n in value]
    if min(x, y) < 0 or min(w, h) <= 0:
        raise SpineError(f'{at}: rectangle outside positive pixel coordinates')
    return x, y, w, h


def validate(req):
    """Compare all renderer rows with the request, never the renderer's own claimed edit."""
    expected = timeline(req)
    rows = req['frame_map']
    if len(rows) != len(expected):
        raise SpineError('frame-map: missing or extra planned pieces')
    spans = {s['id']: s for s in req['plan_data']['spans']}
    for row, wanted in zip(rows, expected):
        plan.interval(row['src'], 'frame-map src')
        plan.interval(row['out'], 'frame-map out')
        for key in ('span', 'src', 'out', 'frames'):
            if row[key] != wanted[key]:
                raise SpineError(f'frame-map: {key} differs from locked source order/edges/hold')
        rectangle(row['crop'], 'frame-map crop')
        asset = row['asset']
        if spans[row['span']].get('hide_picture') is True:
            cards = [c for c in req['cards_data']['cards']
                     if c.get('kind') == 'cover' and c.get('covers') == row['span']]
            if (len(cards) != 1 or row['kind'] != 'cover' or row['card'] != cards[0]['id']
                    or asset['sha256'] != req['inputs'][asset['file']]
                    or row['source_frames'] != [0, 1]):
                raise SpineError('cards: hidden span needs its exact opaque cover asset on every frame')
            if 'file' in cards[0] and asset['file'] != cards[0]['file']:
                raise SpineError('cards: resolved cover differs from locked asset')
        elif (row['kind'] != 'source' or row['source_frames'] != wanted['source_frames']
              or asset != {'file': req['source'], 'sha256': req['source_sha256']}):
            raise SpineError('frame-map: source asset or source frames differ from request')
    elements = req['elements']
    if not isinstance(elements, list):
        raise SpineError('elements: missing resolved layer list')
    for element in elements:
        rectangle(element['box'], 'elements box')
        a, b = plan.interval([element['start_s'], element['end_s']], 'elements')
        if a < 0 or b > rows[-1]['out'][1]:
            raise SpineError('elements: edge outside frame map')
        plan.text(element['what'], 'elements what')
    canon(req)


def bind(video, req, inputs):
    """Hash the renderer's resolved inputs before encoding; the same resolver runs at commit."""
    binding = video.prepare(copy.deepcopy(req))
    if not isinstance(binding, dict) or set(binding) - {
            'frame_map', 'elements', 'audio_binding', 'caption_binding'}:
        raise SpineError('frame-map: invalid renderer preparation result')
    resolved = copy.deepcopy(binding)
    identity = inputs.normalize(resolved)
    req.update(resolved, inputs=inputs.hashes)
    validate(req)
    return identity
