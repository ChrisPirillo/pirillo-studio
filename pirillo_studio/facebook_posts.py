"""Page publication adapters, with the caller supplying transport, configuration and persistence."""

FB_VIDEO = "https://graph-video.facebook.com/v23.0"
FB_RUPLOAD = "https://rupload.facebook.com/video-upload/v23.0"


def facebook_page_row(s, a):
    return a.get("platform") == "facebook" and a.get("kind") != "article"


def facebook_key(s, cfg, token):
    k = s.json.loads(token or "{}")
    want = (cfg.get("platforms", {}).get("facebook") or s.DEFAULTS["platforms"]["facebook"]).get("page")
    if not k.get("page_id") or not k.get("page_token") or (want and
            (k.get("page_handle") or "").lower() != want.lower()):
        raise s.AdapterError("Facebook Page key missing or for another Page; check fbauth.py", False)
    return k


def facebook_live_key(s, cfg, token):
    """Return credentials only for the configured live Page."""
    k = s.json.loads(token or "{}")
    norm = lambda x: (x or "").lower().replace(" ", "")
    want = (cfg.get("platforms", {}).get("facebook") or s.DEFAULTS["platforms"]["facebook"]).get("live_page")
    if not k.get("page_id") or not k.get("page_token") or not want or norm(want) not in (
            norm(k.get("page_handle")), norm(k.get("page_name"))):
        raise s.AdapterError("the live Page's key is missing or names another Page: run fbauth.py for it "
                           "(the Board's Connect card has the clicks)", False)
    return k["page_id"], k["page_token"]


def facebook_live_post(s, a, cfg, token):
    """A text, image or link post to the live Page: the words and a still to /photos, else the words
    (and a link the row names) to /feed. A feed post is public the moment Graph answers, so the
    permalink read-back is a nicety, never a re-send."""
    pid, tok = facebook_live_key(s, cfg, token)
    if a.get("image"):
        body, ctype = s._multipart({"message": a["text"]}, "source", a["image"],
                                 s.mimetypes.guess_type(a["image"])[0] or "image/jpeg")
        _, r = s._http("POST", f"{s.FB_API}/{pid}/photos", tok, body, {"Content-Type": ctype})
    else:
        fields = {"message": a["text"]}
        if a.get("link"):
            fields["link"] = a["link"]
        _, r = s._form("POST", f"{s.FB_API}/{pid}/feed", tok, fields)
    if not (r.get("post_id") or r.get("id")):
        # Graph answered without an id: it may have posted, so never retry (a resend would double-post)
        raise s.AdapterError("live Page post answered without an id; check the Page before sending again", False)
    post_id = str(r.get("post_id") or r.get("id"))
    url = None
    try:
        _, m = s._form("GET", f"{s.FB_API}/{post_id}", tok, {"fields": "permalink_url"}, timeout=30)
        url = m.get("permalink_url")
    except s.AdapterError:
        pass                                      # it posted; a missing permalink never resends it
    if url and url.startswith("/"):
        url = "https://www.facebook.com" + url
    return dict(post_id=post_id, url=url or f"https://www.facebook.com/{post_id}", state="posted")


def facebook_page_url(s, cfg, k=None):
    return "https://www.facebook.com/" + str((cfg.get("platforms", {}).get("facebook") or s.DEFAULTS["platforms"]["facebook"]).get("page") or (k or {}).get("page_id"))


def facebook_pending(s, a, cfg, why=None):
    a.update(facebook_confirm=True, block=None, url=a.get("url") or facebook_page_url(s, cfg))
    n = int(a.get("facebook_confirm_tries") or 0)
    a["facebook_confirm_next"] = s.iso(s.now() + s.dt.timedelta(minutes=min(10 * 2 ** n, 360)))
    if why:
        # Never store a Graph error or request URL: either can contain credentials.
        a["last_error"] = "Facebook accepted a video id; confirmation unavailable"


def facebook_checkpoint(s, a, cfg, vid, phase):
    """Save the allocated id before the next network call, including crash/deadline paths."""
    previous_id = a.get("post_id")
    a.update(post_id=str(vid), facebook_phase=phase)
    facebook_pending(s, a, cfg)
    if a.get("state") != "sending":           # direct adapter probes have no ledger row
        return
    with s.Lock():
        led = s.load()
        live = s.row(led, a["id"])[1]
        if not live or live.get("post_id") not in (None, previous_id, str(vid)):
            raise s.AdapterError("Facebook allocation checkpoint conflicts with the current row", False)
        # A concurrent state change must never discard an allocation response.
        changed = live["state"] != "sending" or live.get("sent_at") != a.get("sent_at")
        live.update({k: a[k] for k in ("post_id", "facebook_phase", "facebook_confirm",
                                     "facebook_confirm_next", "url")})
        s.save(led)
        if changed:
            raise s.AdapterError("Facebook row changed during upload; allocation saved, upload stopped", False)


def facebook(s, a, cfg, token):
    """Upload Page Reels and resumable videos, or route explicit live-Page articles through the API."""
    if a["kind"] == "article":
        if (a.get("account") or {}).get("kind") == "page":
            return facebook_live_post(s, a, cfg, token)
        raise s.AdapterError("text posts go to his profile from the browser, not the Page", False)
    k = s.json.loads(token)
    pid, tok = k["page_id"], k["page_token"]
    want = cfg["platforms"]["facebook"].get("page")
    if want and (k.get("page_handle") or "").lower() != want.lower():   # a key for another Page never posts
        raise s.AdapterError(f"the Facebook key is for {k.get('page_name')}, not {want}: sign in again (fbauth.py)", False)
    # a file_url fetch stops at 1 GB (Graph API); kept for every route: re-encoded to fit, never trimmed
    path = s.fit_video(s.os.path.join(s.content_dir(a["slug"]), a["file"]), s.API_VIDEO_BYTES["facebook"], a["slug"])
    size = s.os.path.getsize(path)
    secs = s._probe_secs(path) or 0
    reel = a["kind"] != "long" and 3 <= secs <= 90
    missed = []
    img, how = s.try_cover(a, "facebook", missed, 8e6)     # /{video-id}/thumbnails: the landscape cut's thumbnail 1, a Reel's frame

    def status(vid):
        _, st = s._form("GET", f"{s.FB_API}/{vid}", tok, {"fields": "status"})
        video_status = st.get("status") or {}
        if video_status.get("video_status") in ("error", "expired") or (video_status.get("uploading_phase") or {}).get("status") == "error":
            raise s.AdapterError(f"Facebook couldn't take the video: {s.json.dumps(video_status)[:200]}", False)
        return video_status, st.get("permalink_url")

    def upload_done(vid):
        for _ in range(120):                          # up to 20 minutes
            video_status, _ = status(vid)
            if (video_status.get("uploading_phase") or {}).get("status") == "complete" or video_status.get("video_status") == "ready":
                return
            s.time.sleep(10)
        raise s.AdapterError("Facebook still taking the video after 20 minutes")

    def reel_upload():
        _, st = s._form("POST", f"{s.FB_API}/{pid}/video_reels", tok, {"upload_phase": "start"})
        vid = st["video_id"]
        facebook_checkpoint(s, a, cfg, vid, "uploading")
        with s.approval_opened(a, path, cfg) as f:                   # streamed: Content-Length lets urllib read it in blocks
            _, r = s._http("POST", f"{FB_RUPLOAD}/{vid}", None, f,
                         {"Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size),
                          "Content-Length": str(size), "Content-Type": "application/octet-stream"}, timeout=1800)
        if not r.get("success"):
            raise s.AdapterError(f"Facebook's upload answered {s.json.dumps(r)[:150]}")
        return vid

    def video_upload():
        try:
            _, st = s._form("POST", f"{FB_VIDEO}/{pid}/videos", tok, {"upload_phase": "start", "file_size": size})
        except s.AdapterError as e:
            if e.retry:
                raise
            return video_by_url()                     # Meta refused to start one: nothing was made yet
        sid, vid = st["upload_session_id"], st["video_id"]
        facebook_checkpoint(s, a, cfg, vid, "uploading")
        lo, hi, stuck = int(st["start_offset"]), int(st["end_offset"]), 0
        with s.approval_opened(a, path, cfg) as f:
            while lo < hi:
                f.seek(lo)
                chunk = f.read(hi - lo)
                b = s.uuid.uuid4().hex
                body = b"".join(f"--{b}\r\nContent-Disposition: form-data; name=\"{n}\"\r\n\r\n{v}\r\n".encode()
                                for n, v in (("access_token", tok), ("upload_phase", "transfer"),
                                             ("start_offset", lo), ("upload_session_id", sid)))\
                    + (f"--{b}\r\nContent-Disposition: form-data; name=\"video_file_chunk\"; filename=\"chunk\"\r\n"
                       f"Content-Type: application/octet-stream\r\n\r\n").encode() + chunk + f"\r\n--{b}--\r\n".encode()
                for attempt in range(3):              # a chunk that failed is sent again from the same offset
                    try:
                        _, r = s._http("POST", f"{FB_VIDEO}/{pid}/videos", None, body,
                                     {"Content-Type": f"multipart/form-data; boundary={b}"}, timeout=600)
                        break
                    except s.AdapterError as e:
                        if not e.retry or attempt == 2:
                            raise
                        s.time.sleep(5)
                new_lo, hi = int(r["start_offset"]), int(r["end_offset"])
                stuck = stuck + 1 if new_lo <= lo else 0
                if stuck >= 3:
                    raise s.AdapterError("Facebook's upload made no progress")
                lo = new_lo
        facebook_checkpoint(s, a, cfg, vid, "publish_requested")
        _, r = s._form("POST", f"{FB_VIDEO}/{pid}/videos", tok, {
            "upload_phase": "finish", "upload_session_id": sid, "description": a["text"], "title": a.get("title") or ""})
        if not r.get("success"):
            raise s.AdapterError(f"Facebook's upload answered {s.json.dumps(r)[:150]}")
        return vid

    def video_by_url():                               # the old way, only if Meta refuses a resumable upload
        with s.public_url(s.approval_check(a, path, cfg)) as url:
            _, st = s._form("POST", f"{FB_VIDEO}/{pid}/videos", tok, {"file_url": url, "description": a["text"],
                                                                   "title": a.get("title") or ""}, timeout=1800)
            facebook_checkpoint(s, a, cfg, st["id"], "publish_requested")
            upload_done(st["id"])                     # the tunnel stays up until Facebook has the file
            return st["id"]

    try:
        if reel:
            vid = reel_upload()
            facebook_checkpoint(s, a, cfg, vid, "publish_requested")
            _, finished = s._form("POST", f"{s.FB_API}/{pid}/video_reels", tok, {
                "upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED", "description": a["text"]})
            if not finished.get("success"):
                raise s.AdapterError(f"Facebook finish not confirmed ({' '.join(s.json.dumps(finished).split())[:120]})", False)
        else:
            vid = video_upload()
        facebook_checkpoint(s, a, cfg, vid, "finished")
        got = s.facebook_confirm.facebook_read(s, a, cfg, token)
    except s.AdapterError:
        if not a.get("post_id") or a.get("facebook_phase") == "uploading":
            raise
        got = None
    if not got:
        facebook_pending(s, a, cfg, True)
        return dict(post_id=a["post_id"], url=a["url"], state="check", facebook_confirm=True,
                    facebook_phase=a.get("facebook_phase"), **s.cover_note(None, missed + ["cover waits for confirmation"]))
    link = got["url"]
    if img:                                           # after the video is up; a miss never undoes the post
        try:
            body, ctype = s._multipart({"access_token": tok, "is_preferred": "true"}, "source", img, "image/jpeg")
            _, r = s._http("POST", f"{s.FB_API}/{vid}/thumbnails", None, body, {"Content-Type": ctype})
            if not r.get("success"):
                raise s.AdapterError(f"Facebook answered {s.json.dumps(r)[:100]}")
        except s.AdapterError as e:
            how = None
            missed.append(f"cover ({str(e)[:150]})")
    if link and link.startswith("/"):
        link = "https://www.facebook.com" + link
    return dict(post_id=vid, url=link or f"https://www.facebook.com/{cfg['platforms']['facebook'].get('page', pid)}",
                state="posted", **s.cover_note(how, missed))
