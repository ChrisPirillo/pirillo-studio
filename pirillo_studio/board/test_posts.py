"""Posts' reader portion of docs/board.md section 5; writer dispatch and UI remain later seams."""
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from . import model, posts

NOW = dt.datetime(2030, 1, 1, 12, tzinfo=dt.timezone.utc)


def descriptor(kind="text post"):
    return dict(kind=kind, platforms=["social"], status="approval", words="A post",
                file=None, scheduled_at="2030-01-02T12:00:00Z", error="", blocker="",
                filed_by="editor", related=[])


class Adapter:
    def records(self, source):
        return source.items()

    def describe(self, key, record):
        return record


class Posts(unittest.TestCase):
    """e2e: tasks.json, normalized source through reader to the Board's serialized task output."""

    def setUp(self):
        self.adapter = Adapter()
        self.source = {"posts:sample": descriptor()}

    def output(self, source=None):
        tasks = posts.read(self.adapter, self.source if source is None else source, NOW)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            path.write_text(json.dumps(tasks, allow_nan=False))
            return json.loads(path.read_text())

    def test_open_source_coverage_once(self):
        source = {"posts:" + kind: descriptor(kind) for kind in posts.KINDS}
        tasks = self.output(source)
        self.assertEqual(len(tasks), 11)
        self.assertEqual(model.coverage(list(source), tasks), [])
        report = posts.coverage(self.adapter, source, NOW)
        self.assertTrue(report["ok"], report)
        class Duplicate(Adapter):
            def records(self, source):
                return list(source.items()) * 2
        report = posts.coverage(Duplicate(), self.source, NOW)
        self.assertFalse(report["ok"])
        self.assertIn("posts:sample: open source has 2 tasks", report["detail"]["problems"])

    def test_inline_judge_and_model_links(self):
        for kind, judge in (("long post", "video"), ("audio post", "file"), ("image post", "image"),
                            ("text post", "words")):
            row = descriptor(kind)
            row["file"] = "media/asset"
            [task] = self.output({"posts:sample": row})
            self.assertEqual(task["judge"]["type"], judge)
            self.assertTrue(task["judge"]["href"].startswith("/board/content/"))
            self.assertEqual(model.problems(task), [])

    def test_image_approval_belongs_to_post(self):
        row = self.source["posts:sample"]
        row.update(status=["approval", "hand", "blocked", "failed"], blocker="Sign in", error="Send failed")
        [task] = self.output()
        self.assertEqual(set(task["facets"]), {"approval", "hand", "stuck", "failed"})
        self.assertEqual(task["source"]["record"], "posts:sample")
        self.assertIn("image-redo", [a["verb"] for a in task["actions"]])
        self.assertIn("Sign in", task["why"])
        self.assertIn("Send failed", task["why"])
        row["kind"] = "approval"
        with self.assertRaisesRegex(ValueError, "belong in facets"):
            self.output()

    def test_primary_action_and_cant_delivery(self):
        [task] = self.output()
        self.assertEqual(task["actions"][0]["verb"], "approve")
        self.assertEqual(task["cant"], {"words": "", "goes_to": "editor"})
        # This seam supplies the recipient; delivery belongs to the action API.
        del self.source["posts:sample"]["filed_by"]
        with self.assertRaisesRegex(ValueError, "filed_by"):
            self.output()

    def test_undo_from_card_and_day_history(self):
        [task] = self.output()
        for action in task["actions"]:
            self.assertTrue(action["writer"])
            self.assertTrue(action["undo"]["writer"] or action["undo"]["unmet"])
            self.assertNotEqual(action["verb"], action["undo"]["verb"])
        self.source["posts:sample"]["status"] = "hand"
        [task] = self.output()
        posted = next(a for a in task["actions"] if a["verb"] == "posted")
        self.assertIn("do not delete published posts", posted["undo"]["why"])

    def test_refresh_on_load_and_focus(self):
        original = copy.deepcopy(self.source)
        [first] = self.output()
        self.assertEqual(first, self.output()[0])
        self.assertEqual(self.source, original)
        self.source["posts:sample"]["content_identity"] = "content-rev2"
        [changed] = self.output()
        self.assertNotEqual(first["id"], changed["id"])
        self.assertEqual(first["source"], changed["source"])
        changed["judge"]["href"] = "/different"
        self.assertEqual(self.source, dict(original, **{"posts:sample": dict(original["posts:sample"],
                                                                           content_identity="content-rev2")}))

    def test_hand_deadline_closes_without_a_writer_run(self):
        row = self.source["posts:sample"]
        row["status"] = "hand"
        row["scheduled_at"] = NOW.isoformat()
        [task] = self.output()
        self.assertEqual((task["loop"], task["actions"], task["timed_out"]), ("done", [], "missed"))
        self.assertEqual(row["status"], "hand")
        row["status"] = "scheduled"
        self.assertEqual(self.output()[0]["loop"], "acted")

    def test_search_answered_done_and_words_history(self):
        row = self.source["posts:sample"]
        row.update(status="done", history=[{"verb": "posted", "at": NOW.isoformat(),
                                           "receipt": "local-mark", "words": "Already handled"}])
        [task] = self.output()
        self.assertEqual(task["loop"], "done")
        self.assertEqual(task["actions"], [])
        self.assertEqual(task["history"], row["history"])
        self.assertEqual(task["source"]["record"], "posts:sample")


class Refusals(unittest.TestCase):
    """guard: incomplete or malformed source input must not become a partial successful Board read."""

    def test_missing_fields_named_and_no_partial_read(self):
        for field in posts.FIELDS:
            row = descriptor()
            del row[field]
            source = {"posts:bad": row, "posts:good": descriptor()}
            report = posts.coverage(Adapter(), source, NOW)
            self.assertFalse(report["ok"], field)
            self.assertIn(field, " ".join(report["detail"]["problems"]))
            self.assertIn("posts:bad: open source has no task", report["detail"]["problems"])
            with self.assertRaisesRegex(ValueError, "posts:bad"):
                posts.read(Adapter(), source, NOW)

    def test_nonfinite_and_missing_identity_refuse(self):
        row = descriptor()
        row["raw_value"] = float("nan")
        with self.assertRaisesRegex(ValueError, "posts:bad"):
            posts.read(Adapter(), {"posts:bad": row}, NOW)
        with self.assertRaisesRegex(ValueError, "source key must be text"):
            posts.read(Adapter(), {"": descriptor()}, NOW)

    def test_paths_stay_with_host_and_model_urls_are_served(self):
        for path in ("file:///source/link", "../link", "//elsewhere.invalid/link"):
            row = descriptor()
            row["file"] = path
            [task] = self.output_for_path(row)
            self.assertNotIn(path, json.dumps(task))
            self.assertEqual(model.problems(task), [])

    def output_for_path(self, row):
        return posts.read(Adapter(), {"posts:sample": row}, NOW)

    def test_adapter_failure_never_returns_success(self):
        class Broken(Adapter):
            def records(self, source):
                yield from super().records(source)
                raise OSError("snapshot interrupted")
        with self.assertRaisesRegex(OSError, "snapshot interrupted"):
            posts.read(Broken(), {"posts:sample": descriptor()}, NOW)

class RawFacts(unittest.TestCase):
    """defect: cb63a48b2ff942089e70381bc98f090d, raw posts were all refused."""

    def test_model_owns_contract(self):
        class RawAdapter:
            def records(self, source):
                return source.items()

            def describe(self, key, row):
                return row

        row = dict(kind="text post", platforms=["social"], status="hand", words="A post",
                   file=None, scheduled_at=NOW.isoformat(), error="", blocker="",
                   filed_by="editor", related=[])
        try:
            task = posts.read(RawAdapter(), {"post:a/b": row}, NOW)[0]
        except ValueError as exc:
            self.fail(f"raw facts must produce a post task: {exc}")
        self.assertEqual((task["loop"], task["timed_out"]), ("done", "missed"))
        self.assertTrue(task["judge"]["href"].startswith("/board/content/post%3Aa%2Fb/"))
        row["scheduled_at"] = None
        task = posts.read(RawAdapter(), {"post:a/b": row}, NOW)[0]
        self.assertNotIn("deadline", task)
        self.assertIn("hand", task["why"])
        self.assertEqual(task["actions"][0]["writer"], "mark_posted")
        self.assertEqual(task["actions"][0]["undo"]["writer"], "unmark_posted")
        row["words"] = "Changed post"
        changed = posts.read(RawAdapter(), {"post:a/b": row}, NOW)[0]
        self.assertNotEqual(task["id"], changed["id"])
        self.assertNotEqual(task["judge"]["href"], changed["judge"]["href"])


class ReviewCorrections(unittest.TestCase):
    """defect: 10c62b49baf84ac69e898c2145fb5660, deadlines hid failures and inverse writers were false."""

    def test_past_due_failure_and_block_keep_controls(self):
        for status in (["failed"], ["stuck"], ["posted", "failed"]):
            with self.subTest(status=status):
                row = descriptor()
                row.update(status=status, error="Send failed", blocker="Sign in", scheduled_at=NOW.isoformat())
                task = posts.read(Adapter(), {"post": row}, NOW)[0]
                self.assertEqual(task["loop"], "shown", "past-due failure must keep its controls")
                self.assertNotIn("deadline", task)
                self.assertTrue(task["actions"])

    def test_unapproved_past_due_is_held(self):
        row = descriptor()
        row["scheduled_at"] = NOW.isoformat()
        task = posts.read(Adapter(), {"post": row}, NOW)[0]
        self.assertEqual(task["loop"], "shown")
        self.assertNotIn("deadline", task)
        self.assertIn("approve", [a["verb"] for a in task["actions"]])

    def test_failure_reasons_are_required(self):
        for status, field in (("stuck", "blocker"), ("failed", "error")):
            row = descriptor()
            row["status"] = status
            with self.assertRaisesRegex(ValueError, field + " is missing"):
                posts.read(Adapter(), {"post": row}, NOW)

    def test_real_inverse_writers_and_unmet_requirements(self):
        for kind in posts.KINDS:
            with self.subTest(kind=kind):
                row = descriptor(kind)
                row.update(status=["approval", "hand", "stuck"], blocker="Sign in")
                task = posts.read(Adapter(), {"post": row}, NOW)[0]
                actions = {a["verb"]: a for a in task["actions"]}
                approval = ("story_ok" if kind in {"story post", "tip post", "vault post", "digest"}
                            else None if kind == "poll" else "ok_post")
                expected = {"approve": approval, "skip": "pass_from_board", "posted": "unmark_posted",
                            "save-words": "edit_post", "signed-in": None}
                if posts.KINDS[kind][1]:
                    expected["image-redo"] = None
                self.assertEqual(set(actions), set(expected))
                for verb, writer in expected.items():
                    undo = actions[verb]["undo"]
                    self.assertEqual(undo["writer"], writer, verb)
                    if writer is None:
                        self.assertTrue(undo["unmet"])
                    elif verb in ("approve", "skip"):
                        self.assertEqual(undo["args"], {"undo": True})
                self.assertEqual(actions["signed-in"]["writer"], "helper.signins")
