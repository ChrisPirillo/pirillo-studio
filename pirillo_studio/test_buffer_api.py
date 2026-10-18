from types import SimpleNamespace
from contextlib import nullcontext
import datetime as dt
import hashlib
import os
from pathlib import Path
import tempfile
import urllib.parse
import json
import re
import unittest
from unittest.mock import Mock, patch

from pirillo_studio import buffer_api


class BufferMutationGuard(unittest.TestCase):
    """guard: never resend a Buffer mutation after an uncertain outcome."""

    def test_uncertain_mutation_calls_transport_once(self):
        for failure in (OSError("offline"), {"errors": [{"message": "unavailable"}]}):
            with self.subTest(failure=failure):
                transport = Mock()
                if isinstance(failure, Exception):
                    transport.side_effect = failure
                else:
                    transport.return_value = (200, failure)
                studio = SimpleNamespace(_http=transport, AdapterError=RuntimeError, json=json, re=re)
                with self.assertRaisesRegex(
                        RuntimeError, "outcome unknown.*check Buffer before resending"):
                    buffer_api.buffer_graphql(studio, "mutation { fixture }", {},
                                              "fixture-token", mutation=True)
                transport.assert_called_once()


class BufferCredentialGuard(unittest.TestCase):
    """guard: never expose Buffer tokens or credential-shaped runs in caller errors."""

    def setUp(self):
        self.token = "fixture-token"
        self.credential = "synthetic_credential_" + "x" * 40
        self.message = f"Denied {self.token} {self.credential}"
        self.studio = SimpleNamespace(_http=Mock(), AdapterError=RuntimeError, json=json, re=re)

    def assert_scrubbed(self, text):
        self.assertNotIn(self.token, text)
        self.assertNotIn(self.credential, text)
        self.assertIn("Denied", text)

    def test_scrub_removes_token_and_long_run(self):
        self.assert_scrubbed(buffer_api.buffer_scrub(self.studio, self.message, self.token))

    def test_graphql_errors_do_not_leak_credentials(self):
        for mutation in (False, True):
            with self.subTest(mutation=mutation):
                self.studio._http.reset_mock()
                self.studio._http.return_value = (200, {
                    "errors": [{"message": self.message,
                                "extensions": {"code": self.credential}}]})
                with self.assertRaises(RuntimeError) as caught:
                    buffer_api.buffer_graphql(self.studio, "fixture", {}, self.token,
                                              mutation=mutation)
                self.assert_scrubbed(str(caught.exception))
                self.studio._http.assert_called_once()

    def test_transport_error_does_not_leak_credentials(self):
        for mutation in (False, True):
            with self.subTest(mutation=mutation):
                self.studio._http.reset_mock()
                self.studio._http.side_effect = OSError(self.message)
                with self.assertRaises(RuntimeError) as caught:
                    buffer_api.buffer_graphql(self.studio, "fixture", {}, self.token,
                                              mutation=mutation)
                self.assertNotIn(self.token, str(caught.exception))
                self.assertNotIn(self.credential, str(caught.exception))
                self.studio._http.assert_called_once()


class BufferDeliveredVideo(unittest.TestCase):
    """Production regression: native non-browser videos need no legacy browser fit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "delivered.mp4"
        self.path.write_bytes(b"approved video bytes" * 64)
        self.when = dt.datetime(2030, 1, 2, 17, tzinfo=dt.timezone.utc)
        self.media = dict(url="https://media.example/video.mp4",
                          sha256=hashlib.sha256(self.path.read_bytes()).hexdigest(),
                          bytes=self.path.stat().st_size, cover_text="EXACT COVER", thumbnail_offset_ms=0)
        self.studio = SimpleNamespace(
            os=os, json=json, re=re, hashlib=hashlib, urllib=urllib, dt=dt,
            AdapterError=RuntimeError, fitted=Mock(return_value=None),
            content_dir=lambda slug: self.tmp.name,
            video_fit_cap=Mock(side_effect=lambda cfg, platform: 285000000 if platform == "instagram" else None),
            fit_video=Mock(side_effect=lambda path, cap, slug: path), seconds=Mock(return_value=20),
            approval_opened=Mock(side_effect=lambda a, path, cfg: open(path, "rb")),
            lacks_cover_words=lambda a: not a.get("cover_text"),
            video_probe=Mock(return_value=(20, 1080, 1920)),
            parse=lambda text: dt.datetime.fromisoformat(text),
            now=lambda: dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc),
            _http=Mock(return_value=(200, {"data": {"createPost": {"post": dict(
                id="accepted", status="scheduled", schedulingType="automatic", channelId="channel",
                dueAt=self.when.isoformat())}}})))

    def row(self, platform="instagram", kind="short"):
        return dict(slug="fixture", platform=platform, kind=kind, file=self.path.name,
                    image="never-fall-back.png", buffer=dict(self.media), title="Fixture title",
                    cover_text="EXACT COVER", text="Fixture copy", scheduled_for=self.when.isoformat(), seconds=20)

    def test_non_tiktok_delivered_videos_schedule_exact_hosted_bytes(self):
        # The five rejected shapes: one full vertical and two shorts on each native route.
        for platform, kind, label in (("instagram", "vertical", "full"),
                                      ("youtube", "short", "first"), ("instagram", "short", "first"),
                                      ("youtube", "short", "second"), ("instagram", "short", "second")):
            with self.subTest(platform=platform, kind=kind, clip=label):
                self.studio._http.reset_mock()
                with patch.object(buffer_api, "buffer_channel", return_value="channel"):
                    result = buffer_api.buffer_post(self.studio, self.row(platform, kind), {}, "fixture-key")
                self.assertEqual(result["state"], "scheduled")
                sent = json.loads(self.studio._http.call_args.args[3])["variables"]["input"]
                self.assertEqual(sent["assets"][0]["video"]["url"], self.media["url"])
                self.assertEqual(sent["dueAt"], self.when.isoformat(timespec="seconds"))
                self.assertNotIn("image", sent["assets"][0])
                self.studio.fitted.assert_not_called()
                self.assertEqual(self.studio.approval_opened.call_args.args[1], str(self.path))

    def test_missing_video_never_uses_attached_image(self):
        self.path.unlink()
        with self.assertRaisesRegex(RuntimeError, "current.*video"):
            buffer_api.buffer_post(self.studio, self.row(), {}, "fixture-key")
        self.studio._http.assert_not_called()

    def test_resolver_uses_route_fit_and_frozen_host_receipt(self):
        derived = self.root / "derived.mp4"
        derived.write_bytes(b"approved derived bytes")
        self.studio.fit_video.return_value = str(derived)
        self.studio.fit_video.side_effect = None
        fit = buffer_api.buffer_video(self.studio, self.row(), {})
        self.assertEqual(fit, dict(path=str(derived), bytes=derived.stat().st_size, seconds=20, buffer=self.media))
        self.studio.fit_video.assert_called_once_with(str(self.path), 285000000, "fixture")

    def test_tiktok_keeps_real_fit_and_legacy_host_receipt(self):
        fit = dict(path=str(self.path), seconds=20, bytes=self.path.stat().st_size, buffer=self.media)
        self.studio.fitted.return_value = fit
        row = self.row("tiktok")
        row.pop("buffer")
        self.assertEqual(buffer_api.buffer_video(self.studio, row, {}), fit)
        self.studio.fitted.assert_called_once_with("fixture", "tiktok", self.path.name)
        self.studio.fit_video.assert_not_called()

    def test_tiktok_still_requires_current_fit(self):
        with self.assertRaisesRegex(RuntimeError, "current fitted video"):
            buffer_api.buffer_post(self.studio, self.row("tiktok"), {}, "fixture-key")
        self.studio._http.assert_not_called()

    def test_changed_hosted_bytes_refuse_before_create(self):
        self.path.write_bytes(b"changed after hosting")
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            buffer_api.buffer_post(self.studio, self.row(), {}, "fixture-key")
        self.studio._http.assert_not_called()

    def test_unmeasurable_video_refuses_before_create(self):
        self.studio.seconds.return_value = None
        with self.assertRaisesRegex(RuntimeError, "current.*video"):
            buffer_api.buffer_post(self.studio, self.row(), {}, "fixture-key")
        self.studio._http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
