"""The release.<name> checks: each a measured fact against one named setting (release.md section 4).

A reading is {check, row, value, setting, result, maker}; a failing one refuses the release naming all six, and
release.search-phrase only warns (host qc.copy.search-phrase is a warn per qc.rules)."""
import datetime as dt
import math
import re
from urllib.parse import parse_qs, urlsplit

from .inputs import CUES, PUBLISH, Unavailable, field, setting
from .rows import disclosures, kind, seconds, words as row_words

SKIP = re.compile(r"(#\w+|@[\w.]+|https?://\S+|\{long_url\}|^Topic:.*$)", re.M)  # host wordcopy.py SKIP_RE
HASHTAG = re.compile(r"(?<![\w&])#\w+")
TOPIC = re.compile(r"^[ \t]*Topic:[ \t]*([^\n]*)$", re.M | re.I)  # host wordcopy.py TOPIC_ANY_RE
BREAK = re.compile(r"[.?!][\"'”’]?\s+\S")  # host qc.py run_chapters: a sentence break inside the cue


HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com",
         "www.youtube-nocookie.com"}


def link_id(link, kind):
    """A video or playlist id read only from a YouTube link, in every form the host reads as his; else None."""
    try:
        url = urlsplit(link if "//" in link else "https://" + link)
        if url.scheme not in ("http", "https"):
            return None
        host, parts, query = url.hostname, [p for p in url.path.split("/") if p], parse_qs(url.query)
        if host == "youtu.be" and kind == "video":
            ids = parts[:1]
        elif host in HOSTS and kind == "playlist" and parts == ["playlist"]:
            ids = query.get("list", [])
        elif host in HOSTS and kind == "video" and parts == ["watch"]:
            ids = query.get("v", [])
        elif host in HOSTS and kind == "video" and len(parts) > 1 and parts[0] in ("shorts", "embed"):
            ids = parts[1:2]
        else:
            return None
        ids = set(ids)  # a repeated v= is one id; two different ids name no one video
        pattern = r"[A-Za-z0-9_-]{11}" if kind == "video" else r"[A-Za-z0-9_-]+"
        return ids.pop() if len(ids) == 1 and re.fullmatch(pattern, next(iter(ids))) else None
    except ValueError:
        return None


def number(s, name):
    v = setting(s, name, float)
    if not math.isfinite(v):
        raise Unavailable(f"release.settings {name}: non-finite number")
    return v


def reading(check, row, value, name, ok, maker, miss="fail"):
    return {"check": f"release.{check}", "row": row, "value": value, "setting": name,
            "result": "pass" if ok else miss, "maker": maker}


def window(today, day, limit, what):
    """{seen, days, from, until}: a day's age on the build's evaluation day and the days it passes in; a malformed
    day is unavailable naming it. Its result is recomputed on later days (fresh), so crossing `until` stales it."""
    try:
        seen = dt.date.fromisoformat(day[:10])
    except (TypeError, ValueError) as e:
        raise Unavailable(f"{what}: not a day ({e})") from None
    return {"seen": day, "days": (dt.date.fromisoformat(today) - seen).days, "from": seen.isoformat(),
            "until": (seen + dt.timedelta(days=math.floor(limit))).isoformat()}


def within(v, today):
    return v is not None and v["from"] <= today <= v["until"]


def fresh(rs, today):
    """Every dated reading still gives its recorded result on `today`."""
    return all(within(r["value"]["window"], today) == (r["result"] == "pass")
               for r in rs if isinstance(r["value"], dict) and r["value"].get("window"))


def patterns(s, name):
    try:
        return [re.compile(x, re.I) for x in setting(s, name, list)]
    except (TypeError, re.error) as e:
        raise Unavailable(f"release.settings {name}: not patterns ({e})") from None


def norm(w):
    return re.sub(r"[^a-z0-9']", "", w.lower().replace("’", "'"))


def not_his(text, pool, skip):
    """Words of text not found, in order, in his transcript (host wordcopy.py check, already right)."""
    bad = []
    for sent in re.split(r"(?<=[.?!])\s+|\n+", SKIP.sub(" ", text)):
        toks = [norm(w) for w in sent.split() if norm(w)]
        if not toks:
            continue
        best = None
        for i, w in enumerate(pool):
            if w != toks[0]:
                continue
            miss, j = [], i
            for tk in toks[1:]:
                k = next((k for k in range(j + 1, min(len(pool), j + 2 + skip)) if pool[k] == tk), None)
                if k is None:
                    miss.append(tk)
                else:
                    j = k
            if best is None or len(miss) < len(best):
                best = miss
            if not miss:
                break
        bad += [toks[0]] + [t for t in toks[1:] if t not in pool] if best is None else best
    return bad


def same(rows, outs):
    """Rows whose bodies match once tags and links are gone: two platforms of one output, or one platform on two
    outputs (host wordcopy.py same, already right). The text post is one body everywhere by design."""
    def body(r):
        return " ".join(SKIP.sub(" ", row_words(r, outs)).split()).lower()
    out, within, across = [], {}, {}
    for r in (r for r in rows if r["output"] is not None):
        b = body(r)
        twins = [x["id"] for x in within.get((r["output"], b), []) + across.get((r["platform"], b), [])] if b else []
        within.setdefault((r["output"], b), []).append(r)
        across.setdefault((r["platform"], b), []).append(r)
        out.append(reading("same", r["id"], twins, "map", not twins, r["by"]))
    return out


def banned_term(term):
    """host wordcopy.py do_not_say: the term's words joined by spaces or hyphens, any case, never inside a word or
    a hashtag; a term with no words is unavailable."""
    ws = re.findall(r"[A-Za-z0-9']+", term) if isinstance(term, str) else None
    if not ws:
        raise Unavailable(f"release.settings do_not_say: {term!r} has no words")
    return re.compile(r"(?<![\w#])" + r"[\s\-]*".join(map(re.escape, ws)) + r"(?!\w)", re.I)


def squash(t):
    return re.sub(r"[^a-z0-9]+", "", t.lower()).rstrip("s")


def fields(outs):
    """(row, words) published outside the rows, as host run_do_not_say reads them: each output's title, the A/B
    titles, the pin, the tags and the chapter names."""
    out = [(f"{o['id']}#title", o["title"]) for o in outs]
    for o in (o for o in outs if o["id"] == "long"):
        out += [(f"long#{t}", t) for t in o.get("ab_titles", [])] + [("long#pinned_comment", o["fields"]["pinned_comment"]),
                                                                       ("long#tags", ", ".join(o["fields"]["tags"]))]
        out += [(f"long#{c['t']}", c["name"]) for c in o["chapters"]]
    return out


def tag(name, most):
    """host wordcopy.py topic_tag: no '.' or '&', one space between words, cut to topic.max_chars."""
    return " ".join(re.sub(r"[.&]", "", name).split())[:int(most)]


def per_row(today, s, rows, outs, pool, book):
    out = []
    skip, fill = int(number(s, "his_words.max_skip")), setting(s, "length.long_url", str)
    banned = [(b, banned_term(b)) for b in setting(s, "do_not_say", list)]
    literals = setting(s, "literals", list)
    if not all(isinstance(x, str) and x for x in literals):
        raise Unavailable("release.settings literals: an entry is not text")
    literals = [(x, re.compile(r"(?<!\w)" + re.escape(x) + r"(?!\w)", re.I)) for x in literals]  # host run_literals
    topic_on, topic_max, fresh = setting(s, "topic.platforms", list), number(s, "topic.max_chars"), number(s, "topic.fresh_days")
    book = {k.casefold(): v for k, v in book.items()}
    captions = {r["id"] for r in rows if r["output"] is not None and r["id"] != "long:youtube"}
    texts = [(r["id"], row_words(r, outs), r["by"]) for r in rows] + [(i, t, "packager") for i, t in fields(outs)]
    for rid, text, by in texts:
        tags = {squash(h) for h in HASHTAG.findall(text)}
        hits = [b for b, rx in banned if rx.search(text) or squash(b) in tags]
        out.append(reading("do-not-say", rid, hits, "do_not_say", not hits, by))
        if "#" not in rid or rid == "long#pinned_comment":  # host run_literals: rows and the pin
            hits = [x for x, rx in literals if rx.search(text)]
            out.append(reading("literals", rid, hits, "literals", not hits, by))
        if rid == "long#pinned_comment" or rid in captions:  # by output and platform, whoever made the words
            missing = not_his(text, pool, skip)
            out.append(reading("his-words", rid, missing, "his_words.max_skip", not missing, by))
    for r in rows:
        text, p, rid, by = row_words(r, outs), r["platform"], r["id"], r["by"]
        most, n = number(s, f"platforms.{p}.max_chars"), len(text.replace("{long_url}", fill))  # sent with the link filled
        out.append(reading("length", rid, n, f"platforms.{p}.max_chars", n <= most, by))
        if r["output"] is None:  # host run_hashtags and topic_problems read captions only
            continue
        lo, hi = number(s, f"platforms.{p}.hashtags.min"), number(s, f"platforms.{p}.hashtags.max")
        n = len(HASHTAG.findall(text))
        out.append(reading("hashtags", rid, n, f"platforms.{p}.hashtags", lo <= n <= hi, by))
        names = [t.strip() for t in TOPIC.findall(text)]
        if p not in topic_on:
            out.append(reading("topic", rid, names, "topic.platforms", not names, by))
            continue
        last = [x for x in text.splitlines() if x.strip()][-1:]
        chain = [t for t in (tag(x, topic_max) for x in names[0].split(">")) if t] if len(names) == 1 else []
        ok = not names or (bool(chain) and bool(TOPIC.match(last[0])))  # optional, one, the last line
        out.append(reading("topic", rid, names, "topic.platforms", ok, by))
        if names and ok:  # host topic_pick: the first fresh tag in the chain; stale only warns
            seen = {t: window(today, book[t.casefold()], fresh, f"topics {t}") for t in chain if t.casefold() in book}
            pick = next((t for t in chain if within(seen.get(t), today)), chain[0])
            out.append(reading("topic", rid, {"tag": pick, "chain": chain, "window": seen.get(pick)}, "topic.fresh_days",
                               within(seen.get(pick), today), by, "warn"))
    return out + same(rows, outs)


def searched(s, rows, outs, phrases):
    """Per output, how many of its rows carry a phrase people search (the host's search_phrases); warns."""
    least, want = number(s, "search.min_rows"), [" ".join(p.lower().split()) for p in phrases]
    out = []
    for o in outs:
        n = sum(any(p in " ".join(row_words(r, outs).lower().split()) for p in want) for r in rows if r["output"] == o["id"])
        out.append(reading("search-phrase", o["id"], n, "search.min_rows", n >= least, "packager", "warn"))
    return out


def text_lint(video, s, rows, outs):
    """Each text post row's final words (after owner edits): a top heading, when there is one, ends with exactly
    release.settings text.headline_end (host headline_problems), and the host's lint_text(words) finds nothing
    (host postlint FAILs); an absent or broken lint is unavailable."""
    out, end = [], setting(s, "text.headline_end", str)
    if not end:
        raise Unavailable("release.settings text.headline_end: empty")
    for r in (r for r in rows if r["output"] is None):
        text = row_words(r, outs)
        first = text.lstrip().split("\n", 1)[0]
        head = first[2:].strip() if first.startswith("# ") else None
        bare = head[:-len(end)] if head and head.endswith(end) else None
        ok = head is None or (bare is not None and not bare.endswith(end[-1]) and bool(bare.strip()))
        out.append(reading("text", r["id"], head, "text.headline_end", ok, r["by"]))
        lint = getattr(video, "lint_text", None)
        if lint is None:
            raise Unavailable("lint_text: the host supplies none")
        try:
            found = lint(text)
        except Exception as e:
            raise Unavailable(f"lint_text: unavailable ({e})") from None
        if not isinstance(found, list) or not all(isinstance(x, str) for x in found):
            raise Unavailable("lint_text: not a list of problems")
        out.append(reading("text", r["id"], found, "lint_text", not found, r["by"]))
    return out


def per_output(video, s, outs, publish, titled, cues, chat, today, pool, said):
    out = []
    for o in outs:
        if kind(o["id"]) == "short":  # host run_cover_text reads shorts alone
            c, why = o["cover"], " ".join(o["cover"]["why"].split())
            n, one = len(c["text"].split()), "\n" not in c["text"].strip()  # host cover_text_problem
            if n:
                out.append(reading("cover", o["id"], {"words": n, "one_line": one}, "cover.words",
                                   number(s, "cover.words.min") <= n <= number(s, "cover.words.max") and one, "packager"))
            ok = any(p.search(why) for p in patterns(s, "titles.why_evidence")) if n else any(
                p.match(why) for p in patterns(s, "cover.none"))  # host why_problem, or a recorded none
            out.append(reading("cover", o["id"], why, "titles.why_evidence" if n else "cover.none", ok, "packager"))
        if o["id"] != "long":
            continue
        own = {k: setting(s, f"own_urls.{k}s", list) for k in ("video", "playlist")}
        for k, ids in own.items():
            if any(not isinstance(i, str) or not i for i in ids):
                raise Unavailable(f"release.settings own_urls.{k}s: not a list of ids")
        ends = o["fields"]["end_screen"]
        foreign = [ends[k] for k in own if ends[k] and link_id(ends[k], k) not in own[k]]
        out.append(reading("own-urls", "long", foreign, "own_urls", not foreign, "packager"))
        owed = disclosures(s, publish, chat)
        lacking = [x for x in owed if x not in o["description"]["text"]]
        out.append(reading("disclosure", "long:youtube", {"chat_shown": chat, "ai_label": o["fields"]["ai_label"],
                                                          "lacking": lacking}, "disclosure", not lacking, "packager"))
        out += chapters(s, o, cues)
        if titled:
            out += titles(video, s, publish, o, today, pool, said)
    return out


def chapters(s, o, cues):
    """host qc.py run_chapters: a chapter after 0:00 starts strictly within chapters.near_s of a cue start, or
    inside a cue whose lines hold a sentence break."""
    near, cs = number(s, "chapters.near_s"), []
    for c in field(cues, "cues", CUES, "captions", list):
        ends = [c.get(k) if isinstance(c, dict) else None for k in ("start_s", "end_s")]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in ends):
            raise Unavailable(f"{CUES}: a cue start or end is not a finite number")
        lines = c.get("lines")
        if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
            raise Unavailable(f"{CUES}: a cue's lines are not text")
        cs.append((*ends, " ".join(lines)))
    if not cs:
        raise Unavailable(f"{CUES}: no cues")
    out = []
    for c in o["chapters"]:
        t = seconds(c["t"], PUBLISH)
        if t == 0:
            continue
        off = min(abs(a - t) for a, _, _ in cs)
        broken = any(a < t < b and BREAK.search(x) for a, b, x in cs)
        out.append(reading("chapters", f"long#{c['t']}", {"off": off, "inside_break": broken}, "chapters.near_s",
                           off < near or broken, "packager"))
    return out


def titles(video, s, publish, o, today, pool, said):
    """Three titles, the lead first, each within length and scored by the external scorer, and each with its whole
    evidence as host why_problems and title_research_problems read it: a why naming evidence (titles.why_evidence), a
    searched phrase with its source (titles.sources) researched lately or a searched line titles.no_data names, a said quote of titles.quote_words or more in
    his transcript (or word ids into it), and no opening titles.never_open names (a how-to shape)."""
    count, most, least = number(s, "titles.count"), number(s, "titles.max_chars"), number(s, "titles.min_score")
    fewest = number(s, "titles.min_chars")
    recent, never = number(s, "titles.research_days"), [x.lower() for x in setting(s, "titles.never_open", list)]
    quoted, why_ok, sources = number(s, "titles.quote_words"), patterns(s, "titles.why_evidence"), patterns(s, "titles.sources")
    untested = patterns(s, "titles.no_data")
    ab = field(publish, "ab_titles", PUBLISH, "packager", list)
    titles_, flat = o["ab_titles"], f" {' '.join(pool)} "
    out = [reading("titles", "long", {"count": len(titles_), "lead_first": titles_[:1] == [o["title"]]}, "titles.count",
                   len(titles_) == count and titles_[:1] == [o["title"]], "packager")]
    for t, x in zip(titles_, ab):
        row, why = f"long#{t}", " ".join(field(x, "why", PUBLISH, "packager").split())
        out.append(reading("titles", row, len(t), "titles.min_chars" if len(t) < fewest else "titles.max_chars",
                           fewest <= len(t) <= most, "packager"))
        number_why = " ".join(str(x.get("number_why") or "").split())  # host run_titles_signals: a number needs its why
        out.append(reading("titles", row, number_why, "titles.why_evidence", bool(number_why) or not re.search(r"\d", t),
                           "packager"))
        out.append(reading("titles", row, why, "titles.why_evidence", any(p.search(why) for p in why_ok), "packager"))
        found = x.get("searched")
        if isinstance(found, str) and any(p.search(found) for p in untested):  # a title set up as the test
            out.append(reading("titles", row, found, "titles.no_data", True, "packager"))
        else:
            found = found if isinstance(found, dict) else {}
            phrase, source, day = found.get("phrase"), found.get("source"), found.get("at")
            out.append(reading("titles", row, source, "titles.sources",
                               isinstance(source, str) and any(p.search(source) for p in sources), "packager"))
            w = window(today, day, recent, f"{PUBLISH} {t} searched.at") if isinstance(day, str) else None
            out.append(reading("titles", row, {"phrase": phrase, "window": w}, "titles.research_days",
                               isinstance(phrase, str) and bool(phrase.strip()) and within(w, today), "packager"))
        tie = x.get("said") if isinstance(x.get("said"), dict) else {}
        ids, q = tie.get("words"), [norm(w) for w in str(tie.get("quote") or "").split() if norm(w)]
        ok = (isinstance(ids, list) and all(type(i) is int and 0 <= i < said for i in ids)
              if ids else len(q) >= quoted and f" {' '.join(q)} " in flat)
        out.append(reading("titles", row, tie, "titles.quote_words", ok, "packager"))
        opens = [n for n in never if re.match(re.escape(n) + r"\b", t.strip().lower())]
        out.append(reading("titles", row, opens, "titles.never_open", not opens, "packager"))
    scorer = getattr(video, "score_titles", None)
    if scorer is None:
        raise Unavailable("title scorer: the host supplies none")
    try:
        scores = scorer(list(titles_))
    except Exception as e:
        raise Unavailable(f"title scorer: unavailable ({e})") from None
    if (not isinstance(scores, list) or len(scores) != len(titles_)
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in scores)):
        raise Unavailable("title scorer: did not return one finite score per title")
    out += [reading("titles", f"long#{t}", v, "titles.min_score", v >= least, "packager") for t, v in zip(titles_, scores)]
    return out


def measure(video, s, outs, rows, publish, titled, transcript, cues, facts, today):
    """Every reading the inputs allow, dated `today` (the build's one evaluation day); an input, setting or adapter
    a reading needs and lacks is unavailable."""
    pool = [norm(w) for w in " ".join(w["text"] for w in transcript).split() if norm(w)]
    return (per_row(today, s, rows, outs, pool, facts["topics"]) + searched(s, rows, outs, facts["search_phrases"])
            + text_lint(video, s, rows, outs)
            + per_output(video, s, outs, publish, titled, cues, facts["chat_shown"], today, pool, len(transcript)))
