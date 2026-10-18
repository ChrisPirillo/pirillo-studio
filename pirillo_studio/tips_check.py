"""The tips' 30-minute X check, using the caller's runtime and ledger."""
import datetime as dt
import os


def tips_x_check(s, cfg, t=None, reader=None):
    """The 30-minute check on X (from published X engagement data): a tip under its expected engagement
    there gets one repost with its alternate hook, queued like any approved post (post_hours apart, never on top
    of a drop) and logged on the piece — one repost max, and a repost is never itself reposted. tips.x_engage_min
    is the threshold (likes+reposts; the account's median for text posts — unset, each tip says once that no
    check ran). Reads X outside the lock, like collect_counts. -> lines to print."""
    if not s.tips_cfg(cfg).get("enabled"):
        return []                                    # the slot off stops its X checks too: nothing reads, nothing reposts
    if os.environ.get("STUDIO_FAKE_KEYS") and not os.environ.get("STUDIO_FAKE_COUNTS"):
        return []                                    # a test's keys are not keys: no test reaches X by accident
    t = t or s.now()
    q = s.tips_cfg(cfg)
    mins = dt.timedelta(minutes=int(q.get("x_check_minutes") or 0))
    if mins <= dt.timedelta(0):
        return []
    read = reader or s.x_engagement
    with s.Lock():
        led = s.load()
        rows = (led.get("storyposts") or {})
        due = {rid: dict(x) for rid, x in rows.items()
               if x.get("tip") and not x.get("repost_of") and x["platform"] == "x" and x.get("state") == "posted"
               and x.get("post_id") and x.get("posted_at") and not x.get("x_check")
               and s.parse(x["posted_at"]) + mins <= t
               and not any(y.get("repost_of") == rid for y in rows.values())}
    if not due:
        return []
    got = {}
    for rid, x in due.items():
        try:
            got[rid] = read(x, cfg)
        except Exception as e:                        # one reading's trouble is that tip's, never the tick's
            got[rid] = f"{type(e).__name__}: {' '.join(str(e).split())[:140]}"
    out = []
    with s.Lock():
        led = s.load()
        rows = led.setdefault("storyposts", {})
        floor = q.get("x_engage_min")
        for rid, n in got.items():
            x = rows.get(rid)
            if not x or x.get("x_check") or any(y.get("repost_of") == rid for y in rows.values()):
                continue                              # it moved while we read: what this tick wrote stands
            if not isinstance(n, int):
                x.update(x_check=s.iso(t), x_miss=n)
                out.append(f"tip {x.get('tip')}: its X reading failed ({n}); not rechecked")
                s.log(led, f"story:{rid}: X reading failed ({n}); no repost check")
                continue
            x.update(x_check=s.iso(t), x_likes=n)
            if floor is None:
                out.append(f"tip {x['tip']}: no X check ran (tips.x_engage_min is unset; it wants the account's "
                           "median likes+reposts for text posts)")
                continue
            if n >= int(floor):
                continue
            alt = x.get("alt_hook")
            if not alt:
                out.append(f"tip {x['tip']}: under its expected X engagement ({n} < {floor}) with no \"Alt hook:\" "
                           "line in data/tips.md; not reposted")
                s.log(led, f"story:{rid}: {n} under {floor}; no alternate hook, not reposted")
                continue
            day = t.astimezone(s.tz(cfg)).date().isoformat()
            rk = f"{day}-t{x['tip']}r:x"
            start, when = s.queue_place(cfg, led, "story:" + rk, [("x", x["method"])], t, days=None, every_day=True)
            if when.get("x") is None:
                out.append(f"tip {x['tip']}: under its expected X engagement ({n} < {floor}) and the queue left the "
                           "repost out; not retried this way")
                s.log(led, f"story:{rid}: {n} under {floor}; repost left out by the queue")
                continue
            rows[rk] = dict(
                day=day, n=f"t{x['tip']}r", tip=x["tip"], platform="x", method=x["method"],
                headline=alt, text=x["text"], alt_hook=x.get("alt_hook"), repost_of=rid,
                why="the 30-minute X check (data/tips.md)", require_image=bool(x.get("require_image")),
                image=x.get("image"), image_alt=x.get("image_alt"), image_sha256=x.get("image_sha256"),
                state="planned", ok=True, ok_at=s.iso(t), start=s.iso(start), when=s.iso(when["x"]),
                added=s.iso(t), url=None, attempts=0)
            s.tip_sync_row(rows[rk], led)
            x["x_repost"] = rk
            out.append(f"tip {x['tip']}: under its expected X engagement ({n} < {floor}); repost \"{alt}\" queued")
            s.log(led, f"story:{rid}: {n} under {floor}; repost story:{rk} queued (one repost max)")
        s.save(led)
    return out
