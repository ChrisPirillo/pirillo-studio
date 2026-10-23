"""Cutting refusals and freshness contracts; test_off_is_unchanged belongs to host seam 3."""
import copy
import json
from unittest.mock import patch

from pirillo_studio.cutting import checks, plan, settle
from pirillo_studio.cutting.test_cutting import Fixture
from pirillo_studio.spine.files import filehash


class Checks(Fixture):
    """e2e: records/checks/cut.json prevents bad plans reaching records/story_map.json."""

    def verdict(self, state, rule=None, span=None):
        checks.check(self.v)
        result = self.get("records/checks/cut.json")
        self.assertEqual(result["state"], state, result)
        if rule:
            self.assertTrue(result["checks"][rule], result)
        if span:
            self.assertIn(span, json.dumps(result["checks"][rule]))
        return result

    def test_no_false_start_or_flub_survives(self):
        for flag in ("false_start", "repeat", "stumble", "filler"):
            with self.subTest(flag=flag):
                self.words[2]["flags"] = [flag]
                self.sync()
                key = settle.settle(self.v)
                self.overlays(key)
                self.verdict("fail", "takes", "s2")
                self.maker["named"] = [{"at_src": 10, "rule": "takes", "why": ""}]
                self.put("records/cut_plan.json", self.maker)
                key = settle.settle(self.v)
                self.overlays(key)
                self.verdict("fail", "takes", "s2")
                self.maker["named"][0]["why"] = "the repeated word is deliberate"
                self.put("records/cut_plan.json", self.maker)
                key = settle.settle(self.v)
                self.overlays(key)
                self.verdict("pass")
                self.maker["named"] = []

    def test_jcut_never_clips_a_word(self):
        cut = self.ready()
        for index, lead in ((0, .2), (1, 9), (1, 3.5)):
            with self.subTest(index=index, lead=lead):
                candidate = copy.deepcopy(cut)
                candidate["spans"][index].update(enter="jcut", lead_s=lead, lead_from="motion onset")
                self.put("records/cut_plan2.json", candidate)
                self.verdict("fail", "jcuts", candidate["spans"][index]["id"])
                self.assertFalse(self.v.asked)
        self.put("records/cut_plan2.json", cut)
        self.verdict("pass")

    def test_edges_sit_in_quiet(self):
        self.sync()
        # Gate silence is not the room floor; an inhale starts after the legal tail.
        audio = self.v.readings["audio"]
        quiet = next(i for i, row in enumerate(audio) if row["start"] == 4.2)
        audio[quiet:quiet + 1] = [
            {"start": 4.2, "end": 4.35, "db": -55, "kind": "quiet"},
            {"start": 4.35, "end": 4.7, "db": -24, "kind": "breath"},
            {"start": 4.7, "end": 10, "db": -55, "kind": "quiet"}]
        audio[0:1] = [{"start": 0, "end": .5, "db": -120, "kind": "quiet"},
                      {"start": .5, "end": 1, "db": -55, "kind": "quiet"}]
        cut = self.ready()
        self.assertLessEqual(cut["spans"][0]["src"][1], 4.35)
        for span in cut["spans"]:
            first = next(w for w in self.words if w["id"] == span["words"][0])
            self.assertLess(span["src"][0], first["start"])
            self.assertGreater(span["edges"]["head_lead_s"], 0)
        self.assertFalse(self.get("records/checks/cut.json")["checks"]["edges"])
        for row in self.v.readings["audio"]:
            if row["end"] == 1 and row["kind"] == "quiet":
                row["end"] = .98
            elif row["start"] == 1 and row["kind"] == "speech":
                row["start"] = .98
            elif row["end"] == 4.2 and row["kind"] == "speech":
                row["end"] = 4.22
            elif row["start"] == 4.2 and row["kind"] == "quiet":
                row["start"] = 4.22
        measured = self.ready()
        self.assertAlmostEqual(cut["spans"][0]["src"][0] - measured["spans"][0]["src"][0], .02)
        self.assertAlmostEqual(measured["spans"][0]["src"][1] - cut["spans"][0]["src"][1], .02)

    def test_unreadable_never_passes(self):
        self.ready()
        for name in ("records/cut_plan2.json", "records/words.json", "records/probe.json"):
            path = self.v.folder / name
            original = path.read_bytes()
            for content in (None, b"{torn"):
                with self.subTest(name=name, content=content):
                    if content is None:
                        path.unlink()
                    else:
                        path.write_bytes(content)
                    result = self.verdict("blocked")
                    self.assertIn(name, json.dumps(result))
                    self.refused(lambda: checks.request_arc(self.v))
                    path.write_bytes(original)
        for error in (OSError("cannot measure source audio"), RuntimeError("host probe failed")):
            with patch.object(self.v, "measure", side_effect=error):
                self.verdict("blocked")
        for field in ("duration_s", "fps", "audio", "motion"):
            readings = copy.deepcopy(self.v.readings)
            del self.v.readings[field]
            self.verdict("blocked")
            self.v.readings = readings
        for name in ("own_words", "edges", "takes", "jcuts", "shots", "cards", "patches", "cold_open", "shorts"):
            with self.subTest(check=name), patch.object(checks, name, side_effect=RuntimeError("unavailable")):
                if name == "patches":
                    self.add_patch()
                    key = settle.settle(self.v)
                    self.overlays(key)
                result = self.verdict("blocked")
                self.assertIn(name.replace("_", "-"), result["detail"])
        self.assertFalse(self.v.asked)

    def test_a_hidden_picture_is_covered_and_a_pop_holds_its_limit(self):
        self.put("records/job.json", {"voice_over": [[10, 13.2]]})
        self.maker["spans"][1]["hide_picture"] = True
        self.put("records/cut_plan.json", self.maker)
        key = settle.settle(self.v)
        self.overlays(key)
        cut = self.get("records/cut_plan2.json")
        self.assertTrue(cut["spans"][1]["hide_picture"])
        self.verdict("fail", "cards", "s2")
        overlay = {"plan": key, "cards": [{"id": "cover", "kind": "cover", "covers": "s2", "text": "Details matter."}]}
        self.put("records/overlay_plan.json", overlay)
        self.verdict("pass")
        patch_record = self.add_patch()
        key = settle.settle(self.v)
        self.overlays(key)
        overlay["plan"] = key
        self.put("records/overlay_plan.json", overlay)
        self.verdict("pass")
        for field, value in (("sample_rate", 44100), ("sample_count", 1), ("duration_s", 4)):
            changed = dict(patch_record, **{field: value})
            self.put("records/patches/p1.json", changed)
            self.maker["spans"][1]["patch"]["sha"] = filehash(self.v.folder / "records/patches/p1.json")
            self.put("records/cut_plan.json", self.maker)
            key = settle.settle(self.v)
            self.overlays(key)
            overlay["plan"] = key
            self.put("records/overlay_plan.json", overlay)
            self.verdict("fail", "patches", "s2")
        self.put("records/patches/p1.json", patch_record)
        self.maker["spans"][1]["patch"]["sha"] = "sha256:incorrect"
        self.put("records/cut_plan.json", self.maker)
        key = settle.settle(self.v)
        self.overlays(key)
        overlay["plan"] = key
        self.put("records/overlay_plan.json", overlay)
        result = self.verdict("fail", "patches", "s2")
        self.assertIn("hash", json.dumps(result["checks"]["patches"]))
        self.reset()
        # A valid silent face pop sits after a completed line and before the next speech.
        self.maker["spans"].insert(1, {"id": "pop", "order": 2, "src": [4.5, 5], "words": [],
                                      "enter": "pop", "shot": "face",
                                      "expression": {"after_word": "w2", "seen": "smiles"}})
        self.maker["spans"][2]["order"] = 3
        self.maker["spans"][3]["order"] = 4
        self.sync()
        cut = self.ready()
        for change in ({"src": [4.5, 7]}, {"shot": "med"}, {"expression": {"after_word": "w1", "seen": "smiles"}}):
            candidate = copy.deepcopy(cut)
            candidate["spans"][1].update(change)
            self.put("records/cut_plan2.json", candidate)
            self.verdict("fail", "shots", "pop")
        self.put("records/cut_plan2.json", cut)
        cut["spans"][0]["finished"] = False
        self.put("records/cut_plan2.json", cut)
        self.verdict("fail", "shots", "pop")

    def test_a_short_stands_alone(self):
        for word, bounds in zip(self.words, [(1, 14), (16, 29), (40, 41.2), (42, 43.2), (50, 51.2), (52, 53.2)]):
            word["start"], word["end"] = bounds
        for span, bounds in zip(self.maker["spans"], [(.9, 29.4), (39.9, 43.5), (49.9, 61.2)]):
            span["src"] = list(bounds)
        self.sync()
        key = settle.settle(self.v)
        shorts = {"plan": key, "shorts": [{"id": "full", "full": True, "point": "whole story"},
                  {"id": "one", "point": "subject works", "spans": [["w1", "w2"]],
                   "opens_on": "w1", "ends_on": "w2", "stands_alone": {"opens_on_subject": True}}]}
        self.put("records/shorts_plan.json", shorts)
        key = settle.settle(self.v)
        self.overlays(key)
        self.verdict("pass")
        settled = self.get("records/shorts_spec.json")
        self.assertEqual(len(settled["shorts"][1]["pieces"]), 2)
        self.assertEqual(settled["shorts"][1]["spans"], [["w1", "w2"]])
        self.words[0]["text"] = "This"
        self.put("records/words.json", {"words": self.words})
        shorts["shorts"][1]["stands_alone"] = {"opens_on_subject": True}
        self.put("records/shorts_plan.json", shorts)
        key = settle.settle(self.v)
        self.overlays(key)
        self.verdict("fail", "shorts", "one")
        shorts["shorts"][1]["stands_alone"] = {"context": "Subject means the useful result"}
        self.put("records/shorts_plan.json", shorts)
        key = settle.settle(self.v)
        self.overlays(key)
        self.verdict("pass")
        settled = self.get("records/shorts_spec.json")
        for mutation in ("band", "words", "full", "point", "decision"):
            candidate = copy.deepcopy(settled)
            if mutation == "band":
                candidate["shorts"][1]["pieces"] = [[1, 1.01]]
            elif mutation == "words":
                candidate["shorts"][1]["spans"] = [["gone", "w2"]]
            elif mutation == "full":
                candidate["shorts"] = candidate["shorts"][1:]
            elif mutation == "point":
                candidate["shorts"][0]["point"] = ""
            else:
                candidate["shorts"][1]["point"] = "a different decision"
            self.put("records/shorts_spec.json", candidate)
            self.verdict("fail", "shorts")

    def test_a_stale_pass_is_refused(self):
        cut = self.locked()
        self.put("records/shorts_plan.json", {"plan": cut["key"], "shorts": [{"id": "full", "full": True, "point": "whole story"}]})
        self.locked()
        names = ("records/job.json", "records/overlay_plan.json", "records/bed.json",
                 "records/callouts_plan.json", "records/shorts_plan.json", "records/shorts_spec.json")
        for name in names:
            with self.subTest(name=name):
                original = (self.v.folder / name).read_bytes()
                value = self.get(name)
                value["revision"] = "changed since approval"
                self.put(name, value)
                self.refused(lambda: checks.request_arc(self.v))
                self.refused(lambda: checks.lock(self.v))
                (self.v.folder / name).write_bytes(original)
        self.add_patch()
        key = settle.settle(self.v)
        self.overlays(key)
        self.put("records/overlay_plan.json", {"plan": key, "cards": [
            {"id": "cover", "kind": "cover", "covers": "s2", "text": "Details matter."}]})
        self.verdict("pass")
        checks.request_arc(self.v)
        self.approve()
        checks.lock(self.v)
        patch_record = self.get("records/patches/p1.json")
        patch_record["take"] = "a different take"
        self.put("records/patches/p1.json", patch_record)
        self.refused(lambda: checks.request_arc(self.v))
        self.refused(lambda: checks.lock(self.v))
        patch_record["take"] = "take1"
        self.put("records/patches/p1.json", patch_record)
        before = self.get("records/checks/cut.json")
        del before["key"]
        self.put("records/checks/cut.json", before)
        self.refused(lambda: checks.request_arc(self.v))
        self.refused(lambda: checks.lock(self.v))
        self.verdict("pass")
        checks.request_arc(self.v)
        self.approve()
        checks.lock(self.v)
        self.assertEqual(self.get("records/story_map.json")["check"], self.get("records/checks/cut.json")["key"])

    def test_checks_never_read_recipe(self):
        self.ready()
        first = (self.v.folder / "records/checks/cut.json").read_bytes()
        for recipe in ("plain", "some-other-recipe"):
            self.put("records/recipe.json", {"recipe": recipe})
            self.v.recipe = lambda: self.fail("cutting read recipe")
            checks.check(self.v)
            self.assertEqual((self.v.folder / "records/checks/cut.json").read_bytes(), first)
        cut = self.get("records/cut_plan2.json")
        inputs = {
            "own_words": (cut, self.words), "edges": (cut, self.words, self.v.readings),
            "takes": (cut, self.words), "jcuts": (cut, self.words, self.v.readings),
            "shots": (cut,), "cold_open": (cut,), "patches": (cut, {}, {"picks": []}),
            "cards": (cut, self.words, self.get("records/job.json"), self.get("records/overlay_plan.json"),
                      self.get("records/callouts_plan.json")),
            "shorts": (self.get("records/shorts_plan.json"), self.get("records/shorts_spec.json"), cut, self.words)}
        written = self.get("records/checks/cut.json")["checks"]
        for name, args in inputs.items():
            self.assertEqual(getattr(checks, name)(*args), written[name.replace("_", "-")])

    def test_nonfinite_plan_and_measure_block(self):
        self.ready()
        path = self.v.folder / "records/cut_plan.json"
        value = copy.deepcopy(self.maker)
        value["spans"][0]["src"][0] = float("nan")
        path.write_text(json.dumps(value))
        self.refused(lambda: settle.settle(self.v))
        self.put("records/cut_plan.json", self.maker)
        self.v.readings["fps"] = float("inf")
        self.verdict("blocked")

    def test_path_alias_and_symlink_refuse(self):
        self.ready()
        original = (self.v.folder / "records/words.json").read_bytes()
        outside = self.v.folder.parent / "outside.json"
        outside.write_bytes(original)
        words = self.v.folder / "records/words.json"
        words.unlink()
        words.symlink_to(outside)
        self.verdict("blocked")
        self.refused(lambda: settle.settle(self.v))
        self.assertEqual(outside.read_bytes(), original)
        words.unlink()
        words.write_bytes(original)
        for alias in ("records/../outside.json", "./records/patches/p1.json", "records//patches/p1.json"):
            self.maker["spans"][1]["patch"] = {"record": alias, "sha": "sha256:bad", "words": ["p1", "p2"]}
            self.put("records/cut_plan.json", self.maker)
            self.refused(lambda: settle.settle(self.v))

    def test_change_between_read_and_write_refuses(self):
        self.ready()
        before = (self.v.folder / "records/cut_plan2.json").read_bytes()
        def changing_measure(source):
            (self.v.folder / "source.mp4").write_bytes(b"changed while being measured")
            return copy.deepcopy(self.v.readings)
        with patch.object(self.v, "measure", changing_measure):
            self.refused(lambda: settle.settle(self.v))
        self.assertEqual((self.v.folder / "records/cut_plan2.json").read_bytes(), before)
        self.ready()
        checks.request_arc(self.v)
        self.approve()
        def changing_clock():
            overlay = self.get("records/overlay_plan.json")
            overlay["changed"] = True
            self.put("records/overlay_plan.json", overlay)
            return "2026-01-01T00:00:00Z"
        with patch.object(self.v, "now", changing_clock):
            self.refused(lambda: checks.lock(self.v))
        self.assertFalse((self.v.folder / "records/story_map.json").exists())
