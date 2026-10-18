"""tick_prepare, tick, wake: moved from the host by dev/port.py; `s` is the host (pirillo_studio.host.View)."""
import datetime as dt
import functools
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pirillo_studio import buffer_api
from pirillo_studio import buffer_checks
from pirillo_studio import counts
from pirillo_studio import facebook_confirm
from pirillo_studio import housekeeping
from pirillo_studio import live_edits
from pirillo_studio import mastodon_edits
from pirillo_studio import reminders
from pirillo_studio import reschedule
from pirillo_studio import scheduling
from pirillo_studio import story_sends
from pirillo_studio import tips_check
from pirillo_studio import tokens
from pirillo_studio import hold


def tick_prepare(s, cfg, led):
    """Plan on a private snapshot. File reads and route preflight never hold the ledger lock."""
    t = s.holds_refresh(led, cfg) or s.now()   # a still that arrived moves its hold on (#1142)
    for v, a in s.assets(led):
        if a["state"] == "sending" and s.parse(a.get("sent_at")) < t - dt.timedelta(minutes=15) \
                and not s.Sending(a["id"]).busy():
            s.move(a, "check", "tick")             # never resend: a session looks at the platform
            s.log(led, f"{a['id']}: a send never finished; check the platform before anything else")
        if a["state"] == "claimed" and s.parse(a.get("claimed_at")) < t - dt.timedelta(hours=cfg["claim_hours"]):
            s.move(a, "check", "tick")             # listed for Chrome, never reported: look before redoing
            s.log(led, f"{a['id']}: a Chrome job was never reported; check the platform")
    scheduling.plan(s, cfg, led)
    s.morning_card(cfg, led, t)
    yt_signed = s.yt_signed_in()
    s.yt_api_route(cfg, led, yt_signed)     # #898: parked browser hand-offs become API uploads; one sign-in ask
    todo, swaps, retimes, buffer_checks = [], [], [], []
    fit_caps = {}
    for v, a in s.assets(led):
        if a.get("block") == s.WAITS_FOR_YOUTUBE:      # an old run's note, not a block
            a["block"] = None
        if a.get("state") == "planned" and s.image_approval_block(a, cfg, led):
            a["block"] = s.image_approval_block(a, cfg, led)
            print(f"{a['id']}: held: {a['block']}", flush=True)
            continue
        if a.get("block") == s.IMAGE_APPROVAL_REASON and s.turn_decisions_live(cfg, t):
            a["block"] = None                      # approved since, or past planned: the hold is over
        if cfg.get("release_media_prepare"):
            why = s.prepare_video_fit(cfg, a, fit_caps)
            old = a.pop("video_fit_wait", None) or (s.VIDEO_FIT_PENDING if a.get("block") == s.VIDEO_FIT_PENDING else None)
            if why:
                a["video_fit_wait"] = why
                if not a.get("block") or a.get("block") == old:
                    a["block"] = why
                print(f"{a['id']}: held: {why}", flush=True)
                continue
            if old and a.get("block") == old:
                a["block"] = None
        if s.due(v, a, t, cfg):
            if not yt_signed and a.get("platform") == "youtube" and a.get("method") == "api_native":
                continue              # #898: the one yt-signin ask carries it; never a per-row block or hand-off
            if (not cfg.get("release_media_prepare") and a.get("platform") == "instagram"
                    and a.get("file") and not a.get("image")):
                try:
                    s.fit_video(os.path.join(s.content_dir(a["slug"]), a["file"]), s.API_VIDEO_BYTES["instagram"], a["slug"], background=True)
                except (s.AdapterError, OSError, ValueError) as e:
                    a["block"] = str(e)
                    print(f"{a['id']}: held: {e}", flush=True)
                    continue
            todo.append(a["id"])
        if reschedule.reschedule_due(s, cfg, a, t):              # its drop moved: the clock moves it on the platform's scheduler
            retimes.append(a["id"])
    for v, a in s.assets(led):
        if a.get("method") in cfg["retired_services"]:
            continue
        if a.get("method") == "buffer" and a["state"] in ("scheduled", "check", "posted") and a.get("post_id"):
            if a["state"] != "scheduled" or not a.get("scheduled_for") or s.parse(a["scheduled_for"]) <= t:
                buffer_checks.append(a["id"])
            continue
        if a["state"] == "scheduled" and a["scheduled_for"] and s.parse(a["scheduled_for"]) <= t:
            if a["platform"] == "mastodon" and a["method"] == "api_native" and a.get("replace"):
                if mastodon_edits.mastodon_replace_due(s, a, t):
                    swaps.append(a["id"])
                continue                     # his words go on after it publishes, then it's posted
            if s.effects_enabled(cfg, a):
                s.effect_pending(a)
            else:
                s.move(a, "posted", "tick")
                a["posted_at"] = a["scheduled_for"]
                if a.pop("reschedule", None):
                    s.log(led, f"{a['id']}: went out at its old time ({s.local(s.parse(a['scheduled_for']), cfg)}) before the "
                             "publisher changed it; recorded there, and the rest of its drop stays where it is")
    for rid, x in (led.get("storyposts") or {}).items():
        if x.get("method") == "buffer" and x.get("state") in ("scheduled", "check", "posted"):
            if x.get("state") != "scheduled" or not x.get("when") or s.parse(x["when"]) <= t:
                buffer_checks.append("story:" + rid)
    s.story_sweep(led, cfg, t)                     # the day's five posts: live, a check, or dropped unapproved
    for line in s.tips_add(led, cfg, t) + s.tips_refill(led, cfg, t):   # the tips slot: top up his Board, mine more when low
        if not line.startswith("tips: off"):
            print(line, flush=True)
    s.vault_sweep(led, cfg, t)                     # the vault post he picked nothing from by the end of its day
    s.image_approval_asks(cfg, led, t)             # a post held on its image's approval is one ask, the image on it
    stories = s.story_due(led, cfg, t)
    said = s.unok_said(led, cfg, t)                # on a platform's scheduler without his OK: say so every tick
    return todo, swaps, retimes, buffer_checks, stories, said


class Steps:
    """A tick's steps, timed (2026-10-17: a 54-minute tick that nobody could place). The step in hand and when it
    began are in DATA_DIR/tick.step.<pid> until the tick ends, which the keeper's stuck alert reads for the pid it
    watches (one file per tick, so a manual tick beside a stuck one hides nothing); a step past cfg
    tick_step_slow_seconds is named in the log as it ends; the tick's last line gives every step's seconds."""

    def __init__(self, data_dir, slow):
        self.path, self.slow, self.began, self.done, self.cur = os.path.join(data_dir, f"tick.step.{os.getpid()}"), slow, time.time(), [], None

    def __call__(self, name=None):
        """End the step in hand and begin `name`; no name ends the tick."""
        if self.cur:
            took = time.time() - self.cur[1]
            self.done.append(f"{self.cur[0]} {took:.0f}")
            if took > self.slow:
                print(f"tick: {self.cur[0]} took {took:.0f} s, over tick_step_slow_seconds {self.slow}", flush=True)
        if name is None:
            began = dt.datetime.fromtimestamp(self.began).astimezone().isoformat(timespec="seconds")
            print(f"tick {began}: {time.time() - self.began:.0f} s ({', '.join(self.done)})", flush=True)
            if self.cur:
                os.remove(self.path)
            return
        began = time.time()
        with open(self.path + ".tmp", "w") as f:
            json.dump(dict(pid=os.getpid(), step=name, since=dt.datetime.now().astimezone().isoformat(timespec="seconds")), f)
        os.replace(self.path + ".tmp", self.path)
        self.cur = (name, began)   # only once its file exists, which the tick's end removes


def tick(s, cfg):
    """Claim/save rows briefly; planning, sends, cleanup and Board rendering run outside Lock."""
    step = Steps(s.DATA_DIR, cfg["tick_step_slow_seconds"])
    try:
        _tick(s, cfg, step)
    finally:
        step()


def _tick(s, cfg, step):
    s.only_installed("tick")
    step("lock")
    with s.Lock():
        with open(s.BEAT, "w") as f:
            f.write(s.now().isoformat())
    step("prepare")
    (todo, swaps, retimes, checks, stories, said), _ = s.tick_update(lambda led: tick_prepare(s, cfg, led))
    if said:
        print(said, flush=True)
    step("wake")
    housekeeping.tick_housekeeping(s, cfg, [], wake_only=True)
    step("sends")
    broke = []
    me = s
    for fn, aids in ((s.send, todo), (functools.partial(live_edits.reschedule_api, me), retimes),
                     (functools.partial(live_edits.replace_after_publish, me), swaps),
                     (functools.partial(buffer_checks.buffer_verify, me), checks), (functools.partial(story_sends.story_send, me), stories)):
        for aid in aids:
            try:
                fn(aid, cfg)
            except Exception as e:                   # one adapter's bug: its row is a check already (send() wrote that
                broke.append(aid)                    # first); the other due rows still go out, and the tick ends loud
                print(f"{aid}: {getattr(fn, 'func', fn).__name__} raised {type(e).__name__}: {' '.join(str(e).split())[:200]}; "
                      "the tick goes on with the rest", file=sys.stderr, flush=True)
                traceback.print_exc()
    step("cleanup")
    buffer_api.buffer_host_cleanup(s)
    if s.effects_enabled(cfg):
        s.effect_recover(cfg)
    step("confirmations")
    if cfg.get("facebook_confirmation", True):
        facebook_confirm.tick_confirmations(s, cfg)
    step("tokens")
    renewed = tokens.refresh_tokens(s)
    step("reminders")
    reminders.remind(s, cfg)
    step("counts")
    try:
        counts.collect_counts(s, cfg)   # each platform's own counts for a post at day 1 and day 7: no model
    except s.LockStuck:
        raise
    except Exception as e:                           # never a reason to lose the Board or the next tick
        print(f"counts: {type(e).__name__}: {' '.join(str(e).split())[:200]}; the tick goes on", file=sys.stderr, flush=True)
    step("tips")
    try:
        for line in tips_check.tips_x_check(s, cfg):               # a tip's 30-minute X check: under engagement, one repost, once
            print(line, flush=True)
    except s.LockStuck:
        raise
    except Exception as e:                           # the tips' check never costs the tick either
        print(f"tips: {type(e).__name__}: {' '.join(str(e).split())[:200]}; the tick goes on", file=sys.stderr, flush=True)
    step("housekeeping")
    housekeeping.tick_housekeeping(s, cfg, renewed)
    step("board")
    housekeeping.tick_board(s, cfg)
    if broke:
        sys.exit(f"tick: {len(broke)} send(s) raised and are checks now ({', '.join(broke)}); everything else ran")


def wake(s, cfg, led):
    """pmset wake 3 minutes before each api_now post in the next 36 hours. Needs, once:
    /etc/sudoers.d/studio-wake with `<user> ALL=(root) NOPASSWD: /usr/bin/pmset schedule wake *`.
    Without it, posts go out the next time the Mac is awake, marked late."""
    s.only_installed("wake")
    if not cfg.get("wake") or not shutil.which("pmset"):
        return
    t = s.now()
    done = set(led.setdefault("wakes", []))
    for v, a in s.assets(led):
        if not (v["approved"] and a["method"] == "api_now" and a["state"] == "planned" and a["scheduled_for"]):
            continue
        w = s.parse(a["scheduled_for"]) - dt.timedelta(minutes=3)
        if t < w < t + dt.timedelta(hours=36):
            stamp = w.astimezone(s.tz(cfg)).strftime("%m/%d/%y %H:%M:%S")
            if stamp in done:
                continue
            try:                                  # under the ledger lock: never unbounded
                r = subprocess.run(["sudo", "-n", "/usr/bin/pmset", "schedule", "wake", stamp],
                                   capture_output=True, text=True, timeout=20)
            except subprocess.TimeoutExpired:
                led["wake_error"] = "pmset didn't answer in 20 s"
                continue
            if r.returncode == 0:
                led["wakes"].append(stamp)
                done.add(stamp)
            else:
                led["wake_error"] = "pmset needs the sudoers line (see content-pipeline SKILL.md)"


def unok_on_scheduler(s, led, cfg, t):
    """Posts that sit on a platform's scheduler without his OK, soonest first: [(row, minutes until it
    publishes)]. The platform publishes each at its time whatever the ledger says; nothing here pulls one."""
    out = [(a, (s.parse(a["scheduled_for"]) - t).total_seconds() / 60) for v, a in s.assets(led)
           if a["state"] == "scheduled" and a.get("scheduled_for") and not v.get("outside_calendar")
           and not scheduling.cleared(s, v, a, cfg)]
    return sorted(out, key=lambda x: x[1])


def _span(s, minutes):
    m = int(round(abs(minutes)))
    words = f"{m} min" if m < 90 else f"{m / 60:.0f} h" if m < 48 * 60 else f"{m / 1440:.0f} days"
    return f"in {words}" if minutes >= 0 else f"{words} ago"


def unok_line(s, a, minutes, cfg):
    return (f"{a['id']}: on {s.plat_name(a['platform'])}'s scheduler without his OK; it publishes "
            f"{s._at(a['scheduled_for'], cfg)} ({_span(s, minutes)})")


def unok_said(s, led, cfg, t):
    """The tick's line for those posts (None when there are none), printed every tick into studio-tick.log.
    A post seen there for the first time also gets one line in the ledger's log."""
    rows = unok_on_scheduler(s, led, cfg, t)
    known = set(led.get("unok_seen") or [])
    for a, m in rows:
        if a["id"] not in known:
            s.log(led, unok_line(s, a, m, cfg))
    if rows or known:
        led["unok_seen"] = [a["id"] for a, _ in rows]
    if not rows:
        return None
    return (f"{len(rows)} post{'s' if len(rows) != 1 else ''} on a platform's scheduler without his OK (each publishes "
            "at its time unless he OKs it on the Board or it is pulled on the platform): "
            + "; ".join(f"{a['id']} {_span(s, m)}" for a, m in rows))


def due(s, v, a, t, cfg):
    """Should the clock send this row now? Asked when the tick looks, and again under the lock
    just before the send (another tick may have sent it in between). Never without his OK on the
    post (cleared): a Mastodon row is handed to its scheduler days ahead, so the OK is checked here."""
    if hold.reason(cfg, a, v) or not scheduling.cleared(s, v, a, cfg) or a["state"] != "planned" or not a["scheduled_for"]:
        return False
    if a.get("next_try") and s.parse(a["next_try"]) > t:
        return False
    if not (a["platform"] == "youtube" and a["kind"] == "long") and not s.anchored(v):
        return False
    when = s.parse(a["scheduled_for"])
    return (a["method"] in ("api_native", "buffer") and when > t + dt.timedelta(minutes=6)) or \
           (a["method"] == "api_now" and when <= t)
