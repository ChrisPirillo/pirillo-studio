"""Map split voice and picture clocks to the same approved draft occurrences.

The read-only measurement flow is in docs/rendering-correspondence.md.
"""
from fractions import Fraction

from ..spine.files import SpineError
from . import mapping, source_rate


def picture_frames(req, crops):
    """Crop runs may differ by output shape; their decoded source timeline may not."""
    frames = []
    for row in mapping.pictures(req):
        for index in range(*row['frames']):
            source = row['source_frames'][0] if row['kind'] == 'cover' else source_rate.index(row, index)
            frames.append((row['span'], row['asset']['sha256'], row['kind'], row.get('card'), source,
                           row['crop'] if crops else None))
    return frames


def split(req, record):
    """Return mapping agreement and audio segments on the approved draft's frame grid."""
    prior = record['request']
    if prior['key'] != record['key']:
        raise SpineError('draft-match: draft request identity differs from its record')
    if 'picture_map' not in req or 'picture_map' not in prior:
        raise SpineError('draft-match: combined and split clocks cannot be mixed')
    for key in ('frame_map', 'picture_map', 'elements'):
        if record[key] != prior[key]:
            raise SpineError(f'draft-match: draft {key} differs from its measured request')
    mapping.validate(prior)
    mapping.validate(req)
    fps = Fraction(req['settings']['video']['fps'])
    if fps != Fraction(prior['settings']['video']['fps']):
        raise SpineError('draft-match: draft and master frame rates differ')
    complete = not req['output'].startswith('short:')
    same = prior['plan_sha256'] == req['plan_sha256']
    rows, approved = req['frame_map'], prior['frame_map']
    if complete:
        def identity(row):
            return ({k: row[k] for k in ('span', 'src', 'out', 'frames', 'source_frames')},
                    row['asset']['sha256'], row.get('silent', False))
        same = same and list(map(identity, rows)) == list(map(identity, approved)) and req['elements'] == prior['elements']
        intervals = [(0, rows[-1]['frames'][1])]
    else:
        intervals = []
        for row in rows:
            if row.get('silent'):
                raise SpineError('draft-match: silent retimed short correspondence unsupported')
            a, b = row['source_frames']
            matches = [r for r in approved if r['span'] == row['span'] and not r.get('silent')
                       and r['asset']['sha256'] == row['asset']['sha256']
                       and r['source_frames'][0] <= a < b <= r['source_frames'][1]]
            if len(matches) != 1:
                raise SpineError('draft-match: short needs one unambiguous approved-draft occurrence')
            chosen = matches[0]
            start = chosen['frames'][0] + a - chosen['source_frames'][0]
            intervals.append((start, start + b - a))
    pictures = picture_frames(prior, req['output'] == 'long')
    expected = [frame for a, b in intervals for frame in pictures[a:b]]
    same = same and expected == picture_frames(req, req['output'] == 'long')
    segments, end = [], 0
    for a, b in intervals:
        count = b - a
        segments.append({'src': [float(a / fps), float(b / fps)],
                         'out': [float(end / fps), float((end + count) / fps)]})
        end += count
    return same, segments
