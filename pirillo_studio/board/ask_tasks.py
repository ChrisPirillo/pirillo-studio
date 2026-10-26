"""Pure ask tasks (docs/board.md, Asks reader): the host inventories raw ask facts; this model owns their Board meaning."""
import copy
import hashlib
import json
import posixpath
from urllib.parse import quote

from . import model

FIELDS = ("what", "about", "options", "docs", "files", "steps", "state", "todo", "acted", "deadline", "default_on_timeout",
          "filed_by", "blocks", "related", "recommendation")
FINISHED = frozenset(("answered", "withdrawn"))
# No owner-facing writer takes an answer back today (approvals.revoke and decisions reopen on their own evidence).
UNMET = "Answer reversal writer is an unmet rebuild requirement"
TYPES = {".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".mp4": "video", ".mov": "video",
         ".m4v": "video"}


def _type(path):
    return TYPES.get(posixpath.splitext(path)[1].lower(), "file")


def _texts(data, field):
    v = data[field]
    if not isinstance(v, list) or not all(isinstance(p, str) and p.strip() for p in v):
        raise ValueError(f"{field} must be a list of paths")
    return v


def task(adapter, key, record):
    """Build one revision-bound ask task. Missing facts refuse by source key and field."""
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"{key!r}: source key must be text")
    try:
        data = copy.deepcopy(adapter.describe(key, copy.deepcopy(record)))
        for field in FIELDS:
            data[field]
        for field in ("what", "state", "filed_by", "blocks"):
            if not isinstance(data[field], str) or not data[field].strip():
                raise ValueError(f"{field} must be text")
        if data["state"] not in FINISHED | {"open"}:
            raise ValueError(f"state {data['state']!r} is not open, answered or withdrawn")
        opts = data["options"]
        if not isinstance(opts, list) or not all(isinstance(o, dict) and isinstance(o.get("label"), str)
                                                 and o["label"].strip() for o in opts):
            raise ValueError("options must each have a label")
        docs, files = _texts(data, "docs"), _texts(data, "files")
        steps = data["steps"]
        if steps is not None and (not isinstance(steps, str) or not steps.strip()):
            raise ValueError("steps must be a path or null")
        content = {k: data[k] for k in ("what", "about", "options", "docs", "files", "steps")}
        if "content_identity" in data:
            content["content_identity"] = data["content_identity"]
        revision = hashlib.sha256(json.dumps(content, sort_keys=True, allow_nan=False).encode()).hexdigest()
        href = "/board/content/" + quote(key, safe="") + "/" + revision
        judge = {"type": _type(files[0]) if files else "words", "href": href}
        parts = [{"type": _type(f), "href": f"{href}/file/{n}", "label": posixpath.basename(f), "role": "content"}
                 for n, f in enumerate(files) if n]
        parts += [{"type": "file", "href": f"{href}/doc/{n}", "label": posixpath.basename(d), "role": "source"}
                  for n, d in enumerate(docs) if d != steps]          # a steps doc is linked once, as the steps
        if steps is not None:
            parts.append({"type": "file", "href": href + "/steps", "label": "Steps", "role": "steps"})
        if parts:
            judge["parts"] = parts
        about = " ".join(str(data["about"] or "").split()).rstrip(" .:")
        why = [f"It holds up: {data['blocks']}."]
        rec = data["recommendation"]
        if isinstance(rec, dict) and rec.get("label") and rec.get("why"):
            why.append(f"Recommended: {rec['label']} ({rec['why']}).")
        undo = dict(verb="unanswer", writer=None, until=None, why=UNMET, unmet=UNMET)
        if opts:
            actions = [dict(verb=f"answer-{n}", label=o["label"], writer="ask_answer", args={"choice": o["label"]},
                            undo=undo) for n, o in enumerate(opts)]
        elif data["todo"]:                                          # a to-do: Done answers it and records it acted
            actions = [dict(verb="done", label="Done", writer="board_data.action", args={}, undo=undo)]
        else:
            actions = [dict(verb="answer", label="Answer", writer="ask_answer", args={}, undo=undo)]
        finished = data["state"] in FINISHED
        for field in ("todo", "acted"):
            if not isinstance(data[field], bool):
                raise ValueError(f"{field} must be true or false")
        acted = data["acted"] or data["state"] == "withdrawn"
        out = dict(kind="ask", what=f"{about}: {data['what']}" if about else data["what"], why=" ".join(why),
                   judge=judge, actions=[] if finished else actions, facets=[],
                   source=dict(record=key, filed_by=data["filed_by"], related=data["related"]),
                   cant=dict(words="", goes_to=data["filed_by"]),
                   loop=("done" if acted else "acted") if finished else "shown")
        if data["deadline"] is not None:
            if not isinstance(data["default_on_timeout"], str) or not data["default_on_timeout"].strip():
                raise ValueError("a deadline needs its default_on_timeout")
            # decisions.expire records the default as the answer, so a passed deadline waits on that writer's run.
            out["deadline"] = dict(at=data["deadline"], outcome=data["default_on_timeout"], run=True)
            out["why"] += f" Unanswered by {data['deadline']}, it answers {data['default_on_timeout']}."
        if "history" in data:
            out["history"] = data["history"]
        encoded = json.dumps([key, record, data], sort_keys=True, allow_nan=False, separators=(",", ":"))
        out["id"] = "ask:" + hashlib.sha256(encoded.encode()).hexdigest()
        return out
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{key}: {exc}") from exc


def read(adapter, source, now):
    """Return effective ask tasks, including history; never a deceptively partial Board."""
    return model.read(adapter, source, now, task)


def coverage(adapter, source, now):
    """Diagnostic inventory of a copied source; unplaced records remain named in problems."""
    return model.inventory(adapter, source, now, task)
