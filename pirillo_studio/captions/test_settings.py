"""Every captions.settings value reaches what cues writes: each case runs once on settings.json and once with that
value changed, and the written record or burn layer must move to the changed value, so a default literal standing
in for a setting fails here (review-r4 3, #1794)."""
import copy
import unittest
from unittest.mock import patch

from pirillo_studio.captions import cues
from pirillo_studio.captions.test_captions import TEXTS, Fixture, Video

LINE = {"start_s": 0, "end_s": 1e3, "what": "card"}  # an element active the whole output


def has(reading, setting):
    return lambda r: (reading, setting) in {(x["reading"], x["setting"]) for x in r["refusals"]}


class SettingsReachTheOutput(Fixture):
    """guard: a captions.settings value hard-coded at its default in place of reading the setting."""

    def run_with(self, output, change, before=lambda: None, after=lambda: None):
        s = cues.settings()
        change(s)
        self.texts, self.elements, self.v.onset_list = copy.deepcopy(TEXTS), [], None
        before()
        self.build(output)
        after()
        with patch.object(cues, "settings", lambda: copy.deepcopy(s)):
            record = cues.cues(self.v, output)
        self.assertEqual(record, self.get(f"records/captions/{output}.json"))
        return record

    def moves(self, output, group, key, value, observe, before=lambda: None, after=lambda: None):
        default = observe(self.run_with(output, lambda s: None, before, after))
        changed = observe(self.run_with(output, lambda s: s[group].update({key: value}), before, after))
        self.assertNotEqual(default, changed, f"{group}.{key} did not reach {output}")
        return changed

    def test_style_and_aligner_reach_the_burn_layer(self):
        style = {"font": "Inter", "size": 80, "outline": 5, "shadow": 2, "primary": "&H00EEEEEE",
                 "secondary": "&H00DDDDDD", "outline_colour": "&H00111111", "back": "&H40000000",
                 "highlight": "&H0000FF00&"}
        self.v.models.update({"Inter": "sha256:" + "e" * 64, "OTHER_FA": "sha256:" + "d" * 64})
        models = []
        self.v.align = lambda voice, words, model: (models.append(model), Video.align(self.v, voice, words, model))[1]

        def change(s):
            s["tall"].update(style)
            s["safe_tall"].update(left=60, right=70, top=200, bottom=600)
            s["frame"].update(width=1000, height=1800)
            s["aligner"]["model"] = "OTHER_FA"
        record = self.run_with("vertical", change)
        self.assertEqual(record["state"], "captioned", record["refusals"])
        self.assertEqual((record["aligner"], models), ({"model": "OTHER_FA", "version": "sha256:" + "d" * 64},
                                                        ["OTHER_FA"]))
        self.assertEqual(self.v.fonts[-1], {k: style[k] for k in ("font", "size", "outline", "shadow")})
        rows = (self.v.folder / "records/captions/vertical.ass").read_text().splitlines()
        names = next(r for r in rows if r.startswith("Format: Name, Fontname"))[8:].split(", ")
        got = {r[7:].split(",")[0]: dict(zip(names, r[7:].split(","))) for r in rows if r.startswith("Style: ")}
        for band, margin in (("bottom", "600"), ("top", "200")):
            self.assertEqual({k: got[band][k] for k in ("Fontname", "Fontsize", "PrimaryColour", "SecondaryColour",
                                                        "OutlineColour", "BackColour", "Outline", "Shadow",
                                                        "MarginL", "MarginR", "MarginV")},
                             {"Fontname": "Inter", "Fontsize": "80", "PrimaryColour": "&H00EEEEEE",
                              "SecondaryColour": "&H00DDDDDD", "OutlineColour": "&H00111111",
                              "BackColour": "&H40000000", "Outline": "5", "Shadow": "2", "MarginL": "60",
                              "MarginR": "70", "MarginV": margin})
        self.assertEqual([r for r in rows if r.startswith("PlayRes")], ["PlayResX: 1000", "PlayResY: 1800"])
        events = [r for r in rows if r.startswith("Dialogue: ")]
        self.assertTrue(events and all("{\\c&H0000FF00&}" in r for r in events))

    def test_geometry_reaches_placement(self):
        """Each band rectangle moves with size, outline, safe_tall and frame; one-pixel elements tell them apart."""
        bottom, top = [0, 1321, 1080, 1], [0, 250, 1080, 1]  # the default bottom band's first row, the top band's
        bands = lambda r: [c["band"] for c in r["cues"]][:1]
        for boxes, group, key, value, expected in (
                ([bottom], "tall", "size", 40, ["bottom"]),
                ([bottom], "tall", "outline", 0, ["bottom"]),
                ([bottom], "safe_tall", "bottom", 600, ["bottom"]),
                ([bottom], "frame", "height", 1800, ["bottom"]),
                ([[0, 1321, 80, 99]], "safe_tall", "left", 70, ["top"]),  # left of the default band, inside a wider
                ([[940, 1321, 10, 99]], "safe_tall", "right", 120, ["top"]),
                ([[940, 1321, 10, 99]], "frame", "width", 1100, ["top"]),
                ([bottom, top], "safe_tall", "top", 251, ["top"]),
                ([], "tall", "bands", ["top", "bottom"], ["top"])):
            before = lambda: self.elements.extend(dict(LINE, box=b) for b in boxes)
            self.assertEqual(self.moves("vertical", group, key, value, bands, before), expected, (group, key))

    def test_limits_reach_grouping_and_refusals(self):
        noop = lambda: None
        overlap = lambda: self.texts["s1"].__setitem__(0, "Here.")
        longer = lambda: self.v.truth["1:w1"].__setitem__(1, self.v.truth["1:w1"][1] + .5)
        late = lambda: setattr(self.v, "onset_list", sorted(a + .3 for a, _ in self.v.truth.values()))
        for output, name in (("vertical", "tall"), ("long", "wide")):
            for key, value, observe, expected, before, after in (
                    ("max_words", 1, lambda r: max(len(c["text"]) for c in r["cues"]), 1, noop, noop),
                    ("pause_s", .1, lambda r: len(r["cues"]), None, noop, noop),
                    ("max_chars", 5, has("max_chars", f"{name}.max_chars"), True, noop, noop),
                    ("max_s", .5, has("max_s", f"{name}.max_s"), True, noop, noop),
                    ("min_cue_s", 5, has("min_cue_s", f"{name}.min_cue_s"), True, noop, noop),
                    ("overlap_tol_s", 1.0, has("overlaps", f"{name}.overlap_tol_s"), False, overlap, longer),
                    ("onset_tol_s", .2, has("max_onset_off_s", f"{name}.onset_tol_s"), True, noop, late)):
                got = self.moves(output, name, key, value, observe, before, after)
                if expected is not None:
                    self.assertEqual(got, expected, (output, key))

if __name__ == "__main__":
    unittest.main()
