"""Measured, word-identified cuts; the transaction follows docs/cutting.md section 3."""
from copy import deepcopy

from ..spine.files import SpineError, canon, digest, inside
from . import plan


def _quiet(measurements):
    cfg = plan.settings()["edges"]
    levels = sorted(row["db"] for row in measurements["audio"]
                    if row["kind"] == "quiet" and row["db"] > cfg["digital_silence_db"])
    if not levels:
        raise SpineError("source audio: no measurable room floor above digital silence")
    floor = levels[int((len(levels) - 1) * cfg["quiet_percentile"])]
    return [row for row in measurements["audio"]
            if row["kind"] == "quiet"
            and cfg["digital_silence_db"] < row["db"] <= floor + cfg["quiet_margin_db"]]


def _covered(start, end, quiet):
    cursor = start
    for row in quiet:
        if row["end"] <= cursor:
            continue
        if row["start"] > cursor + 1e-9:
            return False
        cursor = max(cursor, row["end"])
        if cursor >= end - 1e-9:
            return True
    return False


def _sound(word, measurements):
    speech = [row for row in measurements["audio"] if row["kind"] == "speech"
              and row["start"] < word["end"] and row["end"] > word["start"]]
    if not speech:
        raise SpineError(f"source audio: no measured sound for word {word['id']}")
    return speech[0]["start"], speech[-1]["end"]


def edge_bounds(span, words, measurements, last, end_hold):
    """The same quiet windows define legal edges for settling and checking."""
    if span["enter"] == "pop":
        return list(span["src"])
    selected = plan.selected(words, span["words"])
    first, final = selected[0], selected[-1]
    sound_start, sound_end = _sound(first, measurements)[0], _sound(final, measurements)[1]
    cfg = plan.settings()["edges"]
    quiet = _quiet(measurements)
    head_min, head_max = cfg["head_lead_s"]
    tail_min, tail_max = cfg["tail_s"]
    starts = [sound_start - head_max, sound_start - head_min]
    starts += [row["start"] for row in quiet
               if sound_start - head_max <= row["start"] <= sound_start - head_min]
    starts = [x for x in sorted(starts) if 0 <= x <= first["start"] and _covered(x, sound_start, quiet)]
    if last:
        ends = [sound_end + end_hold]
    else:
        ends = [sound_end + tail_min, sound_end + tail_max]
        ends += [row["end"] for row in quiet
                 if sound_end + tail_min <= row["end"] <= sound_end + tail_max]
    ends = [x for x in sorted(ends) if final["end"] <= x <= measurements["duration_s"]
            and _covered(sound_end, x, quiet)]
    outside = [word for word in words if word["id"] not in {w["id"] for w in selected}]
    starts = [x for x in starts if not any(w["start"] < first["start"] and w["end"] > x
                                         for w in outside)]
    ends = [x for x in ends if not any(w["start"] < x and w["end"] > final["end"]
                                     for w in outside)]
    if not starts or not ends:
        raise SpineError(f"span {span['id']}: edges have no quiet head/tail before a word or breath")
    return [starts[0], ends[0]]


def _lead(span, measurements):
    cfg = plan.settings()["jcuts"]
    frame = 1 / measurements["fps"]
    candidates = [row["time"] + cfg["motion_after_frames"] * frame - span["src"][0]
                  for row in measurements["motion"] if row["value"] >= cfg["motion_threshold"]]
    candidates = [value for value in candidates
                  if cfg["min_frames"] * frame <= value <= cfg["max_lead_s"]]
    if not candidates:
        raise SpineError(f"span {span['id']}: jcut has no measured motion onset within lead bounds")
    span["lead_s"] = min(candidates)
    span["lead_from"] = "motion onset"


def _removals(maker, spans, words, duration):
    """Move the edges of omissions without changing why their words were dropped."""
    ordered = sorted(spans, key=lambda item: item["src"])
    gaps, cursor = [], 0.0
    for span in ordered:
        start, end = span["src"]
        if start < cursor - 1e-9:
            raise SpineError(f"span {span['id']}: measured source intervals overlap")
        if start > cursor:
            gaps.append([cursor, start])
        cursor = end
    if cursor < duration:
        gaps.append([cursor, duration])
    result = []
    for start, end in gaps:
        omitted = [w for w in words if w["start"] < end and w["end"] > start]
        rows = []
        for old in maker["removed"]:
            dropped = old["words"]
            ids = {w["id"] for w in plan.selected(words, dropped)} if len(dropped) == 2 else set(dropped)
            matches = [w for w in omitted if w["id"] in ids]
            if matches:
                rows.append((matches[0]["start"], old, matches))
        if not rows:
            if omitted:
                raise SpineError(f"removed words have no decision: {omitted[0]['id']}")
            previous = [r for r in maker["removed"] if r["src"][0] < end and r["src"][1] > start]
            old = max(previous, key=lambda r: min(end, r["src"][1]) - max(start, r["src"][0])) if previous else None
            row = deepcopy(old) if old is not None else {"why": "silence", "words": []}
            row.update(src=[start, end], words=[])
            result.append(row)
            continue
        rows.sort(key=lambda item: item[0])
        assigned = {word["id"] for _, _, group in rows for word in group}
        if assigned != {word["id"] for word in omitted}:
            raise SpineError("removed words have no retained reason")
        left = start
        for index, (_, old, group) in enumerate(rows):
            right = end if index == len(rows) - 1 else (group[-1]["end"] + rows[index + 1][2][0]["start"]) / 2
            row = deepcopy(old)
            row.update(src=[left, right], words=[group[0]["id"], group[-1]["id"]]
                       if len(group) > 1 else [group[0]["id"]])
            result.append(row)
            left = right
    return result


def short_pieces(ranges, words, measurements):
    cfg = plan.settings()["shorts"]
    minimum = plan.settings()["shots"]["min_frames"] / measurements["fps"]
    pieces = []
    for pair in ranges:
        selected = plan.selected(words, pair)
        groups = [[selected[0]]]
        for word in selected[1:]:
            if word["start"] - groups[-1][-1]["end"] > cfg["gap_cut_s"]:
                groups.append([])
            groups[-1].append(word)
        for index, group in enumerate(groups):
            head = cfg["head_pad_s"] if index == 0 else cfg["pad_s"]
            tail = cfg["tail_pad_s"] if index == len(groups) - 1 else cfg["pad_s"]
            start = max(0, group[0]["start"] - head)
            end = min(measurements["duration_s"], group[-1]["end"] + tail)
            excluded = [w for w in words if w["id"] not in {item["id"] for item in group}]
            before = [w["end"] for w in excluded if w["end"] <= group[0]["start"]]
            after = [w["start"] for w in excluded if w["start"] >= group[-1]["end"]]
            if before:
                start = max(start, max(before))
            if after:
                end = min(end, min(after))
            if end - start >= minimum:
                pieces.append([start, end])
    return pieces


def _shorts(record, words, settled, inputs):
    record = deepcopy(plan.validate_shorts(record, words, settled))
    record["plan"] = settled["key"]
    record["made_from"] = inputs
    for short in record["shorts"]:
        if short.get("full") is not True:
            short["pieces"] = short_pieces(short["spans"], words, settled["measurements"])
    record["key"] = plan.shorts_key(settled["key"], inputs)
    return record


def _named(maker, words, retiming):
    by_id = {word["id"]: word for word in words}
    for entry in maker["named"]:
        if retiming:
            anchor = entry["anchor"]
            if anchor["word"] not in by_id:
                raise SpineError(f"named {entry['rule']}: anchor {anchor['word']} is gone")
            entry["at_src"] = by_id[anchor["word"]]["start"] + anchor["offset_s"]
            continue
        candidates = [word for word in words if word["start"] <= entry["at_src"] <= word["end"]]
        if not candidates:
            for span in maker["spans"]:
                if span["words"]:
                    for time, wid in zip(span["src"], span["words"]):
                        if abs(time - entry["at_src"]) <= plan.settings()["edges"]["tolerance_s"]:
                            candidates.append(by_id[wid])
        if not candidates:
            raise SpineError(f"named {entry['rule']}: at_src must identify a word or span seam")
        word = candidates[0]
        entry["anchor"] = {"word": word["id"], "offset_s": entry["at_src"] - word["start"]}


def _build(maker, words, measurements, inputs, job):
    settled = deepcopy(maker)
    settled["made_from"] = inputs
    settled["measurements"] = measurements
    settled["measurement_key"] = digest(canon(measurements))
    settled["key"] = plan.settle_key(inputs, maker["plan_version"], settled["measurement_key"])
    spans = sorted(settled["spans"], key=lambda span: span["order"])
    for index, span in enumerate(spans):
        span["src"] = edge_bounds(span, words, measurements, index == len(spans) - 1, maker["end_hold_s"])
        if span["words"]:
            selected = plan.selected(words, span["words"])
            span["edges"] = {"head_lead_s": _sound(selected[0], measurements)[0] - span["src"][0],
                             "tail_s": span["src"][1] - _sound(selected[-1], measurements)[1]}
            span["word_ids"] = [word["id"] for word in selected]
            span["word_times"] = {word["id"]: [word["start"], word["end"]] for word in selected}
            if "finished" not in selected[-1]:
                raise SpineError(f"word {selected[-1]['id']}: missing finished field")
            span["finished"] = selected[-1]["finished"]
        if span["enter"] == "jcut":
            _lead(span, measurements)
        if "patch" in span or any(a < span["src"][1] and b > span["src"][0] for a, b in job["voice_over"]):
            span["hide_picture"] = True
    settled["spans"] = spans
    settled["source_duration_s"] = measurements["duration_s"]
    settled["output_duration_s"] = sum(span["src"][1] - span["src"][0] for span in spans)
    settled["removed"] = _removals(maker, spans, words, measurements["duration_s"])
    return settled


def _optional(video, path):
    return plan.read(video, path) if inside(video.folder, path).exists() else None


def _run(video, retiming):
    with plan.writer(video):
        old = _optional(video, "records/cut_plan2.json")
        if retiming and old is None:
            raise SpineError("records/cut_plan2.json: retime needs the last settled plan")
        try:
            maker, words, job, measurements, inputs = plan.snapshot(video, intent=old if retiming else None)
        except plan.MissingEndpoint as error:
            if not retiming:
                raise
            refusal = {"ok": False, "state": "fail", "next_action": "revise",
                       "detail": str(error), "plan": old["key"]}
            plan.write(video, "records/checks/cut.json", refusal)
            return refusal
        _named(maker, words, retiming)
        if retiming:
            previous_times = {wid: times for span in old["spans"] if span["words"]
                              for wid, times in span["word_times"].items()}
            current_times = {word["id"]: word for word in words}
            for span in maker["spans"]:
                if span["enter"] == "pop":
                    anchor = span["expression"]["after_word"]
                    if anchor not in previous_times or anchor not in current_times:
                        raise SpineError(f"span {span['id']}: expression anchor {anchor} is gone")
                    shift = current_times[anchor]["end"] - previous_times[anchor][1]
                    span["src"] = [time + shift for time in span["src"]]
        shorts = _optional(video, "records/shorts_plan.json")
        shorts_inputs = plan.hashes(video, ["records/shorts_plan.json"]) if shorts is not None else {}
        settled = _build(maker, words, measurements, inputs, job)
        if old is not None and old["key"] == settled["key"]:
            settled["touched"], settled["moved"] = old["touched"], old["moved"]
        else:
            prior = {span["id"]: span for span in old["spans"]} if old is not None else {}
            settled["touched"] = [span["id"] for span in settled["spans"] if prior.get(span["id"]) != span]
            settled["touched"] += [sid for sid in prior if sid not in {span["id"] for span in settled["spans"]}]
            settled["moved"] = [span["id"] for span in settled["spans"]
                                if span["id"] in prior and prior[span["id"]]["src"] != span["src"]]
        paper = "\n\n".join(" ".join(word["text"] for word in plan.selected(words, span["words"]))
                              for span in settled["spans"] if span["words"]) + "\n"
        outputs = {"records/paper.md": paper}
        if shorts is not None:
            outputs["records/shorts_spec.json"] = _shorts(shorts, words, settled, shorts_inputs)
        if retiming:
            previous = _optional(video, "records/retime.json")
            outputs["records/retime.json"] = previous if previous is not None and previous["plan"] == settled["key"] else {
                "from": old["key"], "plan": settled["key"], "moved": settled["moved"], "touched": settled["touched"]}
        transaction_inputs = {**inputs, **shorts_inputs}
        if plan.hashes(video, list(transaction_inputs)) != transaction_inputs:
            raise SpineError("settle inputs changed before writing")
        for path, record in outputs.items():
            plan.write(video, path, record)
        if plan.hashes(video, list(transaction_inputs)) != transaction_inputs:
            raise SpineError("settle inputs changed before committing cut_plan2")
        # This is the commit marker: a crash before it leaves the preceding revision for retry.
        plan.write(video, "records/cut_plan2.json", settled)
        key = settled["key"]
    if retiming:
        from .checks import check
        check(video)
    return key


def settle(video):
    """Write measured plans and the paper, replaying the same transaction on retry."""
    return _run(video, False)


def retime(video):
    """Carry decisions by their stable words, settle their new clock, and re-check."""
    return _run(video, True)
