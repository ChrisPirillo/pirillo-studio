"""Release contracts 1 to 7 (docs/release.md section 5), each inspecting the written records; contract 15,
test_switch_off_is_unchanged, is the host switch's (seam 3) and is not written here."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from pirillo_studio.qc import evaluators
from pirillo_studio.release import api
from pirillo_studio.release.inputs import ENVELOPE, RELEASE, REVIEW
from pirillo_studio.spine import engine
from pirillo_studio.spine.files import digest, filehash

SAID = "Subject works well. Details matter a lot. The result lasts for years. Shorter is better here."
SETTINGS = {
    "naming": {"replace": {":": " -", "/": "-"}, "drop": "*\"<>|"},
    "description": {"parts": ["lead", "body", "chapters", "disclosure", "footer", "hashtags"], "footer": "Footer line."},
    "disclosure": {"chat": "The chat is made up.", "ai": "Made with machine help."},
    "map": {"long": ["youtube", "chirp"], "vertical": ["clip"], "short": ["youtube", "clip"], "text": ["board"]},
    "platforms": {"youtube": {"max_chars": 5000, "hashtags": {"min": 0, "max": 3}},
                  "chirp": {"max_chars": 100, "hashtags": {"min": 0, "max": 2}},
                  "clip": {"max_chars": 150, "hashtags": {"min": 0, "max": 3}},
                  "board": {"max_chars": 3000, "hashtags": {"min": 0, "max": 0}}},
    "his_words": {"max_skip": 3}, "do_not_say": ["forbidden thing"], "literals": ["TODO"],
    "own_urls": ["https://own.invalid/"], "length": {"long_url": "https://own.invalid/xxxxxxxx"}, "topic": {"platforms": ["chirp"], "max_chars": 20, "fresh_days": 30},
    "cover": {"words": {"min": 2, "max": 4}, "none": [r"none:\s*\S.{7,}"]}, "chapters": {"near_s": 1.0}, "search": {"min_rows": 1}, "text": {"headline_end": "..."},
    "titles": {"count": 3, "min_chars": 10, "max_chars": 60, "min_score": 50, "research_days": 7, "never_open": ["how to"],
               "quote_words": 3, "why_evidence": [r"\bno data\b", r"\bviews\b", "searched for"],
               "sources": ["autocomplete", r"\bsearch\b"], "no_data": [r"\bno data\b.*\btest"]},
}


class Video:
    """The host adapter: folder, recipe(), settings(name), now(), score_titles(titles), chat_shown(),
    search_phrases(), topics(), lint_text(words); `scored` logs every scorer call."""

    def __init__(self, folder, recipe):
        self.folder, self.name, self.profile, self.scores, self.clock = folder, recipe, None, [80, 70, 60], 0
        self.lint = []
        self.day = "2026-01-01"
        self.chat, self.phrases, self.book, self.scored = True, ["gadget", "result"], {"Gadgets": "2026-01-01"}, []
        self.stages = copy.deepcopy(engine.data("recipes.json")[recipe])

    def recipe(self):
        return self.name, copy.deepcopy(self.stages)

    def chat_shown(self):
        return self.chat

    def search_phrases(self):
        return list(self.phrases)

    def topics(self):
        return dict(self.book)

    def settings(self, name):
        if name != "release.settings":
            raise KeyError(name)
        return copy.deepcopy(self.profile)

    def now(self):
        self.clock += 1
        return f"{self.day}T00:00:{self.clock:02d}Z"

    def lint_text(self, words):
        return list(self.lint)

    def score_titles(self, titles):
        self.scored.append(list(titles))
        return self.scores[:len(titles)]


class Fixture(unittest.TestCase):
    recipe = "plain"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.v = Video(Path(tmp.name) / "video", self.recipe)
        self.v.folder.mkdir()
        self.v.profile = copy.deepcopy(SETTINGS)
        self.make()

    def put(self, name, value):
        f = self.v.folder / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(value if isinstance(value, bytes) else
                      value.encode() if isinstance(value, str) else json.dumps(value).encode())

    def get(self, name):
        return json.loads((self.v.folder / name).read_text())

    def text(self, name):
        return (self.v.folder / name).read_text()

    def files(self):
        return {"YouTube/main.mp4": b"long video", "YouTube/main.srt": b"long cues",
                "Vertical/main.mp4": b"tall video", "Vertical/main.srt": b"tall cues",
                "Shorts/alpha.mp4": b"short video", "Shorts/alpha.srt": b"short cues"}

    def make(self):
        self.members = self.files()
        for p, raw in self.members.items():
            self.put(p, raw)
        self.manifest()
        self.put("records/words.json", {"words": [{"id": f"w{i}", "text": w, "start": i, "end": i + .5}
                                                  for i, w in enumerate(SAID.split())]})
        self.publish = {"title": "Gadget: works well", "description_lead": "Subject works well.",
                        "description_body": "Details matter a lot.", "chapters": [["0:00", "Start"], ["0:10", "Details"]],
                        "hashtags": ["#gadget", "#tech", "#review"], "tags": ["gadget"], "pinned_comment": "Shorter is better here.",
                        "end_screen_video": "https://own.invalid/v1", "end_screen_playlist": "",
                        "end_screen_start_s": 512.0, "category": "Science", "ai_label": False,
                        "thumbnails": ["thumbs/a.jpg"],
                        "ab_titles": [{"title": t, "why": why, "said": said,
                                       "searched": searched}
                                      for t, why, said, searched in (
                                          ("Gadget: works well", "searched for in the topic", {"quote": "Subject works well"}, {"phrase": "gadget review", "source": "autocomplete", "at": "2025-12-30"}),
                                          ("Does the gadget work", "questions get views", {"words": [0]}, {"phrase": "gadget review", "source": "autocomplete", "at": "2025-12-30"}),
                                          ("The gadget after a year", "no data: this is the test", {"quote": "lasts for years"}, "no data, testing"))]}
        self.put("thumbs/a.jpg", b"picture")
        self.copy = {"long": {"chirp": "Details matter a lot.\nTopic: Gadgets"},
                     "vertical": {"clip": "Subject works well. #gadget",
                                  "cover_text": {"text": "It works", "why": "the claim", "at_s": 2.0}},
                     "shorts": {"alpha": {"hook": "Result lasts", "youtube": "The result lasts for years.",
                                          "clip": "The result lasts.", "cover_text": {"text": "It lasts", "why": "no data: the hook is the test", "at_s": 1.5}}}}
        self.put("records/captions/long.json", {"cues": [{"start_s": 0.0, "end_s": 9.0, "lines": ["Subject works well."]},
                                                    {"start_s": 10.4, "end_s": 14.0, "lines": ["Details matter a lot."]}]})
        self.put("records/text_post.md", "A post about the gadget.\n")
        self.sync()

    def manifest(self):
        self.put("records/final_manifest.json", {"members": [{"path": p, "sha256": digest(raw)} for p, raw in self.members.items()]})

    def sync(self, copy_extra="", text_extra=""):
        """Write publish and copy, and both independent reads bound to their current bytes."""
        self.put("records/publish.json", self.publish)
        self.put("records/copy.json", self.copy)
        self.put("records/copy_review.md", f"# Copy read\nreads: records/copy.json {filehash(self.v.folder / 'records/copy.json')}\n"
                                           f"| row | verdict |\n| all | READS |\n{copy_extra}")
        if (self.v.folder / "records/text_post.md").exists():
            self.put("records/text_review.md", f"reads: records/text_post.md {filehash(self.v.folder / 'records/text_post.md')}\n{text_extra}")

    def built(self):
        rec = api.build(self.v)
        self.assertEqual(rec["state"], "built", rec.get("detail"))
        return rec

    def refused(self, *names):
        """Build, then read the written release.json: refused naming each of names."""
        api.build(self.v)
        rec = self.get(RELEASE)
        self.assertEqual(rec["state"], "refused", rec.get("detail"))
        said = json.dumps(rec["detail"])
        for n in names:
            self.assertIn(n, said)
        self.assertEqual(self.get(ENVELOPE)["state"], "fail")
        return rec

    def rows(self):
        return {r["id"]: r for r in self.get(RELEASE)["rows"]}

    def approve_all(self):
        return api.decide(self.v, {rid: "approved" for rid in self.rows()}, "owner")

    def ledger(self):
        """A fake posting ledger write: what posting would send, read only through check."""
        return {r["id"]: r["words"] for r in api.check(self.v)["send"]}

    def qc(self, entry_id="release"):
        entry = next(e for e in json.loads((Path(evaluators.__file__).parent / "catalog.json").read_text())
                     if e["id"] == entry_id)
        env = self.get(ENVELOPE)
        paths = {p: filehash(self.v.folder / p) for p in set(env["reads"]) | {r["record"] for r in entry["reads"]}}
        return evaluators.release(entry, {"release-check": env, "__paths__": paths}, self.v.settings("release.settings"))


class Contracts(Fixture):
    """e2e: records/release.json, Review.md, release_edits.json and the envelope, read back after each call."""

    def test_every_mapped_platform_has_its_row(self):
        rec = self.built()
        self.assertEqual([o["id"] for o in rec["outputs"]], ["long", "vertical", "short-alpha"])
        self.assertEqual(list(self.rows()), ["long:youtube", "long:chirp", "vertical:clip", "short-alpha:youtube",
                                             "short-alpha:clip", "article:board"])
        self.assertEqual(self.qc()["result"], "pass")
        self.assertEqual(self.qc("titles")["result"], "pass")
        long = self.get(RELEASE)["outputs"][0]["fields"]
        self.assertEqual((long["category"], long["ai_label"], long["end_screen"]),
                         ("Science", False, {"start_s": 512.0, "video": "https://own.invalid/v1", "playlist": ""}))
        self.assertEqual([o["cover"]["at_s"] for o in self.get(RELEASE)["outputs"][1:]], [2.0, 1.5])
        for name, bad in (("category", None), ("ai_label", "no"), ("end_screen_start_s", -1), ("at_s", None)):
            with self.subTest(missing=name):
                self.setUp()
                if name == "at_s":
                    del self.copy["shorts"]["alpha"]["cover_text"]["at_s"]
                elif bad is None:
                    del self.publish[name]
                else:
                    self.publish[name] = bad
                self.sync()
                self.refused(f"{name} missing", "packager")
        self.setUp()
        del self.copy["shorts"]["alpha"]["clip"]
        self.sync()
        self.refused("short-alpha:clip", "packager")
        self.copy["shorts"]["alpha"]["clip"] = "The result lasts."
        self.copy["vertical"]["chirp"] = "Shorter is better here."  # a route the map does not send
        self.sync()
        self.refused("vertical", "chirp", "outside map.vertical")
        self.assertEqual(self.qc()["result"], "fail")

    def test_rows_are_what_is_sent(self):
        rec = self.built()
        self.approve_all()
        sent = self.ledger()
        self.assertEqual(set(sent), set(self.rows()))
        self.assertEqual(sent["long:youtube"], rec["outputs"][0]["description"]["text"])
        self.assertTrue(sent["long:youtube"].startswith("Subject works well.\n\nDetails matter a lot.\n\n0:00 Start"))
        self.assertEqual(sent["long:chirp"], self.copy["long"]["chirp"])
        self.assertEqual(sent["article:board"], "A post about the gadget.\n")
        for rid, r in self.rows().items():
            self.assertEqual(digest(sent[rid].encode()), r["words_sha256"])
        (self.v.folder / "records/copy.json").write_text("{}")  # posting reads the record, never the copy again
        self.assertEqual(self.ledger(), {})

    def test_words_are_his_and_within_limits(self):
        def chirp(words):
            self.copy["long"]["chirp"] = words

        def ab(i, k, v):
            self.publish["ab_titles"][i][k] = v
        cases = {
            "his-words": (lambda: chirp("Nobody said this at all."), "his_words.max_skip"),
            "same": (lambda: self.copy["shorts"]["alpha"].update(clip="The result lasts for years."), "map"),
            "do-not-say": (lambda: chirp("Details matter a lot, forbidden thing.\nTopic: Gadgets"), "do_not_say"),
            "literals": (lambda: chirp("Details matter a lot. TODO\nTopic: Gadgets"), "literals"),
            "hashtags": (lambda: chirp("Details matter a lot. #a #b #c\nTopic: Gadgets"), "platforms.chirp.hashtags"),
            "length": (lambda: chirp("Details matter a lot. " * 6 + "\nTopic: Gadgets"), "platforms.chirp.max_chars"),
            "topic": (lambda: chirp("Topic: Gadgets\nDetails matter a lot."), "topic.platforms"),
            "cover": (lambda: self.copy["shorts"]["alpha"]["cover_text"].update(text="It lasts very well here"), "cover.words"),
            "cover one line": (lambda: self.copy["shorts"]["alpha"]["cover_text"].update(text="It\nlasts"), "cover.words"),
            "own-urls": (lambda: self.publish.update(end_screen_video="https://elsewhere.invalid/v"), "own_urls"),
            "disclosure": (lambda: self.v.profile["description"]["parts"].remove("disclosure"), "disclosure"),
            "text": (lambda: self.put("records/text_post.md", "# A heading\nA post about the gadget.\n"), "text.headline_end"),
            "titles": (lambda: ab(1, "searched", {"phrase": "gadget review", "source": "autocomplete", "at": "2025-06-01"}), "titles.research_days"),
            "titles how-to": (lambda: ab(2, "title", "How to keep a gadget"), "titles.never_open"),
            "titles why": (lambda: ab(1, "why", "because"), "titles.why_evidence"),
            "titles source": (lambda: ab(0, "searched", {"phrase": "gadget review", "source": "a hunch", "at": "2025-12-30"}),
                              "titles.sources"),
            "titles quote": (lambda: ab(2, "said", {"quote": "he never says this"}), "titles.quote_words"),
            "titles ids": (lambda: ab(1, "said", {"words": [0, 999, 2]}), "titles.quote_words"),
            "titles score": (lambda: setattr(self.v, "scores", [80, 70, 10]), "titles.min_score"),
        }
        for name, (change, setting) in cases.items():
            with self.subTest(check=name):
                self.setUp()
                change()
                self.sync()
                check = "release." + name.split()[0]
                rec = self.refused(check)
                bad = [r for r in rec["detail"] if r["check"] == check]
                self.assertEqual({r["setting"] for r in bad}, {setting}, bad)
                self.assertTrue(all(r["row"] and r["maker"] and "value" in r and r["result"] == "fail" for r in bad), bad)
                self.assertEqual(self.ledger(), {})
        self.setUp()
        self.v.phrases = ["nothing anyone said"]  # a missing searched phrase warns and never refuses
        warned = [r for r in self.built()["readings"] if r["check"] == "release.search-phrase"]
        self.assertEqual({(r["row"], r["result"], r["setting"]) for r in warned},
                         {(o, "warn", "search.min_rows") for o in ("long", "vertical", "short-alpha")})
        self.setUp()
        self.publish["ai_label"] = True
        self.sync()
        self.assertIn("The chat is made up. Made with machine help.", self.built()["outputs"][0]["description"]["text"])
        api.edit(self.v, "article:board", "# Broken headline\nHis post.\n", "owner")  # lint reads the final row
        bad = [r for r in self.refused("release.text")["detail"] if r["check"] == "release.text"]
        self.assertEqual([(r["row"], r["maker"], r["value"]) for r in bad], [("article:board", "owner", "Broken headline")])

    def test_a_missing_scorer_is_unavailable(self):
        def gone():
            raise OSError("offline")
        for name, broken, named in (("score_titles", None, "title scorer"), ("score_titles", lambda t: 1 / 0, "division"),
                                    ("chat_shown", None, "chat_shown: the host supplies none"),
                                    ("search_phrases", gone, "search_phrases: unavailable (offline)"),
                                    ("topics", lambda: ["a"], "topics: not a dict")):
            with self.subTest(named=named):
                self.setUp()
                setattr(self.v, name, broken)
                rec = api.build(self.v)
                self.assertEqual(rec["state"], "unavailable")
                self.assertIn(named, rec["detail"])
                self.assertFalse(api.check(self.v)["ok"])
                self.assertEqual(self.get(ENVELOPE)["state"], "unavailable")
                self.assertEqual(self.qc()["result"], "unavailable")
                self.assertNotIn("readings", self.get(RELEASE))
        self.setUp()
        del self.v.profile["platforms"]["chirp"]["max_chars"]
        rec = api.build(self.v)
        self.assertEqual((rec["state"], rec["detail"]), ("unavailable", "release.settings platforms.chirp.max_chars: missing"))
        self.assertEqual(api.check(self.v)["state"], "unavailable")

    def test_review_edits_come_back(self):
        old = self.built()
        before = (self.v.folder / "records/copy.json").read_bytes()
        page = self.text(REVIEW)
        self.assertEqual(page.splitlines()[0], f"release {old['key']}")
        self.put(REVIEW, page.replace("```long:chirp\nDetails matter a lot.", "```long:chirp\nDetails matter."))
        rec = self.built()
        edits = self.get("records/release_edits.json")["edits"]
        self.assertEqual([(e["row"], e["text"], e["by"], e["replaced"], e["key"]) for e in edits],
                         [("long:chirp", "Details matter.\nTopic: Gadgets", "owner", self.copy["long"]["chirp"], old["key"])])
        self.assertEqual((self.rows()["long:chirp"]["words"], self.rows()["long:chirp"]["by"]),
                         ("Details matter.\nTopic: Gadgets", "owner"))
        self.assertIn("```long:chirp\nDetails matter.\nTopic: Gadgets\n```", self.text(REVIEW))
        self.assertEqual(self.text(REVIEW).splitlines()[0], f"release {rec['key']}")
        self.assertEqual((self.v.folder / "records/copy.json").read_bytes(), before)

    def test_owner_edit_is_held_to_his_words(self):
        """guard: an owner's edit to a caption never skips release.his-words (r5 review of item 39)."""
        self.built()
        api.edit(self.v, "long:chirp", "Nobody said this at all.\nTopic: Gadgets", "owner")
        rec = self.refused("release.his-words")
        self.assertEqual([(r["row"], r["maker"]) for r in rec["detail"] if r["check"] == "release.his-words"],
                         [("long:chirp", "owner")])
        self.assertEqual(self.ledger(), {})

    def test_unmappable_edit_refuses_and_keeps_his_file(self):
        old = self.built()
        page = self.text(REVIEW)
        cases = {
            "no row ID": page.replace("```long:chirp\nDetails matter a lot.", "```\nDetails matter."),
            "outside the row blocks": page.replace("## vertical", "## my note\n\n## vertical"),
            "a key with no render": page.replace(old["key"], "sha256:" + "0" * 64),
            "names no release key": page.split("\n", 1)[1],
        }
        for named, his in cases.items():
            with self.subTest(named=named):
                self.put(REVIEW, his)
                self.refused(named, "owner")
                self.assertEqual(self.text(REVIEW), his)
                self.assertEqual(self.get("records/release_edits.json"), {"edits": []})
        his = page.replace("```long:chirp\nDetails matter a lot.", "```long:chirp\nDetails matter.")
        self.put(REVIEW, his)
        self.copy["long"]["chirp"] = "Shorter is better here.\nTopic: Gadgets"  # the packager changed it too
        self.sync()
        self.refused("long:chirp", "changed upstream")
        self.assertEqual(self.text(REVIEW), his)
        self.assertEqual(self.get("records/release_edits.json"), {"edits": []})

    def test_an_edit_clears_only_its_row(self):
        self.built()
        self.approve_all()
        self.assertTrue(api.check(self.v)["ok"])
        api.edit(self.v, "short-alpha:youtube", "Result lasts for years.", "owner")
        self.assertEqual(api.check(self.v)["state"], "stale")
        self.assertEqual(self.ledger(), {})
        self.built()
        result = api.check(self.v)
        self.assertEqual((result["state"], result["detail"]["waiting"]), ("waiting_owner", ["short-alpha:youtube"]))
        self.assertEqual(set(self.ledger()), set(self.rows()) - {"short-alpha:youtube"})
        api.decide(self.v, {"short-alpha:youtube": "approved"}, "owner")
        self.assertEqual(self.ledger()["short-alpha:youtube"], "Result lasts for years.")
        self.assertTrue(api.check(self.v)["ok"])


if __name__ == "__main__":
    unittest.main()
