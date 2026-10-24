"""docs/capabilities.md sections 1 and 2, beyond contracts 1 to 12's main cases: the model probes over fake engines,
install identity and probe workloads in the key, settings present but empty, and requalify consumed once
under concurrency.

Fixtures are test_capabilities.py's; every case reads the written record and the fake tools' call log.
"""
import json
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

from pirillo_studio.capabilities import answers
from pirillo_studio.capabilities.answers import capability, qualify, requalify
from pirillo_studio.capabilities.test_capabilities import DECL, FFMPEG, Case, record, tools_probe
from pirillo_studio.spine.files import SpineError

TTS, ASR = "tts:engine/model", "asr:engine/model"


class ModelProbes(Case):
    """guard: a model is qualified only by running its declared engine on the host's fixture, never by presence."""

    def test_speak_fixture_passes_only_on_audio_longer_than_the_minimum(self):
        for said, ok in (("3", True), ("1", False), ("none", False)):
            with self.subTest(seconds=said):
                self.setUp()
                h = self.h
                tools_probe(h, "ffmpeg")
                h.put("speak", said)
                rec = tools_probe(h, TTS)
                self.assertEqual((rec["ok"], record(h, TTS)), (ok, rec))
                spoke = [c for c in h.calls() if c.startswith("speak ")]
                self.assertEqual(len(spoke), 1)
                self.assertTrue(spoke[0].startswith("speak A short test line. ") and spoke[0].endswith("speech.wav"))
                self.assertEqual(len([c for c in h.calls() if "0:a:0" in c]), int(said != "none"))
                self.assertEqual(rec["probe"]["workload"], h.settings["capabilities.speak-fixture"])
        self.setUp()
        h = self.h
        h.put("mode", "refuse")
        tools_probe(h, "ffmpeg")
        a = tools_probe(h, TTS)  # its measuring ffmpeg is unqualified: no engine call, no record
        self.assertEqual((a["state"], [c for c in h.calls() if c.startswith("speak")]), ("unqualified", []))
        self.assertFalse((h.state / "capabilities" / f"{TTS}.json").exists())

    def test_transcribe_fixture_needs_the_expected_words_in_order(self):
        for heard, ok in (("um one, uh, two", True), ("two one", False), ("one", False)):
            with self.subTest(heard=heard):
                self.setUp()
                h = self.h
                h.put("heard", heard)
                rec = tools_probe(h, ASR)
                self.assertEqual((rec["ok"], record(h, ASR)), (ok, rec))
                self.assertEqual([c for c in h.calls() if c.startswith("hear ")], [f"hear {h.root / 'fixture.wav'}"])
        self.setUp()
        h = self.h
        kept = tools_probe(h, ASR)
        self.assertTrue(kept["ok"])
        for change in ("changed", "removed"):  # the kept pass rests on the fixture's bytes: each read checks them
            with self.subTest(fixture=change):
                h.put("fixture.wav", "other audio") if change == "changed" else os.unlink(h.root / "fixture.wav")
                for ask in (capability, qualify):
                    with self.assertRaises(SpineError):
                        ask(h, ASR)
                self.assertEqual((record(h, ASR), len([c for c in h.calls() if c.startswith("hear ")])), (kept, 1))



class ReadOnlyExecutable(Case):
    """guard: an executable that only reads (the package's ffprobe) qualifies `present` on its version call alone:
    no encode is asked of it, and under its minimum it is missing."""

    def test_present_runs_only_the_version_call(self):
        h = self.h
        shipped = json.loads((Path(answers.__file__).parent / "capabilities.json").read_text())["capabilities"]
        h.declare(dict(DECL, capabilities=dict(DECL["capabilities"], ffprobe=shipped["ffprobe"])))
        h.tool("ffprobe", FFMPEG.format(log=h.log, root=h.root))
        rec = tools_probe(h, "ffprobe")
        self.assertEqual((rec["ok"], rec["probe"]["name"], record(h, "ffprobe")), (True, "present", rec))
        self.assertEqual(set(h.calls()), {"-version"})
        h.put("version", "6.1")
        self.assertEqual(capability(h, "ffprobe")["state"], "missing")


class Empty(Case):
    """guard: a setting or declaration present but empty refuses through the public path, never passes as given."""

    def test_present_but_empty_refuses(self):
        h = self.h
        tools_probe(h, "ffmpeg")
        for s, cid, bad in (("capabilities.transcribe-fixture", ASR, {"text": "..."}),
                            ("capabilities.transcribe-fixture", ASR, {"text": " "}),
                            ("capabilities.speak-fixture", TTS, {"sentence": "  "})):
            keep = h.settings[s]
            h.settings[s] = dict(keep, **bad)
            with self.assertRaises(SpineError):
                qualify(h, cid)
            h.settings[s] = keep
        self.assertEqual([c for c in h.calls() if c.startswith(("hear", "speak"))], [])
        self.assertEqual([p.name for p in (h.state / "capabilities").glob("*.json")], ["ffmpeg.json"])
        for field, bad in (("get", "  "), ("version", {"args": [], "min": "7.0"}),
                           ("version", {"args": [" "], "min": "7.0"}), ("capabilities_version", " ")):
            d = json.loads(json.dumps(DECL))
            (d if field == "capabilities_version" else d["capabilities"]["ffmpeg"])[field] = bad
            h.declare(d)
            with self.assertRaises(SpineError):
                capability(h, "ffmpeg")
        h.declare(DECL)
        self.assertTrue(capability(h, "ffmpeg")["ok"])


class Identity(Case):
    """guard: an install's identity and its probe's workload are part of the key, so neither reuses an old pass."""

    def test_a_same_content_replacement_is_a_new_key(self):
        h = self.h
        old = tools_probe(h, "ffmpeg")
        f = h.bin / "ffmpeg"
        copy = h.root / "copy"
        copy.write_bytes(f.read_bytes())
        os.chmod(copy, 0o755)
        os.replace(copy, f)  # same bytes and version, new inode and change time
        a = capability(h, "ffmpeg")
        self.assertEqual(a["resolved"]["sha256"], old["resolved"]["sha256"])
        self.assertNotEqual(a["key"], old["key"])
        self.assertEqual((a["state"], a["next_action"], record(h, "ffmpeg")), ("unqualified", "qualify ffmpeg", old))

    def test_the_probe_workload_comes_from_the_host_and_is_keyed(self):
        h = self.h
        name = "capabilities.encode-sample"
        old = tools_probe(h, "ffmpeg")
        for bad in (None, {}, dict(h.settings[name], rate=0), dict(h.settings[name], rate=float("nan")),
                    dict(h.settings[name], rate=True), dict(h.settings[name], rate="30"),
                    dict(h.settings[name], seconds=2.5), dict(h.settings[name], extra=1)):
            h.settings[name] = bad
            with self.assertRaises(SpineError) as e:
                capability(h, "ffmpeg")
            self.assertIn(name, str(e.exception))
        del h.settings[name]
        with self.assertRaises(SpineError):
            capability(h, "ffmpeg")
        h.settings[name] = {"width": 1280, "height": 720, "rate": 30, "seconds": 2}
        a = capability(h, "ffmpeg")
        self.assertEqual((a["state"], a["next_action"]), ("unqualified", "qualify ffmpeg"))
        self.assertNotEqual(a["key"], old["key"])
        rec = tools_probe(h, "ffmpeg")
        self.assertIn("testsrc2=size=1280x720:rate=30:duration=2", self.encodes()[-1])
        self.assertEqual((rec["ok"], rec["probe"]["workload"]), (True, h.settings[name]))
        for probe, bad in (("transcribe-fixture", {"sha256": "abc"}), ("transcribe-fixture", {"audio": "fixture.wav"}),
                           ("speak-fixture", {"min_seconds": -1})):
            s = f"capabilities.{probe}"
            keep = h.settings[s]
            h.settings[s] = dict(keep, **bad)
            with self.assertRaises(SpineError):
                capability(h, ASR if probe == "transcribe-fixture" else TTS)
            h.settings[s] = keep


class Requalify(Case):
    """guard: two callers acting on one owner decision probe once and write one record, both getting it."""

    def test_concurrent_requalify_probes_once(self):
        h = self.h
        h.put("mode", "refuse")
        failed = tools_probe(h, "ffmpeg")
        tid = answers.task_id("ffmpeg", failed["key"])
        h.decisions[tid] = {"revision": "r1", "verb": "try-again"}
        h.put("mode", "ok")
        n, both, gate, real = len(self.encodes()), [], threading.Barrier(2), answers.current

        def met(*a):  # both callers hold the checked decision before either consumes it
            real(*a)
            gate.wait(5)
        with mock.patch.object(answers, "current", met):
            threads = [threading.Thread(target=lambda: both.append(requalify(h, "ffmpeg", f"{tid}@r1")))
                       for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual((len(both), both[0], len(self.encodes())), (2, both[1], n + 1))
        self.assertEqual((record(h, "ffmpeg"), both[0]["attempt"], both[0]["ok"]), (both[1], 2, True))
        self.assertEqual(sorted(p.name for p in (h.state / "capabilities").glob("*.json")), ["ffmpeg.json"])


if __name__ == "__main__":
    unittest.main()
