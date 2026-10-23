"""Buffer GraphQL client using the caller's runtime."""


BUFFER_API = "https://api.buffer.com"
BUFFER_POST_FIELDS = "id status externalLink dueAt sentAt schedulingType channelId text"


def buffer_account(s, cfg, platform):
    """Select only the configured Keychain account; a missing mapped key never falls back."""
    account = cfg.get("platforms", {}).get(platform, {}).get("buffer_key_account", "buffer")
    if not isinstance(account, str) or not account.strip() or account != account.strip():
        raise s.AdapterError(f"Invalid Buffer key account for {platform}", False)
    return account


def buffer_graphql(s, query, variables, token, timeout=60, mutation=False):
    """Never expose response/error text that could echo credentials. An uncertain create is a check, not a retry."""
    if not token:
        raise s.AdapterError("no Buffer credential available for this route", False)
    try:
        _, result = s._http("POST", BUFFER_API, token,
                          s.json.dumps(dict(query=query, variables=variables)).encode(),
                          {"Content-Type": "application/json"}, timeout=timeout)
    except Exception:
        if mutation:
            raise RuntimeError("Buffer create outcome unknown; check Buffer before resending") from None
        raise s.AdapterError("Buffer read failed", True) from None
    if result.get("errors") or not isinstance(result.get("data"), dict):
        why = buffer_errors(s, result.get("errors"), token)
        if mutation:
            raise RuntimeError(f"Buffer create outcome unknown; GraphQL error{why}, check Buffer before resending")
        raise s.AdapterError(f"Buffer GraphQL read failed{why}", True)
    return result["data"]


def buffer_scrub(s, text, token):
    """Buffer's own words, minus the key and anything shaped like a credential (a long unbroken run)."""
    text = s.re.sub(r"\s+", " ", str(text or "")).strip()
    if token:
        text = text.replace(token, "[key]")
    return s.re.sub(r"[A-Za-z0-9_\-+/=.:]{24,}", "[redacted]", text)[:200]


def buffer_errors(s, errors, token):
    """' (message [code]; ...)' from GraphQL errors, scrubbed, so a failure names its cause; '' when there are none."""
    said = []
    for e in (errors if isinstance(errors, list) else [])[:3]:
        if not isinstance(e, dict):
            continue
        code = (e.get("extensions") or {}).get("code") if isinstance(e.get("extensions"), dict) else None
        part = buffer_scrub(s, e.get("message"), token) + (f" [{buffer_scrub(s, code, token)}]" if code else "")
        if part.strip():
            said.append(part.strip())
    return f" ({'; '.join(said)})" if said else ""


def buffer_channels(s, token):
    """Read all accessible organizations and channels; no mutations or account changes."""
    account = buffer_graphql(s, "query { account { organizations { id } } }", {}, token)
    out = []
    for org in account["account"]["organizations"]:
        data = buffer_graphql(s, "query($input: ChannelsInput!) { channels(input: $input) { "
                              "id service name isDisconnected isLocked isQueuePaused } }",
                              {"input": {"organizationId": org["id"]}}, token)
        out.extend(data["channels"])
    return out


def buffer_channel(s, cfg, token, platform="tiktok"):
    channels = [c for c in buffer_channels(s, token) if c["service"] == platform]
    wanted = cfg["platforms"][platform].get("buffer_channel_id")
    if wanted:
        channels = [c for c in channels if c["id"] == wanted]
    if len(channels) != 1:
        raise s.AdapterError(f"Buffer needs exactly one {platform} channel, or platforms.{platform}.buffer_channel_id", False)
    channel = channels[0]
    if any(channel.get(k) for k in ("isDisconnected", "isLocked", "isQueuePaused")):
        raise s.AdapterError(f"Buffer {platform} channel is disconnected, locked or paused", False)
    return channel["id"]


def buffer_route(s, a):
    """YouTube takes Shorts only; TikTok takes shorts and full verticals."""
    p, kind = a["platform"], a["kind"]
    if p not in ("youtube", "tiktok", "instagram", "threads") or p == "youtube" and kind != "short" \
            or p == "tiktok" and kind not in ("short", "vertical"):
        raise s.AdapterError("Buffer route does not support this post type", False)


def buffer_video(s, a, cfg):
    """Resolve native API media: the delivered file, fitted under its platform's API cap."""
    platform = a["platform"]
    buffer_route(s, a)
    if not a.get("file"):
        raise s.AdapterError("Buffer needs a current video", False)
    path = s.os.path.join(s.content_dir(a["slug"]), a["file"])
    if not s.os.path.isfile(path):
        raise s.AdapterError("Buffer needs a current delivered video", False)
    cap = s.video_fit_cap(cfg, platform)
    if cap:
        path = s.fit_video(path, cap, a["slug"])
    seconds = s.seconds(path)
    if not s.os.path.isfile(path) or not seconds or not 0 < seconds < float("inf"):
        raise s.AdapterError("Buffer needs a current measurable video", False)
    size = s.os.path.getsize(path)
    if not size:
        raise s.AdapterError("Buffer needs a current nonempty video", False)
    return dict(path=path, bytes=size, seconds=seconds, buffer=a.get("buffer") or {})


def buffer_post(s, a, cfg, token):
    """Exact native schedule of approved media. Its buffer object binds hosted bytes and cover frame.
    Buffer has no upload endpoint or cover-text field. Prepared media must stay hosted through publication.
    No quick tunnel: its lifetime ends before Buffer's scheduled fetch can be guaranteed.
    """
    platform = a["platform"]
    buffer_route(s, a)
    words = a.get("cover_text") or ""
    assets_ = []
    if a.get("file") or a.get("image"):
        video = bool(a.get("file"))
        fit = buffer_video(s, a, cfg) if video else None
        path = fit["path"] if fit else a.get("image")
        media = (fit or a).get("buffer") or {}
        if not path:
            raise s.AdapterError("Buffer needs a current fitted video", False)
        url = media.get("url") or ""
        parsed = s.urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise s.AdapterError("Buffer needs a durable public HTTPS media URL in its buffer record", False)
        h = s.hashlib.sha256()
        with (s.approval_opened(a, path, cfg) if video else open(path, "rb")) as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        if media.get("sha256") != h.hexdigest():
            raise s.AdapterError("Buffer hosted media record does not match the fitted file", False)
        asset = {"url": url}
        if video and platform in ("tiktok", "instagram"):
            offset = media.get("thumbnail_offset_ms")
            if (not isinstance(offset, int) or isinstance(offset, bool) or offset < 0
                    or offset >= fit["seconds"] * 1000):
                raise s.AdapterError("Buffer needs a valid thumbnail_offset_ms in its fitted video", False)
            if s.lacks_cover_words(a) or media.get("cover_text") != words:
                raise s.AdapterError("Buffer cannot add cover words: prepare and verify those words in the selected video frame", False)
            asset["metadata"] = {"thumbnailOffset": offset}
        if video and platform == "youtube":
            seconds, width, height = s.video_probe(path)
            if seconds > 180 or width >= height:
                raise s.AdapterError("Buffer YouTube Shorts require vertical video of at most three minutes", False)
        assets_.append({"video" if video else "image": asset})
    elif platform != "threads":
        raise s.AdapterError("Buffer requires media for this platform; Yours to post until media is prepared", False)
    when = s.parse(a.get("scheduled_for") or a.get("when"))
    if not when or when <= s.now() + s.dt.timedelta(minutes=6):
        raise s.AdapterError("Buffer slot must be more than six minutes ahead", False)
    channel_id = buffer_channel(s, cfg, token, platform)
    inp = dict(channelId=channel_id, text=a["text"], schedulingType="automatic", mode="customScheduled",
               dueAt=when.astimezone(s.dt.timezone.utc).isoformat(timespec="seconds"), needsApproval=False,
               assets=assets_)
    if platform == "youtube":
        inp["metadata"] = {"youtube": {"title": a["title"], "categoryId": "28", "privacy": "public", "madeForKids": False}}
    elif platform == "instagram":
        inp["metadata"] = {"instagram": {"type": "reel" if a.get("file") else "post", "shouldShareToFeed": True}}
    elif platform == "threads" and a.get("reply_text"):
        inp["metadata"] = {"threads": {"thread": [{"text": a["text"], "assets": assets_}, {"text": a["reply_text"]}]}}
    answer = buffer_graphql(s, "mutation($input: CreatePostInput!) { createPost(input: $input) { "
                            "... on PostActionSuccess { post { " + BUFFER_POST_FIELDS + " } } "
                            "... on MutationError { message } } }", {"input": inp}, token, mutation=True)
    result = answer.get("createPost") or {}
    post = result.get("post")
    if not post:
        why = buffer_scrub(s, result.get("message"), token)
        raise s.AdapterError("Buffer rejected createPost" + (f" ({why})" if why else "")
                           + "; review channel and media settings", False)
    if not post.get("id"):
        raise RuntimeError("Buffer create returned no id; check Buffer before resending")
    # Never confuse Buffer's management URL with a public TikTok permalink.
    try:
        accepted_at = s.parse((post.get("dueAt") or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        accepted_at = None
    ok = (post.get("status") == "scheduled" and post.get("schedulingType") == "automatic"
          and post.get("channelId") == channel_id and accepted_at == when)
    return dict(state="scheduled" if ok else "check", post_id=post["id"], url=post.get("externalLink"),
                buffer_url="https://publish.buffer.com", cover_text_sent=words if ok else None, image=bool(a.get("image")))


def buffer_read_post(s, post_id, token, metrics=False):
    fields = BUFFER_POST_FIELDS + (" metrics { type value unit } metricsUpdatedAt" if metrics else "")
    return buffer_graphql(s, "query($input: PostInput!) { post(input: $input) { " + fields + " } }",
                          {"input": {"id": post_id}}, token, timeout=s.counts.COUNT_TIMEOUT)["post"]


def buffer_host_cleanup(s):
    """Cleanup is independent of the upload switch; an old accepted schedule still owns its media."""
    s.media_cleanup()
