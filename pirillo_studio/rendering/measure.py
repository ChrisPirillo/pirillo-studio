"""Decoded readings for section 3's measuring state; never edit media or infer semantic coverage.

The lifecycle writes these readings with records.readings_name and commits their hash last.
Plan/subject/layer/stem correspondence requires the renderer's hash-bound preparation.
"""
from fractions import Fraction
from pathlib import Path

from ..spine.files import canon, filehash
from . import coverage as correspondence, decoded, records


def measure(video, req, path):
    try:
        return _measure(video, req, path)
    except (KeyError, TypeError, IndexError) as error:
        decoded.refuse(str(error), 'measurement', f'missing or invalid decoded input: {error}')


def inputs(video, req):
    for name, sha in req['inputs'].items():
        if not sha or filehash(records.path(video, name)) != sha:
            decoded.refuse('inputs.' + name, 'verification.complete_decode', f'{name}: missing or stale input bytes')


def _measure(video, req, path):
    """Return every measured value and explicit unavailable checks; the lifecycle judges required checks."""
    setting = req['settings']
    try:
        canon(setting)
    except ValueError as error:
        decoded.refuse('settings', 'measurement', error)
    policy = setting['measurement']
    for key, value in policy.items():
        decoded.number(value, key, 'measurement.' + key)
    for group, keys in [('audio', ('sample_rate', 'loudness_min_lufs', 'loudness_max_lufs', 'true_peak_max_dbtp')),
                        ('video', ('max_bytes',))]:
        for key in keys:
            decoded.number(setting[group][key], group + '.' + key, group + '.' + key)
    name = Path(path).relative_to(video.folder).as_posix()
    path = records.path(video, name)
    before = filehash(path)
    if before is None:
        decoded.refuse('file_sha256', 'verification.complete_decode', f'{name}: media missing')
    current_tools = decoded.tools()
    for tool, version in current_tools.items():
        if req['tools'].get(tool) != version:
            decoded.refuse('tools.' + tool, 'verification.tools', f'{tool}: missing or stale tool version')
    if not req.get('key'):
        decoded.refuse('key', 'verification.complete_decode', 'missing measurement request identity')
    inputs(video, req)
    streams, packets = decoded.streams(path)
    counts = {kind: sum(row['kind'] == kind for row in streams) for kind in ('video', 'audio')}
    if counts != setting['verification']['streams']:
        decoded.refuse('streams', 'verification.streams', f'unsupported stream counts: {counts}')
    picture = next(row for row in streams if row['kind'] == 'video')
    sound = next(row for row in streams if row['kind'] == 'audio')
    frames, audio, silence = decoded.signals(path, policy)
    result = {'key': req['key'], 'file_sha256': before, 'settings': setting, 'tools': current_tools,
              'duration_s': max(row['decoded_end_s'] for row in streams) -
                            min(row['decoded_start_s'] for row in streams),
              'size_bytes': path.stat().st_size, 'streams': streams, 'signals': frames, **audio,
              'checks': {}, 'frame_checks': []}
    coverage = {'frames': picture['decoded_frames'], 'start_s': picture['decoded_start_s'],
                'end_s': picture['decoded_end_s']}

    def check(label, value, ok, reference, evidence, *, covered=coverage):
        result['checks'][label] = {'ok': ok, 'result': 'unavailable' if value is None else 'pass' if ok else 'fail',
                                   'value': value, 'setting': 'rendering.settings#' + reference,
                                   'coverage': covered, 'evidence': before + ': ' + evidence,
                                   'file': req['file'], 'actor': 'rendering'}

    count = len(picture['decoded'])
    check('decode', {'ffprobe_frames': count, 'ffmpeg_frames': len(frames)},
          len(frames) == count and [row['index'] for row in frames] == list(range(count)),
          'verification.complete_decode', 'complete ffprobe decode and ffmpeg signalstats/ebur128 decode')
    expected_count = picture['metadata'].get('nb_frames')
    check('count', {'decoded': count, 'declared': expected_count},
          expected_count is not None and count == decoded.number(expected_count, 'count', 'verification.complete_decode'),
          'verification.complete_decode', 'decoded frames compared with declared stream count')
    fps = decoded.fraction(setting['video']['fps'], 'fps', 'video.fps')
    time_base = decoded.fraction(picture['metadata'].get('time_base'), 'video.time_base', 'measurement.grid_tolerance_s')
    pts = sorted(Fraction(str(decoded.number(row.get('pts'), 'packet.pts', 'video.fps'))) * time_base
                 for row in packets)
    measured_fps = Fraction(len(pts) - 1, 1) / (pts[-1] - pts[0]) if len(pts) > 1 and pts[-1] > pts[0] else None
    result['fps'] = None if measured_fps is None else f'{measured_fps.numerator}/{measured_fps.denominator}'
    check('fps', result['fps'], measured_fps is not None and measured_fps == fps,
          'video.fps', 'video packet presentation timestamps, not header rate')
    points = picture['frames']
    tolerance = policy['grid_tolerance_s']
    off_grid = [i for i, row in enumerate(points)
                if abs(row['start_s'] - float(i / fps)) > tolerance or
                abs(row['end_s'] - row['start_s'] - float(1 / fps)) > tolerance]
    packet_mismatch = len(pts) != count or any(abs(float(p) - row['start_s']) > tolerance
                                               for p, row in zip(pts, points))
    signal_mismatch = len(frames) != count or any(abs(f['pts_s'] - row['start_s']) > tolerance
                                                 for f, row in zip(frames, points))
    check('clock', {'off_grid_frames': off_grid, 'packet_mismatch': packet_mismatch,
                    'signal_mismatch': signal_mismatch, 'packet_pts_s': [float(p) for p in pts]},
          not off_grid and not packet_mismatch and not signal_mismatch,
          'measurement.grid_tolerance_s', 'every decoded PTS/duration and packet PTS on the requested frame grid')
    dimensions = setting['video']['sizes'][setting['shape']]
    observed = [[row.get('width'), row.get('height')] for row in picture['decoded']]
    check('size', observed, all(size == dimensions for size in observed),
          'video.sizes.' + setting['shape'], 'dimensions on every decoded video frame')
    codecs = {row['kind']: row['codec'] for row in streams}
    check('codec', codecs, all(codecs[kind] == setting[kind]['codec'] for kind in codecs),
          'video.codec + rendering.settings#audio.codec', 'actual stream codecs')
    color = {key: [row.get(key) for row in picture['decoded']] for key in
             ('pix_fmt', 'sample_aspect_ratio', 'color_range', 'color_space', 'color_primaries', 'color_transfer')}
    check('color', color, all(all(value is not None and value == setting['video'][key] for value in values)
                             for key, values in color.items()), 'video', 'color, pixel format and SAR on every decoded frame')
    check('file_cap', result['size_bytes'], result['size_bytes'] <= setting['video']['max_bytes'],
          'video.max_bytes', 'actual media bytes')
    check('sample_rate', sound['metadata'].get('sample_rate'),
          decoded.number(sound['metadata'].get('sample_rate'), 'sample_rate', 'audio.sample_rate') ==
          setting['audio']['sample_rate'], 'audio.sample_rate', 'decoded audio stream sample rate')
    for label, lower, upper in [('loudness_lufs', setting['audio']['loudness_min_lufs'], setting['audio']['loudness_max_lufs']),
                                 ('true_peak_dbtp', None, setting['audio']['true_peak_max_dbtp'])]:
        reference = 'audio.loudness_min_lufs + rendering.settings#audio.loudness_max_lufs' if lower is not None else 'audio.true_peak_max_dbtp'
        check(label, audio[label], (lower is None or audio[label] >= lower) and audio[label] <= upper,
              reference, 'ebur128 integrated loudness and oversampled true peak of the finished mix', covered={
                  'frames': sound['decoded_frames'], 'start_s': sound['decoded_start_s'], 'end_s': sound['decoded_end_s']})
    gaps = {row['kind']: [[a['end_s'], b['start_s']] for a, b in zip(row['frames'], row['frames'][1:])
                         if abs(b['start_s'] - a['end_s']) > policy['gap_tolerance_s']] for row in streams}
    offsets = {edge: sound['decoded_' + edge + '_s'] - picture['decoded_' + edge + '_s'] for edge in ('start', 'end')}
    offsets['drift'] = offsets['end'] - offsets['start']
    duration_errors = {row['kind']: abs(row['decoded_end_s'] - row['end_s']) for row in streams}
    check('av', {'gaps': gaps, 'offsets_s': offsets, 'duration_errors_s': duration_errors},
          not any(gaps.values()) and all(abs(value) <= policy['av_tolerance_s'] for value in offsets.values())
          and all(value <= policy['av_tolerance_s'] for value in duration_errors.values())
          and all(abs(row['decoded_start_s']) <= policy['start_tolerance_s'] and
                  abs(row['start_s']) <= policy['start_tolerance_s'] for row in streams),
          'measurement.av_tolerance_s + rendering.settings#measurement.gap_tolerance_s + rendering.settings#measurement.start_tolerance_s',
          'decoded sample/frame intervals and each declared stream clock')
    tail_start = len(frames) - 1
    while tail_start > 0 and all(frames[tail_start][key] <= policy['held_difference'] for key in ('YDIF', 'UDIF', 'VDIF')):
        tail_start -= 1
    tail_s = float((len(frames) - tail_start) / fps)
    check('held_tail', {'start_frame': tail_start, 'duration_s': tail_s}, tail_s <= policy['held_tail_max_s'],
          'measurement.held_tail_max_s + rendering.settings#measurement.held_difference', 'decoded full-frame Y/U/V differences')
    flashes = [i for i in range(1, len(frames) - 1)
               if abs(frames[i]['YAVG'] - frames[i-1]['YAVG']) >= policy['flash_delta'] and
               abs(frames[i]['YAVG'] - frames[i+1]['YAVG']) >= policy['flash_delta'] and
               abs(frames[i-1]['YAVG'] - frames[i+1]['YAVG']) <= policy['flash_return_delta']]
    check('flash', flashes, not flashes, 'measurement.flash_delta + rendering.settings#measurement.flash_return_delta',
          'isolated luma impulses in decoded frames; not semantic shot approval')
    check('silence', silence, not silence, 'measurement.silence_db + rendering.settings#measurement.silence_min_s',
          'silencedetect on the finished mix; no tacet is inferred')
    for label in ('frame-map', 'draft-match', 'cards', 'context', 'captions', 'subject', 'word-ends', 'bed'):
        check(label, None, False, 'verification.checks', 'no decoded correspondence binding for ' + label,
              covered={'frames': 0, 'unseen': [[coverage['start_s'], coverage['end_s']]]})
    correspondence.measure(video, req, path, result)
    for label in setting['checks']:
        if label not in result['checks']:
            check(label, None, False, 'verification.checks', 'unknown measurement ' + label, covered={'frames': 0})
    result['frame_checks'] = ['decode', 'count', 'clock', 'size', 'color', 'held_tail', 'flash'] + [
        name for name in ('frame-map', 'cards', 'subject', 'word-ends', 'draft-match', 'captions')
        if result['checks'][name]['value'] is not None]
    if filehash(records.path(video, name)) != before:
        decoded.refuse('file_sha256', 'verification.complete_decode', 'media changed during measurement')
    inputs(video, req)
    if decoded.tools() != current_tools:
        decoded.refuse('tools', 'verification.tools', 'tools changed during measurement')
    canon(result)
    return result
