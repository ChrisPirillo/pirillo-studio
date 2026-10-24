"""Fixed source-pixel shot crops; resolution/refusal flow lives in docs/rendering.md."""
import math

from ..cutting import plan
from ..spine.files import SpineError
from . import mapping, pixels


def crop(req, span, source_frames, size):
    """Fit the union across the actual picture interval, never widening the requested shot."""
    shot = span['shot']
    at = f"framing: {span['id']}: {shot}"
    sw, sh = size
    if shot == 'wide':
        return [0, 0, sw, sh]
    if shot not in ('med', 'close'):
        raise SpineError(f'{at}: unsupported shot; landmark geometry unavailable')
    video = req['settings']['video']
    fractions = video.get('shot_fractions')
    if not isinstance(fractions, dict) or shot not in fractions:
        raise SpineError(f'{at}: missing size setting video.shot_fractions.{shot}')
    fraction = plan.number(fractions[shot], at + ' size')
    if not 0 < fraction <= 1:
        raise SpineError(f'{at}: size must be positive and no larger than the source')
    ow, oh = video['sizes'][req['settings']['shape']]
    ow, oh = [pixels.integer(n, at + ' output size', 1) for n in (ow, oh)]
    w = round(sw * fraction)
    h = round(w * oh / ow)
    if min(w, h) <= 0 or w > sw or h > sh:
        raise SpineError(f'{at}: size is empty or larger than the source on the output aspect')
    observations = req['faces_data'].get('frames')
    if not isinstance(observations, list):
        raise SpineError(f'{at}: missing subject observations')
    a, b = source_frames
    seen, boxes = set(), []
    for row in observations:
        frame = pixels.integer(row['frame'], at + ' subject frame')
        if not a <= frame < b:
            continue
        if frame in seen:
            raise SpineError(f'{at}: duplicate subject observation at frame {frame}')
        seen.add(frame)
        if not isinstance(row['boxes'], list) or not row['boxes']:
            raise SpineError(f'{at}: empty subject observation at frame {frame}')
        boxes.extend(mapping.rectangle(box, at + ' subject box') for box in row['boxes'])
    for frame in range(a, b):
        if frame not in seen:
            raise SpineError(f'{at}: missing subject observation at frame {frame}')
    if not boxes:
        raise SpineError(f'{at}: empty subject observations')
    left, top = min(x for x, y, bw, bh in boxes), min(y for x, y, bw, bh in boxes)
    right = max(x + bw for x, y, bw, bh in boxes)
    bottom = max(y + bh for x, y, bw, bh in boxes)
    # Integer containment can be stricter than float width when the subject touches both edges.
    xmin, xmax = max(0, math.ceil(right - w)), min(sw - w, math.floor(left))
    ymin, ymax = max(0, math.ceil(bottom - h)), min(sh - h, math.floor(top))
    if xmin > xmax or ymin > ymax:
        raise SpineError(f'{at}: subject does not fit requested size inside the source')
    x = min(max(round((left + right - w) / 2), xmin), xmax)
    y = min(max(round((top + bottom - h) / 2), ymin), ymax)
    return [x, y, w, h]
