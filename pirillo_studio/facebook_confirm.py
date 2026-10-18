"""Facebook publication confirmation, using the caller's runtime and ledger."""

FB_CONFIRM_TRIES = 6


def facebook_recent(s, a, cfg, k):
    """Bounded Page-owned listing. An id wins; title-only recovery must be unique and near the send."""
    matches = []
    after = None
    for _ in range(3):
        params = {"fields": "id,title,created_time,permalink_url,published", "limit": 50}
        if after:
            params["after"] = after
        _, doc = s._form("GET", f"{s.FB_API}/{k['page_id']}/videos", k["page_token"], params, timeout=10)
        for video in doc.get("data") or []:
            if a.get("post_id"):
                if str(video.get("id")) == str(a["post_id"]) and video.get("published") is True:
                    return video
            elif a.get("title") and video.get("title") == a["title"] and video.get("published") is True:
                if a.get("sent_at") and video.get("created_time") and abs(
                        (s.parse(video["created_time"]) - s.parse(a["sent_at"])).total_seconds()) <= 7200:
                    matches.append(video)
        paging = doc.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after") if paging.get("next") else None
        if not after:
            break
    return matches[0] if len(matches) == 1 else None


def facebook_read(s, a, cfg, token, recent=False):
    k = s.facebook_posts.facebook_key(s, cfg, token)
    if recent or not a.get("post_id"):
        found = facebook_recent(s, a, cfg, k)
        if found:
            return dict(post_id=str(found["id"]), url=facebook_link(s, found.get("permalink_url"), cfg, k), state="posted")
        return None
    # Meta's Reels example reads status alone. A permalink miss cannot invalidate publication.
    try:
        _, doc = s._form("GET", f"{s.FB_API}/{a['post_id']}", k["page_token"], {"fields": "status"}, timeout=30)
    except s.AdapterError:
        return facebook_read(s, a, cfg, token, recent=True)
    if not isinstance(doc, dict) or not isinstance(doc.get("status"), dict):
        raise ValueError("Facebook status response is malformed")
    status = doc["status"]
    for phase in ("publishing_phase", "processing_phase", "uploading_phase"):
        if phase in status and not isinstance(status[phase], dict):
            raise ValueError(f"Facebook {phase} is malformed")
    publishing = status.get("publishing_phase", {})
    published = publishing.get("status") == "complete"
    if not published and not status.get("publishing_phase") and a.get("facebook_phase") == "finished" and status.get("video_status") == "ready":
        published = True
    if not published:
        return None
    link = doc.get("permalink_url")
    if not link:
        try:
            _, meta = s._form("GET", f"{s.FB_API}/{a['post_id']}", k["page_token"], {"fields": "permalink_url"}, timeout=30)
            link = meta.get("permalink_url") if isinstance(meta, dict) else None
        except Exception:
            # Publication is already confirmed; optional metadata cannot undo it.
            pass
    if not isinstance(link, str):
        link = None
    return dict(post_id=str(a["post_id"]), url=facebook_link(s, link, cfg, k), state="posted")


def facebook_link(s, link, cfg, k):
    if link and link.startswith("/"):
        link = "https://www.facebook.com" + link
    return link or s.facebook_posts.facebook_page_url(s, cfg, k)


def facebook_handoff(s, a):
    """Every Page handoff checks recent videos first, even if the direct node read failed."""
    cfg = s.cfg_now()
    try:
        got = facebook_read(s, a, cfg, s.key("facebook"), recent=True)
    except (s.AdapterError, ValueError, KeyError, OSError):
        got = None
        # Cannot exclude an existing post: do not invite another upload.
        s.facebook_posts.facebook_pending(s, a, cfg, True)
        return "check"
    if got:
        a.update(got, posted_at=a.get("posted_at") or a.get("sent_at") or s.iso(s.now()), block=None)
        a.pop("facebook_confirm", None)
        return "posted"
    if a.get("post_id"):
        s.facebook_posts.facebook_pending(s, a, cfg, True)
        return "check"
    return "handoff"


def facebook_confirm_due(s, a, t):
    return (s.facebook_posts.facebook_page_row(s, a) and a.get("facebook_confirm") and a.get("post_id") and
            a.get("state") == "check" and int(a.get("facebook_confirm_tries") or 0) < FB_CONFIRM_TRIES and
            (not a.get("facebook_confirm_next") or s.parse(a["facebook_confirm_next"]) <= t))


def confirm_facebook(s, aid, cfg):
    slot = s.Sending(aid)
    if not slot.acquire():
        return
    try:
        with s.Lock():
            led = s.load()
            a = s.row(led, aid)[1]
            if not a or not facebook_confirm_due(s, a, s.now()):
                return
            job = dict(a)
        try:
            got = facebook_read(s, job, cfg, s.key("facebook"))
        except Exception:
            got = None
        with s.Lock():
            led = s.load()
            a = s.row(led, aid)[1]
            if not a or a["state"] != "check" or a.get("post_id") != job.get("post_id"):
                return
            if got:
                a.update(got, posted_at=a.get("sent_at") or s.iso(s.now()), block=None, last_error=None)
                a.pop("facebook_confirm", None)
                a.pop("facebook_confirm_next", None)
            else:
                a["facebook_confirm_tries"] = int(a.get("facebook_confirm_tries") or 0) + 1
                s.facebook_posts.facebook_pending(s, a, cfg, True)
            s.log(led, f"{aid}: " + ("posted, confirmed on Facebook" if got else "sent, confirming Facebook"))
            s.save(led)
    finally:
        slot.release()


def facebook_confirmation_words(s, a):
    if not a.get("post_id"):
        return "Upload could not be confirmed; check the Page before posting: " + str(a.get("url") or "")
    if int(a.get("facebook_confirm_tries") or 0) >= FB_CONFIRM_TRIES:
        return "Sent, couldn't confirm; check the Page: " + str(a.get("url") or "")
    return "Sent, confirming on Facebook"


def tick_confirmations(s, cfg):
    with s.Lock():
        confirmations = [a["id"] for _, a in s.assets(s.load()) if facebook_confirm_due(s, a, s.now())]
    for aid in confirmations:
        confirm_facebook(s, aid, cfg)
