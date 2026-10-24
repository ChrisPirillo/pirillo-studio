"""Processed voice and approved bed -> retained stem and additive mix (rendering.md seam 2g)."""
import copy
import math
import os
import re
import tempfile
import threading
from fractions import Fraction
from pathlib import Path

from ..spine.files import SpineError, filehash
from . import decoded, records


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise SpineError(f'{name}: finite number required')
    return value


def _keys(value, keys, name):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise SpineError(f'{name}: expected fields {", ".join(keys)}')


def _chain(value, name):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() or
                                         any(c in x for c in ';[]') for x in value):
        raise SpineError(f'{name}: filter chain must be a list of unlabelled audio filters')
    return ','.join(value) if value else 'anull'


def policy(req):
    p = req['settings'].get('mix')
    _keys(p, ('version', 'voice_chain', 'bed_eq', 'bed_loudness_lufs', 'duck', 'pin'), 'mix')
    if not isinstance(p['version'], str) or not p['version'].strip():
        raise SpineError('mix.version: nonempty version required')
    _chain(p['voice_chain'], 'mix.voice_chain')
    _chain(p['bed_eq'], 'mix.bed_eq')
    _number(p['bed_loudness_lufs'], 'mix.bed_loudness_lufs')
    duck = p['duck']
    _keys(duck, ('threshold', 'ratio', 'attack', 'release', 'makeup', 'knee', 'link', 'detection'), 'mix.duck')
    ranges = {'threshold': (0.000976563, 1), 'ratio': (1, 20), 'attack': (0.01, 2000),
              'release': (0.01, 9000), 'makeup': (1, 64), 'knee': (1, 8)}
    for name, (low, high) in ranges.items():
        if not low <= _number(duck[name], 'mix.duck.' + name) <= high:
            raise SpineError(f'mix.duck.{name}: outside compressor range')
    if duck['link'] not in ('average', 'maximum') or duck['detection'] not in ('peak', 'rms'):
        raise SpineError('mix.duck: unsupported link or detection')
    pin = p['pin']
    _keys(pin, ('limit', 'attack', 'release', 'level'), 'mix.pin')
    for name, (low, high) in {'limit': (0.0625, 1), 'attack': (0.1, 80), 'release': (1, 8000)}.items():
        if not low <= _number(pin[name], 'mix.pin.' + name) <= high:
            raise SpineError(f'mix.pin.{name}: outside limiter range')
    if not isinstance(pin['level'], bool):
        raise SpineError('mix.pin.level: boolean required')
    return p


def voice_filter(req):
    return _chain(policy(req)['voice_chain'], 'mix.voice_chain')


def _duration(req):
    return float(Fraction(req['frame_map'][-1]['frames'][1]) / Fraction(req['settings']['video']['fps']))


def bed_plan(req):
    data = req['bed_data']
    allowed = {'plan', 'request', 'tempo_from', 'assets', 'decision', 'tacets', 'title', 'note'}
    if not isinstance(data, dict):
        raise SpineError('records/bed.json: object required')
    unknown = set(data) - allowed
    if unknown:
        raise SpineError(f'records/bed.json: unsupported fields {", ".join(sorted(unknown))}')
    assets = data.get('assets', [])
    if not isinstance(assets, list) or len(assets) > 1:
        raise SpineError('records/bed.json: assets supports at most one audio asset (seam 2h)')
    if assets and data.get('decision') != 'approve':
        raise SpineError('records/bed.json: bed requires explicit approve decision')
    bound = []
    for asset in assets:
        if isinstance(asset, str):
            asset = {'file': asset}
        if not isinstance(asset, dict) or 'file' not in asset or set(asset) - {'file', 'sha256'}:
            raise SpineError('records/bed.json: asset supports only file and sha256')
        name = asset['file']
        if not isinstance(name, str) or not req['inputs'].get(name):
            raise SpineError(f'records/bed.json: missing bound asset {name!r}')
        sha = req['inputs'][name]
        if 'sha256' in asset and asset['sha256'] != sha:
            raise SpineError(f'{name}: bed asset hash differs')
        bound.append({'file': name, 'sha256': sha})
    tacets = data.get('tacets', [])
    if not isinstance(tacets, list) or (tacets and not assets):
        raise SpineError('records/bed.json: tacets require a bed and a list of intervals')
    previous = 0
    for pair in tacets:
        if not isinstance(pair, list) or len(pair) != 2:
            raise SpineError('records/bed.json: invalid tacet interval')
        a, b = [_number(x, 'bed.tacets') for x in pair]
        if not previous <= a < b <= _duration(req):
            raise SpineError('records/bed.json: tacets overlap or exceed output duration')
        previous = b
    return {'mode': 'bed' if bound else 'voice_only', 'assets': bound, 'tacets': copy.deepcopy(tacets),
            'brief': {'file': 'records/bed.json', 'sha256': req['inputs']['records/bed.json']}}


def prepare(video, req):
    """Validate media coverage without creating artifacts; caller owns the rendering transaction."""
    policy(req)
    plan = bed_plan(req)
    for name in records.artifact_names(req)[4:]:
        records.path(video, name, writable=True)
    for asset in plan['assets']:
        rate, count = _samples(_source(video, asset), asset)
        if count < math.ceil(_duration(req) * rate):
            raise SpineError(f"{asset['file']}: bed audio does not cover output duration")
    return plan


_SAMPLES = {}  # bed sha256 -> (sample rate, samples): a render resolves 3 times, the bed's bytes are probed once
_PROBING = threading.Lock()  # concurrent renders resolve before their output locks; one probes, the rest read


def _samples(source, asset):
    """_source checked the bytes against asset sha256 first, so the hash names what was probed; refusals are not kept."""
    with _PROBING:
        if asset['sha256'] not in _SAMPLES:
            probe = decoded.probe(source, ['-show_streams', '-show_frames'])
            streams = probe['streams']
            if len(streams) != 1 or streams[0]['codec_type'] != 'audio':
                raise SpineError(f"{asset['file']}: bed requires exactly one audio stream")
            _SAMPLES[asset['sha256']] = (int(streams[0]['sample_rate']),
                                         sum(int(frame['nb_samples']) for frame in probe['frames']))
        return _SAMPLES[asset['sha256']]


def _source(video, asset):
    source = records.path(video, asset['file'])
    if filehash(source) != asset['sha256']:
        raise SpineError(f"{asset['file']}: bed asset missing or changed")
    return source


def _options(values):
    return ':'.join(f'{key}={int(value) if isinstance(value, bool) else value}' for key, value in values.items())


def _commit(video, name, work):
    target = records.path(video, name, writable=True)
    with work.open('rb') as handle:
        os.fsync(handle.fileno())
    os.replace(work, target)


def build(video, req, voice_path):
    """Write the real contribution and final audio under the caller's records.writer lock; prepare() probed the bed."""
    plan, p = bed_plan(req), policy(req)
    bed_name, mix_name = records.artifact_names(req)[4:]
    target = records.path(video, mix_name, writable=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    rate = req['settings']['audio']['sample_rate']
    count = round(_duration(req) * rate)
    if plan['mode'] == 'voice_only':
        for name in (bed_name, mix_name):
            old = records.path(video, name, writable=True)
            if old.exists():
                old.unlink()
        return
    with tempfile.TemporaryDirectory(prefix='.mix-', dir=target.parent) as temporary:
        work = Path(temporary)
        mixed = work / 'mix.wav'
        source = _source(video, plan['assets'][0])
        shape = f"aresample={rate},atrim=end_sample={count},asetpts=PTS-STARTPTS," + _chain(p['bed_eq'], 'mix.bed_eq')
        reading = decoded.run('ffmpeg', ['-v', 'info', '-i', str(source), '-af', shape + ',ebur128',
                              '-f', 'null', '-'], 'bed.loudness')
        values = re.findall(r'I:\s+(\S+) LUFS', reading.stderr)
        if not values or not math.isfinite(float(values[-1])):
            raise SpineError(f'{source.name}: bed loudness unavailable')
        gain = p['bed_loudness_lufs'] - float(values[-1])
        # Equal audio frames prevent sidechain EOF from discarding the final partial block.
        graph = (f'[0:a:0]{shape},volume={gain}dB,asetnsamples=n=1024:p=0[bed];'
                 '[1:a:0]asetnsamples=n=1024:p=0[sidechain];'
                 f'[bed][sidechain]sidechaincompress={_options(p["duck"])},'
                 f'alimiter={_options(p["pin"])}:latency=1')
        for a, b in plan['tacets']:
            graph += f",aeval=exprs='if(gte(t,{a})*lt(t,{b}),0,val(ch))':c=same"
        graph += f',atrim=end_sample={count}[stem]'
        stem = work / 'bed.wav'
        decoded.run('ffmpeg', ['-v', 'error', '-y', '-i', str(source), '-i', str(voice_path),
                    '-filter_complex', graph, '-map', '[stem]', '-c:a', 'pcm_f32le', str(stem)], 'bed')
        frames = decoded.probe(stem, ['-show_frames'])['frames']
        if sum(int(frame['nb_samples']) for frame in frames) != count:
            raise SpineError('bed: incomplete retained stem samples after ducking')
        decoded.run('ffmpeg', ['-v', 'error', '-y', '-i', str(voice_path), '-i', str(stem),
                    '-filter_complex', '[0:a:0][1:a:0]amix=inputs=2:duration=first:normalize=0[mix]',
                    '-map', '[mix]', '-c:a', 'pcm_f32le', str(mixed)], 'mix')
        _commit(video, bed_name, stem)
        _commit(video, mix_name, mixed)


def bind(req, generated):
    plan = bed_plan(req)
    bed_name, mix_name = records.artifact_names(req)[4:]
    req['bed_binding'] = plan
    if plan['mode'] == 'bed':
        req['mix_binding'] = {'file': mix_name, 'sha256': generated[mix_name]}
        if not generated[mix_name]:
            raise SpineError(f'{mix_name}: missing final mix')
        if not generated[bed_name]:
            raise SpineError(f'{bed_name}: missing retained bed')
        req['audio_binding'] = {'stem': {'file': bed_name, 'sha256': generated[bed_name]},
                                'gain_db': 0, 'offset_s': 0, 'tacets': plan['tacets']}
    else:
        req['mix_binding'] = {k: req['voice_binding'][k] for k in ('file', 'sha256')}
        if any(generated[name] is not None for name in (bed_name, mix_name)):
            raise SpineError(f'{bed_name}: unexpected bed for voice_only')
        req.pop('audio_binding', None)
