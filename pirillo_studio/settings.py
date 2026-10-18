"""The one loader and writer of a studio's merged settings, and its commands (run as `studio.py settings ...`).

  settings check                 data/studio.json against DEFAULTS and data/settings-keys.json: `file: key: why`, exit 1
  settings explain [key]         value, where it comes from, kind, target and meaning
  settings set KEY JSON --why W [--actor A]   writes studio.json in its own style; the change goes to private history
  settings undo [--actor A]      sets the last change not yet undone back
  settings history [key]         every change: when, who, before -> after, why

`s` is the studio whose settings these are: any object with DEFAULTS, CONFIG, RULES, DATA_DIR, HOME and NEW
(posting.hold is checked by pirillo_studio.hold). Production passes its own studio module, whose DEFAULTS move here in a later port.
History is local only: <studio>/run/settings-history.jsonl (git ignores run/).
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import getpass
import json
import os
import re
import tempfile

from pirillo_studio import hold

CONFIG_LABEL = 'data/studio.json'
MERGED = ('chief', 'disk', 'tips')     # dict keys whose named sub-keys win over the defaults
_WS = re.compile(r'\s*')


def merge(defaults, mine, rules=None, refused=ValueError):
    """Today's load_config() rules: his keys win; platforms/map/drops and the MERGED dicts merge one level down;
    gates come from data/rules.json alone (B1); effects is off or on."""
    cfg = json.loads(json.dumps(defaults))
    for k, v in mine.items():
        if k == 'platforms':
            for p, pv in v.items():
                cfg['platforms'].setdefault(p, {}).update(pv)
        elif k in ('map', 'drops'):          # his kinds and slots win; a kind or offset he didn't name stays
            cfg[k].update(v)
        elif k in MERGED and isinstance(v, dict):
            cfg[k].update(v)
        elif k != 'gates':
            cfg[k] = v
    if cfg.get('effects', 'off') not in ('off', 'on'):
        raise refused('effects must be off or on')
    return dict(cfg, gates=rules) if rules is not None else cfg


def _read(path):
    with open(path) as f:
        return json.load(f)


def keys_table(data_dir):
    return _read(os.path.join(data_dir, 'settings-keys.json'))['keys']


def load(s):
    """The merged settings, as studio.load_config() gives them (without its first-run write of a missing file)."""
    return merge(s.DEFAULTS, _read(s.CONFIG) if os.path.exists(s.CONFIG) else {},
                 _read(s.RULES) if os.path.exists(s.RULES) else None)


def _unique(pairs):
    keys = [k for k, _ in pairs]
    dup = next((k for k in keys if keys.count(k) > 1), None)
    if dup is not None:
        raise ValueError(f'duplicate key {dup!r}')
    return dict(pairs)


def parse(text, label=CONFIG_LABEL):
    """(his studio.json object, []) or ({}, [`file: (file): why`]): bad JSON, a repeated key or a non-object root."""
    try:
        mine = json.loads(text, object_pairs_hook=_unique)
    except ValueError as e:     # JSONDecodeError is a ValueError, and _unique raises one
        return {}, [f'{label}: (file): {e}']
    return (mine, []) if isinstance(mine, dict) else ({}, [f'{label}: (file): the root is {_kind(mine)}, not an object'])


@contextlib.contextmanager
def locked(path):
    """One writer of studio.json at a time: settings set/undo and liauth.py hold this lock beside it."""
    with open(path + '.lock', 'a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def write(path, text):
    """Replace path with text: a unique temp file beside it, the old mode kept, then os.replace."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=os.path.basename(path) + '.', suffix='.tmp')
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.chmod(tmp, os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o644)
    os.replace(tmp, path)


def _kind(v):
    return 'number' if isinstance(v, (int, float)) and not isinstance(v, bool) else type(v).__name__


def problems(mine, table, s, label=CONFIG_LABEL):
    """Every `file: key: why` in his studio.json and the key table."""
    defaults = s.DEFAULTS
    out = [f'{label}: {k}: not in data/settings-keys.json' for k in mine if k not in table]
    out += [f'data/settings-keys.json: {k}: a DEFAULTS key with no row' for k in defaults if k not in table]
    out += [f'data/settings-keys.json: {k}: kind/target must be config/settings or state/studio-state.json'
            for k, r in table.items() if (r.get('kind'), r.get('target')) not in
            (('config', 'settings'), ('state', 'studio-state.json')) or not r.get('meaning')]
    out += [f'{label}: {k}: {_kind(v)}, the default is {_kind(defaults[k])}' for k, v in mine.items()
            if k in defaults and defaults[k] is not None and _kind(v) != _kind(defaults[k])]
    plats = mine.get('platforms') if isinstance(mine.get('platforms'), dict) else {}
    out += [f'{label}: platforms: {p} is {_kind(v)}, not an object' for p, v in plats.items() if not isinstance(v, dict)]
    if 'gates' in mine:
        out.append(f'{label}: gates: the dated rules live in data/rules.json alone (B1)')
    if mine.get('effects', 'off') not in ('off', 'on'):
        out.append(f'{label}: effects: {mine["effects"]!r} is neither off nor on')
    if 'posting.hold' in mine:
        try:
            hold.validate(mine['posting.hold'])
        except ValueError as exc:
            out.append(f'{label}: {exc}')
    return out


def _history_path(s):
    return os.path.join(s.HOME, 'Studio' if s.NEW else '', 'run', 'settings-history.jsonl')


def _history(s):
    path = _history_path(s)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _spans(text):
    """{key: (key_start, value_start, value_end)} for each top-level key of a JSON object's text."""
    dec, out = json.JSONDecoder(), {}
    i = _WS.match(text, text.index('{') + 1).end()
    while text[i] != '}':
        start = i
        key, i = dec.raw_decode(text, i)
        i = _WS.match(text, _WS.match(text, i).end() + 1).end()
        end = dec.raw_decode(text, i)[1]
        out[key] = (start, i, end)
        i = _WS.match(text, end).end()
        if text[i] == ',':
            i = _WS.match(text, i + 1).end()
    return out


def _edit(text, key, value, present=True):
    """studio.json's text with one top-level key set (or removed), every other line as he wrote it."""
    spans = _spans(text)
    if key in spans:
        k0, v0, v1 = spans[key]
        if not present:
            prev = max((e for _, _, e in spans.values() if e < k0), default=None)
            nxt = min((s for s, _, _ in spans.values() if s > k0), default=None)
            return text[:prev] + text[v1:] if prev is not None else text[:k0] + text[nxt if nxt is not None else v1:]
        new = json.dumps(value, indent=1).replace('\n', '\n ') if '\n' in text[v0:v1] else json.dumps(value)
        return text[:v0] + new + text[v1:]
    if not present:
        return text
    end = max((e for _, _, e in spans.values()), default=None)
    item = f'{json.dumps(key)}: {json.dumps(value, indent=1).replace(chr(10), chr(10) + " ")}'
    if end is None:
        return '{\n ' + item + '\n}\n'
    return text[:end] + ',\n ' + item + text[end:]


def _mine(s):
    """studio.json's text and object, or a refusal naming what is wrong with the file."""
    with open(s.CONFIG) as f:
        text = f.read()
    mine, bad = parse(text)
    if bad:
        raise SystemExit('\n'.join(bad) + '\nNothing was changed.')
    return text, mine


def put(s, *args, **kw):
    with locked(s.CONFIG):
        return _put(s, *args, **kw)


def _put(s, key, value, why, actor, present=True, undoes=None):
    """Set (or remove) one studio.json key and log it; refuses a key with no row or a value check refuses.
    The caller holds locked()."""
    table = keys_table(s.DATA_DIR)
    if key not in table or key == 'gates':
        raise SystemExit(f'{CONFIG_LABEL}: {key}: not a studio.json key (data/settings-keys.json). Nothing was changed.')
    if not why.strip():
        raise SystemExit('set needs --why "<text>". Nothing was changed.')
    text, mine = _mine(s)
    want = dict(mine)
    if present:
        want[key] = value
    else:
        want.pop(key, None)
    bad = [p for p in problems(want, table, s) if f': {key}: ' in p]
    if bad:
        raise SystemExit('\n'.join(bad) + '\nNothing was changed.')
    new = _edit(text, key, value, present)
    if json.loads(new) != want:          # the text edit must mean exactly the change asked for, or nothing is written
        raise SystemExit(f'{CONFIG_LABEL}: {key}: could not edit the file in its own style. Nothing was changed.')
    entry = {'at': dt.datetime.now().astimezone().isoformat(timespec='seconds'), 'actor': actor, 'key': key}
    entry.update({'before': mine[key]} if key in mine else {})
    entry.update({'after': value} if present else {})
    entry.update({'why': why}, **({'undoes': undoes} if undoes is not None else {}))
    path = _history_path(s)
    try:        # history first: a record whose write then fails is refused by undo, its `after` not being the file's
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except OSError as e:
        raise SystemExit(f'{CONFIG_LABEL}: {key}: history not written ({e}). Nothing was changed.')
    write(s.CONFIG, new)
    return entry


def undo(s, actor):
    with locked(s.CONFIG):
        log = _history(s)
        done = {e['undoes'] for e in log if 'undoes' in e}
        todo = [i for i, e in enumerate(log) if 'undoes' not in e and i not in done]
        if not todo:
            raise SystemExit('settings: no change left to undo.')
        i = todo[-1]
        e = log[i]
        mine = _mine(s)[1]
        if (e['key'] in mine, mine.get(e['key'])) != ('after' in e, e.get('after')):
            raise SystemExit(f"{CONFIG_LABEL}: {e['key']}: changed since {e['at']} by hand; set it instead. Nothing was changed.")
        return _put(s, e['key'], e.get('before'), f"undo: {e['why']}", actor, 'before' in e, undoes=i)


def explain(s, key=None):
    table, mine, cfg = keys_table(s.DATA_DIR), (_read(s.CONFIG) if os.path.exists(s.CONFIG) else {}), load(s)
    lines = []
    for k in ([key] if key else sorted(set(cfg) | set(table))):
        row = table.get(k)
        if k not in cfg and not row:
            raise SystemExit(f'settings: {k}: no such key')
        where = ('data/rules.json' if k == 'gates' else 'studio.json over default' if k in mine and k in s.DEFAULTS
                 and isinstance(mine[k], dict) and k in ('platforms', 'map', 'drops') + MERGED
                 else 'studio.json' if k in mine else 'default' if k in s.DEFAULTS else 'unset')
        value = json.dumps(cfg.get(k))
        value = value if len(value) <= 120 or key else value[:117] + '...'
        lines.append(f'{k} = {value}\n  from {where}; ' + (f"{row['kind']} -> {row['target']}: {row['meaning']}"
                                                          if row else 'not in data/settings-keys.json'))
    return lines


def main(argv, s):
    p = argparse.ArgumentParser(prog='studio.py settings', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('check')
    sub.add_parser('explain').add_argument('key', nargs='?')
    st = sub.add_parser('set')
    st.add_argument('key')
    st.add_argument('value', help='a JSON value')
    st.add_argument('--why', required=True)
    st.add_argument('--actor', default=getpass.getuser())
    sub.add_parser('undo').add_argument('--actor', default=getpass.getuser())
    sub.add_parser('history').add_argument('key', nargs='?')
    a = p.parse_args(argv)
    if a.cmd == 'check':
        with open(s.CONFIG) as f:
            mine, found = parse(f.read())
        found += problems(mine, keys_table(s.DATA_DIR), s)
        print('\n'.join(found) or f'{CONFIG_LABEL}: every key known, typed and in data/settings-keys.json')
        return 1 if found else 0
    if a.cmd == 'explain':
        print('\n'.join(explain(s, a.key)))
    elif a.cmd in ('set', 'undo'):
        try:
            value = json.loads(a.value) if a.cmd == 'set' else None
        except ValueError:
            raise SystemExit(f'{CONFIG_LABEL}: {a.key}: {a.value!r} is not JSON (quote a string: \'"on"\'). Nothing was changed.')
        e = put(s, a.key, value, a.why, a.actor) if a.cmd == 'set' else undo(s, a.actor)
        print(f"{e['key']}: {json.dumps(e.get('before'))} -> {json.dumps(e.get('after'))} ({e['actor']}: {e['why']})")
    else:
        print('\n'.join(f"{e['at']} {e['actor']} {e['key']}: {json.dumps(e.get('before'))} -> {json.dumps(e.get('after'))}"
                        f"  {e['why']}" for e in _history(s) if a.key in (None, e['key'])) or 'no settings changes yet')
    return 0


def load_config(s):
    if not os.path.exists(s.CONFIG):      # first run: his file starts as the defaults
        os.makedirs(s.HOME, exist_ok=True)
        s.wt(s.CONFIG, json.dumps(s.DEFAULTS, indent=1))
    return merge(s.DEFAULTS, s.rj(s.CONFIG), s.rj(s.RULES) if os.path.exists(s.RULES) else None, s.Refused)
