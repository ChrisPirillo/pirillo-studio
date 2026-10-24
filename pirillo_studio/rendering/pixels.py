"""Compare every delivered frame with its bound full-frame source or opaque cover.

FFmpeg subtracts RGB planes; records retain per-frame measured evidence.
Arbitrary grades, transparent overlays and animated transforms require a different binding.
"""
import contextlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..spine.files import filehash
from . import decoded, mapping, records


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        decoded.refuse(name, 'measurement', f'{name}: expected integer >= {minimum}')
    return value


def dimensions(path, fps=None, tolerance=None, *, frames_required=None, exact=False, seeks=None, stream=None):
    entries = ['-select_streams', 'v:0', '-show_streams']
    if fps is not None or frames_required is not None:
        entries += ['-show_frames', '-show_entries', 'stream=width,height,pix_fmt,avg_frame_rate,r_frame_rate,time_base:frame=pts,duration']
    raw = decoded.probe(path, entries)
    streams = raw['streams']
    if len(streams) != 1:
        decoded.refuse('frame-map', 'verification.complete_decode', 'asset has no unique video stream')
    if stream is not None:
        stream.update(streams[0])
    if frames_required is not None:
        count = len(raw['frames'])
        if count < frames_required or (exact and count != frames_required):
            decoded.refuse('frame-map', 'verification.complete_decode', 'missing source/output or unmapped extra output frame')
    if seeks is not None:
        base = decoded.fraction(streams[0]['time_base'], 'frame-map', 'video.fps')
        for index in seeks:
            # A midpoint avoids timestamp rounding that could skip the requested frame.
            points = [decoded.number(raw['frames'][n]['pts'], 'frame-map', 'video.fps') * base
                      for n in (index - 1, index)] if index else []
            if points and points[0] >= points[1]:
                decoded.refuse('frame-map', 'verification.complete_decode', 'unordered frame timestamps')
            seeks[index] = str(float(sum(points) / 2)) if points else None
    if fps is not None:
        expected = decoded.fraction(fps, 'frame-map', 'video.fps')
        for key in ('avg_frame_rate', 'r_frame_rate'):
            if decoded.fraction(streams[0].get(key), 'frame-map', 'video.fps') != expected:
                decoded.refuse('frame-map', 'video.fps', 'source frame rate differs from requested frame grid')
        tolerance = decoded.number(tolerance, 'frame-map', 'measurement.grid_tolerance_s')
        if tolerance < 0 or not raw.get('frames'):
            decoded.refuse('frame-map', 'measurement.grid_tolerance_s', 'invalid tolerance or no source frames')
        base = decoded.fraction(streams[0]['time_base'], 'frame-map', 'video.fps')
        for index, frame in enumerate(raw['frames']):
            pts = decoded.number(frame.get('pts'), 'frame-map', 'video.fps') * base
            duration = decoded.number(frame.get('duration'), 'frame-map', 'video.fps') * base
            if abs(pts - index / expected) > tolerance or abs(duration - 1 / expected) > tolerance:
                decoded.refuse('frame-map', 'video.fps', f'source frame rate clock differs at frame {index}')
    return [integer(streams[0][key], key, 1) for key in ('width', 'height')]


def frames(path, size, *, reference, row, output_seek, source_seek, pix_fmt, raster=None):
    """Yield one span's absolute RGB differences; only a declared cover may repeat."""
    width, height = [integer(value, 'pixel size', 1) for value in size]
    executable = shutil.which('ffmpeg')
    if executable is None:
        decoded.refuse('frame-map', 'verification.tools', 'ffmpeg unavailable')
    args = [executable, '-nostdin', '-v', 'error']
    for source, seek in ((path, output_seek), (reference, source_seek)):
        args += ['-err_detect', 'explode']
        if seek is not None:
            args += ['-seek_timestamp', '1', '-ss', seek, '-accurate_seek']
        args += ['-i', str(source)]
    count = row['frames'][1] - row['frames'][0]
    source_count = row['source_frames'][1] - row['source_frames'][0]
    x, y, w, h = row['crop']
    rgb = f'scale={width}:{height}:flags=bilinear,format=rgb24,format=gbrp'
    clock = 'settb=expr=1/1,setpts=N'
    # Match the encoder's scaling format before RGB comparison, including chroma resampling.
    scaled = f'scale={width}:{height}:flags=bilinear,format={pix_fmt},{rgb}' if raster is None else rgb
    filters = [f'trim=end_frame={source_count}', f'crop={w}:{h}:{x}:{y}:exact=1', scaled]
    if row['kind'] == 'cover' and source_count == 1:
        filters.append(f'loop=loop={count - 1}:size=1:start=0')
    graph = [f'[0:v:0]trim=end_frame={count},{rgb},{clock}[actual]',
             '[1:v:0]' + ','.join(filters + [clock]) + '[reference]',
             '[actual][reference]blend=all_mode=difference:shortest=1:repeatlast=0[diff]']
    if raster is not None:
        args += ['-i', str(raster)]
        start, end = row['frames']
        graph[-1:] = [f'[2:v:0]trim=start_frame={start}:end_frame={end},{clock}[caption]',
                      '[reference][caption]overlay=format=rgb:shortest=1:repeatlast=0,format=gbrp[burned]',
                      '[actual][burned]blend=all_mode=difference:shortest=1:repeatlast=0[diff]']
    args += ['-filter_complex', ';'.join(graph), '-map', '[diff]', '-an', '-fps_mode', 'passthrough',
             '-f', 'rawvideo', '-pix_fmt', 'gbrp', 'pipe:1']
    yield from read_frames(args, width * height * 3, 'frame-map')


def read_frames(args, length, reading):
    """Stream fixed-size decoded frames and fail on partial output or decoder errors."""
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=errors)
        except OSError as error:
            decoded.refuse(reading, 'verification.tools', error)
        try:
            while True:
                frame = process.stdout.read(length)
                if not frame:
                    break
                if len(frame) != length:
                    decoded.refuse(reading, 'verification.complete_decode', 'partial decoded frame')
                yield frame
            result = process.wait()
            errors.seek(0)
            error = errors.read().decode('utf-8', errors='replace').strip()
            if result or error:
                decoded.refuse(reading, 'verification.complete_decode', f'ffmpeg decode: {error}')
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
            process.wait()


def _asset(video, req, row, verified):
    asset = row['asset']
    path = records.path(video, asset['file'])
    if not asset['sha256'] or req['inputs'].get(asset['file']) != asset['sha256'] or filehash(path) != asset['sha256']:
        decoded.refuse('frame-map', 'verification.complete_decode', f"stale or unbound asset: {asset['file']}")
    key = (asset['file'], row['kind'])
    if key not in verified:
        needed = max(r['source_frames'][1] for r in mapping.pictures(req) if r['asset']['file'] == asset['file'])
        seeks = {r['source_frames'][0]: None for r in mapping.pictures(req) if r['asset']['file'] == asset['file']}
        size = dimensions(path, req['settings']['video']['fps'] if row['kind'] == 'source' else None,
                          req['settings']['measurement']['grid_tolerance_s'], frames_required=needed, seeks=seeks)
        verified[key] = {'size': size, 'seeks': seeks}
    size = verified[key]['size']
    crop = row['crop']
    if not isinstance(crop, list) or len(crop) != 4:
        decoded.refuse('frame-map', 'measurement', 'crop must contain x,y,width,height')
    x, y, w, h = [integer(value, 'crop', 1 if i > 1 else 0) for i, value in enumerate(crop)]
    if x + w > size[0] or y + h > size[1]:
        decoded.refuse('frame-map', 'measurement', 'crop exceeds decoded asset bounds')
    return path


def compare(video, req, output_path, *, raster=None):
    """Return checks and per-frame evidence; the caller independently binds rows to the plan."""
    try:
        return _compare(video, req, output_path, raster=raster)
    except (KeyError, TypeError, IndexError) as error:
        decoded.refuse('frame-map', 'measurement', f'missing or invalid frame binding: {error}')


def _compare(video, req, output_path, *, raster=None):
    setting = req['settings']
    policy = setting['measurement']
    limits = {}
    for key in ('pixel_mean_error_max', 'pixel_peak_error_max'):
        limits[key] = decoded.number(policy[key], 'frame-map', 'measurement.' + key)
        if not 0 <= limits[key] <= 255:
            decoded.refuse('frame-map', 'measurement.' + key, 'pixel error must be within 0..255')
    size = setting['video']['sizes'][setting['shape']]
    output = records.path(video, Path(output_path).relative_to(video.folder).as_posix())
    before = filehash(output)
    if before is None:
        decoded.refuse('frame-map', 'verification.complete_decode', 'output missing')
    rows = mapping.pictures(req)
    if not isinstance(rows, list) or not rows:
        decoded.refuse('frame-map', 'measurement', 'empty frame binding')
    evidence, assets, verified, cursor = [], {}, {}, 0
    for row in rows:
        start, end = row['frames']
        integer(start, 'output frame start')
        integer(end, 'output frame end', start + 1)
        if start != cursor or row['kind'] not in ('source', 'cover'):
            decoded.refuse('frame-map', 'measurement', 'frame binding has gap, overlap or unsupported kind')
        source_start, source_end = row['source_frames']
        integer(source_start, 'source frame start')
        integer(source_end, 'source frame end', source_start + 1)
        hold = row['kind'] == 'cover' and source_end - source_start == 1
        if not hold and source_end - source_start != end - start:
            decoded.refuse('frame-map', 'measurement', 'source and output frame counts differ')
        assets[row['asset']['file']] = {'path': _asset(video, req, row, verified), 'sha256': row['asset']['sha256']}
        cursor = end
    seeks = {r['frames'][0]: None for r in rows}
    stream = {}
    if dimensions(output, frames_required=cursor, exact=True, seeks=seeks, stream=stream) != size:
        decoded.refuse('frame-map', 'video.sizes.' + setting['shape'], 'output dimensions differ')
    for row in rows:
        start, end = row['frames']
        source_start, source_end = row['source_frames']
        hold = row['kind'] == 'cover' and source_end - source_start == 1
        name = row['asset']['file']
        source_seek = verified[(name, row['kind'])]['seeks'][source_start]
        with contextlib.closing(frames(output, size, reference=assets[name]['path'], row=row,
                                       output_seek=seeks[start], source_seek=source_seek,
                                       pix_fmt=stream['pix_fmt'],
                                       **({'raster': raster} if raster is not None else {}))) as differences:
            for index in range(start, end):
                delta = next(differences, None)
                if delta is None:
                    decoded.refuse('frame-map', 'verification.complete_decode', f'missing source or output frame {index}')
                mean, peak = sum(delta) / len(delta), max(delta)
                ok = mean <= limits['pixel_mean_error_max'] and peak <= limits['pixel_peak_error_max']
                evidence.append({'frame': index, 'span': row['span'], 'kind': row['kind'],
                                 'asset': row['asset'], 'source_frame': source_start if hold else source_start + index - start,
                                 'mean_error': mean, 'peak_error': peak, 'ok': ok})
            if next(differences, None) is not None:
                decoded.refuse('frame-map', 'verification.complete_decode', 'unexpected difference frame')
    if filehash(records.path(video, output.relative_to(video.folder).as_posix())) != before:
        decoded.refuse('file_sha256', 'verification.complete_decode', 'output changed during pixel comparison')
    for name, asset in assets.items():
        if filehash(records.path(video, name)) != asset['sha256']:
            decoded.refuse('frame-map', 'verification.complete_decode', f'asset changed during comparison: {name}')
    checks = {}
    for name in ('frame-map', 'cards'):
        selected = evidence if name == 'frame-map' else [item for item in evidence if item['kind'] == 'cover']
        ok = all(item['ok'] for item in selected)
        checks[name] = {'ok': ok, 'result': 'pass' if ok else 'fail',
                        'value': {'frames': len(selected), 'failed_frames': [item['frame'] for item in selected if not item['ok']],
                                  'applicable': bool(selected),
                                  'mean_error_max': max(item['mean_error'] for item in selected) if selected else None,
                                  'peak_error_max': max(item['peak_error'] for item in selected) if selected else None},
                        'setting': 'rendering.settings#measurement.pixel_mean_error_max + rendering.settings#measurement.pixel_peak_error_max',
                        'coverage': {'frames': len(selected), 'output_frames': cursor},
                        'evidence': before + ': every mapped frame decoded to RGB and compared with bound asset pixels',
                        'file': req['file'], 'actor': 'rendering'}
    return {'checks': checks, 'frame_checks': evidence}
