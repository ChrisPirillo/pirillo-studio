#!/usr/bin/env python3
"""Scoped posting holds, shared by scheduling, sending, Calendar and published edits.

The C1 setting posting.hold maps scope names to "off" or
{"state": "on", "reason": "...", "actor": "..."}. Unscoped rows belong to
"studio", the existing single brand. Every scope needs an explicit record;
missing or malformed policy holds, never grants permission to send.
Use studio.py settings set posting.hold '<JSON>' --why '<why>' to change it; C1 owns undo.
"""
import copy
import functools
import json


DEFAULT_SCOPE = 'studio'


def scope(row, video=None):
    for source in (row, video or {}):
        for key in ('scope', 'brand', 'client'):
            if key not in source:
                continue
            value = source[key]
            if isinstance(value, dict):
                value = value.get('brand') or value.get('client')
            if not isinstance(value, str) or not value.strip():
                raise ValueError('posting.hold: invalid brand/client scope')
            return value
    return DEFAULT_SCOPE


def validate(records):
    if not isinstance(records, dict) or not records:
        raise ValueError('posting.hold: requires explicit scope records')
    for name, record in records.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError('posting.hold: invalid scope name')
        if record == 'off':
            continue
        if (not isinstance(record, dict) or record.get('state') != 'on'
                or any(not isinstance(record.get(k), str) or not record[k].strip() for k in ('reason', 'actor'))):
            raise ValueError(f'posting.hold: {name} needs off or on with reason and actor')


def reason(cfg, row, video=None):
    """None only for explicit off; damaged policy remains a visible hold."""
    try:
        name = scope(row, video)
        records = cfg['posting.hold']
        validate(records)
        record = records[name]
    except (KeyError, TypeError, ValueError) as exc:
        return f'Posting held: unreadable posting.hold ({exc})'
    return None if record == 'off' else f"Posting held: {record['reason']} (by {record['actor']})"


def current(cfg, path, default):
    """A long tick must not send using an off value cached before a new hold."""
    try:
        with open(path) as source:
            records = json.load(source).get('posting.hold', default['posting.hold'])   # unset in the file: studio.DEFAULTS
    except (OSError, ValueError, KeyError, TypeError):
        records = None  # An unreadable protection holds; reason() reports it.
    return dict(cfg, **{'posting.hold': records})


def edit_video(led, edit):
    for video in led.get('videos', {}).values():
        for row in video.get('assets', []):
            if edit.get('url') and row.get('url') == edit['url']:
                return {'scope': scope(row, video)}
    return {}


def asks(cfg, led):
    """One release request per held scope, including a hold with no queued rows."""
    sources = [(a, v) for v in led.get('videos', {}).values() for a in v.get('assets', [])]
    sources += [(x, {}) for x in led.get('storyposts', {}).values()]
    sources += [(x, edit_video(led, x)) for x in led.get('edits', {}).values()]
    records = cfg.get('posting.hold')
    if isinstance(records, dict):
        sources += [({'scope': name}, {}) for name in records]
    out = {}
    for row, video in sources or [({}, {})]:
        why = reason(cfg, row, video)
        if why:
            try:
                name = scope(row, video)
            except ValueError:
                name = 'invalid scope'
            out[name] = why
    return sorted(out.items())


def preserve_plan(fn):
    """The planner works on a copy; held rows never lose schedules or approvals.

    Keep immediate posts at that slot on release too. Native schedulers use the
    normal planner on release to meet their future-slot requirement.
    """
    @functools.wraps(fn)
    def run(cfg, led, *args, **kwargs):
        held = {a['id']: a for v in led.get('videos', {}).values() for a in v.get('assets', [])
                if a.get('state') == 'planned' and a.get('scheduled_for')
                and (reason(cfg, a, v) or a.get('posting_hold') and a.get('method') == 'api_now')}
        if not held:
            return fn(cfg, led, *args, **kwargs)
        planned = copy.deepcopy(led)
        result = fn(cfg, planned, *args, **kwargs)
        for v in planned['videos'].values():
            for index, row in enumerate(v.get('assets', [])):
                if row['id'] in held:
                    v['assets'][index] = dict(held[row['id']], posting_hold=True)
        led.update(planned)
        return result
    return run


def story_wait(cfg, row, s):
    """Remember approved queued stories so a deadline cannot drop a held post."""
    if row.get('state') != 'planned':
        return False
    if reason(cfg, row):
        if row.get('ok'):
            row['posting_hold'] = True
        return True
    if not row.get('posting_hold') or not row.get('ok'):
        return False
    if row.get('method') == 'buffer' and row.get('when') and s.parse(row['when']) <= s.now() + s.lead(cfg, row['platform'], row['method']):
        row['when'] = s.iso(s.round5(s.now() + s.lead(cfg, row['platform'], row['method'])))
    return True
