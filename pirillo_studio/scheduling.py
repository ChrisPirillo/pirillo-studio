"""Place ledger rows into drops, using the explicit studio context `s`."""
import datetime as dt
from functools import partial

from . import drop_times, hold


def give_way(s, t, cfg, led):
    """A short or text drop that sits where a long release goes at t gives way as a whole. Its
    planned rows are slotted again, and a row on a platform's scheduler whose time can be changed goes with them:
    it is marked to be rescheduled, and the publisher changes its time (a `reschedule` job). A drop that can't
    move (drop_fixed) is never here: slot_long gave the release the next slot instead."""
    gap = dt.timedelta(hours=cfg["min_gap_hours"])
    reach = drop_times.offset_reach(s, cfg)
    groups = {}
    for v2, a in s.assets(led):
        if a["state"] != "skipped" and drop_times.slot_of(s, a) and not a.get("stays"):
            groups.setdefault((v2["slug"], drop_times.drop_key(s, a)), []).append(a)
    for (slug, key), rows in groups.items():
        if key == "release" or drop_times.drop_fixed(s, rows, cfg):
            continue
        for a in rows:
            w = s.parse(drop_times.slot_of(s, a))
            if max(t - w, w - (t + reach)) >= gap:
                continue
            if a["state"] not in s.LIVE:
                a["moved"].append(a["scheduled_for"])
                a["scheduled_for"] = None
            elif a["state"] == "scheduled":
                a["reschedule"] = dict(to=None, asked=s.iso(s.now()), why=f"a long release took its slot ({s.local(t, cfg)})")

def place(s, a, when):
    """Slot a row of a drop. A row on a platform's scheduler stays where the platform has it and is marked to be
    moved there by the publisher (`reschedule.to`); back at the time it already holds, nothing is asked."""
    if a["state"] in s.LIVE:
        if a["scheduled_for"] and s.parse(a["scheduled_for"]) == when:
            a.pop("reschedule", None)
        else:
            a["reschedule"] = dict(a.get("reschedule") or {}, to=s.iso(when))
            a["reschedule"].setdefault("asked", s.iso(s.now()))
    else:
        put(s, a, when)

def put(s, a, when):
    """Slot a row. A row that was somewhere else remembers where."""
    if a["scheduled_for"] and a["scheduled_for"] != s.iso(when):
        a["moved"].append(a["scheduled_for"])
    a["scheduled_for"] = s.iso(when)
    if (a.get("block") or "").startswith("no drop slot"):
        a["block"] = None

def join(s, rows, T, cfg, led, asap=False):
    """Planned rows of a drop whose time T is already set (YouTube, or the release, holds it): each goes
    at T plus its offset. Returns the rows that can't: their platform has no room left that day (a short's
    cap), or T has gone by. The release (asap) goes as soon as the lead allows instead, the same day as
    YouTube; a short's or text post's late row follows as a later drop."""
    left = []
    for a in rows:
        want = T + drop_times.offset(s, cfg, a)
        if drop_times.slot_of(s, a) and s.parse(drop_times.slot_of(s, a)) == want:
            continue                            # already there (stale() moved it if its moment had gone)
        t = want
        if want < drop_times.soonest(s, cfg, a):
            if not asap:
                if not drop_times.slot_of(s, a):
                    left.append(a)
                continue                        # (a row already slotted as a later drop stays there)
            if drop_times.slot_of(s, a) and s.parse(drop_times.slot_of(s, a)) > want:
                continue                        # the as-soon slot it holds stays: re-slotted every tick, it never came due
            t = drop_times.soonest(s, cfg, a)
        if a["kind"] == "short" and a["state"] not in s.LIVE \
                and drop_times.day_count(s, t.astimezone(s.tz(cfg)).date(), a["platform"], "short", led, cfg) \
                >= cfg["platforms"][a["platform"]]["cap"]:
            if not drop_times.slot_of(s, a):
                left.append(a)
            continue
        place(s, a, t)
    return left

def drop_ok(s, T, rows, cfg, led, spans):
    """May this whole drop go at T? Every row has its lead, its day and its cap, and the drop keeps its
    distance from every other drop. One row that can't makes it a no for all of them, never a split."""
    day = T.astimezone(s.tz(cfg)).date()
    if rows[0]["kind"] == "short" and any(      # a video's shorts spread over days: one short drop a day each
            x["slug"] == rows[0]["slug"] and x["kind"] == "short" and x["idx"] != rows[0]["idx"]
            and x["state"] != "skipped" and drop_times.slot_of(s, x)              # a row being moved counts where it is going
            and s.parse(drop_times.slot_of(s, x)).astimezone(s.tz(cfg)).date() == day for _, x in s.assets(led)):
        return False
    for a in rows:
        when = T + drop_times.offset(s, cfg, a)
        if when < s.now() + s.lead(cfg, a["platform"], a.get("method")):
            return False
        if not days_ok(s, day, a["platform"], cfg):
            return False
        if a["kind"] == "short" and a["state"] not in s.LIVE \
                and drop_times.day_count(s, day, a["platform"], "short", led, cfg) >= cfg["platforms"][a["platform"]]["cap"]:
            return False
    offs = [drop_times.offset(s, cfg, a) for a in rows]
    return drop_times.clear_of(s, T + min(offs), T + max(offs), spans, cfg)

def slot_drop(s, rows, day, cfg, led, key):
    """The first drop slot on or after `day` where every row fits; the rows take it. False if none in 120 days."""
    day = max(day, s.now().astimezone(s.tz(cfg)).date())
    spans = drop_times.drop_spans(s, led, key, cfg=cfg)
    for _ in range(120):
        for hhmm in drop_times.day_slots(s, day, cfg):
            T = s.at(day, hhmm, cfg)
            if drop_ok(s, T, rows, cfg, led, spans):
                for a in rows:
                    place(s, a, T + drop_times.offset(s, cfg, a))
                return True
        day += dt.timedelta(days=1)
    for a in rows:
        a["block"] = "no drop slot in 120 days"
    return False

def days_ok(s, day, platform, cfg):
    return day.weekday() in cfg["platforms"].get(platform, {}).get("days", cfg["post_days"])

def article_day(s, base, rows, cfg):
    """A text post: its own day, article_after_days posting days after the YouTube release, so it isn't the
    same feed's second post about one video that day."""
    day, n = base.astimezone(s.tz(cfg)).date(), 0
    while n < cfg.get("article_after_days", 2):
        day += dt.timedelta(days=1)
        n += all(days_ok(s, day, a["platform"], cfg) for a in rows)
    return day

def cleared(s, v, a, cfg):
    """May this post go? Its video is approved and, with post_ok, the post has the owner's OK."""
    return v["approved"] and (a.get("ok") or not cfg.get("post_ok"))

def stale(s, v, a, cfg):
    """Has this planned row's slot gone by (or come too close to act on)? A post not yet cleared
    keeps sliding to the next free slot; a cleared one only moves once its slot is missed."""
    when, t = s.parse(a["scheduled_for"]), s.now()
    if not cleared(s, v, a, cfg):
        return when < t + s.lead(cfg, a["platform"], a.get("method"))
    if a["method"] == "hand":                           # hand-posted; it stays put until it's found
        return False
    if a["method"] == "api_now":
        return when < t - dt.timedelta(minutes=cfg["grace_minutes"])
    if a["method"] in ("api_native", "buffer"):
        return when < t + dt.timedelta(minutes=6)
    if a["method"] == "chrome_now":                     # posted inside its window, or moved
        after = cfg["platforms"].get(a["platform"], {}).get("window_after", 360)
        return when < t - dt.timedelta(minutes=after)
    return when < t + dt.timedelta(minutes=10)          # chrome: a session can still schedule it

def drop_anchor(s, rows, cfg=None):
    """The time T of a drop that is already on a platform, so a drop never splits: its YouTube row's; else the
    earliest row that really went out; else the time of a row that sits on a platform's own scheduler, first one
    whose time can't be changed there (Mastodon's native schedule), then one whose time can (a Substack Note):
    the planned rows join it. Not an anchor: a row the publisher is moving with its drop (`reschedule`), and a
    row the platform would not let move (`stays`), which goes out where it is while its drop goes on."""
    cfg = cfg or s.cfg_now()
    held = [a for a in rows if a["state"] in s.LIVE and a["state"] != "skipped" and a["scheduled_for"]
            and not a.get("reschedule") and not (a["state"] == "scheduled" and a.get("stays"))]
    yt = [s.parse(a["scheduled_for"]) for a in held if a["platform"] == "youtube"]
    if yt:
        return yt[0]
    out = [s.parse(a["scheduled_for"]) for a in held if a["state"] != "scheduled"]
    if out:
        return min(out)
    on = [a for a in held if not drop_times.can_reschedule(s, cfg, a)] or held
    return min((s.parse(a["scheduled_for"]) - drop_times.offset(s, cfg, a) for a in on), default=None)

COVER_WENT = ("posted", "verified", "check", "skipped")     # it went out (or never will): its cover arm is a record

def reroute(s, cfg, led):
    """Before anything is slotted: every row that is not on a platform yet takes the route its fit gives it
    (route_row: the Chrome route, hand-posted, or back to hand when no run reached it in time), and holds only
    a cover arm it can carry: none on a platform outside the cover test, and never the B-roll arm on a short
    with no insert (can_broll, asked of the video's release.json). The arm is held to that on every row that has
    not gone out, including a row a platform already holds for its minute, so a scheduled row never asks
    for a B-roll cover the file does not carry. A post that went out keeps the arm it went out in:
    that is the test's record."""
    t, rels = s.now(), {}
    for v, a in s.assets(led):
        if a.get("kind") != "short":
            continue
        if a["state"] not in s.LIVE:
            s.route_row(cfg, led, a, t)
        if a["state"] in COVER_WENT:
            continue
        if "cover" in a and a["platform"] not in s.COVER_PLATFORMS:
            if a["state"] in s.LIVE:
                s.log(led, f"{a['id']}: cover arm {a['cover']} dropped ({s.plat_name(a['platform'])} is outside the cover test)")
            del a["cover"]
        if a.get("cover") == "broll":
            if v["slug"] not in rels:
                try:
                    rels[v["slug"]] = s.rj(s.release_path(v["slug"])).get("shorts") or []
                except (OSError, ValueError, AttributeError):
                    rels[v["slug"]] = None
            shorts = rels[v["slug"]]
            src = shorts[a["idx"] - 1] if shorts and 0 < a["idx"] <= len(shorts) else None
            if isinstance(src, dict) and not s.can_broll(src.get("broll"), src.get("cover_at")):
                a["cover"] = "face"
                s.log(led, f"{a['id']}: cover arm broll -> face (the short has no full-screen insert)")

def _plan(s, cfg, led, replan=False):
    """Slot everything unslotted, a drop at a time. Rows already handed to a platform never move. A planned
    row whose time has come too close (or passed) is moved; if the video was approved, the move is marked
    late. A drop with nothing on a platform yet slides as a whole while any of
    its posts still waits for the owner's OK; a drop with a row on a platform (YouTube, or what really went out) keeps
    that time and the planned rows join it, and one row that missed its minute follows as a later drop."""
    if s.effects_enabled(cfg):
        for change, row in s.effect_route_changes(cfg, led):
            row["method"] = change["to"]
            if row.get("digest"):
                row["account"].update(method=change["to"], key_account=change["key_account"])
    s.sync_publishing_routes(cfg, led)
    # After the route sync, so an over-length video's hand route is not overwritten.
    for _, a in s.assets(led):
        s.duration_route(cfg, a)
    if cfg.get("vault_links", True):
        for x in (led.get("storyposts") or {}).values():
            if x.get("state") == "planned" and x.get("vault") == "vault":
                card = (led.get("vault") or {}).get(x.get("day")) or {}
                option = next((o for o in card.get("options", []) if str(x.get("n")) == "v" + str(o.get("k"))), None)
                if option:
                    x["vault_video_url"] = s.vault_video_url(option)
    reroute(s, cfg, led)
    groups_all = {}
    for v, a in s.assets(led):
        groups_all.setdefault((v["slug"], drop_times.drop_key(s, a)), []).append(a)
    prev = {}                                       # where an open drop's rows were, to record a real move only
    movers = [(v, a) for v, a in s.assets(led)
              if a["state"] not in s.LIVE and a["scheduled_for"] and (replan or stale(s, v, a, cfg))]
    slide = {(v["slug"], drop_times.drop_key(s, a)) for v, a in movers
             if not replan and not cleared(s, v, a, cfg) and drop_anchor(s, groups_all[(v["slug"], drop_times.drop_key(s, a))], cfg) is None
             and not any(x.get("reschedule") for x in groups_all[(v["slug"], drop_times.drop_key(s, a))])}
    for v, a in movers:
        if not replan:
            a["late"] = a["late"] or cleared(s, v, a, cfg)
        a["moved"].append(a["scheduled_for"])
        a["scheduled_for"] = None
    for v, a in s.assets(led):                        # an unapproved drop moves together
        if (v["slug"], drop_times.drop_key(s, a)) in slide and a["state"] not in s.LIVE and a["scheduled_for"]:
            a["moved"].append(a["scheduled_for"])
            a["scheduled_for"] = None
    for v in led["videos"].values():               # the long moved: everything that follows it moves too
        yt = next((a for a in v["assets"] if a["kind"] == "long" and a["platform"] == "youtube"), None)
        if yt and not yt["scheduled_for"] and yt["state"] not in s.LIVE:
            for a in v["assets"]:
                if a is not yt and a["scheduled_for"] and a["state"] not in s.LIVE:
                    a["moved"].append(a["scheduled_for"])
                    a["scheduled_for"] = None
    order = sorted((v for v in led["videos"].values() if not v.get("outside_calendar")),
                   key=lambda v: (not v.get("timely"), v.get("release_on") or "9999", v["ingested"]))
    order = [v for v in order if any(a["kind"] == "long" and a["platform"] == "youtube" for a in v["assets"])
             or (not s.has_long(v) and any(a["kind"] == "short" for a in v["assets"]))]
    # 1. every video's release first, so a long always has the day's first slot to itself
    base = {}
    for v in order:
        if not s.has_long(v):
            continue
        base[v["slug"]] = drop_times.slot_long(s, v, cfg, led)
        yt = next(a for a in v["assets"] if a["kind"] == "long" and a["platform"] == "youtube")
        if yt["state"] not in ("posted", "verified") and base[v["slug"]] >= s.now():
            give_way(s, base[v["slug"]], cfg, led)    # a short or text drop sitting there gives way, whole
        join(s, [a for a in v["assets"] if a["kind"] in ("long", "vertical") and a["state"] not in s.LIVE],
             base[v["slug"]], cfg, led, asap=True)
    # 2. shorts and text posts: a drop with a row on a platform joins that time; the rest find a slot
    anchored, fresh, catch = [], [], []
    today = s.now().astimezone(s.tz(cfg)).date()
    for n, v in enumerate(order):
        rd = base[v["slug"]].astimezone(s.tz(cfg)).date() if v["slug"] in base else today
        groups = {}
        for a in v["assets"]:
            if a["kind"] in ("short", "article") and a["state"] != "skipped":
                groups.setdefault(drop_times.drop_key(s, a), []).append(a)
        for key, rows in groups.items():
            # what is still to place: the planned rows, and a row the publisher is moving with them
            todo = [a for a in rows if a["state"] not in s.LIVE or (a.get("reschedule") and not a.get("stays"))]
            for a in list(todo):
                if a["kind"] == "short" and not (cfg["platforms"][a["platform"]].get("cap")):
                    a["block"] = f"{a['platform']} takes no shorts"
                    todo.remove(a)
            # a drop never splits, a native schedule included: a stray Mastodon status joins its drop's time, or,
            # its drop already out, follows it (today: as soon as it may; an older drop: a later drop of its own)
            T0 = drop_anchor(s, rows, cfg)
            for a in drop_times.strays(s, cfg, rows, T0):
                a.pop("offset_was", None)          # it is placed again: it takes the stagger's offset
                if T0 + drop_times.offset(s, cfg, a) >= drop_times.soonest(s, cfg, a):
                    todo.append(a)                 # join() places it; the clock changes its time (reschedule_api)
                    continue
                a["catch_up"] = s.iso(s.now())
                d0 = T0.astimezone(s.tz(cfg)).date()
                if d0 >= today:
                    catch.append((T0, a))
                else:
                    fresh.append((key != "article", True, max(d0, today), n, a.get("idx", 0), v, key, [a]))
            if not todo:
                continue
            if v["slug"] not in base:
                ideal = today
            elif key == "article":
                ideal = article_day(s, base[v["slug"]], todo, cfg)
            else:
                ideal = rd + dt.timedelta(days=todo[0]["idx"])
            T = drop_anchor(s, rows, cfg)             # on a platform already: that time
            if T is None and not any(cleared(s, v, a, cfg) for a in todo) and not any(a["state"] in s.LIVE for a in todo):
                for a in todo:                     # nothing of it is approved yet: it takes its place in line again,
                    if a["scheduled_for"]:         # so two videos' shorts interleave
                        prev[a["id"]] = a["scheduled_for"]
                        a["scheduled_for"] = None
            if T is None:                          # else the time its planned rows already have (the earliest, if one
                T = min((s.parse(drop_times.slot_of(s, a)) - drop_times.offset(s, cfg, a) for a in todo if drop_times.slot_of(s, a)), default=None)
            if T is not None:              # (rows set under the older per-platform windows stay until `plan --replan`)
                anchored.append((v, key, todo, T, ideal, n))
            else:
                fresh.append((key != "article", False, ideal, n, todo[0]["idx"], v, key, todo))
    for v, key, todo, T, ideal, n in anchored:
        left = join(s, todo, T, cfg, led)
        if left:                                   # its platform is full that day: a later drop for those
            fresh.append((key != "article", True, max(ideal, T.astimezone(s.tz(cfg)).date()), n, left[0]["idx"], v, key, left))
    for _, a in sorted(catch, key=lambda x: x[0]):   # one at a time, CATCH_UP_GAP apart
        place(s, a, drop_times.catch_up_at(s, cfg, led, a))
    # text posts first (as before, each in its own free slot), then shorts from every video in one pass by ideal
    # day, so two videos' shorts interleave instead of one video's five landing ahead of the other's. A late row
    # of a drop that already went out (alone, a drop of one) waits behind every whole drop.
    for is_short, late, ideal, n, idx, v, key, rows in sorted(fresh, key=lambda x: x[:5]):
        slot_drop(s, rows, ideal, cfg, led, (v["slug"], key))
    for _, a in s.assets(led):
        if a["id"] in prev and a["scheduled_for"] != prev[a["id"]]:
            a["moved"].append(prev[a["id"]])
    far = s.now() + dt.timedelta(days=cfg["backlog_warn_days"])
    late = [a["id"] for _, a in s.assets(led) if a["scheduled_for"] and s.parse(a["scheduled_for"]) > far]
    said = (f"backlog: {len(late)} posts sit more than {cfg['backlog_warn_days']} days out; "
            "fewer shorts per video, or a higher cap in _studio.json") if late else None
    if said != led.get("backlog_said"):     # said once per change, never on every pass
        led["backlog_said"] = said
        if said:
            s.log(led, said)


def plan(s, cfg, led, replan=False):
    """Keep the studio hold policy around the scheduling engine."""
    return hold.preserve_plan(partial(_plan, s))(cfg, led, replan=replan)
