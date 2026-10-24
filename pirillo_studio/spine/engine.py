"""The spine (docs/spine.md): a recipe is data, and one engine answers next, run, check and hand_off for any recipe.

A video is a folder; `records/job.json` names its recipe and `records/spine.json` is its one stage record. The host
supplies every effect through `Video`: starting a maker, filing an owner task, telling the next actor, serving a
judged file, the domain checks and the media preflight of a stage that declares tools. A check is called with its
declared evidence only, never a recipe name.
"""
import contextlib
import datetime as dt
import fcntl
import json
import os
import tempfile
from pathlib import Path, PurePosixPath

from .files import SpineError, canon, digest, filehash, fold, hashes, inside, load, safe  # noqa: F401, filehash is API
from .recipe import actor, validate  # noqa: F401, validate is API

HERE = Path(__file__).parent
ENGINE = "spine:1"  # the version of the checks built in here (decision); a change invalidates their passes
VERDICTS = ("pass", "fail", "running", "blocked")  # a domain check's answers; posted only from the posted check
VERBS = ("approve", "reject", "redo", "not-this", "try-again", "cant", "undo")
KINDS = {".mp4": "video", ".mov": "video", ".png": "image", ".jpg": "image", ".md": "words", ".json": "words",
         ".srt": "words"}


def data(name):
    return json.loads((HERE / name).read_text())


class Video:
    """One video folder and the host's adapters.

    start(actor, run, folder, stage, key) -> receipt starts a maker; ask(task) -> receipt files an owner task in the
    Board's shape; tell(actor, key, handoff) -> receipt delivers a handoff; href(path) -> the served URL of a judged
    file; checks {check id: (version, fn(evidence) -> {"state", "words"})}; preflight(stage, passed, recipe_revision)
    -> {"state", "detail", "next_action", "key"} (docs/preflight.md; required when a stage declares tools). Each
    adapter reconciles by its key, so a retry after a crash never repeats an effect. recipes and catalog default to
    this package's data."""

    def __init__(self, folder, *, start, ask, tell, href, checks, recipes=None, catalog=None, preflight=None):
        self.folder, self.start, self.ask, self.tell, self.href = Path(folder), start, ask, tell, href
        self.checks = checks
        self.recipes = data("recipes.json") if recipes is None else recipes
        self.catalog = data("catalog.json") if catalog is None else catalog
        validate(self.recipes, self.catalog)
        absent = {c for r in self.recipes.values() for st in r["stages"] for c in st["check"]} - set(checks) - {"decision"}
        if absent:
            raise SpineError(f"checks: no implementation for {sorted(absent)}")
        self.preflight = preflight
        tooled = [f"{n} {st['id']}" for n, r in self.recipes.items() for st in r["stages"] if "tools" in st]
        if tooled and preflight is None:
            raise SpineError(f"preflight: no adapter for the stages that declare tools: {tooled}")

    def job(self):
        """(video id, recipe name, recipe) from records/job.json; the id, not the folder, keys every effect."""
        job = self.folder / "records" / "job.json"
        try:
            j = load(self.folder, "records/job.json")
            name, vid = j["recipe"], j["id"]
            if not isinstance(vid, str) or not vid:
                raise TypeError(f"id {vid!r} is not a non-empty string")
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise SpineError(f"{job}: no readable recipe and video id ({e})") from None
        if name not in self.recipes:
            raise SpineError(f"{job}: unknown recipe {name!r}")
        return vid, name, self.recipes[name]

    def recipe(self):
        return self.job()[1:]


def versions(v, st):
    return {c: ENGINE if c == "decision" else v.checks[c][0] for c in st["check"]}


def defn(v, st):
    """The stage's definition and every upstream stage's checks: a revised upstream check reopens what follows."""
    done, stages = {}, v.recipe()[1]["stages"]
    for s in stages[:stages.index(st) + 1]:  # producers come first: validate refuses an input made later
        up = [u for u in stages if u["id"] in done and set(u["outputs"]) & set(s["inputs"])]
        done[s["id"]] = digest(canon([s, [(u["id"], versions(v, u), done[u["id"]]) for u in up]]))
    return done[st["id"]]


def load_record(v):
    f = inside(v.folder, "records/spine.json")
    if not f.exists():
        return {"stages": {}, "history": []}
    try:
        rec = load(v.folder, "records/spine.json")
        if isinstance(rec["stages"], dict) and isinstance(rec["history"], list):
            return rec
        raise TypeError("stages must be an object and history a list")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SpineError(f"{f}: unreadable stage record ({e})") from None


def save(v, rec, sid, why="", r=None):
    """Append the stage's transition (or r, a state not kept) to history and replace spine.json atomically."""
    note(rec, sid, why, r)
    atomic(v.folder / "records" / "spine.json", json.dumps(rec, indent=1, sort_keys=True, allow_nan=False))


def atomic(f, text):
    """Replace f with text whole and on disk, or leave it as it was."""
    fd, tmp = tempfile.mkstemp(dir=f.parent, prefix=f".{f.name}.")
    with os.fdopen(fd, "w") as out:
        out.write(text)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, f)


def note(rec, sid, why, r=None):
    r = r or rec["stages"][sid]
    rec["history"].append({"stage": sid, "attempt": r["attempt"], "state": r["state"], "words": r.get("words", []),
                           "inputs": r["inputs"], "outputs": r.get("outputs"), "checks": r.get("checks"),
                           "receipt": r.get("receipt"), "why": why, "at": dt.datetime.now(dt.timezone.utc).isoformat()})


@contextlib.contextmanager
def writer(v):
    """The video's single writer, held across read, effect and write."""
    (v.folder / "records").mkdir(parents=True, exist_ok=True)
    with open(inside(v.folder, "records/spine.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def holds(v, ids):
    f = inside(v.folder, "records/holds.json")
    if not f.exists():
        return {}, None
    try:
        rows = load(v.folder, "records/holds.json")["holds"]
        out = {h["stage"]: h for h in rows if isinstance(h["why"], str) and isinstance(h["by"], str)}
        bad = [h["stage"] for h in rows if h["stage"] not in ids or h not in out.values()]
        return out, (f"{f}: holds name stages not in this recipe or lack why/by: {bad}" if bad else None)
    except (OSError, ValueError, KeyError, TypeError) as e:
        return {}, f"{f}: unreadable holds ({e})"


def answer(stage, actor_, why, state, missing=()):
    return {"stage": stage, "actor": actor_, "why": why, "state": state, "inputs_missing": list(missing)}


def current(v, st, r, done):
    """Whether record r still stands on the bytes and checks on disk now; done adds outputs and check versions."""
    if r is None or r["def"] != defn(v, st) or r["inputs"] != hashes(v, st["inputs"]):
        return False
    return not done or (r.get("outputs") == hashes(v, judged_files(st, v)) and r.get("checks") == versions(v, st))


def judged_files(st, v):
    """The outputs a pass stands on, with the stage's own preflight record when a check reads it (recipe.py)."""
    own = f"records/ready/{st['id']}.json"
    return st["outputs"] + ([own] if any(own in v.catalog["checks"][c]["reads"] for c in st["check"]) else [])


def decide(v):
    """next's answer and the stage it names, from the files and the record alone."""
    _, recipe = v.recipe()
    stages, rec = recipe["stages"], load_record(v)
    held, bad = holds(v, {st["id"] for st in stages})
    makers = {p: actor(st) for st in stages for p in st["outputs"]}
    for st in stages:
        sid, r = st["id"], rec["stages"].get(st["id"])
        if r and r["state"] == "posted" and current(v, st, r, True):
            continue
        if r and r["state"] == "pass" and r.get("handoff", {}).get("receipt") and current(v, st, r, True):
            continue
        if bad or sid in held:
            return answer(sid, "owner" if bad else held[sid]["by"], bad or held[sid]["why"], "blocked"), st, rec
        now = hashes(v, st["inputs"])
        missing = [p for p, h in now.items() if h is None]
        if missing:
            return answer(sid, makers.get(missing[0], "owner"), "missing inputs", "blocked", missing), st, rec
        if r and uncertain(st, r):
            return answer(sid, "clock", f"send {r['op']} uncertain: run reconciles its receipt before a new attempt",
                          "blocked"), st, rec
        if r is None or r["state"] == "blocked" and "op" not in r and "preflight" not in r:  # looked at again; a
            # preflight's block stands, naming its fix, until its inputs change or its actor runs the stage again
            return answer(sid, actor(st), "ready", "ready"), st, rec
        if not current(v, st, r, r["state"] in ("pass", "posted")):
            return answer(sid, actor(st), f"inputs, outputs or checks changed since attempt {r['attempt']}",
                          "ready"), st, rec
        if r["state"] == "fail" and actor(st) == "owner":
            return answer(sid, "owner", "sent back; the producer's new pass needs a fresh decision", "ready"), st, rec
        if r["state"] == "pass":
            return answer(sid, st["next_actor"], f"passed; hand off to {st['next_actor']}", "pass"), st, rec
        words = "; ".join(r.get("words") or []) or (f"running: {r.get('receipt')}" if r["state"] == "running"
                                                    else r["state"])
        who = {"fail": r.get("maker"), "waiting_owner": r.get("by") or "owner"}.get(r["state"]) or r.get("by") \
            or actor(st)
        return answer(sid, who, words, r["state"]), st, rec
    end = "posted" if rec["stages"][stages[-1]["id"]]["state"] == "posted" else "done"
    return answer(None, None, end, end), None, rec


def next(v):  # noqa: A001, the spec's name for the first answer
    """{stage, actor, why, state, inputs_missing}: the one earliest unfinished stage of the video, read only."""
    return decide(v)[0]


def find(recipe, sid):
    for st in recipe["stages"]:
        if st["id"] == sid:
            return st
    raise SpineError(f"stage {sid} is not in this video's recipe")


def uncertain(st, r):
    """A send started with no receipt back: it may have gone out, so only its own key's receipt settles it."""
    return actor(st) == "clock" and "op" in r and r.get("receipt") is None


def opkey(v, st, attempt, inputs):
    """A maker's attempt is new work; the clock's send is keyed by what it sends, so a rerun reconciles it."""
    turn = "sent" if actor(st) == "clock" else attempt
    return f"{st['id']}:{turn}:{digest(canon([v.job()[0], st['id'], turn, inputs]))[7:23]}"


def run(v, sid, rerun=False):
    """Start the stage next names (or rerun one already passed, as a new attempt); returns its record."""
    with writer(v):
        ans, st, rec = decide(v)
        _, recipe = v.recipe()
        ids = [s["id"] for s in recipe["stages"]]
        st = find(recipe, sid)
        reach = ids.index(ans["stage"]) if ans["stage"] else len(ids)
        if not (sid == ans["stage"] or rerun and ids.index(sid) < reach):
            raise SpineError(f"next is {ans['stage']} ({ans['actor']}: {ans['why']}), not {sid}")
        if sid == ans["stage"] and ans["state"] == "pass" and not rerun:
            raise SpineError(f"{sid} passed: hand it off, or rerun it as a new attempt")
        r = rec["stages"].get(sid)
        inputs = hashes(v, st["inputs"])
        missing = [p for p, h in inputs.items() if h is None]
        held, bad = holds(v, set(ids))
        why = bad or held.get(sid, {}).get("why") or ("missing inputs" if missing else None)
        def stop(state, words, by, note, mine=False, **more):
            b = {"attempt": r["attempt"] if r else 0, "state": state, "def": defn(v, st), "inputs": inputs,
                 "words": words, "by": by, **more}
            if mine and r and "op" in r and r["receipt"] is None and current(v, st, r, False):  # unstarted: resumes
                b.update(op=r["op"], maker=r["maker"], receipt=None)
            if mine or not (r and "op" in r):  # a started attempt keeps its key and receipt; a hold or missing input
                rec["stages"][sid] = b  # is its history (next reads those live); a preflight block only stands here
            save(v, rec, sid, note, b)
            return b
        if why:
            return stop("blocked", [why] + missing, "owner" if bad else held.get(sid, {}).get("by") or ans["actor"],
                        "blocked at run")
        resume = r and (uncertain(st, r) or not rerun and "op" in r and r["state"] in ("running", "waiting_owner",
                                                                                        "blocked") and current(v, st, r, False))
        started = resume and (uncertain(st, r) or r["receipt"] is not None)
        pf = None if started or "tools" not in st else ready(v, recipe, st)  # an unstarted resume asks again
        if pf and pf["state"] != "ready":  # nothing expensive starts unless ready (docs/preflight.md section 3)
            out = "blocked" if pf["state"] == "blocked" else "waiting_owner"
            return stop(out, [pf["detail"], pf["next_action"]], actor(st) if out == "blocked" else "owner",
                        f"preflight {pf['state']}", mine=True, preflight=pf["state"])
        if not resume:
            r = rec["stages"][sid] = {"attempt": (r["attempt"] + 1) if r else 1, "def": defn(v, st),
                                      "inputs": inputs, "maker": actor(st), "receipt": None, "words": [],
                                      "state": "waiting_owner" if actor(st) == "owner" else "running",
                                      **({"preflight": pf["key"]} if pf else {})}
            r["op"] = opkey(v, st, r["attempt"], inputs)
            save(v, rec, sid, "rerun" if rerun else "run")
        elif pf:
            r.update(state="waiting_owner" if actor(st) == "owner" else "running", words=[], preflight=pf["key"])
        if r["receipt"] is None:
            r["receipt"] = v.ask(task(v, recipe, st, r)) if actor(st) == "owner" else \
                v.start(actor(st), st["run"], v.folder, sid, r["op"])
            save(v, rec, sid, "receipt")
        return r


def ready(v, recipe, st):
    """The stage's preflight answer: its outputs with their size rows, each input's producer (freshness is the
    engine's own, so no hash is passed) and the stage's definition as the recipe revision."""
    made = {p: s["id"] for s in recipe["stages"] for p in s["outputs"]}
    view = dict(st, outputs=[{"path": p, "size": st["size"][p]} if p in st.get("size", {}) else p for p in st["outputs"]])
    pf = v.preflight(view, {p: {"by": made.get(p, "the job"), "sha256": None} for p in st["inputs"]}, defn(v, st))
    if not isinstance(pf, dict) or pf.get("state") not in ("ready", "blocked", "broken", "waiting_owner") \
            or pf["state"] == "ready" and not (isinstance(pf.get("key"), str) and pf["key"]):
        raise SpineError(f"stage {st['id']}: preflight answered {pf!r}, not ready with its key, blocked, broken "
                         "or waiting_owner")
    return pf


def task(v, recipe, st, r):
    """The owner stage's one Board task (docs/board.md), its id bound to the judged content and attempt."""
    makers = {p: actor(s) for s in recipe["stages"] for p in s["outputs"]}
    judged = [p for p in st["inputs"] if p in makers]
    producer = makers[judged[0]]
    part = lambda p, role: {"type": KINDS.get(PurePosixPath(p).suffix, "file"), "href": v.href(p), "label": p,
                            "role": role}
    first = part(judged[0], "content")
    return {"id": f"spine:{r['op']}", "kind": "ask", "what": f"Approve {st['id'].replace('-', ' ')}",
            "why": f"{st['next_actor']} waits on this decision; sending it back returns it to {producer}",
            "source": {"record": f"records/spine.json#{st['id']}/{r['attempt']}", "filed_by": producer, "related": []},
            "judge": {"type": first["type"], "href": first["href"], "parts": [part(p, "content") for p in judged]},
            "actions": [{"verb": "approve", "label": "Approve",
                         "undo": {"verb": "undo", "until": None, "why": f"Until {st['next_actor']} starts"}},
                        {"verb": "redo", "label": "Send back",
                         "undo": {"verb": "undo", "until": None, "why": f"Until {producer} starts again"}}],
            "cant": {"words": "", "goes_to": producer}, "loop": "filed"}


def next_maker(makers, st):
    return makers[[p for p in st["inputs"] if p in makers][0]]


def evidence(v, st, c):
    reads = v.catalog["checks"][c]["reads"]
    paths = [p for p in reads if not p.startswith("@")] + (st["inputs"] if "@inputs" in reads else []) \
        + (st["outputs"] if "@outputs" in reads else [])
    return {p: {"path": str(v.folder / p), "sha256": h} for p, h in hashes(v, list(dict.fromkeys(paths))).items()}


def decision(v, st, r, recipe):
    """The built-in owner check: an approval of this exact task passes; a send-back fails to the producer."""
    f = inside(v.folder, st["outputs"][0])
    if not f.exists():
        return {"state": "waiting_owner", "words": [f"waiting on the owner's decision on task spine:{r['op']}"]}
    try:
        d = load(v.folder, st["outputs"][0])
        verb, words, tid = d["verb"], d.get("words", ""), d["task"]
        if verb not in VERBS or not isinstance(words, str):
            raise ValueError(f"verb {verb!r} or its words")
    except (OSError, ValueError, KeyError, TypeError) as e:
        return {"state": "blocked", "words": [f"{f}: unreadable decision ({e})"]}
    if tid != f"spine:{r['op']}":
        return {"state": "waiting_owner", "words": [f"the decision answers task {tid}, an older revision"]}
    if verb == "approve":
        return {"state": "pass", "words": []}
    if verb in ("cant", "undo"):
        makers = {p: actor(s) for s in recipe["stages"] for p in s["outputs"]}
        return {"state": "waiting_owner", "words": [words or verb], "undo": verb == "undo",
                "by": next_maker(makers, st) if verb == "cant" else "owner"}
    return {"state": "fail", "words": [f"owner {verb}: {words}"]}


def check(v, sid):
    """Run the stage's checks on its declared evidence and record {ok, state, next_action, detail}."""
    with writer(v):
        _, recipe = v.recipe()
        rec, st = load_record(v), find(recipe, sid)
        r = rec["stages"].get(sid)
        if r is None or "op" not in r:
            raise SpineError(f"stage {sid} has no started attempt to check: run it first")
        if r["state"] == "fail" and st["check"] != ["decision"]:
            raise SpineError(f"stage {sid} attempt {r['attempt']} failed: run it again as a new attempt")
        seen = hashes(v, st["inputs"] + st["outputs"])
        result = verdict(v, st, r, recipe)
        if result["state"] in ("pass", "posted") and hashes(v, st["inputs"] + st["outputs"]) != seen:
            result = {"state": "running", "words": ["files changed while checking: check again"], "by": actor(st)}
        was = r["state"]
        late = result.get("undo") and too_late(v, rec, recipe, st, was)
        if late:  # past the task's advertised undo window the receipt-backed result stands
            if was == "pass":  # the approval the downstream work used stays the stage's output; spine.json keeps the undo
                atomic(inside(v.folder, st["outputs"][0]), r["decided"])
            result = {"state": was, "words": [f"undo refused: {late} already started"], "by": r.get("by")}
        if result["state"] == "pass" and st["check"] == ["decision"]:
            r["decided"] = inside(v.folder, st["outputs"][0]).read_text()
        r.update(state=result["state"], words=result["words"], outputs=hashes(v, judged_files(st, v)),
                 checks=versions(v, st), by=result.get("by", actor(st)))
        if st["check"] == ["decision"]:
            reopen(rec, recipe, st, result, was)
        save(v, rec, sid, "check")
        state = result["state"]
        step = {"pass": f"hand off to {st['next_actor']}", "posted": None, "fail": f"{r['maker']} repairs and reruns",
                "running": "check again when the work finishes", "waiting_owner": "the owner decides on the Board" if r["by"] == "owner"
                else f"{r['by']} answers the owner: " + "; ".join(result["words"]),
                "blocked": f"{r['by']} resolves: " + "; ".join(result["words"])}[state]
        return {"ok": state in ("pass", "posted"), "state": state, "next_action": step,
                "detail": {"stage": sid, "attempt": r["attempt"], "words": result["words"], "maker": r["maker"]}}


def verdict(v, st, r, recipe):
    if not current(v, st, r, False):
        return {"state": "blocked", "words": [f"inputs changed since attempt {r['attempt']}: run {st['id']} again"],
                "by": actor(st)}
    if st["check"] == ["decision"]:
        return decision(v, st, r, recipe)
    reviews = {p for c in st["check"] for p in v.catalog["checks"][c]["produces"]}
    missing = [p for p, h in hashes(v, st["outputs"]).items() if h is None and p not in reviews]
    if missing:
        return {"state": "blocked", "words": ["maker outputs missing: " + ", ".join(missing)], "by": actor(st)}
    seen = []
    for c in st["check"]:
        ev, made = evidence(v, st, c), v.catalog["checks"][c]["produces"]
        token = judged(ev, made)
        absent = [p for p, e in ev.items() if e["sha256"] is None
                  or p in made and token.encode() not in (v.folder / p).read_bytes()]
        if absent:
            checker = v.catalog["checks"][c]["checker"]
            seen.append(("running", [f"{c}: waiting on {checker} for {', '.join(absent)} naming {token}"], checker)
                        if set(absent) <= set(made) else ("blocked", [f"{c}: evidence missing: {', '.join(absent)}"],
                                                          actor(st)))
            continue
        try:
            out = v.checks[c][1](ev)
            if out["state"] not in VERDICTS + (("posted",) if c == "posted" else ()) \
                    or not isinstance(out.get("words", []), list):
                raise ValueError(f"answered {out!r}")
            seen.append((out["state"], [f"{c}: {w}" for w in out.get("words", [])], None))
        except Exception as e:  # a check tool error never becomes a pass
            seen.append(("blocked", [f"{c}: check error {type(e).__name__}: {e}"], v.catalog["checks"][c]["checker"]))
    for state in ("blocked", "fail", "waiting_owner", "running", "posted", "pass"):  # the first present wins
        won = [x for x in seen if x[0] == state]
        if won:
            by = [b for _, _, b in won if b]
            return {"state": state, "words": [w for _, ws, _ in won for w in ws], "by": by[0] if by else actor(st)}
    raise SpineError(f"stage {st['id']}: no check ran")


def judged(ev, made):
    """What a review judged: the digest of its check's other evidence. A review record names it (docs/spine.md
    section 2), so a review of other bytes, or of bytes the maker has since overwritten, is still awaited."""
    return digest(canon({p: e["sha256"] for p, e in ev.items() if p not in made}))


def too_late(v, rec, recipe, st, was):
    """The stage an undo comes too late for: a consumer started on this approval, or a sent-back producer's new start."""
    passed = rec["stages"][st["id"]].get("outputs") or {}
    for s in recipe["stages"]:
        r, used = rec["stages"].get(s["id"]), set(s["inputs"]) & set(st["outputs"])
        if was == "pass" and r and "op" in r and used and all(r["inputs"][p] == passed.get(p) for p in used):
            return s["id"]
        if was == "fail" and r and set(s["outputs"]) & set(st["inputs"]) and r.get("reopened", {}).get("op") != r["op"]:
            return s["id"]
    return None


def reopen(rec, recipe, st, result, was):
    """An owner send-back fails the producers of the judged content with the owner's words, in the same write as the
    decision; undoing it while no producer has started again restores each producer's pass."""
    makers = {p: s for s in recipe["stages"] for p in s["outputs"]}
    for pid in dict.fromkeys(makers[p]["id"] for p in st["inputs"] if p in makers):
        r = rec["stages"][pid]
        judged = all((r.get("outputs") or {}).get(p) == rec["stages"][st["id"]]["inputs"][p]  # the attempt judged,
                     for p in st["inputs"] if makers.get(p, {}).get("id") == pid)  # never a new one
        if result["state"] == "fail" and judged and r["state"] != "fail":
            rec["stages"][pid] = dict(r, state="fail", words=list(result["words"]),
                                  reopened={k: x for k, x in r.items() if k != "reopened"})
            rec["stages"][pid].pop("handoff", None)  # its handoff told of the bytes the owner sent back
            note(rec, pid, f"reopened by {st['id']}")
        elif result.get("undo") and was == "fail" and r.get("reopened", {}).get("op") == r.get("op"):
            rec["stages"][pid] = r["reopened"]
            note(rec, pid, f"send-back undone by {st['id']}")


def hand_off(v, sid):
    """Deliver a current pass to next_actor once: {actor, told, record}; a retry reconciles the same key."""
    with writer(v):
        _, recipe = v.recipe()
        rec, st = load_record(v), find(recipe, sid)
        r = rec["stages"].get(sid)
        if r is None or r["state"] != "pass" or not current(v, st, r, True):
            raise SpineError(f"stage {sid} has no current pass to hand off")
        key = f"handoff:{r['op']}"
        h = r.get("handoff")
        if not h or h["key"] != key:
            h = r["handoff"] = {"key": key, "actor": st["next_actor"], "receipt": None}
            save(v, rec, sid, "handoff recorded")
        if h["receipt"] is None:
            h["receipt"] = v.tell(st["next_actor"], key, {"video": v.job()[0], "stage": sid,
                                                         "attempt": r["attempt"], "record": "records/spine.json"})
            save(v, rec, sid, "handoff told")
        return {"actor": h["actor"], "told": key, "record": f"records/spine.json#stages/{sid}"}
