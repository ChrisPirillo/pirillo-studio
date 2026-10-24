"""Regression outputs for the seam 2d review's sample and rollback defects."""
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ..spine.files import filehash
from . import compositor, decoded, records, request
from .render import render
from . import test_compositor


class SampleTrim(unittest.TestCase):
    """defect: seam 2d review; fractional frame cuts must concatenate exact samples."""

    def test_delayed_audio_uses_source_clock_before_sample_trim(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            raw = struct.pack('<' + 'h' * 12000, *range(12000))
            with wave.open(str(folder / 'source.wav'), 'wb') as pcm:
                pcm.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
                pcm.writeframes(raw)
            source = folder / 'delayed.nut'
            decoded.run('ffmpeg', ['-v', 'error', '-f', 'lavfi', '-i',
                        'color=size=16x16:rate=30:duration=0.25', '-itsoffset', '0.02', '-i',
                        str(folder / 'source.wav'), '-c:v', 'ffv1', '-c:a', 'pcm_s16le',
                        str(source)], 'fixture')
            stream = decoded.probe(source, ['-select_streams', 'a:0', '-show_streams'])['streams'][0]
            self.assertAlmostEqual(float(stream['start_time']), .02)
            req = {'output_id': 'draft', 'settings': {'mix': request.settings()['shared']['mix'], 'audio': {'sample_rate': 48000},
                   'video': {'fps': '30000/1001'}}, 'plan_data': {'spans': [{'id': 's'}]},
                   'frame_map': [{'span': 's', 'source_frames': [1, 3], 'frames': [0, 2],
                                  'asset': {'file': source.name, 'sha256': filehash(source)}}]}
            compositor.voice(SimpleNamespace(folder=folder), req)
            with wave.open(str(folder / 'records/render/draft-voice.wav')) as pcm:
                self.assertEqual(pcm.getnframes(), 3203)
                self.assertEqual(pcm.readframes(3203), raw[1284:7690],
                                 'delayed audio must retain its source clock')

    def test_fractional_frame_cuts_use_output_sample_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / 'source.wav'
            raw = struct.pack('<' + 'h' * 12000, *range(12000))
            with wave.open(str(source), 'wb') as pcm:
                pcm.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
                pcm.writeframes(raw)
            rows = [{'span': str(i), 'source_frames': [a, a + 1], 'frames': [i, i + 1],
                     'src': [a * 1001 / 30000, (a + 1) * 1001 / 30000],
                     'out': [i * 1001 / 30000, (i + 1) * 1001 / 30000],
                     'asset': {'file': 'source.wav', 'sha256': filehash(source)}}
                    for i, a in enumerate([1, 3, 5])]
            req = {'output_id': 'draft', 'settings': {'mix': request.settings()['shared']['mix'], 'audio': {'sample_rate': 48000},
                    'video': {'fps': '30000/1001'}}, 'frame_map': rows,
                    'plan_data': {'spans': [{'id': str(i)} for i in range(3)]}}
            compositor.voice(SimpleNamespace(folder=folder), req)
            with wave.open(str(folder / 'records/render/draft-voice.wav')) as pcm:
                self.assertEqual(pcm.getnframes(), 4805)
                self.assertEqual(pcm.readframes(4805), raw[3204:6408] + raw[9610:12812] + raw[16016:19220])


class RestoreArtifacts(unittest.TestCase):
    """defect: seam 2d review; failed rerender must restore the accepted caption sidecar."""

    @classmethod
    def setUpClass(cls):
        test_compositor.Compositor.setUpClass()

    def test_same_key_missing_srt_restores_video_after_publish_failure(self):
        case = test_compositor.Compositor('test_jcut_decoded_picture_voice_captions_and_reuse')
        case.setUp()
        self.addCleanup(case.doCleanups)
        accepted = case.accepted()
        names = ['Draft/draft.mp4', 'records/render/draft-voice.wav',
                 'records/captions/draft.json', accepted['readings']]
        before = {name: (case.folder / name).read_bytes() for name in names}
        (case.folder / 'Draft/draft.srt').unlink()
        encode, publish = compositor.encode, records.publish
        def regenerate(video, req, target):
            self.assertEqual(req['key'], accepted['key'])
            encode(video, req, target)
            # A same-key encode can change container bytes without changing its decoded media.
            remuxed = target.with_suffix('.remux.mp4')
            try:
                decoded.run('ffmpeg', ['-v', 'error', '-i', str(target), '-map', '0',
                            '-c', 'copy', '-metadata', 'comment=regenerated', str(remuxed)], 'fixture')
                remuxed.replace(target)
            finally:
                remuxed.unlink(missing_ok=True)
        def fail(video, req, temporary):
            publish(video, req, temporary)
            self.assertNotEqual((case.folder / req['file']).read_bytes(), before[req['file']])
            raise RuntimeError('same-key failure after publication')
        with patch.object(compositor, 'encode', side_effect=regenerate), \
                patch.object(records, 'publish', side_effect=fail):
            result = render(case.v, 'draft')
        self.assertEqual(result['state'], 'render_failed', result)
        self.assertIn('same-key failure after publication', result['error'])
        self.assertEqual(case.saved('draft')['state'], 'render_failed')
        self.assertEqual({name: (case.folder / name).read_bytes() for name in names}, before)
        self.assertFalse((case.folder / 'Draft/draft.srt').exists())
        case.accepted()
        self.assertFalse(list(case.folder.rglob('.render-*')))

    def test_failed_rerender_restores_caption_voice_and_video(self):
        case = test_compositor.Compositor('test_jcut_decoded_picture_voice_captions_and_reuse')
        case.setUp()
        self.addCleanup(case.doCleanups)
        accepted = case.accepted()
        names = ['Draft/draft.mp4', 'records/render/draft-voice.wav',
                 'records/captions/draft.json', 'Draft/draft.srt', accepted['readings']]
        before = {name: (case.folder / name).read_bytes() for name in names}
        case.two_spans()
        publish = records.publish
        def fail(video, req, temporary):
            self.assertNotEqual((case.folder / 'Draft/draft.srt').read_bytes(), before['Draft/draft.srt'])
            publish(video, req, temporary)
            raise RuntimeError('injected failure after publication')
        with patch.object(records, 'publish', side_effect=fail):
            result = render(case.v, 'draft')
        self.assertEqual(result['state'], 'render_failed', result)
        self.assertIn('injected failure', result['error'])
        self.assertEqual({name: (case.folder / name).read_bytes() for name in names}, before)
        case.accepted()
        self.assertFalse(list(case.folder.rglob('.render-*')))
