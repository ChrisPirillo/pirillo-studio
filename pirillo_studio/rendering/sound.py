"""Bound-bed evidence from actual finished audio, using normalized speech-band PCM."""
import array
import math
import sys
import tempfile
from pathlib import Path

from ..spine.files import filehash
from . import decoded, records
from .render import MeasurementRefused


def _refuse(detail):
    decoded.refuse('bed', 'measurement', detail)


def _number(value):
    return decoded.number(value, 'bed', 'measurement')


def _db(rms):
    return 20 * math.log10(rms) if rms > 0 else None


def _pcm(source, destination, policy):
    # Absolute Hz cutoff filters avoid the historical width-unit error in the speech band.
    rate = int(policy['bed_sample_rate'])
    filters = (f'aresample={rate}:async=1:first_pts=0,'
               f"highpass=f={policy['bed_band_low_hz']},"
               f"lowpass=f={policy['bed_band_high_hz']}")
    decoded.run('ffmpeg', ['-v', 'error', '-err_detect', 'explode', '-i', str(source),
                '-map', '0:a:0', '-vn', '-af', filters, '-ac', '1', '-ar', str(rate),
                '-c:a', 'pcm_f32le', '-f', 'f32le', str(destination)], 'bed')


def _samples(handle, count):
    values = array.array('f')
    values.frombytes(handle.read(count * 4))
    if sys.byteorder != 'little':
        values.byteswap()
    if not all(math.isfinite(value) for value in values):
        _refuse('bed: non-finite decoded PCM')
    return values


def measure(video, req, path):
    """Return a bed check; unavailable or stale binding refuses with the bed reading name."""
    try:
        return _measure(video, req, path)
    except MeasurementRefused:
        raise
    except (KeyError, TypeError, ValueError, OSError, OverflowError) as error:
        _refuse(f'bed: missing or invalid input: {error}')


def _measure(video, req, path):
    binding = req['audio_binding']
    stem = binding['stem']
    stem_path = records.path(video, stem['file'])
    target_name = Path(path).relative_to(video.folder).as_posix()
    target = records.path(video, target_name)
    hashes = {stem['file']: filehash(stem_path), target_name: filehash(target)}
    if stem_path == target:
        _refuse('bed: stem must be independent of delivered mix')
    if not hashes[stem['file']] or hashes[stem['file']] != stem['sha256'] or req['inputs'].get(stem['file']) != stem['sha256']:
        _refuse('bed: missing, stale or unbound stem hash')
    if not hashes[target_name]:
        _refuse('bed: delivered mix missing')
    offset, gain = _number(binding['offset_s']), _number(binding['gain_db'])
    policy = req['settings']['measurement']
    keys = ('bed_sample_rate', 'bed_band_low_hz', 'bed_band_high_hz', 'bed_window_s',
            'bed_gain_tolerance_db', 'bed_correlation_min', 'bed_stem_floor_dbfs')
    p = {key: _number(policy[key]) for key in keys}
    if (p['bed_sample_rate'] != int(p['bed_sample_rate']) or
        not 0 < p['bed_band_low_hz'] < p['bed_band_high_hz'] < p['bed_sample_rate'] / 2 or
        p['bed_window_s'] <= 0 or p['bed_gain_tolerance_db'] < 0 or
        not 0 < p['bed_correlation_min'] <= 1 or p['bed_stem_floor_dbfs'] >= 0):
        _refuse('bed: invalid speech-band measurement settings')
    tacets = [tuple(_number(x) for x in pair) for pair in binding['tacets']]
    if any(len(pair) != 2 or not 0 <= pair[0] < pair[1] for pair in tacets):
        _refuse('bed: invalid tacet interval')
    rate = int(p['bed_sample_rate'])
    window = round(rate * p['bed_window_s'])
    if window < 2:
        _refuse('bed: measurement window has fewer than two samples')
    shift = round(offset * rate)
    expected_gain = 10 ** (gain / 20)
    if not math.isfinite(expected_gain) or expected_gain <= 0:
        _refuse('bed: invalid linear gain')
    rows = []
    with tempfile.TemporaryDirectory(prefix='render-bed-') as temporary:
        mix_pcm, stem_pcm = (Path(temporary) / name for name in ('mix.raw', 'stem.raw'))
        _pcm(target, mix_pcm, p)
        _pcm(stem_path, stem_pcm, p)
        count = mix_pcm.stat().st_size // 4
        stem_count = stem_pcm.stat().st_size // 4
        if not count:
            _refuse('bed: no delivered audio samples')
        duration = count / rate
        if any(end > duration for _, end in tacets):
            _refuse('bed: tacet outside delivered audio')
        # Split at tacet boundaries so an exemption never hides an adjacent undeclared gap.
        boundaries = sorted({0, count, *(min(count, max(0, round(t * rate)))
                                        for pair in tacets for t in pair)})
        with mix_pcm.open('rb') as mix_file, stem_pcm.open('rb') as stem_file:
            for left, right in zip(boundaries, boundaries[1:]):
                if any(start <= left / rate and right / rate <= end for start, end in tacets):
                    continue
                for start in range(left, right, window):
                    length = min(window, right - start)
                    mix_file.seek(start * 4)
                    mix = _samples(mix_file, length)
                    stem_start = start - shift
                    if stem_start < 0 or stem_start + length > stem_count:
                        _refuse(f'bed: missing stem coverage at {start / rate}:{(start + length) / rate}s')
                    stem_file.seek(stem_start * 4)
                    reference = _samples(stem_file, length)
                    if len(reference) != length:
                        _refuse('bed: incomplete decoded stem coverage')
                    power = sum(x*x for x in reference) / length
                    mix_power = sum(x*x for x in mix) / length
                    cross = sum(x*y for x, y in zip(reference, mix)) / length
                    coefficient = cross / power if power else 0
                    correlation = cross / math.sqrt(power * mix_power) if power and mix_power else 0
                    stem_db = _db(math.sqrt(power))
                    gain_db = _db(coefficient)
                    residual = math.sqrt(sum((y - coefficient*x)**2 for x, y in zip(reference, mix)) / length)
                    ok = (stem_db is not None and stem_db >= p['bed_stem_floor_dbfs'] and
                          gain_db is not None and abs(gain_db - gain) <= p['bed_gain_tolerance_db'] and
                          correlation >= p['bed_correlation_min'])
                    rows.append({'start_s': start / rate, 'end_s': (start + length) / rate,
                                 'stem_dbfs': stem_db, 'mix_dbfs': _db(math.sqrt(mix_power)),
                                 'residual_dbfs': _db(residual), 'gain_db': gain_db,
                                 'correlation': correlation, 'ok': ok})
    for name, before in hashes.items():
        if filehash(records.path(video, name)) != before:
            _refuse(f'bed: {name} changed during measurement')
    ok = bool(rows) and all(row['ok'] for row in rows)
    return {'bed': {'ok': ok, 'result': 'pass' if ok else 'fail',
                    'value': {'files': hashes, 'windows': rows, 'offset_s': offset, 'expected_gain_db': gain,
                              'speech_band_hz': [p['bed_band_low_hz'], p['bed_band_high_hz']],
                              'units': 'dBFS (normalized PCM RMS); gain dB; correlation dimensionless'},
                    'setting': 'rendering.settings#measurement.bed_gain_tolerance_db',
                    'coverage': {'start_s': 0, 'end_s': duration, 'samples': count,
                                 'measured_samples': sum(round((r['end_s'] - r['start_s']) * rate) for r in rows),
                                 'tacets': binding['tacets']},
                    'evidence': hashes[target_name] + ': decoded speech-band PCM window regression against ' + hashes[stem['file']],
                    'file': req['file'], 'actor': 'rendering'}}


def compare_audio(video, req, path, reference_asset, segments, *, complete_reference=False):
    """Compare all channels of complete mapped PCM; changed mixing cannot stand in for source correspondence."""
    try:
        return _compare_audio(video, req, path, reference_asset, segments, complete_reference)
    except MeasurementRefused:
        raise
    except (KeyError, TypeError, ValueError, OSError, OverflowError) as error:
        decoded.refuse('audio-correspondence', 'measurement', error)


def _compare_audio(video, req, path, asset, segments, complete_reference):
    def number(value):
        return decoded.number(value, 'audio-correspondence', 'measurement')

    policy = req['settings']['measurement']
    rate = number(policy['audio_compare_sample_rate'])
    rms_limit = number(policy['audio_compare_rms_tolerance'])
    peak_limit = number(policy['audio_compare_peak_tolerance'])
    clock_limit = number(policy['audio_compare_duration_tolerance_s'])
    if rate <= 0 or rate != int(rate) or min(rms_limit, peak_limit, clock_limit) < 0:
        decoded.refuse('audio-correspondence', 'measurement', 'invalid PCM comparison settings')
    rate = int(rate)
    reference = records.path(video, asset['file'])
    name = Path(path).relative_to(video.folder).as_posix()
    target = records.path(video, name)
    hashes = {asset['file']: filehash(reference), name: filehash(target)}
    if (not hashes[asset['file']] or hashes[asset['file']] != asset['sha256'] or
            req['inputs'].get(asset['file']) != asset['sha256'] or not hashes[name]):
        decoded.refuse('audio-correspondence', 'measurement', 'missing, stale or unbound audio bytes')
    if target == reference:
        decoded.refuse('audio-correspondence', 'measurement', 'reference is the output itself')
    layouts = []
    for source in (target, reference):
        streams = decoded.probe(source, ['-select_streams', 'a', '-show_streams'])['streams']
        if len(streams) != 1:
            decoded.refuse('audio-correspondence', 'measurement', 'exactly one audio stream required')
        stream = streams[0]
        channels = int(number(stream['channels']))
        layout = stream.get('channel_layout')
        if channels < 1 or channels > 2 and not layout:
            decoded.refuse('audio-correspondence', 'measurement', 'unknown audio channel layout')
        layouts.append((channels, layout))
    if layouts[0] != layouts[1]:
        decoded.refuse('audio-correspondence', 'measurement', 'audio channel layouts differ')
    channels, layout = layouts[0]
    rows = []
    with tempfile.TemporaryDirectory(prefix='render-audio-') as temporary:
        paths = [Path(temporary) / n for n in ('output.raw', 'reference.raw')]
        for source, destination in zip((target, reference), paths):
            decoded.run('ffmpeg', ['-v', 'error', '-err_detect', 'explode', '-i', str(source),
                        '-map', '0:a:0', '-vn', '-af', f'aresample={rate}:async=1:first_pts=0',
                        '-ar', str(rate), '-c:a', 'pcm_f32le', '-f', 'f32le',
                        str(destination)], 'audio-correspondence')
        count, source_count = (p.stat().st_size // (4 * channels) for p in paths)
        cursor, source_cursor = 0, 0
        covered, source_covered = True, True
        with paths[0].open('rb') as output, paths[1].open('rb') as original:
            for segment in segments:
                src, out = [tuple(number(v) for v in segment[key]) for key in ('src', 'out')]
                if any(len(pair) != 2 or not 0 <= pair[0] < pair[1] for pair in (src, out)):
                    decoded.refuse('audio-correspondence', 'measurement', 'invalid audio mapping interval')
                source_start, source_end = (round(v * rate) for v in src)
                start, end = (round(v * rate) for v in out)
                length = end - start
                available = source_end <= source_count and end <= count
                clock_ok = (start == cursor and abs((source_end - source_start) - length) <= clock_limit * rate)
                covered = covered and available and clock_ok
                cursor = end
                source_covered = source_covered and source_start == source_cursor
                source_cursor = source_end
                output.seek(start * 4 * channels)
                original.seek(source_start * 4 * channels)
                squares, peak, read = 0., 0., 0
                # Stream windows to bound memory even for a complete long master.
                while read < length and available:
                    amount = min(rate, length - read)
                    actual, expected = _samples(output, amount * channels), _samples(original, amount * channels)
                    if len(actual) != amount * channels or len(expected) != amount * channels:
                        available = False
                        covered = False
                        break
                    for a, b in zip(actual, expected):
                        error = abs(a - b)
                        squares += error * error
                        peak = max(peak, error)
                    read += amount
                rms = math.sqrt(squares / (read * channels)) if read else None
                ok = available and clock_ok and rms is not None and rms <= rms_limit and peak <= peak_limit
                rows.append({'src': list(src), 'out': list(out), 'samples': read,
                             'rms_error': rms, 'peak_error': peak if read else None, 'ok': ok})
        covered = covered and count > 0 and abs(cursor - count) <= clock_limit * rate
        source_covered = source_covered and abs(source_cursor - source_count) <= clock_limit * rate
        if complete_reference:
            covered = covered and source_covered
    for name_, before in hashes.items():
        if filehash(records.path(video, name_)) != before:
            decoded.refuse('audio-correspondence', 'measurement', f'{name_}: changed during audio comparison')
    ok = bool(rows) and covered and all(row['ok'] for row in rows)
    return {'ok': ok, 'result': 'pass' if ok else 'fail',
            'value': {'files': hashes, 'channels': channels, 'channel_layout': layout, 'segments': rows, 'units': 'normalized PCM amplitude', 'complete': covered,
                      'complete_reference_required': complete_reference, 'complete_reference': source_covered},
            'setting': 'rendering.settings#measurement.audio_compare_rms_tolerance',
            'coverage': {'samples': count, 'start_s': 0, 'end_s': count / rate},
            'evidence': hashes[name] + ': complete mapped decoded PCM comparison against ' + hashes[asset['file']],
            'file': req['file'], 'actor': 'rendering'}
