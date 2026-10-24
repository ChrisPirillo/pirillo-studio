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
            paths = stage["inputs"] + stage["outputs"] + [ENVELOPE]
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
        # Evidence built by hand: a refused build writes no Review.md, and the engine blocks on that before this check
        # runs (docs/release.md section 4, an undecided interface contract).
        ev = self.evidence("release")
        del ev[REVIEW]
        self.state("release", "fail", ev)

    def test_release_undeclared_completion_envelope_blocks(self):
        self.built()
        ev = self.evidence("release")
        del ev[ENVELOPE]
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

    def test_engine_passes_only_a_current_release(self):
        # A minimal recipe explicitly supplies the envelope absent from today's bundled recipe.
        stage = copy.deepcopy(next(s for s in self.v.stages["stages"] if s["id"] == "release"))
        stage["outputs"].append(ENVELOPE)
        stage["next_actor"] = "packager"
        recipe = {"external": stage["inputs"], "stages": [stage]}
        self.v.stages = recipe
        self.put("records/job.json", {"recipe": "fixture", "id": "video"})
        host = Host()
        e = engine.Video(self.v.folder, start=host.start, ask=host.ask, tell=host.tell, href=lambda p: p,
                         checks=spine_checks.checks(self.v), recipes={"fixture": recipe})
        engine.run(e, "release")
        self.built()
        self.assertEqual(engine.check(e, "release")["state"], "pass")
        self.assertEqual(self.get("records/spine.json")["stages"]["release"]["state"], "pass")
        self.put("thumbs/a.jpg", b"different bytes")
        # A previously successful engine attempt cannot authorize a changed transitive input.
        self.assertEqual(engine.check(e, "release")["state"], "blocked")
