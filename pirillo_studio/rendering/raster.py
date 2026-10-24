"""Bound caption alpha edges; the delivered composite is checked by pixels.compare."""
import contextlib
import math
import shutil

from ..spine.files import SpineError, filehash
from . import decoded, mapping, pixels, records


def inspect(video, req, caption):
    """Decode every alpha pixel, requiring cue-clock visibility inside an explicit safe area."""
    binding = req['caption_binding'].get('raster')
    if not isinstance(binding, dict):
        raise SpineError('captions: ASS-only burn; bound caption raster required')
    burn = caption['burn']
    for asset in (burn, binding):
        name = asset['file']
        if (not asset['sha256'] or req['inputs'].get(name) != asset['sha256']
                or filehash(records.path(video, name)) != asset['sha256']):
            raise SpineError(f'captions: stale or unbound raster/burn: {name}')
    if binding['burn_sha256'] != burn['sha256']:
        raise SpineError('captions: raster does not bind the current burn')
    setting = req['settings']
    width, height = setting['video']['sizes'][setting['shape']]
    safe = setting.get('caption_safe')
    if safe is None:
        raise SpineError('captions: profile caption_safe required')
    x, y, w, h = mapping.rectangle(safe, 'captions safe area')
    if x + w > width or y + h > height:
        raise SpineError('captions: safe area exceeds output bounds')
    count = req['frame_map'][-1]['frames'][1]
    path = records.path(video, binding['file'])
    from .render import MeasurementRefused
    try:
        size = pixels.dimensions(path, setting['video']['fps'], setting['measurement']['grid_tolerance_s'],
                                 frames_required=count, exact=True)
    except MeasurementRefused as error:
        decoded.refuse('captions', error.setting, str(error))
    if size != [width, height]:
        raise SpineError('captions: raster dimensions differ from output')
    rate = decoded.fraction(setting['video']['fps'], 'captions', 'video.fps')
    edges = [(math.ceil(decoded.fraction(str(c['start_s']), 'captions', 'video.fps') * rate),
              math.ceil(decoded.fraction(str(c['end_s']), 'captions', 'video.fps') * rate))
             for c in caption['cues']]
    args = [shutil.which('ffmpeg'), '-nostdin', '-v', 'error', '-err_detect', 'explode', '-i', str(path),
            '-vf', 'alphaextract', '-an', '-fps_mode', 'passthrough', '-f', 'rawvideo',
            '-pix_fmt', 'gray', 'pipe:1']
    bounds = []
    with contextlib.closing(pixels.read_frames(args, width * height, 'captions')) as frames:
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
                raise SpineError(f'captions: raster cue edge differs at frame {index}')
            if box and not (x <= left and y <= top and right <= x + w and bottom <= y + h):
                raise SpineError(f'captions: raster outside safe area at frame {index}')
            bounds.append(box)
    if len(bounds) != count or filehash(path) != binding['sha256']:
        raise SpineError('captions: raster changed or incomplete while decoding')
    return {'sha256': binding['sha256'], 'safe': safe, 'bounds': bounds}
