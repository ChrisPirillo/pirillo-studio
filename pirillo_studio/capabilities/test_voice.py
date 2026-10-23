"""docs/capabilities.md section 5, contracts 5, 6, 7 and 10: the owner's voice and take picks, and listening rows.

Fixtures are test_capabilities.py's: a fake Board and fake tools; the speech engine qualifies through its real
speak-fixture probe over the fake engine.
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from pirillo_studio.capabilities.selected import select_voice, stage, status, voice
from pirillo_studio.capabilities import selected as voice_module
from pirillo_studio.capabilities.test_capabilities import Host, tools_probe
from pirillo_studio.qc import verdict
from pirillo_studio.spine.files import SpineError

PROFILE = {"model": "tts:engine/model", "reference": "reference.wav", "sampling": {"temperature": 0.7}}


class Case(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.h = h = Host(Path(os.path.realpath(t.name)))
        for name in ("a", "b"):
            self.profile(name, PROFILE)
        tools_probe(h, "ffmpeg")
        tools_probe(h, "guide:writing-voice")
        self.assertTrue(tools_probe(h, "tts:engine/model")["ok"])

    def profile(self, name, profile):
        d = self.h.studio / "voices" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "profile.json").write_text(json.dumps(profile))
        (d / "reference.wav").write_bytes(name.encode() * 64)

    def choose(self, part, choice, revision="r1"):
        tid = voice_module.choose_id(self.h, part)
        self.h.decisions[tid] = {"revision": revision, "verb": choice}
        return select_voice(self.h, part, choice, f"{tid}@{revision}")

    def take(self, data=b"take one"):
        (self.h.root / "take.wav").write_bytes(data)
        (self.h.root / "original.wav").write_bytes(b"original")
        return {"take": self.h.root / "take.wav", "original": self.h.root / "original.wav"}

    def record(self):
        return json.loads((self.h.studio / "voice.json").read_text())


class Voice(Case):
    """guard: capabilities.md section 5 contracts 5, 6 and 10: no voice or take is chosen but by the owner."""

    def test_voice_waits_on_owner(self):
        h = self.h
        a = voice(h, "speech")
        self.assertEqual((a["state"], a["ok"]), ("waiting_owner", False))
        self.assertEqual(list(h.asks), [a["ask"]])
        task = h.asks[a["ask"]]
        self.assertEqual((task["kind"], task["source"]["filed_by"]), ("ask", "capabilities"))
        self.assertEqual([x["verb"] for x in task["actions"]], ["voices/a", "voices/b", "none"])
        self.assertTrue(all(x["undo"]["why"] == "Until a stage reads the voice record" for x in task["actions"]))
        self.assertEqual([p["href"] for p in task["judge"]["parts"]], ["/served/a", "/served/b"])
        tid = a["ask"]
        for bad in (None, f"{tid}@r1"):  # no decision, then one the Board does not hold
            with self.assertRaises(SpineError):
                select_voice(h, "speech", "voices/a", bad)
        h.decisions[tid] = {"revision": "r2", "verb": "voices/b"}
        for bad in (f"{tid}@r1", f"{tid}@r2", f"other@r2"):  # stale revision, another option, another task
            with self.assertRaises(SpineError):
                select_voice(h, "speech", "voices/a", bad)
        self.assertFalse((h.studio / "voice.json").exists())
        h.decisions[tid] = {"revision": "r2", "verb": "voices/a"}
        with mock.patch.object(voice_module.os, "replace", side_effect=SystemExit("killed mid-write")):
            with self.assertRaises(SystemExit):
                select_voice(h, "speech", "voices/a", f"{tid}@r2")
        again = voice(h, "speech")  # after the crash: the same one ask, nothing half-written read
        self.assertEqual((again["state"], again["ask"], list(h.asks)), ("waiting_owner", tid, [tid]))
        rec = select_voice(h, "speech", "voices/a", f"{tid}@r2")
        self.assertEqual(self.record(), rec)
        self.assertEqual({k: rec["speech"][k] for k in ("state", "voice", "engine", "decision")},
                         {"state": "selected", "voice": "voices/a", "engine": "tts:engine/model",
                          "decision": f"{tid}@r2"})
        self.assertEqual(select_voice(h, "speech", "voices/a", f"{tid}@r2"), rec)
        self.assertEqual((voice(h, "speech")["state"], voice(h, "speech")["ok"]), ("qualified", True))
        for moved in ({"revision": "r3", "verb": "voices/a"}, {"revision": "r2", "verb": "voices/b"}, None):
            h.decisions[tid] = moved  # the Board moved on, or lost the task: a replay of r2 is stale
            with self.assertRaises(SpineError):
                select_voice(h, "speech", "voices/a", f"{tid}@r2")
        self.assertEqual(self.record(), rec)
        h.decisions[tid] = {"revision": "r2", "verb": "voices/a"}
        self.choose("writing", "none")
        cites = self.take()
        for part, more in (("writing", None), ("speech", cites)):
            if part == "speech":
                self.profile("c", PROFILE)
                self.choose("speech", "none", "r3")
            a = voice(h, part, more)
            self.assertEqual((a["state"], a["ok"]), ("missing", False))
        self.assertFalse([t for t in h.asks if t.startswith("capabilities:take:")])  # none offers no patch
        for name, bad in (("e", {"sampling": None}), ("f", {"reference": " "}), ("g", {"sampling": {}})):
            self.profile(name, {k: v for k, v in dict(PROFILE, **bad).items() if v is not None})
            tid = voice_module.choose_id(h, "speech")
            h.decisions[tid] = {"revision": "r4", "verb": f"voices/{name}"}
            with self.assertRaises(SpineError):  # a missing or empty profile field refuses, never a default
                select_voice(h, "speech", f"voices/{name}", f"{tid}@r4")

    def test_take_pick_is_the_owners(self):
        h = self.h
        self.choose("speech", "voices/a")
        cites = dict(self.take(), rank=1, recognizer="pass", picked_by="editor")
        a = voice(h, "speech", cites)
        self.assertEqual((a["state"], a["ok"]), ("waiting_owner", False))
        task = h.asks[a["ask"]]
        self.assertEqual([(p["role"], p["href"]) for p in task["judge"]["parts"]],
                         [("candidate", "/served/take.wav"), ("original", "/served/original.wav")])
        h.decisions["ranking:top"] = {"revision": "r1", "verb": "approve"}  # a maker's or a ranking's pick
        h.decisions[a["ask"]] = {"revision": "r1", "verb": "reject"}
        self.assertEqual(voice(h, "speech", cites)["state"], "waiting_owner")
        h.decisions[a["ask"]] = {"revision": "r2", "verb": "approve"}
        self.assertEqual((voice(h, "speech", cites)["state"], voice(h, "speech", cites)["ok"]), ("qualified", True))
        new = voice(h, "speech", self.take(b"take two"))  # new take bytes reopen it
        self.assertEqual(new["state"], "waiting_owner")
        self.assertNotEqual(new["ask"], a["ask"])
        self.take()
        (h.root / "original.wav").write_bytes(b"re-cut original")  # a new original line reopens it
        self.assertNotIn(voice(h, "speech", cites)["ask"], (a["ask"], new["ask"]))
        self.take()
        self.choose("speech", "voices/b", "r9")  # a new voice reopens it
        b = voice(h, "speech", cites)
        self.assertEqual(b["state"], "waiting_owner")
        self.assertNotIn(b["ask"], (a["ask"], new["ask"]))
        (h.studio / "voices" / "b" / "reference.wav").write_bytes(b"retuned")
        self.assertEqual(voice(h, "speech", cites)["state"], "unqualified")  # changed voice files: chosen again

    def test_a_new_selection_on_the_same_engine_is_a_new_key(self):
        h = self.h
        self.choose("speech", "voices/a")
        a = voice(h, "speech")
        self.assertEqual((a["state"], a["key"]), ("qualified", self.record()["key"]))
        self.choose("speech", "voices/b", "r2")
        b = voice(h, "speech")
        self.assertEqual((b["state"], b["capability_key"]), ("qualified", a["capability_key"]))
        self.assertNotEqual(b["key"], a["key"])  # a consumer citing a's key is stale
        f = h.studio / "voice.json"
        f.write_text(f.read_text().replace(self.record()["key"], a["key"]))  # a key not its parts' refuses
        with self.assertRaises(SpineError):
            voice(h, "speech")

    def test_two_parts_chosen_at_once_both_land(self):
        gate, real = threading.Barrier(2), voice_module.record

        def met(h):  # both writers have read voice.json before either writes, unless one holds the lock
            r = real(h)
            try:
                gate.wait(1)
            except threading.BrokenBarrierError:
                pass
            return r
        with mock.patch.object(voice_module, "record", met):
            threads = [threading.Thread(target=self.choose, args=a) for a in (("speech", "voices/a"), ("writing", "none"))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(sorted(k for k in self.record() if k != "key"), ["speech", "writing"])

    def test_needs_follow_declarations(self):
        h = self.h
        recipes = {"shorts-only": ["ffmpeg"], "plain": ["ffmpeg", "encoder:h264-hardware"]}
        out = {name: stage(h, tools) for name, tools in recipes.items()}
        self.assertEqual(h.asks, {})
        self.assertEqual([a["capability"] for a in out["shorts-only"]], ["ffmpeg"])
        cited = stage(h, recipes["plain"], self.take())
        self.assertEqual(cited[-1]["state"], "waiting_owner")
        self.assertEqual(list(h.asks), [voice_module.choose_id(h, "speech")])
        self.assertEqual(stage(h, ["voice:writing"])[0]["state"], "waiting_owner")
        self.assertEqual(stage(h, recipes["shorts-only"]), stage(h, list(recipes["shorts-only"])))  # tools only

    def test_unreadable_voice_record_refuses(self):
        h = self.h
        f = h.studio / "voice.json"
        for bad in ('{"speech": {"state": "selected"', '{"speech": {"state": "maybe"}}', "[1]",
                    '{"key": "k", "speech": {"state": "selected", "decision": "t@r", "at": "x"}}',
                    '{"key": "k", "speech": {"state": "none", "decision": "t@r", "at": "x"}}'):
            f.write_text(bad)
            with self.assertRaises(SpineError) as e:
                voice(h, "speech")
            self.assertIn(str(f), str(e.exception))
            with self.assertRaises(SpineError):
                status(h)


LISTENING = [  # C1's listening rows as capabilities.md section 4 names them, each a QC manual entry
    {"id": "shorts-listen", "stage": "short", "reads": [{"bind": "file:short", "record": "Shorts/short.mp4"}]},
    {"id": "splice-rendered", "stage": "masters", "reads": [{"bind": "file:long", "record": "YouTube/main.mp4"}]},
    {"id": "archive-silent", "stage": "draft", "reads": [{"bind": "file:draft", "record": "Draft/draft.mp4"}]}]
LOUDNESS = {"name": "integrated_lufs", "unit": "LUFS", "min": -17, "max": -15}


class Video:
    def __init__(self, folder):
        self.folder = folder

    def resolve_setting(self, name):
        if name != "rendering.settings.loudness":
            raise KeyError(name)
        return dict(LOUDNESS)

    def now(self):
        return "2026-10-23T12:00:00Z"


class Listening(unittest.TestCase):
    """guard: capabilities.md section 5 contract 7, through pirillo_studio/qc's verdict machinery over a test catalog:
    each listening row a `manual` entry beside a passing auto entry. Unmet dependency, not covered here: the real
    catalog's C1 listening rows are QC's to add (item 15, parked at seam 2c); capabilities writes no QC row."""

    def test_listening_rows_never_auto_clear(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        root = Path(t.name)
        v = Video(root / "video")
        rows = []
        for e in LISTENING:
            rows += [dict(e, owner="qc", checker="owner", mode="manual", result="judgment", setting=None, rule=None,
                          may_lower=False, fixer="finisher"),
                     {"id": f"{e['stage']}-loudness", "stage": e["stage"], "owner": "rendering",
                      "checker": "checker-renders", "mode": "auto", "result": "measure",
                      "reads": [{"bind": "render:long", "record": "records/render/long.json"}],
                      "setting": {"name": "rendering.settings.loudness", "owner": "rendering"}, "rule": None,
                      "may_lower": False, "fixer": "finisher"}]
            (v.folder / e["reads"][0]["record"]).parent.mkdir(parents=True, exist_ok=True)
            (v.folder / e["reads"][0]["record"]).write_bytes(b"media")
        (v.folder / "records" / "render").mkdir(parents=True)
        (v.folder / "records" / "render" / "long.json").write_text('{"readings": {"integrated_lufs": -16}}')
        (v.folder / "records" / "job.json").write_text('{"video_started": "2026-10-23T00:00:00Z"}')
        (root / "catalog.json").write_text(json.dumps(rows))
        verdict._catalog_cache.clear()
        self.addCleanup(verdict._catalog_cache.clear)
        with mock.patch.object(verdict, "HERE", root):
            for e in LISTENING:
                got = verdict.judge(v, e["stage"])
                self.assertEqual(json.loads((v.folder / "records" / "qc" / f"{e['stage']}.json").read_text()), got)
                self.assertEqual({r["id"]: r["result"] for r in got["checks"]},
                                 {e["id"]: "manual-pending", f"{e['stage']}-loudness": "pass"})
                self.assertEqual((got["state"], verdict.check(got)["ok"]), ("waiting_manual", False))
        src = "".join(p.read_text() for p in Path(voice_module.__file__).parent.glob("*.py") if not
                      p.name.startswith("test_"))
        self.assertNotIn("record_manual", src)
        self.assertNotIn("qc", [line.split()[1].split(".")[-1] for line in src.splitlines()
                                if line.startswith("from ") and "import" in line])


if __name__ == "__main__":
    unittest.main()
