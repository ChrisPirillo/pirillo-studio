"""The media catalog check bound to the spine engine (docs/preflight.md section 1, seam 3c).

checks() -> {"media": (version, fn(evidence) -> {"state", "words"})}, passed to engine.Video(checks=...). It runs no
tool and reads only the engine's evidence: records/probe.json must equal the projection of records/ready/media.json
for source.mp4 as the engine hashed them, and records/faces.json must parse. records/words.json is judged by its
reader, cutting's words().
"""
import json
from pathlib import Path

from ..spine.files import canon, digest
from . import probe

HERE = Path(__file__).parent


def version():
    """The check's code: an edited projection or check reopens every pass made under the old one."""
    return digest(b"".join(f.read_bytes() for f in sorted(HERE.iterdir())
                           if f.suffix in (".py", ".json") and not f.name.startswith("test_")))


def media(evidence):
    def read(p, parse=True):
        raw = Path(evidence[p]["path"]).read_bytes()
        if digest(raw) != evidence[p]["sha256"]:
            raise ValueError(f"{p} changed since the engine's evidence")
        try:
            return json.loads(raw) if parse else raw
        except ValueError as e:
            raise ValueError(f"{p}: {e}") from None
    try:
        want = probe.projection(read(probe.READY, False), evidence["source.mp4"]["sha256"])
        read("records/faces.json")
        have = read("records/probe.json")
        if not isinstance(have, dict):
            raise ValueError("records/probe.json is no object")
    except (OSError, ValueError, KeyError) as e:
        return {"state": "blocked", "words": [str(e)]}
    off = sorted(k for k in want.keys() | have.keys() if canon(have.get(k)) != canon(want.get(k)))
    return {"state": "blocked" if off else "pass",
            "words": [f"records/probe.json {k} is {have.get(k)!r}; {probe.READY} says {want.get(k)!r}" for k in off]}


def checks():
    return {"media": (version(), media)}
