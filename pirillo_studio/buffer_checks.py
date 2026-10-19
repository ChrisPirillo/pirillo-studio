"""Buffer publication checks, using the caller's runtime and ledger."""

from pirillo_studio import buffer_api


def buffer_verify(s, aid, cfg):
    """Only Buffer's sent status and public permalink confirm publication; the slot passing never does."""
    with s.Lock():
        a = s.publishing_row(s.load(), aid)
        if not a or a.get("method") != "buffer" or not a.get("post_id"):
            return
        post_id, platform = a["post_id"], a["platform"]
    try:
        post = buffer_api.buffer_read_post(s, post_id, s.key(buffer_api.buffer_account(s, cfg, platform)))
    except s.AdapterError:
        return                               # transient read failure leaves the row safe, never resends
    with s.Lock():
        led = s.load()
        a = s.publishing_row(led, aid)
        if not a or a.get("post_id") != post_id or a["state"] not in ("scheduled", "check", "posted"):
            return
        if post.get("id") != post_id:
            return
        status = post.get("status")
        if status == "sent" and post.get("externalLink") and post.get("sentAt"):
            if s.effects_enabled(cfg, a):
                receipt = dict(id=post_id, state="confirmed", result=dict(post_id=post_id, state="posted",
                               url=post["externalLink"], posted_at=s.parse(post["sentAt"].replace("Z", "+00:00")).isoformat()))
                observed = s.effect_check(a, cfg, aid, observed=receipt)
                if observed.get("state") != "posted":
                    a.update(observed)
                    s.save(led)
                    return
                a["effect_receipt"] = observed["effect_receipt"]
                a.pop("effect_readback", None)
                a.pop("effect_alert", None)
            if a["state"] != "posted":
                s.move(a, "posted", "buffer")
            a.update(url=post["externalLink"], posted_at=s.parse(post["sentAt"].replace("Z", "+00:00")).isoformat())
            s.move(a, "verified", "buffer")
            a["verified"] = True
            a.pop("last_error", None)
        elif status in ("error", "draft", "needs_approval") and a["state"] == "scheduled":
            s.move(a, "check", "buffer")
            a["last_error"] = "Buffer has not published this post: " + status
        if status == "error":
            a["buffer_host_released"] = True
        s.save(led)
    buffer_api.buffer_host_cleanup(s)
