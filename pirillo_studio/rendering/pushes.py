"""Approved integer crops on the picture grid; flow and phase in docs/rendering.md."""
from fractions import Fraction

from ..cutting import plan
from ..spine.files import SpineError
from . import framing, pixels, source_rate


def frames(req, span, row, size):
    """Resolve endpoints once, retaining source-relative phase in repeated/selected pieces."""
    movement = plan.push(span)
    at = f"push: {span['id']}"
    if not isinstance(size, list) or len(size) != 2:
        raise SpineError(f'{at}: missing source_size')
    sw, sh = [pixels.integer(n, at + ' source_size', 1) for n in size]
    fps = Fraction(req['settings']['video']['fps'])
    origin, end = [round(Fraction(str(t)) * fps) for t in span['src']]
    grid = row.get('source_grid_frames', row['source_frames'])
    interval = [origin, max(end, grid[1])]
    if 'source_indices' in row:
        interval = source_rate.interval(req, interval)
    start = framing.crop(req, span, interval, size)
    finish = framing.crop(req, {**span, 'shot': movement['to']}, interval, size)
    ow, oh = req['settings']['video']['sizes'][req['settings']['shape']]
    if any(c[3] != round(c[2] * oh / ow) for c in (start, finish)):
        raise SpineError(f'{at}: endpoint aspect differs from output')
    duration = Fraction(str(movement['s'])) * fps
    resolved = []
    for output in range(*row['frames']):
        source = grid[0] + output - row['frames'][0]
        phase = min(Fraction(1), max(Fraction(0), (source - origin) / duration))
        x, y, w, _ = [round(a + (b - a) * phase) for a, b in zip(start, finish)]
        h = round(w * oh / ow)
        crop = [min(x, sw - w), min(y, sh - h), w, h]
        # Coalesce the held target so a long tail does not need one decoder per frame.
        if resolved and resolved[-1]['crop'] == crop:
            resolved[-1]['frames'][1] = output + 1
            resolved[-1]['source_frames'][1] = source + 1
        else:
            resolved.append({**row, 'frames': [output, output + 1], 'source_frames': [source, source + 1],
                             'crop': crop, 'source_size': size[:]})
    for item in resolved:
        item['src'] = [float(n / fps) for n in item['source_frames']]
        item['out'] = [float(n / fps) for n in item['frames']]
        if 'source_indices' in row:
            source_rate.bind(req, item)
    return resolved
