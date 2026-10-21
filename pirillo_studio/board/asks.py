"""open_asks, ask_recommendation, ask_title, asks_answered_today, ask_said, ask_line: moved from the host by dev/port.py; `s` is the host (pirillo_studio.host.View)."""
import contextlib
import datetime as dt
import os
import re
import sys


def open_asks(s, led, slug=None):
    return [a for a in (led.get("asks") or []) if isinstance(a, dict) and a.get("state") == "open"
            and (slug is None or a.get("slug") == slug)]


def ask_recommendation(s, a):
    r = a.get("recommendation") or {}
    if not isinstance(r, dict) or not r.get("label") or not r.get("why"):
        return ""
    return f"Recommended: {r['label']} | {r['why']}"


def ask_title(s, a):
    """An ask as the Board and the shell say it: what it is about first (its --about), then its question."""
    about = " ".join(str(a.get("about") or "").split()).rstrip(" .:")
    title = f"{about}: {a['what']}" if about else a["what"]
    return title + (". " + ask_recommendation(s, a) if ask_recommendation(s, a) else "")


def asks_answered_today(s, led, slug, cfg, t=None):
    """The asks of one video that the owner answered today (their zone), in the order they answered them."""
    z, out = s.tz(cfg), []
    today = (t or s.now()).astimezone(z).date()
    for a in led.get("asks") or []:
        if not isinstance(a, dict) or a.get("state") != "answered" or a.get("slug") != slug:
            continue
        try:
            at = s._when((a.get("answer") or {}).get("at"))
        except (ValueError, TypeError, AttributeError):
            continue
        if at.astimezone(z).date() == today:
            out.append((at, a))
    return [a for _, a in sorted(out, key=lambda x: x[0])]


def ask_said(s, a):
    """The owner's answer in a few words: the choice, then what they said."""
    ans = a.get("answer") or {}
    return " ".join(x for x in (ans.get("choice"), f"\"{ans['words']}\"" if ans.get("words") else None) if x)


def ask_line(s, a, cfg):
    by = ""
    if a.get("by"):
        try:
            by = " by " + s._when(a["by"]).astimezone(s.tz(cfg)).strftime("%a %b %-d, %-I:%M %p")
        except ValueError:
            by = f" by {a['by']}"
    opts = (" | options: " + " / ".join(o["label"] for o in a["options"])) if a.get("options") else ""
    state = {"open": "", "answered": f" [answered: {ask_said(s, a)}]",
             "withdrawn": f" [withdrawn: {a.get('why') or ''}]"}.get(a.get("state"), f" [{a.get('state')}]")
    return s._brief(f"{a['id']}  {a['slug']}  from {a['from']}{by}  {ask_title(s, dict(a, recommendation=None))} | waits on it: {a['blocks']}{opts}{state}", 420) + (" | " + ask_recommendation(s, a) if ask_recommendation(s, a) else "")


def ask_by(s, text, cfg):
    """--by as a time in his zone: a day (due by that day's morning run), a day and a time, or a time today."""
    value, z = str(text).strip(), s.tz(cfg)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return s.now().astimezone(z).replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}):(\d{2}))?", value)
    if m:
        d = dt.date.fromisoformat(m.group(1))
        hh, mm = (int(m.group(2)), int(m.group(3))) if m.group(2) else s.MORNING_RUN
        return dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=z)
    return s._when(value).astimezone(z)


def ask_find(s, led, aid):
    a = next((a for a in led.get("asks") or [] if isinstance(a, dict) and a.get("id") == aid), None)
    if not a: sys.exit(f"no ask {aid} (studio.py ask --all lists them); nothing was changed.")  # noqa: E701
    if a.get("state") != "open": sys.exit(f"ask {aid} is already {a.get('state')}; nothing was changed.")  # noqa: E701
    return a


def ask_images(s, ask):
    return list(dict.fromkeys(os.path.abspath(f if os.path.isabs(f) else os.path.join(s.HOME, f))
            for f in ask.get("files") or [] if str(f).lower().endswith((".png", ".jpg", ".jpeg", ".webp"))))


def answered_note(s, a):
    """The stage-log line of his answer: the question, what he chose, what he said, what it held up."""
    ans = a.get("answer") or {}
    opt = next((o for o in a.get("options") or [] if o["label"] == ans.get("choice")), None)
    said = []
    if opt:
        said.append("he chose " + opt["label"] + (f" ({opt['detail']})" if opt.get("detail") else ""))
    if ans.get("words"):
        said.append(("and said: " if opt else "he said: ") + ans["words"])
    return (f"{s.ANSWERED} (ask {a['id']}, filed by {a['from']}): \"{s.ask_title(dict(a, about=None))}\" -> {' '.join(said)}. "
            f"It held up: {a['blocks']}.")


def ask_echo(s, a):
    """His answer into that video's stage log as a chief note, so the seat that asked reads it at its next
    start. True when the line was written (an ask that belongs to no video has no stage log)."""
    import io
    slug = a.get("slug")
    if not slug or slug == s.ASK_STUDIO or not os.path.isdir(s.content_dir(slug)):
        return False
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            s.stage(slug, "chief", "note", answered_note(s, a))
    except (OSError, ValueError, SystemExit):
        return False
    return True


ASK_ISSUE = re.compile(r"#(\d+)")


def reasked_answer(s, led, slug, cfg, what, about, retry):
    """The earlier ask he answered that this question would ask again (same --about or issue, or today's retry
    task), newest answer first, with why in a few words, or (None, None)."""
    def flat(text):
        return " ".join(str(text or "").split()).rstrip(" .:").lower()
    mine = set(ASK_ISSUE.findall(f"{what} {about or ''}"))
    today = [id(a) for a in asks_answered_today(s, led, slug, cfg)]
    answered = [a for a in led.get("asks") or [] if isinstance(a, dict) and a.get("state") == "answered" and a.get("slug") == slug]
    for other in sorted(answered, key=lambda a: str((a.get("answer") or {}).get("at") or ""), reverse=True):
        if (other.get("answer") or {}).get("choice") == s.RETRY_LABEL or (other.get("acted") or {}).get("again"):
            continue                                # a deferral that stood its days asks again
        if about and flat(about) == flat(other.get("about")):
            return other, "the same question"
        if retry and retry == other.get("retry_task") and id(other) in today:
            return other, f"the {retry} ask"
        again = mine & set(ASK_ISSUE.findall(f"{other.get('what')} {other.get('about') or ''}"))
        if again:
            return other, f"issue #{sorted(again, key=int)[0]}"
    return None, None


def ask_answer(s, led, aid, choice=None, words="", via="chat", cfg=None, post=None, shown=None):
    """His answer on one open ask -> the ask. choice: an option's label, as the Board sends it. words: what he
    typed or said; with no choice given, words that are an option's label or number are that choice, and
    "<label>, <more>" is that choice with his words."""
    ask = ask_find(s, led, aid)                           # an ask, not a ledger row: MOVES has no say in its state
    handled = s.ask_answer_override(led, ask, choice, words, shown, cfg)
    if handled is not None:
        return handled
    images = s.ask_images(ask)
    raw_words = words or ""
    opts, words, picked = ask.get("options") or [], raw_words if images else " ".join(raw_words.split()), None

    def option(text):
        t = text.strip().lower()
        hit = next((o for o in opts if o["label"].lower() == t), None)
        return hit or (opts[int(t) - 1] if t.isdigit() and 1 <= int(t) <= len(opts) else None)
    if choice is not None:
        picked = option(choice)
        if not picked:
            sys.exit(f"ask {aid}: {choice!r} is not one of its options ({', '.join(o['label'] for o in opts) or 'it has none'}); "
                     "nothing was changed.")
    elif words:
        picked = option(words.rstrip('.!'))
        if picked:
            words = ""
        else:
            for o in sorted(opts, key=lambda o: -len(o["label"])):
                m = re.match(re.escape(o["label"]) + r"\s*[:,;.—-]\s*(.+)", words, re.I | re.S)
                if m:
                    picked, words = o, m.group(1).strip()
                    break
    if not picked and not words: sys.exit(f"ask {aid}: no answer given; nothing was changed.")  # noqa: E701
    if images:
        s.ask_answer_preflight(led, ask, images, picked, choice, raw_words)
    picked = picked or (option("Approve") if ask.get("answer_type") == "datetime-local" else None)   # a time alone is explicit approval for this answer type
    if s.decision_enabled(cfg):
        s.decision_prepare_ask(ask)
        by, source = s.decision_core.current_actor()
        if picked is None:
            # Free text is feedback, not permission for an effect.
            picked = next(o for o in ask["options"] if o["label"] == "Hold")
        try:
            s.decision_core.answer(ask, picked["label"], by=by, via=source, now=s.now())
        except ValueError as e:
            raise s.Refused(str(e)) from None
        ask["answer"]["words"] = words or None
        if not s.ask_owner_allowed(ask, picked["label"]):
            ask["state"] = "open"
            s.ask_answer_note(led, ask, via, by)
            return ask
        if picked["label"] == "Hold":
            return ask
    if images and s.ask_answer_hold(led, ask, picked, raw_words, via):
        return ask
    answered_at = s.ask_answer_effects(led, ask, picked, words, post, shown, raw_words, via, cfg, images)
    if not s.decision_enabled(cfg) and not s.ask_answer_managed(ask):
        ask.update(state="answered", answer=dict(choice=picked["label"] if picked else None, words=words or None,
                                                 at=answered_at, via=via))
    s.ask_answer_done(led, ask, picked, via)
    return ask
