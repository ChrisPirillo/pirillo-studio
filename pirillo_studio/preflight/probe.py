"""preflight(video, stage, ...): can this stage's expensive work finish? docs/preflight.md sections 1 to 3.

The stage is data (spine.md section 1 plus preflight.md's `tools`, output `size` and `scratch_gb`); no recipe name
reaches it. Every effect is a callable on `host`: capability(id), qualify(id), voice(part, cites), probe_media(path), volume(path) ->
(name, free bytes), now() -> aware datetime, serve_words(id, words) -> the Board's served href, file_task(task). The one write is records/ready/<stage>.json.
"""
import datetime as dt
import hashlib
import json
import math
import os
import re

VERSION = "1"   # preflight_version: change it when a probe's logic changes
DATA = (".json", ".md", ".txt", ".srt", ".vtt", ".csv")   # every other declared file is media: probed and sized
MEDIA_NEEDS = ("container", "duration_s", "fps_measured", "fps_tagged", "audio_rate_hz", "width", "height",
               "audio_gap_s")
GAP = "media.max_audio_gap_s"   # the host's one limit, passed in cfg
GB = 1e9
ORDER = ("inputs", "tools", "media", "disk")
PROBE = "ffprobe"   # the one media probe tool (section 1)


class BrokenMedia(Exception):
    """The file cannot be used: the probe tool ran and the file failed it."""


class ToolFailed(Exception):
    """The probe tool itself could not run; blocks naming the tool, never broken media."""

    def __init__(self, tool, why):
        super().__init__(f"{tool}: {why}")
        self.tool = tool


def _safe(rel):
    if os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
        raise ValueError(f"unsafe stage path {rel!r}")
    return rel


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def _read(path):
    """A record that is missing, torn or from another preflight_version is no record: it never reads as ready."""
    try:
        with open(path) as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    ok = isinstance(rec, dict) and rec.get("preflight_version") == VERSION and isinstance(rec.get("files"), dict) \
        and [p.get("probe") for p in rec.get("probes") or [] if isinstance(p, dict)] == list(ORDER)
    return rec if ok else None


def _write(path, rec):
    tmp = path + ".tmp"   # one writer per video (spine.md), so one fixed temp name; a torn one is overwritten
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(rec, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _row(probe, inputs, state="ready", next_action=None, detail=None):
    return {"probe": probe, "inputs": inputs, "ok": state == "ready", "state": state,
            "next_action": next_action, "detail": detail}


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _media_file(rel):  # shortcut: footage only (a video and an audio stream); an audio-only input is broken, loudly
    return not rel.lower().endswith(DATA)


def media_problem(d, max_gap):
    """Why a probe's detail cannot stand, or None: an unknown value is broken, never a default."""
    if not isinstance(d, dict):
        return "the probe returned no detail"
    for kind in ("video", "audio"):   # first: a missing stream is the cause its unknown fields follow from
        if not (d.get("streams") or {}).get(kind):
            return f"no {kind} stream"
    for k in MEDIA_NEEDS:
        if d.get(k) is None:
            return f"unknown {k}"
    for k in ("duration_s", "fps_measured", "audio_rate_hz", "width", "height", "audio_gap_s"):
        if not _num(d[k]) or d[k] < 0 or d[k] == 0 and k != "audio_gap_s":
            return f"{k} is {d[k]!r}"
    if d["audio_gap_s"] > max_gap:
        return f"an audio gap of {d['audio_gap_s']:g} s, over the {max_gap:g} s limit"
    if d.get("packets_read") != "all":
        return f"packets read: {d.get('packets_read')!r}, not all"
    return None


def _inputs(video, stage, passed, old):
    files, hashes = {}, {}
    for rel in stage["inputs"]:
        full, by = os.path.join(video, _safe(rel)), passed[rel]["by"]
        try:
            st = os.stat(full)
            ident = [st.st_size, st.st_ino, st.st_ctime_ns]
            was = (old or {}).get("files", {}).get(rel) or {}
            sha = was["sha256"] if was.get("stat") == ident else _sha(full)
        except OSError as e:
            why = "missing" if isinstance(e, FileNotFoundError) else "unreadable"
            return None, None, (f"run {by} to produce {rel}", f"inputs: {rel} is {why}", rel)
        if passed[rel].get("sha256") not in (None, sha):
            return None, None, (f"run {by} again: {rel} changed since it passed", f"inputs: {rel} is stale", rel)
        files[rel], hashes[rel] = {"sha256": sha, "stat": ident}, sha
    return files, hashes, None


def _tools(host, stage, media, cites):
    if media and PROBE not in stage["tools"]:   # its version keys the media answer, so it is declared
        return None, (f"the recipe declares {PROBE} for {stage['id']}", f"tools: {stage['id']} reads media and declares no {PROBE}", PROBE, "blocked")
    detail = {}
    for t in stage["tools"]:
        if t.startswith("voice:"):   # the owner's voice choice and a cited patch's take pick (capabilities.md voice)
            st = host.voice(t[len("voice:"):], cites)
            if st["state"] == "qualified":   # the selection, the engine's install and the picked take key the answer
                st = dict(st, key="sha256:" + hashlib.sha256(json.dumps(
                    [st.get("key"), st.get("capability_key"), st["detail"]]).encode()).hexdigest())
        else:
            st = host.capability(t)
            if st["state"] == "unqualified":   # a current key with no record: qualify once (capabilities.md)
                st = host.qualify(t)
        if st["state"] != "qualified":
            end = "waiting_owner" if st["state"] == "waiting_owner" else "blocked"
            return None, (st["next_action"], f"tools: {t} is {st['state']}: {st['detail']}", t, end)
        if not (isinstance(st.get("key"), str) and st["key"]):   # the key is what makes a new install a new answer
            return None, (f"the host fixes capability {t}", f"tools: {t} is qualified with no capability key", t, "blocked")
        detail[t] = {"key": st["key"], "version": (st.get("resolved") or {}).get("version"), "detail": st["detail"]}
    return detail, None


def _media(host, video, stage, files, max_gap):
    """(detail, None) or (detail so far, (state, next_action, line, what, why))."""
    detail = {}
    for rel in filter(_media_file, stage["inputs"]):
        try:
            d = host.probe_media(os.path.join(video, rel))
            why = media_problem(d, max_gap)
        except ToolFailed as e:
            return detail, ("blocked", f"the host fixes the probe tool {e.tool}", f"media: probe tool failed: {e}", e.tool, str(e))
        except BrokenMedia as e:
            d, why = None, str(e) or "unreadable"
        if why:
            detail[rel] = {"sha256": files[rel]["sha256"], "broken": why, "probe": d}
            return detail, ("broken", f"the owner replaces or drops {rel}", f"media: {rel} is broken: {why}", rel, why)
        detail[rel] = d
    return detail, None


def _disk(host, video, stage, cfg, media):
    """(detail, blocks, next_action, line): need per volume against free plus the margin."""
    def stop(what, line):
        return None, [{"probe": "disk", "what": what}], f"the host fixes {what}", "disk: " + line
    try:
        margin = cfg["disk"]["render_floor_gb"]
    except (KeyError, TypeError):
        return stop("disk.render_floor_gb", "the margin setting disk.render_floor_gb is missing")
    if not _num(margin) or margin < 0:
        return stop("disk.render_floor_gb", f"the margin setting disk.render_floor_gb is {margin!r}")
    if not _num(stage.get("scratch_gb")) or stage["scratch_gb"] < 0:
        return stop("scratch_gb", f"stage {stage['id']} declares no scratch_gb")
    durations = [d["duration_s"] for d in media.values()]
    need, free = {}, {}
    places = [(o if isinstance(o, str) else o["path"], None if isinstance(o, str) else o.get("size"))
              for o in stage["outputs"]]
    for rel, size in places + [(".", "scratch")]:
        n = stage["scratch_gb"] * GB if size == "scratch" else 0
        if size != "scratch" and _media_file(rel):
            bps = (size or {}).get("bytes_per_s")
            if not _num(bps) or bps <= 0:
                return stop(rel, f"{rel} has no size declaration")
            if not durations:
                return stop(rel, f"{rel} has no probed duration to size it by")
            n = bps * max(durations)   # the longest media input: never under the need
        try:
            vol, nfree = host.volume(os.path.join(video, _safe(rel)))
        except OSError as e:
            return stop(rel, f"free space under {rel} is unreadable: {e}")
        if not _num(nfree) or nfree < 0:
            return stop(rel, f"free space under {rel} reads {nfree!r}")
        need[vol], free[vol] = need.get(vol, 0) + n, nfree
    vols = [{"volume": v, "need_gb": round(need[v] / GB, 1), "margin_gb": margin, "free_gb": round(free[v] / GB, 1)}
            for v in sorted(need)]
    short = [(v, math.ceil(round((need[v["volume"]] / GB + margin - free[v["volume"]] / GB) * 10, 6)) / 10)
             for v in vols if need[v["volume"]] / GB + margin > free[v["volume"]] / GB]
    if not short:
        return {"volumes": vols}, [], None, None
    v, gb = short[0]
    return {"volumes": vols}, [{"probe": "disk", "what": f"{x['volume']} volume", "short_gb": g} for x, g in short], \
        f"free {gb} GB on the {v['volume']} volume", \
        f"disk: {v['volume']} volume has {v['free_gb']} GB free; {stage['id']} needs {v['need_gb']} GB plus the {margin} GB margin"


def _task(host, key, stage_id, record, rel, why, media_row):
    tid, words = "preflight:" + key.split(":", 1)[1], json.dumps(media_row, sort_keys=True)
    href = host.serve_words(tid, words)
    if not (isinstance(href, str) and href):
        raise ValueError(f"the host served no href for {tid}")
    undo = {"until": None, "why": f"Until the {stage_id} stage runs"}
    return {"id": tid, "kind": "ask", "loop": "filed", "what": f"{rel} cannot be used for {stage_id}; replace it or drop the video",
            "why": why, "source": {"record": record, "filed_by": "preflight"},
            "judge": {"type": "words", "href": href, "words": words},
            "actions": [{"verb": "replace", "label": "Replace the file", "undo": dict(undo, verb="undo-replace")},
                        {"verb": "drop", "label": "Drop the video", "undo": dict(undo, verb="undo-drop")}],
            "cant": {"words": "", "goes_to": "preflight"}}


def preflight(video, stage, cfg, host, recipe_revision, passed, cites=None):
    """The result record for `stage` of the video folder `video`, written to records/ready/<stage>.json.

    passed: each declared input -> {"by": its producing stage, "sha256": its content when that stage last passed,
    or None for an external input}; the spine's freshness evidence and the resolver a block names.
    cites: a voice patch's {"take", "original"} paths, for a stage declaring voice:speech (capabilities.md voice).
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(stage["id"])):
        raise ValueError(f"unsafe stage id {stage['id']!r}")
    record = f"records/ready/{stage['id']}.json"
    path = os.path.join(video, record)
    old = _read(path)
    outs = [o if isinstance(o, str) else o["path"] for o in stage["outputs"]]
    rows = {p: _row(p, list(stage["tools"]) if p == "tools" else outs if p == "disk" else
                    [r for r in stage["inputs"] if p == "inputs" or _media_file(r)],
                    "not_reached") for p in ORDER}
    rec = {"video": "records/job.json", "stage": stage["id"], "recipe_revision": recipe_revision, "key": None,
           "at": host.now().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "preflight_version": VERSION,
           "files": {}, "blocks": []}

    def end(state, next_action, line):
        rec.update(ok=state == "ready", state=state, next_action=next_action, detail=line,
                   probes=[rows[p] for p in ORDER])
        _write(path, rec)
        return rec

    files, hashes, stop = _inputs(video, stage, passed, old)
    if stop:
        rows["inputs"] = _row("inputs", rows["inputs"]["inputs"], "blocked", stop[0], stop[1])
        rec["blocks"] = [{"probe": "inputs", "what": stop[2]}]
        return end("blocked", stop[0], stop[1])
    rec["files"] = files
    rows["inputs"] = _row("inputs", rows["inputs"]["inputs"], detail=hashes)
    tools, stop = _tools(host, stage, rows["media"]["inputs"], cites)
    if stop:
        rows["tools"] = _row("tools", rows["tools"]["inputs"], stop[3], stop[0], stop[1])
        rec["blocks"] = [{"probe": "tools", "what": stop[2]}]
        return end(stop[3], stop[0], stop[1])
    rows["tools"] = _row("tools", rows["tools"]["inputs"], detail=tools)
    max_gap = (cfg.get("media") or {}).get("max_audio_gap_s") if isinstance(cfg, dict) else None
    if rows["media"]["inputs"] and (not _num(max_gap) or max_gap < 0):
        line = f"media: the limit setting {GAP} is {max_gap!r}"
        rows["media"] = _row("media", rows["media"]["inputs"], "blocked", f"the host fixes {GAP}", line)
        rec["blocks"] = [{"probe": "media", "what": GAP}]
        return end("blocked", f"the host fixes {GAP}", line)
    rec["key"] = key = "sha256:" + hashlib.sha256(json.dumps(   # the limit judges the answer, so it keys it
        [stage["id"], recipe_revision, sorted(hashes.items()), sorted((t, d["key"]) for t, d in tools.items()), VERSION, max_gap]).encode()).hexdigest()
    same = old is not None and old.get("key") == key
    was = old["probes"][ORDER.index("media")] if same else None
    if was and was["state"] in ("ready", "broken"):   # the expensive probe is not re-run for an unchanged key
        rows["media"] = was
        if was["state"] == "broken":
            rec["task"] = old["task"]
            rec["blocks"] = old["blocks"]
            return end("broken", was["next_action"], old["detail"])
    else:
        media, stop = _media(host, video, stage, files, max_gap)
        if stop:
            state, nxt, line, what, why = stop
            rows["media"] = _row("media", rows["media"]["inputs"], state, nxt, media)
            rec["blocks"] = [{"probe": "media", "what": what, "why": why}]
            if state == "broken":   # the Board dedupes by the task's id, so a crash before the write files no second
                task = _task(host, key, stage["id"], record, what, why, rows["media"])
                host.file_task(task)
                rec["task"] = task["id"]
            return end(state, nxt, line)
        rows["media"] = _row("media", rows["media"]["inputs"], detail=media)
    detail, blocks, nxt, line = _disk(host, video, stage, cfg, rows["media"]["detail"])
    rows["disk"] = _row("disk", rows["disk"]["inputs"], "blocked" if blocks else "ready", nxt, detail or line)
    if blocks:
        rec["blocks"] = blocks
        return end("blocked", nxt + (f", then run {stage['id']} again" if detail else ""), line)
    return end("ready", f"run {stage['id']}", f"{len(ORDER)} probes passed")
