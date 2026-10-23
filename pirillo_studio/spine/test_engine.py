"""docs/spine.md section 5: the owner's expectations as contract tests, each inspecting the written record files.

Test 14 (the host switch is off-unchanged) belongs to the host switch, seam 3.
"""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from pirillo_studio.spine import engine as spine


class Host:
    """The host's adapters as fakes that keep every call. The posted check is the posting domain's (docs/spine.md
    section 2): here it answers what a test sets, posted by default."""

    def __init__(self, verdicts=None):
        self.started, self.asked, self.told = [], [], []
        self.verdicts = verdicts or {}
        self.seen = {}
        self.fail_tell = self.crash_after = 0

    def start(self, actor, run, folder, stage, key):
        self.started.append((stage, key))
        return f"due 2026-10-23T09:00Z:{key}" if stage == "posting" else f"started:{key}"

    def ask(self, task):
        self.asked.append(task)
        return f"asked:{task['id']}"

    def tell(self, actor, key, handoff):
        if self.fail_tell:
            self.fail_tell -= 1
            raise OSError("crash before the inbox wrote")
        if (actor, key) not in self.told:  # the inbox reconciles by key
            self.told.append((actor, key))
        if self.crash_after:
            self.crash_after -= 1
            raise OSError("crash after the inbox wrote")
        return f"told:{key}"

    def check(self, c):
        def fn(evidence):
            self.seen.setdefault(c, []).append(evidence)
            v = self.verdicts.get(c)
            return v(evidence) if callable(v) else v or {"state": "posted" if c == "posted" else "pass", "words": []}
        return fn

    def video(self, folder, version="1", **kw):
        catalog = kw.pop("catalog", spine.data("catalog.json"))
        return spine.Video(folder, start=self.start, ask=self.ask, tell=self.tell, href=lambda p: "/served/" + p,
                           checks={c: (version, self.check(c)) for c in catalog["checks"]}, catalog=catalog, **kw)


def new_video(root, recipe, name="v"):
    folder = Path(root, name)
    (folder / "records").mkdir(parents=True)
    (folder / "source.mp4").write_bytes(b"footage")
    (folder / "records" / "job.json").write_text(json.dumps({"recipe": recipe, "id": name}))
    return folder


def make(v, st, mark=""):
    """The maker writes the stage's outputs (a manifest lists its media members), then the checker each review,
    naming what it judged."""
    reviews = {p: c for c in st["check"] for p in v.catalog["checks"][c]["produces"]}
    for p in sorted(st["outputs"], key=lambda p: p in reviews):
        f = v.folder / p
        f.parent.mkdir(parents=True, exist_ok=True)
        if p.endswith("_manifest.json"):
            members = [{"path": m, "sha256": spine.filehash(v.folder / m)} for m in st["outputs"]
                       if m.startswith("Shorts/")]
            f.write_text(json.dumps({"members": members}))
        elif p in reviews:
            f.write_text(f"{p} review of {spine.judged(spine.evidence(v, st, reviews[p]), reviews)}")
        elif st["run"].get("role") != "owner":
            f.write_text(f"{p} {mark}")


def decide(v, host, verb="approve", words="", stage=None):
    st = spine.find(v.recipe()[1], stage or spine.next(v)["stage"])
    (v.folder / st["outputs"][0]).write_text(json.dumps({"task": host.asked[-1]["id"], "verb": verb, "words": words}))


def drive(v, host, until=None):
    """Work the video the way the host loop would, from next's answers alone, until `until` or the end."""
    for _ in range(200):
        a = spine.next(v)
        if a["stage"] is None or a["stage"] == until and a["state"] in ("ready", "fail"):
            return a
        st = spine.find(v.recipe()[1], a["stage"])
        if a["state"] == "pass":
            spine.hand_off(v, a["stage"])
        elif a["state"] in ("ready", "fail", "running"):
            if a["state"] != "running":
                spine.run(v, a["stage"])
            make(v, st)
            spine.check(v, a["stage"])
        elif a["state"] == "waiting_owner":
            decide(v, host)
            spine.check(v, a["stage"])
        else:
            return a
    raise AssertionError("did not settle")


def record(v):
    return json.loads((v.folder / "records" / "run.json").read_text())


class Contract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.host = Host()

    def tearDown(self):
        self.tmp.cleanup()

    def video(self, recipe, name="v", **kw):
        return self.host.video(new_video(self.root, recipe, name), **kw)

    def test_next_is_one_reasoned_answer(self):
        posted, fresh = self.video("shorts-only", "posted"), self.video("plain", "fresh")
        drive(posted, self.host)
        for v in (posted, fresh):
            before = (v.folder / "records" / "run.json").read_bytes() if (v.folder / "records" / "run.json").exists() else None
            overview = [spine.next(x) for x in (posted, fresh)]
            self.assertEqual(spine.next(v), overview[[posted, fresh].index(v)])
            after = (v.folder / "records" / "run.json").read_bytes() if (v.folder / "records" / "run.json").exists() else None
            self.assertEqual(before, after)
        self.assertEqual(spine.next(posted), {"stage": None, "actor": None, "why": "posted", "state": "posted",
                                              "inputs_missing": []})
        self.assertEqual(spine.next(fresh), {"stage": "media", "actor": "editor", "why": "ready", "state": "ready",
                                             "inputs_missing": []})
        (fresh.folder / "records" / "job.json").write_text(json.dumps({"recipe": "op-ed", "id": "fresh"}))
        with self.assertRaisesRegex(spine.SpineError, "unknown recipe 'op-ed'"):
            spine.next(fresh)

    def test_recipe_edit_changes_data_only(self):
        recipes = spine.data("recipes.json")
        edited = copy.deepcopy(recipes)
        edited["shorts-only"]["stages"][1]["check"] = ["captions-read", "titles"]
        v = self.video("shorts-only", recipes=edited)
        drive(v, self.host, until="release")
        self.assertIn("titles", self.host.seen)
        bad = [("duplicate id", lambda r: r["stages"].insert(1, dict(r["stages"][1]))),
               ("made by a later stage", lambda r: r["stages"][1]["inputs"].append("records/release.json")),
               ("unsafe path", lambda r: r["stages"][0]["outputs"].append("../elsewhere.mp4")),
               ("neither a registered role", lambda r: r["stages"][0].update(run={"role": "intern"})),
               ("unknown check", lambda r: r["stages"][0]["check"].append("vibes")),
               ("made by no stage", lambda r: r["stages"][0]["inputs"].append("records/nobody.json")),
               ("next_actor", lambda r: r["stages"][0].update(next_actor="writer")),
               ("decision check is an owner stage's only", lambda r: r["stages"][1].update(check=["decision"])),
               ("engine's own record", lambda r: r["stages"][0]["outputs"].append("records/run.json")),
               ("unsafe path", lambda r: r["stages"][0]["outputs"].append("records/./run.json")),
               ("no check or no output", lambda r: r["stages"][3].update(outputs=[])),
               # APFS ignores case and Unicode form: each of these is a second name for one file
               ("engine's own record", lambda r: r["stages"][0]["outputs"].append("records/Run.json")),
               ("engine's own record", lambda r: r["external"].append("records/holds.json")),
               ("already made by short", lambda r: r["stages"][1]["outputs"].append("records/Words.json")),
               ("already made by the job", lambda r: r["external"].append("Source.mp4")),
               ("already made by short", lambda r: r["stages"][0]["outputs"].extend(["records/caf\u00e9.json",
                                                                                      "records/cafe\u0301.json"]))]
        for words, edit in bad:
            r = copy.deepcopy(recipes)
            edit(r["shorts-only"])
            with self.assertRaisesRegex(spine.SpineError, f"recipe shorts-only .*{words}"):
                self.host.video(v.folder, recipes=r)
        cat = spine.data("catalog.json")  # guard: a review the check does not read would never be required
        cat["checks"]["captions-read"]["reads"].remove("records/copy_review.md")
        self.assertRaisesRegex(spine.SpineError, "does not read", self.host.video, v.folder, catalog=cat)
        Path(self.root, "aside.json").write_text(json.dumps({"holds": []}))
        (v.folder / "records" / "holds.json").symlink_to(Path(self.root, "aside.json"))  # a maker's file standing in for an engine record
        with self.assertRaisesRegex(spine.SpineError, "holds.json is a symlink"):
            spine.next(v)
        (v.folder / "records" / "holds.json").unlink()
        os.rename(v.folder / "Shorts", Path(self.root, "Shorts"))
        (v.folder / "Shorts").symlink_to(Path(self.root, "Shorts"))
        with self.assertRaisesRegex(spine.SpineError, "Shorts is a symlink"):
            spine.next(v)

    def test_check_never_reads_recipe(self):
        plain, short = self.video("plain", "p"), self.video("shorts-only", "s")
        for v in (plain, short):
            drive(v, self.host, until="copy")
            spine.run(v, "copy")
            make(v, spine.find(v.recipe()[1], "copy"))
            self.assertTrue(spine.check(v, "copy")["ok"])
        a, b = self.host.seen["captions-read"][-2:]
        self.assertEqual({p: e["sha256"] for p, e in a.items()}, {p: e["sha256"] for p, e in b.items()})
        self.assertFalse([p for p in a if "job" in p or "recipe" in p])

    def test_absent_stage_never_asks(self):
        v = self.video("shorts-only")
        for stray in ("records/paper.md", "records/text_plan.md", "records/cut_plan2.json"):
            (v.folder / stray).write_text("left over from an old run")
        self.assertEqual(drive(v, self.host)["why"], "posted")
        ran = [s for s, _ in self.host.started] + [t["id"].split(":")[1] for t in self.host.asked]
        self.assertEqual(sorted(set(ran)), sorted(st["id"] for st in v.recipe()[1]["stages"]))
        self.assertEqual(sorted(record(v)["stages"]), sorted(st["id"] for st in v.recipe()[1]["stages"]))
        self.assertFalse(set(self.host.seen) & {"paper-read", "text-read", "titles", "delivered-media"})
        plain = self.video("plain", "p")
        self.assertFalse({"chat", "broll", "animation"} & {st["id"] for st in plain.recipe()[1]["stages"]})
        (plain.folder / "source.mp4").unlink()
        self.assertEqual(spine.next(plain)["inputs_missing"], ["source.mp4"])

    def test_every_stage_reruns(self):
        v = self.video("plain")
        drive(v, self.host)
        for st in v.recipe()[1]["stages"]:
            kept = (v.folder / "YouTube" / "main.mp4").read_bytes()
            history, attempt = len(record(v)["history"]), record(v)["stages"][st["id"]]["attempt"]
            spine.run(v, st["id"], rerun=True)
            self.assertEqual(record(v)["stages"][st["id"]]["attempt"], attempt + 1)
            self.assertEqual(spine.next(v)["stage"], st["id"])
            self.assertEqual(drive(v, self.host)["why"], "posted")
            self.assertGreater(len(record(v)["history"]), history)
            self.assertEqual((v.folder / "YouTube" / "main.mp4").read_bytes(), kept)
        spine.run(v, "arc", rerun=True)  # a rerun that makes the same bytes: the review still names them
        self.assertTrue(spine.check(v, "arc")["ok"])
        spine.hand_off(v, "arc")
        approvals = len(self.host.asked)
        spine.run(v, "arc", rerun=True)
        arc = spine.find(v.recipe()[1], "arc")
        make(v, arc)
        (v.folder / "Draft" / "arc.mp4").write_text("tighter")  # the maker overwrites what was reviewed
        self.assertIn("waiting on checker-plans", spine.check(v, "arc")["detail"]["words"][0])
        self.assertEqual((spine.next(v)["state"], spine.next(v)["actor"]), ("running", "checker-plans"))
        make(v, arc, mark="tighter")
        self.assertTrue(spine.check(v, "arc")["ok"])
        spine.hand_off(v, "arc")
        self.assertEqual(spine.next(v)["stage"], "arc-choice")
        self.assertEqual(spine.next(v)["state"], "ready")
        drive(v, self.host)
        self.assertGreater(len(self.host.asked), approvals)
        sent = {k for s, k in self.host.started if s == "posting"}
        spine.run(v, "posting", rerun=True)
        self.assertEqual({k for s, k in self.host.started if s == "posting"}, sent)
        os.rename(v.folder, Path(self.root, "moved"))  # the video's id, not its folder, keys every effect
        moved = self.host.video(Path(self.root, "moved"))
        spine.run(moved, "posting", rerun=True)
        self.assertEqual({k for s, k in self.host.started if s == "posting"}, sent)
        (moved.folder / "records" / "job.json").write_text(json.dumps({"recipe": "plain"}))
        with self.assertRaisesRegex(spine.SpineError, "no readable recipe and video id"):
            spine.run(moved, "posting", rerun=True)

    def test_failure_returns_exact_words_to_maker(self):
        self.host.verdicts["cut-plan"] = {"state": "fail", "words": ["kept a false start at 12.4 s"]}
        v = self.video("plain")
        drive(v, self.host, until="plan")
        spine.run(v, "plan")
        make(v, spine.find(v.recipe()[1], "plan"))
        self.assertEqual(spine.check(v, "plan")["state"], "fail")
        a = spine.next(v)
        self.assertEqual((a["stage"], a["actor"]), ("plan", "editor"))
        self.assertIn("cut-plan: kept a false start at 12.4 s", a["why"])
        r = record(v)["stages"]["plan"]
        self.assertEqual(r["words"], ["cut-plan: kept a false start at 12.4 s"])
        self.assertTrue(all(r["outputs"].values()))
        del self.host.verdicts["cut-plan"]
        drive(v, self.host, until="arc-choice")
        spine.run(v, "arc-choice")
        decide(v, self.host, "reject", "the open drags")
        self.assertEqual(spine.check(v, "arc-choice")["state"], "fail")
        self.assertEqual([spine.next(v)[k] for k in ("stage", "actor", "why")], ["arc", "editor", "owner reject: the open drags"])
        self.assertNotIn("handoff", record(v)["stages"]["arc"])  # no stale receipt tells of the rejected bytes
        with self.assertRaisesRegex(spine.SpineError, "run it again as a new attempt"):
            spine.check(v, "arc")  # the rejected bytes cannot pass again without a new attempt
        before, said = spine.run(v, "arc"), (v.folder / "records" / "arc_decision.json").read_text()
        for text in ("{", said):  # guard: the same send-back read again, even after a torn read, leaves the new attempt
            (v.folder / "records" / "arc_decision.json").write_text(text)
            spine.check(v, "arc-choice")
        self.assertEqual(record(v)["stages"]["arc"], before)

    def test_new_recipe_needs_no_engine_code(self):
        recipes = spine.data("recipes.json")
        third = copy.deepcopy(recipes["shorts-only"])
        third["stages"][0]["next_actor"] = "owner"
        third["stages"].insert(1, {"id": "short-choice", "inputs": ["Shorts/short.mp4"], "run": {"role": "owner"},
                                   "check": ["decision"], "outputs": ["records/short_decision.json"],
                                   "next_actor": "packager"})
        recipes["reviewed-short"] = third
        v = self.video("reviewed-short", recipes=recipes)
        self.assertEqual(drive(v, self.host)["why"], "posted")
        self.assertIn("short-choice", [t["id"].split(":")[1] for t in self.host.asked])

    def test_pass_hands_off_once(self):
        v = self.video("shorts-only")
        drive(v, self.host, until="copy")
        self.host.told.clear()
        spine.run(v, "copy")
        make(v, spine.find(v.recipe()[1], "copy"))
        spine.check(v, "copy")
        with self.assertRaisesRegex(spine.SpineError, "hand it off"):
            spine.run(v, "copy")
        self.host.fail_tell = self.host.crash_after = 1
        for _ in range(2):
            with self.assertRaises(OSError):
                spine.hand_off(v, "copy")
        self.assertEqual(spine.next(v)["state"], "pass")
        self.assertEqual(spine.next(v)["actor"], "packager")
        key = record(v)["stages"]["copy"]["handoff"]["key"]
        first, again = spine.hand_off(v, "copy"), spine.hand_off(v, "copy")
        self.assertEqual(first, again)
        self.assertEqual(self.host.told, [("packager", key)])
        self.assertEqual(record(v)["stages"]["copy"]["handoff"]["receipt"], f"told:{key}")

    def test_block_names_inputs(self):
        v = self.video("shorts-only")
        drive(v, self.host, until="copy")
        (v.folder / "records" / "publish.json").unlink()
        self.assertEqual(spine.next(v)["stage"], "short")
        (v.folder / "source.mp4").rename(v.folder / "aside.mp4")
        a = spine.next(v)
        self.assertEqual((a["state"], a["actor"], a["inputs_missing"]), ("blocked", "owner", ["source.mp4"]))
        spine.run(v, "short")
        self.assertEqual(record(v)["history"][-1]["state"], "blocked")
        self.assertIn("source.mp4", record(v)["history"][-1]["words"])
        (v.folder / "aside.mp4").rename(v.folder / "source.mp4")
        self.assertEqual(drive(v, self.host)["why"], "posted")
        w = self.video("shorts-only", "member")
        spine.run(w, "short")
        make(w, spine.find(w.recipe()[1], "short"))
        for said in (lambda ev: 1 / 0, {"state": "posted"}, {"state": "waiting_owner"}):  # not this check's answers
            self.host.verdicts["short-media"] = said
            self.assertEqual(spine.check(w, "short")["state"], "blocked")
            self.assertIn("check error", spine.next(w)["why"])
        (w.folder / "Shorts" / "short.mp4").write_text("changed after the manifest")
        del self.host.verdicts["short-media"]
        out = spine.check(w, "short")
        self.assertEqual(out["state"], "blocked")
        self.assertIn("Shorts/short.mp4", out["next_action"])
        (w.folder / "records" / "holds.json").write_text("{not json")
        self.assertEqual(spine.next(w)["state"], "blocked")
        self.assertIn("unreadable holds", spine.next(w)["why"])
        (w.folder / "records" / "holds.json").write_text(json.dumps({"holds": [{"stage": "short", "why": "wait for the embargo", "by": "owner"}]}))
        self.assertEqual(spine.next(w)["why"], "wait for the embargo")
        for broken in ("{broken", '{"stages": {}, "history": [], "at": NaN}'):
            (w.folder / "records" / "run.json").write_text(broken)
            with self.assertRaisesRegex(spine.SpineError, "unreadable stage record"):
                spine.next(w)

    def test_uncertain_send_blocks_until_reconciled(self):
        """docs/spine.md section 2: an uncertain external send stays blocked for receipt reconciliation (contract 9)."""
        v = self.video("shorts-only")
        drive(v, self.host, until="posting")
        start, tried = v.start, []
        v.start = lambda *a: tried.append(a[-1]) or (_ for _ in ()).throw(OSError("crash mid-send"))
        with self.assertRaises(OSError):
            spine.run(v, "posting")
        op = record(v)["stages"]["posting"]["op"]
        a = spine.next(v)
        self.assertEqual((a["stage"], a["state"], a["actor"]), ("posting", "blocked", "clock"))
        self.assertIn(op, a["why"])
        spine.run(v, "release-choice", rerun=True)  # the owner approves again: the send's inputs change
        decide(v, self.host, stage="release-choice")
        spine.check(v, "release-choice")
        spine.hand_off(v, "release-choice")
        self.assertEqual(spine.next(v)["state"], "blocked")
        with self.assertRaises(OSError):
            spine.run(v, "posting")
        self.assertEqual(tried, [op, op])  # only the uncertain send's own key, never a new one
        v.start = start
        self.assertEqual(spine.run(v, "posting")["receipt"], f"due 2026-10-23T09:00Z:{op}")
        self.assertEqual(spine.next(v)["state"], "ready")  # reconciled: now a new attempt may send the new inputs
        self.assertNotEqual(spine.run(v, "posting")["op"], op)

    def test_owner_wait_is_one_board_task(self):
        v = self.video("plain")
        drive(v, self.host, until="arc-choice")
        spine.run(v, "arc-choice")
        spine.run(v, "arc-choice")
        self.assertEqual(len(self.host.asked), 1)
        t = self.host.asked[0]
        self.assertEqual((t["kind"], t["judge"]["type"], t["judge"]["href"]), ("ask", "video", "/served/Draft/arc.mp4"))
        self.assertEqual((t["source"]["filed_by"], t["cant"]["goes_to"]), ("editor", "editor"))
        self.assertTrue(all(a["undo"]["verb"] for a in t["actions"]))
        decide(v, self.host, "cant", "I can't watch it on this phone")
        self.assertEqual(spine.check(v, "arc-choice")["state"], "waiting_owner")
        self.assertEqual(spine.next(v)["why"], "I can't watch it on this phone")
        self.assertEqual(record(v)["stages"]["arc-choice"]["by"], "editor")
        self.assertEqual(spine.next(v)["actor"], "editor")
        self.assertEqual(spine.check(v, "arc-choice")["next_action"], "editor answers the owner: I can't watch it on this phone")
        decide(v, self.host, "redo", "oops")
        spine.check(v, "arc-choice")
        self.assertEqual(spine.next(v)["stage"], "arc")
        decide(v, self.host, "undo", stage="arc-choice")
        spine.check(v, "arc-choice")
        self.assertEqual((spine.next(v)["stage"], spine.next(v)["state"]), ("arc-choice", "waiting_owner"))
        self.assertEqual(len(self.host.asked), 1)
        ids = [t["id"]]
        for verb in ("reject", "redo", "not-this", "try-again"):
            decide(v, self.host, verb, "again")
            self.assertEqual(spine.check(v, "arc-choice")["state"], "fail")
            self.assertEqual(spine.next(v)["actor"], "editor")
            drive(v, self.host, until="arc-choice")
            spine.run(v, "arc-choice")
            ids.append(self.host.asked[-1]["id"])
            (v.folder / "records" / "arc_decision.json").write_text(json.dumps({"task": ids[-2], "verb": "approve"}))
            self.assertEqual(spine.check(v, "arc-choice")["state"], "waiting_owner")
        self.assertEqual(len(set(ids)), 5)
        decide(v, self.host)
        self.assertTrue(spine.check(v, "arc-choice")["ok"])
        decide(v, self.host, "undo")  # before the cut is locked, an approval is undone
        self.assertEqual(spine.check(v, "arc-choice")["state"], "waiting_owner")

    def test_posted_needs_all_receipts(self):
        """Whether every release row has its receipt is the posting domain's posted check (docs/spine.md section 2);
        the engine ends at posted only on that check's posted answer over the receipts on disk now."""
        v = self.video("shorts-only")
        drive(v, self.host, until="posting")
        spine.run(v, "posting")
        self.assertIn("due 2026-10-23T09:00Z", spine.next(v)["why"])  # approved and queued is not posted
        rows = v.folder / "records" / "posting.json"
        rows.write_text("a posted, b out")
        for said, want in (("running", "running"), ("fail", "fail"), ("blocked", "blocked"), ("unsure", "blocked"),
                           ("posted", "posted")):
            self.host.verdicts["posted"] = {"state": said, "words": ["b " + said]}
            if record(v)["stages"]["posting"]["state"] == "fail":  # a failed send is retried as a new attempt
                spine.run(v, "posting")
            out = spine.check(v, "posting")
            self.assertEqual((out["state"], out["ok"]), (want, want == "posted"))
            self.assertEqual(spine.next(v)["stage"], None if want == "posted" else "posting")
        self.assertEqual(spine.next(v)["why"], "posted")
        decide(v, self.host, "undo", stage="release-choice")  # after posting started, the approval stands
        self.assertIn("undo refused: posting already started", spine.check(v, "release-choice")["detail"]["words"])
        self.assertEqual(spine.next(v)["why"], "posted")
        rows.write_text("a posted, b withdrawn")  # posted stands only on the receipts it was checked on
        self.assertEqual(spine.next(v)["stage"], "posting")

    def test_run_uses_next_guards(self):
        v = self.video("plain")
        with self.assertRaisesRegex(spine.SpineError, "next is media"):
            spine.run(v, "draft")
        with self.assertRaisesRegex(spine.SpineError, "next is media"):
            spine.run(v, "draft", rerun=True)
        (v.folder / "records" / "holds.json").write_text(json.dumps({"holds": [{"stage": "media", "why": "new mic", "by": "owner"}]}))
        self.assertEqual(spine.run(v, "media")["state"], "blocked")
        self.assertEqual(self.host.started, [])
        (v.folder / "records" / "holds.json").unlink()
        start, calls = self.host.start, []
        self.host.start = lambda *a: calls.append(a[-1]) or (_ for _ in ()).throw(OSError("crashed"))
        v.start = self.host.start
        with self.assertRaises(OSError):
            spine.run(v, "media")
        self.assertEqual(record(v)["stages"]["media"]["state"], "running")
        v.start = start
        spine.run(v, "media")
        spine.run(v, "media")
        self.assertEqual([k for _, k in self.host.started], calls)
        (v.folder / "records" / "holds.json").write_text(json.dumps({"holds": [{"stage": "media", "why": "new mic", "by": "owner"}]}))
        self.assertEqual(spine.run(v, "media")["state"], "blocked")
        (v.folder / "records" / "holds.json").unlink()
        self.assertEqual(spine.run(v, "media")["op"], calls[0])
        self.assertEqual([k for _, k in self.host.started], calls)

    def test_check_binds_the_bytes_it_checked(self):
        """docs/spine.md section 3 check: a pass names the bytes its checker saw."""
        v = self.video("plain")
        drive(v, self.host, until="plan")
        spine.run(v, "plan")
        plan = spine.find(v.recipe()[1], "plan")
        make(v, plan)
        self.host.verdicts["cut-plan"] = lambda ev: [(v.folder / "records" / "paper.md").write_text("edited"),
                                                     {"state": "pass", "words": []}][1]
        self.assertEqual(spine.check(v, "plan")["state"], "running")
        self.host.verdicts.pop("cut-plan")
        self.assertTrue(spine.check(v, "plan")["ok"])

    def test_check_revision_invalidates_result(self):
        v = self.video("shorts-only")
        drive(v, self.host, until="posting")
        approved = self.host.asked[-1]["id"]
        newer = self.host.video(v.folder, version="2")
        self.assertEqual(spine.next(newer)["stage"], "short")
        f = v.folder / "records" / "copy.json"
        stat = f.stat()
        f.write_text(f.read_text().replace("copy", "edit"))
        os.utime(f, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(spine.next(v)["stage"], "copy")
        self.assertIn("changed", spine.next(v)["why"])
        for sid in ("copy", "release"):  # the new copy makes a new release
            drive(v, self.host, until=sid)
            spine.run(v, sid)
            make(v, spine.find(v.recipe()[1], sid), mark="edit")
            spine.check(v, sid)
        self.assertEqual(drive(v, self.host)["why"], "posted")
        self.assertNotEqual(self.host.asked[-1]["id"], approved)
        approved, one = self.host.asked[-1]["id"], self.host.video(v.folder)
        one.checks["short-media"] = ("2", one.checks["short-media"][1])  # one upstream checker revised
        self.assertEqual(spine.next(one)["stage"], "short")
        self.assertEqual(drive(one, self.host, until="posting")["stage"], "posting")
        self.assertNotEqual(self.host.asked[-1]["id"], approved)


if __name__ == "__main__":
    unittest.main()
