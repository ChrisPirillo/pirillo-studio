"""Constant-rate picture sampling, including approved rational expression speed.

See docs/rendering-source-rate.md for the clock and refusal flow.
"""
import math
from fractions import Fraction

from . import decoded, retiming


def rate(value):
    try:
        result = Fraction(value)
        if isinstance(value, bool) or result <= 0:
            raise ValueError('non-positive rate')
        return result
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        decoded.refuse('source rate', 'video.fps', 'missing or non-finite source rate')


def clock(raw, tolerance):
    """Prove the declared CFR against every decoded timestamp and duration."""
    if len(raw['streams']) != 1:
        decoded.refuse('source rate', 'video.fps', 'source has no unique video stream')
    stream = raw['streams'][0]
    fps = rate(stream.get('r_frame_rate'))
    average = rate(stream.get('avg_frame_rate'))
    if average != fps:
        decoded.refuse('source rate', 'video.fps', 'variable-frame-rate source')
    base = decoded.fraction(stream.get('time_base'), 'source rate', 'video.fps')
    tolerance = decoded.number(tolerance, 'source rate', 'measurement.grid_tolerance_s')
    frames = raw.get('frames')
    if tolerance < 0 or not frames:
        decoded.refuse('source rate', 'video.fps', 'decoded source clock missing or invalid tolerance')
    # A frame period of whole ticks (AVI, y4m: time base 1/fps) stamps exactly; otherwise one tick of quantization.
    exact = (1 / fps / base).denominator == 1
    slack = Fraction(str(tolerance)) if exact else max(Fraction(str(tolerance)), base)
    if 2 * slack >= 1 / fps:
        decoded.refuse('source rate', 'video.fps', 'time base too coarse to prove the source clock')
    points = [Fraction(str(decoded.number(f.get('pts'), 'decoded source clock', 'video.fps'))) * base
              for f in frames]
    durations = [Fraction(str(decoded.number(f.get('duration'), 'decoded source clock', 'video.fps'))) * base
                 for f in frames]
    steps = [b - a for a, b in zip(points, points[1:])]
    if steps and max(steps) - min(steps) > 2 * slack:
        decoded.refuse('source rate', 'video.fps', 'variable-frame-rate source decoded clock')
    for index, (point, duration) in enumerate(zip(points, durations)):
        if (duration <= 0 or (index and point <= points[index - 1])
                or abs(point - index / fps) > slack or abs(duration - 1 / fps) > slack):
            decoded.refuse('source rate', 'video.fps', f'decoded source clock disagrees with declared rate at frame {index}')
    return fps


def inspect(path, tolerance):
    raw = decoded.probe(path, ['-select_streams', 'v:0', '-show_streams', '-show_frames',
        '-show_entries', 'stream=codec_type,r_frame_rate,avg_frame_rate,time_base:frame=media_type,pts,duration'])
    return str(clock(raw, tolerance))


def interval(req, grid):
    """Source-frame extent touched by an interval on the source-seconds output grid."""
    ratio = rate(req['source_rate']) / Fraction(req['settings']['video']['fps'])
    a, b = grid
    return [int(a * ratio), int((b - 1) * ratio) + 1]


def bind(req, row):
    """Record every output frame's exact source index, including drops and repeats."""
    grid = row['source_frames'][:]
    ratio = rate(req['source_rate']) / Fraction(req['settings']['video']['fps'])
    speed = retiming.step(req, row)
    count = round((grid[1] - grid[0]) / speed)
    indices = [int((grid[0] + n * speed) * ratio) for n in range(count)]
    row.update(source_grid_frames=grid, source_indices=indices,
               source_frames=[indices[0], indices[-1] + 1])
    return row


def index(row, output):
    offset = output - row['frames'][0]
    return row['source_indices'][offset] if 'source_indices' in row else row['source_frames'][0] + offset


def validate(req, row):
    grid = row.get('source_grid_frames')
    indices = row.get('source_indices')
    count = row['frames'][1] - row['frames'][0]
    if (not isinstance(grid, list) or len(grid) != 2
            or any(type(n) is not int or n < 0 for n in grid)
            or round((grid[1] - grid[0]) / retiming.step(req, row)) != count
            or not isinstance(indices, list) or any(type(n) is not int for n in indices)):
        decoded.refuse('frame-map', 'video.fps', 'missing or invalid source-rate frame indices')
    wanted = bind(req, {'span': row['span'], 'source_frames': grid})
    if any(row.get(k) != wanted[k] for k in ('source_frames', 'source_indices')):
        decoded.refuse('frame-map', 'video.fps', 'source-rate frame indices differ from source clock')


def filters(req, row, *, seeked=False):
    """Stamp exact source instants, then hold the frame covering each output instant."""
    source, output = rate(req['source_rate']), Fraction(req['settings']['video']['fps'])
    speed = retiming.step(req, row)
    source *= speed
    origin = output * speed
    base = math.lcm(source.numerator, output.numerator, origin.numerator)
    a, b = row['source_frames']
    grid = row['source_grid_frames'][0]
    count = row['frames'][1] - row['frames'][0]
    # One tail frame lets fps emit the last sample's entire interval before trimming.
    return (f'trim=start_frame={0 if seeked else a}:end_frame={b - a if seeked else b},'
            f'settb=1/{base},setpts=(N+{a})*{base // source}-{grid}*{base // origin},'
            f'tpad=stop_mode=clone:stop=1,fps={output}:start_time=0:round=up,trim=end_frame={count}')
