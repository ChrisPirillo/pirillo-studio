"""Cutting's checks bound to the spine engine's catalog (docs/cutting.md section 2, docs/spine.md section 1).

checks(video) -> {check id: (version, fn(evidence) -> {"state", "words"})}, passed straight to
engine.Video(checks=...). `video` is the cutting context settle, check, request_arc and lock already take: `folder`
(the video folder the engine runs) and `measure(path)` -> the source's readings. The engine calls fn under the
video's single writer (records/spine.lock), so fn runs cutting's unlocked bodies: taking the writer again would wait
on itself. settle is the maker's (the stage's run): fn reads what settle wrote and never settles.

- cut-plan: the settled plan's checks, before the overlays stage writes cards, bed and shorts.
- overlay-plan: the whole check, written as records/checks/cut.json, the record request_arc and lock authorize from.
- paper-read: the story editor's records/story_review.json, judged by paper_read (seam 3c).
- locked-cut: records/story_map.json is cutting.lock's record for the current plan, check and decision; never locks.

A read is the engine's evidence, or pinned by its hash inside that evidence (the settled plan's and the shorts spec's
made_from, which the check compares with the disk); any other read blocks, naming it.
"""
from pathlib import Path

from ..spine.files import digest
from . import checks as cut
from . import paper_read
from . import plan as io

HERE = Path(__file__).parent


def version():
    """The checks' code and settings: an edited check or threshold reopens every pass made under the old one."""
    return digest(b"".join(f.read_bytes() for f in sorted(HERE.iterdir())
                           if f.suffix in (".py", ".json") and not f.name.startswith("test_")))


def judge(video, evidence, overlays):
    result = cut.verdict(video, overlays)
    pins, reads, stray = set(evidence), result["reads"], []
    if result["state"] != "blocked":  # a pass or fail parsed both pinning records
        for p in ("records/cut_plan2.json", "records/shorts_spec.json"):
            if p in reads:
                pins |= set(io.read(video, p)["made_from"])
        stray = sorted(set(reads) - pins) + [p for p in evidence if p in reads and reads[p] != evidence[p]["sha256"]]
    if stray:
        result = dict(result, ok=False, state="blocked", next_action="supply_inputs",
                      detail=f"reads {', '.join(stray)}: neither the engine's evidence nor pinned by its hash there")
    if overlays:
        io.write(video, "records/checks/cut.json", result)
    if result["state"] == "blocked":
        return {"state": "blocked", "words": [result["detail"]]}
    return {"state": result["state"], "words": [f"{f['rule']}: span {f['span']} at {f['at_src']}s: {f['measure']}"
                                                for f in result["detail"]]}


def locked(video, evidence):
    try:
        record, plan = cut._fresh_inputs(video, (*cut._BASE, *cut._SHORTS))
        have = io.read(video, "records/story_map.json")
        want = cut.story_map(record, plan, evidence["records/arc_decision.json"]["sha256"], have["blocks"])
    except cut._ERRORS as e:
        return {"state": "blocked", "words": [f"the lock cannot be judged: {e}"]}
    wrong = sorted(k for k in want if have.get(k) != want[k])
    return {"state": "fail", "words": [f"records/story_map.json {', '.join(wrong)}: not the lock of the current plan, "
                                       "check and decision; lock again"]} if wrong else {"state": "pass", "words": []}


def checks(video):
    v = version()
    return {"cut-plan": (v, lambda evidence: judge(video, evidence, False)),
            "paper-read": (v, paper_read.judge),
            "overlay-plan": (v, lambda evidence: judge(video, evidence, True)),
            "locked-cut": (v, lambda evidence: locked(video, evidence))}
