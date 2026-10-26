"""Small real-media paths for native voice-first composition and independent clocks."""
import subprocess
import tempfile
from pathlib import Path
import wave
from types import SimpleNamespace
from unittest.mock import patch

from ..captions import cues
from ..spine.files import SpineError, filehash
from . import compositor, coverage, mapping, records, sound
from .render import Video, render
from .test_rendering import Fixture, PowerLoss


class Compositor(Fixture):
    """e2e: Draft/draft.mp4, prepared voice and caption sidecar on separate picture/voice clocks."""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'source.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=160x90:rate=30',
                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '1',
                '-af', 'volume=0.5', '-c:v', 'libx264', '-crf', '0', '-pix_fmt', 'yuv420p',
                '-colorspace', 'bt709', '-c:a', 'aac', str(path)], check=True, capture_output=True)
            cls.media = path.read_bytes()

    def setUp(self):
        super().setUp()
        self.addCleanup(compositor._RATES.clear)
        compositor._RATES.clear()
        self.cut['cold_open'] = None
        self.settings['shared']['video']['sizes']['wide'] = [160, 90]
        self.settings['shared']['encoder']['crf'] = 0
        self.put('records/faces.json', {'frames': [
            {'frame': i, 'boxes': [[70, 35, 20, 20]]} for i in range(30)]})
        self.truth = {'0:w1': [.1, .3]}
        self.heard = []
        self.captioner = SimpleNamespace(folder=self.folder,
            versions=lambda names: {n: 'fixture-aligner-1' for n in names},
            align=self.align, onsets=lambda voice: [a for a, b in self.truth.values()])
        self.v = Video(self.folder, 'draft', versions={'compositor': 'test'},
                       preflight=self.v.preflight, settings=self.settings, captioner=self.captioner)
        settings = cues.settings()
        settings['wide']['min_cue_s'] = .1
        self.addCleanup(patch.stopall)
        patch.object(cues, 'settings', return_value=settings).start()
        self.save_cut()

    def align(self, voice, words, model):
        self.assertTrue(voice.is_file(), 'voice must exist before alignment')
        self.assertEqual(self.saved('draft')['state'], 'rendering')
        with wave.open(str(voice)) as pcm:
            self.assertGreater(pcm.getnframes(), 0)
        self.heard.append(filehash(voice))
        return {'words': {w['id']: self.truth[w['id']] for w in words}, 'unmatched': []}

    def save_cut(self):
        self.put('records/cut_plan2.json', self.cut)
        self.bind_cut()
        blocks = [{'spans': [s['id'] for s in self.cut['spans']], 'why': 'complete story'}]
        arc = self.get('records/arc_decision.json')
        arc['blocks'] = blocks
        self.put('records/arc_decision.json', arc)
        lock = self.get('records/story_map.json')
        lock.update(blocks=blocks, tail=self.cut['spans'][-1]['id'],
                    arc_decision='records/arc_decision.json#' + filehash(self.folder / 'records/arc_decision.json'))
        self.put('records/story_map.json', lock)

    def two_spans(self, enter='jcut'):
        self.put('records/words.json', {'words': [
            {'id': 'w1', 'text': 'First.', 'start': .05, 'end': .2},
            {'id': 'w2', 'text': 'Next.', 'start': .65, 'end': .8}]})
        self.cut['spans'] = [
            {'id': 's1', 'order': 1, 'src': [0, .41], 'words': ['w1', 'w1'], 'enter': 'open', 'shot': 'med'},
            {'id': 's2', 'order': 2, 'src': [.6, 1], 'words': ['w2', 'w2'], 'enter': enter,
             'lead_s': .1, 'shot': 'close'}]
        self.truth = {'0:w1': [.05, .2], '1:w2': [.45, .6]}
        self.save_cut()

    def accepted(self):
        result = render(self.v, 'draft')
        self.assertEqual(result['state'], 'waiting_owner', {k: result[k] for k in ('state', 'step', 'error') if k in result})
        reading = self.get(result['readings'])
        for name in ('voice', 'captions', 'frame-map', 'cards'):
            self.assertTrue(reading['checks'][name]['ok'], reading['checks'][name])
        self.assertEqual(filehash(self.folder / result['file']), result['file_sha256'])
        self.assertTrue(cues.check(self.captioner, 'draft')['ok'])
        return result

    def test_jcut_decoded_picture_voice_captions_and_reuse(self):
        self.two_spans()
        result = self.accepted()
        self.assertEqual([r['frames'] for r in result['frame_map']], [[0, 12], [12, 24]])
        self.assertEqual([r['frames'] for r in result['picture_map']], [[0, 15], [15, 24]])
        self.assertEqual([r['source_frames'] for r in result['picture_map']], [[0, 15], [21, 30]])
        decoded = self.get(result['readings'])['mapped_frames']
        self.assertEqual([(r['frame'], r['source_frame']) for r in decoded[12:16]],
                         [(12, 12), (13, 13), (14, 14), (15, 21)])
        req = result['request']
        with self.assertRaisesRegex(SpineError, 'split-clock master correspondence unsupported'):
            coverage.draft(self.v, {**req, 'output': 'long'}, self.folder / result['file'], True)
        compared = sound.compare_audio(self.v, req, self.folder / result['file'],
            {'file': req['source'], 'sha256': req['source_sha256']}, req['frame_map'])
        self.assertTrue(compared['ok'], compared)
        self.assertIn('00:00:00,450', (self.folder / 'Draft/draft.srt').read_text())
        self.assertEqual(self.heard, [req['voice_binding']['sha256']])
        self.assertEqual(self.accepted()['file_sha256'], result['file_sha256'])
        self.assertEqual(len(self.heard), 1, 'reuse must not regenerate current voice/cues')
        # A bad final mix cannot pass solely because the prepared voice is correct.
        encode = self.v.encode
        def wrong(req, path):
            encode(req, path)
            changed = path.with_suffix('.changed.mp4')
            try:
                subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-af', 'volume=0.2',
                    '-c:v', 'copy', '-c:a', 'aac', str(changed)], check=True, capture_output=True)
                changed.replace(path)
            finally:
                changed.unlink(missing_ok=True)
        self.v.encode = wrong
        self.v.versions['compositor'] = 'mutated-mix'
        refused = render(self.v, 'draft')
        self.assertEqual((refused['state'], refused['reading']), ('measure_refused', 'voice'), refused)
        self.assertEqual(filehash(self.folder / result['file']), result['file_sha256'])

    def test_hidden_incoming_jcut_never_exposes_voice_frames(self):
        self.two_spans()
        self.cut['spans'][1]['hide_picture'] = True
        self.put('records/overlay_plan.json', {'plan': self.cut['key'], 'cards': [
            {'id': 'hidden', 'kind': 'cover', 'covers': 's2', 'file': 'source.mp4'}]})
        self.save_cut()
        with self.assertRaisesRegex(SpineError, 'J-cut:.*unsupported'):
            render(self.v, 'draft')
        self.assertFalse((self.folder / 'Draft/draft.mp4').exists())

    def test_patch_own_duration_audio_cover_and_held_caption(self):
        self.two_spans('jump')
        patch_audio = self.folder / 'patch.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000',
                        '-t', '0.3', '-af', 'volume=0.5', '-c:a', 'pcm_s16le', str(patch_audio)], check=True, capture_output=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=blue:size=160x90:rate=30',
                        '-frames:v', '1', '-c:v', 'libx264', '-crf', '0', '-colorspace', 'bt709', str(self.folder / 'cover.mp4')],
                       check=True, capture_output=True)
        self.put('records/patches/p.json', {'audio': 'patch.wav', 'duration_s': .3, 'sample_rate': 48000,
            'sample_count': 14400, 'words': [{'id': 'p1', 'text': 'Patch.', 'start': .05, 'end': .25, 'flags': ['held']}]})
        self.cut['spans'][0].update(hide_picture=True, patch={'record': 'records/patches/p.json',
            'sha': filehash(self.folder / 'records/patches/p.json'), 'words': ['p1', 'p1']})
        self.put('records/overlay_plan.json', {'plan': self.cut['key'], 'cards': [
            {'id': 'cover', 'kind': 'cover', 'covers': 's1', 'file': 'cover.mp4'}]})
        self.truth = {'0:p1': [.05, .25], '1:w2': [.35, .5]}
        self.save_cut()
        result = self.accepted()
        self.assertEqual([r['src'] for r in result['frame_map']], [[0, .3], [.6, 1]])
        self.assertEqual([r['out'] for r in result['frame_map']], [[0, .3], [.3, .7]])
        frames = self.get(result['readings'])['mapped_frames']
        self.assertEqual([r['kind'] for r in frames[:9]], ['cover'] * 9)
        self.assertEqual(len(frames), 21)
        with wave.open(str(self.folder / 'records/render/draft-voice.wav')) as actual, wave.open(str(patch_audio)) as own:
            self.assertEqual(actual.readframes(14400), own.readframes(14400))
        with self.assertRaisesRegex(SpineError, 'separate picture_map required'):
            mapping.timeline(result['request'])
        captions = self.get('records/captions/draft.json')
        self.assertEqual(captions['held'], ['p1'])
        self.assertIn('00:00:00,050', (self.folder / captions['sidecar']['file']).read_text())
        self.assertIn('Patch.', (self.folder / captions['sidecar']['file']).read_text())
        original = self.get('records/patches/p.json')
        self.put('records/patches/p.json', {**original, 'duration_s': .4})
        with self.assertRaisesRegex(SpineError, 'stale patch record identity'):
            render(self.v, 'draft')
        self.put('records/patches/p.json', original)
        self.cut['cold_open'] = {'span': 's1', 'words': ['w1', 'w1']}
        self.save_cut()
        with self.assertRaisesRegex(SpineError, 'partial patch'):
            render(self.v, 'draft')

    def test_boundaries_and_crash_retry_never_accept_incomplete_artifacts(self):
        for defect in ('missing', 'nonfinite', 'alias'):
            with self.subTest(defect=defect):
                if defect == 'missing':
                    self.captioner.versions = lambda names: {}
                elif defect == 'nonfinite':
                    # A non-finite runtime setting is refused before writing any output.
                    self.settings['shared']['encoder']['crf'] = float('nan')
                else:
                    path = self.folder / 'records/render/draft-voice.wav'
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.symlink_to(self.folder / 'owner.txt')
                with self.assertRaises(SpineError):
                    render(self.v, 'draft')
                self.captioner.versions = lambda names: {n: 'fixture-aligner-1' for n in names}
                self.settings['shared']['encoder']['crf'] = 0
        path.unlink()
        self.assertEqual((self.folder / 'owner.txt').read_bytes(), b'owner original')
        original = self.accepted()
        self.v.versions['compositor'] = 'crash'
        write = records.write
        def crash(video, name, value):
            if name == 'records/render/draft.json' and value['state'] == 'waiting_owner':
                raise PowerLoss()
            return write(video, name, value)
        with patch.object(records, 'write', side_effect=crash), self.assertRaises(PowerLoss):
            render(self.v, 'draft')
        self.assertEqual(self.saved('draft')['state'], 'measuring')
        recovered = self.accepted()
        self.assertEqual(recovered['frame_map'], original['frame_map'])
        # Corrupt generated bytes force regeneration, never a stale successful cache hit.
        (self.folder / 'records/render/draft-voice.wav').write_bytes(b'bad wave')
        self.accepted()

    def test_missing_coverage_and_caption_failure_refuse_before_publication(self):
        self.captioner.align = lambda *args: {'words': {}, 'unmatched': []}
        refused = render(self.v, 'draft')
        self.assertEqual(refused['state'], 'render_failed', refused)
        self.assertIn('captions', refused['error'])
        self.assertFalse((self.folder / 'Draft/draft.mp4').exists())
        self.captioner.align = self.align
        self.settings['shared']['verification']['render_checks'] = ['bed']
        refused = render(self.v, 'draft')
        self.assertEqual(refused['state'], 'waiting_owner', refused)
        self.assertEqual(refused['request']['bed_binding']['mode'], 'voice_only')
        self.assertTrue(self.get(refused['readings'])['checks']['bed']['ok'])
        self.cut['spans'][0]['speed'] = 2
        self.save_cut()
        with self.assertRaisesRegex(SpineError, 'speed/retime with voice unsupported'):
            render(self.v, 'draft')
