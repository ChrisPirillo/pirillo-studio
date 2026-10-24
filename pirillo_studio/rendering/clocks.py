"""Independent voice and picture clocks for whole patches and fixed-grid J-cuts."""
import copy
from fractions import Fraction

from ..cutting import plan
from ..spine.files import SpineError


def split(req, pieces):
    fps = Fraction(req['settings']['video']['fps'])
    voice, picture, end = [], [], 0
    source = {'file': req['source'], 'sha256': req['source_sha256']}
    for span, src in pieces:
        sid = span['id']
        if span['enter'] == 'push' or any(k in span for k in ('retime', 'speed', 'grade')):
            raise SpineError(f'frame-map: {sid}: pushes, retiming and grades unsupported')
        asset = source
        a, b = [round(Fraction(str(plan.number(t, 'frame-map src'))) * fps) for t in src]
        tape = [a, b]
        if 'patch' in span:
            if src != span['src']:
                raise SpineError(f'patch: {sid}: partial patch selection unsupported')
            patch = req['patches'][span['patch']['record']]
            duration = Fraction(str(plan.number(patch['duration_s'], 'patch duration')))
            rate = plan.number(patch['sample_rate'], 'patch sample rate')
            count = plan.number(patch['sample_count'], 'patch sample count')
            if (duration <= 0 or rate != req['settings']['audio']['sample_rate'] or count <= 0
                    or type(patch['sample_count']) is not int or duration * rate != count
                    or (duration * fps).denominator != 1):
                raise SpineError(f'patch: {sid}: duration/sample count/rate or frame grid differs')
            paths = [patch[k] for k in ('audio', 'file', 'path') if k in patch]
            if len(paths) != 1 or not isinstance(paths[0], str):
                raise SpineError(f'patch: {sid}: ambiguous audio asset')
            asset = {'file': paths[0], 'sha256': req['inputs'][paths[0]]}
            a, b = 0, int(duration * fps)
            if span.get('hide_picture') is not True:
                raise SpineError(f'patch: {sid}: opaque cover required')
        if a < 0 or b <= a:
            raise SpineError('frame-map: source interval collapses on the output grid')
        row = {'span': sid, 'frames': [end, end + b - a], 'source_frames': [a, b]}
        voice.append({**row, 'asset': copy.deepcopy(asset)})
        visual = copy.deepcopy(row)
        if 'patch' in span:
            visual['source_frames'] = tape
        if span['enter'] == 'jcut':
            lead = round(Fraction(str(plan.number(span['lead_s'], 'J-cut lead'))) * fps)
            ordered = sorted(req['plan_data']['spans'], key=lambda s: s['order'])
            previous = ordered[span['order'] - 2] if span['order'] > 1 else None
            if (not picture or previous is None or picture[-1]['span'] != previous['id']
                    or previous.get('hide_picture') or span.get('hide_picture') or 'patch' in span
                    or picture[-1]['source_frames'][1] != round(Fraction(str(previous['src'][1])) * fps)
                    or src != span['src']
                    or lead <= 0 or lead >= b - a):
                raise SpineError(f'J-cut: {sid}: unsupported outgoing picture or partial selection')
            picture[-1]['source_frames'][1] += lead
            picture[-1]['frames'][1] += lead
            visual['source_frames'][0] += lead
            visual['frames'][0] += lead
        picture.append(visual)
        end += b - a
    for row in voice + picture:
        row['src'] = [float(frame / fps) for frame in row['source_frames']]
        row['out'] = [float(frame / fps) for frame in row['frames']]
    return voice, picture
