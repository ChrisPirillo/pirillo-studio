"""An accepted render is re-verified before reuse: a refused acceptance clears only on its restored cause."""

from pirillo_studio.rendering.render import render
from pirillo_studio.rendering.test_rendering import Fixture
from pirillo_studio.spine.files import SpineError


class Reuse(Fixture):
    """e2e: records/render/arc.json refused, kept and cleared by its restored bytes."""

    def test_reuse_rechecks_output_bytes(self):
        record = render(self.v, 'arc')
        target = self.folder / record['file']
        accepted = target.read_bytes()
        target.write_bytes(b'tampered owner bytes')
        refused = render(self.v, 'arc')
        self.assertEqual(refused, self.saved('arc'))
        self.assertEqual([refused['state'], refused['step']], ['render_failed', 'verify'])
        self.assertIn('hash', refused['error'])
        stored = (self.folder / 'records/render/arc.json').read_bytes()
        self.assertEqual(render(self.v, 'arc'), refused)  # run twice: one record, bytes untouched
        self.assertEqual((self.folder / 'records/render/arc.json').read_bytes(), stored)
        self.assertEqual(target.read_bytes(), b'tampered owner bytes')
        self.assertEqual(self.encodes, 1)
        self.assertEqual((self.folder / 'source.mp4').read_bytes(), self.media)
        self.assertEqual(refused['refused'], record)  # the refused acceptance is kept, not dropped
        target.write_bytes(accepted)
        self.assertEqual(render(self.v, 'arc')['file_sha256'], record['file_sha256'])  # restored bytes reuse
        self.assertEqual(self.encodes, 1)

    def test_restored_readings_clear_their_refusal(self):
        record = render(self.v, 'arc')
        readings = self.folder / 'records/render/arc-readings.json'
        kept = readings.read_bytes()
        readings.write_bytes(kept + b' ')
        refused = render(self.v, 'arc')
        self.assertEqual([refused['state'], refused['step']], ['render_failed', 'verify'])
        self.assertEqual(refused, self.saved('arc'))
        readings.write_bytes(kept)
        self.assertEqual(render(self.v, 'arc')['file_sha256'], record['file_sha256'])  # the restored cause clears
        self.assertEqual(self.encodes, 1)

    def test_deleted_accepted_output_renders_again(self):
        record = render(self.v, 'arc')
        (self.folder / record['file']).unlink()
        again = render(self.v, 'arc')
        self.assertEqual(again, self.saved('arc'))
        self.assertEqual(again['state'], 'waiting_owner')
        self.assertEqual(self.encodes, 2)
        self.assert_media(again)

    def test_malformed_approval_record_refuses_by_name(self):
        self.v.stage = 'draft'
        render(self.v, 'draft')
        self.v.stage = 'masters'
        self.put('records/draft_decision.json', [])
        with self.assertRaisesRegex(SpineError, 'render long'):
            render(self.v, 'long')
        self.assertFalse((self.folder / 'YouTube/main.mp4').exists())
