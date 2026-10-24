"""Voice -> item 18 cues -> picture encode; see docs/rendering.md's seam 2d flowchart."""
import copy
import os
import tempfile
import wave
from pathlib import Path

from ..captions import cues
from ..spine.files import SpineError, filehash
from . import clocks, decoded, mapping, pixels, records


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


def names(req):
    output = req['output_id']
    return [f'records/render/{output}-voice.wav', f'records/captions/{output}.json',
            cues._srt_name(output)]


def prepare(video, req):
    if req['stage'] not in ('arc', 'draft') or req['settings']['shape'] != 'wide':
        raise SpineError('compositor: native masters/tall and caption burns unsupported (seam 2e)')
    if 'grade' in req['settings']:
        raise SpineError('compositor: grades unsupported (seam 2e)')
    if any(c['kind'] != 'cover' for c in req['cards_data']['cards']):
        raise SpineError('compositor: non-cover card raster coverage unsupported')
    voice, picture = clocks.split(req, mapping.pieces(req))
    spans = {s['id']: s for s in req['plan_data']['spans']}
    for row in picture:
        if spans[row['span']].get('hide_picture'):
            cards = [c for c in req['cards_data']['cards'] if c.get('covers') == row['span']]
            if len(cards) != 1 or 'file' not in cards[0]:
                raise SpineError('cards: opaque raster cover asset required')
            row.update(kind='cover', card=cards[0]['id'], source_frames=[0, 1])
            name = cards[0]['file']
        else:
            row['kind'], name = 'source', req['source']
        row['asset'] = {'file': name, 'sha256': req['inputs'][name]}
        path = records.path(video, name)
        stream = decoded.probe(path, ['-select_streams', 'v:0', '-show_streams'])['streams'][0]
        if stream.get('color_space') != req['settings']['video']['color_space']:
            raise SpineError('compositor: missing or differing source color matrix; conversion unsupported (seam 2e)')
        size = [pixels.integer(stream[k], k, 1) for k in ('width', 'height')]
        row['crop'] = [0, 0, *size]
    return {'frame_map': voice, 'picture_map': picture, 'elements': []}


def reuse(video, req, accepted):
    """Reuse produced inputs only when all bytes still match their accepted record."""
    prior = accepted['request']
    if set(prior.get('generated', {})) != set(names(req)) or any(filehash(records.path(video, p)) != h
                                      for p, h in prior['generated'].items()):
        return False
    bind(req, copy.deepcopy(prior['generated']))
    return True


def voice(video, req):
    target = records.path(video, names(req)[0], writable=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    args, graph, joined = [], [], []
    rate = req['settings']['audio']['sample_rate']
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
        args += ['-i', str(source)]
        a, b = row['src']
        graph.append(f'[{i}:a:0]atrim=start={a}:end={b},asetpts=PTS-STARTPTS,'
                     f'aresample={rate}[a{i}]')
        joined.append(f'[a{i}]')
    graph.append(''.join(joined) + f'concat=n={len(joined)}:v=0:a=1[voice]')
    with tempfile.TemporaryDirectory(prefix='.voice-', dir=target.parent) as temporary:
        work = Path(temporary) / 'voice.wav'
        decoded.run('ffmpeg', ['-v', 'error', '-y', *args, '-filter_complex', ';'.join(graph),
                    '-map', '[voice]', '-c:a', 'pcm_s16le', str(work)], 'voice')
        with wave.open(str(work)) as pcm:
            if pcm.getnframes() != round(req['frame_map'][-1]['out'][1] * rate):
                raise SpineError('voice: incomplete prepared audio samples')
        with work.open('rb') as handle:
            os.fsync(handle.fileno())
        records.path(video, names(req)[0], writable=True)
        os.replace(work, target)


def bind(req, generated):
    req['generated'] = generated
    req['inputs'].update(generated)
    req['voice_binding'] = {'file': names(req)[0], 'sha256': generated[names(req)[0]]}
    req['caption_binding'] = {'record': names(req)[1], 'sha256': generated[names(req)[1]]}


def encode(video, req, path):
    """Artifacts are generated only after the lifecycle records the pending request."""
    voice(video, req)
    caption = cues.cues(video.captioner, req['output_id'])
    if caption['state'] != 'captioned':
        raise SpineError(f"captions: {caption['state']}: {caption['refusals'] or caption['missing']}")
    if caption['burn'] is not None:
        raise SpineError('captions: decoded burn raster coverage unsupported (seam 2e)')
    cues.sidecar(video.captioner, req['output_id'])
    bind(req, {p: filehash(records.path(video, p)) for p in names(req)})
    args, graph, joined = [], [], []
    settings = req['settings']
    width, height = settings['video']['sizes'][settings['shape']]
    for i, row in enumerate(req['picture_map']):
        args += ['-i', str(records.path(video, row['asset']['file']))]
        a, b = row['source_frames']
        count = row['frames'][1] - row['frames'][0]
        x, y, w, h = row['crop']
        filters = f'trim=start_frame={a}:end_frame={b},setpts=PTS-STARTPTS'
        if row['kind'] == 'cover':
            filters += f',loop=loop={count - 1}:size=1:start=0'
        filters += f',crop={w}:{h}:{x}:{y}:exact=1,scale={width}:{height}:flags=bilinear'
        filters += f",setsar={settings['video']['sample_aspect_ratio']},settb=AVTB,setpts=N/({settings['video']['fps']}*TB)"
        graph.append(f'[{i}:v:0]{filters}[v{i}]')
        joined.append(f'[v{i}]')
    graph.append(''.join(joined) + f'concat=n={len(joined)}:v=1:a=0[picture]')
    args += ['-i', str(records.path(video, names(req)[0]))]
    encoder = settings['encoder']
    video_args = ['-c:v', encoder['engine'], '-crf', str(encoder['crf']), '-preset', encoder['preset'],
                  '-pix_fmt', settings['video']['pix_fmt'], '-r', settings['video']['fps']]
    for key in ('color_range', 'color_space', 'color_primaries', 'color_transfer'):
        option = {'color_space': 'colorspace', 'color_transfer': 'color_trc'}.get(key, key)
        video_args += ['-' + option, settings['video'][key]]
    decoded.run('ffmpeg', ['-v', 'error', '-y', *args, '-filter_complex', ';'.join(graph),
                '-map', '[picture]', '-map', f'{len(joined)}:a:0', *video_args,
                '-c:a', settings['audio']['codec'], '-ar', str(settings['audio']['sample_rate']), str(path)], 'encode')
