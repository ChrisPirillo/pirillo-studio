import contextlib
import datetime as dt
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import sys

from pirillo_studio import hold, sends

T = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)
URL = "https://video.example/long"


class Deadline(BaseException):
    pass


class Send(unittest.TestCase):
    """guard: a row is saved as sending before the platform call, and a send that stops mid-way is a check, never sent again."""

    def setUp(self):
        held = patch.object(hold, 'current', lambda cfg, path, defaults: cfg)   # the policy on disk: test_hold
        held.start()
        self.addCleanup(held.stop)
        self.row = dict(id="v:short:x", platform="x", kind="short", method="api_now", state="planned", text="words", attempts=0)
        self.ledger = {"videos": {"v": {"assets": [self.row]}}, "log": []}
        self.saved_states, self.states_at_send = [], []
        self.adapter = Mock(side_effect=self.platform)
        moved = lambda a, to, who: a.update(state=to)
        self.studio = SimpleNamespace(
            only_installed=lambda what: None, 
            CONFIG="config", DEFAULTS={}, Lock=contextlib.nullcontext, load=lambda: self.ledger,
            save=lambda led: self.saved_states.append(self.row["state"]),
            row=lambda led, aid: (led["videos"]["v"], self.row), method_for=lambda cfg, p, kind: "api_now",
            key=lambda account: "fixture-key", ADAPTERS={"x": self.adapter}, KEY_SERVICE="service",
            Sending=lambda aid: SimpleNamespace(acquire=lambda: True, release=Mock()),
            due=lambda v, a, t, cfg: a["state"] == "planned", now=lambda: T, image_approval_block=lambda a, cfg, led: None,
            approval_send_block=lambda v, a, cfg, led: None, approval_after=lambda led, a, e: str(e),
            log=lambda led, line: led["log"].append(line), move=moved, can_move=lambda a, to, who: True,
            iso=lambda t: t.isoformat(), long_url=lambda v: None, outgoing_images=lambda a: [],
            effects_enabled=lambda cfg, a=None: False, HOME="home", freeze_posting_images=lambda job, cfg, led: job,
            record_scheduled_images=Mock(), deadline=lambda seconds: contextlib.nullcontext(),
            effect_send=lambda job, cfg, token, fn, aid: fn(job, cfg, token), AdapterError=RuntimeError,
            SendDeadline=Deadline, link_plan=lambda v, a, cfg: (a["text"], None, None, None))

    def platform(self, job, cfg, token):
        self.states_at_send.append(list(self.saved_states))
        raise KeyError("connection dropped")

    def test_stopped_send_is_a_check_and_never_resent(self):
        cfg = {"max_attempts": 3, "retry_minutes": 5}
        with self.assertRaises(KeyError):
            sends.send(self.studio, self.row["id"], cfg)
        self.assertEqual(self.states_at_send, [["sending"]])
        self.assertEqual(self.row["state"], "check")
        sends.send(self.studio, self.row["id"], cfg)
        self.assertEqual(self.adapter.call_count, 1)

    def test_buffer_video_receipt_reaches_frozen_send_job(self):
        self.row.update(id="v:short:instagram", platform="instagram", method="buffer", file="short.mp4")
        self.studio.method_for = lambda cfg, platform, kind: "buffer"
        receipt = dict(url="https://media.example/short.mp4", sha256="approved-digest")

        def host(aid, cfg, job):
            self.row["buffer"] = dict(receipt)
            if job is not None:
                job["buffer"] = dict(receipt)

        def post(s, job, cfg, token):
            self.assertEqual(job.get("buffer"), receipt, "frozen send job lost hosted video receipt")
            return dict(state="scheduled", post_id="accepted")

        self.studio.media_host_short = host
        with patch.object(sends.buffer_api, "buffer_post", side_effect=post), \
                patch.object(sends.buffer_api, "buffer_host_cleanup"), \
                patch.object(sends.facebook_posts, "facebook_page_row", return_value=False):
            sends.send(self.studio, self.row["id"], {"max_attempts": 3, "retry_minutes": 5})
        self.assertEqual(self.row["state"], "scheduled")
        self.assertEqual(self.row["buffer_post_id"], "accepted")

    def test_buffer_mapped_missing_key_never_uses_other_account(self):
        self.row.update(id="v:short:instagram", platform="instagram", method="buffer")
        self.studio.method_for = lambda cfg, platform, kind: "buffer"
        self.studio.key = Mock(return_value=None)
        cfg = {"max_attempts": 3, "retry_minutes": 5,
               "platforms": {"instagram": {"buffer_key_account": "alternate"}}}
        with patch.object(sends.buffer_api, "buffer_post") as post, \
                patch.object(sends.facebook_posts, "facebook_page_row", return_value=False):
            sends.send(self.studio, self.row["id"], cfg)
        self.studio.key.assert_called_once_with("alternate")
        post.assert_not_called()
        self.assertNotEqual(self.row["state"], "scheduled")



class LinkPlan(unittest.TestCase):
    """guard: approved words are never trimmed or skipped to fit the video link; the row goes without it in the post."""

    def setUp(self):
        words = patch.dict(sys.modules, wordcopy=SimpleNamespace(POST_MAX={"bluesky": 300, "mastodon": 500}))
        words.start()
        self.addCleanup(words.stop)
        self.studio = SimpleNamespace(long_url=lambda v: URL, has_long=lambda v: True)
        self.cfg = {"platforms": {"bluesky": {"link": "same"}}}

    def test_words_that_fill_the_post_keep_every_character(self):
        a = dict(platform="bluesky", kind="short", text="w" * 300)
        text, reply, link, note = sends.link_plan(self.studio, {}, a, self.cfg)
        self.assertEqual((text, reply, link), ("w" * 300, URL, None))
        self.assertTrue(note.startswith(sends.LINK_NO_ROOM))

    def test_room_left_puts_the_link_last(self):
        a = dict(platform="bluesky", kind="short", text="words")
        self.assertEqual(sends.link_plan(self.studio, {}, a, self.cfg), ("words\n" + URL, None, URL, None))


if __name__ == "__main__":
    unittest.main()
