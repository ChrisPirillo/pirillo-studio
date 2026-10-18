import datetime as dt
import io
import json
import os
import tempfile
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request
import uuid

from pirillo_studio import youtube_posts


class Refused(Exception):
    def __init__(self, message, retry=False):
        super().__init__(message)
        self.retry = retry


class Reply(io.BytesIO):
    def __init__(self, body=b"", headers=None):
        super().__init__(body)
        self.headers = headers or {}


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://upload.invalid", code, "fixture", headers or {}, io.BytesIO(b"no"))


class YouTubeUploadGuard(unittest.TestCase):
    """guard: never upload to another or an unnamed channel, never report a refused or unfinished upload as scheduled, never fail the video over an extra."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for name, body in (("clip.mp4", b"0123456789"), ("thumb.png", b"png"), ("clip.srt", b"srt")):
            with open(os.path.join(self.dir, name), "wb") as f:
                f.write(body)
        self.want = "@fixture"
        self.key = dict(channel_handle="@fixture", channel_title="Fixture")
        self.replies, self.requests, self.extras = [], [], []
        self.row = dict(slug="fixture", file="clip.mp4", kind="short", idx=1, title="A short", text="Words.",
                        scheduled_for="2026-01-02T09:30:00-08:00")
        self.s = SimpleNamespace(
            os=os, json=json, dt=dt, uuid=uuid, AdapterError=Refused, YT_API="https://yt.invalid",
            urllib=SimpleNamespace(request=SimpleNamespace(Request=urllib.request.Request, urlopen=self.urlopen),
                                   error=urllib.error),
            load_config=lambda: {"platforms": {"youtube": {"channel_handle": self.want}}},
            yt_access=lambda account: ("token", self.key), content_dir=lambda slug: self.dir,
            rj=lambda path: {"shorts": [{"srt": "clip.srt"}]}, release_path=lambda slug: "release.json",
            parse=dt.datetime.fromisoformat, try_cover=lambda a, platform, missed: (os.path.join(self.dir, "thumb.png"), "cover"),
            approval_opened=lambda a, path, cfg: open(path, "rb"),
            rb=lambda path: b"srt", _http=self.http)

    def urlopen(self, req, timeout):
        self.requests.append(req)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def http(self, method, url, token, body, headers):
        self.extras.append(url)
        if "thumbnails" in url:
            raise Refused("thumbnail refused")
        return 200, {}

    def upload_ok(self):
        self.replies = [Reply(headers={"Location": "https://upload.invalid/session"}),
                        Reply(json.dumps({"id": "vid"}).encode())]

    def test_another_channel_or_no_channel_never_uploads(self):
        for want in ("@someone-else", None):
            self.want = want
            with self.assertRaises(Refused):
                youtube_posts.youtube(self.s, self.row, {}, None)
        self.assertEqual(self.requests, [])

    def test_an_unreadable_release_never_uploads(self):
        def broken(path):
            raise ValueError("not json")
        self.s.rj = broken
        with self.assertRaisesRegex(Refused, "release file is unreadable"):
            youtube_posts.youtube(self.s, self.row, {}, None)
        self.assertEqual(self.requests, [])

    def test_a_missing_file_never_uploads(self):
        os.remove(os.path.join(self.dir, "clip.mp4"))
        with self.assertRaises(Refused):
            youtube_posts.youtube(self.s, self.row, {}, None)
        self.assertEqual(self.requests, [])

    def test_a_refused_upload_raises_and_retries_only_on_5xx(self):
        for code, retry in ((403, False), (503, True)):
            self.replies = [http_error(code)]
            with self.assertRaises(Refused) as e:
                youtube_posts.youtube(self.s, self.row, {}, None)
            self.assertEqual(e.exception.retry, retry)

    def test_an_upload_that_stops_mid_file_is_never_scheduled(self):
        for code in (500, 403):
            self.replies = [Reply(headers={"Location": "https://upload.invalid/session"}), http_error(code)]
            with self.assertRaises(Refused):
                youtube_posts.youtube(self.s, self.row, {}, None)

    def test_a_partial_upload_resumes_from_what_youtube_received(self):
        self.replies = [Reply(headers={"Location": "https://upload.invalid/session"}),
                        http_error(308, {"Range": "bytes=0-3"}), Reply(json.dumps({"id": "vid"}).encode())]
        youtube_posts.yt_upload(self.s, os.path.join(self.dir, "clip.mp4"), {}, {}, "token", chunk=6)
        self.assertEqual([r.headers.get("Content-range") for r in self.requests[1:]],
                         ["bytes 0-5/10", "bytes 4-9/10"])

    def test_a_failed_extra_keeps_the_video_and_says_which(self):
        self.upload_ok()
        r = youtube_posts.youtube(self.s, self.row, {}, None)
        self.assertEqual((r["state"], r["post_id"]), ("scheduled", "vid"))
        self.assertEqual(len(r["missed"]), 1)
        self.assertIn("thumbnail", r["missed"][0])
        self.assertTrue(any("captions" in url for url in self.extras))

    def test_it_uploads_private_with_youtube_holding_the_time(self):
        self.upload_ok()
        youtube_posts.youtube(self.s, self.row, {}, None)
        meta = json.loads(self.requests[0].data)
        self.assertEqual(meta["status"], {"privacyStatus": "private", "publishAt": "2026-01-02T17:30:00Z",
                                          "selfDeclaredMadeForKids": False})


if __name__ == "__main__":
    unittest.main()
