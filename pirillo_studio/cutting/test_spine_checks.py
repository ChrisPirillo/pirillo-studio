"""Cutting's checks run through the real spine engine (docs/cutting.md section 2); the host switch is seam 3b."""
import json
import threading

from pirillo_studio.cutting import checks, settle, spine_checks
from pirillo_studio.cutting.test_cutting import Fixture
from pirillo_studio.spine import engine
from pirillo_studio.spine.test_host import Host

LANDS = {"lands": True, "landing_fact": {"fact": "the price", "earlier": []}, "gag_setups": [],
         "button": {"line": "none"}, "each_point_once": [], "open_loops": [], "pace": [], "look": []}


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

    def mark(self):  # the plan a paper read names: the engine's judged digest of paper and plan
        return engine.judged({p: {"sha256": engine.filehash(self.v.folder / p)}
                              for p in ("records/paper.md", "records/cut_plan2.json")}, [])

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
        self.stage("paper", lambda: self.put("records/story_review.json", dict(LANDS, plan=self.mark())))
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


class PaperRead(Bound):
    """guard: a paper read passes only on a complete story_review.json that lands over its own evidence, bound to the
    plan it read; never a manual QC row (queue item 37 seam 3c)."""

    def paper(self, review, plan=True):  # written as a maker would: json.dumps lets Infinity through, canon refuses it
        self.assertTrue(self.to_plan()["ok"])
        return self.stage("paper", lambda: (self.v.folder / "records/story_review.json").write_text(json.dumps(
            dict(review, plan=self.mark()) if plan else review)))

    def test_a_complete_read_that_lands_passes(self):
        self.assertEqual(self.paper(LANDS)["state"], "pass")
        self.assertIn("records/cut_plan2.json", self.seen["paper-read"][-1])

    def test_lands_over_a_point_told_twice_fails_naming_it(self):
        answer = self.paper(dict(LANDS, each_point_once=[{"point": "it is cheap", "tellings": ["0:05", "1:10"]}]))
        self.assertEqual(answer["state"], "fail", answer)
        self.assertIn("told 2 times (it is cheap)", answer["detail"]["words"][0])

    def test_a_missing_read_and_a_non_finite_gap_fail_with_every_problem(self):
        review = {k: v for k, v in LANDS.items() if k != "pace"}
        review["open_loops"] = [{"promise": "a reveal at 0:10", "paid": "2:00", "gap_s": float("inf")}]
        answer = self.paper(review)
        self.assertEqual(answer["state"], "fail", answer)
        self.assertEqual(sum("it names no" in w for w in answer["detail"]["words"]), 2, answer)

    def test_misshapen_nested_evidence_fails(self):
        review = dict(LANDS, landing_fact={"fact": "the price"}, each_point_once=[{"point": "p", "tellings": [None]}],
                      open_loops=[{"promise": "a reveal", "paid": "2:00", "gap_s": 110, "tease": 123}])
        words = self.paper(review)["detail"]["words"]
        self.assertEqual(sum("it names no" in w for w in words), 2, words)
        self.assertTrue(any("left open (a reveal: paid 110 s later with no tease" in w for w in words), words)

    def test_an_old_review_fails_a_changed_plan_naming_the_new_one(self):
        self.assertEqual(self.paper(LANDS)["state"], "pass")
        old = (self.v.folder / "records/story_review.json").read_text()
        (self.v.folder / "records/paper.md").write_text("an edited paper")
        engine.run(self.e, "plan", rerun=True)
        self.assertTrue(engine.check(self.e, "plan")["ok"])
        engine.hand_off(self.e, "plan")
        engine.run(self.e, "paper", rerun=True)
        (self.v.folder / "records/story_review.json").write_text(old)
        answer = engine.check(self.e, "paper")
        self.assertEqual(answer["state"], "fail", answer)
        self.assertIn(self.mark(), answer["detail"]["words"][0])

    def test_a_read_that_does_not_land_fails_for_a_new_plan(self):
        answer = self.paper(dict(LANDS, lands=False))
        self.assertEqual(answer["state"], "fail", answer)
        self.assertIn("does not land", answer["detail"]["words"][-1])

    def test_unreadable_or_moved_review_is_blocked(self):
        self.paper(LANDS)
        paper = engine.find(self.e.recipe()[1], "paper")
        judge = spine_checks.checks(self.v)["paper-read"][1]
        evidence = engine.evidence(self.e, paper, "paper-read")
        (self.v.folder / "records/story_review.json").write_text("{not json")
        self.assertEqual(judge(evidence)["state"], "blocked")
        evidence = engine.evidence(self.e, paper, "paper-read")
        self.assertEqual(judge(evidence)["state"], "blocked")
