import datetime as dt
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from pirillo_studio import story_calendar

T = dt.datetime(2026, 1, 1, 20, tzinfo=dt.timezone.utc)


class HeldStoryAfterDeadline(unittest.TestCase):
    """defect: working-repo #1170 (an approved browser story held past its deadline sat planned with no job after release)."""

    def jobs(self, held):
        row = dict(platform="substack", method="chrome", state="planned", ok=True, day="2026-01-01", headline="h",
                   when="2026-01-01T15:00:00+00:00", before="2026-01-01T18:00:00+00:00", **({"posting_hold": True} if held else {}))
        studio = SimpleNamespace(
            now=lambda: T, story_sweep=Mock(), digest_allowed=lambda cfg, x: True, scheduled_image_job=lambda *a: None,
            story_image_ready=lambda x, cfg, led: True, facebook_profile_story=lambda x: False, NO_BROWSER=(),
            story_unreported=lambda x, cfg, t: False, parse=dt.datetime.fromisoformat, local=lambda t, cfg: t.isoformat(),
            iso=lambda t: t.isoformat(), story_text=lambda x, cfg: "words", YT_POST_NOTE="", story_job=lambda x, rid: dict(id="story:" + rid),
            record_scheduled_images=Mock())
        return story_calendar.story_jobs(studio, {"posting.hold": {"studio": "off"}}, {"storyposts": {"r": row}})

    def test_released_held_story_goes(self):
        self.assertEqual([j["late"] for j in self.jobs(held=True)], [True])

    def test_unheld_story_past_deadline_never_goes(self):
        self.assertEqual(self.jobs(held=False), [])


if __name__ == "__main__":
    unittest.main()
