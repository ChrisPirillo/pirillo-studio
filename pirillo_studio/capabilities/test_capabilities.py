"""docs/capabilities.md section 5, contracts 1 to 4, 8, 9, 11 and 12, with the seam's boundary probes; contracts 5, 6,
7 and 10 are in test_voice.py. Contract 13, test_off_is_unchanged, is seam 5's (the host switch) and is not here.

Every case reads the written records, the fake Board's asks and the fake tools' call log, never a message. Fake
tools are /bin/sh scripts in a temp dir (the speech and recognition engines are a fake `python`); no real ffmpeg or
model runs. More probe cases (model probes, install identity, workloads, concurrent requalify) are test_probes.py's. No preflight code is in the core yet (item 6),
so these tests stand in for its tools probe: capability, then qualify for an unqualified current key.
"""
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pirillo_studio.capabilities import answers, resolve
from pirillo_studio.capabilities.answers import capability, qualify, requalify
from pirillo_studio.capabilities.selected import status
from pirillo_studio.spine.files import SpineError, filehash

CORE = Path(__file__).resolve().parents[2]
SECRET = "tok-never-read-0042"
FFMPEG = """#!/bin/sh
echo "$*" >> "{log}"
read mode < "{root}/mode"
case "$*" in
  -version) read v < "{root}/version"; echo "ffmpeg version $v Copyright"; exit 0;;
  *lavfi*) [ "$mode" = slow ] && /bin/sleep 5
    [ "$mode" = refuse ] && {{ echo "encoder refused" >&2; exit 187; }}
    for last; do :; done; : > "$last"; exit 0;;
  *0:a:0*) read n < "$5"; echo "#tb 0: 1/16000"; i=0
    while [ $i -lt $n ]; do echo "0, $i, $i, 16000, 32000, abc"; i=$((i+1)); done; exit 0;;
  *framemd5*) n=0; [ "$mode" = short ] && n=50
    while [ $n -lt 60 ]; do echo "0, $n, $n, 1, 10, abc"; n=$((n+1)); done; exit 0;;
esac
exit 1
"""
LOGGER = '#!/bin/sh\necho "$*" >> "{log}"\n{out}'
ENGINE = """#!/bin/sh
echo "$*" >> "{log}"
case "$1" in
  -I) echo "{root}/site/$4.py";;
  speak) read n < "{root}/speak"; [ "$n" = none ] || echo "$n" > "$3";;
  hear) read t < "{root}/heard"; echo "$t";;
esac
"""
DECL = {"capabilities_version": "1", "capabilities": {
    "ffmpeg": {"kind": "executable", "find": "ffmpeg", "version": {"args": ["-version"], "min": "7.0"},
               "qualify": "encode-sample", "get": "install ffmpeg"},
    "encoder:h264-hardware": {"kind": "encoder", "needs": ["ffmpeg"], "codec": "h264_videotoolbox",
                              "qualify": "encode-sample", "get": "use a hardware ffmpeg"},
    "tts:engine/model": {"kind": "model", "runtime": "python", "package": "speakpkg", "weights": "speak-weights",
                         "needs": ["ffmpeg"], "run": ["speak", "{text}", "{out}"], "qualify": "speak-fixture",
                         "get": "install the speech engine"},
    "asr:engine/model": {"kind": "model", "runtime": "python", "package": "hearpkg", "weights": "hear-weights",
                         "run": ["hear", "{audio}"], "qualify": "transcribe-fixture", "get": "install the recognizer"},
    "guide:writing-voice": {"kind": "guide", "find": "writing-voice", "qualify": "present", "get": "install the guide"},
    "account:example": {"kind": "account", "routes": "settings platforms.example", "qualify": "present",
                        "get": "save the key"}}}


class Host:
    """The host adapter (answers.py's docstring): a fake Board and settings over a temp root, rebuilt from disk."""

    def __init__(self, root, fresh=True):
        self.root, self.bin, self.lib = root, root / "bin", root / "lib"
        self.state, self.studio, self.declarations = root / "state", root / "studio", root / "capabilities.json"
        self.search, self.log = [str(self.bin), str(self.lib)], root / "calls.log"
        fixture = root / "fixture.wav"
        self.settings = {"capabilities.probe_seconds": 5, "platforms.example.token_days": 60,
                         "capabilities.encode-sample": {"width": 3840, "height": 2160, "rate": 30, "seconds": 2},
                         "capabilities.speak-fixture": {"sentence": "A short test line.", "min_seconds": 1.5},
                         "capabilities.transcribe-fixture": {"audio": str(fixture), "sha256": "", "text": "One two."}}
        self.asks, self.decisions, self.saved, self.secret_reads = {}, {}, {"example": "2026-10-01T00:00:00Z"}, []
        if fresh:
            for d in (self.bin, self.lib / "writing-voice", self.lib / "speak-weights", self.lib / "hear-weights",
                      self.studio, root / "site"):
                d.mkdir(parents=True)
            self.declare(DECL)
            self.put("mode", "ok")
            self.put("version", "7.1")
            self.tool("ffmpeg", FFMPEG.format(log=self.log, root=root))
            self.tool("python", ENGINE.format(log=self.log, root=root))
            for name in ("brew", "pip", "curl", "post"):  # installers and a posting call: their log stays empty
                self.tool(name, LOGGER.format(log=root / "installs.log", out=""))
            for name, text in (("site/speakpkg.py", ""), ("site/hearpkg.py", ""), ("lib/speak-weights/model.bin", "w"),
                               ("lib/hear-weights/model.bin", "h"), ("speak", "3"), ("heard", "um one, uh, two"),
                               ("fixture.wav", "fixture audio")):
                self.put(name, text)
            self.put("lib/writing-voice/guide.md", "write plainly")
        self.settings["capabilities.transcribe-fixture"]["sha256"] = filehash(fixture)

    def put(self, rel, text):
        (self.root / rel).write_text(text)

    def tool(self, name, text):
        self.put(f"bin/{name}", text)
        os.chmod(self.bin / name, 0o755)

    def declare(self, d):
        self.declarations.write_text(json.dumps(d))

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def setting(self, name):
        return self.settings[name]

    def key_saved(self, platform):
        return self.saved.get(platform)

    @property
    def secret(self):  # the key's value: the domain must never touch it
        self.secret_reads.append("read")
        return SECRET

    def now(self):
        return dt.datetime(2026, 10, 23, 12, tzinfo=dt.timezone.utc)

    def ask(self, task):
        self.asks[task["id"]] = task

    def decided(self, task_id):
        return self.decisions.get(task_id)

    def href(self, path):
        return f"/served/{Path(path).name}"


def record(h, cid):
    return json.loads((h.state / "capabilities" / f"{cid}.json").read_text())


def tools_probe(h, cid):
    """Preflight's tools probe as capabilities.md section 3 calls it: capability, then qualify once for a new key."""
    a = capability(h, cid)
    return qualify(h, cid) if a["state"] == "unqualified" and a["next_action"] == f"qualify {cid}" else a


class Case(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.h = Host(Path(os.path.realpath(t.name)))

    def encodes(self):
        return [c for c in self.h.calls() if "lavfi" in c]


class Contracts(Case):
    """guard: capabilities.md section 5 contracts 1 to 4, 8, 9, 11 and 12, the domain's owner expectations."""

    def test_qualified_capability_passes(self):
        h = self.h
        rec = tools_probe(h, "ffmpeg")
        self.assertEqual((rec["state"], rec["ok"], rec["attempt"], rec["requalify"]), ("qualified", True, 1, None))
        self.assertEqual(record(h, "ffmpeg"), rec)
        self.assertEqual(rec["resolved"]["version"], "7.1")
        self.assertEqual(len(self.encodes()), 1)
        again = tools_probe(h, "ffmpeg")
        self.assertEqual((again["state"], again["ok"], again["key"]), ("qualified", True, rec["key"]))
        self.assertEqual(len(self.encodes()), 1)  # a second call runs no probe
        enc = tools_probe(h, "encoder:h264-hardware")
        self.assertEqual(enc["needs"], {"ffmpeg": rec["key"]})
        self.assertIn("-c:v h264_videotoolbox", self.encodes()[-1])
        self.assertEqual(h.asks, {})

    def test_missing_names_how_to_get_it(self):
        h = self.h
        cases = [("ffmpeg", lambda: os.unlink(h.bin / "ffmpeg")), ("ffmpeg", lambda: h.put("version", "6.9.2")),
                 ("tts:engine/model", lambda: os.unlink(h.lib / "speak-weights" / "model.bin")),
                 ("guide:writing-voice", lambda: os.unlink(h.lib / "writing-voice" / "guide.md")),
                 ("encoder:h264-hardware", lambda: os.unlink(h.bin / "ffmpeg"))]
        for cid, gone in cases:
            with self.subTest(cid=cid):
                self.setUp()
                h = self.h
                if cid == "tts:engine/model":
                    tools_probe(h, "ffmpeg")  # its probe's prerequisite
                n = len(h.calls())
                gone()
                a = tools_probe(h, cid)
                self.assertEqual((a["state"], a["ok"], a["capability"], a["next_action"]),
                                 ("missing", False, cid, DECL["capabilities"][cid]["get"]))
                self.assertFalse((h.state / "capabilities" / f"{cid}.json").exists())
                self.assertEqual(([c for c in h.calls()[n:] if "lavfi" in c or c.startswith("speak")],
                                  (h.root / "installs.log").exists()), ([], False))

    def test_failed_probe_is_unqualified(self):
        for mode in ("refuse", "short", "slow", "raise"):
            with self.subTest(mode=mode):
                self.setUp()
                h = self.h
                tools_probe(h, "ffmpeg")
                n = len(self.encodes())
                h.put("mode", mode if mode != "raise" else "ok")
                h.settings["capabilities.probe_seconds"] = 0.5 if mode == "slow" else 5
                real = resolve.run
                boom = lambda host, argv, limit: (_ for _ in ()).throw(RuntimeError("probe broke")) \
                    if "lavfi" in argv else real(host, argv, limit)
                with mock.patch.object(resolve, "run", boom if mode == "raise" else real):
                    rec = tools_probe(h, "encoder:h264-hardware")
                self.assertEqual((rec["state"], rec["ok"], rec["probe"]["ok"], rec["probe"]["name"]),
                                 ("unqualified", False, False, "encode-sample"))
                self.assertEqual(record(h, "encoder:h264-hardware"), rec)
                a = capability(h, "encoder:h264-hardware")
                self.assertEqual((a["state"], a["ok"]), ("unqualified", False))
                self.assertEqual(list(h.asks), [answers.task_id("encoder:h264-hardware", rec["key"])])
                task = h.asks[answers.task_id("encoder:h264-hardware", rec["key"])]
                self.assertEqual((task["source"]["filed_by"], task["actions"][0]["verb"]), ("capabilities", "try-again"))
                self.assertEqual(len(self.encodes()), n + (mode != "raise"))
                self.assertTrue(all("h264_videotoolbox" in c for c in self.encodes()[n:]))  # nothing substituted
        h = self.h
        f = h.state / "capabilities" / "encoder:h264-hardware.json"
        for torn in ('{"capability": "encoder:h264-hardware", "key": ', '{"capability": "ffmpeg"}', "[]"):
            f.write_text(torn)
            a = capability(h, "encoder:h264-hardware")
            self.assertEqual((a["state"], a["ok"]), ("unqualified", False))
            self.assertIn(str(f), a["detail"])

    def test_new_install_requalifies_once(self):
        h = self.h
        old = tools_probe(h, "ffmpeg")
        changes = [lambda: h.put("version", "7.2"),
                   lambda: h.tool("ffmpeg", (h.bin / "ffmpeg").read_text() + "# rebuilt\n"),
                   lambda: h.declare(dict(DECL, capabilities_version="2")),
                   lambda: resolve.PROBE_VERSIONS.update({"encode-sample": "2"})]
        with mock.patch.dict(resolve.PROBE_VERSIONS):
            for change in changes:
                change()
                a = capability(h, "ffmpeg")
                self.assertEqual((a["state"], a["next_action"]), ("unqualified", "qualify ffmpeg"))
                self.assertNotEqual(a["key"], old["key"])  # the old pass is not read
                n = len(self.encodes())
                status(h)
                self.assertEqual(len(self.encodes()), n)  # capability and status never probe
                old = tools_probe(h, "ffmpeg")
                self.assertEqual((old["key"], old["ok"], len(self.encodes())), (a["key"], True, n + 1))
                tools_probe(h, "ffmpeg")
                self.assertEqual(len(self.encodes()), n + 1)
        h.put("mode", "refuse")
        h.put("version", "7.3")
        failed = tools_probe(h, "ffmpeg")
        tools_probe(h, "ffmpeg")
        self.assertEqual((failed["ok"], len(self.encodes())), (False, n + 2))  # a failed key waits for the owner
        tid = answers.task_id("ffmpeg", failed["key"])
        with self.assertRaises(SpineError):
            requalify(h, "ffmpeg", None)
        h.decisions[tid] = {"revision": "r2", "verb": "try-again"}
        for stale in (f"{tid}@r1", f"other@r2"):
            with self.assertRaises(SpineError):
                requalify(h, "ffmpeg", stale)
        h.decisions[tid] = {"revision": "r2", "verb": "approve"}
        with self.assertRaises(SpineError):
            requalify(h, "ffmpeg", f"{tid}@r2")
        self.assertEqual(len(self.encodes()), n + 2)
        h.decisions[tid] = {"revision": "r2", "verb": "try-again"}
        h.put("mode", "ok")
        rec = requalify(h, "ffmpeg", f"{tid}@r2")
        self.assertEqual((rec["attempt"], rec["requalify"], rec["ok"], rec["key"]), (2, f"{tid}@r2", True, failed["key"]))
        self.assertEqual(record(h, "ffmpeg"), rec)
        self.assertEqual(requalify(h, "ffmpeg", f"{tid}@r2"), rec)  # the same decision again probes nothing
        self.assertEqual(len(self.encodes()), n + 3)
        for moved in ({"revision": "r3", "verb": "try-again"}, {"revision": "r2", "verb": "dismiss"}, None):
            h.decisions[tid] = moved  # the Board moved on: a replay of r2 is stale, never the old record
            with self.assertRaises(SpineError):
                requalify(h, "ffmpeg", f"{tid}@r2")
        h.decisions[tid] = {"revision": "r3", "verb": "try-again"}
        with self.assertRaises(SpineError):
            requalify(h, "ffmpeg", f"{tid}@r3")  # a passed record refuses

    def test_unreadable_is_never_a_pass(self):
        h = self.h
        bad = [lambda d: d.update(capabilities_version=1), lambda d: d["capabilities"]["ffmpeg"].update(find="/x/ffmpeg"),
               lambda d: d["capabilities"]["ffmpeg"]["version"].update(min="seven"),
               lambda d: d["capabilities"]["encoder:h264-hardware"].update(needs=["gone"])]
        for spoil in bad:
            d = json.loads(json.dumps(DECL))
            spoil(d)
            h.declare(d)
            with self.assertRaises(SpineError) as e:
                capability(h, "ffmpeg")
            self.assertIn(str(h.declarations), str(e.exception))
        h.declarations.write_text('{"capabilities_version": "1", "capabilities": {"ffmpeg": {"kind": "executable"')
        with self.assertRaises(SpineError) as e:
            status(h)
        self.assertIn(str(h.declarations), str(e.exception))
        h.declare(DECL)
        for value in ("5", None, True, -1):
            h.settings["capabilities.probe_seconds"] = value
            with self.assertRaises(SpineError) as e:
                capability(h, "ffmpeg")
            self.assertIn("capabilities.probe_seconds", str(e.exception))
        del h.settings["capabilities.probe_seconds"]
        with self.assertRaises(SpineError):
            capability(h, "ffmpeg")
        self.assertFalse(h.state.exists())
        h.settings["capabilities.probe_seconds"] = 5
        h.put("mode", "refuse")
        tools_probe(h, "ffmpeg")
        for a in status(h) + [qualify(h, c) for c in DECL["capabilities"]]:
            self.assertIs(a["ok"], a["state"] == "qualified", a["capability"])
        h.put("mode", "ok")
        good = qualify(h, "guide:writing-voice")
        f = h.state / "capabilities" / "guide:writing-voice.json"
        malformed = [lambda r: r.update(probe={}), lambda r: r.pop("capabilities_version"),
                     lambda r: r.update(capabilities_version="2"), lambda r: r["probe"].update(name="encode-sample"),
                     lambda r: r["probe"].update(version="0"), lambda r: r["probe"].update(ok=False),
                     lambda r: r["probe"].update(seconds=float("nan")), lambda r: r.pop("at"),
                     lambda r: r.update(next_action="x"), lambda r: r.update(detail="other"),
                     lambda r: r.update(attempt=2), lambda r: r.update(requalify="t@r1"),
                     lambda r: r.update(capability="ffmpeg"), lambda r: r.update(extra=1),
                     lambda r: r["resolved"].update(sha256="sha256:" + "0" * 64),  # fabricated content, old key
                     lambda r: r.update(needs={"ffmpeg": "sha256:" + "1" * 64})]
        for spoil in malformed:
            r = json.loads(json.dumps(good))
            spoil(r)
            f.write_text(json.dumps(r).replace("NaN", "1e999"))
            a = capability(h, "guide:writing-voice")
            self.assertEqual((a["state"], a["ok"], a["next_action"]), ("unqualified", False, "qualify guide:writing-voice"))
            self.assertIn(str(f), a["detail"])

    def test_secrets_are_never_read(self):
        h = self.h
        rec = tools_probe(h, "account:example")
        self.assertEqual(rec["resolved"], {"present": True, "saved": "2026-10-01T00:00:00Z", "token_days": 60})
        self.assertEqual((rec["ok"], h.secret_reads, h.calls()), (True, [], []))  # no posting call
        h.saved["example"] = "2026-08-01T00:00:00Z"
        self.assertEqual(capability(h, "account:example")["state"], "missing")  # past its token life
        h.saved["example"] = None
        a = capability(h, "account:example")
        self.assertEqual((a["state"], a["next_action"]), ("missing", "save the key"))
        everything = "".join(p.read_text() for p in h.root.rglob("*") if p.is_file()) + json.dumps(h.asks)
        self.assertNotIn(SECRET, everything)
        self.assertEqual(h.secret_reads, [])

    def test_crash_reaches_one_record(self):
        h = self.h
        folder = h.state / "capabilities"
        for at in ("probe", "write"):
            with self.subTest(at=at):
                h.put("mode", "slow" if at == "probe" else "ok")
                child = subprocess.Popen([sys.executable, "-c", CHILD, str(h.root), at], cwd=CORE,
                                         start_new_session=True)
                if at == "probe":
                    while not self.encodes():
                        time.sleep(0.02)
                    os.killpg(child.pid, signal.SIGKILL)
                self.assertEqual(child.wait(), -signal.SIGKILL)
                self.assertEqual(list(folder.glob("*.json")) if folder.exists() else [], [])
                h.put("mode", "ok")
                rec = tools_probe(h, "ffmpeg")
                self.assertEqual([p.name for p in folder.glob("*.json")], ["ffmpeg.json"])
                self.assertEqual(record(h, "ffmpeg"), rec)
                self.assertEqual(tools_probe(h, "ffmpeg")["key"], rec["key"])
                os.unlink(folder / "ffmpeg.json")
        tools_probe(h, "ffmpeg")  # an older key's record, then a new workload: both callers find it stale
        old, name = record(h, "ffmpeg"), "capabilities.encode-sample"
        h.settings[name] = dict(h.settings[name], width=1280)
        n, both, gate, real = len(self.encodes()), [], threading.Barrier(2), answers.locked

        def met(*a):  # both callers have read the older record and reach cid's lock together
            gate.wait(5)
            return real(*a)
        with mock.patch.object(answers, "locked", met):
            threads = [threading.Thread(target=lambda: both.append(qualify(h, "ffmpeg"))) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual((len(both), both[0], len(self.encodes())), (2, both[1], n + 1))
        self.assertNotEqual(both[0]["key"], old["key"])
        self.assertEqual((record(h, "ffmpeg"), [p.name for p in folder.glob("*.json")]), (both[0], ["ffmpeg.json"]))

    def test_status_is_read_only(self):
        h = self.h
        tools_probe(h, "ffmpeg")
        h.put("mode", "refuse")
        h.put("version", "7.2")
        files = lambda: {p: p.read_bytes() for p in h.root.rglob("*") if p.is_file() and p != h.log}
        before = files()
        n = len(h.calls())
        out = status(h)
        self.assertEqual(files(), before)
        self.assertEqual((h.asks, [c for c in h.calls()[n:] if "lavfi" in c]), ({}, []))
        self.assertEqual(out[:-2], [capability(h, c) for c in DECL["capabilities"]])
        from pirillo_studio.capabilities.selected import voice
        self.assertEqual(out[-2:], [voice(h, p, ask=False) for p in ("speech", "writing")])
        self.assertEqual(out[0]["next_action"], "qualify ffmpeg")


CHILD = """import os, signal, sys
from pathlib import Path
from pirillo_studio.capabilities import answers, test_capabilities as t
from pirillo_studio.capabilities.answers import qualify
if sys.argv[2] == "write":
    os.replace = lambda *a: os.kill(os.getpid(), signal.SIGKILL)
qualify(t.Host(Path(sys.argv[1]), fresh=False), "ffmpeg")
"""


class Boundaries(Case):
    """guard: the seam's boundary families: non-finite numbers, aliases, stale stat keys (a crash is contract 11)."""

    def test_non_finite_or_word_versions_and_limits_refuse(self):
        h = self.h
        for word in ("NaN", "Infinity", "7.x"):
            h.put("version", word)
            self.assertEqual(capability(h, "ffmpeg")["state"], "missing")
            d = json.loads(json.dumps(DECL))
            d["capabilities"]["ffmpeg"]["version"]["min"] = word
            h.declare(d)
            with self.assertRaises(SpineError):
                capability(h, "ffmpeg")
            h.declare(DECL)
        h.declarations.write_text(json.dumps(DECL).replace('"7.0"', "NaN"))
        with self.assertRaises(SpineError):
            capability(h, "ffmpeg")
        h.declare(DECL)
        h.put("version", "7.1")
        for limit in (float("nan"), float("inf"), 0):
            h.settings["capabilities.probe_seconds"] = limit
            with self.assertRaises(SpineError):
                qualify(h, "ffmpeg")
        self.assertFalse(h.state.exists())

    def test_an_alias_is_keyed_by_its_target_and_a_linked_record_folder_refuses(self):
        h = self.h
        real = h.root / "real"
        real.mkdir()
        os.replace(h.bin / "ffmpeg", real / "ffmpeg")
        os.symlink(real / "ffmpeg", h.bin / "ffmpeg")
        rec = tools_probe(h, "ffmpeg")
        self.assertEqual(rec["resolved"]["path"], str(real / "ffmpeg"))
        os.replace(h.state / "capabilities", h.root / "elsewhere")
        os.symlink(h.root / "elsewhere", h.state / "capabilities")
        with self.assertRaises(SpineError):
            capability(h, "ffmpeg")
        with self.assertRaises(SpineError):
            qualify(h, "ffmpeg")

    def test_same_size_and_mtime_with_new_bytes_is_a_new_key(self):
        h = self.h
        f = h.bin / "ffmpeg"
        rec = tools_probe(h, "ffmpeg")
        st = f.stat()
        f.write_text(f.read_text().replace("Copyright", "Copyleft!"))
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))  # only the change time says the bytes moved
        self.assertEqual((f.stat().st_size, f.stat().st_mtime_ns), (st.st_size, st.st_mtime_ns))
        self.assertNotEqual(capability(h, "ffmpeg")["key"], rec["key"])
        os.replace(h.root / "calls.log", h.root / "calls.old")
        copy = h.root / "copy"
        copy.write_bytes(f.read_bytes())
        os.chmod(copy, 0o755)
        os.replace(copy, f)  # same bytes, new inode: hashed again, same key
        a = capability(h, "ffmpeg")
        self.assertEqual(a["resolved"]["stat"][str(f)][1], f.stat().st_ino)
        self.assertNotEqual(a["key"], rec["key"])
        self.assertEqual(a["resolved"]["sha256"], capability(h, "ffmpeg")["resolved"]["sha256"])


if __name__ == "__main__":
    unittest.main()
