"""Approved rational expression speed; contract and flow in docs/rendering-retime.md."""
import re
from fractions import Fraction

from ..spine.files import SpineError


def factor(span):
    fields = [name for name in ('speed', 'retime') if name in span]
    if not fields:
        return Fraction(1)
    at = f"retiming: {span['id']}: {'/'.join(fields)}"
    if len(fields) != 1:
        raise SpineError(f'{at}: specify only one factor')
    value = span[fields[0]]
    try:
        if not (type(value) is int or isinstance(value, str) and re.fullmatch(r'[0-9]+(?:/[0-9]+)?', value)):
            raise ValueError()
        speed = Fraction(value)
        if speed <= 0:
            raise ValueError()
        return speed
    except (ValueError, ZeroDivisionError):
        raise SpineError(f'{at}: expected positive rational integer or ratio string') from None


def validate(req, span):
    speed = factor(span)
    if 'speed' in span or 'retime' in span:
        if (span['enter'] != 'pop' or span['words'] or 'patch' in span
                or any(w['start'] < span['src'][1] and w['end'] > span['src'][0] for w in req['words_data'])):
            raise SpineError(f"retiming: {span['id']}: speed/retime with voice unsupported")
    return speed


def step(req, row):
    return factor(next(s for s in req['plan_data']['spans'] if s['id'] == row['span']))
