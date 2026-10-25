"""build, edit, decide and check (release.md section 2), each held under the spine's per-video writer lock."""
from ..cutting.plan import words as transcript
from ..spine.engine import writer
from ..spine.files import SpineError, canon, digest, filehash, hashes, inside, load
from . import readings, review, rows as rowing
from .inputs import (COPY, COPY_READ, CUES, DECISION, EDITS, ENVELOPE, MANIFEST, NAMED, PUBLISH, RELEASE, REVIEW, TEXT,
                     TEXT_READ, WORDS, Inputs, Refused, Unavailable, current, day, dump, facts, field, key, settings,
                     stages, write)

NEXT = {"pass": "owner_decides", "fail": "revise", "unavailable": "supply_inputs"}


class Stale(SpineError):
    """The release on disk is not the one its inputs make now, or its Review.md is not its render."""


def compose(video):
    """Every input read and every row made, nothing measured: (record, settings, what measuring needs or None)."""
    ins, s, bound_, ctx = Inputs(video), None, {}, None
    try:
        s = settings(video)
        stage, titling = stages(video)
        bound_ = {"stages": {"release": stage, "titles": titling}, "facts": facts(video), "day": day(video)}
        outs = rowing.outputs(ins.members())
        publish, copy = ins.json(PUBLISH), ins.json(COPY)
        ins.read(COPY_READ, COPY)
        text = None
        if TEXT in stage["inputs"]:
            text = ins.text(TEXT)
            ins.read(TEXT_READ, TEXT)
        said = transcript(ins.json(WORDS))
        edits = ins.json(EDITS)
        chat = bound_["facts"]["chat_shown"]
        rowing.dress(ins, s, outs, publish, copy, bool(titling), chat)
        rows = rowing.rows(s, outs, copy, text, edits)
        cues = ins.json(CUES) if any(o["id"] == "long" for o in outs) else None
        rec = {"outputs": outs, "rows": rows, "state": "built", "detail": []}
        ctx = (publish, bool(titling), said, cues, bound_["facts"], bound_["day"])
    except Refused as e:
        rec = {"state": "refused", "detail": [{"check": "release.input", "value": str(e), "maker": e.maker}]}
    except (SpineError, ValueError) as e:
        rec = {"state": "unavailable", "detail": str(e)}
    head = {"version": 3, "key": key(ins.reads, s, **bound_), "reads": ins.reads, "day": bound_.get("day"),
            "settings": "release.settings#" + digest(canon(s)) if s is not None else None}
    for name, p in NAMED.items():
        head.update({name: p if p in ins.reads else None, f"{name}_sha256": ins.reads.get(p)})
    return {**head, **rec}, s, ctx


def measure(video, rec, s, ctx):
    """The readings, once per key: a failing one refuses, a reading that cannot be taken is unavailable."""
    try:
        rec["readings"] = readings.measure(video, s, rec["outputs"], rec["rows"], *ctx)
    except (SpineError, ValueError) as e:
        rec.update(state="unavailable", detail=str(e))
        return
    failed = [r for r in rec["readings"] if r["result"] == "fail"]
    rec.update(state="refused" if failed else "built", detail=failed)


def bound(rec, row):
    """The hashes a decision on this row binds: its words and its output's file, sidecar and thumbnails."""
    o = next((o for o in rec["outputs"] if o["id"] == row["output"]), None)
    files = {} if o is None else {"file_sha256": o["sha256"], "sidecar_sha256": o["sidecar"]["sha256"],
                                  "thumbnails": [t["sha256"] for t in o.get("fields", {}).get("thumbnails", [])]}
    return {"words_sha256": row["words_sha256"], **files}


def _json(video, p):
    try:
        return load(video.folder, p)
    except (OSError, ValueError) as e:
        raise SpineError(f"{p}: unreadable ({e})") from None


def _append(video, row, text, by, replaced, at_key):
    """One edit entry, appended; the same words already latest for the row write nothing."""
    edits = _json(video, EDITS)
    latest = rowing.latest(edits)
    if row in latest and latest[row]["text"] == text:
        return edits
    field(edits, "edits", EDITS, "owner", list).append(
        {"row": row, "text": text, "by": by, "at": video.now(), "replaced": replaced, "key": at_key})
    write(video, EDITS, dump(edits))
    return edits


def _unedited(video):
    """Review.md is missing or still its own render: a refusal may replace it; an owner-edited page is kept."""
    f = inside(video.folder, REVIEW)
    if not f.exists():
        return True
    named = review.parse(f.read_text())[0]
    shown = named and inside(video.folder, review.prev(named))
    return bool(shown) and shown.exists() and shown.read_text() == f.read_text()


def _his_edits(video, rec):
    """Bring the owner's changed Review.md blocks back through edit; True when one was appended."""
    f = inside(video.folder, REVIEW)
    if not f.exists():
        return False
    his = f.read_text()
    named = review.parse(his)[0]
    if named is None:
        raise Refused(f"{REVIEW}: its first line names no release key", "owner")
    shown = inside(video.folder, review.prev(named))
    if not shown.exists():
        raise Refused(f"{REVIEW}: names {named}, a key with no render in records/prev", "owner")
    now = {r["id"]: rowing.words(r, rec["outputs"]) for r in rec["rows"]}
    appended = False
    for rid, (text, before) in review.changed(his, shown.read_text(), named).items():
        if rid not in now:
            raise Refused(f"{REVIEW}: edits row {rid}, which the release no longer has", "owner")
        if now[rid] == text:
            continue  # already brought back, before a crash
        if now[rid] != before:
            raise Refused(f"{REVIEW}: row {rid} was edited and its words also changed upstream since {named}", "owner")
        _append(video, rid, text, "owner", before, named)
        appended = True
    return appended


def _finished(video, rec):
    """A record whose build ran to its end: the envelope (written last) binds it, Review.md exists, and a built one's is its render."""
    env = inside(video.folder, ENVELOPE)
    if not env.exists() or _json(video, ENVELOPE).get("reads", {}).get(RELEASE) != filehash(inside(video.folder, RELEASE)):
        return False
    page = filehash(inside(video.folder, REVIEW))
    return page is not None and (rec["state"] != "built" or page == rec["review"]["sha256"])


def _key_at(video, reads, s, on):
    """The key these reads and settings make with the host's stage data and facts now, evaluated on day `on`."""
    stage, titling = stages(video)
    return key(reads, s, stages={"release": stage, "titles": titling}, facts=facts(video), day=on)


def _reusable(video, old, rec, s):
    """The record on disk was measured from these inputs on its own day, each dated reading still gives its result
    today, and its build finished: returning it redoes nothing."""
    return (old is not None and "readings" in old and old.get("key") == _key_at(video, rec["reads"], s, old.get("day"))
            and readings.fresh(old["readings"], rec["day"]) and _finished(video, old))


def build(video):
    """The release stage's run: the owner's edits first, then release.json, then Review.md, then the envelope. An
    unchanged key whose measured build finished returns that record and measures and writes nothing."""
    with writer(video):
        if not inside(video.folder, EDITS).exists():  # a concrete empty record: QC binds hashes, never an absence
            write(video, EDITS, dump({"edits": []}))
        rec, s, ctx = compose(video)
        if ctx is not None:
            try:
                if _his_edits(video, rec):
                    rec, s, ctx = compose(video)
            except Refused as e:
                rec.update(state="refused", detail=[{"check": "release.review", "value": str(e), "maker": e.maker}])
                ctx = None
            except (SpineError, ValueError) as e:
                rec.update(state="unavailable", detail=str(e))
                ctx = None
        f = inside(video.folder, RELEASE)
        old = _json(video, RELEASE) if f.exists() else None
        if ctx is not None and _reusable(video, old, rec, s):
            return old
        if ctx is not None:
            measure(video, rec, s, ctx)
        page = None
        if rec["state"] == "built" or rec["state"] == "refused" and _unedited(video):
            page = review.render(rec, lambda r: rowing.words(r, rec["outputs"]))
            rec["review"] = {"file": REVIEW, "sha256": digest(page.encode())}
            write(video, review.prev(rec["key"]), page)
        write(video, RELEASE, dump(rec))
        if page is not None:
            write(video, REVIEW, page)
        state = {"built": "pass", "refused": "fail"}.get(rec["state"], "unavailable")
        reads = {**rec["reads"], RELEASE: filehash(f)}
        write(video, ENVELOPE, dump({"ok": state == "pass", "state": state, "next_action": NEXT[state],
                                     "detail": rec["detail"], "reads": reads, "key": key(reads, s)}))
        return rec


def _current(video):
    """The built release, refused unless its inputs, settings and Review.md are the ones it was built from."""
    f = inside(video.folder, RELEASE)
    if not f.exists():
        raise Unavailable(f"{RELEASE}: no release built")
    rec = _json(video, RELEASE)
    if rec["state"] != "built":
        return rec
    if current(video, rec["reads"]) != rec["reads"] or _key_at(video, rec["reads"], settings(video), rec["day"]) != rec["key"]:
        raise Stale(f"{RELEASE}: stale, an input, setting, stage or host fact changed since {rec['key']}; build again")
    if not readings.fresh(rec["readings"], day(video)):
        raise Stale(f"{RELEASE}: stale, a dated reading crossed its window since {rec['day']}; build again")
    if not _finished(video, rec):
        raise Stale(f"{RELEASE}: its build did not finish or {REVIEW} differs from its render; build again")
    return rec


def edit(video, row, text, by):
    """Append one row's new words to release_edits.json and write nothing else; the spine reruns build."""
    if not isinstance(text, str) or not isinstance(by, str) or not by:
        raise SpineError("edit: text and by must be text")
    with writer(video):
        rec = _current(video)
        if rec["state"] != "built":
            raise SpineError(f"{RELEASE}: is {rec['state']}, not built")
        r = next((r for r in rec["rows"] if r["id"] == row), None)
        if r is None:
            raise SpineError(f"edit: {RELEASE} has no row {row}")
        was = rowing.words(r, rec["outputs"])
        if text == was:
            return None  # the row already says this: nothing to record
        return _append(video, row, text, by, was, rec["key"])


def decide(video, rows, by, seen=None):
    """rows maps row ID to "approved" or {"changes": note, "maker": who}; binds each row's current hashes. seen, the
    {path: hash} the decider was shown (the owner task's inputs), refuses as Stale once any of them changed."""
    with writer(video):
        if seen is not None and hashes(video, seen) != seen:
            raise Stale(f"decide: {', '.join(p for p, h in hashes(video, seen).items() if h != seen[p])} changed since the decider saw it")
        rec = _current(video)
        if rec["state"] != "built":
            raise SpineError(f"{RELEASE}: is {rec['state']}, not built")
        known = {r["id"]: r for r in rec["rows"]}
        f = inside(video.folder, DECISION)
        old = _json(video, DECISION) if f.exists() else {"rows": {}}
        out = dict(field(old, "rows", DECISION, "owner", dict))
        for rid, d in rows.items():
            if rid not in known:
                raise SpineError(f"decide: {RELEASE} has no row {rid}")
            if d == "approved":
                entry = {"decision": "approved"}
            elif (isinstance(d, dict) and set(d) == {"changes", "maker"}
                  and all(isinstance(d[k], str) and d[k].strip() for k in d)):
                entry = {"decision": "changes", "note": d["changes"], "maker": d["maker"]}
            else:
                raise SpineError(f"decide: {rid} is neither approved nor {{changes, maker}}")
            entry.update(bound(rec, known[rid]))
            was = old["rows"].get(rid, {})
            if {k: v for k, v in was.items() if k not in ("by", "at")} != entry:  # the same decision keeps its time
                out[rid] = {**entry, "by": by, "at": video.now()}
        if out == old["rows"] and old.get("release_key") == rec["key"]:
            return old
        decision = {"release_key": rec["key"], "by": by, "at": video.now(), "rows": out}
        write(video, DECISION, dump(decision))
        return decision


def check(video):
    """Read only: ok when every row is approved on its current hashes; `send` lists the rows posting may send."""
    try:
        rec = _current(video)
        if rec["state"] != "built":
            return {"ok": False, "state": rec["state"], "next_action": NEXT["fail" if rec["state"] == "refused" else "unavailable"],
                    "detail": rec["detail"], "send": []}
        f = inside(video.folder, DECISION)
        decided = field(_json(video, DECISION), "rows", DECISION, "owner", dict) if f.exists() else {}
    except Stale as e:
        return {"ok": False, "state": "stale", "next_action": "build", "detail": str(e), "send": []}
    except (SpineError, KeyError, TypeError) as e:
        return {"ok": False, "state": "unavailable", "next_action": "supply_inputs", "detail": str(e), "send": []}
    waiting, changes, send = [], [], []
    for r in rec["rows"]:
        d = decided.get(r["id"], {})
        if any(d.get(k) != v for k, v in bound(rec, r).items()):
            waiting.append(r["id"])
        elif d.get("decision") == "approved":
            o = next((o for o in rec["outputs"] if o["id"] == r["output"]), None)
            send.append({"id": r["id"], "output": r["output"], "platform": r["platform"],
                         "words": rowing.words(r, rec["outputs"]), "file": o and o["file"], **bound(rec, r)})
        else:
            changes.append({"row": r["id"], "maker": d.get("maker"), "note": d.get("note")})
    state = "changes" if changes else "waiting_owner" if waiting else "approved"
    return {"ok": state == "approved", "state": state,
            "next_action": {"changes": "maker_revises", "waiting_owner": "owner_decides", "approved": "post"}[state],
            "detail": {"waiting": waiting, "changes": changes}, "send": send}
