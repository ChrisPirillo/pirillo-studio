"""Pure idea tasks over host facts; see docs/board.md, Ideas reader."""
import copy
import hashlib
import json
from urllib.parse import quote

from . import model

FIELDS = ("kind", "what", "why", "outlet", "sources", "saved", "wanted", "skipped", "converted_to", "filed_by", "related")


def task(adapter, key, record):
    """Bind one logical idea and its actions to the complete source revision."""
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"{key!r}: source key must be text")
    try:
        data = copy.deepcopy(adapter.describe(key, copy.deepcopy(record)))
        for field in FIELDS:
            data[field]
        if data["kind"] not in ("video", "story"):
            raise ValueError("kind must be video or story")
        for field in ("what", "why", "filed_by"):
            if not isinstance(data[field], str) or not data[field].strip():
                raise ValueError(f"{field} must be text")
        if not isinstance(data["outlet"], str):
            raise ValueError("outlet must be text")
        for field in ("saved", "wanted", "skipped"):
            if type(data[field]) is not bool:
                raise ValueError(f"{field} must be true or false")
        converted = data["converted_to"]
        if converted is not None and (not isinstance(converted, str) or not converted.strip()):
            raise ValueError("converted_to must be a source key or null")
        sources = data["sources"]
        if not isinstance(sources, list) or any(not isinstance(source, dict) or any(
                not isinstance(source.get(f), str) or not source[f].strip() for f in ("url", "label")) for source in sources):
            raise ValueError("sources must each have a URL and label")
        content = {k: data[k] for k in ("what", "why", "outlet", "sources")}
        revision = hashlib.sha256(json.dumps(content, sort_keys=True, allow_nan=False).encode()).hexdigest()
        href = "/board/content/" + quote(key, safe="") + "/" + revision
        judge = dict(type="words", href=href)
        if sources:
            judge["parts"] = [dict(type="words", href=f"{href}/source/{n}", label=source["label"], role="source")
                              for n, source in enumerate(sources)]
        actions = []
        verbs = [("want", "Want it"), ("bank", "Bank")]
        if data["kind"] == "video":
            verbs.append(("story", "Story"))
        verbs.append(("skip", "Skip"))
        for verb, label in verbs:
            reason = f"{label} reversal writer is an unmet rebuild requirement"
            undo = dict(verb="undo-" + verb, writer=None, until=None, why=reason, unmet=reason)
            if verb == "bank":
                undo = dict(verb="unbank", label="Unbank", writer="star_idea", args=dict(key=key, on=False),
                            until=None, why="Let this saved idea go from the Bank.")
            elif verb == "story":
                undo = dict(verb="unconvert", writer="story_pick", args=dict(sid=converted, want=None),
                            receipt_field="converted_to", until=None,
                            why="Use the conversion receipt; clears only that receipt and pick, and refuses once its post is on the Board.")
            if verb == "bank" and data["saved"]:
                actions.append(dict(verb="unbank", label="Unbank", writer="star_idea", args=dict(key=key, on=False),
                                    undo=dict(verb="bank", writer="star_idea", args=dict(key=key, on=True),
                                              until=None, why="Save the same idea again.")))
            else:
                actions.append(dict(verb=verb, label=label, writer="idea_decide", args=dict(key=key, action=verb), undo=undo))
        # Banking reopens an earlier pick/skip; conversion still owns its story receipt.
        loop = "acted" if converted else "shown" if data["saved"] else "done" if data["skipped"] else "acted" if data["wanted"] else "shown"
        kind = "story idea" if data["kind"] == "story" else "banked idea" if data["saved"] and not converted else "idea"
        out = dict(kind=kind, what=data["what"], why=data["why"] + (f" ({data['outlet']})" if data["outlet"] else ""),
                   judge=judge, actions=[] if loop == "done" else actions, loop=loop,
                   source=dict(record=key, filed_by=data["filed_by"], related=data["related"]),
                   cant=dict(words="", goes_to=data["filed_by"]))
        if "history" in data:
            out["history"] = data["history"]
        encoded = json.dumps([key, record, data], sort_keys=True, allow_nan=False, separators=(",", ":"))
        out["id"] = "idea:" + hashlib.sha256(encoded.encode()).hexdigest()
        return out
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{key}: {exc}") from exc


def read(adapter, source, now):
    """Return all effective tasks, refusing a partial Board."""
    return model.read(adapter, source, now, task)


def coverage(adapter, source, now):
    """Name every copied source and every unplaced record."""
    return model.inventory(adapter, source, now, task)
