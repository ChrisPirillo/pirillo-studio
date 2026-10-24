"""docs/preflight.md section 5, contracts 1 to 11: inputs to written records/ready/<stage>.json and the fakes' call
logs. Contract 12 (the host switch) is seam 3, in the host."""
import copy
import datetime as dt
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

from pirillo_studio.preflight import probe
from pirillo_studio.preflight.local import ffprobe_media

SIZE = {"bytes_per_s": 1e6}
STAGES = {
    "media": {"id": "media", "inputs": ["source.mp4", "records/job.json"], "run": {"role": "editor"},
              "check": ["media"], "outputs": ["records/words.json", "records/probe.json", "records/faces.json"],
              "next_actor": "editor", "tools": ["ffprobe", "ffmpeg", "asr:engine/model"], "scratch_gb": 0.1},
    "draft": {"id": "draft", "inputs": ["source.mp4", "records/cut_plan2.json", "records/story_map.json",
              "records/overlay_plan.json", "records/bed.json", "records/faces.json", "records/callouts_plan.json"],
              "run": {"role": "finisher"}, "check": ["draft-read"], "next_actor": "owner",
              "outputs": [{"path": "Draft/draft.mp4", "size": SIZE}, "records/draft_review.json", "records/vplan.json"],
              "tools": ["ffmpeg", "ffprobe"], "scratch_gb": 1},
    "masters": {"id": "masters", "inputs": ["source.mp4", "records/cut_plan2.json"], "run": {"role": "finisher"},
                "check": ["delivered-media"], "next_actor": "packager", "tools": ["ffmpeg", "ffprobe"], "scratch_gb": 2,
                "outputs": [{"path": "YouTube/main.mp4", "size": {"bytes_per_s": 2e6}}, "YouTube/main.srt",
                            {"path": "Vertical/main.mp4", "size": SIZE}, {"path": "Shorts/short.mp4", "size": SIZE},
                            "records/final_manifest.json"]},
}
BY = {"source.mp4": "intake", "records/job.json": "intake", "records/cut_plan2.json": "plan",
      "records/story_map.json": "locked", "records/overlay_plan.json": "overlays", "records/bed.json": "overlays",
      "records/faces.json": "media", "records/callouts_plan.json": "overlays"}
DETAIL = {"container": "mov,mp4", "streams": {"video": 1, "audio": 1}, "duration_s": 100.0, "fps_measured": 30.0686,
          "fps_tagged": "30/1", "audio_rate_hz": 48000, "width": 3840, "height": 2160, "packets_read": "all",
          "decoded": ["0-2s", "49-51s", "98-100s"], "color": {"color_primaries": None}, "audio_gap_s": 0.02}
CFG = {"disk": {"render_floor_gb": 40}, "media": {"max_audio_gap_s": 0.1}}
AT = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)


class Host:
    """Fakes for every effect, each call logged."""

    def __init__(self):
        self.calls, self.board = [], {}
        self.caps = {t: {"state": "qualified", "next_action": None, "detail": "found", "key": "sha256:" + t,
                         "resolved": {"version": "7.1"}} for t in ("ffprobe", "ffmpeg", "asr:engine/model")}
        self.qualified, self.served, self.voices = {}, {}, {}
        self.media, self.free, self.disk_error = dict(DETAIL), {"work": 500e9, "out": 500e9}, None

    def capability(self, t):
        self.calls.append(("capability", t))
        return dict(self.caps[t])

    def qualify(self, t):
        self.calls.append(("qualify", t))
        return dict(self.qualified[t])

    def voice(self, part, cites):
        self.calls.append(("voice", part, cites))
        return dict(self.voices[part])

    def probe_media(self, path):
        self.calls.append(("probe_media", os.path.basename(path)))
        if isinstance(self.media, Exception):
            raise self.media
        return copy.deepcopy(self.media)

    def volume(self, path):
        self.calls.append(("volume", path))
        if self.disk_error:
            raise self.disk_error
        name = "out" if os.sep + "YouTube" + os.sep in path else "work"
        return name, self.free[name]

    def now(self):
        return AT

    def bump(self, t, version):
        """A new install: its key and version change, its words stay."""
        self.caps[t].update(key=f"sha256:{t}@{version}", resolved={"version": version})

    def serve_words(self, tid, words):
        self.served["/served/" + tid] = words
        return "/served/" + tid

    def file_task(self, task):
        self.calls.append(("file_task", task["id"]))
        self.board[task["id"]] = task

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.dir)
        self.host, self.cfg, self.maker = Host(), copy.deepcopy(CFG), []
        for rel in BY:
            self.put(rel, rel)

    def put(self, rel, text):
        full = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)

    def call(self, sid="media", stage=None, passed=None, revision="sha256:rev"):
        stage = stage or STAGES[sid]
        return probe.preflight(self.dir, stage, self.cfg, self.host, revision,
                               passed or {r: {"by": BY[r], "sha256": None} for r in stage["inputs"]})

    def run_stage(self, sid="media", **kw):
        """The spine's run as these contracts need it: the maker starts only on a ready record, once per key."""
        self.call(sid, **kw)
        rec = self.record(sid)
        if rec["state"] == "ready" and rec["key"] not in self.maker:
            self.maker.append(rec["key"])
        return rec

    def record(self, sid="media"):
        with open(os.path.join(self.dir, "records", "ready", sid + ".json")) as f:
            return json.load(f)

    def hashes(self, sid):
        out = {}
        for root, _, names in os.walk(self.dir):
            for n in names:
                p = os.path.join(root, n)
                if os.path.relpath(p, self.dir) != os.path.join("records", "ready", sid + ".json"):
                    with open(p, "rb") as f:
                        out[os.path.relpath(p, self.dir)] = hashlib.sha256(f.read()).hexdigest()
        return out

    def ends(self):
        """Each non-ready end, set up on a fresh host: (name, stage id, setup)."""
        def missing():
            os.remove(os.path.join(self.dir, "records", "cut_plan2.json"))
        return [("input", "draft", missing),
                ("tool", "media", lambda: self.host.caps.update({"ffmpeg": {"state": "missing", "next_action": "install ffmpeg", "detail": "not found"}})),
                ("broken", "media", lambda: setattr(self.host, "media", probe.BrokenMedia("moov atom not found"))),
                ("disk", "draft", lambda: self.host.free.update(work=33e9))]


class Ready(Case):
    """Contract 1, section 3's ready end."""

    def test_ready_starts_the_maker(self):
        rec = self.run_stage()
        self.assertEqual((rec["ok"], rec["state"], rec["next_action"]), (True, "ready", "run media"))
        self.assertEqual([p["probe"] for p in rec["probes"]], ["inputs", "tools", "media", "disk"])
        self.assertTrue(all(p["state"] == "ready" for p in rec["probes"]))
        self.assertEqual(rec["probes"][2]["detail"]["source.mp4"], DETAIL)
        self.assertEqual(rec["blocks"], [])
        self.assertEqual(len(self.maker), 1)
        again = self.run_stage()
        self.assertEqual((again["key"], len(self.maker), self.host.count("probe_media")), (rec["key"], 1, 1))


class NothingExpensive(Case):
    """Contract 2: no maker and no declared output on any end but ready."""

    def test_nothing_expensive_starts_unless_ready(self):
        for name, sid, setup in self.ends():
            with self.subTest(name):
                self.setUp()
                setup()
                outs = [os.path.join(self.dir, o if isinstance(o, str) else o["path"]) for o in STAGES[sid]["outputs"]]
                before = [o for o in outs if os.path.exists(o)]
                rec = self.run_stage(sid)
                self.assertNotEqual(rec["state"], "ready")
                self.assertEqual(self.maker, [])
                self.assertEqual([o for o in outs if os.path.exists(o)], before)


class MissingInput(Case):
    """Contract 3, section 3's blocked end for an input."""

    def test_missing_input_blocks_naming_the_path(self):
        os.remove(os.path.join(self.dir, "records", "cut_plan2.json"))
        rec = self.run_stage("draft")
        self.assertEqual((rec["state"], rec["blocks"]), ("blocked", [{"probe": "inputs", "what": "records/cut_plan2.json"}]))
        self.assertIn("run plan", rec["next_action"])
        self.assertEqual([p["state"] for p in rec["probes"]], ["blocked", "not_reached", "not_reached", "not_reached"])
        self.put("records/cut_plan2.json", "plan")
        stale = {r: {"by": BY[r], "sha256": "sha256:other" if r == "records/bed.json" else None}
                 for r in STAGES["draft"]["inputs"]}
        rec = self.run_stage("draft", passed=stale)
        self.assertEqual((rec["state"], rec["blocks"][0]["what"]), ("blocked", "records/bed.json"))
        self.assertIn("run overlays again", rec["next_action"])
        if os.geteuid():
            os.chmod(os.path.join(self.dir, "records", "faces.json"), 0)
            rec = self.run_stage("draft")
            self.assertEqual((rec["state"], rec["blocks"][0]["what"]), ("blocked", "records/faces.json"))
            self.assertIn("unreadable", rec["detail"])
        self.assertEqual(self.host.count("probe_media"), 0)


class ShortDisk(Case):
    """Contract 4, section 3's blocked end for the disk."""

    def test_short_disk_blocks_naming_gigabytes(self):
        self.host.free["work"] = 33e9
        rec = self.run_stage("draft")
        self.assertEqual(rec["state"], "blocked")
        self.assertEqual(rec["blocks"], [{"probe": "disk", "what": "work volume", "short_gb": 8.1}])
        self.assertEqual(rec["next_action"], "free 8.1 GB on the work volume, then run draft again")
        self.assertEqual(rec["probes"][3]["detail"]["volumes"],
                         [{"volume": "work", "need_gb": 1.1, "margin_gb": 40, "free_gb": 33.0}])
        self.cfg, self.host.free = dict(CFG, disk={"render_floor_gb": 1}), {"work": 500e9, "out": 0.1e9}
        rec = self.run_stage("masters")
        self.assertEqual([(v["volume"], v["need_gb"]) for v in rec["probes"][3]["detail"]["volumes"]],
                         [("out", 0.2), ("work", 2.2)])
        self.assertEqual(rec["blocks"], [{"probe": "disk", "what": "out volume", "short_gb": 1.1}])
        self.host.disk_error = OSError("no such volume")
        self.assertEqual(self.run_stage("draft")["state"], "blocked")
        self.host.disk_error = None
        for margin in ({}, {"render_floor_gb": None}, {"render_floor_gb": "40"}, {"render_floor_gb": float("nan")}):
            self.cfg = dict(CFG, disk=margin)
            rec = self.run_stage("draft")
            self.assertEqual((rec["state"], rec["blocks"]), ("blocked", [{"probe": "disk", "what": "disk.render_floor_gb"}]))
        self.assertEqual(self.maker, [])


class MissingTool(Case):
    """Contract 5, section 3's blocked end for a tool."""

    def test_missing_tool_blocks_naming_the_tool(self):
        self.host.caps["asr:engine/model"] = {"state": "missing", "next_action": "install the model", "detail": "no weights"}
        rec = self.run_stage()
        self.assertEqual((rec["state"], rec["blocks"]), ("blocked", [{"probe": "tools", "what": "asr:engine/model"}]))
        self.assertEqual(rec["next_action"], "install the model")
        self.assertEqual([c[1] for c in self.host.calls if c[0] == "capability"], ["ffprobe", "ffmpeg", "asr:engine/model"])
        self.assertEqual(self.host.count("probe_media"), 0)
        self.host.caps["asr:engine/model"] = {"state": "unqualified", "next_action": "qualify asr:engine/model", "detail": ""}
        self.host.qualified["asr:engine/model"] = {"state": "unqualified", "next_action": "the owner checks it", "detail": "probe failed"}
        rec = self.run_stage()
        self.assertEqual((rec["state"], rec["blocks"][0]["what"], self.host.count("qualify")), ("blocked", "asr:engine/model", 1))
        self.host.qualified["asr:engine/model"] = {"state": "qualified", "next_action": None, "detail": "model present",
                                                   "key": "sha256:model"}
        self.assertEqual(self.run_stage()["state"], "ready")
        self.host.media = probe.ToolFailed("ffprobe", "exec failed")
        self.host.bump("ffprobe", "7.2")   # a new key, so the media probe runs
        rec = self.run_stage()
        self.assertEqual((rec["state"], rec["blocks"][0]["what"]), ("blocked", "ffprobe"))
        self.assertEqual(self.host.board, {})
        bare = dict(STAGES["media"], tools=["ffmpeg"])   # guard: media read with no declared probe tool to key it
        self.assertEqual(self.call(stage=bare)["blocks"], [{"probe": "tools", "what": "ffprobe"}])


class VoiceTake(Case):
    """defect: item 6 unpark review r11 (Codex): a declared voice:speech went to capability() and raised, and a patch's
    cites never reached voice(), so a take with no owner pick could not wait on the owner (preflight.md section 2)."""

    def test_voice_waits_on_the_owner_and_keys_the_pick(self):
        stage = dict(STAGES["draft"], tools=["ffmpeg", "ffprobe", "voice:speech"])
        cites = {"take": "records/take.wav", "original": "records/original.wav"}
        passed = {r: {"by": BY[r], "sha256": None} for r in stage["inputs"]}
        self.host.voices["speech"] = {"state": "waiting_owner", "next_action": "the owner picks the take on the Board",
                                      "detail": "capabilities:take:1", "key": "sha256:sel", "capability_key": "sha256:eng"}
        rec = probe.preflight(self.dir, stage, self.cfg, self.host, "sha256:rev", passed, cites)
        self.assertEqual((rec["state"], rec["blocks"], rec["next_action"]),
                         ("waiting_owner", [{"probe": "tools", "what": "voice:speech"}], "the owner picks the take on the Board"))
        self.assertIn(("voice", "speech", cites), self.host.calls)
        self.assertNotIn(("capability", "voice:speech"), self.host.calls)
        self.assertEqual(self.host.count("probe_media"), 0)
        keys = []
        for take in ("take a picked", "take b picked"):   # a new take picked is a new answer
            self.host.voices["speech"] = {"state": "qualified", "next_action": None, "detail": take,
                                          "key": "sha256:sel", "capability_key": "sha256:eng"}
            rec = probe.preflight(self.dir, stage, self.cfg, self.host, "sha256:rev", passed, cites)
            self.assertEqual(rec["state"], "ready")
            keys.append(rec["key"])
        self.assertNotEqual(*keys)


class Broken(Case):
    """Contract 6, section 3's broken end and its one Board task."""

    def test_broken_media_reaches_the_owner_once(self):
        cases = [probe.BrokenMedia("moov atom not found"), dict(DETAIL, streams={"video": 1, "audio": 0}),
                 dict(DETAIL, duration_s=None), dict(DETAIL, fps_measured=None), dict(DETAIL, audio_rate_hz=None),
                 dict(DETAIL, audio_gap_s=0.5), dict(DETAIL, duration_s=float("nan"))]
        for media in cases:
            with self.subTest(str(media)[:40]):
                self.setUp()
                self.host.media = media
                rec = self.run_stage()
                self.assertEqual((rec["state"], rec["blocks"][0]["what"]), ("broken", "source.mp4"))
                self.assertTrue(rec["blocks"][0]["why"])
                self.assertEqual(list(self.host.board), [rec["task"]])
                task = self.host.board[rec["task"]]
                self.assertEqual((task["kind"], task["loop"], task["source"]),
                                 ("ask", "filed", {"record": "records/ready/media.json", "filed_by": "preflight"}))
                self.assertEqual(task["why"], rec["blocks"][0]["why"])
                self.assertEqual(json.loads(self.host.served[task["judge"]["href"]]), rec["probes"][2])
                self.assertEqual(self.run_stage()["task"], rec["task"])
                self.assertEqual((self.host.count("file_task"), self.host.count("probe_media")), (1, 1))
        no_audio = dict(DETAIL, streams={"video": 1}, audio_rate_hz=None)   # guard: the cause, not its unknown field
        self.assertEqual(probe.media_problem(no_audio, 0.1), "no audio stream")
        os.remove(os.path.join(self.dir, "records", "ready", "media.json"))
        with mock.patch.object(probe, "_write", side_effect=OSError("killed")):
            self.assertRaises(OSError, self.call)
        self.run_stage()
        self.assertEqual(len(self.host.board), 1)
        self.put("source.mp4", "new take")
        self.run_stage()
        self.assertEqual(len(self.host.board), 2)


class NotReprobed(Case):
    """Contract 7: one media probe per key; disk and tools read every call."""

    def test_unchanged_inputs_are_not_reprobed(self):
        self.call()
        caps, vols = self.host.count("capability"), self.host.count("volume")
        self.call()
        self.assertEqual(self.host.count("probe_media"), 1)
        self.assertEqual((self.host.count("capability"), self.host.count("volume")), (2 * caps, 2 * vols))
        self.host.caps["ffprobe"]["detail"] = "found, other words"   # guard: words are not the install
        self.call()
        self.assertEqual(self.host.count("probe_media"), 1)
        self.put("records/job.json", "changed")
        self.call()
        self.assertEqual(self.host.count("probe_media"), 2)
        self.host.bump("ffmpeg", "7.2")
        self.call()
        self.assertEqual(self.host.count("probe_media"), 3)
        del self.host.caps["ffmpeg"]["key"]   # guard: a status with no key cannot stand for an install
        self.assertEqual(self.call()["blocks"], [{"probe": "tools", "what": "ffmpeg"}])
        self.host.bump("ffmpeg", "7.2")
        with mock.patch.object(probe, "VERSION", "2"):
            self.call()
        self.assertEqual(self.host.count("probe_media"), 4)


class OwnerFiles(Case):
    """Contract 8: only records/ready/<stage>.json is written, on every end."""

    def test_never_changes_owner_files(self):
        for name, sid, setup in self.ends() + [("ready", "media", lambda: None)]:
            with self.subTest(name):
                self.setUp()
                setup()
                before = self.hashes(sid)
                self.call(sid)
                self.assertEqual(self.hashes(sid), before)


class Crash(Case):
    """Contract 9: a crash between probe and write, or mid-write, reaches one well-formed record."""

    def test_crash_reaches_one_end(self):
        with mock.patch.object(probe, "_write", side_effect=OSError("killed")):
            self.assertRaises(OSError, self.call)
        self.assertEqual(self.record_or_none(), None)
        path = os.path.join(self.dir, "records", "ready", "media.json")
        os.makedirs(os.path.dirname(path))
        for name in (path, path + ".tmp"):
            with open(name, "w") as f:
                f.write('{"ok": true, "state": "ready", "preflight_version": "1", "pro')
        self.assertIsNone(probe._read(path))
        rec = self.run_stage()
        self.assertEqual((rec["state"], self.host.count("probe_media")), ("ready", 2))
        self.assertEqual(os.listdir(os.path.dirname(path)), ["media.json"])

    def record_or_none(self):
        return probe._read(os.path.join(self.dir, "records", "ready", "media.json"))


class NoRecipe(Case):
    """Contract 10: the probe sees a stage, never which recipe it came from."""

    def test_preflight_never_reads_recipe(self):
        recipes = {name: {"stages": [copy.deepcopy(STAGES["media"])]} for name in ("plain", "shorts-only")}
        out = []
        for name, recipe in recipes.items():
            self.setUp()
            rec = self.call(stage=recipe["stages"][0], revision="sha256:" + str(len(out)))
            out.append({k: v for k, v in rec.items() if k not in ("key", "recipe_revision", "files")})   # files: inodes
            text = json.dumps([rec, self.host.calls, self.host.board])
            self.assertNotIn("plain", text)
            self.assertNotIn("shorts-only", text)
        self.assertEqual(out[0], out[1])


class Unknown(Case):
    """Contract 11: unknown stays unknown; the record never carries a default."""

    def test_unknown_values_are_never_defaulted(self):
        for field in ("duration_s", "fps_measured", "fps_tagged", "audio_rate_hz", "width", "height", "audio_gap_s"):
            self.host.media = dict(DETAIL, **{field: None})
            self.host.bump("ffprobe", field)   # a new key each time
            rec = self.run_stage()
            self.assertEqual(rec["state"], "broken", field)
            self.assertIn(field, rec["blocks"][0]["why"])
        self.host.media = dict(DETAIL)
        self.host.bump("ffprobe", "7.1")
        rec = self.run_stage()
        self.assertEqual(rec["probes"][2]["detail"]["source.mp4"]["color"], {"color_primaries": None})
        stage = copy.deepcopy(STAGES["draft"])
        stage["outputs"][0] = "Draft/draft.mp4"
        rec = self.run_stage("draft", stage=stage)
        self.assertEqual((rec["state"], rec["blocks"]), ("blocked", [{"probe": "disk", "what": "Draft/draft.mp4"}]))
        info = {"format": {"format_name": "mov,mp4"}, "streams": [
            {"index": 0, "codec_type": "video", "r_frame_rate": "0/0", "width": 3840, "height": 2160},
            {"index": 1, "codec_type": "audio"}]}
        replies = iter([(0, json.dumps(info), ""), (0, "0,0.0,N/A,K_\n0,0.5,N/A,__\n1,0.0,0.02\n", "")])
        d = ffprobe_media("ffprobe", "source.mp4", run=lambda argv: next(replies))
        self.assertEqual((d["duration_s"], d["fps_tagged"], d["audio_rate_hz"], d["color"]["color_space"]),
                         (None, None, None, None))
        self.assertEqual(probe.media_problem(d, 0.1), "unknown duration_s")
        for gap in (None, float("nan"), -1):   # no limit is no answer
            self.cfg = dict(CFG, media={"max_audio_gap_s": gap})
            rec = self.run_stage()
            self.assertEqual((rec["state"], rec["blocks"]), ("blocked", [{"probe": "media", "what": "media.max_audio_gap_s"}]))
        self.cfg, self.host.free = copy.deepcopy(CFG), {"work": float("nan"), "out": 500e9}
        self.assertEqual(self.run_stage("draft")["blocks"], [{"probe": "disk", "what": "Draft/draft.mp4"}])

    def test_every_non_data_file_is_media(self):
        """guard: a declared file of a kind the probe has no name for is probed and sized, never passed unread."""
        stage = dict(copy.deepcopy(STAGES["draft"]), inputs=["source.avi", "records/cut_plan2.json"])
        self.put("source.avi", "footage")
        rec = probe.preflight(self.dir, stage, self.cfg, self.host, "sha256:rev",
                              {r: {"by": "intake", "sha256": None} for r in stage["inputs"]})
        self.assertEqual(rec["probes"][2]["inputs"], ["source.avi"])
        self.assertEqual(self.host.count("probe_media"), 1)
        stage["outputs"][0] = "Draft/draft.webm"
        rec = probe.preflight(self.dir, stage, self.cfg, self.host, "sha256:rev",
                              {r: {"by": "intake", "sha256": None} for r in stage["inputs"]})
        self.assertEqual(rec["blocks"], [{"probe": "disk", "what": "Draft/draft.webm"}])

