import contextlib
import copy
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from pirillo_studio import housekeeping


class Housekeeping(unittest.TestCase):
    """guard: never replace a Board with an empty draft or lose a concurrent ask."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.studio = SimpleNamespace(
            ledger={"videos": {"fixture": {}}, "asks": [], "log": []},
            Lock=contextlib.nullcontext,
            Sending=lambda _: SimpleNamespace(acquire=lambda: True, release=Mock()),
            DATA_DIR=self.temp.name,
            BOARD=str(Path(self.temp.name) / "board.html"),
        )
        self.studio.load = lambda: copy.deepcopy(self.studio.ledger)
        self.studio.save = lambda led: setattr(self.studio, "ledger", copy.deepcopy(led))
        self.studio.log = lambda led, msg: led["log"].append(msg)

    def test_short_draft_keeps_existing_board(self):
        target = Path(self.studio.BOARD)
        target.write_bytes(b"existing board")
        for size in (0, 1023):
            with self.subTest(size=size), contextlib.redirect_stderr(io.StringIO()):
                self.studio.board = lambda cfg, led: Path(self.studio.BOARD).write_bytes(b"x" * size)
                housekeeping.tick_board(self.studio, {})
                self.assertEqual(target.read_bytes(), b"existing board")
                self.assertEqual(self.studio.BOARD, str(target))
                self.assertEqual(list(Path(self.temp.name).iterdir()), [target])

    def test_cleanup_keeps_ask_added_during_tidy(self):
        ask = {"id": "concurrent", "state": "open"}

        def tidy(cfg, led):
            self.studio.ledger["asks"].append(ask)
            led["videos"]["fixture"]["tidied"] = True

        self.studio.tidy = tidy
        housekeeping.tick_housekeeping(self.studio, {}, ["fixture renewal"])
        self.assertEqual(self.studio.ledger["asks"], [ask])
        self.assertTrue(self.studio.ledger["videos"]["fixture"]["tidied"])
        self.assertEqual(self.studio.ledger["log"], ["fixture renewal"])


class BoardBehindBusyLedger(unittest.TestCase):
    """defect: 2026-10-17 studio tick 33881 re-rendered the Board for 42 minutes while asks acted on every one to
    three minutes each changed the ledger and published their own Board."""

    def test_a_board_published_during_the_render_ends_the_tick_board(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        target = Path(temp.name) / "board.html"
        studio = SimpleNamespace(ledger={"n": 0}, Lock=contextlib.nullcontext, DATA_DIR=temp.name, BOARD=str(target))
        studio.load = lambda: copy.deepcopy(studio.ledger)
        studio.save = Mock()
        renders = []

        def board(cfg, led):
            renders.append(led["n"])
            if len(renders) > 3:
                raise AssertionError(f"rendered {len(renders)} times behind a ledger that keeps changing")
            Path(studio.BOARD).write_bytes(b"tick draft " * 200)
            studio.ledger["n"] += 1                 # another command acts on an ask during the render
            target.write_bytes(b"the command's own Board")

        studio.board = board
        housekeeping.tick_board(studio, {})
        self.assertEqual(renders, [0])
        self.assertEqual(target.read_bytes(), b"the command's own Board")
        self.assertEqual(sorted(p.name for p in Path(temp.name).iterdir()), ["board.html"])
        studio.save.assert_not_called()
