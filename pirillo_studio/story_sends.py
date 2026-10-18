"""One API post of a story row (the day's text posts), using the caller's runtime and ledger."""

from pirillo_studio import buffer_api, hold
import functools
import datetime as dt


def story_send(s, rid, cfg):
    """One API post of a story row, as send() sends a video's: the row says 'sending' and is saved before the call,
    the lock is released across it, and a send that stops mid-way is a check, never sent again."""
    s.only_installed("send"); cfg = hold.current(cfg, s.CONFIG, s.DEFAULTS)
    sid = "story:" + rid
    token = None
    slot = s.Sending(sid)
    try:
        import copy
        while True:
            with s.Lock():
                led = s.load()
                original = (led.get("storyposts") or {}).get(rid)
                if not original or rid not in s.story_due(led, cfg, s.now()):
                    return
                x = copy.deepcopy(original)
            ready = s.story_image_ready(x, cfg, led)
            fn = token = None
            if ready:
                if x.get("digest"):
                    fn = s.digest_adapter(x)
                    token = s.key(x["account"]["key_account"])
                else:
                    fn = s.story_adapter(x, cfg) or (functools.partial(buffer_api.buffer_post, s) if x.get("method") == "buffer" else s.ADAPTERS.get(x["platform"]))
                    token = s.key(x["method"] if x.get("method") == "buffer" else ((x.get("account") or {}).get("key_account") or x["platform"]))   # a Page row keeps its own key account (#831)
                x["block"] = (f"no {x['platform']} adapter" if not fn else
                              f"no Keychain key: service {s.KEY_SERVICE}, account {(x.get('account') or {}).get('key_account', x['platform'])}" if not token else None)
            job = s.story_send_job(x, rid, cfg, led, ready)
            if s.effects_enabled(cfg, x):
                job["_effect_source"] = s.effect_source(x)
            with s.Lock():
                led = s.load()
                current = (led.get("storyposts") or {}).get(rid)
                if current != original:
                    continue
                if not current or rid not in s.story_due(led, cfg, s.now()):
                    return
                current.update(x)
                x = current
                if not ready or x["block"] or not slot.acquire():
                    s.save(led)
                    return
                s.record_scheduled_images(x, job.get("image"), fresh=True)
                x.pop("posting_hold", None); x.update(state="sending", sent_at=s.iso(s.now()), attempts=x.get("attempts", 0) + 1)
                mine = (x["sent_at"], x["attempts"])
                job.update(sent_at=x["sent_at"])
                s.log(led, f"{sid}: sending")
                s.save(led)
                break
        r = err = lost = None
        try:
            with s.deadline(cfg.get("send_deadline_minutes", 120) * 60):
                if job.get("method") == "buffer" and job.get("image"):
                    s.media_host_short(sid, cfg, job)
                r = s.effect_send(job, cfg, token, fn, sid)
        except s.AdapterError as e:
            err = e
        except (s.SendDeadline, Exception) as e:
            lost = e
        finally:
            token = None
        with s.Lock():
            led = s.load()
            x = led["storyposts"][rid]
            ours = x["state"] == "sending" and (x.get("sent_at"), x.get("attempts")) == mine
            if r is not None:
                x.update(post_id=r.get("post_id"), url=r.get("url"))
                if "urls" in r:
                    x["urls"] = r["urls"]
                if s.effects_enabled(cfg, x):
                    x.update({k: r[k] for k in ("effect_receipt", "effect_alert", "effect_readback", "last_error") if k in r})
                state = r.get("state", "posted")
                x.update(state=state if ours and state in ("posted", "scheduled") else "check")
                if x["state"] == "posted":
                    x["posted_at"] = s.iso(s.now())
                if "image" in r:
                    x["image_sent"] = bool(r["image"])
                if r.get("image_note"):
                    x["image_note"] = r["image_note"]
                if "topic_why" in r:
                    x.update(topic_sent=r.get("topic"), topic_why=r["topic_why"])
                if r.get("missed"):
                    x["missed"] = r["missed"]
                s.log(led, f"{sid}: {x['state']}" + (f"; missed: {'; '.join(r['missed'])}" if r.get("missed") else ""))
            elif err is not None:
                x["last_error"] = str(err)[:300]
                if ours:
                    if x.get("digest") or not err.retry or x["attempts"] >= cfg["max_attempts"]:
                        x.update(state="handoff")
                    else:
                        x.update(state="planned", next_try=s.iso(s.now() + dt.timedelta(minutes=cfg["retry_minutes"])))
                s.log(led, f"{sid}: failed ({x['last_error'][:120]}) -> {x['state']}")
            else:
                x["last_error"] = f"{type(lost).__name__}: {lost}"[:300]
                if ours:
                    x.update(state="check")
                s.log(led, f"{sid}: the send stopped mid-way ({x['last_error'][:120]}); check the platform")
            s.save(led)
        if lost is not None and not isinstance(lost, s.SendDeadline):
            raise lost
    finally:
        slot.release()
