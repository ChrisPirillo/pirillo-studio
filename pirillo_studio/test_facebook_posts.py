import contextlib
import copy
import datetime as dt
import io
import json
import mimetypes
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from pirillo_studio import facebook_posts


class Refused(Exception):
    def __init__(self, message, retry=False):
        super().__init__(message)
        self.retry = retry


class FacebookPostGuard(unittest.TestCase):
    """guard: never report failed sends as posted, leak keys, upload without a checkpoint, or post to another Page."""

    def setUp(self):
        self.cfg = {"platforms": {"facebook": {"page": "fixture", "live_page": "Live Fixture"}}}
        self.token = json.dumps(dict(page_id="page", page_token="secret-fixture", page_handle="fixture"))
        self.job = dict(id="fixture:facebook", slug="fixture", kind="short", file="clip.mp4",
                        text="Words.", state="sending", sent_at="fixture-time")
        self.live = dict(self.job)
        self.saved, self.calls = [], []
        self.failure = None
        self.studio = SimpleNamespace(
            json=json, dt=dt, os=SimpleNamespace(path=SimpleNamespace(join=os.path.join, getsize=lambda path: 5)),
            mimetypes=mimetypes, uuid=SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="fixture-boundary")),
            AdapterError=Refused, DEFAULTS=self.cfg, FB_API="https://graph.invalid",
            now=lambda: dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), iso=lambda value: value.isoformat(),
            Lock=contextlib.nullcontext, load=lambda: [self.live], row=lambda ledger, aid: (None, ledger[0]),
            save=lambda ledger: self.saved.append(copy.deepcopy(ledger[0])),
            content_dir=lambda slug: "fixture", fit_video=lambda path, *args: path,
            API_VIDEO_BYTES={"facebook": 100}, _probe_secs=lambda path: 30,
            try_cover=lambda *args: (None, None), cover_note=lambda *args: {},
            approval_opened=lambda *args: contextlib.closing(io.BytesIO(b"video")),
            approval_check=lambda *args: "fixture.mp4",
            public_url=lambda path: contextlib.nullcontext("https://fixture.invalid/video"),
            time=SimpleNamespace(sleep=Mock()), _form=self.form, _http=self.http,
            _multipart=Mock(return_value=(b"image", "image/jpeg")),
            facebook_confirm=SimpleNamespace(facebook_read=Mock(return_value={"url": "/fixture-post"})))

    def assert_checkpoint(self, phase):
        self.assertTrue(self.saved, "upload requires a saved allocation")
        self.assertEqual((self.saved[-1]["post_id"], self.saved[-1]["facebook_phase"]), ("allocated", phase))

    def form(self, method, url, token, fields, **kwargs):
        self.calls.append((method, url, dict(fields)))
        if fields.get("upload_phase") == "start":
            return 200, dict(video_id="allocated", upload_session_id="session", start_offset="0", end_offset="5")
        if fields.get("upload_phase") == "finish":
            self.assert_checkpoint("publish_requested")
            return 200, {"success": self.failure != "finish"}
        raise AssertionError("unexpected synthetic request")

    def http(self, *args, **kwargs):
        self.assert_checkpoint("uploading")
        if self.failure == "transfer":
            raise Refused("synthetic transfer refused")
        if "Authorization" in args[4]:
            return 200, {"success": self.failure != "upload"}
        return 200, {"start_offset": "5", "end_offset": "5"}

    def send(self, kind="short"):
        self.job["kind"] = kind
        return facebook_posts.facebook(self.studio, self.job, self.cfg, self.token)

    def test_failed_upload_or_finish_never_reports_posted(self):
        for kind, failure in (("short", "transfer"), ("long", "transfer"), ("short", "upload"),
                              ("short", "finish"), ("long", "finish")):
            with self.subTest(kind=kind, failure=failure):
                self.setUp()
                self.failure = failure
                if failure in ("transfer", "upload"):
                    with self.assertRaises(Refused):
                        self.send(kind)
                    self.assertFalse(any(fields.get("upload_phase") == "finish" for _, _, fields in self.calls))
                    self.studio.facebook_confirm.facebook_read.assert_not_called()
                else:
                    result = self.send(kind)
                    self.assertEqual((result["state"], result["post_id"]), ("check", "allocated"))
                    self.studio.facebook_confirm.facebook_read.assert_not_called()

    def test_missing_confirmation_never_reports_posted(self):
        for response in (None, Refused("synthetic readback refused")):
            with self.subTest(response=response):
                self.setUp()
                reader = self.studio.facebook_confirm.facebook_read
                if isinstance(response, Exception):
                    reader.side_effect = response
                else:
                    reader.return_value = response
                result = self.send()
                self.assertEqual((result["state"], result["post_id"]), ("check", "allocated"))
                self.assertTrue(result["facebook_confirm"])

    def test_failed_video_status_keeps_confirmation_without_publishing(self):
        for status in ({"video_status": "error"}, {"video_status": "expired"},
                       {"uploading_phase": {"status": "error"}}):
            with self.subTest(status=status):
                self.setUp()
                def form(method, url, token, fields, **kwargs):
                    self.calls.append((method, url, dict(fields)))
                    if fields.get("upload_phase") == "start":
                        raise Refused("synthetic resumable refusal")
                    if "file_url" in fields:
                        return 200, {"id": "allocated"}
                    if method == "GET":
                        return 200, {"status": status}
                    raise AssertionError("must not publish a failed video")
                self.studio._form = form
                result = self.send("long")
                self.assertEqual((result["state"], result["post_id"]), ("check", "allocated"))
                self.assertTrue(result["facebook_confirm"])
                self.assertNotIn("secret-fixture", json.dumps(self.job))
                self.studio.facebook_confirm.facebook_read.assert_not_called()

    def test_checkpoints_precede_transfer_and_finish(self):
        for kind in ("short", "long"):
            with self.subTest(kind=kind):
                self.setUp()
                self.assertEqual(self.send(kind)["state"], "posted")
                self.assertEqual([row["facebook_phase"] for row in self.saved],
                                 ["uploading", "publish_requested", "finished"])

    def test_checkpoint_save_failure_stops_irreversible_call(self):
        for kind in ("short", "long"):
            for phase in ("uploading", "publish_requested"):
                with self.subTest(kind=kind, phase=phase):
                    self.setUp()
                    def save(ledger):
                        if ledger[0]["facebook_phase"] == phase:
                            raise OSError("synthetic disk refused")
                        self.saved.append(copy.deepcopy(ledger[0]))
                    self.studio.save = save
                    self.studio._http = Mock(side_effect=self.http)
                    with self.assertRaisesRegex(OSError, "disk refused"):
                        self.send(kind)
                    self.assertFalse(any(fields.get("upload_phase") == "finish" for _, _, fields in self.calls))
                    if phase == "uploading":
                        self.studio._http.assert_not_called()

    def test_changed_or_conflicting_checkpoint_never_transfers(self):
        for changes in ({"state": "check"}, {"sent_at": "changed"}, {"post_id": "another-allocation"}):
            with self.subTest(changes=changes):
                self.setUp()
                self.live.update(changes)
                self.studio._http = Mock(side_effect=AssertionError("must not transfer"))
                with self.assertRaises(Refused):
                    self.send()
                self.studio._http.assert_not_called()
                self.assertEqual(len(self.calls), 1)
                if "post_id" not in changes:
                    self.assert_checkpoint("uploading")
                else:
                    self.assertEqual(self.saved, [])

    def test_pending_never_persists_credentials(self):
        reason = "https://graph.invalid?access_token=secret-fixture Authorization: secret-fixture"
        facebook_posts.facebook_pending(self.studio, self.job, self.cfg, reason)
        self.assertNotIn("secret-fixture", json.dumps(self.job))
        self.assertNotIn("access_token", json.dumps(self.job))
        self.assertTrue(self.job["facebook_confirm"])

    def test_wrong_video_page_never_uploads(self):
        self.token = json.dumps(dict(page_id="other", page_token="secret-fixture", page_handle="other"))
        self.studio.fit_video = Mock(side_effect=AssertionError("must not prepare upload"))
        with self.assertRaises(Refused):
            self.send()
        self.studio.fit_video.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_page_key_rejects_missing_or_wrong_page(self):
        for key in ({}, {"page_id": "page"}, {"page_token": "secret-fixture"},
                    dict(page_id="page", page_token="secret-fixture", page_handle="other")):
            with self.subTest(key=key), self.assertRaises(Refused):
                facebook_posts.facebook_key(self.studio, self.cfg, json.dumps(key))
        self.assertEqual(facebook_posts.facebook_key(self.studio, self.cfg, self.token), json.loads(self.token))

    def test_profile_article_never_uses_page_api(self):
        self.job.update(kind="article")
        with self.assertRaises(Refused):
            self.send(kind="article")
        self.assertEqual(self.calls, [])

    def test_live_row_requires_matching_live_page_credentials(self):
        self.job.update(kind="article", account={"kind": "page"})
        self.studio._form = Mock(return_value=(200, {"id": "live-post"}))
        self.studio._http = Mock(return_value=(200, {"id": "live-post"}))
        for key in ({}, {"page_id": "page"}, {"page_token": "secret-fixture"},
                    dict(page_id="page", page_token="secret-fixture", page_handle="fixture"),
                    dict(page_id="page", page_token="secret-fixture", page_name="Live Fixture Extra")):
            with self.subTest(key=key), self.assertRaises(Refused):
                facebook_posts.facebook(self.studio, self.job, self.cfg, json.dumps(key))
        self.studio._form.assert_not_called()
        self.studio._http.assert_not_called()
        self.cfg["platforms"]["facebook"]["live_page"] = ""
        with self.assertRaises(Refused):
            facebook_posts.facebook(self.studio, self.job, self.cfg, self.token)
        self.cfg["platforms"]["facebook"]["live_page"] = "Live Fixture"
        for field in ("page_handle", "page_name"):
            key = dict(page_id="live-page", page_token="secret-fixture", **{field: "LIVEfixture"})
            result = facebook_posts.facebook(self.studio, self.job, self.cfg, json.dumps(key))
            self.assertEqual(result["state"], "posted")
            self.assertEqual(self.studio._form.call_args_list[-2].args[1], "https://graph.invalid/live-page/feed")

    def test_live_post_missing_id_never_reports_posted_or_retries(self):
        token = json.dumps(dict(page_id="live-page", page_token="secret-fixture", page_name="Live Fixture"))
        for image in (None, "still.jpg"):
            with self.subTest(image=image):
                self.job.update(kind="article", account={"kind": "page"}, image=image)
                self.studio._form = Mock(return_value=(200, {}))
                self.studio._http = Mock(return_value=(200, {}))
                with self.assertRaises(Refused):
                    facebook_posts.facebook(self.studio, self.job, self.cfg, token)
                self.assertEqual(self.studio._form.call_count + self.studio._http.call_count, 1)

    def test_live_permalink_failure_never_resends(self):
        self.job.update(kind="article", account={"kind": "page"})
        token = json.dumps(dict(page_id="live-page", page_token="secret-fixture", page_name="Live Fixture"))
        self.studio._form = Mock(side_effect=[(200, {"id": "live-post"}), Refused("readback refused")])
        result = facebook_posts.facebook(self.studio, self.job, self.cfg, token)
        self.assertEqual((result["state"], result["post_id"]), ("posted", "live-post"))
        self.assertEqual([call.args[0] for call in self.studio._form.call_args_list], ["POST", "GET"])


if __name__ == "__main__":
    unittest.main()
