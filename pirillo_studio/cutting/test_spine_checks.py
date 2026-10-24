"""Cutting's checks run through the real spine engine (docs/cutting.md section 2); the host switch is seam 3b."""
import threading

from pirillo_studio.cutting import checks, settle, spine_checks
from pirillo_studio.cutting.test_cutting import Fixture
from pirillo_studio.spine import engine
from pirillo_studio.spine.test_engine import Host


class Bound(Fixture):
    """e2e: records/spine.json stages plan, paper and overlays judged by cutting's own checks, and story_map.json."""

    def setUp(self):
        super().setUp()
        self.put("records/job.json", {"voice_over": [], "recipe": "plain", "id": "video"})
        self.put("records/faces.json", {"faces": []})
        (self.v.folder / "records/shorts_plan.json").unlink()
        self.host = Host()
        bound = spine_checks.checks(self.v)
        self.seen = {}
        catalog = engine.data("catalog.json")
        self.e = self.host.video(self.v.folder, catalog=catalog)
        for c, (version, fn) in bound.items():  # every other check stays the fake's pass, as test_engine does
            self.e.checks[c] = (version, self.spy(c, fn))
        engine.validate(engine.data("recipes.json"), catalog)

    def spy(self, c, fn):
        def judged(evidence):
            self.seen.setdefault(c, []).append(evidence)
            return fn(evidence)
        return judged

    def stage(self, sid, make=lambda: None):
        engine.run(self.e, sid)
        make()
        answer = engine.check(self.e, sid)
        if answer["ok"]:
            engine.hand_off(self.e, sid)
        return answer

    def to_plan(self):
        self.stage("media")
        return self.stage("plan", lambda: settle.settle(self.v))

    def to_overlays(self):
        self.assertTrue(self.to_plan()["ok"])
        self.stage("paper", lambda: self.put("records/story_review.json", {"read": "lands"}))
        key = self.get("records/cut_plan2.json")["key"]

        def overlays():
            self.overlays(key)
            self.put("records/shorts_plan.json", {"plan": key, "shorts": [
                {"id": "full", "full": True, "point": "whole story"}]})
            settle.settle(self.v)
        return self.stage("overlays", overlays)

    def test_cut_plan_passes_before_any_overlay(self):
        answer = self.to_plan()
        self.assertEqual(answer["state"], "pass", answer)
        self.assertFalse((self.v.folder / "records/overlay_plan.json").exists())
        self.assertIn("records/probe.json", self.seen["cut-plan"][-1])

    def test_a_false_start_fails_naming_span_and_rule(self):
        self.words[2]["flags"] = ["false_start"]
        self.sync()
        answer = self.to_plan()
        self.assertEqual(answer["state"], "fail", answer)
        self.assertTrue(any(w.startswith("cut-plan: takes: span s2 at ") for w in answer["detail"]["words"]), answer)

    def test_a_missing_words_file_is_blocked_and_named(self):
        self.assertTrue(self.to_plan()["ok"])
        (self.v.folder / "records/words.json").unlink()
        plan = engine.find(self.e.recipe()[1], "plan")
        evidence = engine.evidence(self.e, plan, "cut-plan")
        answer = spine_checks.checks(self.v)["cut-plan"][1](evidence)
        self.assertEqual(answer["state"], "blocked")
        self.assertIn("records/words.json: missing", answer["words"][0])

    def test_overlay_plan_passes_then_fails_on_a_card_off_the_kept_words(self):
        answer = self.to_overlays()
        self.assertEqual(answer["state"], "pass", answer)
        self.assertEqual(self.get("records/checks/cut.json")["state"], "pass")
        checks.request_arc(self.v)  # the record overlay-plan wrote authorizes the arc
        engine.run(self.e, "overlays", rerun=True)
        self.put("records/overlay_plan.json", {"plan": self.get("records/cut_plan2.json")["key"], "cards": [
            {"id": "c1", "kind": "instruction", "anchor": ["w2", "w3"], "text": "across a cut"}]})
        answer = engine.check(self.e, "overlays")
        self.assertEqual(answer["state"], "fail", answer)
        self.assertTrue(any("cards c1 anchor leaves kept span" in w for w in answer["detail"]["words"]), answer)

    def test_a_changed_probe_reopens_the_plan(self):
        self.assertTrue(self.to_plan()["ok"])
        self.put("records/probe.json", {"duration_s": 80, "fps": 30, "sample_rate": 48000})
        self.assertEqual(self.stage("media")["state"], "pass")  # media made it: media passes it on first
        self.assertEqual((engine.next(self.e)["stage"], engine.next(self.e)["state"]), ("plan", "ready"))

    def test_the_check_runs_under_the_engines_writer_without_waiting_on_it(self):
        self.stage("media")
        engine.run(self.e, "plan")
        settle.settle(self.v)
        done = []
        worker = threading.Thread(target=lambda: done.append(engine.check(self.e, "plan")), daemon=True)
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive(), "cut-plan waited on the writer the engine holds")
        self.assertEqual(done[0]["state"], "pass", done)

    def test_locked_cut_passes_only_on_cuttings_current_lock(self):
        locked = engine.find(self.e.recipe()[1], "locked")
        judge = spine_checks.checks(self.v)["locked-cut"][1]
        self.put("records/shorts_plan.json", {"plan": "sha256:pending", "shorts": [
            {"id": "full", "full": True, "point": "whole story"}]})
        self.locked()
        self.assertEqual(judge(engine.evidence(self.e, locked, "locked-cut"))["state"], "pass")
        story = self.get("records/story_map.json")
        self.put("records/story_map.json", dict(story, tail="s1"))
        self.assertEqual(judge(engine.evidence(self.e, locked, "locked-cut"))["state"], "fail")
        self.put("records/story_map.json", story)
        self.approve(words="a fresh approval, other bytes")
        answer = judge(engine.evidence(self.e, locked, "locked-cut"))
        self.assertEqual(answer["state"], "fail")
        self.assertIn("arc_decision", answer["words"][0])
        (self.v.folder / "records/story_map.json").unlink()
        self.assertEqual(judge(engine.evidence(self.e, locked, "locked-cut"))["state"], "blocked")

    def test_a_read_neither_declared_nor_pinned_is_blocked_and_named(self):
        self.add_patch()  # a patched span's check reads records/voice.json, which no plan record pins
        answer = self.to_plan()
        self.assertEqual(answer["state"], "blocked", answer)
        self.assertIn("reads records/voice.json: neither", answer["detail"]["words"][0])
