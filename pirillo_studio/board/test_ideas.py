"""Ideas through the shared Board reader; dispatch remains the API seam."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from . import ideas, model

NOW = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)


def idea(**over):
    return dict(dict(kind="video", what="A useful experiment", why="Try it together", outlet="",
                     sources=[], saved=False, wanted=False, skipped=False, converted_to=None,
                     filed_by="strategist", related=[]), **over)


class Adapter:
    def records(self, source):
        return source.items()

    def describe(self, key, record):
        return record


def read(**over):
    return ideas.read(Adapter(), {"one": idea(**over)}, NOW)[0]


class Ideas(unittest.TestCase):
    """e2e: tasks.json from video, saved and story records through the shared reader."""

    def test_task_output_and_coverage(self):
        source = {"one": idea(), "bank": idea(saved=True), "want": idea(wanted=True),
                  "skip": idea(skipped=True), "story": idea(kind="story", outlet="Source",
                  sources=[dict(url="https://example.org/story", label="Source")]),
                  "converted": idea(saved=True, converted_to="story:2030-01-01-1")}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tasks.json"
            path.write_text(json.dumps(ideas.read(Adapter(), source, NOW)))
            tasks = json.loads(path.read_text())
        self.assertEqual(model.coverage(list(source), tasks), [])
        self.assertEqual([t["loop"] for t in tasks], ["shown", "shown", "acted", "done", "shown", "acted"])
        self.assertEqual([t["kind"] for t in tasks], ["idea", "banked idea", "idea", "idea", "story idea", "idea"])
        self.assertEqual(tasks[0]["what"], source["one"]["what"])
        self.assertEqual(tasks[0]["why"], source["one"]["why"])
        self.assertIn("Source", tasks[4]["why"])
        self.assertEqual(tasks[4]["judge"]["parts"][0]["role"], "source")
        self.assertTrue(tasks[4]["judge"]["parts"][0]["href"].startswith(tasks[4]["judge"]["href"]))
        self.assertEqual(ideas.coverage(Adapter(), source, NOW)["detail"], dict(sources=6, tasks=6, problems=[]))

    def test_writers_and_true_inverses(self):
        actions = {a["verb"]: a for a in read()["actions"]}
        self.assertEqual([a["label"] for a in actions.values()], ["Want it", "Bank", "Story", "Skip"])
        self.assertTrue(all(a["writer"] == "idea_decide" and a["args"] == dict(key="one", action=v)
                            for v, a in actions.items()))
        self.assertEqual(actions["bank"]["undo"]["args"], dict(key="one", on=False))
        self.assertEqual(actions["bank"]["undo"]["writer"], "star_idea")
        self.assertEqual(actions["story"]["undo"]["writer"], "story_pick")
        self.assertEqual(actions["story"]["undo"]["receipt_field"], "converted_to")
        self.assertTrue(all(actions[v]["undo"]["unmet"] for v in ("want", "skip")))
        self.assertNotIn("story", [a["verb"] for a in read(kind="story")["actions"]])
        unbank = next(a for a in read(saved=True)["actions"] if a["verb"] == "unbank")
        self.assertEqual((unbank["writer"], unbank["args"]), ("star_idea", dict(key="one", on=False)))

    def test_revisions_and_history(self):
        before = read()
        self.assertNotEqual(before["judge"]["href"], read(why="Different take")["judge"]["href"])
        self.assertNotEqual(before["id"], read(history={"saved_at": "earlier"})["id"])
        self.assertEqual(before["judge"]["href"], read(saved=True)["judge"]["href"])
        self.assertEqual(read(saved=True, skipped=True)["loop"], "shown")


class Refusals(unittest.TestCase):
    """guard: malformed or duplicate records cannot yield a deceptively partial Board."""

    def test_named_bad_facts(self):
        for over in (dict(what=""), dict(why=""), dict(saved=1), dict(kind="poll"), dict(outlet=None),
                     dict(sources=[dict(url="", label="Source")]), dict(converted_to=""), dict(history={"n":float("nan")})):
            with self.subTest(over=over), self.assertRaisesRegex(ValueError, "one:"):
                read(**over)
        row = idea()
        del row["wanted"]
        with self.assertRaisesRegex(ValueError, "one: 'wanted'"):
            ideas.read(Adapter(), {"one": row}, NOW)
        with self.assertRaisesRegex(ValueError, "source key"):
            ideas.task(Adapter(), "", idea())

    def test_duplicate_and_unplaced_are_named(self):
        class Duplicate(Adapter):
            def records(self, source):
                return list(source.items()) * 2
        report = ideas.coverage(Duplicate(), {"one": idea()}, NOW)
        self.assertFalse(report["ok"])
        self.assertIn("one: open source has 2 tasks", report["detail"]["problems"])
        report = ideas.coverage(Adapter(), {"one": idea(what="")}, NOW)
        self.assertEqual(report["detail"]["tasks"], 0)
        self.assertIn("one: what must be text", report["detail"]["problems"])


if __name__ == "__main__":
    unittest.main()
