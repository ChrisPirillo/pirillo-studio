"""Kept words to timed cues, one record per rendered output (docs/captions.md sections 1 to 3).
The host passes its video adapter to all three calls, cues(video, output), sidecar(video, output) and
check(video, output) (the spec's sidecar(record) and check(record)), as cutting's check(video) takes it.
Lead decision 1: per output (arc, draft, long, vertical, short-<id>) it reads records/words.json,
records/cut_plan2.json and its patch records, records/render/<output>-voice.wav, and of
records/render/<output>.json only mode, duration_s, frame_map ([{span, src, out}], one rate per piece, each boundary
a cut) and elements ([{start_s, end_s, box, what}]); item 14 must write those three fields in this shape.
Lead decision 2: kept words are each frame-map piece's span words whose midpoint lies inside its src (a patch span's
are all its patch words); an occurrence is (piece, word id), and a kept `unsaid` word refuses, routed to item 17.
Lead decision 3: an owner-made short (mode measure_existing) reads records/words-<id>.json on the file's own clock,
has no plan and no burn, and job `burn: "supplied"` skips placement; the supplied media is never read or written.
Lead decision 4: the adapter supplies versions(names), align(voice, words, model), onsets(voice) and
width(text, font); split tokens are joined first, and each joined word goes to align as {id, text, said} with id
its first part's occurrence "<piece>:<word id>", since a cold open repeats word IDs.
LookupError from any, or a missing voice artifact, is `unavailable`; no transcript times.
Lead decision 5: a `held` word keeps its own time mapped through its piece; any other takes the aligner's; a kept
word not aligned, or aligned out of order, refuses naming it; a cue outside its piece refuses (layout).
Lead decision 8: cues writes the burn layer (tall rendered outputs only) then the record, each beside then renamed;
sidecar writes the record's .srt only from a current captioned record; check writes nothing.
Lead decision 9: key = digest of the output, every input's content hash, the resolved profile, safe_tall, frame,
the aligner model and version, the font version and this package's code hash; an equal captioned key is reused.
Lead decision 10: every value is in settings.json (captions.settings)."""
import math
import os
import re
import tempfile
from pathlib import Path

from ..cutting import plan as io
from ..spine.engine import writer
from ..spine.files import SpineError, canon, digest, filehash, load
from . import layout, said

OUTPUT = re.compile(r"arc|draft|long|vertical|short-([a-z0-9][a-z0-9_-]*)")
SIDECARS = {"arc": "Draft/arc.srt", "draft": "Draft/draft.srt", "long": "YouTube/main.srt",
            "vertical": "Vertical/main.srt"}


def settings():
    return load(Path(__file__).parent, "settings.json")


def _code():
    return digest(canon({p.name: filehash(p) for p in sorted(Path(__file__).parent.glob("*.py"))
                         if not p.name.startswith("test_")}))


def _field(record, key, at):
    if not isinstance(record, dict) or key not in record:
        raise SpineError(f"{at}: missing {key}")
    return record[key]


def _pieces(render, at, duration):
    rows = _field(render, "frame_map", at)
    if not isinstance(rows, list) or not rows:
        raise SpineError(f"{at}: frame_map must list pieces")
    pieces, end = [], 0
    for i, row in enumerate(rows):
        where = f"{at} frame_map[{i}]"
        span = io.text(_field(row, "span", where), where)
        a, b = io.interval(_field(row, "src", where), where)
        c, d = io.interval(_field(row, "out", where), where)
        if not math.isclose(b - a, d - c, abs_tol=1e-6) or c < end - 1e-6 or d > duration + 1e-6:
            raise SpineError(f"{where}: src and out differ in length, or out is out of order or past duration_s")
        pieces.append({"span": span, "src": [a, b], "out": [c, d]})
        end = d
    return pieces


def _elements(render, at):
    rows = _field(render, "elements", at)
    if not isinstance(rows, list):
        raise SpineError(f"{at}: elements must be a list")
    for i, row in enumerate(rows):
        where = f"{at} elements[{i}]"
        io.interval([_field(row, "start_s", where), _field(row, "end_s", where)], where)
        box = _field(row, "box", where)
        if not isinstance(box, list) or len(box) != 4 or min([io.number(x, where) for x in box][2:]) <= 0:
            raise SpineError(f"{where}: box must be [x, y, w, h]")
        io.text(_field(row, "what", where), where)
    return rows


def _inputs(video, output):
    """Everything one call reads, validated, with its key; missing names what makes the record unavailable."""
    match = OUTPUT.fullmatch(output) if isinstance(output, str) else None
    if match is None:
        raise SpineError(f"{output!r}: not an output id")
    short, s = match.group(1), settings()
    name = "tall" if output == "vertical" or short else "wide"
    at, voice = f"records/render/{output}.json", f"records/render/{output}-voice.wav"
    reads = io.hashes(video, [at])
    render = io.read(video, at)
    mode = _field(render, "mode", at)
    if mode not in ("render", "measure_existing") or (mode == "measure_existing" and not short):
        raise SpineError(f"{at}: mode {mode!r} is not render, or measure_existing for a short")
    duration = io.number(_field(render, "duration_s", at), f"{at} duration_s")
    if duration <= 0:
        raise SpineError(f"{at}: duration_s must be positive")
    owner, supplied = mode == "measure_existing", False
    words_name = f"records/words-{short}.json" if owner else "records/words.json"
    reads.update(io.hashes(video, [words_name, "records/job.json" if owner else "records/cut_plan2.json"]))
    rows = io.words(io.read(video, words_name))
    if owner:
        job = io.read(video, "records/job.json")
        burn = job.get("burn") if isinstance(job, dict) else []  # absent: the owner declared no burn of his own
        if burn not in (None, "supplied"):
            raise SpineError(f"records/job.json: burn {burn!r} is not \"supplied\"")
        supplied = burn == "supplied"
        pieces = [{"span": None, "src": [0, duration], "out": [0, duration]}]
        kept = [(0, w) for w in rows if "unsaid" not in w.get("flags", [])]
    else:
        plan = io.validate_plan(io.read(video, "records/cut_plan2.json"), rows)
        spans, pieces, kept = {x["id"]: x for x in plan["spans"]}, _pieces(render, at, duration), []
        for i, piece in enumerate(pieces):
            span = spans.get(piece["span"])
            if span is None:
                raise SpineError(f"{at} frame_map[{i}]: span {piece['span']} is not in records/cut_plan2.json")
            if "patch" in span:
                patch = span["patch"]["record"]
                reads.update(io.hashes(video, [patch]))
                if reads[patch] != span["patch"]["sha"]:
                    raise SpineError(f"{patch}: differs from the plan's patch hash")
                kept += [(i, w) for w in io.selected(io.words(io.read(video, patch)), span["patch"]["words"])]
            elif span["words"]:
                a, b = piece["src"]
                kept += [(i, w) for w in io.selected(rows, span["words"]) if a <= (w["start"] + w["end"]) / 2 < b]
    if not kept:
        raise SpineError(f"{output}: no kept words to caption")
    measured = name == "tall" and not supplied
    context = {"output": output, "name": name, "settings": s, "reads": reads, "voice": voice, "owner": owner,
               "words": words_name, "pieces": pieces, "kept": kept, "duration": duration, "measured": measured,
               "elements": _elements(render, at) if measured else None, "missing": None, "versions": {}}
    context["voice_sha256"] = filehash(io.path(video, voice))
    model, font = s["aligner"]["model"], s["tall"]["font"] if measured else None
    if context["voice_sha256"] is None:
        context["missing"] = voice
    else:
        try:
            got = video.versions([model] + ([font] if font else []))
        except LookupError as error:
            context["missing"] = str(error.args[0]) if error.args else "versions"
        else:
            if any(not isinstance(got.get(n), str) for n in [model] + ([font] if font else [])):
                raise SpineError(f"versions: no hash for {model} or {font}")
            context["versions"] = got
    render_identity = digest(canon({"mode": mode, "duration_s": duration, "frame_map": pieces,
                                    "elements": context["elements"]}))
    context["key"] = digest(canon({
        "output": output, "reads": {**reads, at: render_identity}, "voice": context["voice_sha256"], "missing": context["missing"],
        "settings": {"profile": name, name: s[name], "safe_tall": s["safe_tall"], "frame": s["frame"]},
        "aligner": {"model": model, "version": context["versions"].get(model)},
        "font": context["versions"].get(font), "code": _code()}))
    return context


def _record(c, state, cues=None, refusals=(), uncaptioned=(), held=(), readings=None, burn=None, sidecar=None):
    model = c["settings"]["aligner"]["model"]
    return {"output": c["output"], "key": c["key"], "words": c["words"], "words_sha256": c["reads"][c["words"]],
            "kept": None if c["owner"] else "records/cut_plan2.json#spans",
            "plan_sha256": None if c["owner"] else c["reads"]["records/cut_plan2.json"],
            "clock": None if c["owner"] else f"records/render/{c['output']}.json#frame_map",
            "frame_map_sha256": digest(canon(c["pieces"])),
            "voice": c["voice"], "voice_sha256": c["voice_sha256"], "settings": f"captions.settings#{c['name']}",
            "aligner": {"model": model, "version": c["versions"].get(model)}, "cues": cues,
            "held": list(held), "uncaptioned": list(uncaptioned), "burn": burn, "sidecar": sidecar,
            "readings": readings, "state": state, "refusals": list(refusals), "missing": c["missing"]}


def cs(t, inward):
    """A time to the centisecond, rounded toward the inside of its word so a cue never crosses a boundary."""
    return inward(round(t * 100, 6)) / 100


def _times(video, c, model):
    """Split tokens joined, then aligned as said, on the output clock; or the refusals that stop grouping."""
    pieces, held = c["pieces"], {(i, w["id"]) for i, w in c["kept"] if "held" in w.get("flags", [])}
    words = said.join([{"ids": [w["id"]], "text": w["text"], "start": w["start"], "end": w["end"], "piece": i}
                       for i, w in c["kept"]])
    asked = [{"id": f"{w['piece']}:{w['ids'][0]}", "text": w["text"], "said": said.said(w["text"])} for w in words]
    got = video.align(io.path(video, c["voice"]), asked, model)
    aligned, unmatched = _field(got, "words", "align"), _field(got, "unmatched", "align")
    if not isinstance(aligned, dict) or not isinstance(unmatched, list):
        raise SpineError("align: words must map ids and unmatched must list intervals")
    refusals, missed, tokens, last = [], [], [], 0
    for heard in unmatched:
        a, b = io.interval(heard, "align unmatched")
        span = next((p["span"] for p in pieces if p["out"][0] < b and p["out"][1] > a), None)
        refusals.append(layout.refusal(None, "unmatched_speech", {"heard": [a, b], "span": span},
                                       "records/cut_plan2.json#spans", "item 17"))
    for word, ask in zip(words, asked):
        piece = pieces[word["piece"]]
        own = [(word["piece"], w) in held for w in (word["ids"][0], word["ids"][-1])]
        if all(own) or ask["id"] in aligned:  # a held first or last part keeps its own edge; the rest is aligned
            a, b = io.interval(aligned[ask["id"]], f"align {ask['id']}") if not all(own) else (None, None)
            a, b = (piece["out"][0] + t - piece["src"][0] if keep else x
                    for keep, t, x in zip(own, (word["start"], word["end"]), (a, b)))
        else:
            refusals += [layout.refusal(None, "aligned", w, "records/cut_plan2.json#spans", "item 17")
                         for w in word["ids"]]
            missed += [{"word": w, "reason": "not aligned"} for w in word["ids"]]
            continue
        start, end = cs(a, math.ceil), cs(b, math.floor)
        if a < last:
            refusals.append(layout.refusal(None, "aligned_order", {"word": word["ids"][0], "at": [a, b]}, "frame_map"))
        if end <= start:  # its joined edges reversed, or too short to be written to the centisecond
            refusals.append(layout.refusal(None, "word_interval", {"word": word["ids"][0], "at": [a, b]}, "frame_map"))
        last = a
        tokens.append(dict(word, start=start, end=end, at=[a, b]))
    return tokens, refusals, missed


def _make(video, c):
    """(record, burn layer text or None) for an input set."""
    if c["missing"]:
        return _record(c, "unavailable"), None
    s, name, model = c["settings"], c["name"], c["settings"]["aligner"]["model"]
    profile = s[name]
    unsaid = [w["id"] for _, w in c["kept"] if "unsaid" in w.get("flags", [])]
    if unsaid:
        return _record(c, "refused", refusals=[layout.refusal(None, "unsaid", w, "records/cut_plan2.json#spans",
                                                              "item 17") for w in unsaid],
                       uncaptioned=[{"word": w, "reason": "unsaid"} for w in unsaid]), None
    held = sorted({w["id"] for _, w in c["kept"] if "held" in w.get("flags", [])})
    font = {k: s["tall"][k] for k in ("font", "size", "outline", "shadow")}
    try:
        tokens, refusals, missed = _times(video, c, model)
        if refusals:
            return _record(c, "refused", refusals=refusals, uncaptioned=missed, held=held), None
        onsets = video.onsets(io.path(video, c["voice"]))
        measure = (lambda text: io.number(video.width(text, font), "width")) if c["measured"] else None
        safe_width = layout.bands(s)["bottom"][2]
        cues, refusals = layout.group(tokens, name, profile, c["pieces"], c["duration"], measure, safe_width)
        if c["measured"]:
            refusals += layout.place(cues, c["pieces"], c["elements"], s, measure)
    except LookupError as error:
        c["missing"] = str(error.args[0]) if error.args else "adapter"
        return _record(c, "unavailable"), None
    if not isinstance(onsets, list) or not onsets:
        raise SpineError("onsets: the adapter measured no onsets")
    onsets = [io.number(t, "onset") for t in onsets]
    off = [round(min(abs(cue["start_s"] - t) for t in onsets), 3) for cue in cues]
    refusals += [layout.refusal(cue["id"], "max_onset_off_s", o, f"{name}.onset_tol_s")
                 for cue, o in zip(cues, off) if o > profile["onset_tol_s"]]
    readings = {"min_cue_s": round(min(x["end_s"] - x["start_s"] for x in cues), 2), "max_onset_off_s": max(off),
                "overlaps": sum(r["reading"] == "overlaps" for r in refusals)}
    if c["measured"]:
        readings["outside_safe"] = sum(r["reading"] == "outside_safe" for r in refusals)
    if refusals:
        return _record(c, "refused", cues, refusals, held=held, readings=readings), None
    burn = layout.ass(cues, s).decode() if c["measured"] and not c["owner"] else None
    sidecar = _srt_name(c["output"])
    record = _record(c, "captioned", cues, held=held, readings=readings, sidecar={
        "file": sidecar, "sha256": digest(layout.srt(cues))}, burn=burn and {
        "file": f"records/captions/{c['output']}.ass", "sha256": digest(burn.encode())})
    return record, burn


def _name(output):
    return f"records/captions/{output}.json"


def _srt_name(output):
    return output.replace("short-", "Shorts/", 1) + ".srt" if output.startswith("short-") else SIDECARS[output]


def _current(video, record):
    """Whether the burn layer a captioned record names holds its bytes."""
    burn = record["burn"]
    return burn is None or filehash(io.path(video, burn["file"])) == burn["sha256"]


def cues(video, output):
    """Make, or return unchanged, the cue record for one output; the record is committed last."""
    with writer(video):
        c = _inputs(video, output)
        path = io.path(video, _name(output))
        old = io.read(video, _name(output)) if path.exists() else None
        if old and old.get("state") == "captioned" and old.get("key") == c["key"] and _current(video, old):
            return old
        record, burn = _make(video, c)
        if io.hashes(video, c["reads"]) != c["reads"] or filehash(io.path(video, c["voice"])) != c["voice_sha256"]:
            raise SpineError(f"{output}: inputs changed while captioning")
        if burn is not None:
            io.write(video, record["burn"]["file"], burn)
        io.write(video, _name(output), record)
        return record


def _put(video, name, raw):
    """Write beside, fsync, rename; every component of the path is refused as a symlink before and at rename."""
    target = io.path(video, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        io.path(video, name)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sidecar(video, output):
    """Write the .srt a current captioned record names, from its cues; nothing when it already holds them."""
    with writer(video):
        record = io.read(video, _name(output))
        if record["state"] != "captioned":
            raise SpineError(f"{output}: no sidecar from a {record['state']} record")
        if _inputs(video, output)["key"] != record["key"]:
            raise SpineError(f"{output}: stale record; make cues again")
        raw, made = layout.srt(record["cues"]), record["sidecar"]
        if digest(raw) != made["sha256"] or made["file"] != _srt_name(output):
            raise SpineError(f"{output}: cues do not give the record's sidecar bytes at its sidecar path")
        if filehash(io.path(video, made["file"])) != made["sha256"]:
            _put(video, made["file"], raw)
        return made["file"]


def check(video, output):
    """{ok, state, next_action, detail}; ok only for a current captioned record whose burn and sidecar match."""
    def answer(state, next_action, detail):
        return {"ok": state == "captioned", "state": state, "next_action": next_action, "detail": detail}
    c = _inputs(video, output)
    if c["missing"]:
        return answer("unavailable", "supply", c["missing"])
    if not io.path(video, _name(output)).exists():
        return answer("missing", "cues", _name(output))
    record = io.read(video, _name(output))
    if record["key"] != c["key"]:
        return answer("stale", "cues", {"record": record["key"], "inputs": c["key"]})
    if record["state"] != "captioned":
        return answer(record["state"], "revise" if record["state"] == "refused" else "supply",
                      record["refusals"] or record["missing"])
    if not _current(video, record):
        return answer("incomplete", "cues", record["burn"]["file"])
    if filehash(io.path(video, record["sidecar"]["file"])) != record["sidecar"]["sha256"]:
        return answer("incomplete", "sidecar", record["sidecar"]["file"])
    return answer("captioned", None, record["readings"])
