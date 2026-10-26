"""Vault options through the shared reader; dispatch and serving remain later seams."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from . import model, vault

NOW = dt.datetime(2030, 1, 1, 12, tzinfo=dt.timezone.utc)


def option(**over):
    return dict(dict(what="An old experiment", why="Try it again", words="social:\nThe copy",
                     file="work/still.png", content_identity=dict(size=12), state="open", deadline=None,
                     converted_to=None, filed_by="editor", related=[]), **over)


class Adapter:
    def records(self, source):
        return source.items()

    def describe(self, key, record):
        return record


class Vault(unittest.TestCase):
    """e2e: tasks.json contains one option, its revision, content and writer declarations."""

    def output(self, **over):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            path.write_text(json.dumps(vault.read(Adapter(), {"vault:one": option(**over)}, NOW)))
            return json.loads(path.read_text())[0]

    def test_content_and_actions(self):
        task = self.output()
        self.assertEqual(model.coverage(["vault:one"], [task]), [])
        self.assertEqual((task["kind"], task["what"], task["loop"]), ("vault option", "An old experiment", "shown"))
        self.assertEqual(task["cant"]["goes_to"], "editor")
        self.assertEqual(task["judge"]["type"], "words")
        self.assertEqual(task["judge"]["parts"][0]["type"], "image")
        self.assertTrue(task["judge"]["parts"][0]["href"].startswith(task["judge"]["href"]))
        actions = {a["verb"]: a for a in task["actions"]}
        self.assertEqual(list(actions), ["want", "skip", "schedule", "earlier", "later", "save", "move", "remove"])
        self.assertEqual(actions["want"]["args"], dict(ids=["vault:one"]))
        self.assertEqual(actions["want"]["writer"], "ok_from_board")
        for verb in list(actions)[1:]:
            self.assertEqual(actions[verb]["writer"], "weekend_change")
            self.assertEqual(actions[verb]["args"], dict(vid="vault:one", action="remove" if verb == "skip" else verb))
        # None of these writers restores the prior option and all its affected platform/queue rows.
        self.assertTrue(all(a["undo"]["writer"] is None and a["undo"]["unmet"] for a in actions.values()))

    def test_revision_history_and_conversion(self):
        before = self.output()
        for change in (dict(words="New copy"), dict(file="new.png"), dict(content_identity=dict(size=13))):
            after = self.output(**change)
            self.assertNotEqual(before["id"], after["id"])
            self.assertNotEqual(before["judge"]["href"], after["judge"]["href"])
        picked = self.output(state="picked", converted_to="story:one", history=dict(picked_at="earlier"))
        self.assertEqual((picked["loop"], picked["actions"]), ("acted", []))
        self.assertEqual(picked["source"]["related"], ["story:one"])
        self.assertEqual(picked["history"], dict(picked_at="earlier"))
        self.assertEqual(picked["judge"]["href"], before["judge"]["href"])
        self.assertNotEqual(picked["id"], before["id"])
        self.assertEqual(self.output(converted_to="story:one")["actions"], [])
        self.assertEqual(self.output(state="held")["loop"], "cant")
        self.assertEqual(self.output(state="removed")["loop"], "done")
        expired = self.output(deadline="2030-01-01T00:00:00Z")
        self.assertEqual((expired["loop"], expired["actions"], expired["timed_out"]), ("done", [], "expired"))


class Refusals(unittest.TestCase):
    """guard: invalid options cannot return partial coverage or stale clickable tasks."""

    def test_named_invalid_records(self):
        for change in (dict(words=""), dict(what=None), dict(file=4), dict(state="unknown"),
                       dict(converted_to=""), dict(content_identity=dict(size=float("inf"))),
                       dict(deadline="2030-01-01T00:00:00")):
            source = {"vault:bad": option(**change), "vault:good": option()}
            with self.subTest(change=change):
                report = vault.coverage(Adapter(), source, NOW)
                self.assertFalse(report["ok"])
                self.assertEqual(report["detail"]["tasks"], 1)
                self.assertIn("vault:bad", str(report["detail"]["problems"]))
                with self.assertRaisesRegex(ValueError, "vault:bad"):
                    vault.read(Adapter(), source, NOW)
        row = option()
        del row["state"]
        with self.assertRaisesRegex(ValueError, "vault:bad.*state"):
            vault.read(Adapter(), {"vault:bad": row}, NOW)
        with self.assertRaisesRegex(ValueError, "source key"):
            vault.task(Adapter(), "", option())

    def test_duplicate_sources_refuse(self):
        class Duplicate(Adapter):
            def records(self, source):
                return list(source.items()) * 2
        report = vault.coverage(Duplicate(), {"vault:one": option()}, NOW)
        self.assertIn("vault:one: open source has 2 tasks", report["detail"]["problems"])


if __name__ == "__main__":
    unittest.main()
