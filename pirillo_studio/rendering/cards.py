"""Wide anchored straight-alpha layers; see docs/rendering-cards.md's flowchart."""
import math
from fractions import Fraction

from ..cutting import plan
from ..spine.files import SpineError, filehash
from . import clocks, mapping, raster, records


def layers(req):
    return [card for card in req['cards_data']['cards'] if card.get('kind') != 'cover']


def inspect(video, req):
    """Resolve word occurrences independently of supplied elements and decode their rasters."""
    cards = layers(req)
    if not cards:
        return []
    if req['settings']['shape'] != 'wide':
        raise SpineError('cards: native tall cards unsupported (seam 2h)')
    voice, _ = clocks.split(req, mapping.pieces(req))
    fps = Fraction(req['settings']['video']['fps'])
    spans = {s['id']: s for s in req['plan_data']['spans']}
    found, ids = [], set()
    for card in cards:
        ident = plan.text(card.get('id'), 'cards id')
        if ident in ids:
            raise SpineError(f'cards: duplicate id {ident}')
        ids.add(ident)
        if card.get('kind') not in ('instruction', 'context'):
            raise SpineError(f'cards: {ident}: unsupported kind')
        if set(card) - {'id', 'kind', 'text', 'anchor', 'raster'}:
            raise SpineError(f'cards: {ident}: unsupported timing or layout fields')
        plan.text(card.get('text'), 'cards text')
        words = plan.selected(req['words_data'], card.get('anchor'))
        a, b = [Fraction(str(plan.number(t, 'cards anchor'))) * fps
                for t in (words[0]['start'], words[-1]['end'])]
        edges = []
        for row in voice:
            span = spans[row['span']]
            # Patch-local and silent retimed clocks cannot locate source-word anchors.
            if any(k in span for k in ('patch', 'speed', 'retime')):
                if words[0]['start'] < span['src'][1] and words[-1]['end'] > span['src'][0]:
                    raise SpineError(f'cards: {ident}: patch/retimed anchor unsupported')
                continue
            start, end = row['source_frames']
            if a >= end or b <= start:
                continue
            if not start <= a < b <= end:
                raise SpineError(f'cards: {ident}: anchor crosses kept piece')
            offset = row['frames'][0] - start
            edge = [math.ceil(t) + offset for t in (a, b)]
            if edge[0] >= edge[1]:
                raise SpineError(f'cards: {ident}: anchor collapses on output grid')
            edges.append(edge)
        if not edges:
            raise SpineError(f'cards: {ident}: anchor has no kept occurrence')
        asset = card.get('raster')
        if (not isinstance(asset, dict) or not isinstance(asset.get('file'), str)
                or not asset.get('sha256')):
            raise SpineError(f'cards: {ident}: raster missing file or content hash')
        if (req['inputs'].get(asset['file']) != asset['sha256']
                or filehash(records.path(video, asset['file'])) != asset['sha256']):
            raise SpineError(f'cards: {ident}: stale or unbound raster')
        value = raster.inspect_alpha(video, {**req, 'frame_map': voice}, asset,
                                     req['settings'].get('cards_safe'), edges, 'cards')
        found.append({'id': ident, 'kind': card['kind'], **value})
    return found


def elements(req, inspected):
    fps = Fraction(req['settings']['video']['fps'])
    return [{'what': card['id'], 'start_s': float(bound['frames'][0] / fps),
             'end_s': float(bound['frames'][1] / fps), 'box': bound['box']}
            for card in inspected for bound in card['bounds'] if bound['box'] is not None]


def prepare(video, req):
    return elements(req, inspect(video, req))
