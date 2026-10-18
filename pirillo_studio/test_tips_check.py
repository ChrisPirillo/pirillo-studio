import contextlib
import datetime as dt
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pirillo_studio import tips_check

T = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)


class TipsXCheck(unittest.TestCase):
    """guard: a tip under its X engagement is reposted once at most, a repost is never reposted, a test never reaches X."""

    def setUp(self):
        self.rows = {"d-t1:x": dict(tip=1, platform="x", method="api_now", state="posted", post_id="p",
                                    posted_at="2026-01-01T11:00:00+00:00", alt_hook="Other hook", text="body")}
        self.ledger = {"storyposts": self.rows}
        self.studio = SimpleNamespace(
            tips_cfg=lambda cfg: dict(enabled=True, x_check_minutes=30, x_engage_min=10),
            now=lambda: T, Lock=contextlib.nullcontext, load=lambda: self.ledger, save=Mock(), log=Mock(),
            parse=dt.datetime.fromisoformat, iso=lambda value: value.isoformat(), tz=lambda cfg: dt.timezone.utc,
            queue_place=Mock(side_effect=lambda cfg, led, rid, plats, t, **kw: (t, {"x": t + dt.timedelta(hours=1)})),
            tip_sync_row=Mock(), x_engagement=Mock(side_effect=AssertionError("reached X")))

    def check(self, likes=2):
        with patch.dict(os.environ, STUDIO_FAKE_COUNTS="1"):
            return tips_check.tips_x_check(self.studio, {}, t=T, reader=lambda x, cfg: likes)

    def reposts(self):
        return [rid for rid, x in self.rows.items() if x.get("repost_of")]

    def test_one_repost_and_never_a_repost_of_a_repost(self):
        self.check()
        self.assertEqual(len(self.reposts()), 1)
        repost = self.rows[self.reposts()[0]]
        repost.update(state="posted", post_id="p2", posted_at="2026-01-01T11:00:00+00:00")
        self.rows["d-t1:x"].pop("x_check")
        self.check()
        self.assertEqual(self.studio.queue_place.call_count, 1)

    def test_test_keys_never_reach_x(self):
        with patch.dict(os.environ, STUDIO_FAKE_KEYS="1"):
            os.environ.pop("STUDIO_FAKE_COUNTS", None)
            self.assertEqual(tips_check.tips_x_check(self.studio, {}, t=T), [])
        self.studio.x_engagement.assert_not_called()

    def test_engaged_tip_is_not_reposted(self):
        self.check(likes=50)
        self.assertEqual(self.reposts(), [])


if __name__ == "__main__":
    unittest.main()
