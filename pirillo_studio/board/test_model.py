"""The task model's contract (docs/board.md section 5): its shape, every end of its loop, and source coverage."""
import unittest

from pirillo_studio.board import model


def task(**over):
    t = {"id": "ask:sample:rev1", "kind": "ask", "what": "Approve the proposed words",
         "why": "The post waits for this decision",
         "source": {"record": "asks:sample", "filed_by": "editor", "related": ["post:sample"]},
         "judge": {"type": "words", "href": "/board/content/sample/rev1"},
         "actions": [{"verb": "approve", "label": "Approve",
                      "undo": {"verb": "undo-approve", "until": None, "why": "Until the writer starts sending"}}],
         "cant": {"words": "", "goes_to": "editor"}, "loop": "shown"}
    t.update(over)
    return t


class Shape(unittest.TestCase):
    """contract: docs/board.md 1, the shape slice of tests 1, 2 and 12 (source and writer halves land in seams 6 to 9)."""

    def test_the_spec_example_holds(self):
        self.assertEqual(model.problems(task()), [])
        a = {"verb": "a", "label": "A", "undo": {"verb": "b", "until": "2030-01-01T12:00:00Z", "why": "w"}}
        self.assertEqual(model.problems(task(actions=[a])), [])

    def test_shape_primary_action_and_cant_recipient(self):
        self.assertTrue(model.problems(task(actions=[])))
        self.assertTrue(model.problems(task(cant={"words": ""})))
        self.assertEqual(model.problems(task(actions=[], loop="done")), [])

    def test_an_action_without_a_real_inverse_fails(self):
        for undo in (None, {"verb": "approve", "until": None, "why": "x"}, {"verb": "undo", "until": None},
                     {"verb": "undo", "why": "x"}):
            a = {"verb": "approve", "label": "Approve", "undo": undo}
            self.assertTrue(model.problems(task(actions=[a])), undo)

    def test_shape_judge_links_are_served_here(self):
        self.assertTrue(model.problems(task(judge={"type": "audio", "href": "/x"})))
        for h in ("//elsewhere.invalid/x.png", "/\\elsewhere.invalid/x", "/\t/elsewhere.invalid/x"):
            self.assertTrue(model.problems(task(judge={"type": "image", "href": h})), h)
        self.assertTrue(model.problems(task(judge={"type": "image", "href": "file:///tmp/x.png"})))
        parts = [{"type": "image", "href": "/board/file/a.png", "label": "Still", "role": "content"}]
        self.assertEqual(model.problems(task(judge={"type": "words", "href": "/w", "parts": parts})), [])
        self.assertTrue(model.problems(task(judge={"type": "words", "href": "/w",
                                                   "parts": [dict(parts[0], role="extra")]})))

    def test_shape_steps_link_once(self):
        steps = {"type": "file", "href": "/board/file/steps.md", "label": "Steps", "role": "steps"}
        self.assertEqual(model.problems(task(judge={"type": "words", "href": "/w", "parts": [steps, steps]})),
                         ["ask:sample:rev1: a steps link appears twice"])

    def test_unknown_kind_and_state_fail(self):
        self.assertTrue(model.problems(task(kind="widget")))
        self.assertTrue(model.problems(task(loop="helper_down")))

    def test_a_value_of_the_wrong_type_is_reported_not_raised(self):
        bad = {"type": "file", "href": ["/a"], "label": "S", "role": "steps"}
        for t in (task(kind=["ask"]), task(loop=["shown"]), task(id=7), task(actions="approve"),
                  task(source={"record": ["x"], "filed_by": "e"}), task(source="asks:sample"),
                  task(judge={"type": "words", "href": "/w", "parts": [bad]}),
                  task(judge={"type": "words", "href": "/w", "parts": 3}), task(what=["x"]), task(why={"x": 1}),
                  task(source={"record": "asks:sample", "filed_by": 5}), task(cant={"goes_to": ["e"]}),
                  task(actions=[{"verb": "a", "label": ["A"], "undo": {"verb": "b", "until": None, "why": "w"}}]),
                  task(actions=[{"verb": "a", "label": "A", "undo": {"verb": "b", "until": 5, "why": "w"}}]),
                  task(actions=[{"verb": "a", "label": "A", "undo": {"verb": "b", "until": "soon", "why": "w"}}]),
                  task(actions=[{"verb": "a", "label": "A",
                                 "undo": {"verb": "b", "until": "2030-01-01T12:00:00-07:00", "why": "w"}}]),
                  task(source={"record": "asks:sample", "filed_by": "e", "related": [5, None]})):
            self.assertTrue(model.problems(t), t)
        self.assertTrue(model.coverage(["x"], [task(source={"record": ["x"], "filed_by": "e"}), "junk"]))
        self.assertIn("3: a source key must be text", model.coverage(["asks:sample", 3], [task()]))
        self.assertEqual(model.problems(task(actions=None, loop="done")), ["ask:sample:rev1: actions must be a list"])


class Loop(unittest.TestCase):
    """contract: docs/board.md 2: each end of the loop chart, and no move it lacks (the writer's result is seam 6's)."""

    def test_every_end_is_reached_by_the_chart(self):
        t = model.advance(task(loop="filed"), "shown")
        self.assertEqual(model.advance(model.advance(t, "acted"), "done")["loop"], "done")
        self.assertEqual(model.advance(model.advance(t, "cant"), "shown")["loop"], "shown")
        undone = model.advance(model.advance(model.advance(t, "acted"), "done"), "undone")
        self.assertEqual(model.advance(undone, "shown")["loop"], "shown")

    def test_no_move_the_chart_lacks(self):
        with self.assertRaises(ValueError):
            model.advance(task(), "done")
        with self.assertRaises(ValueError):
            model.advance(task(loop="cant"), "acted")


class Deadline(unittest.TestCase):
    """contract: docs/board.md 2's deadline paragraph: a task is drawn from its effective state at read time."""

    NOW = model.dt.datetime(2026, 10, 25, 12, tzinfo=model.dt.timezone.utc)

    def test_past_its_deadline_a_task_offers_no_answer_and_closes_itself(self):
        late = {"at": "2026-10-25T11:00:00Z", "outcome": "Skipped: the posting window passed", "run": False}
        self.assertEqual(model.problems(task(deadline=late)), [])
        [t] = model.effective([task(deadline=late)], self.NOW)
        self.assertEqual((t["loop"], t["actions"], t["timed_out"]), ("done", [], late["outcome"]))
        [t] = model.effective([task(deadline=dict(late, run=True))], self.NOW)
        self.assertEqual((t["loop"], t["actions"]), ("acted", []))
        self.assertEqual(model.effective([task(deadline=dict(late, at="2026-10-25T13:00:00Z"))], self.NOW),
                         [task(deadline=dict(late, at="2026-10-25T13:00:00Z"))])
        self.assertEqual(model.effective([task(deadline=late, loop="done", actions=[])], self.NOW),
                         [task(deadline=late, loop="done", actions=[])])
        for bad in (dict(late, at=None), dict(late, at="soon"), dict(late, at="2026-10-25T11:00:00+02:00"),
                    dict(late, at="2026-10-25T11:00:00"), dict(late, outcome=""), dict(late, run="no")):
            self.assertTrue(model.problems(task(deadline=bad)), bad)
        for loop in ("filed", "cant", "undone"):
            [t] = model.effective([task(deadline=late, loop=loop)], self.NOW)
            self.assertEqual((t["loop"], t["actions"]), ("done", []), loop)

    def test_a_decision_closes_with_the_task_it_gates(self):
        gate = task(id="ask:g:rev1", source={"record": "asks:g", "filed_by": "editor", "gates": "asks:sample"})
        self.assertEqual(model.problems(gate), [])
        self.assertEqual(model.effective([gate, task()], self.NOW), [gate, task()])
        late = {"at": "2026-10-25T11:00:00Z", "outcome": "Skipped", "run": False}
        g, _ = model.effective([gate, task(deadline=late)], self.NOW)
        self.assertEqual((g["loop"], g["actions"], g["closed_with"]), ("done", [], "asks:sample"))
        self.assertEqual(model.effective([gate], self.NOW)[0]["loop"], "done")
        self.assertTrue(model.problems(dict(gate, source=dict(gate["source"], gates=""))))
        self.assertEqual(model.effective([gate, task(loop="acted", actions=[])], self.NOW)[0]["loop"], "shown")
        outer = task(id="ask:o:rev1", source={"record": "asks:o", "filed_by": "editor", "gates": "asks:g"})
        self.assertEqual([t["loop"] for t in model.effective([outer, gate, task(deadline=late)], self.NOW)],
                         ["done", "done", "done"])


class Coverage(unittest.TestCase):
    """contract: docs/board.md test 4's model half: open sources map once; missing, duplicate and unknown fail."""

    def test_coverage_once_by_identity(self):
        self.assertEqual(model.coverage(["asks:sample"], [task()]), [])
        self.assertEqual(model.coverage(["asks:sample", "posts:p1"], [task()]), ["posts:p1: open source has no task"])
        self.assertEqual(model.coverage(["asks:sample"], [task(), task(id="ask:sample:rev2")]),
                         ["asks:sample: open source has 2 tasks"])
        self.assertEqual(model.coverage([], [task()]), ["asks:sample: task names no open source"])
        self.assertEqual(model.coverage(["asks:sample"], [task(), task()]),
                         ["ask:sample:rev1: task id appears 2 times", "asks:sample: open source has 2 tasks"])

    def test_a_broken_task_fails_coverage(self):
        self.assertEqual(model.coverage(["asks:sample"], [task(why="")]), ["ask:sample:rev1: why must be text"])


if __name__ == "__main__":
    unittest.main()
