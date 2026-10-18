import copy
import datetime as dt
import types
import unittest
from unittest import mock

from pirillo_studio import scheduling


def studio():
    fixture = types.ModuleType('synthetic_studio')
    fixture.now = lambda: dt.datetime(2026, 1, 5, 8, tzinfo=dt.timezone.utc)
    fixture.tz = lambda config: dt.timezone.utc
    fixture.iso = lambda value: value.isoformat()
    fixture.parse = lambda value: dt.datetime.fromisoformat(value) if value else None
    fixture.at = lambda day, clock, config: dt.datetime.combine(
        day, dt.time.fromisoformat(clock), dt.timezone.utc)
    fixture.local = lambda value, config: value.isoformat()
    fixture.assets = lambda ledger: ((video, row) for video in ledger['videos'].values()
                                     for row in video['assets'])
    fixture.lead = lambda config, platform, method=None: dt.timedelta(minutes=5)
    fixture.has_long = lambda video: any(row['kind'] == 'long' for row in video['assets'])
    fixture.log = lambda ledger, message: ledger['log'].append(message)
    fixture.LIVE = {'sending', 'claimed', 'scheduled', 'posted', 'verified',
                    'handoff', 'check', 'skipped'}
    fixture.FIT_STEP_MIN = 5
    fixture.COVER_PLATFORMS = set()
    fixture.effects_enabled = lambda config: False
    fixture.sync_publishing_routes = mock.Mock()
    fixture.duration_route = mock.Mock()
    fixture.route_row = mock.Mock()
    fixture.on_fit_route = lambda config, row: False
    return fixture


def config():
    return {
        'posting.hold': {'studio': 'off'},
        'drops': {'slots': ['10:00', '16:00'], 'long': '10:00',
                  'offsets': {'youtube': 0, 'hand': 0, 'api': 10, 'chrome': 15},
                  'stagger': {'minutes': 10}},
        'platform_order': ['youtube', 'tiktok', 'feed'],
        'platforms': {platform: {'method': 'api_native', 'cap': 3, 'reschedule': True}
                      for platform in ('youtube', 'tiktok', 'feed')},
        'long_days': list(range(7)), 'post_days': list(range(7)),
        'min_gap_hours': 3, 'article_after_days': 1, 'grace_minutes': 10,
        'backlog_warn_days': 30, 'post_ok': True, 'vault_links': False,
    }


def asset(platform, kind='short', state='planned', when=None, method='api_native'):
    return {'id': f'{kind}-{platform}', 'slug': 'sample', 'platform': platform,
            'kind': kind, 'idx': 1, 'state': state, 'scheduled_for': when,
            'method': method, 'ok': True, 'late': False, 'moved': []}


def ledger(rows):
    return {'videos': {'sample': {'slug': 'sample', 'approved': True,
                                  'ingested': '2026-01-01', 'assets': rows}}, 'log': []}


class SchedulingGuards(unittest.TestCase):
    """guard: replanning never rewrites platform schedules or puts drop siblings before YouTube."""

    def setUp(self):
        self.studio = studio()
        self.config = config()
        self.studio.DEFAULTS = copy.deepcopy(self.config)

    def test_replan_keeps_every_on_platform_timestamp(self):
        for state in sorted(self.studio.LIVE - {'skipped'}):
            with self.subTest(state=state):
                rows = [asset('youtube', state=state, when='2026-01-06T10:00:00+00:00'),
                        asset('feed', state='scheduled', when='2026-01-06T16:20:00+00:00'),
                        asset('tiktok', when='2026-01-07T10:10:00+00:00')]
                original = [row['scheduled_for'] for row in rows[:2]]
                record = ledger(rows)
                scheduling.plan(self.studio, self.config, record, replan=True)
                result = record['videos']['sample']['assets']
                self.assertEqual([row['scheduled_for'] for row in result[:2]], original)
                self.assertEqual(result[2]['scheduled_for'], '2026-01-06T10:10:00+00:00')
                self.assertEqual(result[2]['moved'], ['2026-01-07T10:10:00+00:00'])

    def test_new_release_and_short_drop_never_precede_youtube(self):
        rows = [asset(platform, kind=kind) for kind in ('long', 'short')
                for platform in ('feed', 'tiktok', 'youtube')]
        record = ledger(rows)
        scheduling.plan(self.studio, self.config, record)
        for kind in ('long', 'short'):
            with self.subTest(kind=kind):
                group = [row for row in rows if row['kind'] == kind]
                youtube = next(row for row in group if row['platform'] == 'youtube')
                anchor = self.studio.parse(youtube['scheduled_for'])
                self.assertIsNotNone(anchor)
                for row in group:
                    self.assertIsNotNone(row['scheduled_for'])
                    self.assertGreaterEqual(self.studio.parse(row['scheduled_for']), anchor)
                self.assertEqual(len({row['scheduled_for'] for row in group}), len(group))



class BacklogSaidOnce(unittest.TestCase):
    """defect: studio-tick.log 2026-10-17 repeated "backlog: 16 posts sit more than 14 days out" on every pass."""

    def test_the_backlog_line_is_logged_once_per_change(self):
        fake, cfg = studio(), config()
        rows = [asset('feed', state='scheduled', when='2026-03-01T10:00:00+00:00')]
        record = ledger(rows)
        for _ in range(3):
            scheduling.plan(fake, cfg, record)
        rows.append(dict(asset('tiktok', state='scheduled', when='2026-03-02T10:00:00+00:00'), id='second'))
        scheduling.plan(fake, cfg, record)
        self.assertEqual([line[:10] for line in record['log']], ['backlog: 1', 'backlog: 2'])


if __name__ == '__main__':
    unittest.main()
