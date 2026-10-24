"""Release validators bound to engine evidence, without running build or deciding for the owner."""
import copy
from unittest.mock import patch

from pirillo_studio.release import api, spine_checks
from pirillo_studio.release.inputs import ENVELOPE, RELEASE, REVIEW, TEXT, TEXT_READ
from pirillo_studio.release.test_release import Fixture
from pirillo_studio.spine import engine
from pirillo_studio.spine.files import filehash
from pirillo_studio.spine.test_host import Host


class Bound(Fixture):
    """e2e: records/spine.json and release.json, judged from current evidence without a maker or owner write."""

    def evidence(self, name):
        catalog = engine.data("catalog.json")["checks"][name]
        paths = catalog["reads"]
        if name == "release":
            stage = next(s for s in self.v.stages["stages"] if s["id"] == "release")
            paths = stage["inputs"] + stage["outputs"]
        return {p: {"sha256": filehash(self.v.folder / p)} for p in paths}

    def judge(self, name, evidence=None):
        fn = spine_checks.checks(self.v)[name][1]
        with patch.object(api, "build", side_effect=AssertionError("check ran maker")), \
                patch.object(api, "decide", side_effect=AssertionError("check decided")):
            return fn(self.evidence(name) if evidence is None else evidence)

    def state(self, name, expected, evidence=None):
        answer = self.judge(name, evidence)
        self.assertEqual(answer["state"], expected, answer)
        return answer

    def test_titles_pass_then_changed_evidence_blocks(self):
        self.state("titles", "pass")
        ev = self.evidence("titles")
        self.publish["title"] = "New title"
        self.sync()
        self.state("titles", "blocked", ev)

    def test_titles_fail(self):
        self.v.scores = [0, 0, 0]
        answer = self.state("titles", "fail")
        self.assertIn("release.titles", str(answer["words"]))

    def test_titles_unpinned_read_blocks(self):
        ev = self.evidence("titles")
        del ev["records/words.json"]
        self.assertIn("records/words.json", str(self.state("titles", "blocked", ev)["words"]))

    def test_text_pass_then_changed_evidence_blocks(self):
        self.state("text-read", "pass")
        ev = self.evidence("text-read")
        self.put(TEXT, "Changed text")
        self.state("text-read", "blocked", ev)

    def test_text_fail(self):
        self.sync(text_extra="| sentence | DOESN'T READ |")
        self.state("text-read", "fail")
        self.sync()
        self.v.lint = ["bad sentence"]
        self.state("text-read", "fail")
        self.v.lint = []
        self.put(TEXT, "forbidden thing")
        self.sync()
        self.state("text-read", "fail")
        self.put(TEXT, "x" * (self.v.profile["platforms"]["board"]["max_chars"] + 1))
        self.sync()
        self.state("text-read", "fail")

    def test_text_unpinned_read_blocks(self):
        ev = self.evidence("text-read")
        del ev[TEXT_READ]
        self.assertIn(TEXT_READ, str(self.state("text-read", "blocked", ev)["words"]))

    def test_release_pass_before_decision_then_changed_pinned_read_blocks(self):
        self.built()
        before = {str(p): p.read_bytes() for p in self.v.folder.rglob("*") if p.is_file()}
        self.state("release", "pass")
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.v.folder.rglob("*") if p.is_file()})
        self.assertEqual(api.check(self.v)["state"], "waiting_owner")
        self.put("thumbs/a.jpg", b"new image")
        self.assertIn("thumbs/a.jpg", str(self.state("release", "blocked")["words"]))
        (self.v.folder / "thumbs/a.jpg").unlink()
        self.assertIn("thumbs/a.jpg", str(self.state("release", "blocked")["words"]))

    def test_release_fail(self):
        self.v.scores = [0, 0, 0]
        self.assertEqual(api.build(self.v)["state"], "refused")
        self.state("release", "fail")

    def test_release_undeclared_completion_envelope_blocks(self):
        self.built()
        ev = self.evidence("release")
        ev.pop(ENVELOPE, None)
        self.assertIn(ENVELOPE, str(self.state("release", "blocked", ev)["words"]))

    def test_nonfinite_scorer_and_symlink_block(self):
        self.v.scores = [float("nan"), 70, 60]
        self.state("titles", "blocked")
        self.v.scores = [80, 70, 60]
        p = self.v.folder / "records/words.json"
        p.rename(p.with_name("other.json"))
        p.symlink_to("other.json")
        self.state("titles", "blocked")

    def test_missing_identity_and_partial_build_block(self):
        rec = self.built()
        broken = dict(rec)
        del broken["key"]
        self.put(RELEASE, broken)
        self.state("release", "blocked")
        self.put(RELEASE, rec)
        self.put(ENVELOPE, {"reads": {RELEASE: "sha256:unfinished"}})
        self.state("release", "blocked")

    def test_settings_reopen_the_version_and_stale_release(self):
        self.built()
        version = spine_checks.checks(self.v)["release"][0]
        self.v.profile["titles"]["min_score"] += 1
        self.assertNotEqual(version, spine_checks.checks(self.v)["release"][0])
        self.state("release", "blocked")

    def test_unbuildable_profile_has_a_version_and_blocks(self):
        before = spine_checks.checks(self.v)["titles"][0]
        with patch.object(self.v, "settings", side_effect=ValueError("missing own_urls.playlists")):
            version, check = spine_checks.checks(self.v)["titles"]
            self.assertNotEqual(before, version)
            self.assertEqual(version, spine_checks.checks(self.v)["titles"][0])
            answer = check(self.evidence("titles"))
            self.assertEqual(answer["state"], "blocked")
            self.assertIn("missing own_urls.playlists", str(answer["words"]))

    def engine_release(self):
        """Run the bundled release stage with upstream artifacts supplied by the fixture."""
        stage = copy.deepcopy(next(s for s in self.v.stages["stages"] if s["id"] == "release"))
        stage["next_actor"] = "packager"
        recipe = {"external": stage["inputs"], "stages": [stage]}
        self.v.stages = recipe
        self.put("records/job.json", {"recipe": "fixture", "id": "video"})
        host = Host()
        e = engine.Video(self.v.folder, start=host.start, ask=host.ask, tell=host.tell, href=lambda p: p,
                         checks=spine_checks.checks(self.v), recipes={"fixture": recipe})
        engine.run(e, "release")
        return e

    def test_engine_passes_only_a_current_release(self):
        e = self.engine_release()
        self.built()
        self.assertEqual(engine.check(e, "release")["state"], "pass")
        self.assertEqual(self.get("records/spine.json")["stages"]["release"]["state"], "pass")
        self.put("thumbs/a.jpg", b"different bytes")
        # A previously successful engine attempt cannot authorize a changed transitive input.
        self.assertEqual(engine.check(e, "release")["state"], "blocked")

    def test_engine_envelope_changes_reopen_and_missing_blocks(self):
        e = self.engine_release()
        self.built()
        self.assertEqual(engine.check(e, "release")["state"], "pass")
        self.put(ENVELOPE, {"reads": {RELEASE: "sha256:unfinished"}})
        answer = engine.check(e, "release")
        self.assertEqual(answer["state"], "blocked", answer)
        self.assertIn(RELEASE, str(answer))
        (self.v.folder / ENVELOPE).unlink()
        answer = engine.check(e, "release")
        self.assertEqual(answer["state"], "blocked", answer)
        self.assertIn(ENVELOPE, str(answer))

    def test_refusal_replaces_its_own_page_and_keeps_an_edited_one(self):
        self.built()
        self.v.lint = ["release text needs revision"]
        self.publish["title"] = "Gadget: refused"
        self.sync()
        rec = api.build(self.v)
        self.assertEqual(rec["state"], "refused")
        self.assertTrue(self.text(REVIEW).startswith(f"release {rec['key']}\n\nRelease refused"))
        edited = self.text(REVIEW) + "\nhis note\n"
        self.put(REVIEW, edited)
        self.publish["title"] = "Gadget: still refused"
        self.sync()
        self.assertNotEqual(api.build(self.v)["key"], rec["key"])
        self.assertEqual(self.text(REVIEW), edited)

    def test_engine_refusal_reaches_packager_and_recovers(self):
        e = self.engine_release()
        self.v.lint = ["release text needs revision"]
        rec = api.build(self.v)
        answer = engine.check(e, "release")
        self.assertEqual(answer["state"], "fail", answer)
        self.assertIn("packager", str(answer["next_action"]))
        for detail in rec["detail"]:
            self.assertIn(str(detail["value"]), "\n".join(answer["detail"]["words"]))
            self.assertIn(str(detail), self.text(REVIEW))
        (self.v.folder / REVIEW).unlink()
        api.build(self.v)
        self.assertTrue((self.v.folder / REVIEW).is_file())
        self.v.lint = []
        self.publish["title"] = "Gadget: works better"
        self.sync()
        engine.run(e, "release")
        self.built()
        self.assertEqual(engine.check(e, "release")["state"], "pass")

    def test_other_stage_missing_output_still_blocks(self):
        stage = copy.deepcopy(next(s for s in self.v.stages["stages"] if s["id"] == "text"))
        stage["next_actor"] = "writer"
        recipe = {"external": stage["inputs"], "stages": [stage]}
        self.put("records/text_plan.md", "Plan")
        self.put("records/job.json", {"recipe": "fixture", "id": "video"})
        e = Host().video(self.v.folder, recipes={"fixture": recipe})
        engine.run(e, "text")
        (self.v.folder / TEXT).unlink()
        answer = engine.check(e, "text")
        self.assertEqual(answer["state"], "blocked", answer)
        self.assertIn("maker outputs missing: " + TEXT, str(answer))
