import io
import json
import types
import unittest
import urllib.error
from unittest import mock

from pirillo_studio import tokens


def studio(age, on_mac=True):
    """A synthetic studio: every key `age` days old, each holding OLD-<platform>. No network, no Keychain."""
    return types.SimpleNamespace(key_age_days=lambda p: age, key=lambda p: "OLD-" + p, keychain=lambda: on_mac,
                                 KEY_SERVICE="fixture-service")


class Renewal(unittest.TestCase):
    """guard: a token never reaches the log, and Meta is asked only for a token 20 days old or more."""

    def test_renewed_token_goes_to_the_keychain_only(self):
        answer = lambda url, timeout: io.BytesIO(json.dumps({"access_token": "NEW-SECRET"}).encode())
        with mock.patch.object(tokens.urllib.request, "urlopen", side_effect=answer) as got, \
                mock.patch.object(tokens.subprocess, "run") as run:
            said = tokens.refresh_tokens(studio(21))
        self.assertEqual(said, ["instagram: token renewed (60 more days)", "threads: token renewed (60 more days)"])
        self.assertEqual(got.call_count, 2)
        self.assertEqual([c.args[0][-1] for c in run.call_args_list], ["NEW-SECRET", "NEW-SECRET"])
        self.assertNotIn("SECRET", " ".join(said))

    def test_young_token_is_never_sent_and_a_failure_names_no_token(self):
        with mock.patch.object(tokens.urllib.request, "urlopen") as got:
            self.assertEqual(tokens.refresh_tokens(studio(19)), [])
        got.assert_not_called()
        with mock.patch.object(tokens.urllib.request, "urlopen", side_effect=urllib.error.URLError("down")), \
                mock.patch.object(tokens.subprocess, "run") as run:
            said = tokens.refresh_tokens(studio(30))
        run.assert_not_called()
        self.assertEqual(len(said), 2)
        self.assertTrue(all("the next run tries again" in m and "OLD-" not in m for m in said), said)

    def test_off_the_mac_nothing_is_written(self):
        answer = lambda url, timeout: io.BytesIO(json.dumps({"access_token": "NEW-SECRET"}).encode())
        with mock.patch.object(tokens.urllib.request, "urlopen", side_effect=answer), \
                mock.patch.object(tokens.subprocess, "run") as run:
            self.assertEqual(tokens.refresh_tokens(studio(21, on_mac=False)), [])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
