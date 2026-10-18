"""TikTok draft uploads, using the caller's runtime."""

TT_API = "https://open.tiktokapis.com"


def tiktok(s, a, cfg, token):
    """Upload to the creator's TikTok inbox as a draft via video.upload, FILE_UPLOAD in chunks.
    Refresh the access token when under 10 minutes remain. Nothing posts directly.
    """
    ck = cfg["platforms"]["tiktok"].get("sandbox_client_key")
    t = s.json.loads(token)
    if t.get("expires_at", 0) - s.time.time() < 600:
        _, r = s._http("POST", f"{TT_API}/v2/oauth/token/", None, s.urllib.parse.urlencode({
            "client_key": ck, "client_secret": s.key("tiktok_client_secret"), "grant_type": "refresh_token",
            "refresh_token": t["refresh_token"]}).encode(), {"Content-Type": "application/x-www-form-urlencoded"})
        if not r.get("access_token"):
            raise s.AdapterError(f"TikTok sign-in needs renewing: {r.get('error_description') or r.get('error')}", False)
        t = dict(t, access_token=r["access_token"], refresh_token=r.get("refresh_token", t["refresh_token"]),
                 expires_at=s.time.time() + int(r.get("expires_in", 0)))
        if s.keychain():
            s.subprocess.run(["security", "add-generic-password", "-U", "-s", s.KEY_SERVICE, "-a", "tiktok",
                            "-w", s.json.dumps(t)], capture_output=True, timeout=20)
    path = s.os.path.join(s.content_dir(a["slug"]), a["file"])
    size = s.os.path.getsize(path)
    chunk = size if size <= 64_000_000 else 20_000_000
    count = max(1, size // chunk)
    j = {"Content-Type": "application/json; charset=UTF-8"}
    _, init = s._http("POST", f"{TT_API}/v2/post/publish/inbox/video/init/", t["access_token"], s.json.dumps(
        {"source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                         "total_chunk_count": count}}).encode(), j)
    d = init.get("data") or {}
    if not d.get("upload_url"):
        raise s.AdapterError(f"TikTok refused the upload: {init.get('error')}", False)
    with s.approval_opened(a, path, cfg) as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            f.seek(start)
            data = f.read(end - start + 1)
            req = s.urllib.request.Request(d["upload_url"], data=data, method="PUT", headers={
                "Content-Type": "video/mp4", "Content-Range": f"bytes {start}-{end}/{size}"})
            try:
                s.urllib.request.urlopen(req, timeout=600).read()
            except (s.urllib.error.URLError, OSError) as e:
                raise s.AdapterError(f"TikTok upload part {i + 1}/{count} failed: {e}")
    for _ in range(90):                                   # up to 15 minutes
        _, st = s._http("POST", f"{TT_API}/v2/post/publish/status/fetch/", t["access_token"],
                      s.json.dumps({"publish_id": d["publish_id"]}).encode(), j)
        status = (st.get("data") or {}).get("status")
        if status == "SEND_TO_USER_INBOX":
            break
        if status == "FAILED":
            raise s.AdapterError(f"TikTok couldn't take the video: {(st.get('data') or {}).get('fail_reason')}", False)
        s.time.sleep(10)
    else:
        raise s.AdapterError("TikTok still processing after 15 minutes")
    return dict(post_id=d["publish_id"], url=None, state="handoff", block=s.TIKTOK_DRAFT)
