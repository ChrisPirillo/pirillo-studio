"""A second studio's request, answer, action and rejection mirror survive reload."""
import copy
import datetime as dt
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from pirillo_studio.board import asks, writes


class AskWritesOutput(unittest.TestCase):
    """e2e: ledger.json and image-rejections.jsonl preserve the complete ask writer lifecycle."""

    def test_request_answer_reask_acted_and_interrupted_mirror(self):
        at = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'ledger.json')
            doc = Path(folder, 'context.md')
            doc.write_text('The source context for the question.\n')
            notes = []
            s = types.SimpleNamespace(DATA_DIR=folder, HOME=folder, ASK_STUDIO='studio', CREW=('editor',),
                ASK_USAGE='ask add|answer|acted', ASK_OPTIONS=3, ASK_ABOUT=100, RETRY_TASKS=('retry',), RETRY_LABEL='Later',
                content_dir=lambda slug: str(Path(folder, slug)), now=lambda: at, iso=lambda t: t.isoformat(),
                tz=lambda cfg: dt.timezone.utc, _when=dt.datetime.fromisoformat, _brief=lambda text, limit: text[:limit],
                _at=lambda value, cfg: value, ask_question_refusal=lambda what: None,
                ask_add_policy=lambda *args: None, dispatch_repairs=lambda cfg: False,
                ask_platform_unlinked=lambda *args: None,
                studio_file=lambda file, **kw: file if Path(file).is_file() else None,
                studio_rel=lambda file: Path(file).name,
                decision_enabled=lambda cfg: False, ask_answer_override=lambda *args: None, ask_images=lambda ask: asks.ask_images(s, ask),
                ask_answer_managed=lambda ask: False, ask_attach_steps=lambda *args: None,
                log=lambda ledger, text: ledger['log'].append(text),
                answers_waiting=lambda ledger: [a for a in ledger['asks'] if a['state'] == 'answered' and not a.get('acted')],
                ask_answer_effects=lambda *args: at.isoformat(),
                ask_answer_done=lambda ledger, ask, *args: notes.append(ask['id']))
            led = dict(videos={}, asks=[], log=[])
            args = ['add', 'studio', '--from', 'editor', '--what', 'Which ending should the final file use?',
                    '--about', 'Final ending', '--blocks', 'the finished file', '--option', 'Keep', '--option', 'Change',
                    '--doc', str(doc)]
            lines, changed, answered = writes.ask_add(s, {}, led, args)
            self.assertTrue(changed)
            self.assertIsNone(answered)
            path.write_text(json.dumps(led))
            led = json.loads(path.read_text())
            ask = led['asks'][0]
            self.assertEqual((ask['state'], ask['from'], ask['docs']), ('open', 'editor', ['context.md']))
            aid = ask['id']
            self.assertIn(aid, lines[0])
            before = path.read_bytes()
            with self.assertRaises(SystemExit):
                writes.ask_acted(s, {}, led, ['acted', aid, 'Keep: a premature completion'])
            self.assertEqual(path.read_bytes(), before)
            with mock.patch.object(s, 'ask_answer_effects', side_effect=OSError('editor effect unavailable')):
                with self.assertRaisesRegex(OSError, 'editor effect unavailable'):
                    asks.ask_answer(s, led, aid, choice='Keep', cfg={})
            self.assertEqual((ask['state'], ask['from']), ('open', 'editor'))
            self.assertNotIn('acted', ask)
            self.assertEqual(led, json.loads(path.read_text()))
            asks.ask_answer(s, led, aid, words='Use the source ending, please', cfg={})
            path.write_text(json.dumps(led))
            led = json.loads(path.read_text())
            ask = led['asks'][0]
            self.assertEqual(ask['answer'], dict(choice=None, words='Use the source ending, please',
                                                at=at.isoformat(), via='chat'))
            old = copy.deepcopy(led)
            with self.assertRaisesRegex(SystemExit, 'picked none of'):
                writes.ask_acted(s, {}, led, ['acted', aid, 'the source ending was handled'])
            self.assertEqual(led, old)
            with self.assertRaisesRegex(SystemExit, 'already answered the same question'):
                writes.ask_add(s, {}, led, args)
            self.assertEqual(led, old)
            lines, changed, answered = writes.ask_acted(s, {}, led, ['acted', aid, 'Keep: rendered the selected source ending'])
            path.write_text(json.dumps(led))
            ask = json.loads(path.read_text())['asks'][0]
            self.assertEqual(ask['acted'], dict(at=at.isoformat(), what='Keep: rendered the selected source ending'))
            self.assertIn('0 answered and waiting', lines[0])
            self.assertEqual(notes, [aid])
            # The canonical ledger owns the event; the interrupted append-only mirror is replayed once.
            event = dict(ask=aid, image='checked.png', why='use the source framing')
            led['image_rejection_events'] = [event]
            path.write_text(json.dumps(led))
            mirror = Path(folder, 'image-rejections.jsonl')
            chat = dict(words='An earlier independent comment')
            mirror.write_text(json.dumps(chat) + '\n{"ask":')
            writes.sync_image_rejections(s, json.loads(path.read_text()))
            writes.sync_image_rejections(s, json.loads(path.read_text()))
            self.assertEqual([json.loads(line) for line in mirror.read_text().splitlines()], [chat, event])
