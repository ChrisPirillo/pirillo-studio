"""One API send of a ledger row and the link plan that shapes its words, using the caller's runtime, ledger and
adapter table (the caller's ADAPTERS)."""

from pirillo_studio import buffer_api, facebook_posts, hold
import datetime as dt
import functools
import re
import unicodedata

# A platform here with "link": "same" in its config gets the long cut's link as the last line of a short's (or the
# full vertical's) own post; anywhere else "same" is no link at all. The link is added when the post is sent, never
# written into the copy, so the approved words stay as approved. Text posts never point back at the video.
LINK_SAME = ("bluesky", "mastodon")
LINK_NO_ROOM = "his words leave no room for the video link in the post"


def plain_post(text):
    """The Next 5 post shape as plain text: "# Headline" loses its #, "- **Claim.** more" becomes
    "• Claim. more", other ** and markdown links go (they'd post as symbols)."""
    out = []
    for ln in text.split("\n"):
        ln = re.sub(r"^#{1,6}\s+", "", ln)
        ln = re.sub(r"^\s*[-*]\s+", "• ", ln)
        ln = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", ln)
        out.append(ln.replace("**", "").replace("__", ""))
    return "\n".join(out).strip()


def no_tags(text):
    """A line of only hashtags goes; a tag inside a sentence keeps its word ("#AI" -> "AI")."""
    lines = [ln for ln in text.split("\n") if not re.fullmatch(r"\s*(#\w+\s*)+", ln)]
    out = re.sub(r"(?<![\w/&])#([A-Za-z]\w*)", r"\1", "\n".join(lines))
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def graphemes(text):
    """How many characters a reader sees, near enough for Bluesky's 300: a combining mark, a variation
    selector, a skin tone, a keycap, a tag, and whatever a zero-width joiner joins on, count with the character
    before them; two flag letters are one flag."""
    n, joined, flag = 0, False, False
    for ch in text:
        cp = ord(ch)
        if cp == 0x200D:
            joined = True
            continue
        if unicodedata.combining(ch) or 0xFE00 <= cp <= 0xFE0F or 0x1F3FB <= cp <= 0x1F3FF or cp == 0x20E3 \
                or 0xE0020 <= cp <= 0xE007F:
            continue
        if joined:
            joined = False
            continue
        if 0x1F1E6 <= cp <= 0x1F1FF and flag:
            flag = False
            continue
        flag = 0x1F1E6 <= cp <= 0x1F1FF
        n += 1
    return n


def post_len(platform, text):
    """A post's length as its platform counts it: graphemes on Bluesky; on Mastodon characters, any link as 23."""
    if platform == "mastodon":
        return len(re.sub(r"https?://\S+", "x" * 23, text))
    return graphemes(text)


def link_plan(s, v, a, cfg):
    """(text, reply, link, note) for one row: the words that go out, the link sent as a first reply (or None),
    the link the script adds as the post's own last line (or None), and why a row that should carry it in the
    post does not (or None). text is None while the words need a long cut's URL that is not there yet."""
    import wordcopy
    text, url = a.get("text") or "", s.long_url(v)
    p = cfg["platforms"][a["platform"]]
    if a.get("kind") == "article":             # LinkedIn, X and Facebook show markdown as symbols
        text = plain_post(text)
    if p.get("hashtags") is False:
        text = no_tags(text)
    needs_link = ("{long_url}" in text or (a.get("kind") != "long" and p.get("link") == "reply")
                  or (a.get("kind") == "short" and a["platform"] in ("youtube", "x"))
                  or (a.get("kind") in ("short", "vertical") and p.get("link") == "same" and a["platform"] in LINK_SAME))
    if needs_link and not url and (s.has_long(v) or "{long_url}" in text):
        return None, None, None, "Waits for the long cut's YouTube URL before its link can be attached"
    text = text.replace("{long_url}", url or "")
    if p.get("link") == "reply":
        return text, (url if a["kind"] != "long" else None), None, None
    if p.get("link") != "same" or a["platform"] not in LINK_SAME or a["kind"] not in ("short", "vertical") or not url \
            or not text.strip() or url in text:
        return text, None, None, None          # no link here, none to give yet, or the words already carry it
    both = text.rstrip() + "\n" + url
    if post_len(a["platform"], both) <= wordcopy.POST_MAX[a["platform"]]:
        return both, None, url, None
    # approved words are never trimmed and the post is never skipped: this one row goes without the link
    # in it, as the first reply where the platform's adapter sends one (Bluesky)
    reply = url if a["platform"] == "bluesky" else None
    return text, reply, None, LINK_NO_ROOM + (": it goes as the first reply" if reply else ": this one goes without it")


def render(s, v, a, cfg):
    """The words that go out: {long_url} filled in, no hashtags where the platform takes none (X), and on
    Bluesky and Mastodon the long cut's link as the last line (link_plan). Platforms whose link goes in the
    first reply get it as a separate reply_text for the adapter."""
    return s.link_plan(v, a, cfg)[:2]


def send(s, aid, cfg):
    """One API send, with the ledger lock held only around the row, never across the platform call.
    The row says 'sending' and is SAVED before the call, so a crash can never lead to a second
    post: the next tick finds 'sending' and asks for a check. The Sending lock is taken before the
    row says 'sending' and held until its result is written, so no other tick sends it too, and no
    tick calls a send that is still running a check."""
    s.only_installed("send"); cfg = hold.current(cfg, s.CONFIG, s.DEFAULTS)
    platform = aid.split(":")[-1]
    with s.Lock():
        pending = s.row(s.load(), aid)[1]
        if pending and pending.get("method") == "buffer" and s.method_for(cfg, platform, pending["kind"]) != "buffer":
            return
        account = pending["method"] if pending and pending.get("method") == "buffer" else platform
    token = s.key(account) if account == "buffer" or platform in s.ADAPTERS else None     # the Keychain, before the send lock
    slot = s.Sending(aid)
    try:
        with s.Lock():
            led = s.load()
            v, a = s.row(led, aid)
            if not a or not s.due(v, a, s.now(), cfg):
                return                          # changed since the tick looked: someone else has it
            why = s.image_approval_block(a, cfg, led) or s.approval_send_block(v, a, cfg, led)
            if why:
                a["block"] = why
                s.log(led, f"{aid}: held: {why}")
                print(f"{aid}: held: {why}", flush=True)
                s.save(led)
                return
            fn = functools.partial(buffer_api.buffer_post, s) if a["method"] == "buffer" else s.ADAPTERS.get(a["platform"])
            copy = a.get("text")
            text, reply, _, note = s.link_plan(v, a, cfg) if copy else (None, None, None, None)
            a.pop("link_note", None)
            if note:                            # the reason stays on the row
                a["link_note"] = note
                s.log(led, f"{aid}: {note}")
            a["block"] = (f"no {a['platform']} adapter yet (publisher builds it on first use)" if not fn else
                          f"no Keychain key: service {s.KEY_SERVICE}, account {account}" if not token else
                          "needs copy (packager)" if not copy else
                          "waits for the long cut's YouTube URL" if text is None else None)
            if a["block"] or not slot.acquire():
                s.save(led)
                return
            a.pop("posting_hold", None); s.move(a, "sending", "send")
            a["sent_at"] = s.iso(s.now())
            a["attempts"] += 1
            mine = (a["sent_at"], a["attempts"])
            job = dict(a, text=text, reply_text=reply, long_url=s.long_url(v), require_image=bool(s.outgoing_images(a)))
            if s.effects_enabled(cfg, a):
                job["_effect_source"] = s.effect_source(a)
            job = s.freeze_posting_images(job, cfg, led)   # every image field, not `image` alone
            if a.get("image"):
                s.record_scheduled_images(a, job["image"], fresh=True)
            s.log(led, f"{aid}: sending")
            s.save(led)
        r = err = lost = None
        try:
            with s.deadline(cfg.get("send_deadline_minutes", 120) * 60):
                if job.get("method") == "buffer" and (job.get("file") or job.get("image")):
                    s.media_host_short(aid, cfg, job)
                    with s.Lock():
                        current = s.row(s.load(), aid)[1]
                        if not current or current["state"] != "sending" or (current.get("sent_at"), current["attempts"]) != mine:
                            return
                r = s.effect_send(job, cfg, token, fn, aid)
        except s.AdapterError as e:
            err = e
        except (s.SendDeadline, Exception) as e:              # KeyboardInterrupt leaves 'sending' as it was
            lost = e
        finally:
            token = None
        with s.Lock():
            led = s.load()
            v, a = s.row(led, aid)
            ours = a["state"] == "sending" and (a.get("sent_at"), a["attempts"]) == mine
            if r is not None and not ours:            # it went, but the row moved meanwhile (a hand `done`)
                a.update(post_id=a.get("post_id") or r.get("post_id"), url=a.get("url") or r.get("url"),
                         last_error=f"the API posted while the row said {a['state']}")
                s.move(a, "check", "sent-late")
                s.log(led, f"{aid}: posted by the API after the row changed; check the platform for a double")
            elif r is not None:
                to = r.get("state", "posted")
                if not s.can_move(a, to, "send"):             # an adapter's answer the table doesn't know: it went
                    a["last_error"] = f"the adapter answered with a state nobody knows ({to!r})"[:300]
                    to = "check"                            # out, so someone looks; never sent again
                a.update(post_id=r.get("post_id"), url=r.get("url"))
                if s.effects_enabled(cfg, a):
                    a.update({k: r[k] for k in ("effect_receipt", "effect_alert", "effect_readback", "last_error") if k in r})
                if a["method"] == "buffer":
                    a.update(buffer_post_id=r.get("post_id"), buffer_url=r.get("buffer_url"))
                    if r.get("cover_text_sent") is not None:
                        a["cover_text_sent"] = r["cover_text_sent"]
                s.move(a, to, "send")
                if r.get("block"):                          # e.g. a TikTok draft finished in the app
                    a["block"] = r["block"]
                if r.get("facebook_confirm"):
                    facebook_posts.facebook_pending(s, a, cfg, True)
                elif facebook_posts.facebook_page_row(s, a):
                    a.pop("facebook_confirm", None)
                if a["state"] == "posted":
                    a["posted_at"] = s.iso(s.now())
                if r.get("cover"):
                    a["cover_sent"] = r["cover"]
                if "image" in r:                          # confirmed by the adapter
                    a["image_sent"] = bool(r["image"])
                if r.get("image_note"):
                    a["image_note"] = r["image_note"]
                if "topic_why" in r:                        # the Threads topic it went with, and why that one
                    a.update(topic_sent=r.get("topic"), topic_why=r["topic_why"])
                if r.get("missed"):                         # a cover that didn't go: the post did, say so
                    a["missed"] = r["missed"]
                s.log(led, f"{aid}: {a['state']}" + (f" (cover: {r['cover']})" if r.get("cover") else "")
                      + (f"; missed: {'; '.join(r['missed'])}" if r.get("missed") else ""))
            elif err is not None:
                a["last_error"] = s.approval_after(led, a, err)
                if ours:
                    if facebook_posts.facebook_page_row(s, a) and a.get("post_id"):
                        facebook_posts.facebook_pending(s, a, cfg, True)
                        s.move(a, "check", "send")
                    elif not err.retry or a["attempts"] >= cfg["max_attempts"]:
                        s.move(a, "handoff", "send")
                    else:
                        s.move(a, "planned", "send")
                        a["next_try"] = s.iso(s.now() + dt.timedelta(minutes=cfg["retry_minutes"]))
                s.log(led, f"{aid}: failed ({a['last_error'][:120]}) -> {a['state']}")
            else:                                   # stopped mid-call: it may have posted; never resend
                a["last_error"] = f"{type(lost).__name__}: {lost}"[:300]
                if ours:
                    s.move(a, "check", "send")
                s.log(led, f"{aid}: the send stopped mid-way ({a['last_error'][:120]}); check the platform")
            s.save(led)
        if job.get("method") == "buffer":
            buffer_api.buffer_host_cleanup(s)
        if lost is not None and not isinstance(lost, s.SendDeadline):
            raise lost                              # a bug in an adapter stays loud; the row is safe first
    finally:
        slot.release()
