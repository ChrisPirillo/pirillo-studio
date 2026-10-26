"""Asks' reader portion of docs/board.md section 5; writer dispatch and UI remain later seams."""
import datetime as dt
import unittest

from . import ask_tasks, model

NOW = dt.datetime(2030, 1, 1, 12, tzinfo=dt.timezone.utc)


def ask(**over):
    row = dict(what="Pick one", about="Thumbnail", options=[dict(label="A"), dict(label="B")], docs=[], files=[],
               steps=None, state="open", todo=False, acted=False, deadline=None, default_on_timeout=None, filed_by="packager",
               blocks="the upload", related=[], recommendation=dict(label="A", why="brighter"))
    row.update(over)
    return row


class Adapter:
    def records(self, source):
        return source.items()

    def describe(self, key, record):
        return record


def read(**over):
    return ask_tasks.read(Adapter(), {"ask:1": ask(**over)}, NOW)[0]


class Asks(unittest.TestCase):
    """e2e: tasks.json, each ask state read through to the Board's task output."""

    def test_open_source_coverage_once(self):
        source = {"ask:open": ask(), "ask:todo": ask(options=[], todo=True), "ask:done": ask(state="answered", acted=True),
                  "ask:gone": ask(state="withdrawn")}
        tasks = ask_tasks.read(Adapter(), source, NOW)
        self.assertEqual(model.coverage(list(source), tasks), [])
        self.assertEqual([t["loop"] for t in tasks], ["shown", "shown", "done", "done"])
        class Duplicate(Adapter):
            def records(self, source):
                return list(source.items()) * 2
        report = ask_tasks.coverage(Duplicate(), {"ask:1": ask()}, NOW)
        self.assertIn("ask:1: open source has 2 tasks", report["detail"]["problems"])

    def test_primary_action_and_cant_delivery(self):
        t = read()
        self.assertEqual(t["what"], "Thumbnail: Pick one")
        self.assertEqual([(a["label"], a["args"]) for a in t["actions"]], [("A", {"choice": "A"}), ("B", {"choice": "B"})])
        self.assertIn("Recommended: A (brighter).", t["why"])
        self.assertEqual(t["cant"]["goes_to"], "packager")
        self.assertEqual([a["verb"] for a in read(options=[], todo=True, docs=["w/a.md"])["actions"]], ["done"])
        self.assertEqual([a["verb"] for a in read(options=[])["actions"]], ["answer"])

    def test_answered_waits_on_its_act(self):
        self.assertEqual(read(state="answered")["loop"], "acted")
        self.assertEqual(read(state="answered")["actions"], [])

    def test_todo_primary_and_named_links(self):
        t = read(options=[], todo=True, docs=["w/steps.md", "w/notes.md"], steps="w/steps.md", files=["a.png", "b.mp4", "c.zip"])
        parts = t["judge"]["parts"]
        self.assertEqual([(p["label"], p["role"]) for p in parts],
                         [("b.mp4", "content"), ("c.zip", "content"), ("notes.md", "source"), ("Steps", "steps")])
        self.assertEqual([p["type"] for p in parts[:2]], ["video", "file"])
        self.assertTrue(all(p["href"].startswith(t["judge"]["href"]) for p in parts))
        self.assertEqual(t["judge"]["type"], "image")

    def test_undo_is_declared_unmet(self):
        self.assertTrue(all(a["undo"]["writer"] is None and a["undo"]["unmet"] for a in read()["actions"]))

    def test_decision_deadline_answers_its_default(self):
        t = ask_tasks.read(Adapter(), {"ask:1": ask(deadline="2029-12-31T12:00:00Z", default_on_timeout="Hold")}, NOW)[0]
        self.assertEqual((t["loop"], t["timed_out"], t["actions"]), ("acted", "Hold", []))
        self.assertNotIn("deadline", read())

    def test_changed_words_or_files_change_the_judge_href(self):
        self.assertNotEqual(read()["judge"]["href"], read(what="Pick another")["judge"]["href"])
        self.assertNotEqual(read(content_identity=[1])["judge"]["href"], read(content_identity=[2])["judge"]["href"])


class Refusals(unittest.TestCase):
    """guard: a malformed ask never becomes a partial Board; each refusal names the source and field."""

    def test_named_refusals(self):
        for over, words in ((dict(what=""), "what must be text"), (dict(state="late"), "state 'late'"),
                            (dict(options=[dict()]), "options must each have a label"),
                            (dict(docs=[""]), "docs must be a list"), (dict(acted=None), "acted must be true or false"),
                            (dict(deadline="2030-01-02T00:00:00Z"), "default_on_timeout")):
            with self.subTest(over=over), self.assertRaisesRegex(ValueError, "ask:1: .*" + words):
                read(**over)
        row = ask()
        del row["blocks"]
        with self.assertRaisesRegex(ValueError, "ask:1: 'blocks'"):
            ask_tasks.read(Adapter(), {"ask:1": row}, NOW)


if __name__ == "__main__":
    unittest.main()
