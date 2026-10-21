"""sync_image_rejections, ask_acted, ask_add, check_acted: moved from the host by dev/port.py; `s` is the host (pirillo_studio.host.View)."""
import fcntl
import json
import os
import re
import secrets
import sys

from . import asks


def sync_image_rejections(s, led):
    """Deliver committed answer events to the append-only log, idempotently.

    The answer, rejection event and redo share one atomic ledger write. If delivery
    is interrupted after that commit, load() retries it before returning the ledger.
    The normal Studio lock serializes writers; this mirror is not another owner.
    """
    events = led.get("image_rejection_events") or []
    if not events:
        return
    path = os.path.join(s.DATA_DIR, "image-rejections.jsonl")
    os.makedirs(s.DATA_DIR, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        content = f.read()
        # A crash mid-append can leave only the final row incomplete. Truncate
        # that fragment and redeliver it from the authoritative ledger.
        if content and not content.endswith("\n"):
            end = content.rfind("\n") + 1
            try:
                json.loads(content[end:])
            except ValueError:
                f.seek(0)
                f.truncate(len(content[:end].encode("utf-8")))
                content = content[:end]
            else:
                f.write("\n")
                content += "\n"
        # Chat-sourced rows (the owner's words logged by hand) carry no ask; they are kept and never keyed.
        existing = {(r.get("ask"), r.get("image")) for r in (json.loads(line) for line in content.splitlines() if line.strip())}
        f.seek(0, os.SEEK_END)
        for row in events:
            key = row["ask"], row["image"]
            if key not in existing:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                existing.add(key)
        f.flush()
        os.fsync(f.fileno())


def ask_acted(s, cfg, led, rest):
    why = " ".join(rest[2:]).strip()
    if len(rest) < 3 or len(why) < 8:
        sys.exit("ask acted <id> <why>: say in a few words why it needs no run, or what was started; nothing was changed.\n" + s.ASK_USAGE)
    ask = next((a for a in led.get("asks") or [] if isinstance(a, dict) and a.get("id") == rest[1]), None)
    if not ask:
        sys.exit(f"ask acted: there is no ask {rest[1]!r} (studio.py ask --all); nothing was changed.")
    check_acted(ask, why)   # answered, and an option named when he answered one in words
    ask["acted"] = dict(at=s.iso(s.now()), what=why)
    s.log(led, f"ask {ask['id']} ({ask['slug']}) acted on: {s._brief(why, 100)}")
    return [f"ask {ask['id']}: acted on ({s._brief(why, 100)}); {len(s.answers_waiting(led))} answered and waiting on a run"], True, None


def ask_add(s, cfg, led, rest):
    args, slug, one, many = list(rest[1:]), None, {}, {"--option": [], "--doc": [], "--file": []}
    while args:
        x = args.pop(0)
        if x in ("--from", "--what", "--blocks", "--by", "--about", "--recommend", "--retry-task", "--steps") and args:
            one[x] = args.pop(0)
        elif x in many and args:
            many[x].append(args.pop(0))
        elif slug is None and not x.startswith("--"):
            slug = x
        else:
            sys.exit(f"ask add: {x!r} is not part of an ask; nothing was filed.\n{s.ASK_USAGE}")

    def no(why):
        sys.exit(f"ask add: {why}; nothing was filed.\n{s.ASK_USAGE}")
    if not slug or not (slug == s.ASK_STUDIO or slug in led["videos"] or os.path.isdir(s.content_dir(slug))):
        no(f"{slug!r} is no video folder (use `{s.ASK_STUDIO}` for an ask that belongs to no video)")
    who = (one.get("--from") or "").strip()
    if who not in s.CREW + ("chief",):
        no(f"--from is the seat that asks ({', '.join(s.CREW + ('chief',))})")
    what, blocks = " ".join((one.get("--what") or "").split()), " ".join((one.get("--blocks") or "").split())
    if why := s.ask_question_refusal(what): no(why)  # noqa: E701
    if len(blocks) < 6: no("--blocks says what waits on his answer")  # noqa: E701
    s.ask_add_policy(cfg, led, slug, what, many, no)
    options = []
    for o in many["--option"]:
        label, _, detail = o.partition(" | ")
        label, detail = " ".join(label.split()), " ".join(detail.split())
        if not label or label.lower() in [x["label"].lower() for x in options]:
            no(f"--option {o!r} has no label, or its label is there twice")
        options.append(dict(label=label, detail=detail or None))
    if len(options) > s.ASK_OPTIONS:
        no(f"an ask offers at most {s.ASK_OPTIONS} options")
    recommendation = None
    if s.dispatch_repairs(cfg) or "--recommend" in one:
        label, sep, why = (one.get("--recommend") or "").partition(" | ")
        label, why = " ".join(label.split()), " ".join(why.split())
        if not sep or not label or len(why) < 12:
            no("--recommend is '<label or action> | <why>', with enough context to decide")
        if options and label not in [o["label"] for o in options]:
            no("--recommend label must be one of the --option labels")
        recommendation = dict(label=label, why=why)
    docs, files = [], []
    for d in many["--doc"]:
        real = s.studio_file(d, md=True)
        if not real:
            no(f"--doc {d!r} is not a .md file inside the studio folder")
        docs.append(s.studio_rel(real))
    for f in many["--file"]:
        real = s.studio_file(f, folder=True)
        if not real:
            no(f"--file {f!r} is not a file or folder inside the studio folder")
        files.append(s.studio_rel(real))
    by = None
    if one.get("--by"):
        try:
            by = s.iso(asks.ask_by(s, one["--by"], cfg))
        except ValueError:
            no(f"--by is YYYY-MM-DD, \"YYYY-MM-DD HH:MM\" or HH:MM, not {one['--by']!r}")
    if any(a["what"].lower() == what.lower() for a in asks.open_asks(s, led, slug)):
        no("that question is already open for this video (studio.py ask)")
    sent = s.ask_platform_unlinked(what, blocks, options) if not one.get("--steps") else None
    if sent:
        no(f"it sends him to act on {sent} and carries no link: put the direct URL of each thing he acts on (the ledger "
           "row's url first), else at least its page, in --what or an option's detail (row qc.handoff.ask-link)")
    about = " ".join((one.get("--about") or "").split()).rstrip(" .:")
    if len(about) > s.ASK_ABOUT or (about and about.rstrip("?!").lower() == what.rstrip(" .:?!").lower()):
        no(f"--about is what the ask is about in a few words ({s.ASK_ABOUT} characters at most), not the question again")
    if slug != s.ASK_STUDIO:                         # a second ask on a video the same day says what it is about, in its own words
        for other, how in [(x, "still open") for x in asks.open_asks(s, led, slug)] + [(x, "he answered it today") for x in asks.asks_answered_today(s, led, slug, cfg)]:
            if not about or about.lower() == " ".join(str(other.get("about") or "").split()).rstrip(" .:").lower():
                no(f"this video has another ask today (\"{s._brief(asks.ask_title(s, other), 90)}\", {how}), and the Board shows the two side by side: "
                   "say what this one is about in a few words of its own, --about \"<the post and its platforms>\", so it "
                   "does not read as the same question back")
    retry = one.get("--retry-task")
    if retry is not None and (retry not in s.RETRY_TASKS or s.RETRY_LABEL not in [o["label"] for o in options]):
        no(f"--retry-task is one of {', '.join(s.RETRY_TASKS)} and needs an option labelled {s.RETRY_LABEL!r}")
    other, topic = asks.reasked_answer(s, led, slug, cfg, what, about, retry)
    if other:
        no(f"he already answered {topic} (\"{s._brief(asks.ask_title(s, other), 90)}\", at "
           f"{s._at((other.get('answer') or {}).get('at'), cfg)}): {asks.ask_said(s, other)}. "
           "Act on that answer; never put the question to him again")
    a = dict(id=secrets.token_hex(4), slug=slug, what=what, blocks=blocks, options=options, docs=docs, files=files,
             by=by, at=s.iso(s.now()), state="open", **({"about": about} if about else {}))
    try:
        s.ask_attach_steps(a, one.get("--steps"), cfg, led)
    except (OSError, ValueError, UnicodeError) as e:
        no(str(e))
    a["from"] = who
    if retry:
        a["retry_task"] = retry
    if recommendation:
        a["recommendation"] = recommendation
    if s.decision_enabled(cfg):
        s.decision_prepare_ask(a)
    led.setdefault("asks", []).append(a)
    s.log(led, f"ask {a['id']} ({slug}, from {who}): {s._brief(what, 100)}"
        + (" | " + asks.ask_recommendation(s, a) if asks.ask_recommendation(s, a) else ""))
    return [f"filed: ask {a['id']} is in the Board's Your turn until he answers ({len(asks.open_asks(s, led))} open)"], True, None


def check_acted(ask, why):
    """Refuse unless answered; free text on option asks must name the action actually carried out."""
    if ask.get("state") != "answered":
        sys.exit(f"ask {ask['id']} is {ask.get('state')}, not answered: there is no answer to act on; nothing was changed.")
    labels = [o["label"] for o in ask.get("options") or []]
    if labels and not (ask.get("answer") or {}).get("choice") and not any(re.match(rf"\W*{re.escape(l)}(?!\w)", why, re.I) for l in labels):
        sys.exit(f"ask {ask['id']}: he answered in words and picked none of {', '.join(labels)}; start <why> with the option carried out ('<option>: what ran'), "
                 "or file a new ask with what changed; nothing was changed.")
