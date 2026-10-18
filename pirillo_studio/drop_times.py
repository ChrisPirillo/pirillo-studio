"""Drop timing and spacing, using the explicit studio context `s`.

Everything goes out in drops: each piece reaches its platforms together, YouTube
first. Spacing applies between drops, not between a drop's sibling posts. The
analyst picks drop times from the owner's numbers once there are enough.
A drop is a long release and its full vertical, one short on all its platforms,
or one text post on all its platforms. Each row shares the drop's anchor time;
platform offsets follow YouTube, never precede it. A row already on a platform
keeps its time; if YouTube holds a time, that is the drop's anchor.
"""
import datetime as dt
import sys

from . import scheduling


def dcfg(s, cfg):
    return cfg.get("drops") or s.DEFAULTS["drops"]

def day_slots(s, day, cfg):
    """The drop times of a day, earliest first: that weekday's own list when the analyst found one."""
    d = dcfg(s, cfg)
    return sorted((d.get("by_weekday") or {}).get(str(day.weekday())) or d["slots"])

def long_time(s, day, cfg):
    """The long release takes the day's first slot (drops.long when that is one of the day's slots)."""
    slots = day_slots(s, day, cfg)
    return dcfg(s, cfg).get("long") if dcfg(s, cfg).get("long") in slots else slots[0]

def offset_old(s, cfg, a):
    """The offset before the stagger: YouTube and hand-posted TikTok at T, a script's post at T + 10 min, a post a
    browser schedules or makes at T + 15 min (in minutes)."""
    o = {**s.DEFAULTS["drops"]["offsets"], **(dcfg(s, cfg).get("offsets") or {})}
    k = ("youtube" if a["platform"] == "youtube" else "hand" if a["method"] == "hand" or s.on_fit_route(cfg, a)
         else "chrome" if a["method"].startswith("chrome") else "api")       # (TikTok at T on either route)
    return o[k]

def offset_reach(s, cfg):
    """The latest a drop's row can go after its T, under either rule."""
    old = max({**s.DEFAULTS["drops"]["offsets"], **(dcfg(s, cfg).get("offsets") or {})}.values())
    step = (dcfg(s, cfg).get("stagger") or s.DEFAULTS["drops"]["stagger"])["minutes"]
    return dt.timedelta(minutes=max(old, step * len(cfg.get("platform_order") or s.DEFAULTS["platform_order"])))

def stagger(s, cfg, led):
    """The stagger on the rows already in the ledger (once, the day it landed; again changes nothing): a row on a
    platform, on its way or out keeps its minute and is stamped with the offset it was placed by (offset_was); a
    planned row moves from its drop's T + its old offset to T + its new one, unless that is inside its lead.
    Returns the lines to print: one per row that moved. Once: the ledger's "staggered" says when it ran."""
    if led.get("staggered"):
        return [f"stagger: nothing to move (it ran {led['staggered']}; every row since is placed by the stagger)"]
    out = []
    for v, a in s.assets(led):
        if not a.get("scheduled_for") or a.get("offset_was") is not None or a["state"] == "skipped":
            continue
        old = dt.timedelta(minutes=offset_old(s, cfg, a))
        if a["state"] != "planned":
            a["offset_was"] = int(old.total_seconds() // 60)
            continue
        was = s.parse(a["scheduled_for"])
        to = was - old + offset(s, cfg, a)
        if to == was or to < soonest(s, cfg, a):
            continue
        scheduling.put(s, a, to)
        out.append(f"{a['id']}: {s.local(was, cfg)} -> {s.local(to, cfg)}")
    led["staggered"] = s.iso(s.now())
    s.log(led, f"stagger: {len(out)} planned rows re-timed, no two platforms of a drop at one minute")
    return out or ["stagger: nothing to move"]

def platform_rank(s, cfg, platform):
    order = cfg.get("platform_order") or s.DEFAULTS["platform_order"]
    return order.index(platform) if platform in order else len(order)

def offset(s, cfg, a):
    """How long after its drop's time T a row goes. YouTube at T; every other platform (TikTok too) at T +
    stagger minutes x its place in platform_order, so no two platforms of one drop share a minute.
    A row already on a platform when the stagger began keeps the offset it was placed by
    (`offset_was`, stamped by `studio.py stagger`), so its drop's T reads the same and it is never moved."""
    if a.get("offset_was") is not None:
        return dt.timedelta(minutes=a["offset_was"])
    if a["platform"] == "youtube":
        return dt.timedelta(0)
    step = (dcfg(s, cfg).get("stagger") or s.DEFAULTS["drops"]["stagger"])["minutes"]
    m = step * platform_rank(s, cfg, a["platform"])
    if a["platform"] == "tiktok":                  # its scheduler takes FIT_STEP_MIN steps (picker_time): on a step
        m = -(-m // s.FIT_STEP_MIN) * s.FIT_STEP_MIN
    return dt.timedelta(minutes=m)

def drop_key(s, a):
    """Which drop of its video a row belongs to: release, article, or short<N>."""
    return ("release" if a["kind"] in ("long", "vertical") else
            "article" if a["kind"] == "article" else f"short{a.get('idx', 0)}")

def drop_spans(s, led, skip=None, hard=False, cfg=None):
    """(first, last) row time of every drop on the calendar but `skip` (slug, key): rows within an hour of one
    another are one drop. A row that follows its drop on another day is a drop of its own (spacing applies).
    A row the publisher is moving counts where it is going. hard: only the drops a new long release can't
    displace (releases, and drops with a row that is out or on a scheduler that can't be changed: drop_fixed)."""
    cfg = cfg or s.cfg_now()
    g = {}
    for v, a in s.assets(led):
        if a["state"] != "skipped" and slot_of(s, a) and not a.get("stays"):
            g.setdefault((v["slug"], drop_key(s, a)), []).append(a)
    out = []
    for k, rows in g.items():
        if k == skip or (hard and k[1] != "release" and not drop_fixed(s, rows, cfg)):
            continue
        clusters = []                        # rows within an hour of each other are one drop; a late row of a drop
        for a in sorted(rows, key=lambda a: s.parse(slot_of(s, a))):                                  # that went out is another
            t = s.parse(slot_of(s, a))
            if clusters and t - clusters[-1][-1][0] <= dt.timedelta(hours=1):
                clusters[-1].append((t, a))
            else:
                clusters.append([(t, a)])
        # a lone native schedule (Mastodon, days from the drop it left) is a straggler: no spacing. Alone on the
        # calendar it IS its drop (the rows that join it are not slotted yet), so it counts; one sent on as a later
        # drop of its own (catch_up) holds its slot.
        real = [c for c in clusters if any(a["platform"] == "youtube" or a["state"] != "scheduled" or a.get("reschedule")
                                           or a.get("catch_up") for _, a in c)]
        for c in real or clusters:
            out.append((c[0][0], c[-1][0]))
    return out

def clear_of(s, c0, c1, spans, cfg):
    """A drop taking c0..c1 keeps min_gap_hours from every other drop, and its day has a slot left."""
    gap = dt.timedelta(hours=cfg["min_gap_hours"])
    if any(max(c0 - e, span_start - c1) < gap for span_start, e in spans):
        return False
    day = c0.astimezone(s.tz(cfg)).date()
    return sum(1 for span_start, _ in spans if span_start.astimezone(s.tz(cfg)).date() == day) < len(day_slots(s, day, cfg))

def day_count(s, day, platform, kind, led, cfg):
    n = 0
    for _, a in s.assets(led):
        t = s.parse(a["scheduled_for"])
        if a["platform"] == platform and a["kind"] == kind and t and t.astimezone(s.tz(cfg)).date() == day:
            n += 1
    return n

def long_day_free(s, day, led, cfg, skip_slug):
    for v, a in s.assets(led):
        t = s.parse(a["scheduled_for"])
        if (a["kind"] == "long" and a["platform"] == "youtube" and v["slug"] != skip_slug
                and not v.get("outside_calendar") and a["state"] != "skipped"
                and t and t.astimezone(s.tz(cfg)).date() == day):
            return False
    return True

def soonest(s, cfg, a):
    """The earliest time a row can be slotted now: its lead from now, to the next 5 minutes."""
    t = s.now() + s.lead(cfg, a["platform"], a.get("method"))
    if t.second or t.microsecond:
        t = t.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    return t + dt.timedelta(minutes=(5 - t.minute % 5) % 5)

def slot_long(s, v, cfg, led):
    """The time the long video goes up: the drop time of its release. release_on wins; timely goes at the
    next slot; else the first weekday (long_days) with no other long release, at the day's first slot."""
    yt = next(a for a in v["assets"] if a["kind"] == "long" and a["platform"] == "youtube")
    if yt["scheduled_for"]:
        return s.parse(yt["scheduled_for"])
    earliest = s.now() + s.lead(cfg, "youtube")
    reach = offset_reach(s, cfg)
    if v.get("release_on"):                # a pinned time (release_at) wins over the slots
        day = dt.date.fromisoformat(v["release_on"])
        t = s.at(day, v.get("release_at") or long_time(s, day, cfg), cfg)
        if t < earliest:                   # the pinned day went by: say so, take the next free weekday
            s.log(led, f"{v['slug']}: the pinned day {v['release_on']} went by; moved to the next free weekday")
            v["missed_pin"], v["release_on"], v["release_at"] = v["release_on"], None, None
    if v.get("timely"):
        t = earliest.astimezone(dt.timezone.utc).replace(second=0, microsecond=0)
        t += dt.timedelta(minutes=(15 - t.minute % 15) % 15)
        while not scheduling.days_ok(s, t.astimezone(s.tz(cfg)).date(), "youtube", cfg):     # news waits for a post day
            day = t.astimezone(s.tz(cfg)).date() + dt.timedelta(days=1)
            t = s.at(day, long_time(s, day, cfg), cfg)
    elif v.get("release_on"):
        pass
    else:
        if not cfg["long_days"]:
            sys.exit("_studio.json: long_days is empty; list the weekdays (0 = Monday) long videos go out")
        spans = drop_spans(s, led, (v["slug"], "release"), hard=True, cfg=cfg)
        day = earliest.astimezone(s.tz(cfg)).date()
        while True:
            first = s.at(day, long_time(s, day, cfg), cfg)
            if day.weekday() in cfg["long_days"] and long_day_free(s, day, led, cfg, v["slug"]):
                if first >= earliest:
                    # the day's first slot; if another drop holds it, the day's next free slot
                    later = [s.at(day, h, cfg) for h in day_slots(s, day, cfg) if s.at(day, h, cfg) != first]
                    t = next((x for x in [first] + later if x >= earliest and clear_of(s, x, x + reach, spans, cfg)), None)
                    if t:
                        break
                # an approved video whose slot went by on a free day goes today at the first time the lead allows,
                # up to long_same_day_until, never a whole day later.
                else:
                    soon = earliest.astimezone(dt.timezone.utc).replace(second=0, microsecond=0)
                    soon += dt.timedelta(minutes=(15 - soon.minute % 15) % 15)
                    if (v.get("approved") and soon.astimezone(s.tz(cfg)).date() == day      # approved: ship it
                            and soon <= s.at(day, cfg.get("long_same_day_until", "17:00"), cfg)):
                        t = soon
                        break
            day += dt.timedelta(days=1)
    yt["scheduled_for"] = s.iso(t)
    return t

def can_reschedule(s, cfg, a):
    """May this row's time be changed where it sits on its platform's own scheduler? The platform's `reschedule`
    flag (studio.json; DEFAULTS says why for each), for a post a browser put there (the publisher changes it) or
    one an API handed to the platform's own scheduler (api_native: the clock changes it, reschedule_api)."""
    return bool(a.get("method") in ("chrome", "api_native") and cfg["platforms"].get(a["platform"], {}).get("reschedule"))

def api_moves(s, cfg, a):
    """A row on a platform's native scheduler whose time the clock changes by API (Mastodon's scheduled_statuses)."""
    return a.get("method") == "api_native" and can_reschedule(s, cfg, a)

CATCH_UP_GAP = dt.timedelta(minutes=10)   # a catch-up keeps this far from any other post of its platform

def strays(s, cfg, rows, T):
    """The rows of a drop sitting on a native scheduler apart from it: Mastodon's status handed to its scheduler
    under an older slotting, left behind when the rest of its drop moved. T is the drop's time (drop_anchor). A row
    already sent on as a later drop of its own (`catch_up`), moving, or that its platform would not move (`stays`)
    is not one."""
    if T is None:
        return []
    return [a for a in rows if a["state"] == "scheduled" and api_moves(s, cfg, a) and a.get("scheduled_for")
            and not a.get("reschedule") and not a.get("stays") and not a.get("catch_up")
            and s.parse(a["scheduled_for"]) != T + offset(s, cfg, a)]

def catch_up_at(s, cfg, led, a):
    """The soonest time a stray whose drop went out today can follow it: its lead from now, CATCH_UP_GAP from any
    other post of its platform, so a catch-up never lands on top of another."""
    t = soonest(s, cfg, a)
    others = [s.parse(slot_of(s, x)) for _, x in s.assets(led) if x is not a and x["platform"] == a["platform"]
              and x["state"] != "skipped" and slot_of(s, x)]
    while any(abs(t - o) < CATCH_UP_GAP for o in others):
        t += dt.timedelta(minutes=5)
    return t

def slot_of(s, a):
    """Where the calendar has a row. A row the publisher is moving counts where it is going (None until that is
    known); `scheduled_for` stays what the platform holds until the publisher's `done`."""
    r = a.get("reschedule")
    return r.get("to") if r else a["scheduled_for"]

def drop_fixed(s, rows, cfg):
    """A drop nothing may displace: a row of it is out or in a sender's hands, or sits on a platform's scheduler
    whose time can't be changed (can_reschedule), or is too close to its minute for the publisher to change it."""
    for a in rows:
        if a["state"] not in s.LIVE or a["state"] == "skipped" or not a["scheduled_for"] or a.get("stays"):
            continue
        if a.get("reschedule"):
            continue                           # already moving with its drop
        if a["state"] != "scheduled" or not can_reschedule(s, cfg, a):
            return True
        if s.parse(a["scheduled_for"]) < s.now() + s.lead(cfg, a["platform"], a.get("method")):
            return True
    return False
