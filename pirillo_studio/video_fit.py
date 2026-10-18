"""video_fit_cap, video_fit_media, prepare_video_fit: moved from the host by dev/port.py; `s` is the host (pirillo_studio.host.View)."""
import os


def video_fit_cap(s, cfg, platform, caps=None):
    if platform != "mastodon":
        return s.API_VIDEO_BYTES.get(platform)
    if caps is not None and platform in caps:
        if isinstance(caps[platform], Exception):
            raise caps[platform]
        return caps[platform]
    inst = cfg["platforms"][platform].get("instance")
    if not inst:
        return None
    inst = inst if inst.startswith("http") else "https://" + inst
    try:
        _, info = s._http("GET", f"{inst}/api/v2/instance", None, timeout=5)
    except Exception as error:
        if caps is not None:
            caps[platform] = error       # one failed lookup per release/tick; the next tick gets a fresh cache
        raise
    limit = info.get("configuration", {}).get("media_attachments", {}).get("video_size_limit")
    cap = limit * 0.95 if limit else None
    if caps is not None:
        caps[platform] = cap
    return cap


def video_fit_media(s, a):
    """Only delivered video files enter fitting; an image marker overrides a video-looking name."""
    return (a.get("kind") != "article" and not a.get("image") and
            os.path.splitext(a.get("file") or "")[1].lower() in (".mp4", ".mov", ".m4v", ".webm", ".mkv"))


def prepare_video_fit(s, cfg, a, caps=None):
    """Start local preparation before approval/slot time; never upload or consume an attempt.
    Returns a pending/failure reason, otherwise None. Preparation cannot interrupt the posting clock."""
    try:
        if (not video_fit_media(s, a) or a.get("state") != "planned"
                or a.get("method") not in ("api_now", "api_native", "buffer")
                or s.too_long(a.get("kind"), a["platform"], a.get("seconds"), cfg)):
            return None
        cap = video_fit_cap(s, cfg, a["platform"], caps)
        if cap:
            s.fit_video(os.path.join(s.content_dir(a["slug"]), a["file"]), cap, a["slug"], background=True)
    except Exception as e:                  # optional preparation, including malformed config or a failed lookup
        return str(e) or type(e).__name__
    return None
