"""Pure vault option tasks; the host inventories native options and conversion receipts."""
import copy
import hashlib
import json
from urllib.parse import quote

from . import model

FIELDS = ("what", "why", "words", "file", "state", "deadline", "converted_to", "filed_by", "related")
STATES = frozenset(("open", "queued", "held", "picked", "skipped", "expired", "removed"))


def task(adapter, key, record):
    """Build one option revision; a converted option never asks for a second approval."""
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"{key!r}: source key must be text")
    try:
        data = copy.deepcopy(adapter.describe(key, copy.deepcopy(record)))
        for field in FIELDS:
            data[field]
        for field in ("what", "why", "words", "filed_by"):
            if not isinstance(data[field], str) or not data[field].strip():
                raise ValueError(f"{field} must be text")
        for field in ("file", "converted_to"):
            if data[field] is not None and (not isinstance(data[field], str) or not data[field].strip()):
                raise ValueError(f"{field} must be text or null")
        if data["state"] not in STATES:
            raise ValueError("state must name a vault option state")
        if not isinstance(data["related"], list) or any(not isinstance(r, str) or not r.strip() for r in data["related"]):
            raise ValueError("related must be a list of source keys")
        content = {k: data[k] for k in ("what", "why", "words", "file")}
        if "content_identity" in data:
            content["content_identity"] = data["content_identity"]
        revision = hashlib.sha256(json.dumps(content, sort_keys=True, allow_nan=False).encode()).hexdigest()
        href = "/board/content/" + quote(key, safe="") + "/" + revision
        judge = dict(type="words", href=href)
        if data["file"] is not None:
            judge["parts"] = [dict(type="image", href=href + "/image", label="Option still", role="content")]
        actions = []
        for verb, label in (("want", "Want"), ("skip", "Skip"), ("schedule", "Schedule"), ("earlier", "Earlier"),
                            ("later", "Later"), ("save", "Save words"), ("move", "Move"), ("remove", "Remove")):
            # Opposite queue movement can be a no-op; save loses per-platform copy. Neither restores a snapshot.
            reason = f"{label} reversal writer is an unmet rebuild requirement"
            args = dict(ids=[key]) if verb == "want" else dict(vid=key, action="remove" if verb == "skip" else verb)
            actions.append(dict(verb=verb, label=label, writer="ok_from_board" if verb == "want" else "weekend_change",
                                args=args, undo=dict(verb="undo-" + verb, writer=None, until=None, why=reason, unmet=reason)))
        converted = data["converted_to"]
        related = list(data["related"])
        if converted and converted not in related:
            related.append(converted)
        loop = ("acted" if converted or data["state"] == "picked" else "shown" if data["state"] in ("open", "queued")
                else "cant" if data["state"] == "held" else "done")
        out = dict(kind="vault option", what=data["what"], why=data["why"], judge=judge,
                   actions=actions if loop == "shown" else [], loop=loop,
                   source=dict(record=key, filed_by=data["filed_by"], related=related),
                   cant=dict(words="", goes_to=data["filed_by"]))
        if data["deadline"] is not None:
            out["deadline"] = dict(at=data["deadline"], outcome="expired", run=False)
        if "history" in data:
            out["history"] = data["history"]
        encoded = json.dumps([key, record, data], sort_keys=True, allow_nan=False, separators=(",", ":"))
        out["id"] = "vault:" + hashlib.sha256(encoded.encode()).hexdigest()
        problems = model.problems(out)
        if problems:
            raise ValueError("; ".join(problems))
        return out
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{key}: {exc}") from exc


def read(adapter, source, now):
    """Return all effective options or refuse the complete read."""
    return model.read(adapter, source, now, task)


def coverage(adapter, source, now):
    """Name every source, including every unplaced option and its cause."""
    return model.inventory(adapter, source, now, task)
