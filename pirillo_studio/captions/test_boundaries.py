"""Captions boundary families on the contract fixture: non-finite numbers, path aliases, stale identity."""
import unittest
from pathlib import Path

from pirillo_studio.captions import cues
from pirillo_studio.captions.test_captions import Fixture, Video
from pirillo_studio.spine.files import SpineError


class Boundaries(Fixture):
    """guard: a non-finite number, a symlinked path or a stale identity never yields a passing record."""

    def test_generated_artifacts_include_written_burn(self):
        """defect: queue 14 seam 2f; recovery omitted the caption record's generated ASS."""
        self.build()
        record = self.made()
        names = cues.artifact_names("vertical")
        self.assertIn(record["burn"]["file"], names)
        self.assertEqual((self.v.folder / record["burn"]["file"]).is_file(), True)

    def test_non_finite_numbers_refuse(self):
        self.build()
        words = (self.v.folder / "records/words.json").read_text().replace('"start":1.0', '"start":NaN', 1)
        (self.v.folder / "records/words.json").write_text(words)
        self.assertRaises(SpineError, cues.cues, self.v, "vertical")
        self.build()
        render = (self.v.folder / "records/render/vertical.json").read_text().replace('"out":[0,', '"out":[Infinity,', 1)
        (self.v.folder / "records/render/vertical.json").write_text(render)
        self.assertRaises(SpineError, cues.cues, self.v, "vertical")
        self.build()
        self.v.truth["1:w1"] = [float("nan"), 3.0]
        self.assertRaises(SpineError, cues.cues, self.v, "vertical")
        for row in ('{"box":[1e999,0,10,10],"end_s":1,"start_s":0,"what":"face"}',
                    '{"box":[0,0,10,10],"end_s":1,"start_s":2,"what":"face"}'):  # an infinite x, a reversed interval
            self.build()
            render = (self.v.folder / "records/render/vertical.json").read_text().replace('"elements":[]',
                                                                                        f'"elements":[{row}]')
            (self.v.folder / "records/render/vertical.json").write_text(render)
            self.assertRaises(SpineError, cues.cues, self.v, "vertical")
        self.build()
        self.v.width = lambda text, font: float("inf")
        self.assertRaises(SpineError, cues.cues, self.v, "vertical")
        self.assertFalse((self.v.folder / "records/captions/vertical.json").exists())

    def test_symlinked_paths_refuse(self):
        self.build()
        voice = self.v.folder / "records/render/vertical-voice.wav"
        real = self.v.folder / "elsewhere.wav"
        voice.rename(real)
        voice.symlink_to(real)
        self.assertRaises(SpineError, cues.cues, self.v, "vertical")
        voice.unlink()
        real.rename(voice)
        record = cues.cues(self.v, "vertical")
        for field, alias in (("burn", "../outside.ass"), ("sidecar", "/tmp/main.srt")):  # a record naming a path out
            self.put("records/captions/vertical.json", dict(record, **{field: dict(record[field], file=alias)}))
            self.assertRaises(SpineError, cues.check, self.v, "vertical")
            self.assertRaises(SpineError, cues.sidecar if field == "sidecar" else cues.cues, self.v, "vertical")
        self.put("records/captions/vertical.json", record)
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.v.folder / "Vertical").symlink_to(outside)
        self.assertRaises(SpineError, cues.sidecar, self.v, "vertical")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertRaises(SpineError, cues.check, self.v, "vertical")

    def test_stale_or_missing_identity(self):
        self.build()
        self.made()
        cues.sidecar(self.v, "vertical")
        self.assertTrue(cues.check(self.v, "vertical")["ok"])
        (self.v.folder / "Vertical/main.srt").unlink()
        self.assertEqual(cues.check(self.v, "vertical")["next_action"], "sidecar")
        cues.sidecar(self.v, "vertical")
        (self.v.folder / "records/captions/vertical.ass").write_text("edited by hand")
        self.assertEqual(cues.check(self.v, "vertical")["state"], "incomplete")
        cues.cues(self.v, "vertical")  # a burn layer that lost its bytes is made again, not reused
        self.assertTrue(cues.check(self.v, "vertical")["ok"])
        self.put("records/render/vertical-voice.wav", b"a changed voice artifact")
        self.assertEqual(cues.check(self.v, "vertical")["state"], "stale")
        self.assertRaises(SpineError, cues.sidecar, self.v, "vertical")
        (self.v.folder / "records/captions/vertical.json").unlink()
        self.assertEqual(cues.check(self.v, "vertical")["state"], "missing")
        self.build()
        self.v.align = lambda voice, words, model: (self.put("records/words.json", {"words": self.rows[:-1]}),
                                                    Video.align(self.v, voice, words, model))[1]
        self.assertRaises(SpineError, cues.cues, self.v, "vertical")  # an input changed between read and write
        self.assertFalse((self.v.folder / "records/captions/vertical.json").exists())

    def test_every_word_stays_inside_its_piece(self):
        """defect: review-r4 1, an earlier word heard past its cut hidden by a cue whose last word ends inside."""
        self.build()
        end = self.v.truth["0:w3"][1] = self.pieces[0]["out"][1] + .5  # "the" of the cold open's "the point."
        record = self.made(state="refused")
        cue = next(c for c in record["cues"] if c["words"][0] == "w3")
        self.assertEqual(([(r["cue"], r["reading"], r["value"][1]) for r in record["refusals"]], record["sidecar"]),
                         ([(cue["id"], "outside_piece", end)], None))
        self.assertLessEqual(cue["end_s"], self.pieces[0]["out"][1])  # its own written end is inside: the hidden case
        self.assertFalse((self.v.folder / "records/captions/vertical.ass").exists())


if __name__ == "__main__":
    unittest.main()
