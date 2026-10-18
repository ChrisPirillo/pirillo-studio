"""Mastodon edits and schedule changes, using the caller's runtime."""


def mastodon_replace_due(s, a, t):
    """An api_native Mastodon row with replacement words pending whose scheduled time has passed."""
    return (a["platform"] == "mastodon" and a["method"] == "api_native" and a["state"] == "scheduled"
            and a.get("replace") and a.get("scheduled_for") and s.parse(a["scheduled_for"]) + s.MASTODON_SETTLE <= t)


def mastodon_replace(s, a, cfg, token):
    """Replacement words for a Mastodon post that was scheduled through the API. The web app has no
    scheduled-post screen and the API can't change a scheduled status's text, so the edit waits until
    the status has published, then PUT /api/v1/statuses/:id changes the words (media kept; an edit
    cannot change visibility, so it stays). The status is found among the account's recent ones by
    created time near scheduled_for and by the opening words of the old text. Returns
    dict(state="posted", post_id, url, posted_at) when done, or dict(state="check", reason) on any
    doubt (none or two matches): the post is then left exactly as it is. Never deletes, never posts."""
    inst = cfg["platforms"]["mastodon"].get("instance")
    if not inst:
        raise s.AdapterError("_studio.json: set platforms.mastodon.instance (e.g. https://mastodon.social)", False)
    inst = inst if inst.startswith("http") else f"https://{inst}"
    _, me = s._http("GET", f"{inst}/api/v1/accounts/verify_credentials", token)
    _, recent = s._http("GET", f"{inst}/api/v1/accounts/{me['id']}/statuses?limit=40", token)
    when = s.parse(a["scheduled_for"])
    want = s._opening(a.get("old_text") or a.get("text"))
    new = (a.get("text") or "").strip()
    hits = []
    for st in recent if isinstance(recent, list) else []:
        made = s.dt.datetime.fromisoformat(st["created_at"].replace("Z", "+00:00"))
        if abs(made - when) <= s.MASTODON_WINDOW and s._opening(s.plain_html(st.get("content"))) == want:
            hits.append(st)
    if len(hits) != 1:
        return dict(state="check", reason=f"{len(hits)} published Mastodon statuses match the scheduled one "
                                          f"(time within {s.MASTODON_WINDOW.seconds // 60} min of {a['scheduled_for']} "
                                          f"and the old opening words); left as it is")
    st = hits[0]
    s.effect_checkpoint(dict(post_id=st["id"], url=st.get("url") or st.get("uri")))
    if s.plain_html(st.get("content")) != new:
        data = s.urllib.parse.urlencode([("status", new)] + [("media_ids[]", m["id"]) for m in st.get("media_attachments", [])]
                                      + [("spoiler_text", st.get("spoiler_text") or ""),
                                         ("sensitive", "true" if st.get("sensitive") else "false")]).encode()
        _, st = s._http("PUT", f"{inst}/api/v1/statuses/{st['id']}", token, data,
                      {"Content-Type": "application/x-www-form-urlencoded"})
    return dict(state="posted", post_id=st["id"], url=st.get("url") or st.get("uri"),
                posted_at=s.iso(s.dt.datetime.fromisoformat(st["created_at"].replace("Z", "+00:00"))))


def mastodon_retime(s, a, cfg, token, when):
    """PUT /api/v1/scheduled_statuses/:id with scheduled_at: the status keeps its words and media, only its time
    changes. Returns the time the instance now holds, read off its answer (the PUT's own answer is the read-back)."""
    inst = cfg["platforms"]["mastodon"].get("instance")
    if not inst:
        raise s.AdapterError("_studio.json: set platforms.mastodon.instance (e.g. https://mastodon.social)", False)
    inst = inst if inst.startswith("http") else f"https://{inst}"
    at_ = when.astimezone(s.dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _, r = s._http("PUT", f"{inst}/api/v1/scheduled_statuses/{a['post_id']}", token,
                 s.urllib.parse.urlencode([("scheduled_at", at_)]).encode(),
                 {"Content-Type": "application/x-www-form-urlencoded"})
    got = (r or {}).get("scheduled_at") if isinstance(r, dict) else None
    if not got:
        raise s.AdapterError(f"Mastodon answered without a scheduled_at: {str(r)[:160]}", False)
    return s.dt.datetime.fromisoformat(got.replace("Z", "+00:00"))
