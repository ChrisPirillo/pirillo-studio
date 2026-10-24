"""The paper's measured prompts (queue item 37 seam 3e), ported from the host's paper.py: questions the story
editor's paper read answers, never findings. settle writes them beside paper.md; paper-read fails a read that
leaves one unanswered.

measure(spans) -> [{id, kind, at (story s), what, ...}]: spans are (src [a, b], kept words) in story order, each
span with words one block and one take on a story clock that runs on by each span's source length. Source prompts
(one site on two of the headline plan's cards) wait for the overlays stage, which writes records/callouts_plan.json
after the plan; at plan time there are no cards, so none is measured.
"""
import bisect
import collections
import re

# A promise on screen is paid or teased within this, or a read cannot say the cut lands. Kept here, not in
# settings.json, whose whole content keys every settled plan (plan.settle_key): a story threshold re-keys no plan.
OPEN_LOOP_S = 60.0
# The host's paper_limits: checklist qc.story.pace (hold_s) and qc.story.said-once (share) thresholds, OPEN_LOOP_S.
LIMITS = {"hold_s": 2.0, "loop_s": OPEN_LOOP_S, "share": 0.5}
# The forward references a cut can leave open; PROMISE adds the ones that are a promise wherever they sit.
FWDREF = [r"\bi'?ll (get to|come back to|talk about) (that|this|it)\b", r"\blater\b", r"\bin a (minute|second|bit)\b",
          r"\bcoming up\b", r"\bmore on (that|this)\b"]
PROMISE = FWDREF + [r"\bthere(?:'s| is| are) (?:kind of |sort of |actually )?(?:a|one|another) (?:way|trick|workaround|catch)\b",
                    r"\b(?:i'?ll|i will|we'?ll|i'?m going to|i am going to|we'?re going to|let me) (?:show|tell|explain)\b",
                    r"\bstick around\b", r"\bhold that thought\b"]
APART_S = 20.0                  # two tellings closer than this are one telling
SHARED_MIN = 2                  # key words two lines share before they are compared at all
COMMON = 0.08                   # a key word in more than this share of the lines is the topic, not a point
RARE = 4                        # a key word said this many times or fewer names one thing
WINDOW = (10, 14)               # words before and after a promise that say what it promises
CLOSE_S = 20.0                  # the close: the last seconds of the kept speech
CLOSE_LETTERS = 6               # a word of the close this long, or a number, is one a viewer would know again
MOST = {"hold": 10, "loop": 6, "twice": 6, "close": 4, "source": 4}     # prompts of a kind, the strongest first
STOP = frozenset("""a about above after again against all almost also am an and any anybody are aren't as at be because been
before being below between both but by can can't cannot could did didn't do does doesn't doing don't down during each even
ever every few for from further get gets getting go goes going gonna got had has have having he her here hers him his how i
i'd i'll i'm i've if in into is isn't it it's its just kind know let like lot made make many may maybe me mean might more
most much must my need never no nor not now of off oh ok okay on once one only or other our out over own pretty quite
really right said same say see she should so some something sort still stuff such sure take than that that's the their them
then there there's these they they're thing things think this those though through to too under until up us use used very
want was wasn't way we we're well were what what's when where whether which while who why will with won't would yeah yes
yet you you're your""".split())
_END = re.compile(r"[.?!][\"')\]]*$")
FS_MAX = 6                     # words: a fragment he drops is short ("in November", "You just can't.")
FS_GAP = 6.0                   # s: said again within a few seconds as the start of the next kept line


def norm(w):
    return re.sub(r"[^a-z0-9']+", "", str(w).lower().replace("’", "'"))


def key(w):
    """A word as the measures compare it: lowercased, no possessive, no plural s; None for a word that carries
    no point (STOP, or under three letters)."""
    t = re.sub(r"'s$", "", norm(w))
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]
    return t if len(t) >= 3 and t not in STOP else None


def tc(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


def _brief(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def lines_of(ws):
    """The kept words as lines: a sentence each, broken at a block's end too. [[word, ...], ...]."""
    out, cur = [], []
    for i, w in enumerate(ws):
        cur.append(w)
        nxt = ws[i + 1] if i + 1 < len(ws) else None
        brk = _END.search(w["w"]) or nxt is None or nxt["block"] != w["block"]
        if not brk and len(cur) >= 2 and len(cur) <= FS_MAX and nxt is not None:
            c_toks = [norm(x["w"]) for x in cur if norm(x["w"])]
            if len(c_toks) >= 2 and 0 <= nxt["t"] - w["te"] <= FS_GAP:
                nxt_toks = [norm(x["w"]) for x in ws[i + 1:i + 1 + len(c_toks) + 5] if norm(x["w"])]
                if len(nxt_toks) > len(c_toks) and nxt_toks[:len(c_toks)] == c_toks:
                    brk = True
        if brk:
            out.append(cur)
            cur = []
    return out


def said(line):
    return " ".join(w["w"] for w in line)


def measure(spans):
    """The settled plan's prompts: the host's word rows {w, src, t, te, take, block} built from (src, words) pairs."""
    ws, off = [], 0.0
    for n, ((a, b), words) in enumerate(spans):
        ws += [dict(w=w["text"], src=w["start"], t=off + w["start"] - a, te=off + w["end"] - a, take=n, block=n)
               for w in words]
        off += b - a
    return prompts(ws, lines_of(ws), off, LIMITS)


def prompts(ws, lines, total, limits, cards=()):
    """Every measured prompt of a plan, [{id, kind, at (story s), what}], holds first then by time. limits:
    {"hold_s", "loop_s", "share"} (LIMITS). cards: [{id, site}] of the headline plan."""
    out = []
    # hold: a silence between two kept words longer than the limit
    holds = [(ws[i + 1]["t"] - ws[i]["te"], i) for i in range(len(ws) - 1) if ws[i + 1]["t"] - ws[i]["te"] > limits["hold_s"]]
    for gap, i in sorted(holds, reverse=True)[:MOST["hold"]]:
        tail = " ".join(w["w"] for w in ws[max(0, i - 5):i + 1])
        out.append(dict(id=f"hold@{ws[i]['src']:.1f}", kind="hold", at=ws[i]["te"], gap_s=round(gap, 1),
                        what=f"{gap:.1f} s with no word after \"{_brief(tail, 60)}\" (then \"{_brief(' '.join(w['w'] for w in ws[i + 1:i + 5]), 40)}\"); "
                             f"the limit is {limits['hold_s']:g} s" + ("" if ws[i]["take"] == ws[i + 1]["take"] else ", across a cut")))
    keys = [key(w["w"]) for w in ws]
    df = collections.Counter(k for k in keys if k)
    # loop: a promise whose words come back later than the limit, or never
    text = [norm(w["w"]) for w in ws]
    starts, pos = [], 0
    for t in text:
        starts.append(pos)
        pos += len(t) + 1
    joined, loops, seen, ends = " ".join(text), [], set(), []
    for ln in lines:                                          # where each line stops, as a place among the words
        ends.append((ends[-1] if ends else 0) + len(ln))
    for pat in PROMISE:
        for m in re.finditer(pat, joined):
            i, j = bisect.bisect_right(starts, m.start()) - 1, bisect.bisect_right(starts, m.end() - 1) - 1
            if i in seen:
                continue
            seen.add(i)
            n = bisect.bisect_right(ends, j)                  # the promise's own line says what it promises
            a, b = (ends[n - 1] if n else 0), ends[min(n, len(ends) - 1)]
            rare = {k for k in keys[max(a, i - WINDOW[0]):min(b, j + WINDOW[1] + 1)] if k and df[k] <= RARE}
            hit = next((x for x in range(b, len(ws)) if keys[x] in rare), None)
            gap = ws[hit]["t"] - ws[j]["te"] if hit is not None else None
            if gap is not None and gap <= limits["loop_s"]:
                continue
            here = " ".join(w["w"] for w in ws[max(a, i - 4):min(b, j + 9)])
            back = (f"its words come back {gap:.0f} s later, at {tc(ws[hit]['t'])} (\"{_brief(' '.join(w['w'] for w in ws[max(0, hit - 5):hit + 6]), 70)}\")"
                    if hit is not None else "none of its words come back")
            loops.append((gap if gap is not None else total, dict(id=f"loop@{ws[i]['src']:.1f}", kind="loop", at=ws[i]["t"],
                                                                 gap_s=None if gap is None else round(gap),
                                                                 what=f"a promise, \"{_brief(here, 80)}\": {back}; the limit is {limits['loop_s']:g} s")))
    out += [p for _, p in sorted(loops, key=lambda x: -x[0])[:MOST["loop"]]]
    # twice: two lines well apart that share their key words
    sets = [{key(w["w"]) for w in ln} - {None} for ln in lines]
    cap = max(3, int(COMMON * len(lines)))
    lf = collections.Counter(k for s in sets for k in s)
    sets = [{k for k in s if lf[k] <= cap} for s in sets]
    pairs = []
    for a in range(len(lines)):
        for b in range(a + 1, len(lines)):
            both = sets[a] & sets[b]
            if len(both) < SHARED_MIN or lines[b][0]["t"] - lines[a][-1]["te"] < APART_S:
                continue
            share = len(both) / max(1, min(len(sets[a]), len(sets[b])))
            if share >= limits["share"]:
                pairs.append((len(both), share, a, b, sorted(both)))
    for n, share, a, b, both in sorted(pairs, key=lambda x: (-x[0], -x[1], x[2]))[:MOST["twice"]]:
        out.append(dict(id=f"twice@{lines[a][0]['src']:.1f}+{lines[b][0]['src']:.1f}", kind="twice", at=lines[a][0]["t"], share=round(share, 2),
                        what=f"shares {', '.join(both[:4])} ({share:.0%} of the shorter line's key words; the limit is {limits['share']:.0%}) with "
                             f"{tc(lines[b][0]['t'])}: \"{_brief(said(lines[a]), 56)}\" / \"{_brief(said(lines[b]), 56)}\""))
    # close: a key word of the last seconds, said earlier
    first = next((n for n, ln in enumerate(lines) if ln[-1]["te"] >= total - CLOSE_S), len(lines))
    told = []
    for k in sorted({k for s in sets[first:] for k in s if df[k] <= RARE and (len(k) >= CLOSE_LETTERS or any(c.isdigit() for c in k))}):
        earlier = next((n for n in range(first) if k in sets[n] and lines[first][0]["t"] - lines[n][-1]["te"] >= APART_S), None)
        if earlier is not None:
            told.append((earlier, k))
    by = collections.defaultdict(list)
    for n, k in told:
        by[n].append(k)
    for n in sorted(by, key=lambda n: (-len(by[n]), n))[:MOST["close"]]:
        out.append(dict(id=f"close@{lines[n][0]['src']:.1f}", kind="close", at=lines[n][0]["t"],
                        what=f"the close (from {tc(lines[first][0]['t'])}) says {', '.join(by[n])}, and so does this line: "
                             f"\"{_brief(said(lines[n]), 90)}\""))
    # source: one site on two cards of the headline plan
    sites = collections.defaultdict(list)
    for c in cards:
        if c["site"]:
            sites[c["site"]].append(c["id"])
    for site in sorted((x for x in sites if len(sites[x]) > 1), key=lambda x: (-len(sites[x]), x))[:MOST["source"]]:
        out.append(dict(id=f"source@{site}", kind="source", at=0.0,
                        what=f"the headline plan puts {site} on {len(sites[site])} cards ({', '.join(sites[site][:4])}): one source, proving two lines?"))
    return out
