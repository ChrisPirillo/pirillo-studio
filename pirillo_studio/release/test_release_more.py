"""Release contracts 8 to 14 and the boundary probes (docs/release.md section 5); test_release.py holds 1 to 7, and
contract 15, test_switch_off_is_unchanged, is the host switch's (seam 3)."""
import json
import os
import unittest
from unittest import mock

from pirillo_studio.release import api, inputs
from pirillo_studio.release.inputs import DECISION, ENVELOPE, RELEASE, REVIEW
from pirillo_studio.release.test_release import Fixture
from pirillo_studio.spine import engine
from pirillo_studio.spine.files import digest


def stamps(folder):
    return {str(p.relative_to(folder)): p.stat().st_mtime_ns for p in folder.rglob("*") if p.is_file()}


class Contracts(Fixture):
    """e2e: records/release.json, release_decision.json, Review.md and the envelope, read back after each call."""

    def test_decision_binds_what_was_read(self):
        rec = self.built()
        self.approve_all()
        d = self.get(DECISION)
        self.assertEqual(d["release_key"], rec["key"])
        long = rec["outputs"][0]
        self.assertEqual(d["rows"]["long:chirp"], {
            "decision": "approved", "by": "owner", "at": d["rows"]["long:chirp"]["at"],
            "words_sha256": self.rows()["long:chirp"]["words_sha256"], "file_sha256": long["sha256"],
            "sidecar_sha256": long["sidecar"]["sha256"], "thumbnails": [digest(b"picture")]})
        self.assertEqual(d["rows"]["article:board"]["words_sha256"], digest(b"A post about the gadget.\n"))
        everyone = set(self.rows())
        for changed, rows in (("thumbs/a.jpg", {"long:youtube", "long:chirp"}),  # a bound thumbnail changed
                              ("Vertical/main.mp4", {"vertical:clip"}),  # a valid re-render, the manifest updated
                              ("Shorts/alpha.srt", {"short-alpha:youtube", "short-alpha:clip"})):  # new sidecar
            with self.subTest(changed=changed):
                if changed in self.members:
                    self.members[changed] = b"re-rendered " + changed.encode()
                self.put(changed, self.members.get(changed, b"another picture"))
                self.manifest()
                with self.assertRaisesRegex(Exception, "stale"):
                    api.decide(self.v, {"long:chirp": "approved"}, "owner")
                self.built()
                result = api.check(self.v)
                self.assertEqual(set(result["detail"]["waiting"]), rows)
                self.assertEqual(set(self.ledger()), everyone - rows)
                self.assertEqual(self.get(DECISION), d)  # the decision record is his, never rewritten by a build
                api.decide(self.v, {r: "approved" for r in rows}, "owner")
                d = self.get(DECISION)
                self.assertEqual(set(self.ledger()), everyone)

    def test_stale_read_cannot_pass(self):
        self.built()
        self.approve_all()
        self.copy["long"]["chirp"] = "Shorter is better here.\nTopic: Gadgets"
        self.put("records/copy.json", self.copy)  # the copy changed after its independent read
        self.assertEqual(api.check(self.v)["state"], "stale")
        self.assertEqual(self.ledger(), {})
        self.assertEqual(self.qc()["result"], "unavailable")
        self.refused("records/copy_review.md: stale", "story-editor")
        self.sync(copy_extra="| long:chirp | DOESN'T READ |\n")
        self.refused("DOESN'T READ", "packager")
        self.sync()
        self.put("records/text_post.md", "A newer post about the gadget.\n")  # the text changed after its read
        self.refused("records/text_review.md: stale", "story-editor")
        self.sync(text_extra="| para 1 | DOESN'T READ |\n")
        rec = self.refused("records/text_review.md: 1 row(s) DOESN'T READ")
        self.assertEqual([x["maker"] for x in rec["detail"]], ["writer"])
        self.assertEqual(self.ledger(), {})
        self.sync()
        rec = self.built()
        reads = {k: rec[k] for k in ("copy_read_sha256", "text_read_sha256")}
        for row, words in (("article:board", "His own post.\n"), ("long:chirp", "Details matter.\nTopic: Gadgets")):
            api.edit(self.v, row, words, "owner")
            rec = self.built()
        self.assertEqual({k: rec[k] for k in reads}, reads)  # an owner edit leaves both reads current
        for entry in ("release", "captions-read", "text-read"):
            self.assertEqual(self.qc(entry)["result"], "pass", entry)

    def test_unchanged_inputs_write_nothing(self):
        first = self.built()
        self.approve_all()
        before, self.v.scored = stamps(self.v.folder), []
        self.built()
        self.approve_all()
        api.edit(self.v, "long:chirp", self.rows()["long:chirp"]["words"], "owner")
        api.check(self.v)
        self.assertEqual(stamps(self.v.folder), before)
        self.assertEqual(self.v.scored, [])  # nothing measured again: the scorer not called
        self.v.scores = [10, 10, 10]  # a scorer that changed its mind cannot rewrite an unchanged key
        self.assertEqual(self.built(), first)
        self.v.scores = [80, 70, 60]
        for change, differs in (
                (lambda: self.v.profile["platforms"]["chirp"].update(max_chars=120), {"key", "settings", "review"}),
                (lambda: self.publish.update(pinned_comment="The result lasts for years."),
                 {"key", "reads", "publish_sha256", "outputs", "review"})):
            with self.subTest(differs=sorted(differs)):
                old = self.get(RELEASE)
                change()
                self.sync()
                self.assertEqual(api.check(self.v)["state"], "stale")
                self.assertEqual(self.ledger(), {})
                new = self.built()
                self.assertEqual({k for k in old.keys() | new.keys() if old.get(k) != new.get(k)}, differs)
                self.assertEqual(api.check(self.v)["state"], "approved")  # every row's own hashes are unchanged

    def test_owner_short_release(self):
        self.v.name, self.v.stages = "shorts-only", engine.data("recipes.json")["shorts-only"]
        self.members = {"Shorts/alpha.mp4": b"short video", "Shorts/alpha.srt": b"short cues"}
        for p in ("YouTube/main.mp4", "YouTube/main.srt", "Vertical/main.mp4", "Vertical/main.srt",
                  "records/text_post.md", "records/text_review.md", "records/captions/long.json"):
            os.remove(self.v.folder / p)
        self.manifest()
        self.copy = {"shorts": self.copy["shorts"]}
        self.sync()
        rec = self.built()
        self.assertEqual([(o["id"], o["title"]) for o in rec["outputs"]], [("short-alpha", "Gadget: works well")])
        self.assertEqual(list(self.rows()), ["short-alpha:youtube", "short-alpha:clip"])
        self.assertNotIn("records/text_post.md", rec["reads"])
        self.assertFalse(any(r["check"] in ("release.titles", "release.text", "release.disclosure") for r in rec["readings"]))
        self.assertEqual(self.v.scored, [])
        self.approve_all()
        self.assertTrue(api.check(self.v)["ok"])
        self.assertEqual(self.qc()["result"], "pass")

    def test_delivered_files_are_never_renamed(self):
        self.publish["title"] = "Gadget: works/well*"
        self.publish["ab_titles"][0]["title"] = self.publish["title"]
        self.sync()
        rec = self.built()
        self.approve_all()
        self.assertEqual((rec["outputs"][0]["name"], rec["outputs"][0]["file"]), ("Gadget - works-well", "YouTube/main.mp4"))
        delivered = {**self.members, "thumbs/a.jpg": b"picture"}  # every member, sidecars and thumbnails included
        self.assertEqual({p: (self.v.folder / p).read_bytes() for p in delivered}, delivered)
        self.assertFalse({str(p.relative_to(self.v.folder)) for p in self.v.folder.glob("*/*.*")
                          if p.suffix in (".mp4", ".srt", ".jpg")} - set(delivered))
        self.assertEqual({r["file"] for r in api.check(self.v)["send"]} - {None},
                         {p for p in self.members if p.endswith(".mp4")})
        self.put("Shorts/alpha.mp4", b"changed")
        self.refused("Shorts/alpha.mp4: its bytes differ", "rendering")
        self.assertEqual((self.v.folder / "Shorts/alpha.mp4").read_bytes(), b"changed")

    def test_crash_leaves_nothing_finished(self):
        old = self.built()
        self.approve_all()
        page = self.text(REVIEW)
        his = page.replace("```long:chirp\nDetails matter a lot.", "```long:chirp\nDetails matter.")
        self.put(REVIEW, his)  # his edit, then the packager's new words on another row
        self.copy["vertical"]["clip"] = "Subject works. #gadget"
        self.sync()
        real = inputs.write

        def crash(video, p, text):
            if p == REVIEW:
                raise OSError("power lost")
            return real(video, p, text)
        with mock.patch.object(api, "write", crash), self.assertRaises(OSError):
            api.build(self.v)
        self.assertNotEqual(self.get(RELEASE)["key"], old["key"])
        self.assertEqual(self.text(REVIEW), his)  # his file, naming the older key whose render is kept
        self.assertTrue((self.v.folder / f"records/prev/review-{old['key'][7:]}.md").exists())
        self.assertEqual((api.check(self.v)["state"], self.ledger()), ("stale", {}))
        with self.assertRaisesRegex(Exception, "did not finish"):
            api.decide(self.v, {"long:chirp": "approved"}, "owner")
        rec = self.built()
        edits = self.get("records/release_edits.json")["edits"]
        self.assertEqual([(e["row"], e["by"], e["key"]) for e in edits], [("long:chirp", "owner", old["key"])])
        self.assertEqual({r: (self.rows()[r]["words"], self.rows()[r]["by"]) for r in ("long:chirp", "vertical:clip")},
                         {"long:chirp": ("Details matter.\nTopic: Gadgets", "owner"),
                          "vertical:clip": ("Subject works. #gadget", "packager")})  # no packager words taken as his
        self.assertEqual(self.text(REVIEW).splitlines()[0], f"release {rec['key']}")
        self.assertEqual(set(api.check(self.v)["detail"]["waiting"]), {"long:chirp", "vertical:clip"})
        before, self.v.scored = stamps(self.v.folder), []
        self.assertEqual(self.built(), rec)  # the rerun completes once: nothing measured or written again
        self.assertEqual((stamps(self.v.folder), self.v.scored), (before, []))
        self.assertEqual(self.get("records/release_edits.json")["edits"], edits)

    def test_changes_go_to_the_maker(self):
        self.built()
        api.decide(self.v, {**{rid: "approved" for rid in self.rows()},
                            "vertical:clip": {"changes": "shorter, please", "maker": "packager"}}, "owner")
        d = self.get(DECISION)["rows"]["vertical:clip"]
        self.assertEqual((d["decision"], d["note"], d["maker"]), ("changes", "shorter, please", "packager"))
        result = api.check(self.v)
        self.assertEqual((result["ok"], result["state"], result["next_action"]), (False, "changes", "maker_revises"))
        self.assertEqual(result["detail"]["changes"], [{"row": "vertical:clip", "maker": "packager", "note": "shorter, please"}])
        self.assertEqual(set(self.ledger()), set(self.rows()) - {"vertical:clip"})  # the approved rows still go
        self.copy["vertical"]["clip"] = "Subject works. #gadget"  # the maker's new words: a new decision is asked
        self.sync()
        self.built()
        result = api.check(self.v)
        self.assertEqual((result["state"], result["detail"]["waiting"]), ("waiting_owner", ["vertical:clip"]))
        self.assertEqual(set(self.ledger()), set(self.rows()) - {"vertical:clip"})


class Boundaries(Fixture):
    """guard: never let a non-finite number, an aliased member, an unknown key or a stale decision through."""

    def test_a_non_finite_setting_or_cue_is_unavailable(self):
        self.v.profile["chapters"]["near_s"] = float("nan")
        rec = api.build(self.v)
        self.assertEqual(rec["state"], "unavailable")
        self.assertIn("release.settings", rec["detail"])
        self.v.profile["chapters"]["near_s"] = 1.0
        (self.v.folder / "records/captions/long.json").write_text('{"cues": [{"start_s": NaN}]}')
        rec = api.build(self.v)
        self.assertEqual(rec["state"], "unavailable")
        self.assertIn("records/captions/long.json", rec["detail"])
        self.assertEqual(self.get(ENVELOPE)["state"], "unavailable")

    def test_an_aliased_or_linked_member_is_refused(self):
        os.remove(self.v.folder / "Shorts/alpha.mp4")
        (self.v.folder / "elsewhere.mp4").write_bytes(b"short video")
        os.symlink(self.v.folder / "elsewhere.mp4", self.v.folder / "Shorts/alpha.mp4")
        rec = api.build(self.v)
        self.assertEqual(rec["state"], "unavailable")
        self.assertIn("symlink", rec["detail"])
        os.remove(self.v.folder / "Shorts/alpha.mp4")
        self.put("Shorts/alpha.mp4", b"short video")
        self.put("records/final_manifest.json", {"members": [
            {"path": "Shorts/./alpha.mp4" if p == "Shorts/alpha.mp4" else p, "sha256": digest(b)} for p, b in self.members.items()]})
        rec = api.build(self.v)
        self.assertEqual(rec["state"], "unavailable")
        self.assertIn("unsafe path", rec["detail"])

    def test_an_unknown_review_key_refuses(self):
        self.built()
        page = self.text(REVIEW)
        his = "release sha256:" + "1" * 64 + page[page.index("\n"):]
        self.put(REVIEW, his)
        self.refused("a key with no render", "owner")
        self.assertEqual(self.text(REVIEW), his)

    def test_a_first_edit_stales_the_envelope_for_qc(self):
        self.built()
        self.assertTrue((self.v.folder / "records/release_edits.json").is_file())  # a concrete record, never an absence
        self.assertEqual(self.get("records/release_edits.json"), {"edits": []})
        self.assertEqual(self.qc()["result"], "pass")
        api.edit(self.v, "long:chirp", "Details matter.\nTopic: Gadgets", "owner")
        self.assertEqual(self.qc()["result"], "unavailable")  # QC's evaluator sees the first edit as stale
        self.built()
        self.assertEqual(self.qc()["result"], "pass")

    def test_a_changed_stage_list_stales_the_release(self):
        self.built()
        self.approve_all()
        self.v.stages["stages"] = [s for s in self.v.stages["stages"] if s["id"] != "titles"]
        self.assertEqual((api.check(self.v)["state"], self.ledger()), ("stale", {}))
        with self.assertRaisesRegex(Exception, "stale"):
            api.decide(self.v, {"long:chirp": "approved"}, "owner")
        rec = self.built()
        self.assertFalse(any(r["check"] == "release.titles" for r in rec["readings"]))
        self.assertNotIn("ab_titles", rec["outputs"][0])

    def test_a_dated_reading_stales_when_its_window_ends(self):
        rec = self.built()  # on 2026-01-01; the research of 2025-12-30 passes through titles.research_days (7)
        self.approve_all()
        self.assertEqual(rec["day"], "2026-01-01")
        self.v.day, self.v.scored = "2026-01-06", []  # the window's last day: still current, nothing measured again
        self.assertTrue(api.check(self.v)["ok"])
        self.assertEqual((self.built(), self.v.scored), (rec, []))
        self.v.day = "2026-01-07"  # one day past it
        result = api.check(self.v)
        self.assertEqual((result["state"], self.ledger()), ("stale", {}))
        self.assertIn("crossed its window since 2026-01-01", result["detail"])
        with self.assertRaisesRegex(Exception, "stale"):
            api.decide(self.v, {"long:chirp": "approved"}, "owner")
        bad = self.refused("titles.research_days")["detail"]
        self.assertEqual({(r["setting"], r["value"]["window"]["until"]) for r in bad}, {("titles.research_days", "2026-01-06")})
        self.assertEqual((self.get(RELEASE)["day"], self.qc()["result"]), ("2026-01-07", "fail"))

    def test_a_decision_on_a_changed_input_cannot_send(self):
        self.built()
        self.approve_all()
        self.publish["description_body"] = "Details matter."
        self.sync()
        self.assertEqual(api.check(self.v)["state"], "stale")
        with self.assertRaisesRegex(Exception, "stale"):
            api.decide(self.v, {"long:youtube": "approved"}, "owner")
        self.built()
        self.assertEqual(api.check(self.v)["detail"]["waiting"], ["long:youtube"])
        self.assertNotIn("long:youtube", self.ledger())


if __name__ == "__main__":
    unittest.main()
