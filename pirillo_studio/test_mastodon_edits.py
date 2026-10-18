import contextlib
import copy
import datetime as dt
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.parse

from pirillo_studio import hold, live_edits, mastodon_edits


NOW = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)


class Refused(Exception):
    def __init__(self, message, retry=False):
        super().__init__(message)
        self.retry = retry


class MastodonEditGuard(unittest.TestCase):
    """guard: refused or ambiguous edits never mark a post edited or moved, delete it, or send a second copy."""

    def setUp(self):
        held = patch.object(hold, 'current', lambda cfg, path, defaults: dict(cfg, **{'posting.hold': {'studio': 'off'}}))
        held.start()   # the policy on disk: test_hold
        self.addCleanup(held.stop)
        self.asset = dict(id="fixture", slug="fixture", kind="short", platform="mastodon", method="api_native",
                          state="scheduled", post_id="scheduled", scheduled_for=NOW.isoformat(),
                          old_text="Original words.", text="New words.", replace=True, moved=[],
                          reschedule=dict(to="2026-01-01T14:00:00+00:00"))
        self.config = dict(platforms=dict(mastodon=dict(instance="mastodon.invalid")))
        self.status = dict(id="published", url="https://mastodon.invalid/published", created_at="2026-01-01T12:00:00Z",
                           content="Original words.", media_attachments=[dict(id="media")],
                           spoiler_text="Warning", sensitive=True)
        self.recent = [self.status]
        self.answer = dict(scheduled_at="2026-01-01T14:03:00Z")
        self.refuse = False
        self.calls = []
        self.runtime = SimpleNamespace(
            dt=dt, urllib=urllib, AdapterError=Refused, _http=self.http,
            _opening=lambda text: " ".join((text or "").casefold().split()[:6]),
            drop_times=SimpleNamespace(api_moves=lambda *args: True),
            parse=dt.datetime.fromisoformat, iso=lambda value: value.isoformat(), plain_html=lambda value: value or "",
            MASTODON_WINDOW=dt.timedelta(minutes=3), MASTODON_SETTLE=dt.timedelta(minutes=1),
            MASTODON_SOON=dt.timedelta(minutes=5), effect_checkpoint=Mock(),
            key=lambda account: "token", Lock=contextlib.nullcontext, load=lambda: {},
            row=lambda ledger, aid: (None, self.asset), CONFIG="config", DEFAULTS={},
            now=lambda: NOW + dt.timedelta(minutes=2), tz=lambda cfg: dt.timezone.utc,
            render=lambda *args: (self.asset["text"], None), save=Mock(), log=Mock(), plat_name=str,
            move=lambda asset, state, actor: asset.update(state=state),
            late_still=lambda asset: {}, _at=lambda value, cfg: value)

    def http(self, method, url, token, data=None, headers=None):
        self.calls.append((method, url, data, headers))
        if method == "PUT":
            if self.refuse:
                raise Refused("PUT refused")
            if "/scheduled_statuses/" in url:
                return 200, self.answer
            return 200, dict(self.status, id="confirmed", url="https://mastodon.invalid/confirmed",
                             created_at="2026-01-01T12:00:02Z", content=self.asset["text"])
        if url.endswith("verify_credentials"):
            return 200, dict(id="account")
        return 200, self.recent

    def assert_no_duplicate(self):
        self.assertTrue(all(call[0] in ("GET", "PUT") for call in self.calls), self.calls)

    def test_retime_uses_answer_not_requested_time(self):
        original = copy.deepcopy(self.asset)
        result = mastodon_edits.mastodon_retime(self.runtime, self.asset, self.config, "token", NOW)
        self.assertEqual(result, dt.datetime(2026, 1, 1, 14, 3, tzinfo=dt.timezone.utc))
        self.assertEqual(self.asset, original)
        self.assertEqual(len(self.calls), 1)
        method, url, data, headers = self.calls[0]
        self.assertEqual((method, url), ("PUT", "https://mastodon.invalid/api/v1/scheduled_statuses/scheduled"))
        self.assertEqual(urllib.parse.parse_qs(data.decode()), dict(scheduled_at=["2026-01-01T12:00:00Z"]))
        self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded")

    def test_empty_or_malformed_retime_never_confirms_move(self):
        for answer in (None, {}, [], "bad", dict(scheduled_at=""), dict(scheduled_at="not-a-time")):
            with self.subTest(answer=answer):
                self.answer, self.calls = answer, []
                original = copy.deepcopy(self.asset)
                with self.assertRaises((Refused, ValueError)):
                    mastodon_edits.mastodon_retime(self.runtime, self.asset, self.config, "token", NOW)
                self.assertEqual(self.asset, original)
                self.assertEqual(len(self.calls), 1)
                self.assert_no_duplicate()

    def test_refused_retime_never_records_move_or_reposts(self):
        self.refuse = True
        original = copy.deepcopy(self.asset)
        with self.assertRaisesRegex(Refused, "PUT refused"):
            mastodon_edits.mastodon_retime(self.runtime, self.asset, self.config, "token", NOW)
        self.assertEqual(self.asset, original)
        self.assertEqual(len(self.calls), 1)
        self.assert_no_duplicate()

    def test_refused_retime_bookkeeping_keeps_original_schedule(self):
        self.refuse = True
        self.asset["scheduled_for"] = "2026-01-01T13:00:00+00:00"
        live_edits.reschedule_api(self.runtime, "fixture", self.config)
        self.assertEqual((self.asset["state"], self.asset["scheduled_for"], self.asset["moved"]),
                         ("scheduled", "2026-01-01T13:00:00+00:00", []))
        self.assertEqual(self.asset["stays"]["why"], "PUT refused")
        self.assertTrue(self.asset["replace"])
        self.assertNotIn("reschedule", self.asset)
        self.assertEqual(len(self.calls), 1)
        self.assert_no_duplicate()

    def test_zero_or_ambiguous_replace_never_writes(self):
        for recent in ([], [self.status, dict(self.status, id="another")],
                       [dict(self.status, content="Different opening.")],
                       [dict(self.status, created_at="2026-01-01T11:00:00Z")]):
            with self.subTest(recent=recent):
                self.recent, self.calls = recent, []
                original = copy.deepcopy(self.asset)
                result = mastodon_edits.mastodon_replace(self.runtime, self.asset, self.config, "token")
                self.assertEqual(result["state"], "check")
                self.assertEqual(self.asset, original)
                self.assertTrue(all(call[0] == "GET" for call in self.calls))
                self.runtime.effect_checkpoint.assert_not_called()

    def test_successful_replace_preserves_media_and_uses_answer(self):
        result = mastodon_edits.mastodon_replace(self.runtime, self.asset, self.config, "token")
        self.assertEqual(result, dict(state="posted", post_id="confirmed", url="https://mastodon.invalid/confirmed",
                                     posted_at="2026-01-01T12:00:02+00:00"))
        self.assertEqual([call[0] for call in self.calls], ["GET", "GET", "PUT"])
        self.assertEqual(self.calls[-1][1], "https://mastodon.invalid/api/v1/statuses/published")
        self.assertEqual(urllib.parse.parse_qs(self.calls[-1][2].decode()),
                         {"status": ["New words."], "media_ids[]": ["media"],
                          "spoiler_text": ["Warning"], "sensitive": ["true"]})
        self.runtime.effect_checkpoint.assert_called_once_with(
            dict(post_id="published", url="https://mastodon.invalid/published"))

    def test_refused_replace_never_marks_edited_or_reposts(self):
        self.refuse = True
        live_edits.replace_after_publish(self.runtime, "fixture", self.config)
        self.assertEqual(self.asset["state"], "check")
        self.assertTrue(self.asset["replace"])
        self.assertEqual(self.asset["post_id"], "scheduled")
        self.assertEqual(self.asset["moved"], [])
        self.assertIn("PUT refused", self.asset["last_error"])
        self.assertEqual([call[0] for call in self.calls], ["GET", "GET", "PUT"])
        self.assert_no_duplicate()

    def test_replace_due_refuses_wrong_route_state_or_unsettled_time(self):
        self.assertTrue(mastodon_edits.mastodon_replace_due(self.runtime, self.asset, NOW + dt.timedelta(minutes=1)))
        for changes in (dict(platform="other"), dict(method="chrome"), dict(state="posted"),
                        dict(replace=False), dict(scheduled_for=None)):
            with self.subTest(changes=changes):
                self.assertFalse(mastodon_edits.mastodon_replace_due(
                    self.runtime, dict(self.asset, **changes), NOW + dt.timedelta(minutes=2)))
        self.assertFalse(mastodon_edits.mastodon_replace_due(self.runtime, self.asset, NOW))


if __name__ == "__main__":
    unittest.main()
