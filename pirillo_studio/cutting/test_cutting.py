"""Cutting section 5 contracts inspect output records; test_off_is_unchanged is host seam 3."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pirillo_studio.cutting import checks, plan, settle
from pirillo_studio.spine import engine
from pirillo_studio.spine.files import canon, filehash


class Video:
    def __init__(self, folder):
        self.folder, self.asked = folder, []
        self.readings = None

    def measure(self, source):
        if self.readings is None:
            raise OSError("source audio unavailable")
        return copy.deepcopy(self.readings)

    def request_arc(self, task):
        self.asked.append(copy.deepcopy(task))
        (self.folder / "records/arc.json").write_bytes(canon(task))
        return {"ok": True, "state": "waiting_owner", "next_action": "read arc", "detail": task}

    def now(self):
        return "2026-01-01T00:00:00Z"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.v = Video(Path(self.tmp.name) / "video")
        self.v.folder.mkdir()
        self.reset()

    def put(self, name, value):
        path = self.v.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canon(value))

    def get(self, name):
        return json.loads((self.v.folder / name).read_text())

    def reset(self):
        self.v.asked.clear()
        self.words = [{"id": f"w{i + 1}", "text": text, "start": start, "end": start + 1.2,
                       "flags": [], "finished": True}
                      for i, (text, start) in enumerate(zip(
                          ["Subject", "works.", "Details", "matter.", "Result", "lasts."],
                          [1, 3, 10, 12, 20, 22]))]
        self.maker = {"video": "records/job.json", "plan_version": "1", "spans": [
            {"id": "s1", "order": 1, "src": [.9, 4.5], "words": ["w1", "w2"], "enter": "open", "shot": "med"},
            {"id": "s2", "order": 2, "src": [9.9, 13.5], "words": ["w3", "w4"], "enter": "jump", "shot": "close"},
            {"id": "s3", "order": 3, "src": [19.9, 31.2], "words": ["w5", "w6"], "enter": "jump", "shot": "wide"}],
            "removed": [], "named": [], "cold_open": {"span": "s1", "words": ["w1", "w2"]}, "end_hold_s": 8.0}
        (self.v.folder / "source.mp4").write_bytes(b"synthetic source, measures supplied by adapter")
        (self.v.folder / "owner.txt").write_text("owner's original")
        self.put("records/job.json", {"voice_over": []})
        self.put("records/shorts_plan.json", {"plan": "sha256:pending", "shorts": [
            {"id": "full", "full": True, "point": "whole story"}]})
        self.sync()

    def sync(self):
        self.put("records/words.json", {"words": self.words})
        self.put("records/probe.json", {"duration_s": 80, "fps": 25, "sample_rate": 48000})
        self.maker["removed"] = []
        last = 0
        for span in sorted(self.maker["spans"], key=lambda s: s["src"][0]):
            if span["src"][0] > last:
                self.maker["removed"].append({"src": [last, span["src"][0]], "words": [], "why": "silence"})
            last = span["src"][1]
        if last < 80:
            self.maker["removed"].append({"src": [last, 80], "words": [], "why": "silence"})
        self.put("records/cut_plan.json", self.maker)
        audio, last = [], 0
        for word in self.words:
            if word["start"] > last:
                audio.append({"start": last, "end": word["start"], "db": -55, "kind": "quiet"})
            audio.append({"start": word["start"], "end": word["end"], "db": -15, "kind": "speech"})
            last = word["end"]
        audio.append({"start": last, "end": 80, "db": -55, "kind": "quiet"})
        self.v.readings = {"duration_s": 80, "fps": 25, "sample_rate": 48000, "audio": audio,
                           "motion": [{"time": i / 25, "value": 1 if i % 25 == 0 else 0} for i in range(2001)]}

    def overlays(self, key):
        self.put("records/overlay_plan.json", {"plan": key, "cards": []})
        self.put("records/bed.json", {"plan": key, "request": "quiet bed", "tempo_from": "words_per_s"})
        self.put("records/callouts_plan.json", {"plan": key, "callouts": []})

    def ready(self):
        key = settle.settle(self.v)
        self.overlays(key)
        checks.check(self.v)
        result = self.get("records/checks/cut.json")
        self.assertEqual(result["state"], "pass", result)
        return self.get("records/cut_plan2.json")

    def add_patch(self):
        record = {"sample_rate": 48000, "sample_count": 96000, "duration_s": 2,
                  "words": [{"id": "p1", "text": "Details", "start": .1, "end": 1},
                            {"id": "p2", "text": "matter.", "start": 1.1, "end": 2}],
                  "joins": {"before_s": .1, "after_s": .1}, "take": "take1", "owner_pick": "pick1"}
        self.put("records/patches/p1.json", record)
        self.put("records/voice.json", {"picks": [{"id": "pick1", "take": "take1", "approved": True,
                  "capability": "voice", "kind": "speech", "cites": ["owner-authorized take"]}]})
        self.maker["spans"][1]["hide_picture"] = True
        self.maker["spans"][1]["patch"] = {"record": "records/patches/p1.json",
                "sha": filehash(self.v.folder / "records/patches/p1.json"), "words": ["p1", "p2"]}
        self.put("records/cut_plan.json", self.maker)
        return record

    def approve(self, decision="approve", words=""):
        arc = self.get("records/arc.json")
        self.put("records/arc_decision.json", {"decision": decision, "plan": arc["plan"],
            "check": arc["check"], "arc": arc["key"], "words": words,
            "blocks": [{"spans": [s["id"] for s in self.get("records/cut_plan2.json")["spans"]],
                        "why": "one story"}]})

    def locked(self):
        cut = self.ready()
        checks.request_arc(self.v)
        self.approve()
        checks.lock(self.v)
        self.assertEqual(self.get("records/story_map.json")["plan"], cut["key"])
        return cut

    def refused(self, operation):
        try:
            answer = operation()
        except (ValueError, OSError) as error:
            return str(error)
        self.assertIsInstance(answer, dict)
        self.assertIn(answer["state"], ("fail", "blocked"), answer)
        return json.dumps(answer)


class Prompts(Fixture):
    """guard: settle writes the paper's measured prompts beside paper.md: a hold for a long silence between kept words
    and a loop for a promise never paid (queue item 37 seam 3e, the host's paper.py prompts)."""

    def test_a_long_silence_and_an_unpaid_promise_are_prompts(self):
        self.words[0]["text"] = "Later"
        self.words[5].update(start=25, end=26.2)
        self.sync()
        settle.settle(self.v)
        got = {p["kind"]: p for p in self.get("records/paper_prompts.json")["prompts"]}
        self.assertEqual(sorted(got), ["hold", "loop"])
        self.assertEqual((got["hold"]["id"], got["hold"]["gap_s"]), ("hold@20.0", 3.8))
        self.assertEqual(got["loop"]["id"], "loop@1.0")
        self.assertIn("none of its words come back", got["loop"]["what"])


class Cutting(Fixture):
    """e2e: records/cut_plan2.json, paper.md, retime.json and story_map.json."""

    def test_every_kept_word_is_his(self):
        self.maker["spans"].insert(1, {"id": "pop", "order": 2, "src": [4.5, 5], "words": [],
            "enter": "pop", "shot": "face", "expression": {"after_word": "w2", "seen": "smiles"}})
        for index, span in enumerate(self.maker["spans"], 1):
            span["order"] = index
        self.sync()
        cut = self.locked()
        by_id = {w["id"]: w for w in self.words}
        for index, span in enumerate(cut["spans"]):
            if span["enter"] == "pop":
                self.assertEqual(span["words"], [])
                self.assertEqual(cut["spans"][index - 1]["words"][-1], span["expression"]["after_word"])
                self.assertFalse([w for w in self.words if w["start"] < span["src"][1] and w["end"] > span["src"][0]])
                continue
            first, last = map(by_id.__getitem__, span["words"])
            self.assertLessEqual(span["src"][0], first["start"])
            self.assertGreaterEqual(span["src"][1], last["end"])
        tiled = sorted(s["src"] for s in cut["spans"] + cut["removed"])
        self.assertEqual(tiled[0][0], 0)
        self.assertEqual(tiled[-1][1], cut["source_duration_s"])
        for left, right in zip(tiled, tiled[1:]):
            self.assertAlmostEqual(left[1], right[0])
        self.assertEqual((self.v.folder / "records/paper.md").read_text().split(),
                         [w["text"] for w in self.words])
        self.assertEqual(self.get("records/story_map.json")["check"], self.get("records/checks/cut.json")["key"])

    def test_story_order_is_the_plan_order(self):
        for order in ([1, 2, 3], [2, 1, 3]):
            for span, position in zip(self.maker["spans"], order):
                span["order"], span["enter"] = position, "open" if position == 1 else "jump"
            self.put("records/cut_plan.json", self.maker)
            cut = self.ready()
            spans = sorted(cut["spans"], key=lambda s: s["order"])
            self.assertEqual([s["id"] for s in spans], [f"s{order.index(i) + 1}" for i in range(1, 4)])
            expected = [w["text"] for s in spans for w in self.words
                        if int(s["words"][0][1:]) <= int(w["id"][1:]) <= int(s["words"][1][1:])]
            self.assertEqual((self.v.folder / "records/paper.md").read_text().split(), expected)

    def test_owner_wait_then_lock(self):
        self.ready()
        answer = checks.request_arc(self.v)
        self.assertEqual(answer["state"], "waiting_owner")
        self.assertTrue(self.v.asked)
        self.approve("reject", "The middle loses the point")
        refusal = self.refused(lambda: checks.lock(self.v))
        self.assertIn("The middle loses the point", refusal)
        self.assertFalse((self.v.folder / "records/story_map.json").exists())
        for negative in ("redo", "not this", "try again", False, "approved"):
            self.approve(negative)
            self.refused(lambda: checks.lock(self.v))
            self.assertFalse((self.v.folder / "records/story_map.json").exists())
        self.approve()
        decision = self.get("records/arc_decision.json")
        decision["plan"] = "sha256:old"
        self.put("records/arc_decision.json", decision)
        self.refused(lambda: checks.lock(self.v))
        self.approve()
        checks.lock(self.v)
        locked = self.get("records/story_map.json")
        self.assertEqual(locked["plan"], self.get("records/cut_plan2.json")["key"])
        self.assertIn("arc_decision", locked)

    def test_edit_after_lock_reopens_only_what_it_touches(self):
        before = self.locked()
        previous = self.get("records/checks/cut.json")
        self.maker["spans"][1]["shot"] = "face"
        self.put("records/cut_plan.json", self.maker)
        after = self.ready()
        self.refused(lambda: checks.lock(self.v))
        for old, new in zip(before["spans"], after["spans"]):
            if old["id"] != "s2":
                self.assertEqual(old, new)
        current = self.get("records/checks/cut.json")
        self.assertEqual(previous["checks"], current["checks"])
        checks.request_arc(self.v)
        self.assertEqual(self.v.asked[-1]["touched"], ["s2"])

    def test_retime_keeps_his_decisions(self):
        self.maker["named"] = [{"rule": "takes", "at_src": 1, "why": "intentional emphasis"}]
        self.put("records/cut_plan.json", self.maker)
        before = self.ready()
        overlay = self.get("records/overlay_plan.json")
        overlay["cards"] = [{"id": "c1", "kind": "instruction", "anchor": ["w3", "w4"], "text": "Details matter."}]
        self.put("records/overlay_plan.json", overlay)
        self.put("records/shorts_plan.json", {"plan": before["key"], "shorts": [{"id": "full", "full": True, "point": "whole story"}]})
        for word in self.words:
            word["start"] += .2
            word["end"] += .2
        self.words.insert(1, {"id": "split", "text": "still", "start": 2.6, "end": 2.8, "flags": [], "finished": False})
        self.sync()
        settle.retime(self.v)
        after = self.get("records/cut_plan2.json")
        for old, new in zip(before["spans"], after["spans"]):
            for field in ("id", "order", "enter", "shot", "words"):
                self.assertEqual(old[field], new[field])
            self.assertAlmostEqual(new["src"][0] - old["src"][0], .2)
        for old, new in zip(before["named"], after["named"]):
            for field in ("rule", "why", "anchor"):
                self.assertEqual(old[field], new[field])
            self.assertAlmostEqual(new["at_src"] - old["at_src"], .2)
        self.assertEqual(self.get("records/overlay_plan.json")["cards"], overlay["cards"])
        self.assertEqual(self.get("records/shorts_spec.json")["shorts"][0]["id"], "full")
        record = self.get("records/retime.json")
        self.assertEqual(record["moved"], ["s1", "s2", "s3"])
        self.assertEqual(record["plan"], after["key"])
        self.assertEqual(self.get("records/checks/cut.json")["plan"], after["key"])

    def test_retime_that_loses_a_word_refuses(self):
        self.ready()
        before = (self.v.folder / "records/cut_plan2.json").read_bytes()
        for missing in ("w1", "w2"):
            with self.subTest(missing=missing):
                self.put("records/words.json", {"words": [w for w in self.words if w["id"] != missing]})
                self.refused(lambda: settle.retime(self.v))
                refusal = self.get("records/checks/cut.json")
                self.assertEqual(refusal["state"], "fail")
                self.assertFalse(refusal["ok"])
                self.assertIn("s1", str(refusal["detail"]))
                self.assertEqual((self.v.folder / "records/cut_plan2.json").read_bytes(), before)

    def test_overlays_keep_the_settled_plan_key(self):
        (self.v.folder / "records/shorts_plan.json").unlink()
        key = settle.settle(self.v)
        self.overlays(key)
        self.put("records/shorts_plan.json", {"plan": key, "shorts": [
            {"id": "full", "full": True, "point": "whole story"}]})
        settle.settle(self.v)
        self.assertEqual(self.get("records/cut_plan2.json")["key"], key)
        self.assertNotIn("records/shorts_plan.json", self.get("records/cut_plan2.json")["made_from"])
        spec = self.get("records/shorts_spec.json")
        self.assertEqual(spec["made_from"], {
            "records/shorts_plan.json": filehash(self.v.folder / "records/shorts_plan.json")})
        checks.check(self.v)
        self.assertEqual(self.get("records/checks/cut.json")["state"], "pass")
        shorts = self.get("records/shorts_plan.json")
        shorts["shorts"][0]["point"] = "revised point"
        self.put("records/shorts_plan.json", shorts)
        checks.check(self.v)  # the shorts plan changed after settle: its spec is stale
        self.assertEqual(self.get("records/checks/cut.json")["state"], "blocked")
        self.assertIn("shorts_spec.json: stale", self.get("records/checks/cut.json")["detail"])
        settle.settle(self.v)
        self.assertEqual(self.get("records/cut_plan2.json")["key"], key)
        self.assertNotEqual(self.get("records/shorts_spec.json")["key"], spec["key"])

    def test_single_video_writer(self):
        self.assertIs(plan.writer, engine.writer)

    def test_finished_is_required(self):
        del self.words[1]["finished"]
        self.sync()
        self.refused(lambda: settle.settle(self.v))
        self.assertFalse((self.v.folder / "records/cut_plan2.json").exists())
        self.words[1]["finished"] = False
        self.sync()
        settle.settle(self.v)
        self.assertIs(self.get("records/cut_plan2.json")["spans"][0]["finished"], False)

    def test_same_inputs_same_plan(self):
        first = settle.settle(self.v)
        path = self.v.folder / "records/cut_plan2.json"
        original = path.read_bytes()
        self.assertEqual(settle.settle(self.v), first)
        self.assertEqual(path.read_bytes(), original)
        self.maker["plan_version"] = "2"
        self.put("records/cut_plan.json", self.maker)
        replace = plan.os.replace
        attempts = []
        def crash(temporary, target):
            if target.name == "cut_plan2.json":
                attempts.append(json.loads(Path(temporary).read_text()))
                raise OSError("crash before atomic replacement")
            return replace(temporary, target)
        with patch.object(plan.os, "replace", crash):
            self.refused(lambda: settle.settle(self.v))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(attempts[0]["plan_version"], "2")
        self.assertFalse(list(path.parent.glob(".cut_plan2.json.*")))
        second = settle.settle(self.v)
        self.assertNotEqual(first, second)
        changed = path.read_bytes()
        self.assertEqual(settle.settle(self.v), second)
        self.assertEqual(path.read_bytes(), changed)
        (self.v.folder / "source.mp4").write_bytes(b"new synthetic source revision")
        self.assertNotEqual(settle.settle(self.v), second)

    def test_never_changes_owner_files(self):
        files = [self.v.folder / "source.mp4", self.v.folder / "owner.txt"]
        before = {str(p): filehash(p) for p in files}
        self.locked()
        self.words[0]["flags"] = ["false_start"]
        self.put("records/words.json", {"words": self.words})
        key = settle.retime(self.v)
        self.overlays(key)
        checks.check(self.v)
        self.assertEqual(self.get("records/checks/cut.json")["state"], "fail")
        (self.v.folder / "records/words.json").write_text("torn")
        checks.check(self.v)
        self.assertEqual(self.get("records/checks/cut.json")["state"], "blocked")
        self.assertEqual({str(p): filehash(p) for p in files}, before)
