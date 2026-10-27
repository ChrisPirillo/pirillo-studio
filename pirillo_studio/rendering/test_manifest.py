"""Final delivery consumes the actual native master publication path."""
import json
import wave
import unittest
from unittest.mock import patch

from ..release import rows
from ..spine.files import SpineError, filehash, hashes
from . import records, test_short_master
from .render import render
from .test_rendering import PowerLoss


class Manifest(unittest.TestCase):
    """e2e: records/final_manifest.json binds rendered masters and their measured evidence."""

    @classmethod
    def setUpClass(cls):
        test_short_master.ShortMaster.setUpClass()

    def setUp(self):
        self.short = test_short_master.ShortMaster()
        self.short.setUp()
        self.addCleanup(self.short.doCleanups)
        self.c = self.short.c
        for output in ('draft', 'long'):
            self.c.settings['profiles'][output]['video'] = {
                **self.c.settings['shared']['video'], 'pix_fmt': 'yuv444p'}
            self.c.settings['profiles'][output]['encoder'] = {
                **self.c.settings['shared']['encoder'], 'engine': 'libx264'}
        self.short.approve(jcut=True)
        self.short.vertical.supply()
        # Each provider output keeps its own raster, as a real provider must.
        (self.c.folder / 'caption.nut').rename(self.c.folder / 'vertical-caption.nut')
        caption = self.c.get('records/captions/vertical.json')
        caption['raster']['file'] = 'vertical-caption.nut'
        self.c.put('records/captions/vertical.json', caption)
        self.short.supply()
        for output in ('long', 'vertical', 'short:one'):
            result = render(self.c.v, output)
            self.assertEqual(result['state'], 'delivered', result.get('error'))
        self.manifest = self.c.folder / 'records/final_manifest.json'

    def finish(self):
        # Import after rendering: the base fails on missing integration, with real artifacts already proved.
        from .delivery import finalize
        return finalize(self.c.v)

    def test_manifest_binds_measured_files_and_existing_readers(self):
        result = self.finish()
        self.assertEqual(json.loads(self.manifest.read_text()), result)
        self.assertEqual([o['id'] for o in rows.outputs([(m['path'], m['sha256']) for m in result['members']])],
                         ['long', 'vertical', 'short-one'])
        self.assertTrue(all(hashes(self.c.v, ['records/final_manifest.json']).values()))
        source = self.short.vertical.long.decode(self.c.folder / 'source.mp4', 'pcm')
        expected = source[:12 * 1600 * 2] + source[18 * 1600 * 2:30 * 1600 * 2]
        for output in result['outputs']:
            record = self.c.get(output['record'])
            reading = self.c.get(output['readings'])
            self.assertEqual(output['key'], record['request']['key'])
            self.assertEqual(output['key'], reading['key'])
            self.assertEqual(filehash(self.c.folder / output['record']), output['record_sha256'])
            self.assertEqual(filehash(self.c.folder / output['readings']), output['readings_sha256'])
            self.assertEqual(filehash(self.c.folder / record['file']), reading['file_sha256'])
            self.assertTrue(reading['checks']['draft-match']['ok'])
            pixels = self.short.vertical.long.decode(self.c.folder / record['file'], 'rgb')
            size = 160 * 90 if output['output'] == 'long' else 50 * 90
            self.assertEqual(len(pixels), 24 * size * 3)
            voice = self.c.folder / f"records/render/{record['request']['output_id']}-voice.wav"
            with wave.open(str(voice)) as pcm:
                samples = pcm.readframes(pcm.getnframes())
            self.assertEqual(samples, expected)
            print(json.dumps({'output': record['file'], 'frames': 24, 'exact_voice_samples': len(samples) // 2,
                'sha256': reading['file_sha256'], 'request': output['key'],
                'correspondence': output['readings_sha256']}), flush=True)
        before = self.manifest.read_bytes(), self.manifest.stat().st_mtime_ns
        self.assertEqual(self.finish(), result)
        self.assertEqual((self.manifest.read_bytes(), self.manifest.stat().st_mtime_ns), before)

    def test_refusals_do_not_publish_a_partial_manifest(self):
        c = self.c
        self.finish()
        before = self.manifest.read_bytes()
        for choice in ('reject', 'redo', 'not this', 'try again'):
            c.approve(decision=choice)
            with self.assertRaisesRegex(SpineError, 'final manifest: long: waiting_owner'):
                self.finish()
            self.assertEqual(self.manifest.read_bytes(), before)
        c.approve()
        self.finish()
        encoder = c.settings['profiles']['long']['encoder']
        c.settings['profiles']['long']['encoder'] = {**encoder, 'crf': float('nan')}
        with self.assertRaisesRegex(SpineError, 'encoder.crf: expected finite number'):
            self.finish()
        c.settings['profiles']['long']['encoder'] = encoder
        record = c.get('records/render/long.json')
        damaged = dict(record)
        del damaged['key']
        c.put('records/render/long.json', damaged)
        with self.assertRaisesRegex(SpineError, 'final manifest:.*key'):
            self.finish()
        c.put('records/render/long.json', record)
        for path, output, reason in (
                ('records/render/long-readings.json', 'long', 'accepted readings hash changed'),
                ('Shorts/one.mp4', 'short-one', 'accepted file hash changed')):
            target = c.folder / path
            raw, accepted = target.read_bytes(), c.get(f'records/render/{output}.json')
            target.write_bytes(raw + b'changed')
            with self.assertRaisesRegex(SpineError, reason):
                self.finish()
            self.assertEqual(self.manifest.read_bytes(), before)
            target.write_bytes(raw)
            c.put(f'records/render/{output}.json', accepted)
        self.manifest.unlink()
        self.manifest.symlink_to(c.folder / 'owner.txt')
        with self.assertRaisesRegex(SpineError, 'final_manifest.json:.*symlink'):
            self.finish()
        self.assertEqual((c.folder / 'owner.txt').read_bytes(), b'owner original')

    def test_interrupted_manifest_publication_retries_the_exact_set(self):
        write = records.write
        for after in (False, True):
            def crash(video, name, value):
                if name == 'records/final_manifest.json':
                    if after:
                        write(video, name, value)
                    raise PowerLoss()
                return write(video, name, value)
            with patch.object(records, 'write', side_effect=crash), self.assertRaises(PowerLoss):
                self.finish()
            if after:
                candidate = self.manifest.read_bytes()
            else:
                self.assertFalse(self.manifest.exists())
            completed = self.finish()
            self.assertEqual(completed, json.loads(self.manifest.read_text()))
            self.assertTrue(all(hashes(self.c.v, ['records/final_manifest.json']).values()))
            if after:
                self.assertEqual(self.manifest.read_bytes(), candidate)
            else:
                self.manifest.unlink()
