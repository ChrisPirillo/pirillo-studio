"""Voice -> item 18 cues -> picture encode; see docs/rendering.md's seam 2d flowchart."""
import copy
import os
import tempfile
import threading
import wave
from pathlib import Path
from fractions import Fraction

from ..captions import cues
from ..spine.files import SpineError, filehash
from . import cards, clocks, color, decoded, framing, grade, mapping, mix, pixels, pushes, raster, records, source_rate


def caption_identity(video):
    adapter = video.captioner
    if adapter is None or Path(adapter.folder).resolve() != video.folder.resolve():
        raise SpineError('captions: adapter for this video is required')
    settings = cues.settings()
    model = settings['aligner']['model']
    versions = adapter.versions([model])
    if not isinstance(versions.get(model), str) or not versions[model]:
        raise SpineError('captions: aligner version unavailable')
    return {'settings': settings, 'code': cues._code(), 'versions': versions}


names = records.artifact_names
_RATES = {}  # source sha256 -> (tolerance, rate); only successful clock proofs are kept
_COLORS = {}  # asset sha256 -> decoded color; source and opaque covers share the proof
_PROBING = threading.Lock()  # request resolution precedes output locks


def prepare(video, req):
    if req['stage'] not in ('arc', 'draft') and not (req['stage'] == 'masters'
            and (req['output'] in ('long', 'vertical') or req['output'].startswith('short:'))):
        raise SpineError('compositor: native masters unsupported (seam 2h)')
    tolerance = req['settings']['measurement']['grid_tolerance_s']
    with _PROBING:
        cached = _RATES.get(req['source_sha256'])
        if cached is None or cached[0] != tolerance:
            rate = source_rate.inspect(records.path(video, req['source']), tolerance)
            _RATES[req['source_sha256']] = (tolerance, rate)
        rate = _RATES[req['source_sha256']][1]
    req = {**req, 'source_rate': rate}
    voice, picture = clocks.split(req, mapping.pieces(req))
    spans = {s['id']: s for s in req['plan_data']['spans']}
    resolved = []
    for row in picture:
        if spans[row['span']].get('hide_picture'):
            covers = [c for c in req['cards_data']['cards'] if c.get('covers') == row['span']]
            if len(covers) != 1 or 'file' not in covers[0]:
                raise SpineError('cards: opaque raster cover asset required')
            row.update(kind='cover', card=covers[0]['id'], source_frames=[0, 1])
            name = covers[0]['file']
        else:
            row['kind'], name = 'source', req['source']
        row['asset'] = {'file': name, 'sha256': req['inputs'][name]}
        path = records.path(video, name)
        stream = decoded.probe(path, ['-select_streams', 'v:0', '-show_streams'])['streams'][0]
        with _PROBING:
            digest = row['asset']['sha256']
            if digest not in _COLORS:
                _COLORS[digest] = color.inspect(path)
            row['color_conversion'] = color.binding(_COLORS[digest], req['settings']['video'])
        transform = grade.binding(req, row)
        if transform is not None:
            row['grade'] = transform
        size = pixels.geometry(stream)
        if (row['kind'] == 'cover' and req['settings']['shape'] == 'tall'
                and size != req['settings']['video']['sizes']['tall']):
            raise SpineError('cards: tall cover dimensions must match video.sizes.tall')
        if row['color_conversion']['input'] != row['color_conversion']['output'] and any(n % 2 for n in size):
            raise SpineError('color: conversion requires even source dimensions')
        span = spans[row['span']]
        if span['enter'] == 'push':
            resolved.extend(pushes.frames(req, span, row, size))
        else:
            row['crop'] = ([0, 0, *size] if row['kind'] == 'cover' else
                           framing.crop(req, span, row['source_frames'], size))
            resolved.append(row)
    mix.prepare(video, {**req, 'frame_map': voice})
    return {'source_rate': rate, 'frame_map': voice, 'picture_map': resolved,
            'elements': cards.prepare(video, {**req, 'frame_map': voice})}


def reuse(video, req, accepted):
    """Reuse produced inputs only when all bytes still match their accepted record."""
    prior = accepted['request']
    if set(prior.get('generated', {})) != set(names(req)) or any(filehash(records.path(video, p)) != h
                                      for p, h in prior['generated'].items()):
        return False
    candidate = copy.deepcopy(req)
    try:
        bind(video, candidate, copy.deepcopy(prior['generated']))
    except SpineError:
        return False
    req.update(candidate)
    return True


def voice(video, req):
    target = records.path(video, names(req)[0], writable=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    args, graph, joined = [], [], []
    rate = req['settings']['audio']['sample_rate']
    samples_per_frame = rate / Fraction(req['settings']['video']['fps'])
    for i, row in enumerate(req['frame_map']):
        source = records.path(video, row['asset']['file'])
        if filehash(source) != row['asset']['sha256']:
            raise SpineError('voice: stale source audio')
        span = next(s for s in req['plan_data']['spans'] if s['id'] == row['span'])
        if 'patch' in span:
            probe = decoded.probe(source, ['-select_streams', 'a:0', '-show_streams', '-show_frames'])
            patch = req['patches'][span['patch']['record']]
            if (len(probe['streams']) != 1 or int(probe['streams'][0]['sample_rate']) != rate
                    or sum(f['nb_samples'] for f in probe['frames']) != patch['sample_count']):
                raise SpineError('patch: actual audio sample count/rate differs from record')
        a = round(row['source_frames'][0] * samples_per_frame)
        count = round(row['frames'][1] * samples_per_frame) - round(row['frames'][0] * samples_per_frame)
        if row.get('silent') is True:
            args += ['-f', 'lavfi', '-i', f'anullsrc=r={rate}:cl=mono']
        else:
            args += ['-i', str(source)]
        graph.append(f'[{i}:a:0]aresample={rate}:async=1:first_pts=0,atrim=start_sample={a}:end_sample={a + count},'
                     f'asetpts=PTS-STARTPTS[a{i}]')
        joined.append(f'[a{i}]')
    graph.append(''.join(joined) + f'concat=n={len(joined)}:v=0:a=1,{mix.voice_filter(req)}[voice]')
    with tempfile.TemporaryDirectory(prefix='.voice-', dir=target.parent) as temporary:
        work = Path(temporary) / 'voice.wav'
        decoded.run('ffmpeg', ['-v', 'error', '-y', *args, '-filter_complex', ';'.join(graph),
                    '-map', '[voice]', '-c:a', 'pcm_s16le', str(work)], 'voice')
        with wave.open(str(work)) as pcm:
            if pcm.getnframes() != round(req['frame_map'][-1]['frames'][1] * samples_per_frame):
                raise SpineError('voice: incomplete prepared audio samples')
        with work.open('rb') as handle:
            os.fsync(handle.fileno())
        records.path(video, names(req)[0], writable=True)
        os.replace(work, target)


def bind(video, req, generated):
    req['generated'] = generated
    req['inputs'].update({p: h for p, h in generated.items() if h is not None})
    req['voice_binding'] = {'file': names(req)[0], 'sha256': generated[names(req)[0]],
                            'processing_chain': copy.deepcopy(req['settings']['mix']['voice_chain']),
                            'settings_version': req['settings']['mix']['version']}
    mix.bind(req, generated)
    req['caption_binding'] = {'record': names(req)[1], 'sha256': generated[names(req)[1]]}
    caption = records.read(video, names(req)[1])
    if caption['burn'] is not None:
        supplied = caption.get('raster')
        if not isinstance(supplied, dict):
            raise SpineError('captions: burn requires supplied raster coverage')
        req['caption_binding']['raster'] = copy.deepcopy(supplied)
        for asset in (caption['burn'], supplied):
            if not isinstance(asset.get('file'), str) or not asset.get('sha256'):
                raise SpineError('captions: raster/burn missing file or content hash')
            req['inputs'][asset['file']] = asset['sha256']
        raster.binding(video, req, caption)
    elif 'raster' in caption:
        raise SpineError('captions: raster without a bound burn')


def encode(video, req, path):
    """Artifacts are generated only after the lifecycle records the pending request."""
    voice(video, req)
    caption = cues.cues(video.captioner, req['output_id'])
    if caption['state'] != 'captioned':
        raise SpineError(f"captions: {caption['state']}: {caption['refusals'] or caption['missing']}")
    cues.sidecar(video.captioner, req['output_id'])
    mix.build(video, req, records.path(video, names(req)[0]))
    bind(video, req, {p: filehash(records.path(video, p)) for p in names(req)})
    if caption['burn'] is not None:
        raster.inspect(video, req, caption)
    inspected = cards.inspect(video, req)
    if req['elements'] != cards.elements(req, inspected):
        raise SpineError('cards: resolved elements differ from raster bounds')
    layers = [records.path(video, c['raster']['file']) for c in cards.layers(req)]
    if caption['burn'] is not None:
        layers.append(records.path(video, req['caption_binding']['raster']['file']))
    args, graph, joined = [], [], []
    settings = req['settings']
    width, height = settings['video']['sizes'][settings['shape']]
    for i, row in enumerate(req['picture_map']):
        args += ['-i', str(records.path(video, row['asset']['file']))]
        a, b = row['source_frames']
        count = row['frames'][1] - row['frames'][0]
        x, y, w, h = row['crop']
        filters = f'trim=start_frame={a}:end_frame={b},setpts=PTS-STARTPTS'
        if 'source_indices' in row:
            filters = source_rate.filters(req, row)
        if row['kind'] == 'cover':
            filters += f',loop=loop={count - 1}:size=1:start=0'
        filters += ',' + color.filters(row['color_conversion'])
        filters += ',' + grade.filters(video, req, row)
        filters += f',crop={w}:{h}:{x}:{y}:exact=1,scale={width}:{height}:flags=bilinear'
        # Fix scaling's format before concat can negotiate a different chroma path.
        filters += ',format=' + ('rgb24' if layers else settings['video']['pix_fmt'])
        filters += f",setsar={settings['video']['sample_aspect_ratio']},settb=AVTB,setpts=N/({settings['video']['fps']}*TB)"
        graph.append(f'[{i}:v:0]{filters}[v{i}]')
        joined.append(f'[v{i}]')
    # Singleton crop runs have no inferred concat duration; stamp the complete fixed grid.
    graph.append(''.join(joined) + f"concat=n={len(joined)}:v=1:a=0,settb=AVTB,"
                 f"setpts=N/({settings['video']['fps']}*TB)[picture]")
    args += ['-i', str(records.path(video, req['mix_binding']['file']))]
    picture = '[picture]'
    clock = f"settb=expr=1/({settings['video']['fps']}),setpts=N"
    if layers:
        graph.append(f'[picture]{clock}[grid]')
        picture = '[grid]'
    for i, layer in enumerate(layers):
        args += ['-i', str(layer)]
        graph.append(f'[{len(joined) + 1 + i}:v:0]{clock}[overlay{i}];'
                     f'{picture}format=gbrp[base{i}];[base{i}][overlay{i}]'
                     f'overlay=format=rgb:shortest=1:repeatlast=0[layer{i}]')
        picture = f'[layer{i}]'
    encoder = settings['encoder']
    video_args = ['-c:v', encoder['engine'], '-crf', str(encoder['crf']), '-preset', encoder['preset'],
                  '-pix_fmt', settings['video']['pix_fmt'], '-r', settings['video']['fps']]
    for key in ('color_range', 'color_space', 'color_primaries', 'color_transfer'):
        option = {'color_space': 'colorspace', 'color_transfer': 'color_trc'}.get(key, key)
        video_args += ['-' + option, settings['video'][key]]
    decoded.run('ffmpeg', ['-v', 'error', '-y', *args, '-filter_complex', ';'.join(graph),
                '-map', picture, '-map', f'{len(joined)}:a:0', *video_args,
                '-c:a', settings['audio']['codec'], '-ar', str(settings['audio']['sample_rate']), str(path)], 'encode')
