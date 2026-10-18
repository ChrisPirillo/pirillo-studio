import contextlib
import datetime as dt
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pirillo_studio import threads_posts


class Refused(Exception):
    pass


def wordcopy():
    """A stand-in for the caller's copy rules: the last "Topic:" line is split off the words and always picked."""
    def split(text):
        words, _, topic = text.rpartition("\n\nTopic: ")
        return (words, [topic]) if words else (text, [])
    return SimpleNamespace(topic_split=split, topic_chain=lambda topic: [topic] if topic else [],
                           topic_book=lambda path: {}, topic_pick=lambda chain, book, today: (
                               (chain[0], "fixture") if chain else (None, "no topic")))


class ThreadsPostGuard(unittest.TestCase):
    """guard: never publish an unprocessed container, never post a long cut without its link, never put a topic in
    the words, never report an unconfirmed image as posted, and never lose a live post to a failed reply."""

    def setUp(self):
        self.calls, self.status, self.meta, self.reply_fails = [], "FINISHED", {"media_type": "IMAGE", "media_url": "u"}, False
        self.studio = SimpleNamespace(
            TH_API="https://threads.invalid", AdapterError=Refused, _form=self.form, os=os, time=SimpleNamespace(sleep=Mock()),
            contextlib=contextlib, content_dir=lambda slug: slug, _probe_secs=lambda path: 30,
            public_url=lambda path: contextlib.nullcontext("https://fixture.invalid/media"),
            approval_check=lambda a, path, cfg: path, effect_checkpoint=Mock(),
            api_text_image=Mock(return_value=("still.jpg", "Alt.")), api_text_commit=lambda form, *args: form(*args),
            now=lambda: dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), tz=lambda cfg: dt.timezone.utc,
            iso=lambda t: t.isoformat(), TOPICS="topics.json")
        patcher = patch.dict("sys.modules", wordcopy=wordcopy())
        patcher.start()
        self.addCleanup(patcher.stop)

    def form(self, method, url, token, fields):
        self.calls.append((method, url, fields))
        if url.endswith("/me/threads"):
            if fields.get("reply_to_id") and self.reply_fails:
                raise Refused("reply refused")
            return 200, {"id": "container"}
        if url.endswith("/container"):
            return 200, {"status": self.status, "error_message": "fixture"}
        if url.endswith("/threads_publish"):
            return 200, {"id": "post"}
        return 200, dict(self.meta, permalink="https://fixture.invalid/post")

    def published(self):
        return [c for c in self.calls if c[1].endswith("/threads_publish")]

    def row(self, **kw):
        return dict(dict(slug="fixture", file="short.mp4", kind="short", text="Words."), **kw)

    def test_unprocessed_container_never_publishes(self):
        for status in ("ERROR", "EXPIRED"):
            for send in (lambda: threads_posts.threads(self.studio, self.row(), {}, "token"),
                         lambda: threads_posts.threads_text(self.studio, self.row(kind="article"), {}, "token")):
                self.calls, self.status = [], status
                with self.assertRaisesRegex(Refused, "couldn't process"):
                    send()
                self.assertEqual(self.published(), [])

    def test_long_cut_without_link_never_posts(self):
        with self.assertRaisesRegex(Refused, "YouTube URL"):
            threads_posts.threads(self.studio, self.row(kind="long"), {}, "token")
        self.assertEqual(self.calls, [])

    def test_topic_goes_only_as_topic_tag(self):
        r = threads_posts.threads(self.studio, self.row(text="Words.\n\nTopic: Fixture"), {}, "token")
        sent = self.calls[0][2]
        self.assertEqual((sent["text"], sent["topic_tag"], r["topic"]), ("Words.", "Fixture", "Fixture"))
        self.assertTrue(all("Topic:" not in str(c[2]) for c in self.calls))

    def test_unconfirmed_image_is_a_check(self):
        for meta in ({"media_type": "TEXT"}, {"media_type": "IMAGE"}):
            self.meta = meta
            r = threads_posts.threads_text(self.studio, self.row(kind="article"), {}, "token")
            self.assertEqual((r["state"], r["image"]), ("check", False))
            self.assertIn("image not confirmed", r["image_note"])

    def test_failed_reply_keeps_the_live_post(self):
        self.reply_fails = True
        r = threads_posts.threads(self.studio, self.row(reply_text="https://fixture.invalid/long"), {}, "token")
        self.assertEqual((r["post_id"], r["state"]), ("post", "posted"))
        self.studio.effect_checkpoint.assert_called_once_with(dict(post_id="post"))
        self.assertEqual(len(self.published()), 1)


if __name__ == "__main__":
    unittest.main()
