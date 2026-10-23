"""voice and select_voice (docs/capabilities.md sections 1 to 3): the owner's speech and writing voice, by content.

`voice.json` sits in the host adapter's `studio` folder, in section 1's shape; a speech choice is `voices/<id>`, a
folder there holding `profile.json` (model, reference, sampling), and a writing choice is a declared guide ID; `none`
is the owner's no. Board asks are docs/board.md tasks (`judge.parts` roles candidate and original for a take). The
choice ask's actions are one verb per option, the option itself, so a decision `<task id>@<revision>` whose verb is
the choice is the owner's pick. `stage(host, tools, cites)` is how a stage's `tools` list (preflight.md's spec
extension) is answered: `voice:<part>` asks voice(part), any other entry capability(id), and a cited patch asks for
the speech take pick whatever the list says; preflight (item 6) is its caller once built.
"""
import os
from pathlib import Path

from ..spine.files import SpineError, canon, digest, filehash, inside, load
from .answers import answer, capability, current, iso, locked, write_beside
from .declare import blank, declarations

PARTS = {"speech": ("voice", "profile_sha256", "engine"), "writing": ("guide", "guide_sha256")}
PROFILE = ("model", "reference", "sampling")
UNDO = {"verb": "undo", "until": None, "why": "Until a stage reads the voice record"}


def record(host):
    """voice.json, or {} before any choice; a malformed one, or one whose key is not its parts', refuses naming it."""
    f = inside(Path(host.studio), "voice.json")
    if not f.exists():
        return {}
    try:
        r = load(Path(host.studio), "voice.json")
        parts = {k: v for k, v in r.items() if k != "key"}
        if not set(parts) <= set(PARTS) or not parts or r.get("key") != digest(canon(parts)):
            raise TypeError("expected speech or writing, and key over them")
        for part, p in parts.items():
            need = {"state", "decision", "at"} | (set(PARTS[part]) if p.get("state") == "selected" else set())
            if p.get("state") not in ("selected", "none") or set(p) != need \
                    or any(not isinstance(p[k], str) for k in need) or "@" not in p["decision"]:
                raise TypeError(f"{part} needs exactly {sorted(need)}")
        return r
    except (OSError, ValueError, TypeError, AttributeError) as x:
        raise SpineError(f"{f}: unreadable voice record ({x})") from None


def options(host, part):
    if part == "speech":
        v = inside(Path(host.studio), "voices")
        names = sorted(p.name for p in v.iterdir() if (p / "profile.json").is_file()) if v.is_dir() else []
        return [f"voices/{n}" for n in names] + ["none"]
    d = declarations(host)["capabilities"]
    return sorted(c for c in d if d[c]["kind"] == "guide") + ["none"]


def choose_id(host, part):
    return f"capabilities:voice:{part}:{digest(canon(options(host, part)))[7:19]}"


def folder_hash(host, rel):
    """(sha over the voice folder's files, its profile), or (None, None) when the folder or its profile is gone."""
    f = inside(Path(host.studio), rel)
    files = sorted(p for p in f.rglob("*") if p.is_file()) if f.is_dir() else []
    if not (f / "profile.json").is_file():
        return None, None
    try:
        profile = load(f, "profile.json")
    except (OSError, ValueError) as x:
        raise SpineError(f"{f / 'profile.json'}: unreadable profile ({x})") from None
    return digest(canon({str(p.relative_to(f)): filehash(p) for p in files})), profile


def voice(host, part, cites=None, ask=True):
    """The answer for one voice part; cites {"take", "original"} paths a patch's take pick. Writes nothing but asks.
    `key` is voice.json's selection key, the one a consumer cites; `capability_key` the engine's or guide's."""
    if part not in PARTS:
        raise SpineError(f"voice part {part!r}: expected one of {sorted(PARTS)}")
    r = record(host)
    p, at = r.get(part), f"voice:{part}"
    if p is None:
        t = choice_task(host, part)
        if ask:
            host.ask(t)
        return answer(at, "waiting_owner", f"the owner chooses the {part} voice on the Board", t["id"], ask=t["id"])
    sel = dict(key=r["key"])
    if p["state"] == "none":
        return answer(at, "missing", None, f"the owner chose no {part} voice ({p['decision']}): a patch is never offered",
                      **sel)
    if part == "writing":
        a = capability(host, p["guide"])
        if a["ok"] and a["resolved"]["sha256"] != p["guide_sha256"]:
            return answer(at, "unqualified", f"the owner chooses the {part} voice again",
                          f"{p['guide']} changed since {p['decision']}", **sel)
        return answer(at, a["state"], a["next_action"], a["detail"], capability_key=a.get("key"), **sel)
    sha, _ = folder_hash(host, p["voice"])
    if sha is None:
        return answer(at, "missing", f"the owner chooses the {part} voice again", f"{p['voice']} is gone", **sel)
    if sha != p["profile_sha256"]:
        return answer(at, "unqualified", f"the owner chooses the {part} voice again",
                      f"{p['voice']} changed since {p['decision']}", **sel)
    a = capability(host, p["engine"])
    sel["capability_key"] = a.get("key")
    if not a["ok"] or cites is None:
        return answer(at, a["state"], a["next_action"], a["detail"], **sel)
    return take(host, at, sel, p, cites, ask)


def take(host, at, sel, speech, cites, ask):
    """A patch's take is the owner's pick only: an approve on the task bound to the take's and the original's bytes
    and the speech key (the speech part and its engine's install): a new voice, engine, take or original reopens it."""
    sha, orig = filehash(Path(cites["take"])), filehash(Path(cites["original"]))
    if sha is None or orig is None:
        gone = cites["take"] if sha is None else cites["original"]
        return answer(at, "missing", "the maker renders the take again", f"{gone} is gone", **sel)
    tid = f"capabilities:take:{digest(canon([sha, orig, speech, sel['capability_key']]))[7:19]}"
    got = host.decided(tid)
    if got and got.get("verb") == "approve":
        return answer(at, "qualified", None, f"take {sha} picked by the owner ({tid}@{got['revision']})", **sel)
    t = {"id": tid, "kind": "ask", "what": "Pick this take for the patch",
         "why": "a patched line plays only on the owner's pick; a ranking or a recognizer is evidence, never the pick",
         "source": {"record": "voice.json", "filed_by": "capabilities", "related": []},
         "judge": {"type": "file", "href": host.href(Path(cites["take"])),
                   "parts": [{"type": "file", "href": host.href(Path(cites["take"])), "label": "Patch",
                              "role": "candidate"},
                             {"type": "file", "href": host.href(Path(cites["original"])), "label": "Original line",
                              "role": "original"}]},
         "actions": [{"verb": "approve", "label": "Use this take", "undo": UNDO},
                     {"verb": "reject", "label": "Not this take", "undo": UNDO}],
         "cant": {"words": "", "goes_to": "capabilities"}, "loop": "filed"}
    if ask:
        host.ask(t)
    return answer(at, "waiting_owner", "the owner picks the take on the Board", tid, ask=tid, **sel)


def choice_task(host, part):
    opts = options(host, part)
    root = Path(host.studio)
    parts = [{"type": "file" if part == "speech" else "words", "label": o, "role": "candidate",
              "href": host.href(inside(root, o)) if part == "speech" else host.href(Path(capability(host, o)
                                                                                         ["resolved"]["path"]))}
             for o in opts if o != "none" and (part == "speech" or "resolved" in capability(host, o))]
    return {"id": choose_id(host, part), "kind": "ask", "what": f"Choose the {part} voice",
            "why": f"a stage declares voice:{part} and no {part} voice is chosen; it waits on this choice",
            "source": {"record": "voice.json", "filed_by": "capabilities", "related": []},
            "judge": {"type": parts[0]["type"] if parts else "words", "href": parts[0]["href"] if parts else
                      host.href(inside(root, "voice.json")), "parts": parts},
            "actions": [{"verb": o, "label": "No synthesized voice" if o == "none" else o, "undo": UNDO}
                        for o in opts],
            "cant": {"words": "", "goes_to": "capabilities"}, "loop": "filed"}


def select_voice(host, part, choice, decision):
    """Write the owner's choice into voice.json from his current decision; the same decision again changes nothing."""
    if part not in PARTS:
        raise SpineError(f"voice part {part!r}: expected one of {sorted(PARTS)}")
    if not isinstance(decision, str) or "@" not in decision:
        raise SpineError(f"select_voice {part}: needs the owner's Board decision as <task id>@<revision>")
    if choice not in options(host, part):
        raise SpineError(f"select_voice {part}: {choice!r} is not one of the offered options")
    current(host, decision, choose_id(host, part), choice)  # a replay too: the Board may have moved on
    with locked(host, "voice"):  # one writer of voice.json: two parts chosen at once both land
        r = record(host)
        if r.get(part, {}).get("decision") == decision:
            return r
        p = {"state": "none"}
        if choice != "none" and part == "speech":
            sha, profile = folder_hash(host, choice)
            if not isinstance(profile, dict) or any(blank(profile.get(k)) for k in PROFILE):
                raise SpineError(f"{choice}/profile.json: needs {PROFILE}, none empty, never a default")
            if profile["model"] not in declarations(host)["capabilities"]:
                raise SpineError(f"{choice}/profile.json: model {profile['model']!r} is not a declared capability")
            p = {"state": "selected", "voice": choice, "profile_sha256": sha, "engine": profile["model"]}
        elif choice != "none":
            a = capability(host, choice)
            if "resolved" not in a:
                raise SpineError(f"select_voice writing: {choice} is {a['state']} ({a['detail']})")
            p = {"state": "selected", "guide": choice, "guide_sha256": a["resolved"]["sha256"]}
        r = {k: v for k, v in r.items() if k != "key"}
        r[part] = dict(p, decision=decision, at=iso(host))
        r["key"] = digest(canon(r))
        f = inside(Path(host.studio), "voice.json")
        os.replace(write_beside(f, r), f)
        return r


def stage(host, tools, cites=None):
    """[status] for a stage's tools list, in its order; a cited patch adds the speech take pick."""
    out = [voice(host, t.split(":", 1)[1]) if t.startswith("voice:") else capability(host, t) for t in tools]
    if cites is not None:
        out.append(voice(host, "speech", cites))
    return out


def status(host):
    """Every declared capability and both voice parts, read only: no probe, no write, no ask."""
    return [capability(host, c) for c in declarations(host)["capabilities"]] + \
           [voice(host, part, ask=False) for part in PARTS]
