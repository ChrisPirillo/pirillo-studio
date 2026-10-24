"""Strict cutting inputs and atomic records; the host supplies measured audio and motion."""
import copy
import json
import math
import os
import tempfile
from pathlib import Path

from ..spine.engine import writer
from pirillo_studio.spine.files import SpineError, canon, digest, filehash, inside, load, safe


def number(value, at):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SpineError(f"{at}: expected finite number")
    return value


def push(span):
    """Validate movement evidence at both the plan and renderer boundaries."""
    at = f"{span['id']}: push"
    if span['enter'] != 'push':
        if 'push' in span:
            raise SpineError(f'{at}: contradicts entry')
        return None
    value = span.get('push')
    if not isinstance(value, dict):
        raise SpineError(f'{at}: expected object with to and s')
    if value.get('to') not in ('wide', 'med', 'close', 'face', 'mouth'):
        raise SpineError(f'{at}.to: missing or unknown shot')
    if number(value.get('s'), at + '.s') <= 0:
        raise SpineError(f'{at}.s: duration must be positive')
    return value


def settings():
    return load(Path(__file__).parent, "settings.json")


def path(video, name):
    safe(name, "cutting")
    return inside(video.folder, name)


def read(video, name):
    try:
        path(video, name)
        return load(video.folder, name)
    except (OSError, ValueError, TypeError) as exc:
        raise SpineError(f"{name}: unreadable ({exc})") from None


def hashes(video, names):
    result = {}
    for name in names:
        try:
            value = filehash(path(video, name))
        except OSError as exc:
            raise SpineError(f"{name}: unreadable ({exc})") from None
        if value is None:
            raise SpineError(f"{name}: missing")
        result[name] = value
    return result


def write(video, name, value):
    safe(name, "cutting output")
    if not name.startswith("records/"):
        raise SpineError(f"{name}: cutting writes records only")
    target = path(video, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = value.encode() if isinstance(value, str) else canon(value) + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        path(video, name)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def text(value, at):
    if not isinstance(value, str) or not value.strip():
        raise SpineError(f"{at}: expected nonempty text")
    return value


def interval(value, at):
    if not isinstance(value, list) or len(value) != 2:
        raise SpineError(f"{at}: expected two bounds")
    a, b = (number(x, at) for x in value)
    if a < 0 or b <= a:
        raise SpineError(f"{at}: reversed or negative interval")
    return a, b


def words(record):
    try:
        rows = record["words"]
        if not isinstance(rows, list) or not rows:
            raise SpineError("records/words.json: no words")
        ids, end = set(), 0
        for word in rows:
            wid = text(word["id"], "word id")
            text(word["text"], wid)
            a, b = interval([word["start"], word["end"]], wid)
            if wid in ids or a < end:
                raise SpineError(f"records/words.json: duplicate or overlapping word {wid}")
            if "flags" in word and (not isinstance(word["flags"], list)
                                    or any(not isinstance(x, str) for x in word["flags"])):
                raise SpineError(f"{wid}: invalid flags")
            if "finished" in word and type(word["finished"]) is not bool:
                raise SpineError(f"{wid}: finished must be boolean")
            ids.add(wid)
            end = b
        return rows
    except (KeyError, TypeError) as exc:
        raise SpineError(f"records/words.json: malformed words ({exc})") from None


class MissingEndpoint(SpineError):
    """A word identity is gone; retime must record a refusal without moving it."""


def selected(rows, bounds):
    if not isinstance(bounds, list) or len(bounds) != 2:
        raise SpineError(f"words: expected first and last IDs: {bounds}")
    ids = [w["id"] for w in rows]
    try:
        a, b = (ids.index(w) for w in bounds)
    except ValueError:
        raise MissingEndpoint(f"words: missing endpoint {bounds}") from None
    if b < a:
        raise SpineError(f"words: reversed endpoints {bounds}")
    return rows[a:b + 1]


def validate_plan(plan, rows):
    try:
        if plan["video"] != "records/job.json":
            raise SpineError("plan video must be records/job.json")
        text(plan["plan_version"], "plan_version")
        if number(plan["end_hold_s"], "end_hold_s") < 0:
            raise SpineError("end_hold_s: negative")
        spans = plan["spans"]
        if not isinstance(spans, list) or not spans:
            raise SpineError("plan: no spans")
        ids, orders = set(), set()
        for span in spans:
            sid = text(span["id"], "span id")
            order = number(span["order"], sid)
            if type(order) is not int or order < 1 or sid in ids or order in orders:
                raise SpineError(f"{sid}: invalid order or duplicate identity")
            ids.add(sid)
            orders.add(order)
            interval(span["src"], sid)
            if span["enter"] not in {"open", "jump", "jcut", "push", "pop"}:
                raise SpineError(f"{sid}: unknown entry")
            if span["shot"] not in {"wide", "med", "close", "face", "mouth"}:
                raise SpineError(f"{sid}: unknown shot")
            if span["enter"] == "pop":
                if span["words"] != []:
                    raise SpineError(f"{sid}: expression has words")
                text(span["expression"]["after_word"], sid)
                text(span["expression"]["seen"], sid)
            else:
                try:
                    selected(rows, span["words"])
                except SpineError as exc:
                    raise type(exc)(f"{sid}: {exc}") from None
            push(span)
            if "hide_picture" in span and type(span["hide_picture"]) is not bool:
                raise SpineError(f"{sid}: hide_picture is not boolean")
            if "patch" in span:
                patch = span["patch"]
                safe(patch["record"], sid)
                if not patch["record"].startswith("records/patches/"):
                    raise SpineError(f"{sid}: patch outside records/patches")
                text(patch["sha"], sid)
                if not isinstance(patch["words"], list) or len(patch["words"]) != 2:
                    raise SpineError(f"{sid}: patch requires own word endpoints")
        if orders != set(range(1, len(spans) + 1)):
            raise SpineError("plan: orders must be consecutive")
        for removed in plan["removed"]:
            interval(removed["src"], "removed")
            if removed["why"] not in {"false_start", "flub", "filler", "repeat", "tangent", "recap", "silence"}:
                raise SpineError("removed: unknown reason")
            if not isinstance(removed["words"], list):
                raise SpineError("removed: missing words")
            if removed["why"] in {"false_start", "flub", "repeat"} and removed["kept_take"] not in ids:
                raise SpineError("removed: missing kept_take")
        for named in plan["named"]:
            number(named["at_src"], "named")
            text(named["rule"], "named")
            if not isinstance(named["why"], str):
                raise SpineError("named: reason must be text")
            if "anchor" in named:
                selected(rows, [named["anchor"]["word"]] * 2)
                number(named["anchor"]["offset_s"], "named anchor")
        cold = plan["cold_open"]
        if cold is not None:
            if cold["span"] not in ids:
                raise SpineError("cold_open: unknown span")
            selected(rows, cold["words"])
        canon(plan)
        return plan
    except MissingEndpoint:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SpineError(f"records/cut_plan.json: {exc}") from None


def validate_shorts(record, rows, plan):
    try:
        text(record["plan"], "shorts plan key")
        ids, full = set(), 0
        for short in record["shorts"]:
            sid = text(short["id"], "short id")
            if sid in ids:
                raise SpineError(f"{sid}: duplicate short")
            ids.add(sid)
            text(short["point"], sid)
            if short.get("full") is True:
                full += 1
                if any(k in short for k in ("spans", "pieces", "opens_on", "ends_on", "stands_alone")):
                    raise SpineError(f"{sid}: full vertical has short fields")
            else:
                if not short["spans"]:
                    raise SpineError(f"{sid}: no short ranges")
                for bounds in short["spans"]:
                    selected(rows, bounds)
                text(short["opens_on"], sid)
                text(short["ends_on"], sid)
                standalone = short["stands_alone"]
                if not isinstance(standalone, dict):
                    raise SpineError(f"{sid}: stands_alone must be a record")
        if full != 1:
            raise SpineError("shorts: exactly one full vertical required")
        canon(record)
        return record
    except (KeyError, TypeError, ValueError) as exc:
        raise SpineError(f"records/shorts_plan.json: {exc}") from None


def measurements(video, probe):
    try:
        measured = video.measure(path(video, "source.mp4"))
        for key in ("duration_s", "fps", "sample_rate"):
            if number(measured[key], f"measure {key}") <= 0:
                raise SpineError(f"measure {key}: must be positive")
            if number(probe[key], f"records/probe.json {key}") != measured[key]:
                raise SpineError(f"measure {key}: disagrees with records/probe.json")
        duration = measured["duration_s"]
        if not isinstance(measured["audio"], list) or not measured["audio"]:
            raise SpineError("source audio: missing readings")
        end = 0
        for row in measured["audio"]:
            a, b = interval([row["start"], row["end"]], "source audio")
            if not math.isclose(a, end, abs_tol=1e-9) or b > duration:
                raise SpineError("source audio: gap, overlap or reading beyond duration")
            number(row["db"], "source audio db")
            if row["kind"] not in {"quiet", "speech", "breath"}:
                raise SpineError("source audio: unknown sound classification")
            end = b
        if not math.isclose(end, duration, abs_tol=1e-9):
            raise SpineError("source audio: readings do not cover duration")
        if not isinstance(measured["motion"], list) or not measured["motion"]:
            raise SpineError("source motion: missing readings")
        last = -1
        for row in measured["motion"]:
            t = number(row["time"], "source motion time")
            value = number(row["value"], "source motion value")
            if t < 0 or t > duration or t <= last or value < 0:
                raise SpineError("source motion: invalid reading order or value")
            last = t
        canon(measured)
        return measured
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise SpineError(f"source measure: {exc}") from None


def snapshot(video, intent=None):
    """Read one immutable input set; a host measurement cannot silently change its source."""
    names = ["records/cut_plan.json", "records/words.json", "source.mp4",
             "records/probe.json", "records/job.json"]
    before = hashes(video, names)
    maker = read(video, "records/cut_plan.json") if intent is None else copy.deepcopy(intent)
    rows = words(read(video, "records/words.json"))
    validate_plan(maker, rows)
    job = read(video, "records/job.json")
    try:
        if not isinstance(job["voice_over"], list):
            raise SpineError("records/job.json: voice_over must be a list")
        for cover in job["voice_over"]:
            interval(cover, "records/job.json voice_over")
    except (KeyError, TypeError) as exc:
        raise SpineError(f"records/job.json: {exc}") from None
    for span in maker["spans"]:
        if "patch" in span:
            name = span["patch"]["record"]
            before.update(hashes(video, [name]))
            read(video, name)
    probe = read(video, "records/probe.json")
    measured = measurements(video, probe)
    if rows[-1]["end"] > measured["duration_s"]:
        raise SpineError("records/words.json: word beyond measured duration")
    if hashes(video, before) != before:
        raise SpineError("cutting inputs changed while being measured")
    return maker, rows, job, measured, before


def settle_key(inputs, plan_version, measurement_key):
    """One identity for settlement and its independent freshness check."""
    return digest(canon({"inputs": inputs, "settings": settings(),
                         "plan_version": plan_version, "measurements": measurement_key}))


def shorts_key(plan_key, inputs):
    """The shorts spec's identity, for settle and the check alike."""
    return digest(canon({"plan": plan_key, "inputs": inputs, "settings": settings()["shorts"]}))
