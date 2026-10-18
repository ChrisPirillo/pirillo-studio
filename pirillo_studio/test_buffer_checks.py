import contextlib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pirillo_studio import buffer_api, buffer_checks


class BufferCheck(unittest.TestCase):
    """guard: a Buffer post is marked live only on Buffer's sent status with its permalink; a failed read changes nothing."""

    def setUp(self):
        for name in ('buffer_read_post', 'buffer_host_cleanup'):
            stub = patch.object(buffer_api, name)
            stub.start()
            self.addCleanup(stub.stop)
        self.asset = dict(id="fixture", method="buffer", post_id="p1", state="scheduled")
        self.ledger = {"asset": self.asset}
        self.studio = SimpleNamespace(
            Lock=contextlib.nullcontext, AdapterError=RuntimeError, load=lambda: self.ledger,
            publishing_row=lambda led, aid: led["asset"], key=lambda account: "fixture-key",
            effects_enabled=lambda cfg, a=None: False, parse=lambda value: SimpleNamespace(isoformat=lambda: value),
            move=lambda a, state, by: a.update(state=state), save=Mock())

    def check(self, **post):
        buffer_api.buffer_read_post.return_value = dict(id="p1", **post)
        buffer_checks.buffer_verify(self.studio, "fixture", {})

    def test_slot_passing_without_permalink_never_marks_live(self):
        self.check(status="sent", sentAt="2026-01-01T00:00:00Z")
        self.assertEqual(self.asset["state"], "scheduled")
        self.assertNotIn("verified", self.asset)

    def test_failed_read_changes_nothing(self):
        buffer_api.buffer_read_post.side_effect = RuntimeError("offline")
        buffer_checks.buffer_verify(self.studio, "fixture", {})
        self.assertEqual(self.asset, dict(id="fixture", method="buffer", post_id="p1", state="scheduled"))
        self.studio.save.assert_not_called()

    def test_sent_with_permalink_is_verified(self):
        self.check(status="sent", sentAt="2026-01-01T00:00:00Z", externalLink="https://example.test/post")
        self.assertEqual((self.asset["state"], self.asset["verified"], self.asset["url"]),
                         ("verified", True, "https://example.test/post"))


if __name__ == "__main__":
    unittest.main()
