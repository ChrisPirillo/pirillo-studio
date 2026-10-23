"""Resolvers and probes (docs/capabilities.md sections 1 and 2): what a declared capability is on this machine.

Everything is found on the host adapter's one search path (`search`, a list of absolute folders); executables run
with that path as their only PATH, under the setting `capabilities.probe_seconds`. A content hash is reused from the
capability's last record while the file's path, size, inode, mtime and change time are the same (preflight.md's
cache), else the file is hashed again with spine.files.filehash, never read whole. That install identity is part of
the key, so a same-content replacement is a new install. A probe's workload is the setting `capabilities.<probe>`
(WORKLOAD's fields), bound into the key; a fixture file it names is checked against its sha256 on every read.
"""
import datetime as dt
import math
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from ..spine.files import SpineError, canon, digest, filehash
from .declare import ROUTE, blank, version

PROBE_VERSIONS = {"encode-sample": "1", "present": "1", "speak-fixture": "1", "transcribe-fixture": "1"}
WORKLOAD = {"encode-sample": {"width": int, "height": int, "rate": int, "seconds": int},
            "speak-fixture": {"sentence": str, "min_seconds": float},
            "transcribe-fixture": {"audio": str, "sha256": str, "text": str}}
FIND_SPEC = "import importlib.util, sys; s = importlib.util.find_spec(sys.argv[1]); print(s.origin if s else '')"
SAID = re.compile(r"version\s+n?(\d+(?:\.\d+)*)")
FRAME = re.compile(r"\d+,")  # one framemd5 line per decoded frame; error lines never start with a number and comma
TB = re.compile(r"#tb 0: 1/(\d+)")
SHA = re.compile(r"sha256:[0-9a-f]{64}")
WORD = re.compile(r"[\w']+")


class Missing(Exception):
    """The item does not resolve: its words go into the `missing` answer beside the get line."""


def number(host, name):
    try:
        v = host.setting(name)
    except KeyError:
        raise SpineError(f"setting {name}: missing") from None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise SpineError(f"setting {name}: {v!r} is not a positive finite number")
    return v


def workload(host, e):
    """The validated setting `capabilities.<probe>`, or None for `present`; missing or invalid refuses naming it."""
    name = e["qualify"]
    if name == "present":
        return None
    s = f"capabilities.{name}"
    try:
        w = host.setting(s)
    except KeyError:
        raise SpineError(f"setting {s}: missing") from None
    want = WORKLOAD[name]
    ok = isinstance(w, dict) and set(w) == set(want) and all(
        (isinstance(w[k], str) and not blank(w[k])) if t is str else
        (not isinstance(w[k], bool) and isinstance(w[k], (int, float) if t is float else int)
         and math.isfinite(w[k]) and w[k] > 0) for k, t in want.items())
    if ok and name == "transcribe-fixture":
        ok = os.path.isabs(w["audio"]) and bool(SHA.fullmatch(w["sha256"])) and bool(WORD.findall(w["text"]))
    if not ok:
        raise SpineError(f"setting {s}: {w!r} needs exactly {sorted(want)}, positive numbers or non-empty text"
                         + (", an absolute audio path, its sha256 and words" if name == "transcribe-fixture" else ""))
    # checked on every read, not only when probing: a kept pass rests on these bytes
    if name == "transcribe-fixture" and not (os.path.isfile(w["audio"]) and filehash(Path(w["audio"])) == w["sha256"]):
        raise SpineError(f"setting {s}: {w['audio']} is not {w['sha256']}")
    return w


def search(host):
    dirs = host.search
    if not isinstance(dirs, list) or not dirs or not all(isinstance(d, str) and os.path.isabs(d) for d in dirs):
        raise SpineError(f"host search path {dirs!r}: expected a non-empty list of absolute folders")
    return dirs


def run(host, argv, limit):
    """(exit code, output) of argv with the search path as PATH; a timeout raises subprocess.TimeoutExpired."""
    p = subprocess.run([str(a) for a in argv], capture_output=True, text=True, errors="replace", timeout=limit,
                       stdin=subprocess.DEVNULL, env={"PATH": os.pathsep.join(search(host))})
    return p.returncode, p.stdout + p.stderr


def stamp(f):
    st = os.stat(f)
    return [st.st_size, st.st_ino, st.st_mtime_ns, st.st_ctime_ns]


def content(files, prior):
    """{real path: sha256} for files, each reused from prior's record while its stamp is unchanged."""
    hashes, stamps = prior.get("files", {}), prior.get("stat", {})
    out, st = {}, {}
    for f in files:
        k = str(f)
        try:
            st[k] = stamp(f)
        except FileNotFoundError:
            raise Missing(f"{f} is gone") from None
        out[k] = hashes[k] if stamps.get(k) == st[k] and k in hashes else filehash(f)
    return out, st


def where(host, name, folder=False):
    for d in search(host):
        p = Path(d) / name
        if (p.is_dir() if folder else p.is_file() and os.access(p, os.X_OK)):
            return Path(os.path.realpath(p))  # an alias runs its target, so the target's bytes are the key
    raise Missing(f"{name} is not on the studio search path")


def tree(folder):
    files = sorted(Path(os.path.realpath(f)) for f in folder.rglob("*") if not f.is_dir())
    if not files:
        raise Missing(f"{folder} holds no files")
    return files


def resolve(host, e, prior, needs):
    """The resolved item as the record keeps it; Missing when it does not resolve or is under version.min."""
    limit = number(host, "capabilities.probe_seconds")
    kind = e["kind"]
    if kind == "encoder":
        return {"path": ffmpeg(e, needs), "codec": e["codec"]}
    if kind == "account":
        p = ROUTE.fullmatch(e["routes"]).group(1)
        days = number(host, f"platforms.{p}.token_days")
        saved = host.key_saved(p)  # presence and its date only: the value stays the posting adapter's
        if saved is None:
            raise Missing(f"no key is saved for {p}")
        try:
            age = (host.now() - dt.datetime.fromisoformat(saved.replace("Z", "+00:00"))).total_seconds() / 86400
        except (AttributeError, TypeError, ValueError) as x:
            raise SpineError(f"saved date of the {p} key: {saved!r} ({x})") from None
        if age >= days:
            raise Missing(f"the {p} key is {age:.0f} days old; its token life is {days} days")
        return {"present": True, "saved": saved, "token_days": days}
    if kind == "guide":
        folder = where(host, e["find"], folder=True)
        files, st = content(tree(folder), prior)
        return {"path": str(folder), "sha256": digest(canon(files)), "files": files, "stat": st}
    exe = where(host, e["find"] if kind == "executable" else e["runtime"])
    files, st = content([exe], prior)
    out = {"path": str(exe), "sha256": files[str(exe)]}
    if kind == "executable":
        try:
            code, said = run(host, [exe] + e["version"]["args"], limit)
        except (OSError, subprocess.TimeoutExpired) as x:
            raise Missing(f"{exe} {' '.join(e['version']['args'])} did not answer ({x})") from None
        m = SAID.search(said)
        if code or not m:
            raise Missing(f"{exe} gave no readable version (exit {code}: {said.strip()[:80]!r})")
        if version(m.group(1)) < version(e["version"]["min"]):
            raise Missing(f"{exe} is version {m.group(1)}, under {e['version']['min']}")
        return dict(out, version=m.group(1), files=files, stat=st)
    try:
        code, origin = run(host, [exe, "-I", "-c", FIND_SPEC, e["package"]], limit)
    except (OSError, subprocess.TimeoutExpired) as x:
        raise Missing(f"{exe} could not look for {e['package']} ({x})") from None
    origin = origin.strip()
    if code or not origin or not os.path.isfile(origin):
        raise Missing(f"{e['package']} is not installed in {exe}")
    weights = tree(where(host, e["weights"], folder=True))
    files, st = content([exe, Path(origin)] + weights, prior)
    out = dict(out, package=origin, weights=digest(canon({str(w): files[str(w)] for w in weights})), files=files,
               stat=st)
    return dict(out, measure=ffmpeg(e, needs)) if e["qualify"] == "speak-fixture" else out


def ffmpeg(e, needs):
    """The path of the executable among e's needs (declare.py makes sure there is one)."""
    return next(needs[n]["path"] for n in e["needs"] if "version" in needs[n])


def key(cv, e, resolved, needs_keys, work):
    """sha256 over the entry, capabilities_version, what resolved (content, version and install identity: path, size,
    inode, change time), the probe, its workload and the needs' keys."""
    probe = [e["qualify"], PROBE_VERSIONS[e["qualify"]], work]
    return digest(canon({"entry": e, "capabilities_version": cv, "probe": probe, "needs": needs_keys,
                         "resolved": resolved}))


def probe(host, e, resolved, work):
    """Run the declared probe once under the time limit: (ok, words). A timeout or an OS error is a failed probe."""
    name = e["qualify"]
    if name == "present":
        return True, "present, keyed by its content" if e["kind"] == "guide" else "key present inside its token life"
    limit = number(host, "capabilities.probe_seconds")
    deadline = time.monotonic() + limit
    left = lambda: max(deadline - time.monotonic(), 0.001)
    with tempfile.TemporaryDirectory() as t:
        try:
            return PROBES[name](host, e, resolved, work, Path(t), left)
        except subprocess.TimeoutExpired:
            return False, f"no answer inside the {limit} s time limit"
        except OSError as x:
            return False, f"could not run {resolved['path']}: {x}"


def encode_sample(host, e, resolved, work, t, left):
    w, h, rate, secs = (work[k] for k in ("width", "height", "rate", "seconds"))
    out = t / "sample.mkv"
    code, said = run(host, [resolved["path"], "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                            f"testsrc2=size={w}x{h}:rate={rate}:duration={secs}", "-c:v", e.get("codec", "ffv1"),
                            "-y", out], left())
    if code:
        return False, f"exit {code} encoding a {secs} s {w}x{h} sample: {said.strip()[-200:]}"
    code, said = run(host, [resolved["path"], "-v", "error", "-nostdin", "-i", out, "-map", "0:v", "-f", "framemd5",
                            "-"], left())
    frames = sum(1 for line in said.splitlines() if FRAME.match(line))
    if code or frames != rate * secs:
        return False, f"exit {code}; decode of the sample read {frames} of {rate * secs} frames"
    return True, f"encoded and decoded {rate * secs} frames"


def engine(e, resolved, **fill):
    """The declared engine command: the resolved runtime, then e's run args with their {placeholders} filled."""
    return [resolved["path"]] + [a.format(**fill) for a in e["run"]]


def speak_fixture(host, e, resolved, work, t, left):
    out = t / "speech.wav"
    code, said = run(host, engine(e, resolved, text=work["sentence"], out=out), left())
    if code or not out.is_file():
        return False, f"exit {code}; the engine wrote no {out.name}: {said.strip()[-200:]}"
    code, said = run(host, [resolved["measure"], "-v", "error", "-nostdin", "-i", out, "-map", "0:a:0", "-f",
                            "framemd5", "-"], left())
    tb = TB.search(said)
    secs = sum(int(line.split(",")[3]) for line in said.splitlines() if FRAME.match(line)) / int(tb.group(1)) \
        if tb and not code else 0
    if secs <= work["min_seconds"]:
        return False, f"exit {code}; the spoken fixture decoded as {secs:.2f} s of audio, not over {work['min_seconds']} s"
    return True, f"spoke the fixture sentence: {secs:.2f} s of audio"


def transcribe_fixture(host, e, resolved, work, t, left):
    code, said = run(host, engine(e, resolved, audio=work["audio"]), left())
    want, heard = WORD.findall(work["text"].lower()), iter(WORD.findall(said.lower()))
    if code or not all(w in heard for w in want):  # each expected word found after the last: in order
        return False, f"exit {code}; the engine's words do not hold {work['text']!r} in order: {said.strip()[-200:]!r}"
    return True, f"heard the fixture's {len(want)} words in order"


PROBES = {"encode-sample": encode_sample, "speak-fixture": speak_fixture, "transcribe-fixture": transcribe_fixture}
