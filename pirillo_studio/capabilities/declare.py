"""The capability declaration (docs/capabilities.md section 1): read and refused whole, naming the place.

The file is the one the host adapter's `declarations` names (this package's capabilities.json holds the generic
entries). A stage's tools list names these IDs; `voice:<part>` in a tools list names a voice part (section 2).
"""
import re
import string
from pathlib import Path

from ..spine.files import SpineError, load, safe

PROBES = {"executable": ("encode-sample",), "encoder": ("encode-sample",),
          "model": ("transcribe-fixture", "speak-fixture"), "guide": ("present",), "account": ("present",)}
FIELDS = {"executable": ("find", "version"), "encoder": ("needs", "codec"), "model": ("runtime", "package", "weights", "run"),
          "guide": ("find",), "account": ("routes",)}
FILL = {"speak-fixture": {"text", "out"}, "transcribe-fixture": {"audio"}}  # the run args' placeholders
VERSION = re.compile(r"\d+(\.\d+)*")
ROUTE = re.compile(r"settings platforms\.([a-z0-9][a-z0-9_-]*)")


def version(text):
    """'7.1' -> (7, 1); anything else (empty, NaN, a word) is None."""
    return tuple(int(n) for n in text.split(".")) if isinstance(text, str) and VERSION.fullmatch(text) else None


def blank(v):
    """Present but empty: None, a string of only whitespace, or an empty list or object."""
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, (list, dict)) and not v)


def text(e, k, at):
    if not isinstance(e.get(k), str) or blank(e[k]):
        raise SpineError(f"{at}: {k} must be a non-empty string")
    return e[k]


def declarations(host):
    """{capabilities_version, capabilities}, validated; a malformed file refuses naming its path and entry."""
    f = Path(host.declarations)
    try:
        d = load(f.parent, f.name)
        caps, cv = d["capabilities"], d["capabilities_version"]
        if not isinstance(caps, dict) or not caps or not isinstance(cv, str) or blank(cv):
            raise TypeError("needs capabilities_version (string) and capabilities (non-empty object)")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SpineError(f"{f}: unreadable capability declaration ({e})") from None
    for cid, e in caps.items():
        at = f"{f} capability {cid}"
        if not isinstance(e, dict) or e.get("kind") not in PROBES:
            raise SpineError(f"{at}: kind must be one of {sorted(PROBES)}")
        for k in FIELDS[e["kind"]]:
            if k not in e:
                raise SpineError(f"{at}: needs {k}")
        text(e, "get", at)
        if e.get("qualify") not in PROBES[e["kind"]]:
            raise SpineError(f"{at}: qualify must be one of {PROBES[e['kind']]}")
        if not isinstance(e.get("needs", []), list) or any(n not in caps for n in e.get("needs", [])):
            raise SpineError(f"{at}: needs must list declared capabilities")
        kind = e["kind"]
        if e.get("qualify") in ("encode-sample", "speak-fixture") and kind != "executable" and \
                not any(isinstance(caps[n], dict) and caps[n].get("kind") == "executable" for n in e.get("needs", [])):
            raise SpineError(f"{at}: needs the ffmpeg its probe runs")
        if kind == "executable":
            v = e["version"]
            if "/" in text(e, "find", at) or not isinstance(v, dict) or version(v.get("min")) is None \
                    or not isinstance(v.get("args"), list) or blank(v["args"]) \
                    or not all(isinstance(a, str) and not blank(a) for a in v["args"]):
                raise SpineError(f"{at}: find must be a bare name and version {{args, min}} a numeric minimum")
        elif kind == "encoder":
            text(e, "codec", at)
        elif kind == "model":
            if "/" in text(e, "runtime", at):
                raise SpineError(f"{at}: runtime must be a bare name")
            r = e["run"]
            try:
                names = {f for a in r for _, f, _, _ in string.Formatter().parse(a) if f is not None}
            except (TypeError, ValueError):
                names = None
            if not isinstance(r, list) or names != FILL[e["qualify"]]:
                raise SpineError(f"{at}: run must be the engine's args after its runtime, using exactly "
                                 f"{sorted('{' + n + '}' for n in FILL[e['qualify']])}")
            text(e, "package", at)
            safe(text(e, "weights", at), at)
        elif kind == "guide":
            safe(text(e, "find", at), at)
        elif not ROUTE.fullmatch(text(e, "routes", at)):
            raise SpineError(f"{at}: routes must be 'settings platforms.<platform>'")
    for cid in caps:  # needs form no cycle: a capability is never its own prerequisite
        seen, todo = set(), list(caps[cid].get("needs", []))
        while todo:
            n = todo.pop()
            if n == cid:
                raise SpineError(f"{f} capability {cid}: needs itself")
            if n not in seen:
                seen.add(n)
                todo += caps[n].get("needs", [])
    return d
