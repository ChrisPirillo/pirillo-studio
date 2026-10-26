"""Pure post tasks: the host inventories raw facts; this model owns their Board meaning."""
import copy
import hashlib
import json
from urllib.parse import quote

from . import model

# Section 3: judge type and whether this kind offers image editing.
KINDS = {"long post": ("video", False), "vertical post": ("video", False),
         "short post": ("video", False), "text post": ("words", True),
         "story post": ("words", True), "tip post": ("words", False),
         "vault post": ("words", True), "digest": ("words", True), "poll": ("words", False),
         "audio post": ("file", False), "image post": ("image", True)}
TERMINAL = frozenset(("posted", "verified", "done", "closed", "expired", "removed", "skipped", "picked"))
FACETS = {"approval": "approval", "hand": "hand", "blocked": "stuck", "stuck": "stuck", "failed": "failed"}
FIELDS = ("kind", "platforms", "status", "words", "file", "scheduled_at", "error", "blocker", "filed_by", "related")
# Inverses are operation declarations for the API's source snapshot, never effects here.
ACTIONS = {
    "approve": ("Approve", "ok_from_board", "undo-approve", "ok_post", "Until sending starts"),
    "skip": ("Skip", "pass_from_board", "restore", "pass_from_board", "Restore the prior source state"),
    "posted": ("Posted", "mark_posted", "undo-posted", "unmark_posted", "Local marks do not delete published posts"),
    "save-words": ("Save words", "edit_post", "restore-words", "edit_post", "Restore the saved words before sending"),
    "signed-in": ("Signed in", "helper.signins", "undo-signed-in", None, "Sign-in acknowledgement restoration writer is an unmet rebuild requirement"),
    "image-redo": ("Redo image", "image_redo_from_board", "restore-image", None, "Image restoration writer is an unmet rebuild requirement"),
}


def task(adapter, key, record):
    """Build one revision-bound task. Missing facts refuse by source key and field."""
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"{key!r}: source key must be text")
    try:
        data = copy.deepcopy(adapter.describe(key, copy.deepcopy(record)))
        for field in FIELDS:
            data[field]
        kind = data["kind"]
        if kind not in KINDS:
            raise ValueError("kind must be a post kind; approval/hand/stuck/failed belong in facets")
        for field in ("words", "error", "blocker"):
            if not isinstance(data[field], str):
                raise ValueError(f"{field} must be text")
        if not isinstance(data["platforms"], list) or not data["platforms"] or not all(
                isinstance(p, str) and p.strip() for p in data["platforms"]):
            raise ValueError("platforms must name the post destinations")
        if data["file"] is not None and (not isinstance(data["file"], str) or not data["file"].strip()):
            raise ValueError("file must be a path, URL or null")
        statuses = data["status"] if isinstance(data["status"], list) else [data["status"]]
        if not statuses or not all(isinstance(state, str) and state.strip() for state in statuses):
            raise ValueError("status must name the source state")
        facets = sorted({FACETS[state] for state in statuses if state in FACETS})
        selection = kind in {"poll", "tip post"} and "planned" in statuses and "approval" in facets
        if selection:
            facets.append("selection")
        finished = all(state in TERMINAL for state in statuses)
        content = {k: data[k] for k in ("words", "file", "platforms")}
        if "content_identity" in data:
            content["content_identity"] = data["content_identity"]
        revision = hashlib.sha256(json.dumps(content, sort_keys=True, allow_nan=False).encode()).hexdigest()
        href = "/board/content/" + quote(key, safe="") + "/" + revision
        judge_type, images = KINDS[kind]
        judge = {"type": judge_type, "href": href}
        if judge_type != "words":
            judge["parts"] = [{"type": "words", "href": href + "/words", "label": "Post words", "role": "content"}]
        elif data["file"] is not None:
            judge["parts"] = [{"type": "image", "href": href + "/image", "label": "Post image", "role": "content"}]
        why = [f"{kind.capitalize()} for {', '.join(data['platforms'])}."]
        if "hand" in facets:
            why.append("The owner posts it by hand.")
        if "approval" in facets:
            why.append("Choose whether to use this post." if selection else "The post waits for approval.")
        for facet, field in (("stuck", "blocker"), ("failed", "error")):
            if facet in facets:
                if not data[field].strip():
                    raise ValueError(f"{field} is missing for {facet} post")
                why.append(data[field])
        verbs = ["posted"] if not selection and ("hand" in facets or "stuck" in facets) else []
        if "stuck" in facets:
            verbs.append("signed-in")
        if "approval" in facets:
            verbs.append("approve")
        verbs += ["skip", "save-words"]
        if images:
            verbs.append("image-redo")
        actions = []
        for verb in verbs:
            label, writer, inverse, undo_writer, limit = ACTIONS[verb]
            if verb == "approve":
                if kind in {"story post", "tip post", "vault post", "digest"}:
                    undo_writer = "story_ok"
                elif kind == "poll":
                    undo_writer, limit = None, "Poll approval reversal writer is an unmet rebuild requirement"
            undo = dict(verb=inverse, writer=undo_writer, until=None, why=limit)
            if undo_writer is None:
                undo["unmet"] = limit
            elif verb in ("approve", "skip"):
                undo["args"] = {"undo": True}
            if verb == "approve" and selection:
                verb, label = "want", "Want"
            actions.append(dict(verb=verb, label=label, writer=writer, undo=undo))
        out = dict(kind=kind, what=f"Review {kind}", why=" ".join(why), judge=judge,
                   actions=[] if finished else actions, facets=facets,
                   source=dict(record=key, filed_by=data["filed_by"], related=data["related"]),
                   cant=dict(words="", goes_to=data["filed_by"]), loop="done" if finished else "shown")
        if not selection and data["scheduled_at"] is not None and not {"failed", "stuck"}.intersection(facets) and (
                "hand" in facets or "approval" not in facets):
            out["deadline"] = dict(at=data["scheduled_at"], outcome="missed" if "hand" in facets else "send", run="hand" not in facets)
            out["why"] += f" Its send is scheduled at {data['scheduled_at']}."
        if "history" in data:
            out["history"] = data["history"]
        encoded = json.dumps([key, record, data], sort_keys=True, allow_nan=False, separators=(",", ":"))
        out["id"] = "post:" + hashlib.sha256(encoded.encode()).hexdigest()
        return out
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{key}: {exc}") from exc


def read(adapter, source, now):
    """Return effective tasks, including history; never return a deceptively partial Board."""
    return model.read(adapter, source, now, task)


def coverage(adapter, source, now):
    """Diagnostic inventory of a copied source; unplaced records remain named in problems."""
    return model.inventory(adapter, source, now, task)
