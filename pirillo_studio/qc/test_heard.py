"""QC contracts 14 and 15: QC's own heard and delivered evaluators, judged through the real catalog."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from pirillo_studio.qc import verdict
from pirillo_studio.spine.files import canon, filehash

ALIGNER = {'model': 'aligner-a', 'version': 'sha256:' + 'a' * 64}
WORDS = [('w1', 'hello', 1.0, 1.4), ('w2', 'um', 1.4, 1.8), ('w3', 'world', 1.8, 2.4), ('w4', 'again', 2.4, 2.9),
         ('w5', 'friends', 2.9, 3.4)]
HEARD = [('hello', 0.1, 0.5), ('world', 0.5, 1.1), ('again', 1.1, 1.6), ('friends', 1.6, 2.1)]
MEDIA = {'long': ('YouTube/main.mp4', 'YouTube/main.srt'), 'vertical': ('Vertical/main.mp4', 'Vertical/main.srt'),
         'short-a': ('Shorts/a.mp4', 'Shorts/a.srt'), 'short-b': ('Shorts/b.mp4', 'Shorts/b.srt')}
FRAMES = [{'span': 's1', 'src': [0.9, 1.4], 'out': [0.0, 0.5]}, {'span': 's2', 'src': [1.8, 3.4], 'out': [0.5, 2.1]}]


class Video:
    def __init__(self, folder):
        self.folder = folder
        self.settings = {'qc.heard': {'match_s': 0.5, 'onset_s': 0.1, 'clip_s': 0.1, 'removed_run': 3}}

    def resolve_setting(self, name):
        return json.loads(json.dumps(self.settings[name]))

    def now(self):
        return '2026-01-01T00:00:00Z'


class Delivered(unittest.TestCase):
    """e2e: records/qc/masters.json and records/qc/short.json, heard and delivered rows on the actual files."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.n = 0

    def video(self, stage='masters'):
        self.n += 1
        self.v = Video(self.root / f'video{self.n}')
        self.put('records/job.json', {'video_started': '2026-01-01T00:00:00Z'})
        self.put('records/words.json', {'words': [{'id': i, 'text': t, 'start': a, 'end': b} for i, t, a, b in WORDS]})
        cue = {'id': 'c1', 'words': ['w1', 'w3', 'w4', 'w5'], 'lit': [[0.1, 0.5], [0.5, 1.1], [1.1, 1.6], [1.6, 2.1]]}
        self.outputs = list(MEDIA) if stage == 'masters' else ['short-a']
        for output in self.outputs:
            media, sidecar = MEDIA[output]
            self.raw(media, f'{output} picture'.encode())
            self.raw(sidecar, f'{output} sidecar'.encode())
            self.put(f'records/render/{output}.json', {'output': output, 'file': media, 'state': 'delivered',
                                                       'file_sha256': filehash(self.v.folder / media), 'frame_map': FRAMES})
            self.put(f'records/captions/{output}.json', {'aligner': ALIGNER, 'cues': [cue], 'state': 'captioned',
                                                         'sidecar': {'file': sidecar, 'sha256': filehash(self.v.folder / sidecar)}})
            self.hear(output)
        if stage == 'short':
            self.put('records/words-a.json', {'words': [{'id': i, 'text': t, 'start': a, 'end': b}
                                                        for i, (t, a, b) in zip(['w1', 'w3', 'w4', 'w5'], HEARD)]})
            self.put('records/heard_named.json', {'named': []})
        self.manifest()
        self.plan()
        return self.v

    def put(self, name, value):
        self.raw(name, canon(value))

    def raw(self, name, data):
        path = self.v.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def manifest(self, **paths):
        members = [paths.get(p, p) for pair in MEDIA.values() for p in pair if (self.v.folder / paths.get(p, p)).exists()]
        self.put('records/final_manifest.json', {'members': [
            {'path': p, 'sha256': filehash(self.v.folder / p) if '..' not in p else 'sha256:' + '0' * 64} for p in members]})

    def hear(self, output='long', heard=HEARD, **changes):
        media = MEDIA[output][0]
        record = {'file': media, 'file_sha256': filehash(self.v.folder / media), 'engine': 'asr:other',
                  'model': 'sha256:' + 'b' * 64, 'words': [{'text': t, 'start_s': a, 'end_s': b} for t, a, b in heard]}
        self.put(f'records/hear/{output}.json', dict(record, **changes))

    def plan(self, named=()):
        self.put('records/cut_plan2.json', {
            'spans': [{'id': 's1', 'order': 1, 'src': [0.9, 1.4], 'words': ['w1', 'w1']},
                      {'id': 's2', 'order': 2, 'src': [1.8, 3.4], 'words': ['w3', 'w5']}],
            'removed': [{'src': [1.4, 1.8], 'words': ['w2'], 'why': 'filler'}], 'named': list(named)})

    def row(self, check, stage='masters'):
        verdict.judge(self.v, stage)
        record = json.loads((self.v.folder / f'records/qc/{stage}.json').read_text())
        return next(r for r in record['checks'] if r['id'] == check)

    def assertRow(self, check, result, *names, stage='masters'):
        row = self.row(check, stage)
        self.assertEqual(row['result'], result, row)
        for name in names:
            self.assertIn(name, row['words'])
        return row

    def test_heard_flags_delivered_words(self):
        self.video()
        self.assertRow('heard', 'pass')
        cases = {'clipped': [('hello', 0.1, 0.5), ('world', 0.5, 0.6), *HEARD[2:]],
                 'removed': [*HEARD[:1], ('um', 0.45, 0.5), *HEARD[1:]],
                 'short-b': [*HEARD[:2], ('again', 1.3, 1.6), HEARD[3]],
                 'caption-off': [*HEARD[:2], ('again', 1.3, 1.6), HEARD[3]]}
        for kind, heard in cases.items():
            with self.subTest(kind=kind):
                self.video()
                self.hear(kind if kind == 'short-b' else 'long', heard=heard)
                self.assertRow('heard', 'fail', kind)
        self.video()
        self.hear(heard=[*HEARD, ('um', 2.6, 2.7)])  # a removed word heard far from both its bounds is not the flag
        self.assertRow('heard', 'pass', '4 outputs')
        self.video()
        self.hear(heard=cases['caption-off'])
        self.plan(named=[{'rule': 'heard.caption-off', 'at_src': 2.5, 'why': 'he swallows it; the caption stays'}])
        row = self.assertRow('heard', 'manual-pending', 'he swallows it')
        self.assertEqual(row['judge'], 'checker-renders')
        self.plan(named=[{'rule': 'heard.caption-off', 'at_src': 2.5, 'why': ' '}])
        self.assertRow('heard', 'fail', 'caption-off')
        self.video()
        self.hear(model=ALIGNER['version'], engine=ALIGNER['model'])
        self.assertRow('heard', 'unavailable', ALIGNER['model'], ALIGNER['version'], 'independent engine')
        self.video()
        passed = verdict.judge(self.v, 'masters')
        self.hear(heard=HEARD[::-1])
        self.assertEqual(verdict.check(passed)['state'], 'stale')
        self.video('short')
        (self.v.folder / 'records/cut_plan2.json').unlink()
        self.assertRow('heard', 'pass', stage='short')
        self.hear('short-a', heard=cases['caption-off'])
        self.assertRow('heard', 'fail', 'short-a: w4', stage='short')
        named = {'rule': 'heard.caption-off', 'at_src': 1.3, 'why': 'his own cut'}
        self.put('records/heard_named.json', {'named': [dict(named, output='short-b')]})  # another short's clock
        self.assertRow('heard', 'fail', 'short-a: w4', stage='short')
        self.put('records/heard_named.json', {'named': [dict(named, output='short-a')]})
        self.assertRow('heard', 'manual-pending', 'his own cut', stage='short')
        for members, token in (([], 'lists no members'), ([{'path': 'Shorts/A Clip.mp4', 'sha256': 'sha256:' + '0' * 64}],
                                                           'Shorts/A Clip.mp4: not a Shorts/<id>.mp4'),
                               ([{'path': 'Shorts/a.srt', 'sha256': 'sha256:' + '0' * 64}], 'no output to judge')):
            with self.subTest(members=members):
                self.put('records/final_manifest.json', {'members': members})
                self.assertRow('heard', 'unavailable', token, stage='short')
        self.manifest()
        (self.v.folder / 'records/heard_named.json').unlink()
        self.assertRow('heard', 'unavailable', 'records/heard_named.json', stage='short')

    def test_removed_tape_is_a_run_beside_its_bound(self):
        """e2e: records/qc/masters.json heard row: a removed range is heard again only as a run of its words next to a
        kept word that bounds it on that output's clock."""
        mid = {'spans': [{'id': 's1', 'order': 1, 'src': [0.9, 1.4], 'words': ['w1', 'w1']},
                         {'id': 's2', 'order': 2, 'src': [2.4, 3.4], 'words': ['w4', 'w5']}],
               'removed': [{'src': [1.4, 2.4], 'words': ['w2', 'w3'], 'why': 'false start'}], 'named': []}
        start = {'spans': [mid['spans'][1]], 'named': [],
                 'removed': [{'src': [1.0, 2.4], 'words': ['w1', 'w3'], 'why': 'false start'}]}
        plain = ([{'span': 's1', 'src': [0.9, 1.4], 'out': [0.0, 0.5]}, {'span': 's2', 'src': [2.4, 3.4], 'out': [0.5, 1.5]}],
                 ['w1', 'w4', 'w5'], [('hello', 0.1, 0.5), ('again', 0.5, 1.0), ('friends', 1.0, 1.5)])
        cold = ([{'span': 's2', 'src': [2.4, 2.9], 'out': [0.0, 0.5]}, {'span': 's1', 'src': [0.9, 1.4], 'out': [0.5, 1.0]},
                 {'span': 's2', 'src': [2.4, 3.4], 'out': [1.0, 2.0]}], ['w4', 'w1', 'w4', 'w5'],
                [('again', 0.0, 0.5), ('hello', 0.6, 1.0), ('again', 1.0, 1.5), ('friends', 1.5, 2.0)])
        late = ([{'span': 's2', 'src': [2.4, 3.4], 'out': [0.3, 1.3]}], ['w4', 'w5'], [('again', 0.3, 0.8), ('friends', 0.8, 1.3)])
        tail = ([{'span': 's2', 'src': [2.9, 3.4], 'out': [0.0, 0.5]}], ['w5'], [('friends', 0.0, 0.5)])
        um_world = [('um', 0.45, 0.5), ('world', 0.5, 0.6)]
        for case, plan, layout, heard, result in (
                ('one word of a run of two', mid, plain, [plain[2][0], ('world', 0.5, 0.6), *plain[2][1:]], 'pass'),
                ('beside its bound', mid, plain, [plain[2][0], *um_world, *plain[2][1:]], 'fail'),
                ('far from both bounds', mid, plain, [*plain[2], ('um', 3.0, 3.1), ('world', 3.1, 3.2)], 'pass'),
                ('beside the cold open\'s second copy', mid, cold, [*cold[2], ('um', 2.0, 2.1), ('world', 2.1, 2.2)], 'fail'),
                ('a range with a kept word after it only', start, late,
                 [('hello', 0.0, 0.1), ('um', 0.1, 0.2), *late[2]], 'fail'),
                ('an output with neither bound', mid, tail, [*tail[2], ('um', 0.5, 0.6), ('world', 0.6, 0.7)], 'pass')):
            with self.subTest(case=case):
                self.video()
                self.v.settings['qc.heard']['removed_run'] = 2
                self.put('records/cut_plan2.json', plan)
                for output in self.outputs:  # the case's output is long; the rest carry the run nowhere
                    frames, words, said = layout if output == 'long' else (plain if plan is mid else late)
                    lit = [[a, b] for _, a, b in said]
                    for name, field, value in (('render', 'frame_map', frames),
                                               ('captions', 'cues', [{'id': 'c1', 'words': words, 'lit': lit}])):
                        record = json.loads((self.v.folder / f'records/{name}/{output}.json').read_text())
                        self.put(f'records/{name}/{output}.json', dict(record, **{field: value}))
                    self.hear(output, heard=heard if output == 'long' else said)
                self.assertRow('heard', result, *(['removed: long: removed'] if result == 'fail' else []))

    def test_heard_refuses_a_stale_missing_or_nonfinite_hear_record(self):
        for changes, token in (({'file_sha256': 'sha256:' + 'c' * 64}, 'stale'), ({'file_sha256': None}, 'stale'),
                               ({'file': 'Vertical/main.mp4'}, 'stale')):
            with self.subTest(changes=changes):
                self.video()
                self.hear(**changes)
                self.assertRow('heard', 'unavailable', token)
        self.video()
        (self.v.folder / 'records/hear/short-b.json').unlink()
        self.assertRow('heard', 'unavailable', 'records/hear/short-b.json')
        for name in MEDIA['short-b']:
            (self.v.folder / name).unlink()
        self.manifest()  # shorts are the manifest's
        self.assertRow('heard', 'pass', '3 outputs')
        render = json.loads((self.v.folder / 'records/render/short-a.json').read_text())
        self.put('records/render/short-a.json', dict(render, file='Vertical/main.mp4'))  # another output's file
        self.assertRow('heard', 'unavailable', 'short-a: its render record names Vertical/main.mp4')
        self.put('records/render/short-a.json', render)
        self.assertRow('heard', 'pass', '3 outputs')
        self.video()
        raw = (self.v.folder / 'records/hear/long.json').read_bytes().replace(b'"start_s":0.1', b'"start_s":1e400')
        self.raw('records/hear/long.json', raw)
        self.assertRow('heard', 'unavailable', 'long hear word 0')
        for key in ('onset_s', 'removed_run'):
            self.video()
            self.v.settings['qc.heard'].pop(key)
            self.assertRow('heard', 'unavailable', f'qc.heard {key}')
        self.video()
        cues = json.loads((self.v.folder / 'records/captions/long.json').read_text())
        for empty in ([], [{'id': 'c1', 'words': [], 'lit': []}]):
            with self.subTest(cues=empty):
                self.raw('records/captions/long.json', canon(dict(cues, cues=empty)))
                self.assertRow('heard', 'unavailable', 'records/captions/long.json: no cues to hear')

    def test_delivered_set_is_complete(self):
        self.video()
        self.assertRow('delivered', 'pass')
        self.video()
        (self.v.folder / 'Shorts/a.srt').unlink()
        self.assertRow('delivered', 'fail', 'Shorts/a.srt: missing')
        self.video()
        self.raw('Shorts/c.mp4', b'an unrendered short')
        self.manifest(**{'Shorts/b.mp4': 'Shorts/c.mp4'})
        self.assertRow('delivered', 'unavailable', 'records/render/short-c.json')
        self.video()
        os.rename(self.v.folder / 'YouTube/main.mp4', self.v.folder / 'YouTube/A Title.mp4')
        self.manifest(**{'YouTube/main.mp4': 'YouTube/A Title.mp4'})
        self.assertRow('delivered', 'fail', 'YouTube/A Title.mp4: no render or cue record',
                       'YouTube/main.mp4: render:long is missing from the manifest')
        self.video()
        render = json.loads((self.v.folder / 'records/render/vertical.json').read_text())
        self.put('records/render/vertical.json', dict(render, state='measure_refused'))
        self.assertRow('delivered', 'fail', 'Vertical/main.mp4: its record render:vertical is not passing')
        self.video()
        passed = verdict.judge(self.v, 'masters')
        os.link(self.v.folder / 'Shorts/b.mp4', self.root / f'scratch{self.n}.mp4')
        self.assertEqual(verdict.check(passed)['state'], 'stale')
        self.assertRow('delivered', 'fail', 'Shorts/b.mp4: 2 names share its inode')

    def test_delivered_names_an_alias_or_symlink_member(self):
        self.video()
        (self.root / 'outside.srt').write_bytes(b'vertical sidecar')
        (self.v.folder / 'Vertical/main.srt').unlink()
        (self.v.folder / 'Vertical/main.srt').symlink_to(self.root / 'outside.srt')
        self.assertRow('delivered', 'fail', 'Vertical/main.srt: ', 'symlink')
        self.video()
        self.manifest(**{'Shorts/a.srt': '../outside.srt'})
        self.assertRow('delivered', 'fail', "'../outside.srt'", 'Shorts/a.srt: captions:short-a is missing')


class Version(unittest.TestCase):
    """guard: an evaluator's version covers the core helpers it calls in other modules, so a changed helper restales."""

    def test_heard_version_covers_its_cutting_helpers(self):
        import inspect
        from pirillo_studio.cutting import plan
        from pirillo_studio.qc import evaluators
        source = verdict._source(evaluators.heard).decode()
        for helper in (plan.words, plan.selected, evaluators._said):
            self.assertIn(inspect.getsource(helper), source)


if __name__ == '__main__':
    unittest.main()
