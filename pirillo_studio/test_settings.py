import contextlib
import fcntl
import io
import json
import os
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

from pirillo_studio import settings

DEFAULTS = {'tz': 'America/Los_Angeles', 'keep_renders_days': 3, 'overnight': {'since_hour': 22}, 'effects': 'off',
            'platforms': {'x': {'on': True}}, 'map': {}, 'drops': {}, 'chief': {}, 'disk': {}, 'tips': {}}
KEYS = {k: {'kind': 'config', 'target': 'settings', 'meaning': k} for k in (*DEFAULTS, 'posting.hold')}
MINE = '{\n "tz": "America/Los_Angeles",\n "keep_renders_days": 3\n}\n'


def refuse_hold(value):
    if value != 'off':
        raise ValueError(f'posting.hold: {value!r} is not off')


class Home:
    """A temp studio (<tmp>/Studio/data) holding studio.json and the key table, and the `s` that names it."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name, 'Studio', 'data')
        self.data.mkdir(parents=True)
        (self.data / 'studio.json').write_text(MINE)
        (self.data / 'settings-keys.json').write_text(json.dumps({'keys': KEYS}))
        self.s = types.SimpleNamespace(DEFAULTS=DEFAULTS, HOME=self.tmp.name, NEW=True, DATA_DIR=str(self.data),
                                       CONFIG=str(self.data / 'studio.json'), RULES=str(self.data / 'rules.json'),
                                       hold=types.SimpleNamespace(validate=refuse_hold))
        self.history = Path(self.tmp.name, 'Studio', 'run', 'settings-history.jsonl')
        return self

    def __exit__(self, *exc):
        self.tmp.cleanup()

    def run(self, *args):
        """(exit code, stdout): a refusal is a SystemExit with its message, never a traceback."""
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                return settings.main(list(args), self.s), out.getvalue()
        except SystemExit as e:
            return (e.code if isinstance(e.code, int) else 1), out.getvalue() + str(e.code)

    def text(self):
        return (self.data / 'studio.json').read_text()


class InvalidFileRefused(unittest.TestCase):
    """e2e: studio.json: an unknown, mistyped or bad-effects key is refused as `file: key: why` by check."""

    def test_check_names_each_key(self):
        with Home() as h:
            (h.data / 'studio.json').write_text(json.dumps({'bogus': 1, 'tz': 5, 'effects': 'maybe', 'posting.hold': 'on'}))
            code, out = h.run('check')
            self.assertEqual(code, 1)
            for key in ('bogus', 'tz', 'effects', 'posting.hold'):
                self.assertRegex(out, rf'(?m)^data/studio\.json: {key}: \S')


class SetUndoHistory(unittest.TestCase):
    """e2e: studio.json: set then undo returns explain to its prior value and source, the file to its own bytes,
    and history names each change's actor."""

    def test_set_then_undo(self):
        with Home() as h:
            for key, value in (('keep_renders_days', 5), ('tz', 'UTC'), ('overnight', {'since_hour': 21})):
                before = h.run('explain', key)
                self.assertEqual(h.run('set', key, json.dumps(value), '--why', 'test', '--actor', 'tester')[0], 0)
                self.assertNotEqual(h.run('explain', key), before)
                self.assertEqual(h.run('check')[0], 0)
                self.assertEqual(h.run('undo', '--actor', 'undoer')[0], 0)
                self.assertEqual(h.run('explain', key), before)
            self.assertEqual(h.text(), MINE)
            self.assertEqual([line.split()[1] for line in h.run('history')[1].splitlines()], ['tester', 'undoer'] * 3)
            self.assertTrue(h.history.is_file())

    def test_refused_set_writes_nothing(self):
        with Home() as h:
            for args in (('effects', '"maybe"'), ('tz', '5'), ('gates', '{}'), ('bogus', '1'), ('posting.hold', '"on"')):
                self.assertNotEqual(h.run('set', *args, '--why', 'x')[0], 0, args)
            self.assertEqual(h.text(), MINE)
            self.assertEqual(h.run('history')[1].strip(), 'no settings changes yet')


class OneWriterAtATime(unittest.TestCase):
    """defect: Codex review of C1 0b326dc3 (Studio): set and undo rewrote studio.json with no lock and one shared
    .tmp name, so concurrent writers lost changes."""

    def test_set_and_undo_wait_for_the_lock(self):
        with Home() as h:
            for args in (('set', 'tz', '"UTC"', '--why', 'x'), ('undo',)):
                with open(h.data / 'studio.json.lock', 'a') as f:
                    fcntl.flock(f, fcntl.LOCK_EX)
                    before, got = h.text(), []
                    t = threading.Thread(target=lambda: got.append(h.run(*args)[0]))
                    t.start()
                    time.sleep(0.5)
                    self.assertEqual(h.text(), before, args)
                t.join()
                self.assertEqual(got, [0], args)

    def test_another_writers_temp_file_is_left_alone(self):
        with Home() as h:
            (h.data / 'studio.json.tmp').write_text('another writer')
            self.assertEqual(h.run('set', 'tz', '"UTC"', '--why', 'x')[0], 0)
            self.assertEqual((h.data / 'studio.json.tmp').read_text(), 'another writer')
            self.assertEqual(json.loads(h.text())['tz'], 'UTC')


class BrokenFileRefused(unittest.TestCase):
    """defect: Codex review of C1 0b326dc3 (Studio): platforms {"x": null} passed check and set though merge() crashes
    on it; malformed JSON, a non-object root and a repeated key crashed or passed instead of a `file: key: why`."""

    def test_check_and_set_refuse(self):
        cases = (('{"platforms": {"x": null}}', r'data/studio\.json: platforms: x '),
                 ('{"tz": "UTC",', r'data/studio\.json: \(file\): '),
                 ('[1]', r'data/studio\.json: \(file\): '),
                 ('{"tz": "UTC", "tz": "Europe/Paris"}', r'data/studio\.json: \(file\): .*tz'),
                 ('{"chief": {"context_k": 1, "context_k": 2}}', r'data/studio\.json: \(file\): .*context_k'))
        with Home() as h:
            for text, line in cases:
                (h.data / 'studio.json').write_text(text)
                code, out = h.run('check')
                self.assertEqual(code, 1, text)
                self.assertRegex(out, '(?m)^' + line, text)
                if '(file)' in line:        # set refuses only its own key's problems
                    self.assertNotEqual(h.run('set', 'keep_renders_days', '5', '--why', 'x')[0], 0, text)
                    self.assertEqual(h.text(), text)
            (h.data / 'studio.json').write_text('{}')
            self.assertNotEqual(h.run('set', 'platforms', '{"x": null}', '--why', 'x')[0], 0)
            self.assertEqual(h.text(), '{}')


class HistoryBeforeConfig(unittest.TestCase):
    """defect: Codex review of C1 0b326dc3 (Studio): studio.json was replaced before its history line was written,
    so a failed history write left a change nobody could see or undo."""

    def test_unwritable_history_changes_nothing(self):
        with Home() as h:
            h.history.mkdir(parents=True)
            self.assertNotEqual(h.run('set', 'tz', '"UTC"', '--why', 'x')[0], 0)
            self.assertEqual(h.text(), MINE)

    def test_a_record_whose_write_never_happened_is_not_undone(self):
        with Home() as h:
            h.history.parent.mkdir(parents=True)
            h.history.write_text(json.dumps({'at': 'now', 'actor': 'a', 'key': 'tz', 'after': 'never/Written', 'why': 'x'}) + '\n')
            self.assertNotEqual(h.run('undo')[0], 0)
            self.assertEqual(h.text(), MINE)


if __name__ == '__main__':
    unittest.main()
