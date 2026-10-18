import contextlib
import datetime as dt
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pirillo_studio import hold, live_edits, mastodon_edits, reschedule

T = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)


class Refused(Exception):
    def __init__(self, msg, retry):
        super().__init__(msg)
        self.retry = retry


class LiveEdits(unittest.TestCase):
    """guard: a platform's refusal to edit a scheduled post never marks it posted and never sends it again."""

    def setUp(self):
        held = patch.object(hold, 'current', lambda cfg, path, defaults: dict(cfg, **{'posting.hold': {'studio': 'off'}}))
        held.start()   # the policy on disk: test_hold
        self.addCleanup(held.stop)
        self.asset = dict(id="fixture", platform="mastodon", state="scheduled", post_id="p",
                          reschedule={"to": "2026-01-01T14:00:00+00:00"}, replace=True)
        self.ledger = {}
        self.studio = SimpleNamespace(
            key=lambda account: "fixture-key", Lock=contextlib.nullcontext, load=lambda: self.ledger,
            row=lambda led, aid: (None, self.asset), CONFIG="config", DEFAULTS={},
            reschedule_due=lambda cfg, a, t: True, mastodon_replace_due=lambda a, t: True, now=lambda: T,
            parse=dt.datetime.fromisoformat, MASTODON_SOON=dt.timedelta(minutes=5), AdapterError=Refused,
            render=lambda v, a, cfg: ("words", None), log=Mock(), save=Mock(), plat_name=str, iso=str,
            tz=lambda cfg: dt.timezone.utc, reschedule_done=Mock(), reschedule_fail=Mock(),
            move=lambda a, state, by: a.update(state=state),
            mastodon_retime=Mock(side_effect=Refused("no", False)),
            mastodon_replace=Mock(side_effect=Refused("no", False)))
        for module, name in ((reschedule, 'reschedule_due'), (mastodon_edits, 'mastodon_replace_due')):
            patcher = patch.object(module, name, return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        for module, name in ((reschedule, 'reschedule_done'), (reschedule, 'reschedule_fail'),
                             (mastodon_edits, 'mastodon_retime'), (mastodon_edits, 'mastodon_replace')):
            patcher = patch.object(module, name, getattr(self.studio, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_refused_retime_keeps_the_post_where_it_is(self):
        live_edits.reschedule_api(self.studio, "fixture", {})
        self.studio.reschedule_fail.assert_called_once()
        self.studio.reschedule_done.assert_not_called()
        self.assertEqual(self.studio.mastodon_retime.call_count, 1)
        self.assertEqual(self.asset["state"], "scheduled")

    def test_refused_edit_is_a_check_never_posted(self):
        live_edits.replace_after_publish(self.studio, "fixture", {})
        self.assertEqual(self.asset["state"], "check")
        self.assertEqual(self.studio.mastodon_replace.call_count, 1)

    def test_retryable_edit_failure_stays_scheduled(self):
        self.studio.mastodon_replace.side_effect = Refused("busy", True)
        live_edits.replace_after_publish(self.studio, "fixture", {})
        self.assertEqual(self.asset["state"], "scheduled")


if __name__ == "__main__":
    unittest.main()
