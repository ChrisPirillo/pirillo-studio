"""Pure task-source reader, with source-declared actions; the host supplies native facts."""
import copy
import hashlib
import json
from urllib.parse import quote

from . import model

FIELDS = ("kind", "what", "why", "words", "actions", "filed_by", "related", "evidence")


def task(adapter, key, record):
    """Bind instructions, evidence and declared actions to one source revision, without executing writers."""
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"{key!r}: source key must be text")
    try:
        data = copy.deepcopy(adapter.describe(key, copy.deepcopy(record)))
        for field in FIELDS:
            data[field]
        if data["kind"] not in ("task", "crew task"):
            raise ValueError("kind must be task or crew task")
        for field in ("what", "why", "words", "filed_by"):
            if not isinstance(data[field], str) or not data[field].strip():
                raise ValueError(f"{field} must be text")
        if not isinstance(data["related"], list) or any(not isinstance(r, str) or not r.strip() for r in data["related"]):
            raise ValueError("related must be a list of source keys")
        evidence = data["evidence"]
        if not isinstance(evidence, list):
            raise ValueError("evidence must be a list")
        for n, part in enumerate(evidence):
            if not isinstance(part, dict) or any(not isinstance(part.get(f), str) or not part[f].strip()
                                                for f in ("type", "label", "role", "target")):
                raise ValueError(f"evidence {n} needs type, label, role and target")
            if not isinstance(part.get("identity"), dict) or not part["identity"]:
                raise ValueError(f"evidence {n} needs its content identity")
        content = {k: data[k] for k in ("what", "why", "words", "evidence")}
        revision = hashlib.sha256(json.dumps(content, sort_keys=True, allow_nan=False).encode()).hexdigest()
        href = "/board/content/" + quote(key, safe="") + "/" + revision
        judge = dict(type="words", href=href)
        if evidence:
            judge["parts"] = [dict(type=p["type"], label=p["label"], role=p["role"], href=f"{href}/evidence/{n}")
                              for n, p in enumerate(evidence)]
        actions = data["actions"]
        if not isinstance(actions, list) or not actions:
            raise ValueError("actions must declare a primary action")
        verbs = set()
        for action in actions:
            if not isinstance(action, dict) or any(not isinstance(action.get(f), str) or not action[f].strip()
                                                   for f in ("verb", "label", "writer")):
                raise ValueError("each action needs verb, label and writer")
            if action["verb"] in verbs:
                raise ValueError("action verbs must be unique")
            verbs.add(action["verb"])
            if not isinstance(action.get("args"), dict):
                raise ValueError("action args must be an object")
            undo = action.get("undo")
            if not isinstance(undo, dict) or "writer" not in undo:
                raise ValueError("each undo must declare its writer")
            if undo["writer"] is None:
                if not isinstance(undo.get("unmet"), str) or not undo["unmet"].strip():
                    raise ValueError("an undo without a writer needs its unmet requirement")
            elif not isinstance(undo["writer"], str) or not undo["writer"].strip() or not isinstance(undo.get("args"), dict):
                raise ValueError("undo needs a writer and args")
        out = dict(kind=data["kind"], what=data["what"], why=data["why"], judge=judge, actions=actions, loop="shown",
                   source=dict(record=key, filed_by=data["filed_by"], related=data["related"]),
                   cant=dict(words="", goes_to=data["filed_by"]))
        if "history" in data:
            out["history"] = data["history"]
        encoded = json.dumps([key, record, data], sort_keys=True, allow_nan=False, separators=(",", ":"))
        out["id"] = "task:" + hashlib.sha256(encoded.encode()).hexdigest()
        problems = model.problems(out)
        if problems:
            raise ValueError("; ".join(problems))
        return out
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{key}: {exc}") from exc


def read(adapter, source, now):
    """Return the complete task-source slice or refuse it by source identity."""
    return model.read(adapter, source, now, task)


def coverage(adapter, source, now):
    """Name every source not placed; other task source kinds remain explicit host inventory exclusions."""
    return model.inventory(adapter, source, now, task)
