"""Local decoders only; command failures and absent readings retain their names."""
import json
import math
import re
import shutil
import subprocess
from fractions import Fraction

from .render import MeasurementRefused


def refuse(reading, setting, error):
    raise MeasurementRefused(reading, 'rendering.settings#' + setting, error)


def number(value, reading, setting):
    try:
        result = float(value)
        if isinstance(value, bool) or not math.isfinite(result):
            raise ValueError('non-finite or boolean')
        return result
    except (ValueError, TypeError):
        refuse(reading, setting, f'{reading}: missing or invalid value {value!r}')


def fraction(value, reading, setting):
    try:
        result = Fraction(value)
        if isinstance(value, bool) or result <= 0:
            raise ValueError('non-positive fraction')
        return result
    except (ValueError, TypeError, ZeroDivisionError):
        refuse(reading, setting, f'{reading}: missing or invalid fraction {value!r}')


def run(tool, args, reading):
    executable = shutil.which(tool)
    if executable is None:
        refuse(reading, 'verification.tools', f'{reading}: required tool {tool} unavailable')
    try:
        result = subprocess.run([executable, '-nostdin', *args] if tool == 'ffmpeg' else
                                [executable, *args], capture_output=True, text=True)
    except OSError as error:
        refuse(reading, 'verification.tools', f'{tool}: {error}')
    if result.returncode:
        refuse(reading, 'verification.tools', f'{tool} {reading}: {result.stderr.strip()}')
    return result


def tools():
    versions = {}
    for tool in ('ffmpeg', 'ffprobe'):
        lines = run(tool, ['-version'], 'tools.' + tool).stdout.splitlines()
        if not lines or not lines[0].startswith(tool + ' version '):
            refuse('tools.' + tool, 'verification.tools', f'{tool}: unknown tool version')
        versions[tool] = lines[0]
    return versions


def probe(path, entries):
    result = run('ffprobe', ['-v', 'error', '-err_detect', 'explode', *entries,
                           '-of', 'json', str(path)], 'decode')
    if result.stderr.strip():
        refuse('decode', 'verification.complete_decode', result.stderr.strip())
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        refuse('decode', 'verification.complete_decode', error)


def streams(path):
    raw = probe(path, ['-show_streams', '-show_frames'])
    output = []
    for stream in raw['streams']:
        kind, index = stream['codec_type'], stream['index']
        if kind not in ('video', 'audio'):
            refuse('streams', 'verification.streams', f'unsupported stream {index}: {kind}')
        rows = [row for row in raw['frames'] if row['stream_index'] == index]
        base = fraction(stream.get('time_base'), kind + '.time_base', 'measurement.grid_tolerance_s')
        points = []
        for row in rows:
            start = number(row.get('pts'), f'{kind}.pts', 'measurement.grid_tolerance_s') * base
            duration = (number(row.get('nb_samples'), 'audio.samples', 'audio.sample_rate') /
                        number(stream.get('sample_rate'), 'audio.sample_rate', 'audio.sample_rate')
                        if kind == 'audio' else number(row.get('duration'), 'video.duration',
                                                       'measurement.grid_tolerance_s') * base)
            if duration <= 0:
                refuse(kind + '.duration', 'measurement.grid_tolerance_s', 'non-positive decoded duration')
            points.append({'start_s': float(start), 'end_s': float(start + duration)})
        if not points:
            refuse(kind + '.frames', 'verification.complete_decode', 'no decoded frames')
        start = number(stream.get('start_time'), kind + '.start', 'measurement.start_tolerance_s')
        duration = number(stream.get('duration'), kind + '.duration', 'measurement.av_tolerance_s')
        output.append({'index': index, 'kind': kind, 'codec': stream.get('codec_name'),
                       'start_s': start, 'end_s': start + duration, 'duration_s': duration,
                       'decoded_start_s': points[0]['start_s'], 'decoded_end_s': points[-1]['end_s'],
                       'decoded_frames': len(rows), 'frames': points, 'metadata': stream, 'decoded': rows})
    packets = probe(path, ['-select_streams', 'v:0', '-show_packets'])['packets']
    return output, packets


def signals(path, policy):
    noise, duration = policy['silence_db'], policy['silence_min_s']
    filters = ('[0:v:0]signalstats,metadata=mode=print:file=-[v];'
               f'[0:a:0]ebur128=peak=true,silencedetect=noise={noise}dB:d={duration}[a]')
    result = run('ffmpeg', ['-v', 'info', '-xerror', '-err_detect', 'explode', '-copyts', '-i', str(path),
                          '-filter_complex', filters, '-map', '[v]', '-map', '[a]',
                          '-fps_mode', 'passthrough', '-f', 'null', '-'], 'decode')
    frames = []
    for line in result.stdout.splitlines():
        if line.startswith('frame:'):
            match = re.fullmatch(r'frame:(\d+)\s+pts:\s*(-?\d+)\s+pts_time:(\S+)\s*', line)
            if not match:
                refuse('decode', 'verification.complete_decode', 'unreadable signalstats frame')
            frames.append({'index': int(match[1]), 'pts_s': number(match[3], 'video.pts',
                                                                 'measurement.grid_tolerance_s')})
        elif line.startswith('lavfi.signalstats.'):
            key, value = line.split('=', 1)
            if key.rsplit('.', 1)[-1] in ('YAVG', 'YDIF', 'UDIF', 'VDIF'):
                frames[-1][key.rsplit('.', 1)[-1]] = number(value, key, 'measurement.flash_delta')
    loudness = re.findall(r'I:\s+(\S+) LUFS', result.stderr)
    peak = re.findall(r'Peak:\s+(\S+) dBFS', result.stderr)
    audio = {'loudness_lufs': number(loudness[-1] if loudness else None, 'loudness_lufs', 'audio.loudness_min_lufs'),
             'true_peak_dbtp': number(peak[-1] if peak else None, 'true_peak_dbtp', 'audio.true_peak_max_dbtp')}
    intervals, opened = [], None
    for kind, value in re.findall(r'silence_(start|end):\s*(\S+)', result.stderr):
        point = number(value, 'silence', 'measurement.silence_min_s')
        if kind == 'start':
            if opened is not None:
                refuse('silence', 'measurement.silence_min_s', 'unclosed silence interval')
            opened = point
        else:
            if opened is None:
                refuse('silence', 'measurement.silence_min_s', 'missing silence start')
            intervals.append([opened, point])
            opened = None
    if opened is not None:
        refuse('silence', 'measurement.silence_min_s', 'missing silence end')
    return frames, audio, intervals
