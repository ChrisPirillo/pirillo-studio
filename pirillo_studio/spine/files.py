"""Content hashing and safe paths for the spine: a file's bytes are its version (docs/spine.md section 4)."""
import hashlib
import json
import unicodedata
from pathlib import PurePosixPath

_memo = {}


class SpineError(ValueError):
    """Bad recipe data, an unknown recipe, an unreadable record or an operation next does not allow."""


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def canon(x):
    return json.dumps(x, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def safe(p, at):
    q = PurePosixPath(p) if isinstance(p, str) else None
    if q is None or not p or q.is_absolute() or ".." in q.parts or "\\" in p or q.as_posix() != p:  # one spelling
        raise SpineError(f"{at}: unsafe path {p!r}")


def fold(p):
    """The one file a path names on a disk that ignores case and Unicode form (APFS)."""
    return unicodedata.normalize("NFD", p).casefold()


def inside(folder, p):
    """folder / p, refused when a component under the video folder is a symlink: an alias would let a maker's file
    stand for an engine or owner record."""
    f = folder
    for part in PurePosixPath(p).parts:
        f = f / part
        if f.is_symlink():
            raise SpineError(f"{folder / p}: {part} is a symlink")
    return f


def finite(c):
    raise ValueError(f"non-finite number {c}")


def load(folder, p):
    return json.loads(inside(folder, p).read_text(), parse_constant=finite)


def hashes(v, paths):
    """path -> content hash, each manifest's members included; a missing file, or a member whose bytes differ from
    its manifest, is None."""
    out = {}
    for p in paths:
        out[p] = filehash(inside(v.folder, p))
        if out[p] and p.endswith("_manifest.json"):
            try:
                for m in load(v.folder, p)["members"]:
                    safe(m["path"], p)
                    out[m["path"]] = m["sha256"] if filehash(inside(v.folder, m["path"])) == m["sha256"] else None
            except (ValueError, KeyError, TypeError) as e:
                raise SpineError(f"{v.folder / p}: unreadable manifest ({e})") from None
    return out


def filehash(f):
    try:
        st = f.stat()
    except FileNotFoundError:
        return None
    key = (str(f), st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)
    if key not in _memo:  # a source is hashed once per process; any write changes ctime
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        _memo[key] = "sha256:" + h.hexdigest()
    return _memo[key]
