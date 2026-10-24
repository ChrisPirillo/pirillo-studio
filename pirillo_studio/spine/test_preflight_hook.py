"""guard: never start a maker whose media preflight is not ready, and never accept a media stage without its tools, size
rows and scratch (docs/preflight.md sections 2 and 3; queue item 6 seam 3a)."""
import copy
import tempfile
import unittest

from pirillo_studio.spine import engine as spine
from pirillo_studio.spine.test_engine import Host, new_video, record


class PreflightHook(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root, self.host, self.calls, self.n = tmp.name, Host(), [], 0
        self.recipes = spine.data("recipes.json")

    def video(self, answer, recipes=None):
        def pf(stage, passed, revision):
            self.calls.append((stage, passed, revision))
            return answer
        self.n += 1
        return self.host.video(new_video(self.root, "plain", f"v{self.n}"), preflight=pf, recipes=recipes or self.recipes)

    def refused(self, edit, words, at=4):  # stage 4 is arc, which writes Draft/arc.mp4; 5 is the owner's arc-choice
        recipes = copy.deepcopy(self.recipes)
        edit(recipes["plain"]["stages"][at])
        with self.assertRaisesRegex(spine.SpineError, words):
            self.video({"state": "ready", "key": "k"}, recipes)

    def test_a_media_stage_without_its_extension_refuses(self):
        self.refused(lambda st: st.pop("tools"), "declares no tools, yet reads or writes media")
        self.refused(lambda st: st.pop("size"), "media output Draft/arc.mp4 has no size row")
        for bad in (float("nan"), float("inf"), True, 0, -1, "2"):
            self.refused(lambda st: st["size"]["Draft/arc.mp4"].update(bytes_per_s=bad), "size must map")
        self.refused(lambda st: st["size"].update({"Draft/other.mp4": {"bytes_per_s": 1}}), "size must map")
        self.refused(lambda st: st.pop("scratch_gb"), "scratch_gb None")
        self.refused(lambda st: st.update(scratch_gb=-1), "scratch_gb -1")
        self.refused(lambda st: st.update(tools=[]), "tools must be a non-empty list")
        self.refused(lambda st: st.update(scratch_gb=1), "an owner stage declares no tools", 5)

    def test_a_tooled_recipe_needs_an_adapter(self):
        with self.assertRaisesRegex(spine.SpineError, "preflight: no adapter"):
            self.host.video(new_video(self.root, "plain"), preflight=None)

    def test_not_ready_starts_nothing_and_records_why(self):
        for state, by in (("blocked", "editor"), ("broken", "owner"), ("waiting_owner", "owner")):
            v = self.video({"state": state, "detail": f"{state} detail", "next_action": "do it", "key": None})
            r = spine.run(v, "media")
            self.assertEqual((r["state"], r["by"], r["words"]),
                             ("blocked" if state == "blocked" else "waiting_owner", by, [f"{state} detail", "do it"]))
            self.assertEqual(record(v)["stages"]["media"]["words"], [f"{state} detail", "do it"])
            self.assertEqual([spine.next(v)[k] for k in ("state", "actor", "why")], [r["state"], by, f"{state} detail; do it"])
            self.assertEqual(self.host.started, [])

    def test_ready_starts_once_and_the_attempt_keeps_its_key(self):
        v = self.video({"state": "ready", "detail": "", "next_action": "", "key": "sha256:ok"})
        spine.run(v, "media")
        stage, passed, revision = self.calls[0]
        self.assertEqual((stage["id"], passed["source.mp4"]["by"], revision),
                         ("media", "the job", spine.defn(v, spine.find(v.recipe()[1], "media"))))
        self.assertEqual(len(self.host.started), 1)
        self.assertEqual(record(v)["stages"]["media"]["preflight"], "sha256:ok")
        spine.run(v, "media")  # a running attempt resumes: no second preflight, no second start
        self.assertEqual((len(self.calls), len(self.host.started)), (1, 1))

    def test_the_view_carries_size_rows(self):
        v = self.video({"state": "ready", "key": "k"})
        st = spine.find(v.recipe()[1], "arc")
        spine.ready(v, v.recipe()[1], st)
        self.assertIn({"path": "Draft/arc.mp4", "size": {"bytes_per_s": 2000000}}, self.calls[0][0]["outputs"])
        self.assertEqual(self.calls[0][1]["records/faces.json"]["by"], "media")

    def test_an_unknown_answer_raises(self):
        for answer in ({"state": "ok"}, {"state": "ready", "key": ""}, None):
            v = self.video(answer)
            with self.assertRaisesRegex(spine.SpineError, "preflight answered"):
                spine.run(v, "media")
            self.assertEqual(self.host.started, [])


class PreflightRecovery(unittest.TestCase):
    """defect: Codex review r1 of queue item 6 seam 3 (an unstarted attempt resumed without preflight; a block on a
    started attempt went only to history, so next named the old failure)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.host, self.answer, self.calls = Host(), {"state": "ready", "key": "sha256:ok"}, []

        def pf(stage, passed, revision):
            self.calls.append(stage["id"])
            return self.answer
        self.v = self.host.video(new_video(tmp.name, "plain"), preflight=pf)
        self.off = {"state": "blocked", "detail": "preflight is off", "next_action": "turn it on", "key": None}

    def test_an_unstarted_attempt_asks_again_and_keeps_its_key(self):
        start, self.v.start = self.v.start, lambda *a: (_ for _ in ()).throw(OSError("crash before start"))
        with self.assertRaises(OSError):
            spine.run(self.v, "media")
        op, self.answer, self.v.start = record(self.v)["stages"]["media"]["op"], self.off, start
        self.assertEqual(spine.run(self.v, "media")["state"], "blocked")
        self.assertEqual((self.host.started, spine.next(self.v)["why"]), ([], "preflight is off; turn it on"))
        self.answer = {"state": "ready", "key": "sha256:ok"}
        spine.run(self.v, "media")
        self.assertEqual((self.host.started, len(self.calls)), ([("media", op)], 3))

    def test_an_unstarted_attempt_on_changed_inputs_is_a_new_attempt(self):
        start, self.v.start = self.v.start, lambda *a: (_ for _ in ()).throw(OSError("crash before start"))
        with self.assertRaises(OSError):
            spine.run(self.v, "media")
        op, self.v.start, self.answer = record(self.v)["stages"]["media"]["op"], start, self.off
        (self.v.folder / "source.mp4").write_bytes(b"new footage")
        spine.run(self.v, "media")
        self.answer = {"state": "ready", "key": "sha256:ok"}
        r = spine.run(self.v, "media")
        self.assertEqual((r["attempt"], len(self.host.started)), (2, 1))
        self.assertNotEqual(self.host.started[0][1], op)

    def test_a_block_after_a_failed_attempt_is_what_next_names(self):
        spine.run(self.v, "media")
        rec = spine.load_record(self.v)
        rec["stages"]["media"].update(state="fail", words=["old failure"])
        spine.save(self.v, rec, "media", "test")
        self.answer = self.off
        spine.run(self.v, "media")
        self.assertEqual([spine.next(self.v)[k] for k in ("state", "why")], ["blocked", "preflight is off; turn it on"])
        self.assertEqual(len(self.host.started), 1)


if __name__ == "__main__":
    unittest.main()
