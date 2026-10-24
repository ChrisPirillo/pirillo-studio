"""Cue grouping, placement and the two files made from the same cues (docs/captions.md contracts 3 to 5).
Lead decision 6: a cue never holds two pieces' words; it closes on pause_s, a sentence end, max_words, max_chars,
max_s and (tall) a measured width over the safe width; it ends at its last word, raised to min_cue_s only within the
next cue's start, its piece's end and the file's duration, else it refuses (never shrunk); a completed cue still
over max_chars or max_s, or outside its piece or the file, refuses.
Lead decision 7: each piece takes the first of the profile's bands clear of every element active during it.
Lead decision 8: one line per cue, the transcript's words as written; each word lit from its start to the next
word's start, the last to the cue end; times are kept to the centisecond, the burn layer's own precision, rounded
inward, so the .srt and the .ass carry identical times; a cue with any word whose heard or written edge crosses its
unrounded piece or file refuses (outside_piece), and a lit interval collapsed by rounding refuses (word_interval)."""
import re

SENT_END = re.compile(r"[.!?…][\"')\]”]*$")
_STYLE = ("Style: {name},{font},{size},{primary},{secondary},{outline_colour},{back},-1,0,0,0,100,100,0,0,1,"
          "{outline},{shadow},{align},{left},{right},{margin},1")


def line(tokens):
    return " ".join(t["text"] for t in tokens)


def refusal(cue, reading, value, setting, route="captions"):
    owner = "captions.settings" if route == "captions" else "item 17"
    return {"cue": cue, "reading": reading, "value": value, "setting": setting, "owner": owner, "route": route}


def group(tokens, name, profile, pieces, duration, measure, safe_width):
    """tokens on the output clock, in order -> (cues, refusals); measure is None when width is not measured."""
    groups, current = [], []
    for token in tokens:
        if current:
            trial = current + [token]
            if (token["piece"] != current[-1]["piece"] or token["start"] - current[-1]["end"] > profile["pause_s"]
                    or SENT_END.search(current[-1]["text"]) or len(trial) > profile["max_words"]
                    or len(line(trial)) > profile["max_chars"] or token["end"] - current[0]["start"] > profile["max_s"]
                    or (measure is not None and measure(line(trial)) > safe_width)):
                groups.append(current)
                current = []
        current.append(token)
    groups.append(current)
    cues, refusals = [], []
    for i, words in enumerate(groups):
        cid, start, end = f"c{i + 1}", words[0]["start"], words[-1]["end"]
        later = groups[i + 1] if i + 1 < len(groups) else None
        piece_start, piece_end = pieces[words[0]["piece"]]["out"]
        limit = min(later[0]["start"] if later else duration, piece_end, duration)
        if end - start < profile["min_cue_s"]:
            raised = round(start + profile["min_cue_s"], 2)
            if raised <= limit:
                end = raised
            else:
                refusals.append(refusal(cid, "min_cue_s", round(end - start, 2), f"{name}.min_cue_s"))
        # every word of a completed cue, as heard and as written, inside its unrounded piece and file
        edges = [t for w in words for t in (w["start"], w["end"], *w["at"])] + [end]
        if min(edges) < piece_start or max(edges) > min(piece_end, duration):
            refusals.append(refusal(cid, "outside_piece", [min(edges), max(edges)], "frame_map"))
        if len(line(words)) > profile["max_chars"]:
            refusals.append(refusal(cid, "max_chars", len(line(words)), f"{name}.max_chars"))
        if round(end - start, 2) > profile["max_s"]:
            refusals.append(refusal(cid, "max_s", round(end - start, 2), f"{name}.max_s"))
        starts = [w["start"] for w in words[1:]] + [end]
        lit = [[w["start"], b] for w, b in zip(words, starts)]
        refusals += [refusal(cid, "word_interval", {"word": w["ids"][0], "lit": span}, "frame_map")
                     for w, span in zip(words, lit) if span[1] <= span[0]]  # two starts in one centisecond
        cues.append({"id": cid, "piece": words[0]["piece"], "start_s": start, "end_s": end,
                     "words": [i for w in words for i in w["ids"]], "lines": [line(words)],
                     "breaks_at": piece_end if not later or later[0]["piece"] != words[0]["piece"] else None,
                     "lit": lit,
                     "text": [w["text"] for w in words]})
    for a, b in zip(cues, cues[1:]):
        if a["end_s"] - b["start_s"] > profile["overlap_tol_s"]:
            refusals.append(refusal(b["id"], "overlaps", round(a["end_s"] - b["start_s"], 3), f"{name}.overlap_tol_s"))
    return cues, refusals


def bands(settings):
    """Band name -> [x, y, w, h] on the frame: one block of the font size plus outline, inside safe_tall."""
    tall, safe, frame = settings["tall"], settings["safe_tall"], settings["frame"]
    high, x, width = tall["size"] + tall["outline"], safe["left"], frame["width"] - safe["left"] - safe["right"]
    return {"bottom": [x, frame["height"] - safe["bottom"] - high, width, high], "top": [x, safe["top"], width, high]}


def _meets(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def place(cues, pieces, elements, settings, measure):
    """Give every cue its piece's band and measured width; refusals for a piece with no clear band or a wide line."""
    rects, refusals, chosen = bands(settings), [], {}
    safe_width = rects["bottom"][2]
    for cue in cues:
        index = cue["piece"]
        if index not in chosen:
            out = pieces[index]["out"]
            active = [e["box"] for e in elements if e["start_s"] < out[1] and e["end_s"] > out[0]]
            chosen[index] = next((b for b in settings["tall"]["bands"]
                                  if not any(_meets(rects[b], box) for box in active)), None)
            if chosen[index] is None:
                refusals.append(refusal(cue["id"], "band", {"piece": index, "span": pieces[index]["span"]},
                                        "tall.bands"))
        cue["band"] = chosen[index]
        cue["width_px"] = max(measure(text) for text in cue["lines"])
        if cue["width_px"] > safe_width:
            refusals.append(refusal(cue["id"], "outside_safe", cue["width_px"], "safe_tall"))
    return refusals


def _srt_time(t):
    ms = round(t * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def srt(cues):
    return "".join(f"{i}\n{_srt_time(c['start_s'])} --> {_srt_time(c['end_s'])}\n{chr(10).join(c['lines'])}\n\n"
                   for i, c in enumerate(cues, 1)).encode()


def _ass_time(t):
    cs = round(t * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _escape(text):
    return text.replace("\\", "\\⁠").replace("{", "\\{").replace("}", "\\}")


def ass(cues, settings):
    """The burn layer: one Dialogue per lit interval, that word in the highlight colour."""
    tall, safe, frame = settings["tall"], settings["safe_tall"], settings["frame"]
    styles = [_STYLE.format(name=name, align=align, margin=margin, left=safe["left"], right=safe["right"], **{
        k: tall[k] for k in ("font", "size", "outline", "shadow", "primary", "secondary", "outline_colour", "back")})
        for name, align, margin in (("bottom", 2, safe["bottom"]), ("top", 8, safe["top"]))]
    head = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {frame['width']}", f"PlayResY: {frame['height']}",
            "WrapStyle: 2", "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
            "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding", *styles, "", "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    events = []
    for cue in cues:
        for k, (a, b) in enumerate(cue["lit"]):
            text = " ".join(f"{{\\c{tall['highlight']}}}{_escape(w)}{{\\r}}" if j == k else _escape(w)
                            for j, w in enumerate(cue["text"]))
            events.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},{cue['band']},,0,0,0,,{text}")
    return ("\n".join(head + events) + "\n").encode()
