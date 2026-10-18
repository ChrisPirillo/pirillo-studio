import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pirillo_studio import clock


class TickSteps(unittest.TestCase):
    """defect: 2026-10-17 studio tick 33881 ran 54 minutes and neither its log nor the keeper could name the step."""

    def test_the_step_in_hand_is_on_disk_and_every_step_is_timed(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(clock.time, "time", side_effect=[0, 0, 5, 5, 405, 410]):
            step = clock.Steps(temp.name, 300)
            step("prepare")
            self.assertEqual(json.loads(Path(temp.name, f"tick.step.{os.getpid()}").read_text())["step"], "prepare")
            step("board")
            row = json.loads(Path(temp.name, f"tick.step.{os.getpid()}").read_text())
            step()
        self.assertFalse(Path(temp.name, f"tick.step.{os.getpid()}").exists())
        self.assertEqual((row["step"], row["pid"]), ("board", os.getpid()))
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "tick: board took 400 s, over tick_step_slow_seconds 300")
        self.assertRegex(lines[1], r"^tick \S+: 410 s \(prepare 5, board 400\)$")


if __name__ == "__main__":
    unittest.main()
