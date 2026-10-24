"""docs/preflight.md section 1's records/probe.json and media check (seam 3c), on contracts 1 to 11's fixture."""
import copy
import hashlib
import json
import os

from pirillo_studio.preflight import probe, spine_checks
from pirillo_studio.preflight.test_contract import Case


class ProbeRecord(Case):
    """Section 1's records/probe.json (seam 3c): one producer, the media ready record's projection, never a measure."""

    def source(self):
        return probe._sha(os.path.join(self.dir, "source.mp4"))

    def ready_raw(self):
        with open(os.path.join(self.dir, probe.READY), "rb") as f:
            return f.read()

    def test_probe_is_the_ready_records_projection(self):
        rec = self.run_stage()
        out = probe.write_probe(self.dir)
        with open(os.path.join(self.dir, "records", "probe.json")) as f:
            self.assertEqual(json.load(f), out)
        self.assertEqual(out, {"probe_version": "1", "source": self.source(), "ready": rec["key"],
                               "ready_sha256": "sha256:" + hashlib.sha256(self.ready_raw()).hexdigest(),
                               "duration_s": 100.0, "fps": 30.0686, "fps_tagged": "30/1", "sample_rate": 48000,
                               "width": 3840, "height": 2160, "color": {"color_primaries": None}})
        self.assertEqual(self.host.count("probe_media"), 1)
        good = json.loads(self.ready_raw())
        for why, edit in [("is blocked", lambda r: r.update(state="blocked")),
                          ("is broken", lambda r: r.update(state="broken")),
                          ("no current media ready record", lambda r: r.update(stage="draft")),
                          ("has no key", lambda r: r.update(key=None)),
                          ("another source.mp4", lambda r: r["files"]["source.mp4"].update(sha256="sha256:other")),
                          ("lacks fps_measured", lambda r: r["probes"][2]["detail"]["source.mp4"].update(fps_measured=None))]:
            with self.subTest(why):
                bad = copy.deepcopy(good)
                edit(bad)
                with self.assertRaisesRegex(ValueError, why):
                    probe.projection(json.dumps(bad).encode(), self.source())


class MediaCheck(Case):
    """Section 1's media catalog check: passes only on the exact projection of the current ready record."""

    def evidence(self):
        out = {}
        for p in ("source.mp4", probe.READY, "records/probe.json", "records/words.json", "records/faces.json"):
            path = os.path.join(self.dir, p)
            with open(path, "rb") as f:
                out[p] = {"path": path, "sha256": "sha256:" + hashlib.sha256(f.read()).hexdigest()}
        return out

    def words(self, answer):
        self.assertEqual(answer["state"], "blocked", answer)
        return " ".join(answer["words"])

    def test_media_check_names_the_differing_field(self):
        media = spine_checks.checks()["media"][1]
        self.put("records/words.json", "{}")
        self.put("records/faces.json", "{}")
        self.run_stage()
        probe.write_probe(self.dir)
        self.assertEqual(media(self.evidence()), {"state": "pass", "words": []})
        written = os.path.join(self.dir, "records", "probe.json")
        with open(written) as f:
            good = json.load(f)
        self.put("records/probe.json", json.dumps(dict(good, fps=30.0)))
        self.assertIn("records/probe.json fps is 30.0", self.words(media(self.evidence())))
        self.put("records/probe.json", json.dumps(good))
        self.put(probe.READY, json.dumps(json.loads(self.ready_raw()), indent=2))   # same key, other bytes
        said = self.words(media(self.evidence()))
        self.assertIn("ready_sha256", said)
        self.assertNotIn("probe.json ready is", said)
        self.put("records/job.json", "another job")   # a new key
        self.run_stage()
        self.assertIn("records/probe.json ready is", self.words(media(self.evidence())))
        probe.write_probe(self.dir)
        self.assertEqual(media(self.evidence())["state"], "pass")
        ev = self.evidence()
        self.put("records/faces.json", "{")
        self.assertIn("records/faces.json changed", self.words(media(ev)))
        self.assertIn("records/faces.json: Expecting", self.words(media(self.evidence())))
        os.remove(os.path.join(self.dir, "records", "faces.json"))
        self.assertIn("faces.json", self.words(media(ev)))
        self.put("records/faces.json", "{}")
        self.put("source.mp4", "other footage")
        self.assertIn("another source.mp4", self.words(media(self.evidence())))

    ready_raw = ProbeRecord.ready_raw
