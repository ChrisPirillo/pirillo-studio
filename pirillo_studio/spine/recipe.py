"""The recipe validator (docs/spine.md section 1): a recipe the engine could not answer from refuses, naming the place.

A non-owner stage that reads or writes media declares the stage extension preflight.md section 2 asks for: `tools`
(capability ids), `size` ({output path: {"bytes_per_s": n}} for each media output) and `scratch_gb`.
"""
import math

from ..preflight.probe import DATA
from .files import SpineError, fold, safe

RESERVED = ("records/spine.json", "records/spine.lock", "records/holds.json")


def actor(st):
    return st["run"]["role"] if "role" in st["run"] else st["run"]["command"][0]


def claim(made, p, by, at):
    """Record that stage `by` (None: the job) makes p: never an engine record nor a second spelling of one file."""
    safe(p, at)
    if fold(p) in RESERVED:
        raise SpineError(f"{at}: output {p} is the engine's own record")
    twin = [q for q in made if fold(q) == fold(p)]
    if twin:
        raise SpineError(f"{at}: output {p} is already made by {made[twin[0]] or 'the job'}")
    made[p] = by


def validate(recipes, catalog):
    """Refuse, naming the place, anything next could not answer from: shape, ids, roles, checks, paths, order."""
    roles, tools, checks = set(catalog["roles"]), set(catalog["tools"]), catalog["checks"]
    if not isinstance(recipes, dict) or not recipes:
        raise SpineError("recipes: expected a non-empty object of recipe name to recipe")
    for name, r in recipes.items():
        if not isinstance(r, dict) or not isinstance(r.get("stages"), list) or not r["stages"] \
                or not isinstance(r.get("external"), list):
            raise SpineError(f"recipe {name}: needs external (list) and stages (non-empty list)")
        made, seen = {}, set()
        for p in r["external"]:
            claim(made, p, None, f"recipe {name} external")
        later = {p for st in r["stages"] if isinstance(st, dict) for p in st.get("outputs") or ()}
        for i, st in enumerate(r["stages"]):
            at = f"recipe {name} stage {i}"
            if not isinstance(st, dict) or not all(isinstance(st.get(k), list) for k in ("inputs", "check", "outputs")) \
                    or not isinstance(st.get("id"), str) or not isinstance(st.get("run"), dict):
                raise SpineError(f"{at}: needs id, inputs, run, check, outputs and next_actor")
            at = f"recipe {name} stage {st['id']}"
            if st["id"] in seen:
                raise SpineError(f"{at}: duplicate id")
            seen.add(st["id"])
            run = st["run"]
            if not (set(run) == {"role"} and run["role"] in roles
                    or set(run) == {"command"} and isinstance(run["command"], list) and run["command"]
                    and run["command"][0] in tools and all(isinstance(a, str) for a in run["command"])):
                raise SpineError(f"{at}: run {run!r} is neither a registered role nor a registered tool command")
            for p in st["inputs"]:
                safe(p, at)
                if p not in made:
                    raise SpineError(f"{at}: input {p} is " + ("made by a later stage" if p in later else
                                     "made by no stage and not external"))
            if actor(st) == "owner" and not any(made[p] for p in st["inputs"]):
                raise SpineError(f"{at}: an owner stage judges no stage's output")
            if not st["check"] or not st["outputs"]:
                raise SpineError(f"{at}: no check or no output")
            if ("decision" in st["check"]) != (actor(st) == "owner") or actor(st) == "owner" and st["check"] != ["decision"]:
                raise SpineError(f"{at}: the decision check is an owner stage's only check, and only theirs")
            for c in st["check"]:
                if c not in checks:
                    raise SpineError(f"{at}: unknown check {c}")
                for p in checks[c]["reads"]:
                    if p not in ("@inputs", "@outputs") and p not in st["inputs"] + st["outputs"] and not (
                            "tools" in st and p == f"records/ready/{st['id']}.json"):  # its preflight's record
                        raise SpineError(f"{at}: check {c} reads {p}, which the stage does not declare")
                if checks[c]["checker"] == actor(st) or not set(checks[c]["produces"]) <= set(st["outputs"]) or \
                        "@outputs" not in checks[c]["reads"] and not set(checks[c]["produces"]) <= set(checks[c]["reads"]):
                    raise SpineError(f"{at}: check {c}'s checker is its maker, or it writes what the stage does not declare "
                                     "or it does not read")
            for p in st["outputs"]:
                claim(made, p, st["id"], at)
            extension(st, at)
            following = r["stages"][i + 1] if i + 1 < len(r["stages"]) else st
            if st.get("next_actor") != actor(following):
                raise SpineError(f"{at}: next_actor {st.get('next_actor')!r} is not {actor(following)!r}, "
                                 "who runs the next stage")


def number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0


def extension(st, at):
    """tools, size and scratch_gb: required where the stage touches media, never on an owner stage (its run is a
    Board ask). A media path is one whose suffix is not preflight's DATA; a stage with tools is preflighted, and the
    preflight sizes its disk need from scratch_gb and each media output's size row."""
    media = [p for p in st["inputs"] + st["outputs"] if not p.lower().endswith(DATA)]
    if actor(st) == "owner":
        if {"tools", "size", "scratch_gb"} & set(st):
            raise SpineError(f"{at}: an owner stage declares no tools, size or scratch_gb")
        return
    tools, size = st.get("tools"), st.get("size", {})
    if tools is None:
        if media or {"size", "scratch_gb"} & set(st):
            raise SpineError(f"{at}: declares no tools, yet " + (f"reads or writes media ({media[0]})" if media
                                                                 else "declares size or scratch_gb"))
        return
    if not (isinstance(tools, list) and tools and all(isinstance(t, str) and t.strip() for t in tools)):
        raise SpineError(f"{at}: tools must be a non-empty list of capability ids")
    if not isinstance(size, dict) or any(p not in st["outputs"] or not isinstance(s, dict) or set(s) != {"bytes_per_s"}
                                         or not number(s["bytes_per_s"]) or not s["bytes_per_s"] for p, s in size.items()):
        raise SpineError(f"{at}: size must map its outputs to {{\"bytes_per_s\": a positive finite number}}")
    unsized = [p for p in st["outputs"] if p in media and p not in size]
    if unsized:
        raise SpineError(f"{at}: media output {unsized[0]} has no size row")
    if not number(st.get("scratch_gb")):
        raise SpineError(f"{at}: scratch_gb {st.get('scratch_gb')!r} is not a finite number >= 0")
