"""Task-source reader contracts; serving and writer execution belong to the API seam."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from . import model, tasks

NOW = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)


def card(**over):
    actions = [dict(verb=verb, label=label, writer=writer, args={arg: "card:one", "title": "Read the report"},
                    undo=dict(verb="undo-" + verb, writer=None, until=None, why="No inverse", unmet="No inverse"))
               for verb, label, writer, arg in (("done", "Done", "board_v5a.complete_card", "id"),
                                               ("reply", "Reply", "board_tasks.board_post", "item"))]
    return dict(dict(kind="task", what="Read the report", why="The next run needs a decision",
                     words="Read the attached report and reply", writer_title="Read the report",
                     filed_by="editor", related=[], actions=actions, evidence=[dict(type="file", label="Report", role="source",
                     target="work/report.txt", identity=dict(sha256="one"))]), **over)


class Adapter:
    def records(self, source):
        return source.items()

    def describe(self, key, record):
        return record


class Tasks(unittest.TestCase):
    """e2e: tasks.json contains source instructions, served evidence and revision-bound writer declarations."""

    def output(self, **over):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "tasks.json"
            output.write_text(json.dumps(tasks.read(Adapter(), {"card:one": card(**over)}, NOW)))
            return json.loads(output.read_text())[0]

    def test_instructions_evidence_and_writers(self):
        task = self.output()
        self.assertEqual(model.coverage(["card:one"], [task]), [])
        self.assertEqual((task["kind"], task["what"], task["why"], task["loop"]),
                         ("task", "Read the report", "The next run needs a decision", "shown"))
        self.assertEqual(task["cant"]["goes_to"], "editor")
        self.assertEqual(task["judge"]["type"], "words")
        self.assertEqual(task["judge"]["parts"], [dict(type="file", label="Report", role="source",
                         href=task["judge"]["href"] + "/evidence/0")])
        actions = task["actions"]
        self.assertEqual([(a["verb"], a["writer"], a["args"]) for a in actions],
                         [("done", "board_v5a.complete_card", dict(id="card:one", title="Read the report")),
                          ("reply", "board_tasks.board_post", dict(item="card:one", title="Read the report"))])
        self.assertTrue(all(a["undo"]["writer"] is None and a["undo"]["unmet"] for a in actions))
        self.assertEqual(self.output(kind="crew task")["kind"], "crew task")
        self.assertEqual(self.output(evidence=[])["judge"].get("parts", []), [])

    def test_source_actions_output(self):
        undo = dict(verb="restore", writer="restore_notice", args=dict(key="notice:one"),
                    until=None, why="Restore the saved notice")
        actions = [dict(verb="dismiss", label="Dismiss notice", writer="clear_notice",
                        args=dict(key="notice:one"), undo=undo)]
        task = self.output(actions=actions)
        self.assertEqual(task["actions"], actions)
        changed = self.output(actions=[dict(actions[0], args=dict(key="notice:two"))])
        self.assertNotEqual(task["id"], changed["id"])
        self.assertEqual(task["judge"], changed["judge"])
        actions[0]["undo"] = dict(verb="restore", writer=None, until=None,
                                   why="No restore writer", unmet="No restore writer")
        self.assertEqual(self.output(actions=actions)["actions"], actions)

    def test_content_revision_and_source_receipts(self):
        before = self.output()
        for change in (dict(words="Read the revised report"), dict(what="Another instruction"),
                       dict(why="Another reason"), dict(evidence=[dict(type="file", label="Report", role="source",
                            target="work/report.txt", identity=dict(sha256="two"))])):
            with self.subTest(change=change):
                after = self.output(**change)
                self.assertNotEqual(before["id"], after["id"])
                self.assertNotEqual(before["judge"]["href"], after["judge"]["href"])
        changed = self.output(history=dict(replies=["A recorded reply"]))
        self.assertNotEqual(before["id"], changed["id"])
        self.assertEqual(before["judge"]["href"], changed["judge"]["href"])
        self.assertEqual(changed["history"], dict(replies=["A recorded reply"]))
        self.assertNotEqual(before["id"], self.output(writer_title="Native title changed")["id"])


class Refusals(unittest.TestCase):
    """guard: incomplete task inventories and non-finite evidence cannot produce clickable partial output."""

    def test_invalid_records_are_named_and_read_refuses(self):
        for change in (dict(what=""), dict(words=None), dict(kind="video"),
                       dict(related=[None]), dict(evidence=[{}]), dict(evidence=[dict(type="file", role="source",
                       label="Report", target="work/report.txt", identity=dict(size=float("nan")))])):
            with self.subTest(change=change):
                source = {"card:bad": card(**change), "card:good": card()}
                report = tasks.coverage(Adapter(), source, NOW)
                self.assertFalse(report["ok"])
                self.assertEqual(report["detail"]["tasks"], 1)
                self.assertIn("card:bad: open source has no task", report["detail"]["problems"])
                with self.assertRaisesRegex(ValueError, "card:bad"):
                    tasks.read(Adapter(), source, NOW)
        missing = card()
        del missing["actions"]
        with self.assertRaisesRegex(ValueError, "card:missing: 'actions'"):
            tasks.read(Adapter(), {"card:missing": missing}, NOW)
        with self.assertRaisesRegex(ValueError, "source key"):
            tasks.task(Adapter(), "", card())

    def test_source_actions_refuse_missing_writers(self):
        action = dict(verb="dismiss", label="Dismiss", writer="clear_notice", args={},
                      undo=dict(verb="restore", writer=None, until=None, why="No inverse", unmet="No inverse"))
        for actions in (None, [], [dict(action, writer="")], [dict(action, args=None)],
                        [dict(action, undo=dict(action["undo"], unmet=""))], [action, action],
                        [dict(action, undo=dict(action["undo"], writer="restore_notice", args=None))]):
            with self.subTest(actions=actions):
                with self.assertRaisesRegex(ValueError, "card:one"):
                    tasks.read(Adapter(), {"card:one": card(actions=actions)}, NOW)

    def test_duplicate_sources_refuse(self):
        class Duplicate(Adapter):
            def records(self, source):
                return list(source.items()) * 2
        report = tasks.coverage(Duplicate(), {"card:one": card()}, NOW)
        self.assertIn("card:one: open source has 2 tasks", report["detail"]["problems"])
        with self.assertRaisesRegex(ValueError, "2 tasks"):
            tasks.read(Duplicate(), {"card:one": card()}, NOW)


if __name__ == "__main__":
    unittest.main()
