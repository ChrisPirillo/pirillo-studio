"""The Board's task model (docs/board.md sections 1 and 2): one task shape, its loop, and the coverage check.

Built new to the spec, not moved: every source's tasks are checked here, so a renderer never decides what a card has.
"""
import datetime as dt

KINDS = frozenset((
    "video", "long post", "vertical post", "short post", "text post", "story post", "tip post", "vault post",
    "digest", "poll", "audio post", "image post", "approval", "hand post", "stuck post", "failed post", "ask",
    "story idea", "vault option", "poll idea", "tip idea", "idea", "banked idea", "doing", "image redo", "task",
    "crew task"))
JUDGES = frozenset(("image", "video", "words", "file"))
ROLES = frozenset(("content", "original", "candidate", "source", "steps", "destination"))
# The loop chart's arrows; helper_down is the page's connection state, never a task's.
MOVES = {"filed": {"shown"}, "shown": {"acted", "cant"}, "cant": {"shown"}, "acted": {"done", "undone"},
         "done": {"undone"}, "undone": {"shown"}}
LOOP = frozenset(MOVES)
OPEN = frozenset(("filed", "shown", "cant", "undone"))


def _text(v):
    return isinstance(v, str) and v.strip() != ""


def _at(v):
    return dt.datetime.fromisoformat(v.replace("Z", "+00:00"))


def _utc(v):
    try:
        return v is None or (_text(v) and _at(v).utcoffset() == dt.timedelta(0))
    except ValueError:
        return False


def _get(d, k):
    return d.get(k) if isinstance(d, dict) else None


def _judge(j, where):
    h = _get(j, "href")
    # A browser reads a backslash as "/" and drops control characters, so either can turn "/x" into another origin.
    if not _text(_get(j, "type")) or j["type"] not in JUDGES or not _text(h) or not h.startswith("/") or h.startswith("//") \
            or "\\" in h or any(ord(c) < 32 or ord(c) == 127 for c in h):
        return [f"{where}: needs a type in {sorted(JUDGES)} and a served href on this origin"]
    return []


def problems(t):
    """Why a task breaks the shape, one line each; empty when it holds."""
    if not isinstance(t, dict):
        return ["not a task"]
    i = t["id"] if _text(t.get("id")) else "?"
    out = [f"{i}: {k} must be text" for k in ("id", "kind", "what", "why", "loop") if not _text(t.get(k))]
    out += [f"{i}: {k} must be an object" for k in ("source", "judge", "cant") if not isinstance(t.get(k), dict)]
    if not isinstance(t.get("actions"), list):
        out.append(f"{i}: actions must be a list")
    if out:
        return out
    if t["kind"] not in KINDS:
        out.append(f"{i}: unknown kind {t['kind']!r}")
    if t["loop"] not in LOOP:
        out.append(f"{i}: loop {t['loop']!r} is not a task state")
    src = t["source"]
    if not _text(src.get("record")) or not _text(src.get("filed_by")) or not isinstance(src.get("related", []), list) \
            or not all(_text(k) for k in src.get("related", [])):
        out.append(f"{i}: source needs record, filed_by and a related list")
    if "gates" in src and not _text(src["gates"]):
        out.append(f"{i}: source.gates must be the gated record's key")
    if "deadline" in t:
        d = t["deadline"]
        if not _text(_get(d, "at")) or not _utc(d["at"]) or not _text(d.get("outcome")) \
                or not isinstance(d.get("run"), bool):
            out.append(f"{i}: deadline needs a UTC at, its outcome and whether it needs a run")
    out += _judge(t["judge"], f"{i} judge")
    parts = t["judge"].get("parts", [])
    parts = parts if isinstance(parts, list) else [None]
    for n, p in enumerate(parts):
        out += _judge(p, f"{i} part {n}")
        if isinstance(p, dict) and (not _text(p.get("role")) or p["role"] not in ROLES or not _text(p.get("label"))):
            out.append(f"{i} part {n}: needs a label and a role in {sorted(ROLES)}")
    steps = [p["href"] for p in parts if _get(p, "role") == "steps" and _text(p.get("href"))]
    if len(steps) != len(set(steps)):
        out.append(f"{i}: a steps link appears twice")
    if t["loop"] == "shown" and not t["actions"]:
        out.append(f"{i}: a shown task needs its primary action")
    for a in t["actions"]:
        u = _get(a, "undo")
        if not _text(_get(a, "verb")) or not _text(a.get("label")) or not _text(_get(u, "verb")) \
                or u["verb"] == a["verb"] or not _text(u.get("why")) or "until" not in u \
                or not _utc(u["until"]):
            out.append(f"{i}: action {a!r} needs verb, label and an undo with its own verb, until and why")
    if not _text(t["cant"].get("goes_to")) or not isinstance(t["cant"].get("words", ""), str):
        out.append(f"{i}: cant needs the recipient its words go to")
    return out


def advance(t, to):
    """The task in its next loop state; a move the chart has no arrow for raises."""
    if to not in MOVES.get(t["loop"], ()):
        raise ValueError(f"{t['id']}: no move from {t['loop']} to {to}")
    return dict(t, loop=to)


def effective(tasks, now):
    """The valid tasks as drawn at aware `now` (docs/board.md 2), never only as stored.

    Past its deadline an open task offers no answer and carries timed_out, the deadline's outcome, not the owner's
    choice: done when the timeout needs no run, else acted until its writer records the run. A decision whose
    source.gates record has no open task left closes with it."""
    out = [dict(t, actions=[], loop="acted" if t["deadline"]["run"] else "done", timed_out=t["deadline"]["outcome"])
           if t["loop"] in OPEN and "deadline" in t and now >= _at(t["deadline"]["at"]) else t for t in tasks]
    while True:  # a chain of gates closes link by link until nothing changes
        live = {t["source"]["record"] for t in out if t["loop"] != "done"}
        nxt = [dict(t, actions=[], loop="done", closed_with=t["source"]["gates"])
               if t["loop"] in OPEN and "gates" in t["source"] and t["source"]["gates"] not in live else t for t in out]
        if nxt == out:
            return out
        out = nxt


def coverage(sources, tasks):
    """Each open source record maps to exactly one valid task, and every task to an open source.

    sources: the record keys (text) every source reader found open. Missing, duplicate and unknown fail by identity."""
    out = [p for t in tasks for p in problems(t)]
    ids = [t.get("id") for t in tasks if isinstance(t, dict) and isinstance(t.get("id"), str)]
    out += [f"{i}: task id appears {ids.count(i)} times" for i in sorted(set(ids), key=str) if ids.count(i) > 1]
    owned = [t["source"]["record"] for t in tasks if isinstance(t, dict) and isinstance(t.get("source"), dict)
             and isinstance(t["source"].get("record"), str)]
    out += [f"{r!r}: a source key must be text" for r in sources if not _text(r)]
    want = {r for r in sources if _text(r)}
    out += [f"{r}: open source has no task" for r in sorted(want - set(owned))]
    out += [f"{r}: open source has {owned.count(r)} tasks" for r in sorted(want & set(owned)) if owned.count(r) > 1]
    out += [f"{r}: task names no open source" for r in sorted(set(owned) - want)]
    return out
