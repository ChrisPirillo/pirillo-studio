import contextlib
import datetime as dt
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from pirillo_studio import facebook_confirm


class Confirmation(unittest.TestCase):
    """guard: never leak a page token or mark a post live without a Graph confirmation."""

    def setUp(self):
        self.asset = dict(id="fixture", platform="facebook", state="check", post_id="video",
                          facebook_confirm=True, facebook_phase="finished")
        self.ledger = {"asset": self.asset}
        self.secret = "fixture-page-secret"
        self.studio = SimpleNamespace(
            FB_API="https://graph.example.test", AdapterError=RuntimeError,
            Lock=contextlib.nullcontext,
            Sending=lambda aid: SimpleNamespace(acquire=lambda: True, release=Mock()),
            load=lambda: self.ledger, row=lambda led, aid: (None, led["asset"]),
            assets=lambda led: [(None, led["asset"])], save=Mock(), log=Mock(),
            now=lambda: dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            iso=lambda value: value.isoformat(), key=lambda platform: self.secret,
            facebook_posts=SimpleNamespace(
                facebook_key=lambda s, cfg, token: dict(page_id="page", page_token=token),
                facebook_page_row=lambda s, asset: asset["platform"] == "facebook",
                facebook_page_url=lambda s, cfg, key: "https://example.test/page",
                facebook_pending=Mock()), _form=Mock(),
        )

    def test_graph_failures_never_leak_token_or_mark_live(self):
        self.studio._form.side_effect = RuntimeError(self.secret)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            facebook_confirm.tick_confirmations(self.studio, {})
        self.assertEqual(self.asset["state"], "check")
        self.assertEqual(self.asset["facebook_confirm_tries"], 1)
        self.assertNotIn(self.secret, output.getvalue() + repr(self.studio.log.call_args_list)
                         + repr(self.ledger))

    def test_only_graph_publication_marks_live(self):
        for status in ({"video_status": "processing"}, {"publishing_phase": {"status": "complete"}}):
            with self.subTest(status=status):
                self.studio._form.side_effect = [(200, {"status": status}), (200, {})]
                facebook_confirm.tick_confirmations(self.studio, {})
                self.assertEqual(self.asset["state"],
                                 "posted" if "publishing_phase" in status else "check")
        self.assertNotIn("facebook_confirm", self.asset)
