"""Actual encoded files exercise preparation, decoded coverage and committed render records together."""
import copy
import subprocess
from unittest.mock import patch

from ..captions import cues
from ..spine.files import SpineError, digest, filehash
from . import coverage, records, request
from .render import Video, render
from .test_rendering import Fixture, PowerLoss
from .test_compositor import COLOR_TAGS


class RendererCoverage(Fixture):
    """e2e: Draft/draft.mp4 and its frame-map/coverage record, with lavfi source and real encoding."""

    @classmethod
    def setUpClass(cls):
        from .test_compositor import Compositor
        Compositor.setUpClass.__func__(cls)

    def setUp(self):
        super().setUp()
        self.cut['cold_open'] = None
        self.put('records/cut_plan2.json', self.cut)
        self.bind_cut()
        self.settings['shared']['video']['sizes'] = {'wide': [160, 90], 'tall': [50, 90]}
        self.settings['shared']['verification'] = {**request.settings()['shared']['verification'],
                                                    'checks': ['decode'], 'render_checks': ['frame-map', 'cards']}
        self.v = Video(self.folder, 'draft', versions={'encoder': 'fixture-lossless'},
                       preflight=self.v.preflight, encode=self.encode_map,
                       settings=self.settings)
        self.v.prepare = self.prepare

    def prepare(self, req):
        from . import color, mapping
        rows = mapping.timeline(req)
        for row in rows:
            row.update(color_conversion=color.binding(color.inspect(self.folder / req['source']), req['settings']['video']),
                       kind='source', asset={'file': req['source'], 'sha256': req['source_sha256']},
                       crop=[55, 0, 50, 90] if req['settings']['shape'] == 'tall' else [0, 0, 160, 90])
        return {'frame_map': rows, 'elements': []}

    def encode_map(self, req, path):
        self.encodes += 1
        graph, joins = [], []
        width, height = req['settings']['video']['sizes'][req['settings']['shape']]
        for i, row in enumerate(req['frame_map']):
            a, b = row['source_frames']
            x, y, w, h = row['crop']
            graph += [f'[0:v]trim=start_frame={a}:end_frame={b},setpts=PTS-STARTPTS,'
                      f'crop={w}:{h}:{x}:{y}:exact=1,scale={width}:{height}:flags=bilinear[v{i}]',
                      f"[0:a]atrim=start={row['src'][0]}:end={row['src'][1]},asetpts=PTS-STARTPTS[a{i}]"]
            joins.append(f'[v{i}][a{i}]')
        graph.append(''.join(joins) + f'concat=n={len(joins)}:v=1:a=1[v][a]')
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(self.folder / req['source']),
                        '-filter_complex', ';'.join(graph), '-map', '[v]', '-map', '[a]',
                        '-c:v', 'libx264', '-crf', '0', '-pix_fmt', 'yuv420p', *COLOR_TAGS, '-c:a', 'aac', str(path)],
                       check=True, capture_output=True)

    def accepted(self, output='draft'):
        result = render(self.v, output)
        self.assertIn(result['state'], ('waiting_owner', 'delivered'), result)
        return result

    def test_locked_plan_is_the_viewed_draft(self):
        self.cut['cold_open'] = {'span': 's1', 'words': ['w1', 'w1']}
        self.cut['end_hold_s'] = .5  # Already materialized in the settled final src interval [0,1].
        self.put('records/cut_plan2.json', self.cut)
        self.bind_cut()
        result = self.accepted()
        self.assertEqual([r['source_frames'] for r in result['frame_map']], [[3, 15], [0, 30]])
        self.assertEqual([r['frames'] for r in result['frame_map']], [[0, 12], [12, 42]])
        self.assertEqual(result['mode'], 'render')
        self.assertEqual(result['elements'], [])
        reading = self.get(result['readings'])
        self.assertEqual(len(reading['mapped_frames']), 42)
        self.assertTrue(reading['checks']['frame-map']['ok'])
        self.assertEqual(cues._pieces(result, 'render', result['duration_s']), [
            {k: r[k] for k in ('span', 'src', 'out')} for r in result['frame_map']])
        self.v.versions['encoder'] = 'wrong-order'
        encode = self.encode_map
        def reverse(req, path):
            wrong = copy.deepcopy(req)
            wrong['frame_map'].reverse()
            encode(wrong, path)
        self.v.encode = reverse
        refused = render(self.v, 'draft')
        self.assertEqual((refused['state'], refused['reading']), ('measure_refused', 'frame-map'))

    def test_vertical_keeps_subject_every_frame(self):
        self.put('records/faces.json', {'frames': [{'frame': n, 'boxes': [[70, 10, 10, 20]]} for n in range(30)]})
        self.settings['profiles']['vertical']['render_checks'] = ['subject']
        self.accepted()
        self.approve()
        self.v.stage = 'masters'
        result = self.accepted('vertical')
        self.assertTrue(self.get(result['readings'])['checks']['subject']['ok'])
        original = self.get('records/faces.json')
        for problem in ('unseen', 'outside'):
            rows = copy.deepcopy(original['frames'])
            if problem == 'unseen':
                rows.pop(15)
            else:
                rows[15]['boxes'] = [[100, 10, 10, 20]]
            self.put('records/faces.json', {'frames': rows})
            self.v.stage = 'draft'
            self.accepted()
            self.approve()
            self.v.stage = 'masters'
            result = render(self.v, 'vertical')
            self.assertEqual((result['state'], result['reading']), ('measure_refused', 'subject'), result)
        self.put('records/faces.json', original)

    def test_masters_match_approved_draft(self):
        self.accepted()
        self.approve()
        self.v.stage = 'masters'
        result = self.accepted('long')
        self.assertTrue(self.get(result['readings'])['checks']['draft-match']['ok'])
        self.v.versions['encoder'] = 'changed-sound'
        self.v.stage = 'draft'
        self.accepted()
        self.approve()
        self.v.stage = 'masters'
        encode = self.encode_map
        def changed(req, path):
            encode(req, path)
            altered = path.with_suffix('.altered.mp4')
            try:
                subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-af', 'volume=0.2',
                                '-c:v', 'copy', '-c:a', 'aac', str(altered)], check=True, capture_output=True)
                altered.replace(path)
            finally:
                altered.unlink(missing_ok=True)
        self.v.encode = changed
        refused = render(self.v, 'long')
        self.assertEqual((refused['state'], refused['reading']), ('measure_refused', 'draft-match'), refused)

    def test_masters_reuse_verified_draft_picture(self):
        self.accepted()
        self.approve()
        self.v.stage = 'masters'
        for output in ('vertical', 'short:one'):
            with self.subTest(output=output):
                result = self.accepted(output)
                req = request.resolve(self.v, 'masters', output)
                with patch.object(coverage.pixels, 'compare', wraps=coverage.pixels.compare) as compare:
                    check = coverage.draft(self.v, req, self.folder / result['file'], True)
                self.assertTrue(check['ok'])
                self.assertEqual(compare.call_count, 0, 'verified draft must not be decoded again')
                record = self.saved('draft')
                evidence = self.get(record['readings'])
                for bad in (None, {'ok': False, 'value': {}}, {'ok': 'yes', 'value': {}}):
                    altered = copy.deepcopy(evidence)
                    if bad is None:
                        del altered['checks']['frame-map']
                    else:
                        altered['checks']['frame-map'] = bad
                    self.put(record['readings'], altered)
                    record['readings_sha256'] = filehash(self.folder / record['readings'])
                    self.put('records/render/draft.json', record)
                    with self.assertRaisesRegex(SpineError, 'draft-match.*frame-map'):
                        coverage.draft(self.v, req, self.folder / result['file'], True)
                self.put(record['readings'], evidence)
                record['readings_sha256'] = filehash(self.folder / record['readings'])
                self.put('records/render/draft.json', record)

    def test_each_short_has_its_own_complete_ends(self):
        self.settings['profiles']['short']['render_checks'] = ['subject', 'context', 'word-ends']
        self.put('records/faces.json', {'frames': [{'frame': n, 'boxes': [[70, 10, 10, 20]]} for n in range(30)]})
        words = self.get('records/words.json')
        words['words'][0]['start'] = .3
        self.put('records/words.json', words)
        shorts = self.get('records/shorts_spec.json')
        shorts['shorts'].append({**shorts['shorts'][1], 'id': 'two', 'pieces': [[.2, .7]]})
        self.put('records/shorts_spec.json', shorts)
        self.bind_cut()
        self.accepted()
        self.approve()
        self.v.stage = 'masters'
        result = self.accepted('short:one')
        ends = self.get(result['readings'])['checks']['word-ends']['value']
        self.assertEqual([r['frames'] for r in ends['words']], [[9, 15], [9, 15]])
        self.assertTrue(ends['audio']['complete'])
        second = self.accepted('short:two')
        other = self.get(second['readings'])['checks']['word-ends']['value']
        self.assertEqual([r['frames'] for r in other['words']], [[3, 9], [3, 9]])
        req = request.resolve(self.v, 'masters', 'short:one')
        req['selected']['ends_on'] = 'missing-word'
        from .render import MeasurementRefused
        with self.assertRaisesRegex(MeasurementRefused, 'missing-word'):
            self.v.measure(req, self.folder / result['file'])

    def captioned(self):
        """Use item 18's real sidecar writer, with its synthetic alignment adapter on extracted audio."""
        from ..captions.test_captions import Video as CaptionVideo
        result = self.accepted()
        voice = self.folder / 'records/render/draft-voice.wav'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(self.folder / result['file']),
                        '-vn', '-c:a', 'pcm_s16le', str(voice)], check=True, capture_output=True)
        adapter = CaptionVideo(self.folder)
        adapter.output, adapter.truth = 'draft', {'0:w1': [.1, .5]}
        caption = cues.cues(adapter, 'draft')
        self.assertEqual(caption['state'], 'captioned', caption)
        cues.sidecar(adapter, 'draft')
        prepare = self.prepare
        def bound(req):
            return {**prepare(req), 'caption_binding': {'record': 'records/captions/draft.json',
                                                       'sha256': filehash(self.folder / 'records/captions/draft.json')}}
        self.v.prepare = bound
        self.settings['shared']['verification']['render_checks'].append('captions')
        return adapter

    def test_delivered_files_meet_named_profiles(self):
        adapter = self.captioned()
        result = self.accepted()
        check = self.get(result['readings'])['checks']['captions']
        self.assertTrue(check['ok'])
        self.assertEqual(check['value']['sidecar_sha256'], filehash(self.folder / 'Draft/draft.srt'))
        self.assertEqual(cues.check(adapter, 'draft')['state'], 'captioned')
        self.assertEqual(render(self.v, 'draft'), result)

    def test_evidence_and_layers_follow_output_bytes(self):
        self.captioned()
        result = self.accepted()
        from .render import MeasurementRefused
        original = self.get('records/captions/draft.json')
        for field, value in [('frame_map_sha256', digest(b'stale-clock')),
                             ('words_sha256', digest(b'stale-words')),
                             ('burn', {'file': 'caption.ass', 'sha256': digest(b'unknown glyphs')})]:
            caption = {**original, field: value}
            if field == 'burn':
                (self.folder / 'caption.ass').write_bytes(b'unknown glyphs')
            self.put('records/captions/draft.json', caption)
            req = request.resolve(self.v, 'draft', 'draft')
            with self.assertRaises(MeasurementRefused) as refused:
                from .render import measured
                measured(self.v, req, result['file'])
            self.assertEqual(refused.exception.reading, 'captions')
        self.put('records/captions/draft.json', original)
        req = request.resolve(self.v, 'draft', 'draft')
        (self.folder / 'Draft/draft.srt').write_text('wrong sidecar')
        with self.assertRaisesRegex(MeasurementRefused, 'Draft/draft.srt'):
            self.v.measure(req, self.folder / result['file'])

    def test_hidden_span_requires_its_resolved_opaque_cover(self):
        cover = self.folder / 'cover.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                        'color=blue:size=160x90:rate=30', '-frames:v', '1', '-c:v', 'libx264', '-crf', '0',
                        *COLOR_TAGS, str(cover)], check=True, capture_output=True)
        self.cut['spans'][0]['hide_picture'] = True
        self.put('records/cut_plan2.json', self.cut)
        self.put('records/overlay_plan.json', {'plan': self.cut['key'], 'cards': [
            {'id': 'cover-1', 'kind': 'cover', 'covers': 's1', 'text': 'Ready.'}]})
        self.bind_cut()
        def prepare(req):
            bound = self.prepare(req)
            bound['frame_map'][0].update(kind='cover', card='cover-1', source_frames=[0, 1],
                                         asset={'file': 'cover.mp4', 'sha256': filehash(cover)})
            return bound
        self.v.prepare = prepare
        exposed = False
        def encode(req, path):
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-stream_loop', '-1', '-i', str(cover),
                            '-i', str(self.folder / 'source.mp4'), '-map', '0:v', '-map', '1:a', '-t', '1',
                            '-vf', "drawbox=x=0:y=0:w=2:h=90:color=white:t=fill:enable='eq(n,0)'" if exposed else 'null',
                            '-c:v', 'libx264', '-crf', '0', '-c:a', 'aac', str(path)], check=True, capture_output=True)
        self.v.encode = encode
        result = self.accepted()
        self.assertEqual(self.get(result['readings'])['checks']['cards']['value']['frames'], 30)
        self.assertEqual(result['frame_map'][0]['asset']['sha256'], filehash(cover))
        exposed = True
        self.v.versions['encoder'] = 'exposed-edge'
        refused = render(self.v, 'draft')
        self.assertEqual(refused['state'], 'measure_refused', refused)
        self.assertEqual(refused['reading'], 'frame-map')

    def test_missing_identity_alias_nonfinite_and_publish_crash_refuse(self):
        prepare = self.prepare
        for defect in ('missing', 'identity', 'nonfinite', 'alias'):
            def invalid(req):
                bound = prepare(req)
                row = bound['frame_map'][0]
                if defect == 'missing':
                    del bound['frame_map']
                elif defect == 'identity':
                    row['asset']['sha256'] = digest(b'stale')
                elif defect == 'nonfinite':
                    row['crop'][0] = float('nan')
                else:
                    (self.folder / 'alias.mp4').symlink_to(self.folder / 'source.mp4')
                    row['asset']['file'] = 'alias.mp4'
                return bound
            self.v.prepare = invalid
            with self.assertRaises(SpineError):
                render(self.v, 'draft')
        self.v.prepare = prepare
        original = self.accepted()
        self.v.versions['encoder'] = 'crash'
        write = records.write
        def crash(video, name, value):
            if name == 'records/render/draft.json' and value['state'] == 'waiting_owner':
                raise PowerLoss()
            return write(video, name, value)
        with patch.object(records, 'write', side_effect=crash), self.assertRaises(PowerLoss):
            render(self.v, 'draft')
        self.assertEqual(self.saved('draft')['state'], 'measuring')
        result = self.accepted()
        self.assertEqual(result['frame_map'], original['frame_map'])
        self.assertEqual(filehash(self.folder / result['file']), result['file_sha256'])

    def test_dependency_change_after_decode_refuses(self):
        from . import coverage
        from .render import MeasurementRefused
        result = self.accepted()
        req = request.resolve(self.v, 'draft', 'draft')
        compare = coverage.pixels.compare
        def mutate(*args):
            value = compare(*args)
            self.put('records/faces.json', {'frames': [], 'revision': 'changed during decode'})
            return value
        with patch.object(coverage.pixels, 'compare', side_effect=mutate), self.assertRaises(MeasurementRefused) as caught:
            self.v.measure(req, self.folder / result['file'])
        self.assertEqual(caught.exception.reading, 'inputs.records/faces.json')
