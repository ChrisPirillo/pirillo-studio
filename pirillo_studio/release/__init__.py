"""The release: delivered files to the words that go out with them (docs/release.md, queue item 39).

api.py answers build, edit, decide and check; inputs.py reads and hashes; rows.py makes outputs and rows;
readings.py holds the release.<name> checks; review.py renders and reads Review.md.

The host adapter, duck-typed like cutting's (`video`):
- folder: the video folder (a pathlib.Path).
- recipe() -> (name, recipe): the spine's recipe data; only its stage list is read, never the name. The `release`
  stage's inputs say which records are read (a `records/text_post.md` input brings the text post and its read and
  the `text` rows); a stage whose check is `titles` brings the title test, the lead title's A/B titles and, for a
  short, its copy `hook` as its title (without it every output takes publish.json's `title`). Both stages' data
  are bound in the release key, so a changed stage list stales the release.
- settings(name) -> dict: the resolved `release.settings` profile (one place for every value below).
- now() -> str: the time an edit or decision is recorded at. A build captures its day once (the evaluation day,
  bound in the key and kept as `day`); topic and title research ages count to it, and a release whose dated reading
  would give another result today (a window crossed) is stale.
- score_titles([title, ...]) -> [number, ...]: the external title scorer; absent or failing is `unavailable`.
- lint_text(words) -> [problem, ...]: the host's text post lint (host postlint FAILs), per text post row
  (release.text); absent or failing is `unavailable`.
- chat_shown() -> bool: the video shows a made-up chat (release.disclosure).
- search_phrases() -> [phrase, ...]: what people search for the topic, from research (release.search-phrase).
- topics() -> {topic: "YYYY-MM-DD"}: the host's topic book, each topic's last day seen active, any case
  (release.topic). A caption's topic is optional: one `Topic:` line, its last, and only on topic.platforms; its
  chain ("a > b") takes the first fresh tag, and a stale one only warns (host topic_problems, topic_pick).
chat_shown, search_phrases and topics are bound in the release key; each absent or broken is `unavailable` naming
it, so no release builds without them. An unchanged key whose measured build finished is returned as it is:
neither the scorer nor lint_text is called again.

`release.settings`, every key required where a check reads it (a missing key or a non-finite number anywhere in the
profile is `unavailable` naming it):
- naming: {"replace": {char: text}, "drop": chars}: an output's `name` from its title (release.naming).
- description: {"parts": [...], "footer": text}: the long description in order. A part is `chapters` ("m:ss name"
  lines), `footer`, `hashtags` (publish.json `hashtags` joined), `disclosure` (the disclosure texts owed, if any),
  or any other name, read from publish.json `description_<part>`; parts are joined by a blank line.
- disclosure: {"chat": text, "ai": text}: owed in the long description when chat_shown() is true and when
  publish.json `ai_label` is true (release.disclosure).
- map: {"long"|"vertical"|"short"|"text": [platform, ...]}: where each output kind (and the text post) goes.
- platforms: {platform: {"max_chars": n, "hashtags": {"min": n, "max": n}}}; length: {"long_url": text} (a link
  of the length posting fills `{long_url}` with, counted in its place).
- his_words: {"max_skip": n}; do_not_say: [phrase]; literals: [text]; own_urls: [url prefix];
  topic: {"platforms": [platform], "max_chars": n, "fresh_days": n}; cover: {"words": {"min": n, "max": n}, "none":
  [regex]} (a short's cover: one line, as host cover_text_problem, its why matching titles.why_evidence; or no
  text and a why matching a `none` pattern, a recorded none); titles: {"count": n, "min_chars": n, "max_chars": n, "min_score": n, "research_days": n,
  "never_open": [phrase], "quote_words": n, "why_evidence": [regex], "sources": [regex]} (host why_problems and
  title_research_problems: a why matching an evidence pattern, a searched source matching a source pattern);
  chapters: {"near_s": seconds}; search: {"min_rows": n}; text: {"headline_end": text}.

Maker fields read (a missing or malformed one refuses naming it): publish.json title, ab_titles [{title, why,
number_why (required when the title holds a digit), searched: {phrase, source, at}, said: {quote} or {words: [index into words.json]}}], description_<part>, chapters, hashtags, tags, pinned_comment, category, ai_label (bool),
end_screen_start_s, end_screen_video, end_screen_playlist, thumbnails; copy.json per output its platforms' words,
and beside a vertical's or short's words cover_text {text, why, at_s} (and a short's hook when a titles stage runs).

An independent read (records/copy_review.md, records/text_review.md) binds what it read with a line
`reads: <path> sha256:<hex>`; a read naming other bytes is stale, and a table row saying DOESN'T READ refuses (the
row test is host qc.py run_copy_review's, already right). release.his-words is host wordcopy.py `check` (his words
in order, a few skips), already right, read against records/words.json with no glossary fixes; release.same is
wordcopy.py `same`, already right. Both are copied, not imported.

Contract 15, test_switch_off_is_unchanged, is the host switch's (seam 3) and is not written here.
"""
