import copy
import datetime as dt
import threading
import types
import unittest
from unittest import mock

from pirillo_studio import reminders


def studio():
    fixture = types.ModuleType("synthetic_studio")
    fixture.ledger = {
        "video": {"approved": True},
        "asset": {"id": "post-1", "method": "hand", "state": "planned",
                  "platform": "fixture", "title": "Fixture headline",
                  "scheduled_for": "2026-01-01T11:00:00+00:00"},
        "log": [],
    }

    def save(ledger):
        fixture.ledger = copy.deepcopy(ledger)

    def row(ledger, asset_id):
        assert ledger["asset"]["id"] == asset_id
        return ledger["video"], ledger["asset"]

    lock = threading.Lock()
    fixture.Lock = lambda: lock
    fixture.only_installed = mock.Mock()
    fixture.load = lambda: copy.deepcopy(fixture.ledger)
    fixture.save = mock.Mock(side_effect=save)
    fixture.assets = lambda ledger: [(ledger["video"], ledger["asset"])]
    fixture.row = row
    fixture.now = mock.Mock(return_value=dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc))
    fixture.parse = dt.datetime.fromisoformat
    fixture.iso = lambda value: value.isoformat()
    fixture.render = mock.Mock(return_value=("Fixture description", []))
    fixture.plat_name = lambda platform: platform.title()
    fixture.log = lambda ledger, message: ledger["log"].append(message)
    return fixture


class Reminders(unittest.TestCase):
    """guard: reminders never send twice, lose an active claim, or send without a recipient."""

    def setUp(self):
        self.studio = studio()
        self.config = {"reminders": {"to": "fixture-recipient", "from": "fixture-sender"}}

    def test_due_approved_hand_planned_row_is_mailed_once(self):
        with mock.patch("pirillo_studio.reminders.send_mail", return_value=True) as send:
            reminders.remind(self.studio, self.config)
            reminders.remind(self.studio, self.config)
        send.assert_called_once_with("Fixture: Fixture headline", "Fixture description",
                                     "fixture-recipient", "fixture-sender")
        asset = self.studio.ledger["asset"]
        self.assertEqual(asset["reminded_at"], self.studio.iso(self.studio.now()))
        self.assertNotIn("reminding_at", asset)
        self.assertEqual(self.studio.ledger["log"], ["post-1: reminder emailed"])

    def test_overlapping_tick_leaves_active_claim_unchanged(self):
        def sending(*args):
            claim = self.studio.ledger["asset"]["reminding_at"]
            saves = self.studio.save.call_count
            self.assertTrue(self.studio.Lock().acquire(blocking=False))
            self.studio.Lock().release()
            self.studio.now.return_value += dt.timedelta(minutes=14)
            reminders.remind(self.studio, self.config)
            self.assertEqual(self.studio.ledger["asset"]["reminding_at"], claim)
            self.assertEqual(self.studio.save.call_count, saves)
            return True

        with mock.patch("pirillo_studio.reminders.send_mail", side_effect=sending) as send:
            reminders.remind(self.studio, self.config)
        self.assertEqual(send.call_count, 1)

    def test_false_send_clears_claim_and_next_run_retries(self):
        with mock.patch("pirillo_studio.reminders.send_mail", side_effect=[False, True]) as send:
            reminders.remind(self.studio, self.config)
            self.assertNotIn("reminding_at", self.studio.ledger["asset"])
            self.assertNotIn("reminded_at", self.studio.ledger["asset"])
            self.assertEqual(self.studio.ledger["log"],
                             ["post-1: reminder email failed; the next run tries again"])
            reminders.remind(self.studio, self.config)
        self.assertEqual(send.call_count, 2)
        self.assertIn("reminded_at", self.studio.ledger["asset"])
        self.assertNotIn("reminding_at", self.studio.ledger["asset"])

    def test_unset_recipient_never_sends(self):
        before = self.studio.load()
        with mock.patch("pirillo_studio.reminders.send_mail") as send:
            reminders.remind(self.studio, {"reminders": {}})
        send.assert_not_called()
        self.studio.save.assert_not_called()
        self.assertEqual(self.studio.ledger, before)


if __name__ == "__main__":
    unittest.main()
