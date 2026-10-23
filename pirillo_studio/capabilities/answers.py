"""capability, qualify and requalify (docs/capabilities.md sections 2 and 3): one record per capability and key.

The host adapter supplies `declarations` (the declaration file's path), `search` (the one search path), `state` (the
machine's state folder: records are `capabilities/<id>.json` there), `studio` (the folder `voice.json` sits in),
`setting(name)` (raises KeyError when unset), `key_saved(platform)` (an account key's saved date, ISO, or None:
never its value), `now()` (an aware datetime), `ask(task)` (files a Board task in docs/board.md's shape, reconciled by
its id, so filing again files nothing new), `decided(task id)` (the owner's act on that task's current revision,
`{"revision", "verb"}`, or None) and `href(path)` (the served URL of a judged file). A decision is cited as
`<task id>@<revision>`, the shape capabilities.md section 1 gives. A record counts only whole: every field in its
shape, its probe the declared one, and its key the one its own fields make under the current declaration. No preflight code is in the core yet (item 6): its
tools probe is the caller of capability and qualify, and the tests stand in for it.
"""
import contextlib
import datetime as dt
import fcntl
import json
import math
import os
import tempfile
import time
from pathlib import Path

from ..spine.files import SpineError, inside, load, safe
from .declare import declarations
from .resolve import PROBE_VERSIONS, SHA, Missing, key, probe, resolve, workload

ASK = "the owner checks {} and asks for a re-run on the Board"
FIELDS = {"capability", "capabilities_version", "key", "at", "ok", "state", "next_action", "detail", "resolved",
          "needs", "probe", "attempt", "requalify"}
PROBE_FIELDS = {"name", "version", "ok", "seconds", "detail", "workload"}


def iso(host):
    return host.now().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rel(cid):
    p = f"capabilities/{cid}.json"
    safe(p, f"capability {cid}")
    return p


def read(host, cid, d):
    """(record or None, problem or None): a torn, malformed or inconsistent record is a problem naming its file."""
    f = inside(Path(host.state), rel(cid))
    if not f.exists():
        return None, None
    try:
        r = load(Path(host.state), rel(cid))
        e, p = d["capabilities"][cid], r["probe"]
        if set(r) != FIELDS or not isinstance(p, dict) or set(p) != PROBE_FIELDS:
            raise TypeError(f"needs exactly {sorted(FIELDS)} and probe {sorted(PROBE_FIELDS)}")
        if r["capability"] != cid or r["capabilities_version"] != d["capabilities_version"] \
                or p["name"] != e["qualify"] or p["version"] != PROBE_VERSIONS[e["qualify"]]:
            raise TypeError("not a record of this capability's declared probe")
        if r["state"] not in ("qualified", "unqualified") or r["ok"] is not (r["state"] == "qualified") \
                or p["ok"] is not r["ok"] or r["next_action"] != (None if r["ok"] else ASK.format(cid)) \
                or not isinstance(r["at"], str) or not isinstance(p["detail"], str) \
                or r["detail"] != f"{p['name']}: {p['detail']}" \
                or isinstance(p["seconds"], bool) or not isinstance(p["seconds"], (int, float)) \
                or not math.isfinite(p["seconds"]) or p["seconds"] < 0:
            raise TypeError("its state, ok, next action, detail and probe result disagree")
        if type(r["attempt"]) is not int or r["attempt"] < 1 or (r["requalify"] is None) is not (r["attempt"] == 1) \
                or (r["requalify"] is not None and (not isinstance(r["requalify"], str) or "@" not in r["requalify"])):
            raise TypeError("attempt and requalify disagree")
        if not isinstance(r["key"], str) or not SHA.fullmatch(r["key"]) or not isinstance(r["resolved"], dict) \
                or not isinstance(r["needs"], dict) or r["key"] != key(d["capabilities_version"], e, r["resolved"],
                                                                         r["needs"], p["workload"]):
            raise TypeError("its key is not the one its own fields make")
        return r, None
    except (OSError, ValueError, KeyError, TypeError) as x:
        return None, f"{f}: unreadable record ({x})"


def answer(cid, state, next_action, detail, **more):
    return dict(ok=state == "qualified", state=state, next_action=next_action, detail=detail, capability=cid, **more)


def capability(host, cid, d=None):
    """The current answer for cid; reads only, never probes. key, resolved and needs are set once it resolves."""
    d = d or declarations(host)
    if cid not in d["capabilities"]:
        raise SpineError(f"capability {cid}: not declared in {host.declarations}")
    e, needs, keys = d["capabilities"][cid], {}, {}
    for n in e.get("needs", []):
        a = capability(host, n, d)
        if not a["ok"]:
            gone = a["state"] == "missing"
            return answer(cid, "missing" if gone else "unqualified", e["get"] if gone else a["next_action"],
                          f"needs {n}: {a['detail']}")
        needs[n], keys[n] = a["resolved"], a["key"]
    rec, problem = read(host, cid, d)
    try:
        resolved = resolve(host, e, rec["resolved"] if rec else {}, needs)
    except Missing as x:
        return answer(cid, "missing", e["get"], f"{cid}: {x}")
    work = workload(host, e)
    k = key(d["capabilities_version"], e, resolved, keys, work)
    found = dict(key=k, resolved=resolved, needs=keys, workload=work)
    if problem:
        return answer(cid, "unqualified", f"qualify {cid}", problem, **found)
    if rec is None or rec["key"] != k:
        return answer(cid, "unqualified", f"qualify {cid}", f"{cid}: no qualification for this install ({k})", **found)
    if not rec["ok"]:
        return answer(cid, "unqualified", ASK.format(cid), f"{rec['probe'].get('name')}: {rec['probe'].get('detail')}",
                      **found)
    return answer(cid, "qualified", None, f"{cid}: qualified by {rec['probe'].get('name')}", **found)


def qualify(host, cid):
    """The record for cid's current key, probing once when it has none; a missing item or prerequisite is its answer."""
    a = capability(host, cid)
    if "key" not in a or a["state"] == "missing":
        return a
    with locked(host, cid):  # read, check, probe and replace under one lock: one probe per key
        rec, _ = read(host, cid, declarations(host))
        if rec and rec["key"] == a["key"]:
            return rec
        return probed(host, cid, a, 1, None)


def requalify(host, cid, decision):
    """Probe a failed key again on the owner's current Board decision; attempt + 1, requalify names the decision.
    The decision is checked against the Board first, also on a replay, and consumed under cid's lock."""
    if not isinstance(decision, str) or "@" not in decision:
        raise SpineError(f"requalify {cid}: needs the owner's Board decision as <task id>@<revision>")
    a = capability(host, cid)
    if "key" not in a:
        raise SpineError(f"requalify {cid}: {a['detail']}")
    current(host, decision, task_id(cid, a["key"]), "try-again")
    with locked(host, cid):
        rec, problem = read(host, cid, declarations(host))
        if rec is None or rec["key"] != a["key"]:
            raise SpineError(f"requalify {cid}: no record for the current key ({problem or a['detail']})")
        if rec["requalify"] == decision:
            return rec
        if rec["ok"]:
            raise SpineError(f"requalify {cid}: its record passed")
        return probed(host, cid, a, rec["attempt"] + 1, decision)


@contextlib.contextmanager
def locked(host, cid):
    """cid's one writer (qualify and requalify, or "voice" for voice.json), held across read and write
    (spine/engine.py's pattern)."""
    f = inside(Path(host.state), rel(cid)[:-len(".json")] + ".lock")
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def current(host, decision, task, verb):
    """Refuse unless decision is the owner's `verb` on the current revision of task."""
    t, _, r = decision.rpartition("@")
    got = host.decided(t)
    if t != task:
        raise SpineError(f"decision {decision} answers task {t}, not {task}")
    if not got or got.get("revision") != r:
        raise SpineError(f"decision {decision} is not the current revision of {t} ({got and got.get('revision')})")
    if got.get("verb") != verb:
        raise SpineError(f"decision {decision} is {got.get('verb')!r}, not {verb!r}")


def task_id(cid, k):
    return f"capabilities:{cid}:{k[7:19]}"


def probed(host, cid, a, attempt, requalify_):
    d = declarations(host)
    e = d["capabilities"][cid]
    t = time.monotonic()
    try:
        ok, words = probe(host, e, a["resolved"], a["workload"])
    except SpineError:
        raise
    except Exception as x:  # a probe that raises is a failed probe, recorded with its words
        ok, words = False, f"raised {type(x).__name__}: {x}"
    p = {"name": e["qualify"], "version": PROBE_VERSIONS[e["qualify"]], "ok": ok,
         "seconds": round(time.monotonic() - t, 3), "detail": words, "workload": a["workload"]}
    rec = {"capability": cid, "capabilities_version": d["capabilities_version"], "key": a["key"], "at": iso(host),
           "ok": ok, "state": "qualified" if ok else "unqualified", "next_action": None if ok else ASK.format(cid),
           "detail": f"{p['name']}: {words}", "resolved": a["resolved"], "needs": a["needs"], "probe": p,
           "attempt": attempt, "requalify": requalify_}
    if not ok:  # filed before the rename: a crash in between re-files the same id, which the Board reconciles
        host.ask(failed_task(host, cid, a["key"], words))
    return put(host, cid, rec)


def failed_task(host, cid, k, words):
    return {"id": task_id(cid, k), "kind": "ask", "what": f"Check {cid} on this machine",
            "why": f"{words}; the stages that need {cid} stay blocked until it passes",
            "source": {"record": rel(cid), "filed_by": "capabilities", "related": []},
            "judge": {"type": "words", "href": host.href(inside(Path(host.state), rel(cid)))},
            "actions": [{"verb": "try-again", "label": "Run the check again",
                         "undo": {"verb": "undo", "until": None, "why": "Until the re-run starts"}}],
            "cant": {"words": "", "goes_to": "capabilities"}, "loop": "filed"}


def put(host, cid, rec):
    """Write beside, then rename; its caller holds cid's lock."""
    f = inside(Path(host.state), rel(cid))
    f.parent.mkdir(parents=True, exist_ok=True)
    inside(Path(host.state), rel(cid))  # a folder made a symlink while we waited is refused, not written through
    tmp = write_beside(f, rec)
    try:
        os.replace(tmp, f)
        return rec
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def write_beside(f, rec):
    # shortcut: a writer killed before its rename leaves its dot-temp here, never read; sweep it in the host's tidy
    # once seam 5 switches the host over, if they pile up.
    fd, tmp = tempfile.mkstemp(dir=f.parent, prefix=f".{f.name}.")
    with os.fdopen(fd, "w") as out:
        out.write(json.dumps(rec, indent=1, sort_keys=True, allow_nan=False))
        out.flush()
        os.fsync(out.fileno())
    return tmp
