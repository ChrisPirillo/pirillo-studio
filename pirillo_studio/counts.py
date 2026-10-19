"""What each platform itself says about a post of his, read by the posting clock at one day and at seven, and what
the analyst files from the platforms no key reads (run as `studio.py counts ...`).

`s` is the studio whose ledger and keys these are: production passes its own studio module (tools/studio.py). It
gives the ledger (assets, row, Lock, load, save, log), the clock (now, tz, _when), the platform calls (_form, _http,
_g, key, IG_API, TH_API, FB_API, AdapterError), NO_BROWSER, and the Board's words
(plat_name, asset_file_label, against_usual, b5_meter).
"""

from pirillo_studio import buffer_api
import datetime as dt
import json
import os
import sys
import urllib.parse

# Counts come from every platform, not only YouTube. No model runs here: each reader is one GET with the key already on file. What a platform gives was read
# off its own API on 2026-10-01 (one live post each). A platform no key of the studio's reads is in COUNT_NEEDS with
# its own analytics page: the analyst reads it there in the browser, once a day on the morning run, and files what it
# saw with `studio.py counts` (a browser read once a day is enough). A route of None is recorded by the analyst in growth.json `sources` on its first read.
COUNT_DAYS = (1, 7)                  # a post is read at one day old and at seven,
COUNT_WINDOW = {1: 2.0, 7: 9.0}      # and no later than this many days old: a late reading is not that day's number
COUNT_KEYS = ("views", "likes", "comments", "shares")
COUNT_TRIES = 3                      # a reading that fails is tried on this many ticks, then left
COUNT_PER_TICK = 8                   # the most posts one tick reads
COUNT_TIMEOUT = 15                   # seconds a platform has to answer
COUNT_GIVES = {"instagram": ("views", "likes", "comments", "shares"), "threads": ("views", "likes", "comments", "shares"),
               "facebook": ("views", "likes", "comments"), "bluesky": ("likes", "comments", "shares"),
               "mastodon": ("likes", "comments", "shares")}
COUNT_NEEDS = {"x": "https://x.com/i/account_analytics/content?type=posts&sort=impressions&dir=desc&days=28",
               "tiktok": "https://www.tiktok.com/tiktokstudio/analytics", "linkedin": None, "substack": None}
COUNT_BROWSER_SLACK = 0.25           # days a browser reading may run past its window: the morning run is not on the minute
COUNT_WORDS = {"views": ("view", "views"), "likes": ("like", "likes"), "comments": ("reply", "replies"), "shares": ("share", "shares")}


def _insights(r):
    """Meta's insights answer as {metric: its lifetime value}."""
    return {x.get("name"): ((x.get("values") or [{}])[0] or {}).get("value") for x in (r.get("data") or []) if isinstance(x, dict)}


def count_instagram(s, a, cfg):
    i = _insights(s._form("GET", f"{s.IG_API}/{a['post_id']}/insights", s.key("instagram"), {"metric": "views,likes,comments,shares"}, timeout=COUNT_TIMEOUT)[1])
    return dict(views=i.get("views"), likes=i.get("likes"), comments=i.get("comments"), shares=i.get("shares"))


def count_threads(s, a, cfg):
    i = _insights(s._form("GET", f"{s.TH_API}/{a['post_id']}/insights", s.key("threads"), {"metric": "views,likes,replies,reposts,quotes,shares"}, timeout=COUNT_TIMEOUT)[1])
    return dict(views=i.get("views"), likes=i.get("likes"), comments=i.get("replies"),
                shares=sum(i.get(k) or 0 for k in ("reposts", "quotes", "shares")))


def count_facebook(s, a, cfg):
    r = s._form("GET", f"{s.FB_API}/{a['post_id']}", json.loads(s.key("facebook") or "{}").get("page_token"),
                {"fields": "views,likes.summary(true).limit(0),comments.summary(true).limit(0)"}, timeout=COUNT_TIMEOUT)[1]
    return dict(views=r.get("views"), likes=s._g(r, "likes", "summary", "total_count"), comments=s._g(r, "comments", "summary", "total_count"))


def count_bluesky(s, a, cfg):
    r = s._http("GET", "https://public.api.bsky.app/xrpc/app.bsky.feed.getPosts?uris=" + urllib.parse.quote(str(a["post_id"]), safe=""),
                None, timeout=COUNT_TIMEOUT)[1]
    post = (r.get("posts") or [None])[0]
    if not isinstance(post, dict):
        raise s.AdapterError("Bluesky has no post by that id", False)
    return dict(likes=post.get("likeCount"), comments=post.get("replyCount"), shares=(post.get("repostCount") or 0) + (post.get("quoteCount") or 0))


def count_mastodon(s, a, cfg):
    sid = str(a.get("url") or "").rstrip("/").rsplit("/", 1)[-1]     # the row's post_id is the scheduler's; the link ends in the post's own
    if not sid.isdigit():
        raise s.AdapterError("its link does not end in the post's id", False)
    r = s._http("GET", f"https://{cfg['platforms']['mastodon'].get('instance')}/api/v1/statuses/{sid}", None, timeout=COUNT_TIMEOUT)[1]
    return dict(likes=r.get("favourites_count"), comments=r.get("replies_count"), shares=r.get("reblogs_count"))


def count_buffer(s, a, cfg):
    """Experimental, daily refreshed Buffer data; absent metrics remain absent, not invented zeros."""
    post = buffer_api.buffer_read_post(s, a.get("buffer_post_id") or a["post_id"], s.key(buffer_api.buffer_account(s, cfg, a["platform"])), metrics=True)
    values = {m["type"]: m.get("value") for m in (post.get("metrics") or []) if m.get("unit") == "count"}
    return dict({k: values.get("reactions" if k == "likes" else k) for k in COUNT_KEYS},
                buffer_updated_at=post.get("metricsUpdatedAt"))


COUNT_READERS = {"instagram": count_instagram, "threads": count_threads, "facebook": count_facebook,
                 "bluesky": count_bluesky, "mastodon": count_mastodon}


def _buffer(a):
    return a.get("method") == "buffer" or bool(a.get("buffer_post_id"))


def count_day(s, a, t, slack=0.0):
    """(the day a reading of this post taken now would be: 1, 7 or None outside both windows, the post's age in days
    or None when it has no time)."""
    try:
        age = (t - s._when(a.get("posted_at") or a.get("scheduled_for"))).total_seconds() / 86400
    except (ValueError, TypeError, AttributeError):
        return None, None
    return next((d for d in COUNT_DAYS if d <= age < COUNT_WINDOW[d] + slack), None), age


def counts_due(s, led, t, browser=False):
    """[(post id, day)] of every reading that is due: a live post on a platform the studio can read, one day old
    (or seven) and not yet past that day's window, with no reading for that day and tries left. browser=True: the
    same for the platforms the analyst reads in the browser (COUNT_NEEDS), each due until a reading is filed."""
    out = []
    for v, a in s.assets(led):
        if browser and a["platform"] in s.NO_BROWSER:
            continue
        if (a["platform"] not in (COUNT_NEEDS if browser else COUNT_READERS) and not (_buffer(a) and not browser)) or a["state"] not in ("posted", "verified"):
            continue
        if a["platform"] == "tiktok" and not browser and not _buffer(a):
            continue
        day, st = count_day(s, a, t, COUNT_BROWSER_SLACK if browser else 0.0)[0], a.get("stats") or {}
        if day and browser and st.get(f"by_d{day}") not in ("api", "buffer", "browser"):
            out.append((a["id"], day))
        elif day and not browser and a.get("post_id") and f"pulled_d{day}" not in st and (st.get(f"tries_d{day}") or 0) < COUNT_TRIES:
            out.append((a["id"], day))
    return out


def browser_due(s, cfg, led, t=None):
    """What the analyst reads in the browser today, as lines to print: each platform with its analytics page, and
    under it every post inside a day-1 or day-7 window with no reading filed (its id, the day, its link)."""
    t, out, due = t or s.now(), [], {}
    for aid, day in counts_due(s, led, t, browser=True):
        due.setdefault(s.row(led, aid)[1]["platform"], []).append((aid, day))
    for p in COUNT_NEEDS:
        if p in due:
            out.append(f"{s.plat_name(p)}: " + (COUNT_NEEDS[p] or "its own post analytics (the route is in growth.json `sources`; record the one that works on your first read)"))
            for aid, day in due[p]:
                v, a = s.row(led, aid)
                out.append(f"  {aid}  day {day}  {a.get('url') or s.asset_file_label(v, a)}")
    return out or ["nothing is due today"]


def browser_counts(s, cfg, led, entries, t=None):
    """File what the analyst read off a platform's own analytics page: entries [{id, day (1 or 7; left out, the
    window the post is in now), views, likes, comments, shares}] -> (how many posts were written, one line per entry
    refused). It writes the fields collect_counts writes (views_d1 ... pulled_d1), marked by_d1 "browser". A count
    the page did not show is left out of the entry and stays out of the row: never a zero. A reading the clock made
    by key is kept, a post outside the day's window is refused (a late reading is not that day's number), and one
    entry's refusal never stops the rest."""
    t = t or s.now()
    today, done, said = t.astimezone(s.tz(cfg)).date().isoformat(), 0, []
    for n, e in enumerate(entries if isinstance(entries, list) else [entries], 1):
        aid = e.get("id") if isinstance(e, dict) else None
        a = s.row(led, aid)[1] if isinstance(aid, str) else None
        if not a:
            said.append(f"entry {n}: " + (f"no post {aid} in the ledger" if aid else "it names no post (\"id\")"))
            continue
        odd = sorted(k for k in e if k not in ("id", "day") + COUNT_KEYS)
        if odd:
            said.append(f"{aid}: {', '.join(odd)} is not a count the ledger keeps ({', '.join(COUNT_KEYS)})")
            continue
        if a["state"] not in ("posted", "verified"):
            said.append(f"{aid}: it is not live ({a['state']})")
            continue
        fits, age = count_day(s, a, t, COUNT_BROWSER_SLACK)
        day = e.get("day", fits)
        day = int(day) if not isinstance(day, bool) and str(day).isdigit() else None
        if day not in COUNT_DAYS or day != fits:
            said.append(f"{aid}: " + ("it has no posted time" if age is None else
                                      f"it is {age:.1f} days old, outside {'day ' + str(day) + chr(39) + 's window' if day in COUNT_DAYS else 'both windows'}"
                                      f" (day 1 is read at 1 to {COUNT_WINDOW[1]:g} days old, day 7 at 7 to {COUNT_WINDOW[7]:g})"))
            continue
        got, bad = {}, None
        for k in COUNT_KEYS:
            x = e.get(k)
            if x is None:
                continue
            if isinstance(x, str) and x.replace(",", "").strip().isdigit():
                x = int(x.replace(",", "").strip())
            if isinstance(x, bool) or not isinstance(x, int) or x < 0:
                bad = f"{aid}: {k} {json.dumps(e.get(k))} is not a whole number"
                break
            got[k] = x
        if bad or not got:
            said.append(bad or f"{aid}: no count in it")
            continue
        st = a.setdefault("stats", {})
        if st.get(f"by_d{day}") in ("api", "buffer"):
            said.append(f"{aid}: the clock read its day-{day} counts by key; kept")
            continue
        st.update({f"{k}_d{day}": x for k, x in got.items()})
        st.update({f"pulled_d{day}": today, f"by_d{day}": "browser"})
        st.pop(f"miss_d{day}", None)
        done += 1
    return done, said


def collect_counts(s, cfg):
    """Read what is due and write it to each post's `stats` (views_d1, likes_d1, comments_d1, shares_d1, pulled_d1,
    by_d1; the same at d7) -> how many were read. The ledger lock is held to pick the rows and to write them, never
    across a platform's answer. A reading that fails counts a try and says why (miss_d<day>); it never stops the tick."""
    if os.environ.get("STUDIO_FAKE_KEYS") and not os.environ.get("STUDIO_FAKE_COUNTS"):
        return 0                                     # a test's keys are not keys: no test reaches a platform by accident
    with s.Lock():
        led = s.load()
        due = counts_due(s, led, s.now())[:COUNT_PER_TICK]
        rows = {aid: dict(s.row(led, aid)[1]) for aid, _ in due}
    got = {}
    for aid, day in due:
        a = rows[aid]
        try:
            reader = count_buffer if _buffer(a) else COUNT_READERS[a["platform"]]
            c = reader(s, a, cfg)
            got[(aid, day)] = {k: int(c[k]) for k in COUNT_KEYS if isinstance(c.get(k), (int, float)) and not isinstance(c.get(k), bool)}
            if not got[(aid, day)]:
                got[(aid, day)] = "the platform answered with no counts"
            elif a["platform"] == "tiktok":
                got[(aid, day)]["buffer_updated_at"] = c.get("buffer_updated_at")
        except Exception as e:                       # one reader's trouble is that reading's, never the tick's
            got[(aid, day)] = f"{type(e).__name__}: {' '.join(str(e).split())[:140]}"
    if not got:
        return 0
    read = 0
    with s.Lock():
        led = s.load()
        today = s.now().astimezone(s.tz(cfg)).date().isoformat()
        for (aid, day), c in got.items():
            a = s.row(led, aid)[1]
            if not a:
                continue
            st = a.setdefault("stats", {})
            if isinstance(c, dict):
                st.update({f"{k}_d{day}": n for k, n in c.items() if k in COUNT_KEYS})
                if a["platform"] == "tiktok":
                    st[f"buffer_updated_at_d{day}"] = c.get("buffer_updated_at")
                st.update({f"pulled_d{day}": today, f"by_d{day}": "buffer" if _buffer(a) else "api"})
                st.pop(f"miss_d{day}", None)
                read += 1
            else:
                st[f"tries_d{day}"] = (st.get(f"tries_d{day}") or 0) + 1
                st[f"miss_d{day}"] = c
                if st[f"tries_d{day}"] >= COUNT_TRIES:
                    s.log(led, f"{aid}: its day-{day} counts could not be read ({c})")
        s.save(led)
    return read


COUNTS_USAGE = """usage: studio.py counts --due
       studio.py counts <id> [--day 1|7] [--views N] [--likes N] [--comments N] [--shares N]
       studio.py counts -        the day's read in one call, as JSON on stdin:
         [{"id": "<post id>", "day": 1, "views": 1204, "likes": 31, "comments": 4, "shares": 2}, ...]
  What the analyst read off a platform's own analytics page in the browser, filed to each post's stats beside the
  counts the posting clock reads by key, marked as read in the browser. --due lists what to read today: each
  platform the clock cannot read, its analytics page, and every post inside its day-1 or day-7 window.
  Leave out a count the page does not show (never 0 for it) and "day" to take the window the post is in. A count
  is a whole number. An entry is refused, and the rest are still filed, when the post is outside the window, is not
  live, or the clock already read that day by key."""


def counts_entries(rest, stdin):
    """The entries `counts` was given, from its flags or the JSON on stdin; the usage and nothing filed otherwise."""
    if rest[0] == "-":
        try:
            doc = json.loads(stdin or "")
        except ValueError as e:
            sys.exit(f"nothing filed: stdin is not JSON ({e})\n{COUNTS_USAGE}")
        return doc if isinstance(doc, list) else [doc]
    flags = rest[1:]
    if len(flags) % 2 or any(f not in ["--day"] + ["--" + k for k in COUNT_KEYS] for f in flags[::2]):
        sys.exit(COUNTS_USAGE)
    return [dict({f[2:]: x for f, x in zip(flags[::2], flags[1::2])}, id=rest[0])]


def post_counts(a):
    """(the newest reading of a post as {views, likes, comments, shares}, its day) or ({}, None)."""
    st = a.get("stats") or {}
    for day in reversed(COUNT_DAYS):
        c = {k: st[f"{k}_d{day}"] for k in COUNT_KEYS if isinstance(st.get(f"{k}_d{day}"), (int, float))}
        if c:
            return c, day
    return {}, None


def platform_rows(s, cfg, led, t=None):
    """The Growth page's "Other platforms": one row per platform he posts to besides YouTube, for the last 7 days ->
    [{p, out (posts that went out), read (how many of them have a reading), totals {count: sum}, best (title, url,
    its count, what the count is), fig (a post's first day there, for b5_fig), verdict, usual_note, note, browser
    (the analyst reads it in the browser), last (the day of its newest browser reading)}]. A platform is
    scored on views where it gives them, else on likes, replies and shares together. Its usual is the middle
    day-1 score of its earlier posts, once there are five."""
    t = t or s.now()
    since, out = t - dt.timedelta(days=7), []
    for p in cfg["platforms"]:
        if p == "youtube":
            continue
        gives = COUNT_GIVES.get(p) or (COUNT_KEYS if p in COUNT_NEEDS else None)
        score = (lambda c: c.get("views")) if gives and "views" in gives else (lambda c: sum(c.get(k) or 0 for k in ("likes", "comments", "shares")) if c else None)
        week, earlier = [], []
        for v, a in s.assets(led):
            when = a.get("posted_at") or a.get("scheduled_for")
            if a["platform"] != p or a["state"] not in ("posted", "verified") or not when:
                continue
            try:
                (week if s._when(when) >= since else earlier).append((v, a))
            except ValueError:
                continue
        r = dict(p=p, out=len(week), read=0, totals={}, best=None, verdict="", usual_note="", note="", gives=gives or (), fig=None,
                 browser=p in COUNT_NEEDS, last=None)
        if not gives:
            r["note"] = "Not read: the studio has no way to read this platform's counts yet."
            out.append(r)
            continue
        if r["browser"]:
            stats = [a.get("stats") or {} for _, a in week + earlier]
            r["last"] = max((str(st[f"pulled_d{d}"]) for st in stats for d in COUNT_DAYS if st.get(f"by_d{d}") == "browser" and st.get(f"pulled_d{d}")), default=None)
            try:
                r["note"] = f"Read in the browser by the analyst, once a day; last read {dt.date.fromisoformat(r['last']).strftime('%a %b %-d')}."
            except (ValueError, TypeError):
                r["last"], r["note"] = None, f"The analyst reads this from {s.plat_name(p)}'s analytics page on the morning run."
        elif "views" not in gives:
            r["note"] = f"{s.plat_name(p)} gives no view count."
        best = None
        for v, a in week:
            c, _ = post_counts(a)
            if not c:
                continue
            r["read"] += 1
            for k in gives:
                if k in c or not r["browser"]:               # a count the analyst could not see is no zero
                    r["totals"][k] = r["totals"].get(k, 0) + (c.get(k) or 0)
            if score(c) is not None and (best is None or score(c) > best[0]):
                best = (score(c), v, a)
        if best and best[0]:
            r["best"] = dict(title=s.asset_file_label(best[1], best[2]), url=best[2].get("url"), n=best[0],
                             what="views" if "views" in gives else "likes, replies and shares")
        d1 = lambda a: {k: (a.get("stats") or {}).get(f"{k}_d1") for k in COUNT_KEYS if isinstance((a.get("stats") or {}).get(f"{k}_d1"), (int, float))}
        before = sorted(x for x in (score(d1(a)) for _, a in earlier if d1(a)) if x is not None)
        now_ = sorted(x for x in (score(d1(a)) for _, a in week if d1(a)) if x is not None)
        what = "views" if "views" in gives else "likes, replies and shares"
        if now_:                                     # the middle first day of this week's posts, against the middle of the earlier ones
            mine = now_[len(now_) // 2]
            r["fig"] = dict(said=f"{mine:,}", word="", usual="", where="", meter="", what=what)
            if len(before) >= 5:
                usual = before[len(before) // 2]
                r["verdict"] = s.against_usual(mine, usual)
                r["fig"].update(word=r["verdict"], usual=f"{usual:,}", where="the middle of your earlier posts here", meter=s.b5_meter(mine, usual, r["verdict"]))
        if len(before) < 5:
            r["usual_note"] = (f"A usual here needs five earlier posts with a first-day reading; it has {len(before)}." if week or earlier else "No post here yet.")
        out.append(r)
    return out
