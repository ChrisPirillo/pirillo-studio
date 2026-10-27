"""Bound caption alpha edges; the delivered composite is checked by pixels.compare."""
import contextlib
import math
import shutil
from fractions import Fraction

from ..spine.files import SpineError, filehash
from . import decoded, mapping, pixels, records


def binding(video, req, caption):
    """Require current raster and burn bytes bound into this request."""
    binding = req['caption_binding'].get('raster')
    if not isinstance(binding, dict):
        raise SpineError('captions: ASS-only burn; bound caption raster required')
    burn = caption['burn']
    if not isinstance(burn, dict):
        raise SpineError('captions: raster without a bound burn')
    for asset in (burn, binding):
        name = asset.get('file')
        if not isinstance(name, str) or not asset.get('sha256'):
            raise SpineError('captions: raster/burn missing file or content hash')
        if (req['inputs'].get(name) != asset['sha256']
                or filehash(records.path(video, name)) != asset['sha256']):
            raise SpineError(f'captions: stale or unbound raster/burn: {name}')
    if binding.get('burn_sha256') != burn['sha256']:
        raise SpineError('captions: raster does not bind the current burn')
    return binding


def inspect(video, req, caption):
    """Decode every alpha pixel, retaining contiguous intervals of equal bounds."""
    asset = binding(video, req, caption)
    rate = decoded.fraction(req['settings']['video']['fps'], 'captions', 'video.fps')
    edges = []
    for cue in caption['cues']:
        edge = []
        for field in ('start_s', 'end_s'):
            try:
                value = Fraction(str(cue.get(field)))
                if value < 0:
                    raise ValueError('negative time')
            except (ValueError, ZeroDivisionError):
                decoded.refuse('captions', 'captions.cue_times.' + field, f'captions: invalid cue {field}')
            edge.append(math.ceil(value * rate))
        edges.append(edge)
    return inspect_alpha(video, req, asset, req['settings'].get('caption_safe'), edges, 'captions')


def inspect_alpha(video, req, asset, safe, edges, reading):
    """Decode a full-output straight-alpha layer against independently resolved edges."""
    setting = req['settings']
    width, height = setting['video']['sizes'][setting['shape']]
    if safe is None:
        raise SpineError(f'{reading}: profile safe area required')
    x, y, w, h = mapping.rectangle(safe, reading + ' safe area')
    if x + w > width or y + h > height:
        raise SpineError(f'{reading}: safe area exceeds output bounds')
    count = req['frame_map'][-1]['frames'][1]
    path = records.path(video, asset['file'])
    from .render import MeasurementRefused
    try:
        size = pixels.dimensions(path, setting['video']['fps'], setting['measurement']['grid_tolerance_s'],
                                 frames_required=count, exact=True)
    except MeasurementRefused as error:
        decoded.refuse(reading, error.setting, str(error))
    if size != [width, height]:
        raise SpineError(f'{reading}: raster dimensions differ from output')
    args = [shutil.which('ffmpeg'), '-nostdin', '-v', 'error', '-err_detect', 'explode', '-i', str(path),
            '-vf', 'alphaextract', '-an', '-fps_mode', 'passthrough', '-f', 'rawvideo',
            '-pix_fmt', 'gray', 'pipe:1']
    bounds, decoded_count = [], 0
    with contextlib.closing(pixels.read_frames(args, width * height, reading)) as frames:
        for index, frame in enumerate(frames):
            left, top, right, bottom = width, height, 0, 0
            for row in range(height):
                line = frame[row * width:(row + 1) * width]
                if line.strip(b'\0'):
                    left = min(left, width - len(line.lstrip(b'\0')))
                    right = max(right, len(line.rstrip(b'\0')))
                    top, bottom = min(top, row), row + 1
            box = [left, top, right - left, bottom - top] if bottom else None
            active = any(a <= index < b for a, b in edges)
            if bool(box) != active:
                raise SpineError(f'{reading}: raster cue edge differs at frame {index}')
            if box and not (x <= left and y <= top and right <= x + w and bottom <= y + h):
                raise SpineError(f'{reading}: raster outside safe area at frame {index}')
            if bounds and bounds[-1]['box'] == box:
                bounds[-1]['frames'][1] = index + 1
            else:
                bounds.append({'frames': [index, index + 1], 'box': box})
            decoded_count += 1
    if decoded_count != count or filehash(path) != asset['sha256']:
        raise SpineError(f'{reading}: raster changed or incomplete while decoding')
    return {'sha256': asset['sha256'], 'safe': safe, 'bounds': bounds}
