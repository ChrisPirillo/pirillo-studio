import contextlib
import io
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import urllib.error
import urllib.parse
import urllib.request

from pirillo_studio import tiktok_drafts


class TikTokDraftGuard(unittest.TestCase):
    """guard: never upload with a dead token or without approval, post directly, or report a failed upload as delivered."""

    def setUp(self):
        self.asset = dict(slug="fixture", file="video.mp4")
        self.config = {"platforms": {"tiktok": {"sandbox_client_key": "fixture-client"}}}
        self.token = json.dumps(dict(access_token="access", refresh_token="refresh", expires_at=5000))
        self.transport = Mock(side_effect=[
            (200, {"data": {"upload_url": "https://example.test/upload", "publish_id": "draft"}}),
            (200, {"data": {"status": "SEND_TO_USER_INBOX"}})])
        self.upload = Mock(return_value=SimpleNamespace(read=lambda: b""))
        self.opened = Mock(return_value=contextlib.nullcontext(io.BytesIO(b"video")))
        self.studio = SimpleNamespace(
            json=json, time=SimpleNamespace(time=lambda: 1000, sleep=Mock()),
            urllib=SimpleNamespace(parse=urllib.parse, error=urllib.error,
                                   request=SimpleNamespace(Request=urllib.request.Request, urlopen=self.upload)),
            os=SimpleNamespace(path=SimpleNamespace(join=os.path.join, getsize=lambda path: 5)),
            _http=self.transport, key=Mock(return_value="secret"), keychain=lambda: False,
            subprocess=SimpleNamespace(run=Mock()), KEY_SERVICE="fixture-service",
            content_dir=lambda slug: slug, approval_opened=self.opened,
            AdapterError=RuntimeError, TIKTOK_DRAFT="Finish the draft in the app.")

    def send(self):
        return tiktok_drafts.tiktok(self.studio, self.asset, self.config, self.token)

    def test_refused_refresh_never_uploads(self):
        self.token = json.dumps(dict(access_token="expired", refresh_token="refresh", expires_at=0))
        self.transport.side_effect = [(200, {"error": "invalid_grant"})]
        with self.assertRaisesRegex(RuntimeError, "sign-in needs renewing"):
            self.send()
        self.assertEqual(self.transport.call_count, 1)
        self.assertTrue(self.transport.call_args.args[1].endswith("/oauth/token/"))
        self.opened.assert_not_called()
        self.upload.assert_not_called()

    def test_refused_initialization_never_uploads(self):
        self.transport.side_effect = [(200, {"error": "denied"})]
        with self.assertRaisesRegex(RuntimeError, "refused the upload"):
            self.send()
        self.assertEqual(self.transport.call_count, 1)
        self.opened.assert_not_called()
        self.upload.assert_not_called()

    def test_missing_approval_never_uploads(self):
        self.opened.side_effect = RuntimeError("approval refused")
        with self.assertRaisesRegex(RuntimeError, "approval refused"):
            self.send()
        self.upload.assert_not_called()
        self.assertEqual(self.transport.call_count, 1)

    def test_failed_chunk_never_reports_delivery(self):
        self.upload.side_effect = urllib.error.URLError("offline")
        with self.assertRaisesRegex(RuntimeError, "upload part 1/1 failed"):
            self.send()
        self.upload.assert_called_once()
        self.assertEqual(self.transport.call_count, 1)

    def test_failed_processing_never_reports_delivery(self):
        self.transport.side_effect = [
            (200, {"data": {"upload_url": "https://example.test/upload", "publish_id": "draft"}}),
            (200, {"data": {"status": "FAILED", "fail_reason": "rejected"}})]
        with self.assertRaisesRegex(RuntimeError, "couldn't take the video.*rejected"):
            self.send()

    def test_processing_timeout_never_reports_delivery(self):
        self.transport.side_effect = [
            (200, {"data": {"upload_url": "https://example.test/upload", "publish_id": "draft"}})
        ] + [(200, {"data": {"status": "PROCESSING_UPLOAD"}})] * 90
        with self.assertRaisesRegex(RuntimeError, "still processing"):
            self.send()

    def test_success_only_delivers_an_inbox_draft(self):
        result = self.send()
        self.assertEqual(result, dict(post_id="draft", url=None, state="handoff", block=self.studio.TIKTOK_DRAFT))
        self.assertEqual([call.args[1] for call in self.transport.call_args_list], [
            tiktok_drafts.TT_API + "/v2/post/publish/inbox/video/init/",
            tiktok_drafts.TT_API + "/v2/post/publish/status/fetch/"])
        self.assertEqual(json.loads(self.transport.call_args_list[0].args[3])["source_info"]["source"], "FILE_UPLOAD")
        self.opened.assert_called_once_with(self.asset, "fixture/video.mp4", self.config)
        self.upload.assert_called_once()
        self.assertEqual(self.upload.call_args.args[0].data, b"video")
        self.assertEqual(self.upload.call_args.args[0].get_method(), "PUT")


if __name__ == "__main__":
    unittest.main()
