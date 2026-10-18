"""story_due, STORY_NOTE, STORY_LOOK, story_jobs: moved from the host by dev/port.py; `s` is the host (pirillo_studio.host.View)."""
import datetime as dt
from pirillo_studio import hold
from pirillo_studio.sends import post_len


def story_due(s, led, cfg, t):
    """The approved rows the clock sends now (an API platform at or after its slot)."""
    return [rid for rid, x in (led.get("storyposts") or {}).items()
            if not hold.reason(cfg, x) and s.digest_allowed(cfg, x) and not s.facebook_profile_story(x) and (x.get("posting_hold") or not cfg.get("vault_keep") or x.get("vault") not in ("vault", "history", "day")
                or x.get("day") == t.astimezone(s.tz(cfg)).date().isoformat())   # its own day, weekends too (#636)
            and x.get("state") == "planned" and x.get("ok") and x.get("when") and (x["method"].startswith("api") or x["method"] == "buffer")
            and (x.get("posting_hold") or not x.get("weekend") or s.wild_image_post(x)
                 and s.parse(x["when"]).astimezone(s.tz(cfg)).date() == t.astimezone(s.tz(cfg)).date())
            and (s.parse(x["when"]) > t + dt.timedelta(minutes=6) if x["method"] == "buffer" else s.parse(x["when"]) <= t)
            and (x.get("posting_hold") or not x.get("before") or t <= s.parse(x["before"]))
            and not (x.get("next_try") and s.parse(x["next_try"]) > t)]


STORY_NOTE = ("One of the day's five posts (`kind` storypost; studio.md, \"The five posts\"): a text post with no video, the "
              "same as a video's text post (chrome-jobs.md, \"schedule_text and post_text_now\"): paste `text` exactly, attach "
              "`image` with `image_alt`, nothing added. `late` false: schedule it at `when`; `late` true (its slot passed before a "
              "run reached it): post it now. Then `done <id> --url <the post> --state scheduled|posted`. ")


STORY_LOOK = ("LOOK FIRST: an earlier run was handed this post and never reported. Open the platform: if it is there, `done "
              "<id> --url <the post> --state posted` and post nothing. Only if it is not there, go on. ")


def story_jobs(s, cfg, led, claim=True, only=None):
    """The publisher's jobs for approved story rows on a browser platform: YouTube and Substack scheduled at the
    row's slot (`schedule_text`). Facebook profile rows are the owner's manual posts. Vault rows
    with a deadline also get an evening job when regular runs are over. `only` selects one story row;
    a publisher sent for one video gets none."""
    if only and not only.startswith("story:"):
        return []
    t, jobs = s.now(), []
    s.story_sweep(led, cfg, t)
    for rid, x in sorted((rid, x) for rid, x in (led.get("storyposts") or {}).items() if not only or only == "story:" + rid):
        if hold.reason(cfg, x) or not s.digest_allowed(cfg, x):
            continue
        correction = s.scheduled_image_job(x, "story:" + rid, cfg, led)
        if correction and not s.facebook_profile_story(x) and x.get("platform") not in s.NO_BROWSER and not x.get("method", "").startswith("api"):
            jobs.append(correction)
            continue
        if not s.story_image_ready(x, cfg, led):
            continue
        if s.facebook_profile_story(x) or x.get("platform") in s.NO_BROWSER:
            continue                       # Image holds still apply to manual rows; neither route gets a browser job.
        look = s.story_unreported(x, cfg, t)
        if not ((x.get("state") == "planned" and x.get("ok") and x.get("when")) or look) or x["method"] == "hand" or x["method"].startswith("api"):
            continue
        before = None if x.get("posting_hold") else x.get("before")   # a held post keeps its approval past its deadline, as story_due (#1170)
        if before and t > s.parse(before):
            continue
        when = s.parse(x["when"])
        if (x["method"] == "chrome_now" or before) and t < when - dt.timedelta(minutes=30):
            continue                                  # leave distant browser slots for their run
        if x.get("digest") and x["method"] == "chrome_now" and t < when:
            continue
        goes = when if when > t else t
        job = dict(s.story_job(x, rid), kind="storypost", do="post_text_now" if x["method"] == "chrome_now" else "schedule_text",
                   when=s.local(goes, cfg), when_iso=s.iso(goes), late=when <= t, text=s.story_text(x, cfg),
                   require_image=bool(x.get("image") or x.get("digest")),
                   note=(STORY_LOOK if look else "") + STORY_NOTE + (s.YT_POST_NOTE if x["platform"] == "youtube" else ""))
        if before:
            job.update(before=before, before_iso=before, when_iso=goes.isoformat())
            if when <= t or x["method"] == "chrome_now" or x.get("vault_evening"):
                job["do"] = "post_text_now"
            job["note"] = (STORY_LOOK if look else "") + (
                "Today's vault post: do date-bound vault jobs in time order before ordinary uploads. "
                "Publish on its own day, at when or as soon as possible if late. "
                f"Finish before {s.local(s.parse(before), cfg)}. Never publish after that deadline; "
                "report a missed deadline instead. Paste text exactly, attach image with image_alt, "
                "nothing added. Then done <id> --url <the post> --state scheduled|posted. ")
        if x.get("digest"):
            job.update(account=x["account"], note=(STORY_LOOK if look else "") + s.digest_chrome_note(x))
        if x.get("image"):
            job["image"] = s.frozen_image(x["image"], cfg, led, x.get("image_approval"))
        jobs.append(job)
        if claim:
            s.record_scheduled_images(x, job.get("image"), fresh=not look)
            x.update(state="claimed", claimed_at=s.iso(t))
    return jobs


def story_holds(s, cfg, x):
    """Why an OK on this story row would send nothing he should see go, one plain line each ([] when it can go):
    a link in its words, no route to its platform, its still gone."""
    if x.get("tip"):
        s.tip_sync_row(x)
    out = s.headline_gate_problems(x.get("day"), x.get("text"), x.get("headline"), cfg) \
        if not x.get("vault") and not x.get("digest") and not x.get("tip") else []   # a tip's words are Claude's, not the five's dated title
    if cfg.get("vault_keep") and x.get("vault") in ("vault", "history", "day"):
        when = s.parse(x["when"]).astimezone(s.tz(cfg)) if x.get("when") else s.now().astimezone(s.tz(cfg))
        if when.date().isoformat() != x.get("day"):
            out.append("a vault, history or day post must stay on its own day")
    if x.get("vault") == "wild_image_prompt" or x.get("kind") == "wild_image_prompt":
        prompt = x.get("generation_prompt")
        if not prompt or prompt not in (x.get("text") or "") or x.get("includes_chris") is not True or not x.get("image"):
            out.append("wild image post needs the owner's image and the exact generation prompt")
    if x.get("weekend") and not s.wild_image_post(x):
        out.append("only a wild image + exact prompt post can use the weekend queue")
    vault = x.get("vault") == "vault"
    bad_link = s.vault_link_problem(x.get("text"), x.get("vault_video_url")) if vault else s.link_problem(x.get("text"))
    if vault and cfg.get("vault_links", True):
        if not x.get("vault_video_url"):
            out.append("its old video's video.url is missing; refresh the vault option")
        elif post_len(x["platform"], s.story_text(x, cfg)) > s.vault_limit(x["platform"]):
            out.append("its words are too long with the required old-video link")
    if bad_link and not x.get("digest"):
        out.append("it carries links, and these posts go out without them: it comes back to you rewritten with each source named")
    m = x.get("method") or ""
    if m.startswith("api") and x["platform"] not in s.ADAPTERS:
        out.append(f"nothing can post it to {s.plat_name(x['platform'])} yet")
    elif not (m in ("hand", "buffer") or m.startswith("api") or m.startswith("chrome")):
        out.append(f"nothing can post it to {s.plat_name(x['platform'])} yet")
    if not x.get("image") and (x.get("vault") or str(x.get("n") or "").startswith("v")):
        o = s.vault_row_option(x)
        if o:
            still = o.get("image") or s.vault_still(x.get("day"), o.get("kind") or x.get("vault"), o)
            if still:
                x["image"] = still
                if not x.get("image_alt"):
                    x["image_alt"] = o.get("image_alt") or s.vault_alt(o) or s.still_alt(still) or ""
    if (x.get("image") or s.story_has_promised_still(x)) and s.story_image_hash(x.get("image")) is None:
        out.append(s.STORY_IMAGE_MISSING)
    return out


def story_open_until(s, x, cfg):
    if x.get("retained") or x.get("tip"):     # a retained post and a tip are evergreen: no Board hours run out
        return dt.datetime.max.replace(tzinfo=s.tz(cfg))
    return s.parse(x["added"]) + dt.timedelta(hours=s.story_cfg(cfg)["open_hours"])


def story_unreported(s, x, cfg, t):
    return (x.get("state") == "claimed" and x.get("claimed_at")
            and s.parse(x["claimed_at"]) < t - dt.timedelta(hours=cfg["claim_hours"]))


def story_weekend_overflow(s, led, cfg, rid, x, t):
    """Retain an unapproved overflow post as one weekend decision across its platforms."""
    key = rid.split(":")[0]
    rows = s.story_post_rows(led, key)
    item = dict(source_id="overflow:" + key, headline=x["headline"],
                text={p: row["text"] for p, row in rows.items() if row.get("state") == "planned" and not row.get("ok")},
                image=x.get("image"), image_alt=x.get("image_alt"), kind=x.get("vault") or x.get("kind"),
                generation_prompt=x.get("generation_prompt"), includes_chris=x.get("includes_chris"),
                review=x.get("review"))
    try:
        queued = s.weekend_enqueue(led, cfg, item, t, image_day=x.get("image_day") or x["day"])
    except s.Refused:
        # Unready copy remains on record and on the Board; it cannot become a posting job.
        x.update(retained=True)
        return
    x.update(state="skipped", ok=False, skip_reason="Kept in weekend queue", weekend_item=queued)


def facebook_profile_story(s, x):
    """Story rows without an explicit Page account belong to the owner's personal profile."""
    return x.get("platform") == "facebook" and (x.get("account") or {}).get("kind") != "page"


def story_sweep(s, led, cfg, t=None):
    """A scheduled post whose minute has come needs a receipt; a send that never finished is a check; one he never approved
    is dropped when its hours on his Board are up. A date-bound vault row expires at its deadline; ordinary approved
    posts are never dropped. Returns the ids it changed."""
    t, changed = t or s.now(), []
    for rid, x in (led.get("storyposts") or {}).items():
        if hold.story_wait(cfg, x, s): continue
        if (x.get("method") == "buffer" and x.get("state") == "planned" and x.get("ok") and x.get("when")
                and s.parse(x["when"]) <= t + dt.timedelta(minutes=6)):
            why = x["method"].capitalize() + " scheduling window missed"
            x.update(state="handoff", block=why + "; yours to choose a new slot", last_error=why)
            changed.append(rid)
        if (facebook_profile_story(s, x) and x.get("method") != "hand"
                and (x.get("method") not in cfg["retired_services"]
                     or x.get("state") == "planned" and not s.publishing_started(x))):
            x["method"] = "hand"           # migrate legacy profile rows without changing their content or slot
            changed.append(rid)
        if x.get("method") == "hand" and x.get("block") == s.IMAGE_APPROVAL_REASON:
            x.pop("block")                 # he sees the image as he posts it by hand
            changed.append(rid)
        if facebook_profile_story(s, x) and x.get("state") == "planned" and x.get("ok"):
            continue                       # His manual backlog survives its slot and vault deadline until Posted or skip.
        if (x.get("weekend") and x.get("state") in ("claimed", "sending") and x.get("when")
                and s.parse(x["when"]).astimezone(s.tz(cfg)).date() < t.astimezone(s.tz(cfg)).date()):
            x.update(state="check", ok=False, weekend_missed=True,
                     last_error="Weekend slot ended; confirm whether it posted before approving another send")
            changed.append(rid)
        if (cfg.get("vault_keep") and x.get("vault") in ("vault", "history", "day")
                and x.get("state") == "planned"
                and (x.get("day", "9999") < t.astimezone(s.tz(cfg)).date().isoformat()
                     or x.get("when") and s.parse(x["when"]).astimezone(s.tz(cfg)).date().isoformat() != x.get("day"))):
            x.update(state="expired", ok=False, skip_reason="the vault day ended or its slot left its day")
            changed.append(rid)
        if x.get("weekend") and x.get("state") == "planned" and not s.wild_image_post(x):
            x.update(state="expired", ok=False, skip_reason="only wild image + prompt posts may use the weekend queue")
            changed.append(rid)
        if (x.get("weekend") and x.get("state") == "planned" and x.get("when")
                and s.parse(x["when"]).astimezone(s.tz(cfg)).date() < t.astimezone(s.tz(cfg)).date()):
            rows = s.story_post_rows(led, rid.split(":")[0])
            for y in rows.values():
                if y.get("state") == "planned":
                    y.update(ok=False, when=None)
            for p, y in rows.items():
                if y.get("state") == "planned":
                    story_weekend_overflow(s, led, cfg, rid.split(":")[0] + ":" + p, y, t)
            changed.append(rid)
        if x.get("before") and t > s.parse(x["before"]) and x.get("state") == "planned":
            x.update(state="expired" if cfg.get("vault_keep") else "skipped", skip_reason="the vault day ended before this platform posted")
            changed.append(rid)
        elif (x.get("state") == "scheduled" and x.get("method") != "buffer"
              and x.get("method") not in cfg["retired_services"] and x.get("when") and s.parse(x["when"]) <= t):
            if s.effects_enabled(cfg, x):
                s.effect_pending(x)
            else:
                x.update(state="posted", posted_at=x["when"])
            changed.append(rid)
        elif x.get("state") == "sending" and s.parse(x.get("sent_at")) < t - dt.timedelta(minutes=15) \
                and not s.Sending("story:" + rid).busy():
            x.update(state="check")
            s.log(led, f"story:{rid}: a send never finished; check the platform before anything else")
            changed.append(rid)
        elif cfg.get("vault_keep") and x.get("retained") and s.wild_image_post(x) and x.get("state") == "planned" and not x.get("ok") and not story_holds(s, cfg, x):
            story_weekend_overflow(s, led, cfg, rid, x, t)
            changed.append(rid)
        elif x.get("state") == "planned" and not x.get("ok") and t > story_open_until(s, x, cfg):
            if cfg.get("vault_keep") and s.wild_image_post(x):
                story_weekend_overflow(s, led, cfg, rid, x, t)
            else:
                x.update(state="skipped", skip_reason=s.STORY_STALE.format(h=s.story_cfg(cfg)["open_hours"]))
            s.log(led, f"story:{rid}: {x.get('skip_reason') or 'text retained, needs checking'}")
            changed.append(rid)
    return changed
