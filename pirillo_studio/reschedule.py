"""Native schedule bookkeeping, using the caller's runtime."""


def reschedule_due(s, cfg, a, t):
    """A row on a native scheduler whose drop moved, while the platform will still change it (5 minutes out)."""
    return (a["state"] == "scheduled" and s.drop_times.api_moves(s, cfg, a) and (a.get("reschedule") or {}).get("to")
            and a.get("scheduled_for") and s.parse(a["scheduled_for"]) > t + s.MASTODON_SOON)


def reschedule_done(s, led, cfg, a, to, opts):
    """A reschedule job that went: the row now holds the time its drop moved to. Or (`--state posted`) the post
    went out where it was before anyone changed its time: recorded there, never posted again."""
    r, old = a["reschedule"], a["scheduled_for"]
    if to == "posted":
        s.move(a, "posted", "done")
        a["posted_at"] = old if old and s.parse(old) <= s.now() else s.iso(s.now())
        a.pop("reschedule")
        s.log(led, f"{a['id']}: found live at its old time ({s._at(old, cfg)}); recorded there, the rest of its drop stays")
        return
    new = r.get("claimed_to") or r.get("to")
    if not new:
        s.sys.exit(f"done: {a['id']}: not recorded. Its drop has no new time yet (studio.py plan), so there is none to record.")
    still = s.late_still(a)
    s.move(a, "scheduled", "done")
    a["moved"].append(old)
    a["scheduled_for"] = new
    a.pop("claimed_at", None)
    if r.get("to") and s.parse(r["to"]) != s.parse(new):   # the drop moved again while the publisher had it: one more change
        a["reschedule"] = dict(to=r["to"], asked=s.iso(s.now()), why="its drop moved again while its time was being changed")
    else:
        a.pop("reschedule")
    if r.get("words"):
        a.pop("replace", None)                 # the platform shows the replacement words
    if still and str(opts.get("--image") or "").strip().lower() == "attached":
        a.update(image=s.os.path.relpath(still["image"], s.content_dir(a["slug"])), image_alt=still["image_alt"] or "")
        a.pop("image_note", None)
    a["block"] = None
    s.log(led, f"{a['id']}: its time on {s.plat_name(a['platform'])} changed from {s._at(old, cfg)} to {s._at(new, cfg)}, with its drop")


def reschedule_fail(s, led, cfg, a, why):
    """The platform would not change the scheduled post's time: it stays where it is and goes out there, as it
    is; nothing is deleted and nothing is posted again. Its drop goes on without it (drop_anchor skips `stays`)."""
    r = a.pop("reschedule")
    if a["state"] != "scheduled":
        s.move(a, "scheduled", "fail")
    a.pop("claimed_at", None)
    a["stays"] = dict(why=" ".join(why.split())[:300], wanted=r.get("to"), at=s.iso(s.now()))
    s.log(led, f"{a['id']}: {s.plat_name(a['platform'])} would not change its time ({a['stays']['why'][:120]}); it goes out "
             f"{s._at(a['scheduled_for'], cfg)} as scheduled, and the rest of its drop stays where it is")
