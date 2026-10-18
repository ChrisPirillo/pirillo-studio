import contextlib
import datetime as dt
import json
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pirillo_studio import live_edits, reschedule

T = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)
OFF = {'posting.hold': {'studio': 'off'}}


class HoldMidTick(unittest.TestCase):
    """defect: 57d1cffc (#1170): a hold set mid-tick did not stop a reschedule of a published post."""

    def test_hold_set_mid_tick_stops_a_reschedule(self):
        with tempfile.TemporaryDirectory() as d:
            config = os.path.join(d, 'studio.json')
            with open(config, 'w') as f:
                json.dump({'posting.hold': {'studio': {'state': 'on', 'reason': 'pause', 'actor': 'chris'}}}, f)
            asset = dict(id='a', platform='mastodon', state='scheduled', post_id='p', reschedule={'to': T.isoformat()})
            s = SimpleNamespace(key=lambda account: 'k', CONFIG=config, DEFAULTS=OFF, Lock=contextlib.nullcontext,
                                load=dict, row=lambda led, aid: ({}, asset), now=lambda: T, parse=dt.datetime.fromisoformat,
                                MASTODON_SOON=dt.timedelta(minutes=5), AdapterError=RuntimeError, iso=str,
                                tz=lambda cfg: dt.timezone.utc, save=lambda led: None)
            with patch.object(reschedule, 'reschedule_due', return_value=True), patch.object(reschedule, 'reschedule_done'), \
                    patch('pirillo_studio.mastodon_edits.mastodon_retime', return_value=T) as retime:
                live_edits.reschedule_api(s, 'a', dict(OFF))   # the tick's cached settings still say off
            retime.assert_not_called()


if __name__ == '__main__':
    unittest.main()
