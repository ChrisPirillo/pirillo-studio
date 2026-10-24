"""Captions section 5 contracts 1 to 11 inspect the written record and files; 12 (test_switch_off_is_unchanged)
is the host switch's, seam 3, as cutting's and the spine's off tests are."""
import copy
import json
import math
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pirillo_studio.captions import cues
from pirillo_studio.spine.files import SpineError, canon, filehash

MODEL, FONT = "sha256:" + "a" * 64, "sha256:" + "b" * 64
TEXTS = {"s1": ["Here", "is", "the", "point."], "s2": ["Old", "words."], "s3": ["Cuts", "land", "clean."]}
BASE = {"s1": 1.0, "s2": 10.0, "s3": 20.0}
DELAY = 0.05  # the delivered voice runs this late against the transcript's mapped times


class Video:
    def __init__(self, folder):
        self.folder, self.models, self.truth, self.unmatched = folder, {"MMS_FA": MODEL, "Poppins": FONT}, {}, []
        self.asked, self.fonts, self.onset_list, self.broken, self.output, self.heard = None, [], None, None, None, []

    def voice(self, path):
        """The adapter is only ever handed this output's own voice artifact."""
        assert path == self.folder / f"records/render/{self.output}-voice.wav", path
        self.heard.append(path)

    def versions(self, names):
        missing = [n for n in names if n not in self.models]
        if missing:
            raise LookupError(missing[0])
        return {n: self.models[n] for n in names}

    def align(self, voice, words, model):
        if self.broken == "align":
            raise LookupError("aligner runtime")
        self.voice(voice)
        self.asked = copy.deepcopy(words)
        return {"words": {w["id"]: self.truth[w["id"]] for w in words if w["id"] in self.truth},
                "unmatched": self.unmatched}

    def onsets(self, voice):
        self.voice(voice)
        return sorted(a for a, _ in self.truth.values()) if self.onset_list is None else self.onset_list

    def width(self, text, font):
        self.fonts.append(font)
        return 30 * len(text)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.v = Video(Path(self.tmp.name) / "video")
        self.v.folder.mkdir()
        self.texts, self.step, self.flags, self.elements = copy.deepcopy(TEXTS), {"s1": 1, "s2": 1, "s3": 1}, {}, []

    def put(self, name, value):
        path = self.v.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else canon(value))

    def get(self, name):
        return json.loads((self.v.folder / name).read_text())

    def build(self, output="vertical"):
        """Words, plan, patch, render record and voice for one output; the aligner's truth from its frame map."""
        rows, spans, n = [], [], 0
        for sid, texts in self.texts.items():
            base, step, first = BASE[sid], self.step[sid], n + 1
            for k, text in enumerate(texts):
                n += 1
                rows.append({"id": f"w{n}", "text": text, "start": round(base + k * step, 3),
                             "end": round(base + k * step + .8 * step, 3), "flags": self.flags.get(f"w{n}", [])})
            spans.append({"id": sid, "order": len(spans) + 1, "src": [base - .1, base + len(texts) * step],
                          "words": [f"w{first}", f"w{n}"], "enter": {"s1": "open", "s2": "jump", "s3": "jcut"}[sid],
                          "shot": {"s1": "med", "s2": "close", "s3": "wide"}[sid]})
        spans[2]["lead_s"] = .2
        self.rows = rows
        self.put("records/words.json", {"words": rows})
        self.patch = [{"id": "p1", "text": "New", "start": .1, "end": .9}, {"id": "p2", "text": "words.", "start": 1.0, "end": 1.7}]
        self.put("records/patches/p1.json", {"words": self.patch})
        spans[1].update(hide_picture=True, patch={"record": "records/patches/p1.json",
                        "sha": filehash(self.v.folder / "records/patches/p1.json"), "words": ["p1", "p2"]})
        s1 = [w for w in rows if w["id"] in ("w3", "w4")]
        self.plan = {"video": "records/job.json", "plan_version": "1", "key": "sha256:plan", "spans": spans,
                     "removed": [], "named": [], "cold_open": {"span": "s1", "words": ["w3", "w4"]}, "end_hold_s": 1.0}
        self.put("records/cut_plan2.json", self.plan)
        srcs = [("s1", [s1[0]["start"] - .1, spans[0]["src"][1]])] + [(s["id"], s["src"]) for s in spans]
        self.pieces, at = [], 0
        for sid, (a, b) in srcs:
            self.pieces.append({"span": sid, "src": [a, b], "out": [at, round(at + b - a, 6)]})
            at = round(at + b - a, 6)
        self.duration = at
        self.kept = []
        for i, piece in enumerate(self.pieces):
            span = next(s for s in spans if s["id"] == piece["span"])
            words = self.patch if "patch" in span else [
                w for w in rows if w["id"] in span["words"] or (
                    int(span["words"][0][1:]) <= int(w["id"][1:]) <= int(span["words"][1][1:]))]
            for w in words:
                if "patch" in span or piece["src"][0] <= (w["start"] + w["end"]) / 2 < piece["src"][1]:
                    self.kept.append((i, w["id"]))
                    shift = piece["out"][0] + (DELAY if "patch" in span else DELAY - piece["src"][0])
                    self.v.truth[f"{i}:{w['id']}"] = [round(w["start"] + shift, 4), round(w["end"] + shift, 4)]
        self.put(f"records/render/{output}.json", {"mode": "render", "duration_s": self.duration,
                                                    "frame_map": self.pieces, "elements": self.elements})
        self.put(f"records/render/{output}-voice.wav", f"voice of {output}".encode())
        self.put("records/job.json", {"voice_over": []})
        self.v.output, self.v.heard = output, []
        return output

    def made(self, output="vertical", state="captioned"):
        record = cues.cues(self.v, output)
        self.assertEqual(record["state"], state, record.get("refusals"))
        self.assertEqual(record, self.get(f"records/captions/{output}.json"))
        return record

    def srt(self, path):
        blocks = (self.v.folder / path).read_text().strip().split("\n\n")
        out = []
        for block in blocks:
            _, times, *lines = block.split("\n")
            a, b = (sum(float(x) * m for x, m in zip(t.replace(",", ".").split(":"), (3600, 60, 1)))
                    for t in times.split(" --> "))
            out.append((round(a, 3), round(b, 3), "\n".join(lines)))
        return out

    def ass(self, path):
        merged = []
        for row in (self.v.folder / path).read_text().splitlines():
            if row.startswith("Dialogue: "):
                _, a, b, band, _, _, _, _, _, text = row[10:].split(",", 9)
                a, b = (sum(float(x) * m for x, m in zip(t.split(":"), (3600, 60, 1))) for t in (a, b))
                text = re.sub(r"\{[^}]*\}", "", text)
                if merged and merged[-1][2] == text and abs(merged[-1][1] - a) < 1e-6:
                    merged[-1] = (merged[-1][0], round(b, 3), text)
                else:
                    merged.append((round(a, 3), round(b, 3), text))
        return merged


class Captions(Fixture):
    """e2e: records/captions/<output>.json, records/captions/<output>.ass and the output's .srt sidecar."""

    def test_every_kept_word_is_captioned_once(self):
        self.build()
        record = self.made()
        shown = [(c["piece"], w) for c in record["cues"] for w in c["words"]]
        self.assertEqual(shown, self.kept)
        self.assertEqual(len(set(shown)), len(shown))
        self.assertIn((0, "w3"), shown)
        self.assertIn((1, "w3"), shown)  # the cold open repeats w3 and w4 as their own occurrences
        self.assertNotIn("w5", {w for _, w in shown})  # the patch replaced the tape's words
        self.flags = {"w2": ["unsaid"]}
        self.build()
        record = cues.cues(self.v, "vertical")
        self.assertEqual((record["state"], record["cues"]), ("refused", None))
        self.assertEqual(record["refusals"][0]["route"], "item 17")
        self.assertEqual(record["uncaptioned"], [{"word": "w2", "reason": "unsaid"}])
        self.flags = {}
        self.build()
        self.v.unmatched = [[3.0, 3.4]]  # a kept phrase the transcript lost (#147)
        record = cues.cues(self.v, "vertical")
        self.assertEqual(record["state"], "refused")
        self.assertEqual([(r["reading"], r["value"]["span"], r["route"]) for r in record["refusals"]],
                         [("unmatched_speech", "s1", "item 17")])

    def test_cues_follow_the_delivered_sound(self):
        self.flags = {"w8": ["held"]}
        self.build()
        self.v.truth["3:w8"] = [self.v.truth["3:w8"][0] + .3, self.v.truth["3:w8"][1] + .3]
        record = self.made()
        self.assertEqual(self.v.heard, [self.v.folder / "records/render/vertical-voice.wav"] * 2)  # align, onsets
        tol = cues.settings()["tall"]["onset_tol_s"]
        for cue in record["cues"]:
            first = self.v.truth[f"{cue['piece']}:{cue['words'][0]}"][0]
            self.assertLessEqual(abs(cue["start_s"] - first), tol)
            self.assertEqual(cue["start_s"], round(first, 2))
        self.assertEqual(record["held"], ["w8"])
        lit = {w: span for c in record["cues"] if c["piece"] == 3 for w, span in zip(c["words"], c["lit"])}
        piece = self.pieces[3]
        self.assertEqual(lit["w8"][0], round(piece["out"][0] + 21.0 - piece["src"][0], 2))  # its own time, mapped
        patched = [c for c in record["cues"] if c["piece"] == 2]
        self.assertEqual(patched[0]["start_s"], round(self.pieces[2]["out"][0] + .1 + DELAY, 2))
        self.assertEqual([c["words"] for c in patched], [["p1", "p2"]])

    def test_cues_never_overlap_or_shrink(self):
        self.build()
        record = self.made()
        minimum, tol = cues.settings()["tall"]["min_cue_s"], cues.settings()["tall"]["overlap_tol_s"]
        for a, b in zip(record["cues"], record["cues"][1:]):
            self.assertLessEqual(a["end_s"] - b["start_s"], tol)
        self.assertTrue(all(c["end_s"] - c["start_s"] >= minimum - 1e-9 for c in record["cues"]))
        self.texts["s3"], self.step["s3"] = ["Cuts.", "land", "clean."], .5
        self.build()
        record = cues.cues(self.v, "vertical")
        self.assertEqual(record["state"], "refused")
        short = [r for r in record["refusals"] if r["reading"] == "min_cue_s"]
        self.assertEqual(len(short), 1)
        cue = next(c for c in record["cues"] if c["id"] == short[0]["cue"])
        self.assertEqual(round(cue["end_s"] - cue["start_s"], 2), short[0]["value"])  # never shortened or raised
        self.assertLess(short[0]["value"], minimum)
        self.texts, self.step["s3"] = copy.deepcopy(TEXTS), 1
        self.texts["s1"][0] = "Here."
        self.build()
        self.v.truth["1:w1"][1] += .5
        record = cues.cues(self.v, "vertical")
        self.assertEqual([r["reading"] for r in record["refusals"]], ["overlaps"])
        self.assertEqual(record["readings"]["overlaps"], 1)

    def test_lines_fit_inside_the_safe_zone(self):
        self.build()
        self.elements[:] = [{"start_s": self.pieces[3]["out"][0], "end_s": self.duration,
                             "box": [100, 1300, 400, 200], "what": "face"}]
        self.build()
        record = self.made()
        self.assertEqual({c["piece"]: c["band"] for c in record["cues"]}, {0: "bottom", 1: "bottom", 2: "bottom", 3: "top"})
        safe = cues.settings()["safe_tall"]
        self.assertTrue(all(c["width_px"] <= 1080 - safe["left"] - safe["right"] for c in record["cues"]))
        self.assertEqual(self.v.fonts[0], {"font": "Poppins", "size": 92, "outline": 7, "shadow": 3})
        self.assertEqual(record["readings"]["outside_safe"], 0)
        self.elements.append({"start_s": self.pieces[2]["out"][0], "end_s": self.pieces[2]["out"][1],
                              "box": [0, 0, 1080, 1920], "what": "card"})
        self.build()
        record = cues.cues(self.v, "vertical")
        self.assertEqual([(r["reading"], r["value"]) for r in record["refusals"]],
                         [("band", {"piece": 2, "span": "s2"})])
        self.elements.clear()
        self.texts["s3"] = ["Indistinguishable."]  # within max_chars, wider than the safe zone
        self.build()
        self.v.width = lambda text, font: 60 * len(text)
        record = cues.cues(self.v, "vertical")
        self.assertEqual([(r["reading"], r["setting"]) for r in record["refusals"]], [("outside_safe", "safe_tall")])
        self.assertEqual(record["readings"]["outside_safe"], 1)

    def test_sidecar_and_burn_are_the_same_cues(self):
        self.build()
        record = self.made()
        self.assertEqual(cues.sidecar(self.v, "vertical"), "Vertical/main.srt")
        srt, burn = self.srt("Vertical/main.srt"), self.ass("records/captions/vertical.ass")
        self.assertEqual(srt, burn)
        self.assertEqual(srt, [(c["start_s"], c["end_s"], c["lines"][0]) for c in record["cues"]])
        for name, output in (("tall", "vertical"), ("wide", "long")):
            self.build(output)
            record = self.made(output)
            limits = cues.settings()[name]
            for cue in record["cues"]:
                piece = self.pieces[cue["piece"]]
                self.assertLessEqual(len(cue["lines"][0]), limits["max_chars"])
                self.assertLessEqual(len(cue["text"]), limits["max_words"])
                self.assertLessEqual(cue["end_s"], min(piece["out"][1], self.duration))
            self.assertEqual([c["breaks_at"] for c in record["cues"] if c["breaks_at"]],
                             [p["out"][1] for p in self.pieces])
        self.assertIsNone(record["burn"])
        cues.sidecar(self.v, "long")
        self.assertEqual(self.srt("YouTube/main.srt"), [(c["start_s"], c["end_s"], c["lines"][0]) for c in record["cues"]])
        self.assertFalse((self.v.folder / "records/captions/long.ass").exists())
        self.texts["s3"], self.step["s3"] = ["W" * 42 + "."], 9  # one unsplittable 43-character word said over 7.2 s
        self.build("long")
        record = cues.cues(self.v, "long")
        self.assertEqual(([(r["reading"], r["setting"]) for r in record["refusals"]], record["sidecar"]),
                         ([("max_chars", "wide.max_chars"), ("max_s", "wide.max_s")], None))
        self.texts, self.step["s3"] = copy.deepcopy(TEXTS), 1
        for occurrence, piece, past in (("1:w4", 1, .01), ("3:w9", 3, .01), ("1:w4", 1, .004)):
            self.build("long")  # past a cut, past the file's end, and past a cut by less than the written precision
            self.v.truth[occurrence][1] = self.pieces[piece]["out"][1] + past
            record = cues.cues(self.v, "long")
            self.assertEqual([r["reading"] for r in record["refusals"]], ["outside_piece"])
            self.assertEqual(record["refusals"][0]["value"][1], self.v.truth[occurrence][1])  # the heard edge

    def test_words_read_as_said(self):
        self.texts["s3"] = ["cross", "-platform", "costs", "$5,795.", "and", "$2", ",000", "at", "example", ".com",
                            "Y", "'all", "R", "&D", "100", "%", "5", "'5\""]
        self.step["s3"] = .5
        self.build("long")
        record = self.made("long")
        shown = [t for c in record["cues"] for t in c["text"]]
        for joined in ("cross-platform", "$5,795.", "$2,000", "example.com", "Y'all", "R&D", "100%", "5'5\""):
            self.assertIn(joined, shown)
        joined = next(c for c in record["cues"] if "cross-platform" in c["text"])
        self.assertEqual(joined["words"][:2], ["w7", "w8"])
        said = {w["text"]: w["said"] for w in self.v.asked}
        self.assertEqual(said["$5,795."], "five thousand seven hundred ninety five dollars")
        self.assertEqual(said["100%"], "one hundred percent")
        self.assertEqual(said["$2,000"], "two thousand dollars")  # joined before it is read, never "zero zero zero"
        self.assertEqual(next(w["id"] for w in self.v.asked if w["text"] == "$2,000"), "3:w12")
        self.assertFalse({",000", "%", "-platform"} & set(said))
        self.assertEqual(said["Y'all"], "Y'all")
        number = next(c for c in record["cues"] if "$5,795." in c["text"])
        at = number["text"].index("$5,795.")
        self.assertEqual(number["lit"][at][0], round(self.v.truth["3:w10"][0], 2))  # timed where the voice says it
        self.assertEqual(" ".join(shown), " ".join(t for t in self.texts["s1"][2:] + self.texts["s1"] + ["New", "words."]
                                                   ) + " cross-platform costs $5,795. and $2,000 at example.com "
                                                   "Y'all R&D 100% 5'5\"")
        self.texts["s3"], self.flags = ["costs", "cross", "-platform."], {"w8": ["held"]}  # held first part only
        self.build("long")
        piece, mapped = self.pieces[3], lambda t: round(self.pieces[3]["out"][0] + t - self.pieces[3]["src"][0], 2)
        joined = next(c for c in self.made("long")["cues"] if "cross-platform." in c["text"])
        self.assertEqual(joined["lit"][1][0], mapped(20.5))  # "cross" keeps its own start
        self.assertEqual(joined["end_s"], round(self.v.truth["3:w8"][1], 2))  # "-platform." ends where it is heard
        self.assertNotEqual(joined["end_s"], mapped(21.4))
        for occurrence, at in (("3:w8", lambda t: [mapped(20.5) - .3, mapped(20.5) - .1]),  # heard end before the held start
                               ("1:w4", lambda t: [round(t[0], 2) + .004, round(t[0], 2) + .006])):  # under 1 cs
            self.build("long")
            (self.v.folder / "records/captions/long.json").unlink(missing_ok=True)  # same inputs: a new run, not reuse
            self.v.truth[occurrence] = at(self.v.truth[occurrence])
            record = cues.cues(self.v, "long")
            self.assertEqual(([r["reading"] for r in record["refusals"]], record["sidecar"]), (["word_interval"], None))
        self.build("long")
        first = self.v.truth["1:w1"][0] = math.floor(self.v.truth["1:w1"][0] * 100) / 100 + .001
        self.v.truth["1:w2"][0] = first + .003  # valid words whose starts round into one centisecond: lit collapses
        record = cues.cues(self.v, "long")
        self.assertEqual(([(r["reading"], r["value"]["word"]) for r in record["refusals"]], record["sidecar"]),
                         ([("word_interval", "w1")], None))

    def test_unchanged_inputs_reuse_the_record(self):
        self.build()
        first = self.made()
        stamp = lambda: [os.stat(self.v.folder / p)[1:9:7] for p in
                         ("records/captions/vertical.json", "records/captions/vertical.ass")]
        before = stamp()
        self.assertEqual(cues.cues(self.v, "vertical"), first)
        self.assertEqual(stamp(), before)

        def changed(expected):
            self.assertEqual(cues.check(self.v, "vertical")["state"], "stale")
            record = cues.cues(self.v, "vertical")
            self.assertEqual({k for k in record if record[k] != first[k]}, {"key"} | expected)
            cues.sidecar(self.v, "vertical")
            self.assertEqual(cues.check(self.v, "vertical")["state"], "captioned")
            self.texts = copy.deepcopy(TEXTS)
            self.put("records/render/vertical-voice.wav", b"voice of vertical")
            self.build()
            self.v.models["MMS_FA"] = MODEL

        cues.sidecar(self.v, "vertical")
        self.put("records/render/vertical-voice.wav", b"another take of the same voice")
        changed({"voice_sha256"})
        self.plan["end_hold_s"] = 2.0
        self.put("records/cut_plan2.json", self.plan)
        changed({"plan_sha256"})
        self.texts["s3"][2] = "clear."
        self.build()
        changed({"words_sha256", "cues", "burn", "sidecar"})
        self.v.models["MMS_FA"] = "sha256:" + "c" * 64
        changed({"aligner"})
        settings = cues.settings()
        settings["tall"]["pause_s"] = .5
        with patch.object(cues, "settings", lambda: copy.deepcopy(settings)):
            changed(set())

    def test_missing_aligner_is_unavailable(self):
        self.build()
        for name, undo in (("MMS_FA", lambda: self.v.models.pop("MMS_FA")), ("Poppins", lambda: self.v.models.pop("Poppins")),
                           ("records/render/vertical-voice.wav", lambda: (self.v.folder / "records/render/vertical-voice.wav").unlink()),
                           ("aligner runtime", lambda: setattr(self.v, "broken", "align"))):
            undo()
            self.v.asked = None
            record = cues.cues(self.v, "vertical")
            self.assertEqual((record["state"], record["missing"], record["cues"], record["sidecar"]),
                             ("unavailable", name, None, None))
            self.assertFalse((self.v.folder / "records/captions/vertical.ass").exists())
            if name != "aligner runtime":
                self.assertIsNone(self.v.asked)  # no transcript-time fallback ever asks around the missing piece
            self.assertFalse(cues.check(self.v, "vertical")["ok"])
            self.assertEqual(cues.check(self.v, "vertical")["state"], "unavailable")
            self.assertRaises(SpineError, cues.sidecar, self.v, "vertical")
            self.v.models.update({"MMS_FA": MODEL, "Poppins": FONT})
            self.v.broken = None
            self.build()
        self.assertEqual(cues.cues(self.v, "vertical")["state"], "captioned")  # installing it clears the record

    def test_refusal_names_cue_reading_and_setting(self):
        self.texts["s3"], self.step["s3"] = ["Cuts.", "land", "clean."], .5
        self.build()
        del self.v.truth["1:w2"]
        record = cues.cues(self.v, "vertical")
        self.assertEqual(record["refusals"], [{"cue": None, "reading": "aligned", "value": "w2",
                         "setting": "records/cut_plan2.json#spans", "owner": "item 17", "route": "item 17"}])
        self.assertEqual(record["uncaptioned"], [{"word": "w2", "reason": "not aligned"}])
        self.build()
        record = cues.cues(self.v, "vertical")
        cue = next(c for c in record["cues"] if c["text"] == ["Cuts."])
        self.assertEqual(record["refusals"], [{"cue": cue["id"], "reading": "min_cue_s", "value": .4,
                         "setting": "tall.min_cue_s", "owner": "captions.settings", "route": "captions"}])
        answer = cues.check(self.v, "vertical")
        self.assertEqual((answer["ok"], answer["state"], answer["next_action"], answer["detail"]),
                         (False, "refused", "revise", record["refusals"]))
        self.assertIsNone(record["sidecar"])
        self.v.onset_list = [0.0]
        self.texts = copy.deepcopy(TEXTS)
        self.step["s3"] = 1
        self.build()
        record = cues.cues(self.v, "vertical")
        self.assertTrue(record["refusals"] and all(r["reading"] == "max_onset_off_s"
                                                   and r["setting"] == "tall.onset_tol_s" for r in record["refusals"]))

    def test_crash_leaves_nothing_finished(self):
        self.build()
        real = os.replace

        def crash_on(suffix):
            def replace(a, b):
                if str(b).endswith(suffix):
                    raise OSError(f"killed before {suffix}")
                return real(a, b)
            return patch("os.replace", replace)

        for suffix, call in ((".ass", cues.cues), ("vertical.json", cues.cues)):
            with crash_on(suffix):
                self.assertRaises(OSError, call, self.v, "vertical")
            self.assertFalse(cues.check(self.v, "vertical")["ok"])
            self.assertRaises(SpineError, cues.sidecar, self.v, "vertical")
        self.assertEqual(cues.check(self.v, "vertical")["state"], "missing")
        record = cues.cues(self.v, "vertical")
        with crash_on("main.srt"):
            self.assertRaises(OSError, cues.sidecar, self.v, "vertical")
        self.assertEqual(cues.check(self.v, "vertical")["next_action"], "sidecar")
        self.assertEqual(sorted(p.name for p in (self.v.folder / "Vertical").iterdir()), [])
        cues.sidecar(self.v, "vertical")
        self.assertTrue(cues.check(self.v, "vertical")["ok"])
        clean = Fixture("setUp")
        clean.setUp()
        clean.build()
        cues.cues(clean.v, "vertical")
        self.assertEqual((self.v.folder / "records/captions/vertical.json").read_bytes(),
                         (clean.v.folder / "records/captions/vertical.json").read_bytes())  # one record for the key
        clean.tmp.cleanup()
        self.assertEqual(record, self.get("records/captions/vertical.json"))
        leftovers = [p for p in self.v.folder.rglob(".*") if p.name != "spine.lock"]
        self.assertEqual(leftovers, [])

    def test_owner_short_keeps_its_media(self):
        words = [{"id": f"o{i}", "text": t, "start": i / 2 + .2, "end": i / 2 + .6, "flags": []}
                 for i, t in enumerate(["My", "own", "short."])]
        self.put("records/words-own.json", {"words": words})
        self.put("records/render/short-own.json", {"mode": "measure_existing", "duration_s": 3.5, "elements": []})
        self.put("records/render/short-own-voice.wav", b"the supplied file's own sound")
        self.put("records/job.json", {"burn": None})
        media = b"the owner's finished short, bytes never touched"
        self.put("Shorts/own.mp4", media)
        self.v.truth = {f"0:{w['id']}": [w["start"] + .04, w["end"] + .04] for w in words}
        self.v.output = "short-own"
        record = self.made("short-own")
        self.assertEqual(self.v.heard, [self.v.folder / "records/render/short-own-voice.wav"] * 2)  # its own sound
        self.assertEqual((record["kept"], record["plan_sha256"], record["clock"], record["burn"]), (None, None, None, None))
        self.assertEqual([c["start_s"] for c in record["cues"]], [round(words[0]["start"] + .04, 2)])
        self.assertEqual(record["cues"][0]["band"], "bottom")
        self.assertEqual(cues.sidecar(self.v, "short-own"), "Shorts/own.srt")
        self.assertTrue(cues.check(self.v, "short-own")["ok"])
        self.put("records/job.json", {"burn": "supplied"})
        self.put("records/render/short-own.json", {"mode": "measure_existing", "duration_s": 3.5})  # no elements read
        self.v.models.pop("Poppins")  # no font is needed when nothing is placed
        self.v.fonts.clear()
        record = cues.cues(self.v, "short-own")
        self.assertEqual(record["state"], "captioned")
        self.assertNotIn("band", record["cues"][0])
        self.assertEqual(self.v.fonts, [])
        cues.sidecar(self.v, "short-own")
        self.assertEqual((self.v.folder / "Shorts/own.mp4").read_bytes(), media)
        self.assertEqual(sorted(p.name for p in (self.v.folder / "records/captions").iterdir()), ["short-own.json"])
        self.assertEqual(self.srt("Shorts/own.srt")[0][2], "My own short.")


if __name__ == "__main__":
    unittest.main()
