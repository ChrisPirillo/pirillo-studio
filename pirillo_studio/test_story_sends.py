import contextlib
import datetime as dt
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pirillo_studio import hold, story_sends

T = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)


class Deadline(Exception):
    pass


class StorySend(unittest.TestCase):
    """guard: a story post is saved as sending before the platform call, and a send that stops mid-way is a check, never sent again."""

    def setUp(self):
        held = patch.object(hold, 'current', lambda cfg, path, defaults: cfg)   # the policy on disk: test_hold
        held.start()
        self.addCleanup(held.stop)
        self.ledger = {"storyposts": {"r": dict(platform="x", state="planned", text="words")}, "log": []}
        self.states_at_send = []
        self.adapter = Mock(side_effect=self.platform)
        self.studio = SimpleNamespace(
            only_installed=lambda what: None, 
            CONFIG="config", DEFAULTS={}, Sending=lambda sid: SimpleNamespace(acquire=lambda: True, release=Mock()),
            Lock=contextlib.nullcontext, load=lambda: self.ledger, save=Mock(side_effect=self.saved),
            story_due=lambda led, cfg, t: [rid for rid, x in led["storyposts"].items() if x["state"] == "planned"],
            now=lambda: T, story_image_ready=lambda x, cfg, led: True, key=lambda account: "fixture-key",
            ADAPTERS={"x": self.adapter}, KEY_SERVICE="service", story_send_job=lambda x, rid, cfg, led, ready: dict(x),
            effects_enabled=lambda cfg, x=None: False, record_scheduled_images=Mock(), iso=lambda t: t.isoformat(),
            log=lambda led, line: led["log"].append(line), deadline=lambda seconds: contextlib.nullcontext(),
            effect_send=lambda job, cfg, token, fn, sid: fn(job, cfg, token), AdapterError=RuntimeError,
            SendDeadline=Deadline, HOME="home", story_adapter=lambda x, cfg: None)
        self.saved_states = []

    def saved(self, led):
        self.saved_states.append(led["storyposts"]["r"]["state"])

    def platform(self, job, cfg, token):
        self.states_at_send.append(list(self.saved_states))
        raise KeyError("connection dropped")

    def test_stopped_send_is_a_check_and_never_resent(self):
        with self.assertRaises(KeyError):
            story_sends.story_send(self.studio, "r", {"max_attempts": 3, "retry_minutes": 5})
        self.assertEqual(self.states_at_send, [["sending"]])
        self.assertEqual(self.ledger["storyposts"]["r"]["state"], "check")
        story_sends.story_send(self.studio, "r", {"max_attempts": 3, "retry_minutes": 5})
        self.assertEqual(self.adapter.call_count, 1)


if __name__ == "__main__":
    unittest.main()
