"""Approved SDR transforms, bound to source, LUT bytes and parameters; rendering-grade.md."""
import math
from fractions import Fraction

from ..spine.files import SpineError, canon, digest, filehash
from . import mapping, records, retiming


def number(value, at):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise SpineError(f'grade: {at}: finite number required')
    return value


def approved(value, source, *, src=None):
    fields = {'decision', 'source_sha256', 'parameters', 'sha256'} | ({'lut'} if src is None else set())
    if not isinstance(value, dict) or set(value) != fields:
        raise SpineError('grade: unsupported or missing transform fields')
    if value['decision'] != 'approve':
        raise SpineError('grade: explicit approval required')
    if value['source_sha256'] != source:
        raise SpineError('grade: stale source identity')
    payload = {k: v for k, v in value.items() if k not in ('decision', 'sha256')}
    if src is not None:
        payload['src'] = src
    try:
        expected = digest(canon(payload))
    except (TypeError, ValueError) as error:
        raise SpineError('grade: parameters must contain finite JSON values') from error
    if value['sha256'] != expected:
        raise SpineError('grade: stale or missing approval hash')
    params = value['parameters']
    if not isinstance(params, dict):
        raise SpineError('grade: parameters must be an object')
    if src is None:
        if set(params) != {'skin_target', 'knee', 'max_stop', 'n'}:
            raise SpineError('grade: missing or unsupported finish parameters')
        for key in ('skin_target', 'knee'):
            if not 0 < number(params[key], key) < 1:
                raise SpineError(f'grade: {key}: expected 0 < value < 1')
        if number(params['max_stop'], 'max_stop') <= 0:
            raise SpineError('grade: max_stop must be positive')
        if type(params['n']) is not int or not 2 <= params['n'] <= 256:
            raise SpineError('grade: n must be an integer in 2..256')
    else:
        if set(params) != {'exposure', 'contrast', 'space'} or params['space'] not in ('rgb', 'luma'):
            raise SpineError('grade: unsupported local parameters or space')
        for key in ('exposure', 'contrast'):
            pair = params[key]
            if not isinstance(pair, list) or len(pair) != 2:
                raise SpineError(f'grade: {key}: endpoint pair required')
            for item in pair:
                n = number(item, key)
                if (key == 'exposure' and n >= 2.4) or (key == 'contrast' and n <= 0):
                    raise SpineError(f'grade: {key}: nonpositive curve exponent or contrast')
    return value


def lut(video, value, inputs):
    asset = value['lut']
    if not isinstance(asset, dict) or set(asset) != {'file', 'sha256'}:
        raise SpineError('grade: LUT file and sha256 required')
    path = records.path(video, asset['file'])
    if (path.suffix != '.cube' or not asset['sha256'] or inputs.get(asset['file']) != asset['sha256']
            or filehash(path) != asset['sha256']):
        raise SpineError('grade: missing, stale or unbound LUT bytes')
    size, samples, domain = None, 0, set()
    try:
        for line in path.read_text().splitlines():
            fields = line.split('#', 1)[0].split()
            if not fields or fields[0] == 'TITLE':
                continue
            if fields[0] == 'LUT_3D_SIZE' and size is None and len(fields) == 2:
                size = int(fields[1])
            elif fields[0] in ('DOMAIN_MIN', 'DOMAIN_MAX'):
                tag = fields[0]
                if tag in domain or fields[1:] != (['0', '0', '0'] if tag == 'DOMAIN_MIN' else ['1', '1', '1']):
                    raise ValueError('non-unit or repeated domain')
                domain.add(tag)
            elif len(fields) == 3:
                values = [float(n) for n in fields]
                if any(not math.isfinite(n) or not 0 <= n <= 1 for n in values):
                    raise ValueError('invalid sample')
                samples += 1
            else:
                raise ValueError('unsupported cube entry')
        if size != value['parameters']['n'] or samples != size ** 3:
            raise ValueError('cube size/sample count contradicts n')
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise SpineError(f'grade: invalid LUT: {error}') from error
    if filehash(path) != asset['sha256']:
        raise SpineError('grade: LUT changed during validation')
    return path


def resolve(video, req, inputs, job):
    selected = [span for span, _ in mapping.pieces(req) if not span.get('hide_picture')]
    if video.prepare is None and selected and ('grade' in job or any('grade' in s for s in selected)):
        raise SpineError('grade: adapter requires prepare to bind declared transforms')
    for span in selected:
        if 'grade' in span:
            approved(span['grade'], req['source_sha256'], src=span['src'])
    if 'grade' not in job or not selected:
        return
    value = approved(job['grade'], req['source_sha256'])
    if video.settings['shared'].get('grade') != value['parameters']:
        raise SpineError('grade: finish parameters contradict shared.grade settings')
    asset = value['lut']
    if not isinstance(asset, dict) or not isinstance(asset.get('file'), str):
        raise SpineError('grade: LUT file required')
    inputs.add(asset['file'])
    lut(video, value, inputs.hashes)
    req['grade'] = value


def binding(req, row):
    if row['kind'] != 'source':
        return None
    span = next(s for s in req['plan_data']['spans'] if s['id'] == row['span'])
    local = span.get('grade')
    if 'grade' in span:
        approved(local, req['source_sha256'], src=span['src'])
        local = {**local, 'src': span['src']}
    finish = req.get('grade')
    if finish is not None:
        approved(finish, req['source_sha256'])
    return {'finish': finish, 'local': local} if finish is not None or local is not None else None


def validate(video, req, row):
    wanted = binding(req, row) if 'plan_data' in req else None
    if row.get('grade') != wanted:
        raise SpineError('grade: missing, stale or contradictory picture binding')
    if wanted and wanted['finish']:
        lut(video, wanted['finish'], req['inputs'])


def filters(video, req, row):
    value = row.get('grade')
    if value is None:
        return 'null'
    target = row['color_conversion']['output']['color_range']
    to_rgb = f'scale=in_color_matrix=bt709:in_range={target}:out_range=pc,format=gbrp'
    to_yuv = f'scale=in_range=pc:out_color_matrix=bt709:out_range={target},format=yuv444p'
    chain = []
    if value['finish']:
        path = lut(video, value['finish'], req['inputs'])
        # Escape both the option parser and the surrounding filtergraph parser.
        escaped = str(path)
        for special in ("\\", "'", ":"):
            escaped = escaped.replace(special, "\\" + special)
        for special in ("\\", "'", "[", "]", ",", ";"):
            escaped = escaped.replace(special, "\\" + special)
        chain += [to_rgb, f"lut3d=file={escaped}:interp=tetrahedral", to_yuv]
    local = value['local']
    if local:
        params = local['parameters']
        a, b = local['src']
        rate = Fraction(req.get('source_rate', req['settings']['video']['fps']))
        if 'source_grid_frames' in row:
            output = Fraction(req['settings']['video']['fps'])
            speed = retiming.step(req, row)
            ratio = rate / output
            grid = row['source_grid_frames'][0]
            instant = (f'floor(({grid * speed.denominator}+N*{speed.numerator})*{ratio.numerator}'
                       f'/{speed.denominator * ratio.denominator})*{rate.denominator}/{rate.numerator}')
        else:
            instant = f'(N+{row["source_frames"][0]})*{rate.denominator}/{rate.numerator}'
        weight = f'clip((({instant})-{a})/({b}-{a}),0,1)'
        def ramp(key):
            lo, hi = params[key]
            return f'({lo}+({hi}-{lo})*{weight})'
        exponent, contrast = f'(1-{ramp("exposure")}/2.4)', ramp('contrast')
        def curve(signal):
            return f'pow(clip({contrast}*(({signal})-0.5)+0.5,0,1),{exponent})'
        if params['space'] == 'rgb':
            expression = ':'.join(f"{c}='255*{curve(c + '(X,Y)/255')}'" for c in ('r', 'g', 'b'))
            chain += [to_rgb, 'geq=' + expression, to_yuv]
        else:
            black, extent = (16, 219) if target == 'tv' else (0, 255)
            signal = f'(lum(X,Y)-{black})/{extent}'
            chain += ['format=yuv444p', f"geq=lum='{black}+{extent}*{curve(signal)}':cb='cb(X,Y)':cr='cr(X,Y)'"]
    return ','.join(chain)
