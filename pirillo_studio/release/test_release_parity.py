"""Host parity of the release checks (review-r4 and its sweep), each read from the written records/release.json."""
from pirillo_studio.release import api
from pirillo_studio.release.inputs import RELEASE
from pirillo_studio.release.test_release import Fixture


class Parity(Fixture):
    """e2e: records/release.json read back after each build; the host rule each case keeps is in its message."""

    def outcome(self, change):
        """Build with `change` applied; (state, {(check, row, setting): result}) from the written record."""
        self.setUp()
        change()
        self.sync()
        api.build(self.v)
        rec = self.get(RELEASE)
        rs = {}
        for r in rec.get("readings", []):  # one key may hold two readings; the worst is kept
            k = (r["check"], r["row"], r["setting"])
            rs[k] = r["result"] if rs.get(k) in (None, "pass") else rs[k]
        return rec["state"], rs

    def expect(self, cases):
        for name, (change, state, reading, result) in cases.items():
            with self.subTest(case=name):
                got, rs = self.outcome(change)
                self.assertEqual(got, state, (name, [k for k, v in rs.items() if v != "pass"]))
                if reading:
                    self.assertEqual(rs.get(reading), result, name)

    def chirp(self, words):
        return lambda: self.copy["long"].update(chirp=words)

    def test_literals_and_banned_phrases_match_as_the_host(self):
        """defect: review-r4 points 1 and 2 (qc.py run_literals, run_do_not_say; wordcopy.py do_not_say)."""
        pin = lambda words: lambda: self.publish.update(pinned_comment=words)
        self.expect({
            "any case": (self.chirp("Details matter a lot. todo\nTopic: Gadgets"), "refused",
                         ("release.literals", "long:chirp", "literals"), "fail"),
            "inside a word": (lambda: self.publish.update(description_body="Details matter a lot. TODOS"), "built",
                              ("release.literals", "long:youtube", "literals"), "pass"),
            "pin literal": (pin("Shorter is better here. TODO"), "refused",
                            ("release.literals", "long#pinned_comment", "literals"), "fail"),
            "pin banned": (pin("Forbidden thing is better here."), "refused",
                           ("release.do-not-say", "long#pinned_comment", "do_not_say"), "fail"),
            "title banned": (lambda: self.publish.update(title="Gadget: forbidden thing"), "refused",
                             ("release.do-not-say", "long#title", "do_not_say"), "fail"),
            "tag banned": (lambda: self.publish.update(tags=["forbidden-thing"]), "refused",
                           ("release.do-not-say", "long#tags", "do_not_say"), "fail"),
            "hashtag banned": (self.chirp("Details matter a lot. #ForbiddenThings\nTopic: Gadgets"), "refused",
                               ("release.do-not-say", "long:chirp", "do_not_say"), "fail"),
            "chapter banned": (lambda: self.publish["chapters"][1].__setitem__(1, "Forbidden thing"), "refused",
                               ("release.do-not-say", "long#0:10", "do_not_say"), "fail"),
            "literal not text": (lambda: self.v.profile.update(literals=[3]), "unavailable", None, None),
            "pin his words": (pin("Nobody said this pin."), "refused",
                              ("release.his-words", "long#pinned_comment", "his_words.max_skip"), "fail"),
        })

    def test_a_headline_ends_exactly_and_the_host_lint_reads_the_post(self):
        """defect: review-r4 point 3 (wordcopy.py headline_problems) and qc.py run_postlint."""
        post = lambda words: lambda: self.put("records/text_post.md", words)
        self.expect({
            "four dots": (post("# Heading....\nA post about the gadget.\n"), "refused",
                          ("release.text", "article:board", "text.headline_end"), "fail"),
            "three dots": (post("# Heading...\nA post about the gadget.\n"), "built",
                           ("release.text", "article:board", "text.headline_end"), "pass"),
            "lint": (lambda: setattr(self.v, "lint", ["list-bullets: 3 bullet lines"]), "refused",
                     ("release.text", "article:board", "lint_text"), "fail"),
            "no lint": (lambda: setattr(self.v, "lint_text", None), "unavailable", None, None),
        })

    def test_chapters_read_as_the_host(self):
        """defect: review-r4 point 4 (qc.py run_chapters: 0:00 skipped, strict slack, a break inside the cue)."""
        def cues(*cs):
            return lambda: self.put("records/captions/long.json", {"cues": [
                {"start_s": a, "end_s": b, "lines": [x]} for a, b, x in cs]})
        late = (10.4, 14.0, "Details matter a lot.")
        five = lambda: self.publish["chapters"].insert(1, ["0:05", "Middle"])
        self.expect({
            "zero skipped": (cues((3.0, 9.0, "Subject works well."), late), "built", None, None),
            "break inside": (lambda: (five(), cues((0.0, 10.0, "Subject works. Details matter."), late)()),
                             "built", ("release.chapters", "long#0:05", "chapters.near_s"), "pass"),
            "no break": (lambda: (five(), cues((0.0, 10.0, "Subject works well"), late)()), "refused",
                         ("release.chapters", "long#0:05", "chapters.near_s"), "fail"),
            "at the slack": (cues((0.0, 9.0, "Subject works well."), (11.0, 14.0, "Details.")), "refused",
                             ("release.chapters", "long#0:10", "chapters.near_s"), "fail"),
            "no end": (lambda: self.put("records/captions/long.json", {"cues": [{"start_s": 0.0, "lines": []}]}),
                       "unavailable", None, None),
        })

    def test_a_topic_is_optional_last_and_only_where_set(self):
        """defect: review-r4 point 5 (wordcopy.py topic_problems, topic_pick)."""
        stale = ("release.topic", "long:chirp", "topic.fresh_days")
        self.expect({
            "first line": (self.chirp("Topic: Gadgets\nDetails matter a lot."), "refused",
                           ("release.topic", "long:chirp", "topic.platforms"), "fail"),
            "two lines": (self.chirp("Details matter a lot.\nTopic: Gadgets\nTopic: Gadgets"), "refused",
                          ("release.topic", "long:chirp", "topic.platforms"), "fail"),
            "off its platforms": (lambda: self.copy["vertical"].update(clip="Subject works well.\nTopic: Gadgets"),
                                  "refused", ("release.topic", "vertical:clip", "topic.platforms"), "fail"),
            "none": (self.chirp("Details matter a lot."), "built", ("release.topic", "long:chirp", "topic.platforms"), "pass"),
            "stale warns": (lambda: self.v.book.update(Gadgets="2025-06-01"), "built", stale, "warn"),
            "first fresh in the chain": (self.chirp("Details matter a lot.\nTopic: Old things > gadgets"), "built", stale,
                                         "pass"),
        })

    def test_limits_read_as_the_host(self):
        """defect: review-r4 sweep (qc.py run_hashtags captions only, run_caption_length's {long_url} fill)."""
        self.expect({
            "article hashtags": (lambda: self.put("records/text_post.md", "A post about the #gadget.\n"), "built", None, None),
            "link filled": (self.chirp("Details matter a lot. " * 3 + "{long_url}\nTopic: Gadgets"), "refused",
                            ("release.length", "long:chirp", "platforms.chirp.max_chars"), "fail"),
        })

    def test_covers_and_title_signals_read_as_the_host(self):
        """defect: review-r4 sweep (qc.py run_cover_text, why_problem, run_titles_signals)."""
        cover = lambda **kw: lambda: self.copy["shorts"]["alpha"]["cover_text"].update(**kw)
        ab = lambda **kw: lambda: self.publish["ab_titles"][2].update(**kw)
        self.expect({
            "recorded none": (cover(text="", why="none: a face frame alone"), "built",
                              ("release.cover", "short-alpha", "cover.none"), "pass"),
            "bare none": (cover(text="", why="none"), "refused", ("release.cover", "short-alpha", "cover.none"), "fail"),
            "why without evidence": (cover(why="the point"), "refused",
                                     ("release.cover", "short-alpha", "titles.why_evidence"), "fail"),
            "vertical unread": (lambda: self.copy["vertical"]["cover_text"].update(text="One two three four five"),
                                "built", None, None),
            "too short": (ab(title="Gadget"), "refused", ("release.titles", "long#Gadget", "titles.min_chars"), "fail"),
            "number without why": (ab(title="The gadget after 1 year"), "refused",
                                   ("release.titles", "long#The gadget after 1 year", "titles.why_evidence"), "fail"),
            "number with why": (ab(title="The gadget after 1 year", number_why="a year is the claim"), "built", None, None),
        })
