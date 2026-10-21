"""An answer and its repeated question in a second studio's saved ledger."""
import datetime as dt
import json
import tempfile
import types
import unittest
from pathlib import Path

from pirillo_studio.board import asks


class AnswerOutput(unittest.TestCase):
    """e2e: ledger.json preserves an answer and recognizes the same question after reload."""

    def test_answer_saved_and_reasked_after_reload(self):
        at = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)
        effects, notes = [], []

        def answer_effects(led, ask, picked, words, post, shown, raw_words, via, cfg, images):
            effects.append((ask['id'], picked['label'], words))
            return at.isoformat()

        s = types.SimpleNamespace(HOME='unused-without-images', RETRY_LABEL='Later',
            now=lambda: at, tz=lambda cfg: dt.timezone.utc, _when=dt.datetime.fromisoformat,
            decision_enabled=lambda cfg: False, ask_answer_override=lambda *args: None, ask_images=lambda ask: asks.ask_images(s, ask),
                ask_answer_managed=lambda ask: False, ask_attach_steps=lambda *args: None,
            ask_answer_effects=answer_effects, ask_answer_done=lambda *args: notes.append(args[1]['id']))
        original = dict(id='pick-output', slug='sample', state='open', what='Which output?', about='Output choice',
                        options=[dict(label='Keep'), dict(label='Change')])
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'ledger.json')
            path.write_text(json.dumps(dict(asks=[original])))
            led = json.loads(path.read_text())
            with self.assertRaises(SystemExit):
                asks.ask_answer(s, led, original['id'], choice='unknown', cfg={})
            self.assertEqual(led, json.loads(path.read_text()))
            self.assertEqual(effects, [])
            asks.ask_answer(s, led, original['id'], words='Keep, use the clean ending', via='panel', cfg={})
            path.write_text(json.dumps(led))
            loaded = json.loads(path.read_text())
            answer = loaded['asks'][0]
            self.assertEqual(answer['state'], 'answered')
            self.assertEqual(answer['answer'], dict(choice='Keep', words='use the clean ending',
                                                     at=at.isoformat(), via='panel'))
            repeat, why = asks.reasked_answer(s, loaded, 'sample', {}, 'Choose the final output?', 'output choice.', None)
            self.assertIs(repeat, answer)
            self.assertEqual(why, 'the same question')
            self.assertEqual(effects, [('pick-output', 'Keep', 'use the clean ending')])
            self.assertEqual(notes, ['pick-output'])
