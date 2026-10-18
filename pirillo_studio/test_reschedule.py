import copy
import datetime as dt
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pirillo_studio import drop_times, reschedule


NOW = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)
OLD = "2026-01-01T13:00:00+00:00"
NEW = "2026-01-01T14:00:00+00:00"


class RescheduleGuard(unittest.TestCase):
    """guard: failed changes preserve the original schedule; done never records an unconfirmed new time."""

    def setUp(self):
        self.asset = dict(id="fixture", slug="fixture", kind="short", platform="mastodon", state="scheduled",
                          scheduled_for=OLD, moved=[], claimed_at=NOW.isoformat(), replace=True,
                          reschedule=dict(to=NEW))
        self.runtime = SimpleNamespace(
            sys=sys, drop_times=drop_times,
            dt=dt, parse=dt.datetime.fromisoformat, now=lambda: NOW, iso=lambda value: value.isoformat(),
            MASTODON_SOON=dt.timedelta(minutes=5), move=Mock(side_effect=self.move),
            late_still=lambda asset: {}, log=Mock(), _at=lambda value, cfg: value, plat_name=str)

    def move(self, asset, state, actor):
        asset["state"] = state

    def test_failure_records_refusal_without_moving_or_losing_pending_words(self):
        for state in ("scheduled", "claimed"):
            with self.subTest(state=state):
                asset = copy.deepcopy(dict(self.asset, state=state))
                reschedule.reschedule_fail(self.runtime, {}, {}, asset, "  platform\n refused  ")
                self.assertEqual((asset["state"], asset["scheduled_for"], asset["moved"]), ("scheduled", OLD, []))
                self.assertEqual(asset["stays"], dict(why="platform refused", wanted=NEW, at=NOW.isoformat()))
                self.assertNotIn("reschedule", asset)
                self.assertNotIn("claimed_at", asset)
                self.assertTrue(asset["replace"])

    def test_done_without_new_time_refuses_without_mutation(self):
        for pending in ({}, dict(claimed_to=None, to=None)):
            with self.subTest(pending=pending):
                asset = copy.deepcopy(dict(self.asset, reschedule=pending))
                original = copy.deepcopy(asset)
                with self.assertRaises(SystemExit):
                    reschedule.reschedule_done(self.runtime, {}, {}, asset, None, {})
                self.assertEqual(asset, original)
                self.runtime.move.assert_not_called()

    def test_manual_target_counts_as_confirmation(self):
        reschedule.reschedule_done(self.runtime, {}, {}, self.asset, None, {})
        self.assertEqual((self.asset["state"], self.asset["scheduled_for"], self.asset["moved"]),
                         ("scheduled", NEW, [OLD]))
        self.assertNotIn("reschedule", self.asset)
        self.assertNotIn("claimed_at", self.asset)
        self.assertTrue(self.asset["replace"])

    def test_done_records_claimed_time_and_retries_changed_target(self):
        confirmed = "2026-01-01T14:03:00+00:00"
        self.asset["reschedule"].update(claimed_to=confirmed, words=True)
        reschedule.reschedule_done(self.runtime, {}, {}, self.asset, None, {})
        self.assertEqual((self.asset["scheduled_for"], self.asset["moved"]), (confirmed, [OLD]))
        self.assertIn("reschedule", self.asset)
        self.assertEqual(self.asset["reschedule"]["to"], NEW)
        self.assertEqual(self.asset["reschedule"]["asked"], NOW.isoformat())
        self.assertNotIn("claimed_to", self.asset["reschedule"])
        self.assertNotIn("replace", self.asset)

    def test_equivalent_claimed_time_needs_no_retry(self):
        self.asset["reschedule"]["claimed_to"] = "2026-01-01T09:00:00-05:00"
        reschedule.reschedule_done(self.runtime, {}, {}, self.asset, None, {})
        self.assertEqual(self.asset["scheduled_for"], "2026-01-01T09:00:00-05:00")
        self.assertNotIn("reschedule", self.asset)

    def test_found_live_keeps_old_time_without_a_new_schedule(self):
        self.asset["reschedule"] = {}
        reschedule.reschedule_done(self.runtime, {}, {}, self.asset, "posted", {})
        self.assertEqual((self.asset["state"], self.asset["scheduled_for"], self.asset["moved"]),
                         ("posted", OLD, []))
        self.assertEqual(self.asset["posted_at"], NOW.isoformat())
        self.assertNotIn("reschedule", self.asset)

    def test_due_requires_native_permission_target_and_time_outside_floor(self):
        with patch.object(drop_times, "api_moves", return_value=True) as allowed:
            self.assertTrue(reschedule.reschedule_due(self.runtime, {}, self.asset, NOW))
            allowed.assert_called_once_with(self.runtime, {}, self.asset)
            for changes in (dict(state="posted"), dict(reschedule={}), dict(scheduled_for=None),
                            dict(scheduled_for=(NOW + self.runtime.MASTODON_SOON).isoformat())):
                with self.subTest(changes=changes):
                    self.assertFalse(reschedule.reschedule_due(self.runtime, {}, dict(self.asset, **changes), NOW))
        with patch.object(drop_times, "api_moves", return_value=False):
            self.assertFalse(reschedule.reschedule_due(self.runtime, {}, self.asset, NOW))


if __name__ == "__main__":
    unittest.main()
