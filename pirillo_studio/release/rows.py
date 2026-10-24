"""Outputs and rows from the manifest, publish, copy, the text post and the owner's edits (release.md section 1)."""
import math
import re

from ..spine.files import digest
from .inputs import COPY, EDITS, MANIFEST, PUBLISH, Refused, field, setting

_OUTPUT = re.compile(r"(YouTube|Vertical)/main\.(mp4|srt)|Shorts/([^/]+)\.(mp4|srt)")
_CLOCK = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})")
NOT_PLATFORMS = ("hook", "at", "cover_text")  # copy.json fields beside the platforms' words


def kind(output_id):
    return "short" if output_id.startswith("short-") else output_id


def outputs(members):
    """One output per delivered video in manifest order, each with its .srt sidecar; rendering's paths kept."""
    out, sidecars = {}, {}
    for p, sha in members:
        m = _OUTPUT.fullmatch(p)
        if not m:
            raise Refused(f"release.complete: {MANIFEST} member {p} is no output rendering delivers", "rendering")
        oid, ext = ({"YouTube": "long", "Vertical": "vertical"}[m[1]], m[2]) if m[1] else (f"short-{m[3]}", m[4])
        into = out if ext == "mp4" else sidecars
        if oid in into:
            raise Refused(f"release.complete: {MANIFEST} lists {p} twice", "rendering")
        into[oid] = {"id": oid, "file": p, "sha256": sha} if ext == "mp4" else {"file": p, "sha256": sha}
    for oid in sidecars.keys() - out.keys():
        raise Refused(f"release.complete: {sidecars[oid]['file']} has no video in {MANIFEST}", "rendering")
    for oid, o in out.items():
        if oid not in sidecars:
            raise Refused(f"release.complete: {o['file']} has no .srt sidecar in {MANIFEST}", "captions")
        o["sidecar"] = sidecars[oid]
    if not out:
        raise Refused(f"release.complete: {MANIFEST} delivers no video", "rendering")
    return list(out.values())


def seconds(t, at):
    m = _CLOCK.fullmatch(t) if isinstance(t, str) else None
    if not m:
        raise Refused(f"{at}: chapter time {t!r} is not m:ss", "packager")
    return int(m[1] or 0) * 3600 + int(m[2]) * 60 + int(m[3])


def chapters(publish):
    out = []
    for c in field(publish, "chapters", PUBLISH, "packager", list):
        if not isinstance(c, list) or len(c) != 2 or not isinstance(c[1], str) or not c[1].strip():
            raise Refused(f"{PUBLISH}: chapter {c!r} is not [time, name]", "packager")
        seconds(c[0], PUBLISH)
        out.append({"t": c[0], "name": c[1]})
    return out


def at_s(d, key, at, maker):
    """A required time in seconds: a finite number, not negative."""
    v = d.get(key) if isinstance(d, dict) else None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise Refused(f"{at}: {key} missing or not seconds", maker)
    return v


def disclosures(s, publish, chat):
    """The disclosure texts this video owes (release.disclosure): the chat's when a chat is shown, the AI one when
    fields.ai_label is set; each text lives in release.settings disclosure."""
    return ([setting(s, "disclosure.chat", str)] if chat else []) + \
        ([setting(s, "disclosure.ai", str)] if field(publish, "ai_label", PUBLISH, "packager", bool) else [])


def strings(d, key, at, maker):
    v = field(d, key, at, maker, list)
    if not all(isinstance(x, str) for x in v):
        raise Refused(f"{at}: {key} holds a non-text entry", maker)
    return v


def description(s, publish, chaps, chat):
    parts = []
    for part in setting(s, "description.parts", list):
        if part == "disclosure":
            parts += [" ".join(owed)] if (owed := disclosures(s, publish, chat)) else []
        elif part == "chapters":
            parts.append("\n".join(f"{c['t']} {c['name']}" for c in chaps))
        elif part == "footer":
            parts.append(setting(s, "description.footer", str))
        elif part == "hashtags":
            parts.append(" ".join(strings(publish, "hashtags", PUBLISH, "packager")))
        else:
            parts.append(field(publish, f"description_{part}", PUBLISH, "packager").strip())
    return "\n\n".join(parts)


def name(s, title):
    """The name a platform or a person is handed (release.naming); the delivered file keeps its path."""
    for a, b in setting(s, "naming.replace", dict).items():
        title = title.replace(a, b)
    drop = setting(s, "naming.drop", str)
    return re.sub(r"\s+", " ", "".join(ch for ch in title if ch not in drop)).strip().rstrip(".")


def group(copy, o):
    if o["id"] in ("long", "vertical"):
        return field(copy, o["id"], COPY, "packager", dict)
    return field(field(copy, "shorts", COPY, "packager", dict), o["id"][len("short-"):], COPY, "packager", dict)


def dress(ins, s, outs, publish, copy, titled, chat):
    """Each output's title, name, and its own fields: the long's description and publish fields, a cover."""
    lead = field(publish, "title", PUBLISH, "packager")
    for o in outs:
        words = group(copy, o)
        o["title"] = field(words, "hook", COPY, "packager") if titled and kind(o["id"]) == "short" else lead
        o["name"] = name(s, o["title"])
        if o["id"] == "long":
            chaps = chapters(publish)
            if titled:
                o["ab_titles"] = [field(t, "title", PUBLISH, "packager") for t in field(publish, "ab_titles", PUBLISH, "packager", list)]
            o["description"] = {"text": description(s, publish, chaps, chat)}
            o["chapters"] = chaps
            o["fields"] = {"tags": strings(publish, "tags", PUBLISH, "packager"),
                           "pinned_comment": field(publish, "pinned_comment", PUBLISH, "packager"),
                           "category": field(publish, "category", PUBLISH, "packager"),
                           "ai_label": field(publish, "ai_label", PUBLISH, "packager", bool),
                           "end_screen": {"start_s": at_s(publish, "end_screen_start_s", PUBLISH, "packager"),
                                          "video": field(publish, "end_screen_video", PUBLISH, "packager"),
                                          "playlist": field(publish, "end_screen_playlist", PUBLISH, "packager")},
                           "thumbnails": [{"file": p, "sha256": ins.file(p, "owner")}
                                          for p in strings(publish, "thumbnails", PUBLISH, "packager")]}
        else:
            cover = field(words, "cover_text", COPY, "packager", dict)
            o["cover"] = {"at_s": at_s(cover, "at_s", COPY, "packager"), "text": field(cover, "text", COPY, "packager"),
                          "why": field(cover, "why", COPY, "packager")}
    return outs


def latest(edits):
    """row ID -> its latest edit; release_edits.json is appended, so the last entry wins."""
    out = {}
    for e in field(edits, "edits", EDITS, "owner", list) if edits is not None else []:
        for k in ("row", "text", "by"):
            field(e, k, EDITS, "owner")
        out[e["row"]] = e
    return out


def rows(s, outs, copy, text, edits):
    """One row per output and mapped platform, plus the text post's; words are exactly what posting sends."""
    out, platforms = [], setting(s, "platforms", dict)
    for o in outs:
        words, mapped = group(copy, o), setting(s, f"map.{kind(o['id'])}", list)
        outside = sorted(p for p in words if p in platforms and p not in mapped)
        if outside:
            raise Refused(f"release.complete: {COPY} routes {o['id']} to {outside}, outside map.{kind(o['id'])}", "packager")
        for p in mapped:
            setting(s, f"platforms.{p}", dict)
            said = o["description"]["text"] if (o["id"], p) == ("long", "youtube") else words.get(p)
            if not isinstance(said, str) or p in NOT_PLATFORMS:
                raise Refused(f"release.complete: row {o['id']}:{p} has no words in {COPY}", "packager")
            out.append({"id": f"{o['id']}:{p}", "output": o["id"], "platform": p, "words": said, "by": "packager"})
    if text is not None:
        out += [{"id": f"article:{p}", "output": None, "platform": p, "words": text, "by": "writer"}
                for p in setting(s, "map.text", list)]
    edited, ids = latest(edits), {r["id"] for r in out}
    for rid in sorted(edited.keys() - ids):
        raise Refused(f"{EDITS}: edits row {rid}, which the release does not have", "owner")
    for r in out:
        if r["id"] in edited:
            r.update(words=edited[r["id"]]["text"], by=edited[r["id"]]["by"])
        r["words_sha256"] = digest(r["words"].encode())
        if r["id"] == "long:youtube":  # its words are the description, kept once
            next(o for o in outs if o["id"] == "long")["description"] = {"text": r.pop("words"), "sha256": r["words_sha256"]}
    return out


def words(row, outs):
    return row["words"] if "words" in row else next(o for o in outs if o["id"] == "long")["description"]["text"]
