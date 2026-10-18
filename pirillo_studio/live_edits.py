"""Edits to posts already on a platform's scheduler (a new time, the words after publishing), using the caller's runtime and ledger."""
import datetime as dt

from pirillo_studio import hold, mastodon_edits, reschedule


def reschedule_api(s, aid, cfg):
    """One native-scheduled row follows its drop: the clock changes its time by API (the ledger lock released
    across the call, like send()). A time under 5 minutes out goes 6 out (the platform's floor). The time the
    platform answers with is what the row records; a refusal keeps the post where it is (`stays`), never deleted,
    never posted again."""
    token, cfg = s.key("mastodon"), hold.current(cfg, s.CONFIG, s.DEFAULTS)   # a hold set mid-tick stops it (#1170)
    with s.Lock():
        led = s.load()
        v, a = s.row(led, aid)
        if not a or hold.reason(cfg, a, v) or not reschedule.reschedule_due(s, cfg, a, s.now()):
            return
        to = max(s.parse(a["reschedule"]["to"]), s.now() + s.MASTODON_SOON + dt.timedelta(minutes=1))
        job = dict(a)
    if not token:
        return
    if not job.get("post_id"):
        got, err = None, s.AdapterError("no scheduled status id on the row", False)
    else:
        try:
            got, err = mastodon_edits.mastodon_retime(s, job, cfg, token, to), None
        except s.AdapterError as e:
            got, err = None, e
    token = None
    with s.Lock():
        led = s.load()
        v, a = s.row(led, aid)
        if not a or a["state"] != "scheduled" or not a.get("reschedule"):
            return
        if err is not None:
            a["last_error"] = str(err)[:300]
            if err.retry:
                s.log(led, f"{aid}: changing its time on {s.plat_name(a['platform'])} failed ({a['last_error'][:120]}); "
                         "the next tick tries again")
            else:
                reschedule.reschedule_fail(s, led, cfg, a, str(err))
        else:
            a["reschedule"]["claimed_to"] = s.iso(got.astimezone(s.tz(cfg)))
            reschedule.reschedule_done(s, led, cfg, a, None, {})
            a.pop("last_error", None)
        s.save(led)


def replace_after_publish(s, aid, cfg):
    """One row's swap, the ledger lock released across the network call (like send())."""
    token, cfg = s.key("mastodon"), hold.current(cfg, s.CONFIG, s.DEFAULTS)   # a hold set mid-tick stops it (#1170)
    with s.Lock():
        led = s.load()
        v, a = s.row(led, aid)
        if not a or hold.reason(cfg, a, v) or not mastodon_edits.mastodon_replace_due(s, a, s.now()):
            return
        text, _ = s.render(v, a, cfg)             # {long_url} filled in, and the link as its last line
        job = dict(a, text=text if text is not None else a.get("text"))
    if not token:
        return
    try:
        r = mastodon_edits.mastodon_replace(s, job, cfg, token)
        err = None
    except s.AdapterError as e:
        r, err = None, e
    finally:
        token = None
    with s.Lock():
        led = s.load()
        v, a = s.row(led, aid)
        if not a or a["state"] != "scheduled" or not a.get("replace"):
            return
        if err is not None:
            a["last_error"] = str(err)[:300]
            if not err.retry:
                s.move(a, "check", "swap")
            s.log(led, f"{aid}: the edit after publishing failed ({a['last_error'][:120]}) -> {a['state']}")
        elif r["state"] == "check":
            s.move(a, "check", "swap")
            a["last_error"] = r["reason"]
            s.log(led, f"{aid}: {r['reason']}")
        else:
            s.move(a, "posted", "swap")
            a.update(post_id=r["post_id"], url=r["url"], posted_at=r["posted_at"])
            a.pop("replace", None)
            a.pop("last_error", None)
            s.log(led, f"{aid}: published, then edited to his words (status {r['post_id']})")
        s.save(led)
