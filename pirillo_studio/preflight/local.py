"""The standard-library effects a host may wire into preflight's `host`: the ffprobe media probe, free space, the clock.

None of them defaults a value it could not read: an unknown field stays None for probe.media_problem to call broken.
"""
import bisect
import datetime as dt
import json
import math
import os
import shutil
import subprocess

from .probe import BrokenMedia, ToolFailed


def _run(argv):
    p = subprocess.run(argv, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _call(run, exe, args, path, noise=False):
    try:
        code, out, err = run([exe, "-v", "error"] + args + [path])
    except OSError as e:
        raise ToolFailed(exe, str(e)) from None
    if code < 0:   # killed by a signal: the tool failed, the file is not judged
        raise ToolFailed(exe, f"killed by signal {-code}")
    if code or err.strip() and not noise:
        raise BrokenMedia((err.strip() or f"{os.path.basename(exe)} exit {code}").splitlines()[0])
    return out


def _f(text):
    """A finite float, or ValueError: inf and nan are no reading."""
    x = float(text)
    if not math.isfinite(x):
        raise ValueError(text)
    return x


def _rate(text):
    n, _, d = str(text).partition("/")
    try:
        r = _f(n) / _f(d or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return text if math.isfinite(r) and r > 0 else None


def ffprobe_media(exe, path, run=_run):
    """The media probe of preflight.md section 1 through the one configured ffprobe `exe`: container and streams,
    every packet demuxed, frame rate measured from video packet timestamps, the widest audio gap (silence at either
    end included) from audio packet timestamps and durations, a 2 s decode at start, middle and end."""
    try:
        info = json.loads(_call(run, exe, ["-show_format", "-show_streams", "-of", "json"], path))
    except ValueError:
        raise ToolFailed(exe, "printed no JSON") from None
    fmt = info.get("format") or {}
    streams = [st for st in info.get("streams") or [] if not (st.get("disposition") or {}).get("attached_pic")]  # cover art
    kinds = {}
    for st in streams:
        kinds[st.get("codec_type")] = kinds.get(st.get("codec_type"), 0) + 1
    v = next((st for st in streams if st.get("codec_type") == "video"), {})
    a = next((st for st in streams if st.get("codec_type") == "audio"), {})
    try:
        duration = _f(fmt["duration"])
        start = _f(fmt["start_time"])
    except (KeyError, ValueError, TypeError):
        duration = start = None
    audio = {str(st.get("index")): [] for st in streams if st.get("codec_type") == "audio"}
    vids, keys = {str(st.get("index")): [] for st in streams if st.get("codec_type") == "video"}, []
    for line in _call(run, exe, ["-show_entries", "packet=stream_index,pts_time,duration_time,flags", "-of", "csv=p=0"],
                      path).split():
        idx, t, dur, flags = (line.split(",") + ["", "", ""])[:4]
        try:
            if idx in vids:
                vids[idx].append(_f(t))
                if "K" in flags and idx == str(v.get("index")):
                    keys.append(vids[idx][-1])
                if dur not in ("", "N/A"):   # a video packet's duration is optional, never non-finite
                    _f(dur)
            elif idx in audio:
                audio[idx].append((_f(t), _f(dur)))
        except ValueError:
            raise BrokenMedia(f"a stream {idx} packet has no finite timestamp or duration ({line!r})") from None
    # ffprobe ends a read at the first packet past its end; in decode order a reordered packet runs ahead of frames
    # still to show by up to this lead (a B-frame beside a held still screen: seconds), so each read runs that far on
    lead = 0.0
    for order in vids.values():
        low = math.inf
        for i in range(len(order) - 1, 0, -1):   # the latest pts read before each packet against the earliest to come
            low = min(low, order[i])
            lead = max(lead, order[i - 1] - low)
        order.sort()
    pts = vids.get(str(v.get("index")), [])
    gaps = []
    for packets in audio.values():
        ends = [start] + [a + d for a, d in sorted(packets)]
        gaps += [a - e for (a, _), e in zip(sorted(packets), ends)] + [start + duration - ends[-1]] \
            if packets and duration else [duration]
    fps = (len(pts) - 1) / (pts[-1] - pts[0]) if len(pts) > 1 and pts[-1] > pts[0] else None
    decoded = []
    if v and not keys:
        raise BrokenMedia("no video keyframe")
    if duration:
        keys.sort()
        av = [str(st.get("index")) for st in streams if st.get("codec_type") in ("video", "audio")]  # not timecode
        for t in sorted({start, start + max(duration / 2 - 1, 0.0), start + max(duration - 2, 0.0)}):  # pts, not 0
            end = min(t + 2, start + duration)
            seen = {}
            # the head is read unseeked (an MPEG-TS has no index to seek by); a seek into a GOP logs reference
            # errors at exit 0, so the frames returned are the verdict, never stderr
            k = bisect.bisect_right(keys, t) - 2   # the keyframe before t's own: a seek lands either side of it
            for line in _call(run, exe, ["-read_intervals", f"%+{2 + lead:g}" if t == start else
                                         f"{max(start, keys[k] - .5) if k >= 0 else start:g}%{t + 2 + lead:g}", "-show_entries",
                                         "frame=stream_index,best_effort_timestamp_time,duration_time", "-of",
                                         "csv=p=0"], path, noise=True).split():
                idx, ts, dur = (line.split(",") + ["", ""])[:3]
                if idx not in av:   # a cover picture decodes with no timestamp and is not the footage
                    continue
                try:   # a picture shows until the next one's pts (a still screen is held, not lost); a sound for its duration
                    at, d = _f(ts), 0.0 if dur in ("", "N/A") else _f(dur)
                    shown = vids.get(idx)
                    n = bisect.bisect_right(shown, at) if shown is not None else 0
                    seen.setdefault(idx, []).append((at, at + d if shown is None else
                                                     shown[n] if n < len(shown) else start + duration))
                except ValueError:
                    raise BrokenMedia(f"a stream {idx} frame has no finite timestamp or duration ({line!r})") from None
            near = {i: [(a, e) for a, e in seen.get(i, []) if a <= end and e >= t] for i in av}   # inside the window
            lost = [i for i in av if not near[i] or not _covers(near[i], t, end)]
            if lost:
                raise BrokenMedia(f"no frame decoded from stream {', '.join(lost)} at {t:g}-{t + 2:g}s")
            decoded.append(f"{t:g}-{t + 2:g}s")
    rate = a.get("sample_rate")
    return {"container": fmt.get("format_name"), "streams": kinds, "duration_s": duration,
            "fps_measured": round(fps, 4) if fps else None, "fps_tagged": _rate(v.get("r_frame_rate")),
            "audio_rate_hz": int(rate) if str(rate or "").isdigit() else None,
            "width": v.get("width"), "height": v.get("height"), "packets_read": "all", "decoded": decoded,
            "audio_gap_s": round(max(gaps), 6) if gaps and None not in gaps else None,
            "color": {k: v.get(k) for k in ("color_primaries", "color_transfer", "color_space")}}


def _covers(spans, t, end):
    """No stretch over 0.5 s from t to end is left without a decoded frame on screen or sound playing."""
    reach = t
    for a, e in sorted(spans):
        if a > reach + .5:
            return False
        reach = max(reach, e)
    return reach >= end - .5


def volume(path):
    """(mount point, free bytes) of the volume that holds `path` or, not yet made, its nearest existing folder."""
    p = os.path.realpath(path)   # a symlink's target volume, the one disk_usage reads
    while not os.path.exists(p):
        p = os.path.dirname(p)
    free = shutil.disk_usage(p).free
    while not os.path.ismount(p):
        p = os.path.dirname(p)
    return p, free


def now():
    return dt.datetime.now(dt.timezone.utc)
