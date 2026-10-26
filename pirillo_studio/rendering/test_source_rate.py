"""Real decoded frame identities prove CFR conversion independently of render filters."""
import copy
import json
from fractions import Fraction
import subprocess
import unittest
from unittest.mock import patch

from ..spine.files import SpineError, filehash
from . import coverage, decoded, mapping, pixels, request, test_compositor


class SourceRate(unittest.TestCase):
    """e2e: Draft/draft.mp4 keeps source-frame identity across differing CFR clocks."""

    @classmethod
    def setUpClass(cls):
        test_compositor.Compositor.setUpClass()

    def setUp(self):
        self.case = test_compositor.Compositor('test_jcut_decoded_picture_voice_captions_and_reuse')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def source(self, rate, seconds=1, variable=False):
        path = self.case.folder / 'source.mp4'
        # Every source frame has its own gray value; crops cannot erase its identity.
        filters = "geq=lum='32+mod(N*7,180)':cb=128:cr=128"
        if variable:
            filters += ",select='lt(n,15)+gte(n,15)*not(mod(n,2))'"
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
            f'nullsrc=size=160x90:rate={rate}', '-f', 'lavfi', '-i',
            'sine=frequency=440:sample_rate=48000', '-t', str(seconds),
            '-vf', filters, '-af', 'volume=0.5', '-fps_mode', 'vfr' if variable else 'passthrough',
            '-c:v', 'libx264', '-crf', '0', '-pix_fmt', 'yuv420p', *test_compositor.COLOR_TAGS,
            '-c:a', 'aac', str(path)], check=True, capture_output=True)
        self.case.v.preflight['source_sha256'] = filehash(path)
        self.case.cut['spans'][0]['shot'] = 'wide'
        self.case.put('records/faces.json', {'frames': [
            {'frame': i, 'boxes': [[70, 35, 20, 20]]}
            for i in range(int(Fraction(rate) * seconds) + 1)]})
        self.case.save_cut()
        return path

    def rgb(self, path):
        raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:v:0',
            '-an', '-fps_mode', 'passthrough', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'],
            check=True, capture_output=True).stdout
        length = 160 * 90 * 3
        self.assertEqual(len(raw) % length, 0)
        return [raw[i:i + length] for i in range(0, len(raw), length)]

    def converted(self, rate):
        result = self.case.accepted()
        req = result['request']
        self.assertEqual(Fraction(req['source_rate']), Fraction(rate))
        mapping.validate(req)
        indices = []
        for row in req['picture_map']:
            grid = row['source_grid_frames']
            expected = [int(Fraction(n) * Fraction(rate) / 30) for n in range(*grid)]
            self.assertEqual(row['source_indices'], expected)
            self.assertEqual(row['source_frames'], [expected[0], expected[-1] + 1])
            self.assertEqual(row['src'], [n / 30 for n in grid])
            indices.extend(expected)
        measured = self.case.get(result['readings'])['mapped_frames']
        self.assertEqual([row['source_frame'] for row in measured], indices)
        self.assertTrue(all(row['ok'] for row in measured))
        subject = coverage.subjects(req, True)
        self.assertTrue(subject['ok'], subject)
        source = self.rgb(self.case.folder / 'source.mp4')
        output = self.rgb(self.case.folder / result['file'])
        self.assertEqual(len(output), len(indices))
        peak = 0
        for frame, index in zip(output, indices):
            # Plain decoding only: no shared selection, timestamps, crop or fps filters.
            peak = max(peak, max(abs(a - b) for a, b in zip(frame, source[index])))
        self.assertLessEqual(peak, 3)
        wrong = copy.deepcopy(req)
        wrong['picture_map'][0]['source_indices'][0] += 1
        with self.assertRaisesRegex(SpineError, 'source.*frames'):
            mapping.validate(wrong)
        with self.assertRaisesRegex(SpineError, 'source-rate frame indices'):
            pixels.compare(self.case.v, wrong, self.case.folder / result['file'])
        print('SOURCE_RATE_OUTPUT ' + json.dumps({'rate': rate, 'file_sha256': result['file_sha256'],
            'indices': indices, 'independent_rgb_peak_error': peak,
            'pixels': self.case.get(result['readings'])['checks']['frame-map']['value'],
            'subjects': subject['value'], 'voice_seconds': [r['src'] for r in req['frame_map']]}), flush=True)
        return req

    def test_sixty_to_thirty_keeps_every_other_source_frame(self):
        self.source('60')
        req = self.converted('60')
        self.assertEqual(req['picture_map'][0]['source_indices'], list(range(0, 60, 2)))

    def test_fractional_rate_uses_absolute_source_offset(self):
        self.source('30000/1001', seconds=2)
        self.case.cut['spans'][0]['src'] = [1, 2]
        self.case.put('records/words.json', {'words': [
            {'id': 'w1', 'text': 'Ready.', 'start': 1.1, 'end': 1.3}]})
        self.case.save_cut()
        req = self.converted('30000/1001')
        self.assertEqual(req['picture_map'][0]['source_indices'], list(range(29, 59)))

    def test_twenty_four_to_thirty_repeats_source_frames(self):
        self.source('24')
        req = self.converted('24')
        self.assertEqual(req['picture_map'][0]['source_indices'][:6], [0, 0, 1, 2, 3, 4])

    def test_sixty_push_and_jcut_use_picture_clock(self):
        self.source('60')
        self.case.two_spans()
        self.case.cut['spans'][0].update(enter='push', shot='wide', push={'to': 'close', 's': .2})
        self.case.settings['shared']['video']['shot_fractions'] = {'med': .8, 'close': .5}
        self.case.save_cut()
        req = self.converted('60')
        indices = [i for r in req['picture_map'] for i in r['source_indices']]
        self.assertEqual(indices, list(range(0, 30, 2)) + list(range(42, 60, 2)))
        self.assertEqual([r['src'] for r in req['frame_map']], [[0, .4], [.6, 1]])
        self.assertEqual([r['frames'] for r in req['frame_map']], [[0, 12], [12, 24]])

    def test_variable_frame_rate_is_refused_by_name(self):
        self.source('30', variable=True)
        with self.assertRaisesRegex(SpineError, 'variable-frame-rate'):
            request.resolve(self.case.v, 'draft', 'draft')

    def altered_probe(self, change):
        real = decoded.probe
        def probe(path, entries):
            raw = copy.deepcopy(real(path, entries))
            if (str(path).endswith('source.mp4') and '-show_frames' in entries
                    and any('r_frame_rate' in entry for entry in entries)):
                change(raw)
            return raw
        return patch.object(decoded, 'probe', side_effect=probe)

    def test_coarse_time_base_cannot_hide_a_missing_frame(self):
        self.source('30')
        def change(raw, drop=True):
            raw['streams'][0]['time_base'] = '1/30'
            for index, frame in enumerate(raw['frames']):
                frame.update(pts=index, duration=1)
            if drop:
                del raw['frames'][2]
        with self.altered_probe(change), self.assertRaisesRegex(
                SpineError, 'variable-frame-rate source decoded clock'):
            request.resolve(self.case.v, 'draft', 'draft')
        with self.altered_probe(lambda raw: change(raw, drop=False)):  # a time base of one frame stamps exactly
            request.resolve(self.case.v, 'draft', 'draft')

    def test_source_clock_probe_is_reused_but_refusals_are_not(self):
        self.source('60')
        calls = []
        def change(raw):
            calls.append(raw)
            if len(calls) == 1:
                raw['streams'][0]['avg_frame_rate'] = 'NaN'
        with self.altered_probe(change):
            with self.assertRaisesRegex(SpineError, 'source rate'):
                request.resolve(self.case.v, 'draft', 'draft')
            first = request.resolve(self.case.v, 'draft', 'draft')
            second = request.resolve(self.case.v, 'draft', 'draft')
        self.assertEqual(first['source_rate'], second['source_rate'])
        self.assertEqual(len(calls), 2, 'successful source clock was decoded again')

    def test_missing_or_nonfinite_declared_source_rate_refuses(self):
        self.source('60')
        for value in (None, 'NaN', '1/0'):
            with self.subTest(value=value):
                def change(raw):
                    video = next(s for s in raw['streams'] if s['codec_type'] == 'video')
                    if value is None:
                        video.pop('avg_frame_rate', None)
                    else:
                        video['avg_frame_rate'] = value
                with self.altered_probe(change), self.assertRaisesRegex(SpineError, 'source rate'):
                    request.resolve(self.case.v, 'draft', 'draft')

    def test_declared_rate_disagrees_with_decoded_source_clock(self):
        self.source('60')
        def change(raw):
            frames = [r for r in raw['frames'] if r['media_type'] == 'video']
            for frame in frames:
                frame['pts'] *= 2
                frame['duration'] *= 2
        with self.altered_probe(change), self.assertRaisesRegex(SpineError, 'decoded source clock'):
            request.resolve(self.case.v, 'draft', 'draft')


class Retiming(unittest.TestCase):
    """e2e: Draft/draft.mp4 samples approved expression speeds and preserves following speech."""

    @classmethod
    def setUpClass(cls):
        test_compositor.Compositor.setUpClass()
    setUp = SourceRate.setUp
    source = SourceRate.source
    rgb = SourceRate.rgb

    def expression(self, speed=2, rate='60', field='speed'):
        self.source(rate)
        self.case.put('records/words.json', {'words': [
            {'id': 'w1', 'text': 'First.', 'start': .05, 'end': .2},
            {'id': 'w2', 'text': 'Next.', 'start': .75, 'end': .9}]})
        self.case.cut['spans'] = [
            {'id': 's1', 'order': 1, 'src': [0, .3], 'words': ['w1', 'w1'], 'enter': 'open', 'shot': 'wide'},
            {'id': 's2', 'order': 2, 'src': [.3, .7], 'words': [], 'enter': 'pop', 'shot': 'wide',
             'expression': {'after_word': 'w1', 'seen': 'looks aside'}, field: speed},
            {'id': 's3', 'order': 3, 'src': [.7, 1], 'words': ['w2', 'w2'], 'enter': 'jump', 'shot': 'wide'}]
        count = round(Fraction(12) / Fraction(speed))
        offset = Fraction(9 + count, 30) - Fraction(7, 10)
        self.case.truth = {'0:w1': [.05, .2], '2:w2': [float(Fraction(3, 4) + offset),
                                                       float(Fraction(9, 10) + offset)]}
        self.case.save_cut()
        return count

    def delivered(self, speed, rate, field='speed'):
        import wave
        count = self.expression(speed, rate, field)
        result = self.case.accepted()
        req = result['request']
        mapping.validate(req)
        indices = ([int(Fraction(n) * Fraction(rate) / 30) for n in range(9)]
                   + [int((9 + n * Fraction(speed)) * Fraction(rate) / 30) for n in range(count)]
                   + [int(Fraction(n) * Fraction(rate) / 30) for n in range(21, 30)])
        self.assertEqual([i for row in req['picture_map'] for i in row['source_indices']], indices)
        self.assertEqual(req['picture_map'][1]['src'], [.3, .7])
        measured = self.case.get(result['readings'])
        self.assertEqual([r['source_frame'] for r in measured['mapped_frames']], indices)
        self.assertTrue(all(r['ok'] for r in measured['mapped_frames']))
        self.assertTrue(coverage.subjects(req, True)['ok'])
        source, output = self.rgb(self.case.folder / 'source.mp4'), self.rgb(self.case.folder / result['file'])
        self.assertEqual(len(output), 18 + count)
        peak = max(max(abs(a - b) for a, b in zip(frame, source[index])) for frame, index in zip(output, indices))
        self.assertLessEqual(peak, 3)
        with wave.open(str(self.case.folder / 'records/render/draft-voice.wav')) as voice:
            self.assertEqual(voice.getnframes(), (18 + count) * 1600)
            voice.setpos(9 * 1600)
            self.assertEqual(set(voice.readframes(count * 1600)), {0})
        captions = self.case.get('records/captions/draft.json')
        self.assertAlmostEqual(captions['cues'][-1]['start_s'], self.case.truth['2:w2'][0], delta=.005)
        for defect in ('index', 'grid', 'silence'):
            wrong = copy.deepcopy(req)
            if defect == 'index':
                wrong['picture_map'][1]['source_indices'][0] += 1
            elif defect == 'grid':
                wrong['picture_map'][1].pop('source_grid_frames')
            else:
                wrong['frame_map'][1].pop('silent')
            with self.assertRaises(SpineError):
                mapping.validate(wrong)
        wrong = copy.deepcopy(req)
        wrong['picture_map'][1]['source_indices'][0] += 1
        with self.assertRaisesRegex(SpineError, 'source-rate frame indices'):
            pixels.compare(self.case.v, wrong, self.case.folder / result['file'])
        print('RETIME_OUTPUT ' + json.dumps({'speed': speed, 'rate': rate, 'indices': indices,
            'file_sha256': result['file_sha256'], 'independent_rgb_peak_error': peak,
            'silent_samples': count * 1600, 'following_caption_s': captions['cues'][-1]['start_s']}), flush=True)
        return result

    def test_fast_expression_decoded_frames_and_silent_voice(self):
        self.delivered(2, '60')

    def test_slow_expression_at_fractional_source_rate(self):
        self.delivered('1/2', '30000/1001')

    def test_rational_alias_rounds_duration_on_fixed_grid(self):
        self.delivered('7/3', '60', 'retime')

    def test_invalid_factors_voice_and_unsupported_combinations_refuse_by_name(self):
        from . import clocks
        self.expression()
        req = request.resolve(self.case.v, 'draft', 'draft')
        for factor in (0, -1, float('nan'), float('inf'), 1.5, True, None, 'NaN', '1/0', 'sqrt(2)', {}, []):
            with self.subTest(factor=factor):
                wrong = copy.deepcopy(req)
                wrong['plan_data']['spans'][1]['speed'] = factor
                with self.assertRaisesRegex(SpineError, 's2.*speed.*positive rational'):
                    clocks.split(wrong, mapping.pieces(wrong))
        for defect, message in [('voice', 'speed.*voice'), ('overlap', 'speed.*voice'),
                                ('alias', 'speed.*retime'), ('grade', 'grade.*unsupported'),
                                ('jcut', 'J-cut.*retim'), ('collapse', 'retim.*collapses')]:
            with self.subTest(defect=defect):
                wrong = copy.deepcopy(req)
                spans = wrong['plan_data']['spans']
                if defect == 'voice':
                    spans[0]['speed'] = 2
                elif defect == 'overlap':
                    wrong['words_data'][0]['end'] = .4
                elif defect == 'alias':
                    spans[1]['retime'] = 2
                elif defect == 'grade':
                    spans[1]['grade'] = 'warm'
                elif defect == 'jcut':
                    spans[2].update(enter='jcut', lead_s=.1)
                else:
                    spans[1]['speed'] = 100
                with self.assertRaisesRegex(SpineError, message):
                    clocks.split(wrong, mapping.pieces(wrong))
        with self.assertRaisesRegex(SpineError, 'separate picture_map'):
            mapping.timeline(req)

    def test_retime_identity_alias_and_crash_recovery(self):
        from . import records
        from .render import render
        from .test_rendering import PowerLoss
        self.expression()
        request.resolve(self.case.v, 'draft', 'draft')
        target = self.case.folder / 'records/render/draft-voice.wav'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(self.case.folder / 'owner.txt')
        with self.assertRaises(SpineError):
            render(self.case.v, 'draft')
        self.assertEqual((self.case.folder / 'owner.txt').read_bytes(), b'owner original')
        target.unlink()
        first = self.case.accepted()
        self.case.cut['spans'][1]['speed'] = 3
        self.case.put('records/cut_plan2.json', self.case.cut)
        with self.assertRaisesRegex(SpineError, 'stale cut check'):
            render(self.case.v, 'draft')
        self.case.save_cut()
        self.case.truth['2:w2'] = [float(Fraction(29, 60)), float(Fraction(19, 30))]
        write = records.write
        def crash(video, name, value):
            if name == 'records/render/draft.json' and value['state'] == 'waiting_owner':
                raise PowerLoss()
            return write(video, name, value)
        with patch.object(records, 'write', side_effect=crash), self.assertRaises(PowerLoss):
            render(self.case.v, 'draft')
        recovered = self.case.accepted()
        self.assertNotEqual(first['key'], recovered['key'])
        self.assertEqual(recovered['request']['picture_map'][1]['source_indices'], [18, 24, 30, 36])
        self.assertEqual(len(self.rgb(self.case.folder / recovered['file'])), 22)
        self.assertEqual(self.case.accepted()['key'], recovered['key'])


class ColorConversion(unittest.TestCase):
    """e2e: Draft/draft.mp4 converts decoded BT.601/range with independent RGB proof."""

    setUpClass = classmethod(lambda cls: test_compositor.Compositor.setUpClass())
    setUp = SourceRate.setUp

    def source(self, standard='smpte170m', level='tv', transfer=None):
        path = self.case.folder / 'source.mp4'
        transfer = standard if transfer is None else transfer
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'testsrc=size=160x90:rate=30', '-f', 'lavfi', '-i',
            'sine=frequency=440:sample_rate=48000', '-t', '1', '-af', 'volume=0.5',
            '-vf', 'format=yuv444p,lutyuv=y=val*0.5+64:u=val*0.5+64:v=val*0.5+64',
            '-c:v', 'libx264', '-crf', '0', '-pix_fmt', 'yuv444p',
            '-x264-params', f'colorprim={standard}:transfer={transfer}:colormatrix={standard}',
            '-color_range', level, '-c:a', 'aac', str(path)], check=True, capture_output=True)
        self.case.v.preflight['source_sha256'] = filehash(path)
        self.case.cut['spans'][0]['shot'] = 'wide'
        self.case.save_cut()
        return path

    def planes(self, path):
        return subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:v:0',
            '-an', '-c:v', 'rawvideo', '-fps_mode', 'passthrough', '-f', 'rawvideo', 'pipe:1'],
            check=True, capture_output=True).stdout

    @staticmethod
    def rgb(y, u, v, matrix, level):
        y = (y - 16) / 219 if level == 'tv' else y / 255
        u, v = [(c - 128) / (224 if level == 'tv' else 255) for c in (u, v)]
        kr, kb = (.2126, .0722) if matrix == 'bt709' else (.299, .114)
        return [y + 2*(1-kr)*v, y - 2*kb*(1-kb)/(1-kr-kb)*u - 2*kr*(1-kr)/(1-kr-kb)*v,
                y + 2*(1-kb)*u]

    def delivered(self, standard, level, output='tv'):
        path = self.source(standard, level)
        self.case.settings['shared']['video'].update(color_range=output,
            pix_fmt='yuv444p' if output == 'tv' else 'yuvj444p')
        result = self.case.accepted()
        req = result['request']
        source, target = self.planes(path), self.planes(self.case.folder / result['file'])
        indices = [i for row in req['picture_map'] for i in row['source_indices']]
        self.assertEqual(len(target), len(indices)*160*90*3)
        matrices = {'bt709': ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
            'smpte170m': ((.939542064, .050181357, .010276579), (.017772223, .965792862, .016434914),
                         (-.0016216, -.00436975, 1.00599135)),
            'bt470bg': ((1.044043209, -.044043209, 0), (0, 1, 0), (0, .011793378, .988206622))}
        peak = 0
        for frame, index in enumerate(indices):
            for x in (12, 32, 52, 72, 92, 112, 132, 152):
                p = 45*160+x
                values = [source[index*43200 + c*14400+p] for c in range(3)]
                rgb = self.rgb(*values, standard, level)
                linear = [v**2.8 if standard == 'bt470bg' else
                          v/4.5 if v < .081 else ((v+.099)/1.099)**(1/.45) for v in rgb]
                linear = [sum(a*b for a, b in zip(row, linear)) for row in matrices[standard]]
                expected = [4.5*v if v < .018 else 1.099*v**.45-.099 for v in linear]
                actual = self.rgb(*(target[frame*43200 + c*14400+p] for c in range(3)), 'bt709', output)
                peak = max(peak, *(abs(a-b)*255 for a, b in zip(actual, expected)))
        self.assertLessEqual(peak, 3)
        reading = self.case.get(result['readings'])
        self.assertTrue(reading['checks']['color']['ok'], reading['checks']['color'])
        self.assertEqual(reading['checks']['color']['coverage']['frames'], len(indices))
        print('COLOR_OUTPUT ' + json.dumps({'input': standard, 'range': level, 'output_range': output,
            'frames': len(indices), 'independent_rgb_peak_error': peak, 'sha256': result['file_sha256']}), flush=True)
        return result

    def test_bt601_and_ranges_have_independent_decoded_rgb_proof(self):
        for standard in ('smpte170m', 'bt470bg', 'bt709'):
            for level in ('tv', 'pc'):
                with self.subTest(standard=standard, level=level):
                    self.delivered(standard, level)
        self.delivered('smpte170m', 'tv', 'pc')

    def test_ambiguous_decoded_colors_and_hdr_refuse_by_name(self):
        self.source()
        probe = decoded.probe
        for defect, message in [('missing', 'color_primaries'), ('unknown', 'color_transfer'),
                                ('nonfinite', 'color_space'), ('mixed', 'mixed.*color'),
                                ('declared', 'declared.*color'), ('pq', 'HDR.*tone'), ('hlg', 'HDR.*tone')]:
            with self.subTest(defect=defect):
                def altered(path, args):
                    raw = probe(path, args)
                    if '-show_frames' in args and raw.get('frames'):
                        frames = [r for r in raw['frames'] if r.get('media_type', 'video') == 'video']
                        if defect == 'missing':
                            frames[0].pop('color_primaries', None)
                        elif defect == 'unknown':
                            frames[0]['color_transfer'] = 'unknown'
                        elif defect == 'nonfinite':
                            frames[0]['color_space'] = float('nan')
                        elif defect == 'mixed':
                            frames[-1]['color_range'] = 'pc'
                        elif defect == 'declared':
                            raw['streams'][0]['color_range'] = 'pc'
                        else:
                            frames[0]['color_transfer'] = 'smpte2084' if defect == 'pq' else 'arib-std-b67'
                    return raw
                with patch.object(decoded, 'probe', side_effect=altered), self.assertRaisesRegex(SpineError, message):
                    request.resolve(self.case.v, 'draft', 'draft')

    def test_color_binding_cache_identity_and_recovery(self):
        from . import records
        from .render import render
        from .test_rendering import PowerLoss
        path = self.source()
        probe, calls = decoded.probe, []
        def counted(path, args):
            if '-show_frames' in args and any('color_primaries' in arg for arg in args):
                calls.append(str(path))
                if len(calls) == 1:
                    raw = probe(path, args)
                    raw['frames'][0].pop('color_range')
                    return raw
            return probe(path, args)
        with patch.object(decoded, 'probe', side_effect=counted):
            with self.assertRaisesRegex(SpineError, 'color_range'):
                request.resolve(self.case.v, 'draft', 'draft')
            first = request.resolve(self.case.v, 'draft', 'draft')
            request.resolve(self.case.v, 'draft', 'draft')
        self.assertEqual(len(calls), 2)
        self.assertEqual(first['picture_map'][0]['color_conversion']['input']['color_space'], 'smpte170m')
        result = self.case.accepted()
        wrong = copy.deepcopy(result['request'])
        wrong['picture_map'][0].pop('color_conversion')
        with self.assertRaisesRegex(SpineError, 'color.*binding'):
            pixels.compare(self.case.v, wrong, self.case.folder / result['file'])
        self.source('bt470bg')
        with self.assertRaisesRegex(SpineError, 'stale|unbound'):
            pixels.compare(self.case.v, result['request'], self.case.folder / result['file'])
        write = records.write
        def crash(video, name, value):
            if name == 'records/render/draft.json' and value['state'] == 'waiting_owner':
                raise PowerLoss()
            return write(video, name, value)
        with patch.object(records, 'write', side_effect=crash), self.assertRaises(PowerLoss):
            render(self.case.v, 'draft')
        recovered = self.case.accepted()
        self.assertNotEqual(result['key'], recovered['key'])
        alias = self.case.folder / 'alias.mp4'
        alias.symlink_to(path)
        wrong = copy.deepcopy(recovered['request'])
        wrong['picture_map'][0]['asset']['file'] = 'alias.mp4'
        with self.assertRaises(SpineError):
            pixels.compare(self.case.v, wrong, self.case.folder / recovered['file'])
