"""What build reads: every input hashed as it is read, so the key covers exactly the bytes used."""
import datetime as dt
import json
import re

from ..spine.engine import atomic
from ..spine.files import SpineError, canon, digest, filehash, finite, inside, safe

MANIFEST, PUBLISH, COPY, COPY_READ = ("records/final_manifest.json", "records/publish.json", "records/copy.json",
                                      "records/copy_review.md")
TEXT, TEXT_READ, WORDS, EDITS = ("records/text_post.md", "records/text_review.md", "records/words.json",
                                 "records/release_edits.json")
CUES = "records/captions/long.json"
RELEASE, REVIEW, DECISION, ENVELOPE = ("records/release.json", "Review.md", "records/release_decision.json",
                                       "records/checks/release.json")
NAMED = {"manifest": MANIFEST, "publish": PUBLISH, "copy": COPY, "copy_read": COPY_READ, "text_post": TEXT,
         "text_read": TEXT_READ, "edits": EDITS}
_NOT_READ = re.compile(r"DOESN[’']T READ")


class Unavailable(SpineError):
    """A measure cannot be taken: an input, setting or adapter is missing or broken (flowchart unavailable end)."""


class Refused(SpineError):
    """A maker's input breaks a rule; `maker` fixes it (flowchart refused end)."""

    def __init__(self, what, maker):
        super().__init__(what)
        self.maker = maker


def settings(video):
    try:
        s = video.settings("release.settings")
        canon(s)  # refuses NaN and infinity anywhere in the profile
    except Exception as e:
        raise Unavailable(f"release.settings: unavailable ({e})") from None
    if not isinstance(s, dict):
        raise Unavailable("release.settings: not a profile")
    return s


def setting(s, name, kind=None):
    """s[name] by dotted name; missing, or not of kind, is unavailable naming the setting."""
    v = s
    for part in name.split("."):
        if not isinstance(v, dict) or part not in v:
            raise Unavailable(f"release.settings {name}: missing")
        v = v[part]
    if kind is float:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise Unavailable(f"release.settings {name}: not a number")
    elif kind is not None and not isinstance(v, kind):
        raise Unavailable(f"release.settings {name}: not a {kind.__name__}")
    return v


def field(d, key, at, maker, kind=str):
    """A maker record's required field; missing or the wrong type refuses naming it and its maker."""
    if not isinstance(d, dict) or key not in d or not isinstance(d[key], kind) or isinstance(d[key], bool) != (kind is bool):
        raise Refused(f"{at}: {key} missing or not {kind.__name__}", maker)
    return d[key]


def stages(video):
    """(the release stage, the stages whose check is `titles`): the stage data a release depends on, bound in its key."""
    try:
        listed = video.recipe()[1]["stages"]
        return next(s for s in listed if s["id"] == "release"), [s for s in listed if "titles" in s["check"]]
    except Exception as e:
        raise Unavailable(f"recipe: no release stage ({e})") from None


def day(video):
    """The build's one evaluation day (YYYY-MM-DD) from the adapter's now(), bound in the key; every dated reading
    counts to it. A malformed now() is unavailable."""
    try:
        return dt.date.fromisoformat(video.now()[:10]).isoformat()
    except Exception as e:
        raise Unavailable(f"now: not a day ({e})") from None


FACTS = {"chat_shown": bool, "search_phrases": list, "topics": dict}


def facts(video):
    """The host adapter's facts a reading needs (package docstring); absent or broken is unavailable naming it."""
    out = {}
    for name, kind in FACTS.items():
        f = getattr(video, name, None)
        if f is None:
            raise Unavailable(f"{name}: the host supplies none")
        try:
            v = f()
            canon(v)
        except Exception as e:
            raise Unavailable(f"{name}: unavailable ({e})") from None
        if not isinstance(v, kind) or isinstance(v, bool) != (kind is bool):
            raise Unavailable(f"{name}: not a {kind.__name__}")
        out[name] = v
    if not all(isinstance(x, str) and x.strip() for x in out["search_phrases"]):
        raise Unavailable("search_phrases: an entry is not text")
    if not all(isinstance(d, str) for d in out["topics"].values()):
        raise Unavailable("topics: a last-seen day is not text")
    return out


class Inputs:
    """Reads one video's inputs, recording each path's content hash in `reads`."""

    def __init__(self, video):
        self.video, self.reads = video, {}

    def path(self, p):
        safe(p, "release input")
        return inside(self.video.folder, p)

    def raw(self, p):
        try:
            raw = self.path(p).read_bytes()
        except FileNotFoundError:
            raise Unavailable(f"{p}: missing") from None
        except OSError as e:
            raise Unavailable(f"{p}: unreadable ({e})") from None
        self.reads[p] = digest(raw)
        return raw

    def json(self, p):
        raw = self.raw(p)
        try:
            return json.loads(raw, parse_constant=finite)
        except ValueError as e:
            raise Unavailable(f"{p}: unreadable ({e})") from None

    def text(self, p):
        return self.raw(p).decode()

    def file(self, p, maker):
        """A delivered or supplied file, hashed in blocks, never read whole."""
        h = filehash(self.path(p))
        if h is None:
            raise Refused(f"{p}: missing", maker)
        self.reads[p] = h
        return h

    def members(self):
        """The final manifest's members, in order, each present with the bytes the manifest names."""
        m = self.json(MANIFEST)
        members = field(m, "members", MANIFEST, "rendering", list)
        for x in members:
            p, sha = field(x, "path", MANIFEST, "rendering"), field(x, "sha256", MANIFEST, "rendering")
            if self.file(p, "rendering") != sha:
                raise Refused(f"{p}: its bytes differ from {MANIFEST}", "rendering")
        return [(x["path"], x["sha256"]) for x in members]

    def read(self, p, of):
        """An independent read bound by hash to the bytes of `of`; a DOESN'T READ row refuses."""
        body = self.text(p)
        want = f"reads: {of} {self.reads[of]}"
        if want not in body.splitlines():
            raise Refused(f"{p}: stale, it does not read the current {of} ({self.reads[of]})", "story-editor")
        rows = [line.strip() for line in body.splitlines() if line.lstrip().startswith("|") and _NOT_READ.search(line)]
        if rows:
            raise Refused(f"{p}: {len(rows)} row(s) DOESN'T READ: {rows[0][:90]}", "packager" if of == COPY else "writer")


def current(video, reads):
    """The present hash of every recorded input; a record changed, gone or appeared differs."""
    out = {}
    for p in reads:
        f = inside(video.folder, p)
        out[p] = filehash(f) if f.exists() else None
    return out


def key(reads, s, **bound):
    """The content key; QC's envelope key is reads and settings alone, the release's also binds `bound`."""
    return digest(canon({"reads": reads, "settings": s, **bound}))


def write(video, p, text):
    """Beside then renamed (spine atomic); unchanged bytes are not written."""
    f = inside(video.folder, p)
    if f.exists() and f.read_text() == text:
        return False
    f.parent.mkdir(parents=True, exist_ok=True)
    atomic(f, text)
    return True


def dump(x):
    return json.dumps(x, indent=1, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
