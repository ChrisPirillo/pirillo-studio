"""YouTube uploads: the adapter, the resumable upload, its extras and the channel gate, using the caller's runtime."""
import json


def yt_upload(s, path, snippet, status, tok, chunk=64 * 1024 * 1024, f=None):
    """videos.insert, resumable: start the session, then send the file in chunks. Returns the video."""
    size = s.os.path.getsize(path)
    req = s.urllib.request.Request(f"https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable"
                                 f"&part=snippet,status", method="POST",
                                 data=s.json.dumps({"snippet": snippet, "status": status}).encode(),
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json",
                                          "X-Upload-Content-Type": "video/*", "X-Upload-Content-Length": str(size)})
    try:
        with s.urllib.request.urlopen(req, timeout=60) as r:
            session = r.headers["Location"]
    except s.urllib.error.HTTPError as e:
        raise s.AdapterError(f"YouTube refused the upload: HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}",
                           retry=e.code >= 500)
    sent = 0
    with f or open(path, "rb") as f:
        while sent < size:
            f.seek(sent)
            buf = f.read(chunk)
            put = s.urllib.request.Request(session, method="PUT", data=buf, headers={
                "Authorization": f"Bearer {tok}", "Content-Length": str(len(buf)),
                "Content-Range": f"bytes {sent}-{sent + len(buf) - 1}/{size}"})
            try:
                with s.urllib.request.urlopen(put, timeout=600) as r:
                    return s.json.loads(r.read())                 # 200/201: the last chunk, the video
            except s.urllib.error.HTTPError as e:
                if e.code != 308:                                # 308 = keep going
                    raise s.AdapterError(f"YouTube upload stopped: HTTP {e.code}: "
                                       f"{e.read()[:300].decode('utf-8', 'replace')}", retry=e.code >= 500)
                rng = e.headers.get("Range")                     # bytes=0-N received so far
                sent = int(rng.split("-")[1]) + 1 if rng else 0
    raise s.AdapterError("YouTube upload ended without a video")


def yt_extras(s, vid, tok, thumb=None, srt=None, playlist=None, lang="en"):
    """Thumbnail, captions and playlist for an uploaded video. Each is its own call; one failing
    leaves the video up and says which."""
    missed = []
    if thumb and s.os.path.exists(thumb):
        try:
            with open(thumb, "rb") as f:
                s._http("POST", f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={vid}", tok,
                      f.read(), {"Content-Type": "image/png" if thumb.lower().endswith(".png") else "image/jpeg"})
        except s.AdapterError as e:
            missed.append(f"thumbnail ({e})")
    if srt and s.os.path.exists(srt):
        b = s.uuid.uuid4().hex
        meta = s.json.dumps({"snippet": {"videoId": vid, "language": lang, "name": ""}})
        body = (f"--{b}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{b}\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + s.rb(srt) \
            + f"\r\n--{b}--\r\n".encode()
        try:
            s._http("POST", "https://www.googleapis.com/upload/youtube/v3/captions?uploadType=multipart&part=snippet",
                  tok, body, {"Content-Type": f"multipart/related; boundary={b}"})
        except s.AdapterError as e:
            missed.append(f"captions ({e})")
    if playlist:
        try:
            s._http("POST", f"{s.YT_API}/playlistItems?part=snippet", tok, s.json.dumps({"snippet": {
                "playlistId": playlist, "resourceId": {"kind": "youtube#video", "videoId": vid}}}).encode(),
                {"Content-Type": "application/json"})
        except s.AdapterError as e:
            missed.append(f"playlist ({e})")
    return missed


def yt_expect_channel(s, k):
    """The channel-identity gate every posting path shares: the config must name the channel the key
    is for. A missing channel_handle refuses (a key with nothing to be checked against never uploads,
    to whatever channel happened to sign in), and a different one refuses (never another channel)."""
    want = s.load_config()["platforms"]["youtube"].get("channel_handle")
    if not want:
        raise s.AdapterError("platforms.youtube.channel_handle is missing from studio.json, so the YouTube "
                           "key has no channel to be checked against; nothing posts", False)
    if (k.get("channel_handle") or "").lower() != want.lower():
        raise s.AdapterError(f"the YouTube key is for {k.get('channel_title')}, not {want}", False)


def youtube(s, a, cfg, token):
    """The YouTube adapter: videos.insert with publishAt (YouTube's own scheduler holds the time),
    then the thumbnail and captions. A failing extra never fails the upload (yt_extras)."""
    tok, k = s.yt_access("youtube")
    yt_expect_channel(s, k)
    path = s.os.path.join(s.content_dir(a["slug"]), a["file"])
    if not s.os.path.exists(path):
        raise s.AdapterError(f"its file is missing: {a['file']}", False)
    try:
        rel = s.rj(s.release_path(a["slug"]))
    except (OSError, ValueError) as e:      # never upload without its release words and tags
        raise s.AdapterError(f"its release file is unreadable: {e}", False)
    snippet = dict(title=a["title"], categoryId="28", description=a.get("description") or a["text"] or "")
    if a["kind"] == "long":
        spec = rel.get("long") or {}
        snippet.update(description=a.get("description") or spec.get("description") or "", tags=spec.get("tags", []))
    else:
        shorts = rel.get("shorts") or []
        spec = shorts[a["idx"] - 1] if a["kind"] == "short" and a["idx"] <= len(shorts) else {}
    when = s.parse(a["scheduled_for"])
    v = yt_upload(s, path, snippet, {"privacyStatus": "private",
                                  "publishAt": when.astimezone(s.dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                                  "selfDeclaredMadeForKids": False}, tok, f=s.approval_opened(a, path, cfg))
    missed = []
    thumb, how = s.try_cover(a, "youtube", missed)
    missed += yt_extras(s, v["id"], tok, thumb=thumb,
                        srt=s.os.path.join(s.content_dir(a["slug"]), spec["srt"]) if spec.get("srt") else None)
    return dict(state="scheduled", post_id=v["id"], url=f"https://youtu.be/{v['id']}", cover=how,
                missed=missed or None)


YT_PARKED = "YouTube is never automated in a browser"      # the 2026-10-09 hand-off rows' last_error


def yt_signed_in(s, account="youtube"):
    """Is there a refresh token for the channel (ytauth.py)? The demo channel's key is never for posts.
    A revoked grant stops counting: yt_access drops the dead refresh token the moment Google refuses it."""
    raw = s.key(account)
    try:
        return bool(json.loads(raw).get("refresh_token")) if raw else False
    except ValueError:
        return False


def yt_api_route(s, cfg, led, signed):
    """The #898 move, each tick: a parked browser hand-off (YT_PARKED's last_error) becomes the studio's
    own API upload, planned again at its own time, and a row parked by a revoked sign-in (YT_REVOKED's
    last_error) comes back the moment the key does; and until the owner signs in, ONE Board ask says so,
    never a hand-off per row. The ask is withdrawn the tick the key lands, or when nothing waits."""
    moved = []
    for v, a in s.assets(led):
        err = a.get("last_error") or ""
        if (a.get("platform") == "youtube" and a.get("state") == "handoff"
                and ((a.get("method") == "chrome" and err.startswith(YT_PARKED))
                     or (signed and a.get("method") == "api_native" and s.YT_REVOKED in err))):
            s.move(a, "planned", "requeue")
            a.update(method="api_native", attempts=0, next_try=None, last_error=None, block=None,
                     requeued_at=s.iso(s.now()))
            a.pop("claimed_at", None)
            moved.append(a["id"])
    if moved:
        s.log(led, f"{', '.join(moved)}: onto the YouTube API at its own time (issue #898)")
    mine = [x for x in s.open_asks(led, s.ASK_STUDIO) if x.get("key") == s.YT_SIGNIN["key"]]
    waiting = [a for _, a in s.assets(led) if a.get("platform") == "youtube" and a.get("method") == "api_native"
               and a.get("state") in ("planned", "handoff") and a.get("scheduled_for")]
    if signed or not waiting:
        for x in mine:
            s.ask_cmd(cfg, led, ["withdraw", x["id"], "signed in" if signed else "nothing waits on the YouTube API"])
        return
    blocks = f"{len(waiting)} YouTube upload{'s' if len(waiting) != 1 else ''} ({', '.join(a['id'] for a in waiting)})"
    if mine:
        mine[0]["blocks"] = blocks
        return
    s.ask_cmd(cfg, led, ["add", s.ASK_STUDIO, "--from", "publisher", "--what", s.YT_SIGNIN["what"], "--blocks", blocks,
                       "--about", s.YT_SIGNIN["about"], "--by",
                       min(s.parse(a["scheduled_for"]) for a in waiting).astimezone(s.tz(cfg)).strftime("%Y-%m-%d %H:%M")])
    led["asks"][-1]["key"] = s.YT_SIGNIN["key"]
