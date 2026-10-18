import contextlib
import datetime as dt
import inspect
import os
import types
import unittest
from unittest import mock

from pirillo_studio import counts

T0 = dt.datetime.fromisoformat("2026-09-28T08:00:00-07:00")


class Fail(Exception):
    pass


def studio(rows, answers):
    """A synthetic studio: one video holding `rows`, a clock at T0, and Instagram's insights answered from `answers`
    ({post_id: {metric: value}}, or a Fail to raise). No network, no Keychain."""
    led = {"videos": {"v": {"assets": rows}}, "log": []}
    s = types.SimpleNamespace(now=lambda: T0, tz=lambda cfg: dt.timezone(dt.timedelta(hours=-7)), NO_BROWSER=("instagram",),
                              _when=dt.datetime.fromisoformat, Lock=contextlib.nullcontext, load=lambda: led, save=lambda l: None,
                              log=lambda l, line: l["log"].append(line), key=lambda name: "fixture", IG_API="https://ig.test",
                              assets=lambda l: [(v, a) for v in l["videos"].values() for a in v["assets"]],
                              row=lambda l, aid: next(((v, a) for v in l["videos"].values() for a in v["assets"] if a["id"] == aid), (None, None)))

    def form(method, url, token, params, timeout):
        got = answers[url.split("/")[-2]]
        if isinstance(got, Exception):
            raise got
        return 200, {"data": [{"name": k, "values": [{"value": n}]} for k, n in got.items()]}
    s._form = form
    return s, led


def post(aid, hours, **kv):
    return dict(dict(id=aid, platform="instagram", state="posted", post_id=aid, posted_at=(T0 - dt.timedelta(hours=hours)).isoformat()), **kv)


class CollectorWritesOnlyWhatThePlatformSaid(unittest.TestCase):
    """guard: the posting clock's counts never reach a platform from a test, never invent a count the platform did not
    give, and one reader's failure costs only its own reading, tried COUNT_TRIES times and then logged."""

    def collect(self, s):
        with mock.patch.dict(os.environ, STUDIO_FAKE_KEYS="1", STUDIO_FAKE_COUNTS="1"):
            return counts.collect_counts(s, {})

    def test_a_tests_keys_reach_no_platform(self):
        s, led = studio([post("a", 30)], {})
        s._form = mock.Mock(side_effect=AssertionError("reached a platform"))
        with mock.patch.dict(os.environ, STUDIO_FAKE_KEYS="1"):
            os.environ.pop("STUDIO_FAKE_COUNTS", None)
            self.assertEqual(counts.collect_counts(s, {}), 0)

    def test_day_one_reads_what_was_given_and_a_failure_is_tried_then_logged(self):
        s, led = studio([post("a", 30), post("b", 30), post("late", 72)], {"a": {"views": 734, "likes": 25}, "b": Fail("HTTP 500")})
        self.assertEqual(self.collect(s), 1)
        a, b, late = led["videos"]["v"]["assets"]
        self.assertEqual(a["stats"], dict(views_d1=734, likes_d1=25, pulled_d1="2026-09-28", by_d1="api"))   # no comments: none made up
        self.assertEqual(b["stats"], dict(tries_d1=1, miss_d1="Fail: HTTP 500"))
        self.assertNotIn("stats", late)                                                # three days old: not its day-1 number
        for _ in range(counts.COUNT_TRIES):
            self.collect(s)
        self.assertEqual(b["stats"]["tries_d1"], counts.COUNT_TRIES)                  # left after its tries, said once
        self.assertEqual(led["log"], ["b: its day-1 counts could not be read (Fail: HTTP 500)"])


class BrowserFilingKeepsKeyReads(unittest.TestCase):
    """guard: a count the analyst files from the browser never overwrites one the clock read by key, never stands in
    as a zero for a count the page did not show, and never files a reading outside its day's window."""

    def test_filing(self):
        rows = [post("x", 30, platform="x"), post("key", 30, stats=dict(views_d1=734, by_d1="api")), post("old", 72, platform="x")]
        s, led = studio(rows, {})
        done, said = counts.browser_counts(s, {}, led, [{"id": "x", "views": "1,204", "likes": 31}, {"id": "key", "views": 9},
                                                          {"id": "old", "views": 50}])
        self.assertEqual(done, 1)
        self.assertEqual(rows[0]["stats"], dict(views_d1=1204, likes_d1=31, pulled_d1="2026-09-28", by_d1="browser"))
        self.assertEqual(rows[1]["stats"], dict(views_d1=734, by_d1="api"))
        self.assertNotIn("stats", rows[2])
        self.assertEqual(said[0], "key: the clock read its day-1 counts by key; kept")
        self.assertTrue(said[1].startswith("old: it is 3.0 days old, outside both windows"))


class NothingThatThinks(unittest.TestCase):
    """guard: no model runs in the counts: one GET a reading, nothing that calls a model or a process."""

    def test_source(self):
        code = "".join(inspect.getsource(f) for f in (counts.collect_counts, counts.counts_due, 
                                                      counts.count_buffer, *counts.COUNT_READERS.values())).lower()
        for word in ("claude", "anthropic", "subprocess", "model"):
            self.assertNotIn(word, code, word)


if __name__ == "__main__":
    unittest.main()
