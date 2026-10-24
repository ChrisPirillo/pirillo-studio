"""Resolve immutable rendering inputs, separating byte freshness from media identity."""
import copy
import re
from fractions import Fraction
from pathlib import Path

from ..cutting import checks, plan as cutting
from ..spine.files import SpineError, canon, digest, inside, load, safe
from . import records

RENDERER_VERSION = "rendering-2f-1"
_OUTPUTS = {"arc": ("arc", "Draft/arc.mp4"), "draft": ("draft", "Draft/draft.mp4"),
            "long": ("masters", "YouTube/main.mp4"), "vertical": ("masters", "Vertical/main.mp4")}
_PATH_FIELDS = {"file", "path", "record", "asset", "stem", "audio", "image", "font", "lut", "source", "voice"}
_IGNORED = {"title", "display_title", "locked_at", "note"}


def settings():
    return load(Path(__file__).parent, "settings.json")


def _hash(value, at):
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise SpineError(f"{at}: missing or invalid content hash")
    return value


class Inputs:
    def __init__(self, video):
        self.video, self.hashes, self.active = video, {}, set()

    def add(self, name):
        records.path(self.video, name)
        value = cutting.hashes(self.video, [name])[name]
        if name in self.hashes and self.hashes[name] != value:
            raise SpineError(f"{name}: changed while resolving request")
        self.hashes[name] = value
        return value

    def read(self, name):
        self.add(name)
        value = cutting.read(self.video, name)
        canon(value)
        self.add(name)
        return value

    def content(self, name):
        safe(name, "render input")
        if name in self.active:
            raise SpineError(f"{name}: circular asset reference")
        actual = self.add(name)
        if not name.endswith(".json"):
            return {"bytes": actual}
        self.active.add(name)
        try:
            return {"content": self.normalize(self.read(name))}
        finally:
            self.active.remove(name)

    def normalize(self, value, field=""):
        if isinstance(value, list):
            return [self.normalize(item, field) for item in value]
        if isinstance(value, dict):
            refs = [v for k, v in value.items() if k in _PATH_FIELDS and isinstance(v, str)]
            if len(refs) > 1 and any(k in value for k in ("sha", "sha256", "file_sha256")):
                raise SpineError("asset hash: ambiguous file references")
            if len(refs) == 1:
                for label in ("sha", "sha256", "file_sha256"):
                    if label in value and value[label] != self.add(refs[0]):
                        raise SpineError(f"{refs[0]}: {label} contradicts actual bytes")
            ignored = _IGNORED | ({"sha", "sha256", "file_sha256"} if len(refs) == 1 else set())
            return {key: self.normalize(item, field if field in {"assets", "stems"} else key)
                    for key, item in value.items() if key not in ignored}
        if isinstance(value, str) and (field in _PATH_FIELDS or field in {"assets", "stems"}):
            return self.content(value)
        return value


def _profile(video, kind, inputs):
    supplied = copy.deepcopy(video.settings)
    profile = supplied["profiles"][kind]
    if not isinstance(profile, dict) or not isinstance(profile["groups"], list):
        raise SpineError(f"rendering.settings#{kind}: groups must be a list")
    result = {"profile": kind}
    for group in profile["groups"]:
        if group in result:
            raise SpineError(f"rendering.settings#{kind}: duplicate group {group}")
        result[group] = copy.deepcopy(supplied["shared"][group])
    result.update({k: v for k, v in profile.items() if k not in {"groups", "note"}})
    result["checks"] = result["verification"]["checks"]
    for required in ("video", "audio", "encoder", "checks"):
        if required not in result:
            raise SpineError(f"rendering.settings#{kind}: missing {required}")
    if not isinstance(result["checks"], list) or not result["checks"]:
        raise SpineError(f"rendering.settings#{kind}: missing checks")
    if any(not isinstance(x, str) or not x for x in result["checks"]):
        raise SpineError(f"rendering.settings#{kind}: invalid checks")
    if len(set(result["checks"])) != len(result["checks"]):
        raise SpineError(f"rendering.settings#{kind}: duplicate checks")
    for key in ("fps", "codec", "pix_fmt"):
        cutting.text(result["video"][key], f"rendering.settings video.{key}")
    try:
        fps = Fraction(result["video"]["fps"])
    except (ValueError, ZeroDivisionError):
        raise SpineError("rendering.settings video.fps: expected positive rational") from None
    if fps <= 0:
        raise SpineError("rendering.settings video.fps: must be positive")
    if cutting.number(result["audio"]["sample_rate"], "rendering.settings audio.sample_rate") <= 0:
        raise SpineError("rendering.settings audio.sample_rate: must be positive")
    cutting.text(result["audio"]["codec"], "rendering.settings audio.codec")
    encoder = result["encoder"]
    cutting.text(encoder["engine"], "rendering.settings encoder.engine")
    if "preset" in encoder:
        cutting.text(encoder["preset"], "rendering.settings encoder.preset")
    if "crf" in encoder and cutting.number(encoder["crf"], "rendering.settings encoder.crf") < 0:
        raise SpineError("rendering.settings encoder.crf: negative quality")
    canon(result)
    inputs.normalize(result)
    return result


def _checked(video, inputs, settled):
    # Cutting owns eligibility; hashing its declared maker bytes is evidence, not a rendering plan read.
    required = ("records/cut_plan2.json", "records/words.json", "source.mp4", "records/job.json",
                "records/overlay_plan.json", "records/bed.json", "records/callouts_plan.json")
    record, current = checks._fresh(video, required)
    inputs.read("records/checks/cut.json")
    if current != settled:
        raise SpineError("records/cut_plan2.json: changed during cutting check")
    for path, expected in record["reads"].items():
        if inputs.add(path) != _hash(expected, path):
            raise SpineError(f"{path}: stale cutting check")
    return record


def _locked(inputs, settled, check):
    name = "records/story_map.json"
    lock = inputs.read(name)
    if lock["plan"] != settled["key"] or lock["check"] != check["key"]:
        raise SpineError(f"{name}: stale plan or check")
    ref, sep, expected = lock["arc_decision"].partition("#")
    if not sep or ref != "records/arc_decision.json" or inputs.add(ref) != _hash(expected, ref):
        raise SpineError(f"{name}: stale arc decision")
    decision, arc = inputs.read(ref), inputs.read("records/arc.json")
    if (decision["decision"] != "approve" or decision["plan"] != settled["key"]
            or decision["check"] != check["key"] or decision["arc"] != arc["key"]
            or arc["plan"] != settled["key"] or arc["check"] != check["key"]):
        raise SpineError(f"{name}: arc was not explicitly approved for current plan")
    required = {"records/overlay_plan.json", "records/bed.json", "records/callouts_plan.json",
                "records/shorts_plan.json", "records/shorts_spec.json"}
    if set(lock["reads"]) != required:
        raise SpineError(f"{name}: missing or contradictory locked reads")
    for path, expected in lock["reads"].items():
        if expected != check["reads"].get(path) or inputs.add(path) != expected:
            raise SpineError(f"{path}: stale story lock")
    order = [s["id"] for s in sorted(settled["spans"], key=lambda s: s["order"])]
    if ([s for block in lock["blocks"] for s in block["spans"]] != order
            or lock["tail"] != order[-1] or decision["blocks"] != lock["blocks"]):
        raise SpineError(f"{name}: blocks or tail contradict settled story order")
    for block in lock["blocks"]:
        cutting.text(block["why"], f"{name}: block why")
    return lock


def _short(inputs, settled, output):
    spec = inputs.read("records/shorts_spec.json")
    if spec["plan"] != settled["key"]:
        raise SpineError("records/shorts_spec.json: stale plan")
    _hash(spec["key"], "records/shorts_spec.json key")
    entries = spec["shorts"]
    ids = [s["id"] for s in entries]
    if len(ids) != len(set(ids)):
        raise SpineError("records/shorts_spec.json: duplicate short identity")
    chosen = [entry for entry in entries if entry.get("full") is True] if output == "vertical" else [
        entry for entry in entries if entry["id"] == output.split(":", 1)[1]]
    if len(chosen) != 1 or (output != "vertical" and chosen[0].get("full") is True):
        raise SpineError(f"records/shorts_spec.json: missing or contradictory {output}")
    selected = chosen[0]
    if output != "vertical":
        if not selected["pieces"]:
            raise SpineError(f"{output}: no settled pieces")
        for piece in selected["pieces"]:
            cutting.interval(piece, output)
        cutting.text(selected["point"], f"{output} point")
    return selected


def resolve(video, stage, output):
    """Return a content-bound request or refuse the named missing/contradictory input."""
    try:
        return _resolve(video, stage, output)
    except (OSError, KeyError, TypeError, AttributeError, ValueError) as error:
        if isinstance(error, SpineError):
            raise
        raise SpineError(f"render request {output}: {error}") from None


def _resolve(video, stage, output):
    inputs = Inputs(video)
    job = inputs.read("records/job.json")
    imported = stage == "short"
    if output.startswith("short:"):
        short_id = output.split(":", 1)[1]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", short_id) or short_id.endswith("-readings"):  # its own readings name
            raise SpineError(f"output: unsafe short identity {short_id!r}")
        kind, output_id, destination = "short", f"short-{short_id}", f"Shorts/{short_id}.mp4"
        expected_stage = "short" if imported else "masters"
    elif output in _OUTPUTS:
        kind, output_id = output, output
        expected_stage, destination = _OUTPUTS[output]
    else:
        raise SpineError(f"output: unknown {output!r}")
    if stage != expected_stage:
        raise SpineError(f"{output}: incompatible stage {stage}")
    if job["recipe"] != ("shorts-only" if imported else "plain"):
        raise SpineError("records/job.json: recipe contradicts render mode")
    source = job["source"] if imported else "source.mp4"
    if imported and job["short_id"] != short_id:
        raise SpineError("records/job.json: supplied short identity differs")
    source_hash = inputs.add(source)
    safe(destination, "render output")
    inside(video.folder, destination)
    if source == destination:
        raise SpineError(f"{destination}: destination is the supplied source")
    preflight = video.preflight
    if (preflight["ok"] is not True or preflight["state"] != "ready"
            or preflight["source_sha256"] != source_hash):
        raise SpineError("input preflight: not ready for current source bytes")
    versions = copy.deepcopy(video.versions)
    if not isinstance(versions, dict) or not versions:
        raise SpineError("tool versions: missing")
    for key, value in versions.items():
        cutting.text(key, "tool name")
        cutting.text(value, f"tool version {key}")
    profile = _profile(video, kind, inputs)
    if not imported:
        profile["checks"] = profile["checks"] + profile["verification"]["render_checks"] + profile["render_checks"]
        if stage == 'masters':
            profile['checks'] += profile['verification']['master_checks']
    result = {"output": output, "output_id": output_id, "stage": stage,
              "mode": "measure_existing" if imported else "render", "source": source,
              "source_sha256": source_hash, "file": destination, "plan_sha256": None,
              "plan_key": None, "settings": profile, "tools": versions}
    identity = {"output": output, "stage": stage, "mode": result["mode"], "source": source_hash,
                "settings": inputs.normalize(profile), "tools": versions, "renderer": RENDERER_VERSION}
    if not imported:
        settled = inputs.read("records/cut_plan2.json")
        _hash(settled["key"], "records/cut_plan2.json key")
        rows = cutting.words(inputs.read("records/words.json"))
        cutting.validate_plan(settled, rows)
        check = _checked(video, inputs, settled)
        result.update(plan="records/cut_plan2.json", plan_sha256=inputs.hashes["records/cut_plan2.json"],
                      plan_key=settled["key"], plan_data=settled, words_data=rows)
        if stage != "arc":
            result.update(lock="records/story_map.json", lock_data=_locked(inputs, settled, check))
        selected = _short(inputs, settled, output) if kind in {"short", "vertical"} else None
        result["selected"] = selected
        result["patches"] = {}
        for span in settled["spans"]:
            if "patch" in span:
                patch_name = span["patch"]["record"]
                patch = inputs.read(patch_name)
                if inputs.hashes[patch_name] != span['patch']['sha']:
                    raise SpineError(f'{patch_name}: stale patch record identity')
                result['patches'][patch_name] = patch
                if not any(isinstance(patch.get(k), str) for k in ("audio", "file", "path")):
                    raise SpineError(f"{patch_name}: missing actual patch audio path")
        identity["plan"] = inputs.normalize({k: settled[k] for k in ("spans", "cold_open", "end_hold_s")})
        identity["words"], identity["selected"] = inputs.normalize(rows), inputs.normalize(selected)
        for label, name in (("cards", "records/overlay_plan.json"), ("bed", "records/bed.json"),
                            ("faces", "records/faces.json")):
            data = inputs.read(name)
            if label != "faces" and data["plan"] != settled["key"]:
                raise SpineError(f"{name}: stale plan")
            result[label + "_data"] = data
            identity[label] = inputs.normalize({k: v for k, v in data.items() if k != "plan"})
    if not imported and video.native:
        from . import compositor
        identity['captions'] = compositor.caption_identity(video)
        profile['checks'] = list(dict.fromkeys(profile['checks'] + ['voice', 'captions', 'frame-map', 'cards']))
        identity['settings'] = inputs.normalize(profile)
    result['inputs'] = inputs.hashes
    if not imported and video.prepare is not None:
        from . import mapping
        identity['renderer_binding'] = mapping.bind(video, result, inputs)
    canon(result)
    if cutting.hashes(video, list(inputs.hashes)) != inputs.hashes:
        raise SpineError("render inputs changed while resolving")
    result.update(key=digest(canon(identity)), inputs=inputs.hashes)
    return result
