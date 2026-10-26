"""Decoded color identity and one explicit SDR conversion; docs/rendering-color.md."""
from . import decoded

FIELDS = ('color_primaries', 'color_transfer', 'color_space', 'color_range')


def description(value):
    result = {key: value.get(key) for key in FIELDS}
    if result['color_transfer'] in ('smpte2084', 'arib-std-b67'):
        decoded.refuse('color', 'video', 'HDR transfer requires tone mapping (next grade seam)')
    for key, item in result.items():
        supported = ('tv', 'pc') if key == 'color_range' else ('bt709', 'smpte170m', 'bt470bg')
        if item not in supported:
            decoded.refuse('color', 'video', f'unspecified, unknown or unsupported {key}: {item!r}')
    return result


def validate(raw):
    if len(raw.get('streams', [])) != 1 or not raw.get('frames'):
        decoded.refuse('color', 'video', 'missing unique decoded source color stream/frames')
    declared = description(raw['streams'][0])
    first = description(raw['frames'][0])
    for index, frame in enumerate(raw['frames']):
        if description(frame) != first:
            decoded.refuse('color', 'video', f'mixed source color at frame {index}')
    if first != declared:
        decoded.refuse('color', 'video', 'declared source color disagrees with decoded frames')
    return first


def inspect(path):
    fields = ','.join(FIELDS)
    return validate(decoded.probe(path, ['-select_streams', 'v:0', '-show_streams', '-show_frames',
                                       '-show_entries', f'stream={fields}:frame={fields}']))


def binding(source, profile):
    target = description(profile)
    if any(target[key] != 'bt709' for key in FIELDS[:3]):
        decoded.refuse('color', 'video', 'unsupported output color; BT.709 required')
    return {'input': description(source), 'output': target}


def filters(conversion):
    source, target = conversion['input'], conversion['output']
    if source == target:
        return 'null'
    options = {'color_primaries': 'primaries', 'color_transfer': 'trc',
               'color_space': 'space', 'color_range': 'range'}
    return 'colorspace=' + ':'.join(
        f'{prefix}{option}={values[key]}' for prefix, values in (('i', source), ('', target))
        for key, option in options.items()) + ':format=yuv444p:fast=0'
