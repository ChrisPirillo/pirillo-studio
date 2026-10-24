"""Review.md: rendered from the release record, one fenced block per row; the owner's changed blocks come back."""
import re

from .inputs import REVIEW, Refused

_KEY = re.compile(r"release (sha256:[0-9a-f]{64})")
_OPEN = re.compile(r"(`{3,})(.*)")


def prev(key):
    return f"records/prev/review-{key.removeprefix('sha256:')}.md"


def _fence(text):
    return "`" * (max([len(m) for m in re.findall(r"`+", text)] + [2]) + 1)


def render(rec, words):
    """First line names the key; each row's words in a fence longer than any backtick run inside them."""
    out = f"release {rec['key']}\n\n"
    if rec["state"] == "refused":
        return out + "Release refused\n\n" + "\n".join(str(d) for d in rec["detail"]) + "\n"
    heads = [(o["id"], f"{o['id']}: {o['title']}") for o in rec["outputs"]] + [(None, "text post")]
    for oid, head in heads:
        rows = [r for r in rec["rows"] if r["output"] == oid]
        if rows:
            out += f"## {head}\n\n"
        for r in rows:
            w = words(r)
            f = _fence(w)
            out += f"{f}{r['id']}\n{w}\n{f}\n\n"
    return out


def parse(text):
    """(key, [(row ID, words)], the text outside the blocks with a mark where each block sits)."""
    lines = text.split("\n")
    m = _KEY.fullmatch(lines[0])
    blocks, outside, i = [], [], 1
    while i < len(lines):
        o = _OPEN.fullmatch(lines[i])
        if not o:
            outside.append(lines[i])
            i += 1
            continue
        j = next((j for j in range(i + 1, len(lines)) if lines[j] == o[1]), None)
        if j is None:
            raise Refused(f"{REVIEW}: the block opened at line {i + 1} is never closed", "owner")
        blocks.append((o[2].strip(), "\n".join(lines[i + 1:j])))
        outside.append(None)
        i = j + 1
    return (m[1] if m else None), blocks, outside


def changed(his, rendered, key):
    """row ID -> (his words, the words he was shown) for each block he changed; anything unmappable refuses."""
    _, mine, around = parse(his)
    _, shown, was = parse(rendered)
    if around != was:
        raise Refused(f"{REVIEW}: text changed outside the row blocks of {key}", "owner")
    out = {}
    for (rid, text), (sid, before) in zip(mine, shown):
        if rid != sid:
            raise Refused(f"{REVIEW}: a block names {rid or 'no row ID'} where {key} rendered {sid}", "owner")
        if text != before:
            out[rid] = (text, before)
    return out
