"""Release checks for engine.Video(checks=...), called under records/spine.lock; never run a maker.

Bound: titles, text-read and release. text-plan stays unbound: item 39 has no plan validator yet.
captions-read stays with item 39's copy-read seam; posted belongs to posting (item 11), decision to the spine
(item 37). A file read must be engine evidence or hash-pinned in its release record; otherwise it blocks.
The release check judges the built package, before the owner's decision: api._current is the read-only part
of api.check. Two gaps in the release/spine interface (the lead's to settle before the host binds it): the
completion envelope must be supplied as evidence, and today's catalog does not declare it; and a refused build
writes no Review.md, a declared output, so engine.verdict blocks on it before this check can say why.
"""
from pathlib import Path

from ..spine.files import canon, digest, filehash
from . import api, readings
from .inputs import (ENVELOPE, PUBLISH, RELEASE, REVIEW, TEXT, TEXT_READ, WORDS, Inputs, Refused,
                     Unavailable, day, field, settings)

HERE = Path(__file__).parent


def version(video):
    """Code, bundled settings and the host profile reopen checks; an unreadable profile still blocks."""
    code = digest(b"".join(f.read_bytes() for f in sorted(HERE.iterdir())
                           if f.suffix in (".py", ".json") and not f.name.startswith("test_")))
    try:
        return digest(canon([code, settings(video)]))
    except Unavailable as e:  # settings() raises only this for a profile it cannot build; judge blocks naming it
        return digest(canon([code, f"{type(e).__name__}: {e}"]))


def pinned(ins, evidence, pins):
    """Compare bytes, not just path membership; null hashes and aliases never authorize a read."""
    for p, sha in ins.reads.items():
        want = evidence[p]["sha256"] if p in evidence else pins.get(p)
        if not sha or sha != want:
            raise Unavailable(f"reads {p}: changed or neither engine evidence nor pinned by its hash there")


def titles(video, ins):
    publish = ins.json(PUBLISH)
    said = api.transcript(ins.json(WORDS))
    out = {"title": field(publish, "title", PUBLISH, "packager"),
           "ab_titles": [field(t, "title", PUBLISH, "packager")
                         for t in field(publish, "ab_titles", PUBLISH, "packager", list)]}
    pool = [readings.norm(w) for w in " ".join(w["text"] for w in said).split() if readings.norm(w)]
    return readings.titles(video, settings(video), publish, out, day(video), pool, len(said))


def text_read(video, ins):
    text = ins.text(TEXT)
    ins.read(TEXT_READ, TEXT)
    s = settings(video)
    rows = api.rowing.rows(s, [], {}, text, {"edits": []})
    if not rows:
        raise Unavailable("release.settings map.text: no text rows to judge")
    return (readings.per_row(day(video), s, rows, [], [], {})
            + readings.text_lint(video, s, rows, []))


def release(video, ins, evidence):
    rec = ins.json(RELEASE)
    pinned(ins, evidence, {})  # only a bound release may introduce transitive reads
    pins = field(rec, "reads", RELEASE, "packager", dict)
    for p in pins:
        ins.reads[p] = filehash(ins.path(p))
    ins.json(ENVELOPE)
    if rec["state"] == "built":
        pins = {**pins, REVIEW: rec["review"]["sha256"]}
        ins.reads[REVIEW] = filehash(ins.path(REVIEW))
    pinned(ins, evidence, pins)
    # _current returns refused records early; their input hashes and completion must be current too.
    if any(ins.reads[p] != sha for p, sha in rec["reads"].items()):
        raise Unavailable(f"{RELEASE}: stale inputs")
    if rec["state"] == "unavailable":
        raise Unavailable(str(rec["detail"]))
    if api._key_at(video, rec["reads"], settings(video), rec["day"]) != rec["key"] or not api._finished(video, rec):
        raise Unavailable(f"{RELEASE}: stale identity or incomplete build")
    rec = api._current(video)
    if rec["state"] not in ("built", "refused"):
        raise Unavailable(f"{RELEASE}: unknown state {rec['state']}")
    if rec["state"] == "refused" and not rec["detail"]:
        raise Unavailable(f"{RELEASE}: refused without a reason")
    return rec["detail"] if rec["state"] == "refused" else []


def judge(video, evidence, name):
    ins = Inputs(video)
    try:
        # Check supplied hashes before semantic failures can obscure changed evidence.
        for p in evidence:
            ins.reads[p] = filehash(ins.path(p))
        pinned(ins, evidence, {})
        settings(video)  # an unbuildable profile blocks before missing maker inputs obscure its cause
        if name == "release":
            failed = release(video, ins, evidence)
        else:
            measured = titles(video, ins) if name == "titles" else text_read(video, ins)
            pinned(ins, evidence, {})
            failed = [r for r in measured if r["result"] == "fail"]
        return {"state": "fail" if failed else "pass", "words": [str(r) for r in failed]}
    except Refused as e:
        try:
            pinned(ins, evidence, {})
        except Unavailable as missing:
            return {"state": "blocked", "words": [str(missing)]}
        return {"state": "fail", "words": [str(e)]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        return {"state": "blocked", "words": [str(e)]}


def checks(video):
    v = version(video)
    return {name: (v, lambda evidence, name=name: judge(video, evidence, name))
            for name in ("titles", "text-read", "release")}
