"""Threads posts and their topic tags, using the caller's runtime."""


def threads_text(s, a, cfg, token):
    """A text post, with its image when the row has one; an image the readback does not confirm is a check."""
    path, alt = s.api_text_image(a, "threads", 8e6)
    words, topic, why = threads_topic(s, a, cfg, token)
    fields = {"media_type": "IMAGE" if path else "TEXT", "text": words}
    if path:
        fields["alt_text"] = alt
    if topic:
        fields["topic_tag"] = topic
    told = dict(topic=topic, topic_why=why, **({"missed": [f"topic: {why}"]} if not topic else {}))
    with (s.public_url(path) if path else s.contextlib.nullcontext(None)) as url:
        _, container = s._form("POST", f"{s.TH_API}/me/threads", token, dict(fields, **({"image_url": url} if path else {})))
        for _ in range(60):
            _, status = s._form("GET", f"{s.TH_API}/{container['id']}", token, {"fields": "status,error_message"})
            if status.get("status") == "FINISHED":
                break
            if status.get("status") in ("ERROR", "EXPIRED"):
                raise s.AdapterError("Threads couldn't process the post", False)
            s.time.sleep(5)
        else:
            raise s.AdapterError("Threads post still processing after 5 minutes")
        _, post = s.api_text_commit(s._form, "POST", f"{s.TH_API}/me/threads_publish", token, {"creation_id": container["id"]})
    image, note, meta = False, None, {}
    try:
        _, meta = s._form("GET", f"{s.TH_API}/{post['id']}", token, {"fields": "permalink,media_type,media_url"})
        if path:
            image = meta.get("media_type") == "IMAGE" and bool(meta.get("media_url"))
            if not image:
                note = "image not confirmed: Threads readback returned " + str(meta.get("media_type") or "no media type")
    except Exception:
        note = "image not confirmed: published post readback unavailable" if path else None
    missed = told.get("missed", []) + ([note] if note else [])
    return dict(told, post_id=post["id"], url=meta.get("permalink"), state="check" if note else "posted",
                image=image, image_note=note, missed=missed)


def threads_topic_api(s, name, token):
    """Ask the API whether a topic is live on Threads: keyword_search in TAG mode, recent posts. (evidence, how) when a
    post it returns carries exactly this topic_tag; None when it cannot say or finds none. A search hit alone is no
    proof: the app's standard access returns only the account's own posts, matched on their words (2026-10-22: "Meta AI"
    returned 4 posts, none tagged Meta AI)."""
    try:
        _, r = s._form("GET", f"{s.TH_API}/keyword_search", token, {"q": name, "search_mode": "TAG", "search_type": "RECENT",
                                                               "fields": "id,timestamp,topic_tag", "limit": "25"})
        got = [x for x in (r or {}).get("data") or [] if isinstance(x, dict)
               and " ".join(str(x.get("topic_tag") or "").split()).casefold() == " ".join(name.split()).casefold()]
    except Exception:                                   # a probe: it never stops a post
        return None
    if not got:
        return None
    newest = max(str(x.get("timestamp") or "") for x in got)[:10]
    return f"{len(got)} recent posts tagged it, the newest {newest or 'undated'}", "Threads API keyword_search, TAG mode"


def threads_topic(s, a, cfg, token=None, t=None):
    """(the words that show, the topic_tag to send or None, why) for a Threads post. Every "Topic:" line comes out of
    the words. The topic chain is the last Topic: line's, else the row's own `topic` (a story post's); the first of it
    seen on Threads, existing and active, goes (wordcopy.topic_pick). One not on record is asked of the API first."""
    import wordcopy
    words, chain = wordcopy.topic_split(a.get("text") or "")
    chain = chain or wordcopy.topic_chain(a.get("topic"))
    t = t or s.now()
    try:
        today = t.astimezone(s.tz(cfg)).date()
    except (KeyError, TypeError):
        today = t.date()
    book = wordcopy.topic_book(s.TOPICS)
    if token and chain and not wordcopy.topic_pick(chain, book, today)[0]:
        for name in chain:
            e = book.get(wordcopy.topic_key(name))
            if e and wordcopy._fresh(e, today, wordcopy.TOPIC_FRESH_DAYS):
                continue
            seen = threads_topic_api(s, name, token)
            if seen:
                try:
                    wordcopy.topic_note(s.TOPICS, name, True, seen[1], seen[0], s.iso(t))
                except (OSError, ValueError):
                    pass
                break
        book = wordcopy.topic_book(s.TOPICS)
    topic, why = wordcopy.topic_pick(chain, book, today)
    return words, topic, why


def threads(s, a, cfg, token):
    """Threads API: video by public URL, the topic tag as threads_topic picks it (never in the words), and the link
    as the first reply. A long cut, or a video over Threads' 5 minutes, goes as a link post to its YouTube URL.
    (No custom thumbnail: the Threads API has no cover or thumb_offset field.)"""
    if a.get("kind") == "article":
        return threads_text(s, a, cfg, token)
    words, topic, why = threads_topic(s, a, cfg, token)
    fields = {"media_type": "VIDEO", "text": words}
    if topic:
        fields["topic_tag"] = topic
    told = dict(topic=topic, topic_why=why, **({"missed": [f"topic: {why}"]} if not topic else {}))
    path = s.os.path.join(s.content_dir(a["slug"]), a["file"]) if a.get("file") else None
    secs = s._probe_secs(path) if path else None
    if a.get("kind") == "long" or (secs and secs > 300):
        if not a.get("long_url"):
            raise s.AdapterError("waits for the long cut's YouTube URL")
        fields = dict(fields, media_type="TEXT", text=f"{fields['text']}\n\n{a['long_url']}",
                      link_attachment=a["long_url"])
        _, c = s._form("POST", f"{s.TH_API}/me/threads", token, fields)
        s.time.sleep(5)
        _, p = s._form("POST", f"{s.TH_API}/me/threads_publish", token, {"creation_id": c["id"]})
        s.effect_checkpoint(dict(post_id=p["id"]))
        _, m = s._form("GET", f"{s.TH_API}/{p['id']}", token, {"fields": "permalink"})
        return dict(told, post_id=p["id"], url=m.get("permalink"), state="posted")
    with s.public_url(s.approval_check(a, path, cfg)) as url:
        _, c = s._form("POST", f"{s.TH_API}/me/threads", token, dict(fields, video_url=url))
        for _ in range(90):                           # up to 15 minutes
            _, st = s._form("GET", f"{s.TH_API}/{c['id']}", token, {"fields": "status,error_message"})
            if st.get("status") == "FINISHED":
                break
            if st.get("status") in ("ERROR", "EXPIRED"):
                raise s.AdapterError(f"Threads couldn't process the video: {st.get('error_message')}", False)
            s.time.sleep(10)
        else:
            raise s.AdapterError("Threads still processing after 15 minutes")
    _, p = s._form("POST", f"{s.TH_API}/me/threads_publish", token, {"creation_id": c["id"]})
    s.effect_checkpoint(dict(post_id=p["id"]))
    _, m = s._form("GET", f"{s.TH_API}/{p['id']}", token, {"fields": "permalink"})
    if a.get("reply_text"):                           # the link, as the first reply
        try:
            _, r = s._form("POST", f"{s.TH_API}/me/threads", token,
                         {"media_type": "TEXT", "text": a["reply_text"], "reply_to_id": p["id"]})
            s.time.sleep(5)
            s._form("POST", f"{s.TH_API}/me/threads_publish", token, {"creation_id": r["id"]})
        except s.AdapterError:
            pass                                      # the post is live; a missing reply is a verify job
    return dict(told, post_id=p["id"], url=m.get("permalink"), state="posted")
