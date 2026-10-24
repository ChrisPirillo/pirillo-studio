"""The paper-read catalog check (docs/spine.md section 1; queue item 37 seam 3c), answered from the story editor's
records/story_review.json with no manual QC row. Ported from the host's check_story_review and check_paper_read.

The review names the plan it read as its "plan": the engine's judged digest of the other evidence (records/paper.md
and records/cut_plan2.json), the host's paper.py mark; a review of another plan fails naming the digest to read, so an
unchanged old review cannot pass a new plan.
judge(evidence) -> {"state", "words"}: blocked when the review cannot be read as the engine hashed it; fail with
every problem named when a read is missing, misshapen, or says it lands over its own evidence, a note's "owner" is
not one catalog role (or "none"), or it says it does not land; else pass (seam 3d: the closing read and the note owners,
from the host's closing_problem, closing_line_problem and note_owner_problem).
"""
import json
import re
from pathlib import Path

from ..spine.engine import data, judged
from ..spine.files import digest
REVIEW = "records/story_review.json"
# A promise on screen is paid or teased within this, or a read cannot say the cut lands. Kept here, not in
# settings.json, whose whole content keys every settled plan (plan.settle_key): a story threshold re-keys no plan.
OPEN_LOOP_S = 60.0
SHAPES = {
    "landing_fact": '"landing_fact": {"fact": the fact or claim the closing line lands on, "earlier": [each earlier '
                    'telling of it in the cut]}',
    "gag_setups": '"gag_setups": [{"gag": the gag and its time, "setup": the line that sets it up and where it plays '
                  'before the gag, or "none"}], [] when the cut has no gag',
    "button": '"button": {"line": the recorded sign-off with its time, or "none" when none was recorded, "ends": true '
              'when the video ends on it}',
    "each_point_once": '"each_point_once": [{"point": a point told more than once, "tellings": [each telling\'s time '
                       'and line]}], [] when every point is told once',
    "open_loops": '"open_loops": [{"promise": a promise made on screen with its time, "paid": where it is paid, or '
                  '"none", "gap_s": the seconds between them, "tease": the reminder between them, or "none"}], [] when '
                  'the cut makes none',
    "pace": '"pace": [{"at": the time, "what": the hold, setup or card that stays past its point, "trim_s": the seconds '
            'to take out}], [] when nothing overstays',
    "look": '"look": [{"at", "what"}], each thing only the render can show that the story editor will look at first, '
            '[] when the paper is enough',
}
# A closing line ends on a concrete punch or a callback, in about twelve words, one or two short sentences.
CLOSING_WORDS = 16
CLOSING_BAD = ("instruction", "moral", "summary")
CLOSING_KINDS = ("punch", "callback") + CLOSING_BAD
SHAPES["closing"] = ('"closing": {"line": the last line said before the button, with its time, "kind": one of '
                     + ", ".join(CLOSING_KINDS) + ', "offer": a closing line offered to record, or "none"}')
REFRAME = re.compile(r"\b(?:isn['’]t|is not|wasn['’]t|was not|not)\b[^.!?]{0,80}?[,;.—–-]\s*(?:but\s+)?"
                     r"(?:it['’]s|it is|it was|that['’]s)\b|\bis\b[^.!?]{0,60},\s*not\b", re.I)
ORDERS = ("decide", "ask", "think", "remember", "consider", "choose", "check", "try", "go", "stop", "make", "look",
          "keep", "don't", "don’t", "do", "be", "get", "start", "pick", "vote", "tell", "let")
TELLS = ("you should", "you need to", "you have to", "you must", "ask yourself", "make sure", "so ask", "so decide",
         "so remember", "so go")
MORALS = ("the lesson", "the moral", "the takeaway", "bottom line", "at the end of the day", "in the end", "that's why",
          "that’s why", "which is why", "in short", "to sum up", "the point is", "what matters", "the real question",
          "the question is", "and that's the", "all comes down to", "the truth is")


def none(x):
    return str(x if x is not None else "").strip().strip(".").lower() in ("", "none", "no", "n/a", "nothing")


def text(x, k):
    return isinstance(x, dict) and isinstance(x.get(k), str) and bool(x[k].strip())


def strings(v):
    return isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)


def rows(v, *keys):
    """The read's list when every row is an object carrying each key as text; None when it is misshapen."""
    return v if isinstance(v, list) and all(all(text(x, k) for k in keys) for x in v) else None


def closing_line(line):
    """Why an offered closing line would not land, or None: empty, too long, a reframe, an instruction or a moral."""
    words = re.findall(r"[\w'’]+", line)
    if not words:
        return "it is empty"
    low = " ".join(line.lower().split())
    if len(words) > CLOSING_WORDS:
        return f"it runs {len(words)} words; a closing line is about twelve, one or two short sentences, punch word last"
    if REFRAME.search(low):
        return "it is a reframe (\"isn't X, it's Y\" or \"is X, not Y\"): say the punch, not the correction"
    firsts = [x.split()[0].strip("\"'“”‘’,") for x in re.split(r"(?<=[.!?])\s+|^so\s+|,\s*so\s+", low) if x.split()]
    told = next((t for t in TELLS if t in low), None) or next((f for f in firsts if f in ORDERS), None)
    if told:
        return f'it tells the viewer what to do ("{told}"): end on the punch or a callback, never an instruction'
    moral = next((m for m in MORALS if m in low), None)
    return (f'it is a moral or a summary ("{moral}"): end on a concrete image or the title turned, never the lesson'
            if moral else None)


def owners(d):
    """Why the review's notes cannot stand: somewhere in it an "owner" is not one catalog role or "none"."""
    roles, bad = set(data("catalog.json")["roles"]) | {"none"}, []

    def walk(x, path):
        if isinstance(x, dict):
            if "owner" in x and not (isinstance(x["owner"], str) and x["owner"] in roles):
                bad.append(f'{path or "the review"} "owner": {json.dumps(x["owner"])[:80]}')
            for k, v in x.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(x, list):
            for i, v in enumerate(x):
                walk(v, f"{path}[{i}]")
    walk(d, "")
    return [f"a note's owner is not one role ({b}): an owner is one of {', '.join(sorted(roles))}, and anything more "
            "goes in the note's own words" for b in bad]


def field(key, d):
    """Why one named read is not on record, or cannot stand beside "lands": true; None when it holds."""
    v, lands, shape = d.get(key), d["lands"] is True, f"it names no {SHAPES[key]}"
    if key == "landing_fact":
        if not text(v, "fact") or not strings(v.get("earlier")):
            return shape
        early = v["earlier"]
        return (f'it says "lands": true while the fact the close lands on is told earlier ({str(early[0])[:120]}): '
                "the earlier telling goes or becomes a tease") if lands and early else None
    if key == "button":
        if not text(v, "line") or (not none(v["line"]) and not isinstance(v.get("ends"), bool)):
            return shape
        return (f'it says "lands": true while the video does not end on the recorded sign-off ({v["line"][:100]})'
                if lands and not none(v["line"]) and not v["ends"] else None)
    if key == "gag_setups":
        if rows(v, "gag", "setup") is None:
            return shape
        bare = [x["gag"] for x in v if none(x["setup"])]
        return (f'it says "lands": true while a gag plays with no setup in the kept cut ({bare[0][:100]})'
                if lands and bare else None)
    if key == "each_point_once":
        if rows(v, "point") is None or [x for x in v if not (strings(x.get("tellings")) and x["tellings"])]:
            return shape
        twice = [x for x in v if len(x["tellings"]) > 1]
        return (f'it says "lands": true while a point is told {len(twice[0]["tellings"])} times '
                f'({twice[0]["point"][:100]})' if lands and twice else None)
    if key == "open_loops":
        def gap(x):
            g = x.get("gap_s")
            return isinstance(g, (int, float)) and not isinstance(g, bool) and 0 <= g < float("inf")
        if rows(v, "promise", "paid") is None or [x for x in v if not (none(x["paid"]) or gap(x))]:
            return shape
        limit = OPEN_LOOP_S
        left = [x for x in v if none(x["paid"]) or (x["gap_s"] > limit and (not text(x, "tease") or none(x["tease"])))]
        if not (lands and left):
            return None
        how = "never paid" if none(left[0]["paid"]) else f"paid {left[0]['gap_s']:g} s later with no tease; the limit is {limit:g} s"
        return f'it says "lands": true while a promise is left open ({left[0]["promise"][:100]}: {how})'
    if key == "closing":
        if not (text(v, "line") and v.get("kind") in CLOSING_KINDS and text(v, "offer")):
            return shape
        if lands and v["kind"] in CLOSING_BAD:
            return (f'it says "lands": true while the closing line is {"an" if v["kind"][0] in "aeiou" else "a"} '
                    f'{v["kind"]} ({v["line"][:100]}): quote it as a fix and end on an earlier punch, or offer a pickup')
        bad = None if none(v["offer"]) else closing_line(v["offer"])
        return f'the closing line it offers ({v["offer"][:100]}) does not land: {bad}' if bad else None
    return shape if rows(v, *{"pace": ("at", "what"), "look": ("what",)}[key]) is None else None


def problems(d, plan):
    """Every reason the review does not stand as a paper read of `plan` that lands; [] when it does."""
    if not isinstance(d, dict) or not isinstance(d.get("lands"), bool):
        return ['it has no "lands": true or false']
    found = [] if d.get("plan") == plan else [
        f'its "plan" is {d.get("plan")!r} and the plan it reads is {plan}: read records/paper.md as it stands and '
        'name that "plan"']
    found += [f for f in map(lambda k: field(k, d), SHAPES) if f] + owners(d)
    if d["lands"] is False:
        found.append("the story editor's read says the plan does not land: the editor re-plans "
                     "(records/cut_plan2.json and records/paper.md), which reopens plan and paper")
    return found


def judge(evidence):
    try:
        raw = Path(evidence[REVIEW]["path"]).read_bytes()
        if digest(raw) != evidence[REVIEW]["sha256"]:
            raise ValueError(f"{REVIEW} changed since the engine's evidence")
        d = json.loads(raw)
    except (OSError, ValueError, KeyError) as e:
        return {"state": "blocked", "words": [f"{REVIEW}: {e}"]}
    words = [f"{REVIEW}: {p}" for p in problems(d, judged(evidence, [REVIEW]))]
    return {"state": "fail" if words else "pass", "words": words}
