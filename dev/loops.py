"""Each loop's chart, drawn from its definition (dev/loops/*.json) with every dev/queue.json item on its state: an
HTML fragment a host puts on its Board (the product never imports it). A state, transition or item the loop does not
know raises; a malformed file raises as json reads it.

usage: python3 dev/loops.py [root]   prints every loop's chart
"""
import json, os, sys
from html import escape as e

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def render(loop, items):
    """One loop: its states in order, each with the items on it and where it goes next, by whom; the done state marked."""
    mine = [i for i in items if i["loop"] == loop["name"]]
    used = [loop["done"]] + [t[k] for t in loop["transitions"] for k in ("from", "to")] + [i["state"] for i in mine]
    unknown = sorted(set(used) - set(loop["states"]))
    if unknown:
        raise ValueError(f"loop {loop['name']}: unknown state {', '.join(unknown)}")
    cols = ""
    for s in loop["states"]:
        on = [i for i in mine if i["state"] == s]
        cols += (f'<li class="state{" done" if s == loop["done"] else ""}" data-state="{e(s)}"><b>{e(s)}</b> <span>{len(on)}</span>'
                 + "".join(f'<p class=to>→ {e(t["to"])}: {e(t["by"])}</p>' for t in loop["transitions"] if t["from"] == s)
                 + "<ul>" + "".join(f'<li>#{i["id"]} {e(i["title"])}</li>' for i in on) + "</ul></li>")
    return f'<section class=loop data-loop="{e(loop["name"])}"><h3>{e(loop["name"])}</h3><ol class=states>{cols}</ol></section>'


def charts(root=ROOT):
    """Every loop in dev/loops, in file order, with dev/queue.json's items on their states."""
    d = os.path.join(root, "dev", "loops")
    with open(os.path.join(root, "dev", "queue.json")) as f:
        items = json.load(f)
    defs = []
    for n in sorted(os.listdir(d)):
        with open(os.path.join(d, n)) as f:
            defs.append(json.load(f))
    lost = sorted({i["loop"] for i in items} - {l["name"] for l in defs})
    if lost:
        raise ValueError(f"queue items on unknown loop {', '.join(lost)}")
    return "".join(render(l, items) for l in defs)


if __name__ == "__main__":
    if "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__.strip())
        sys.exit(0)
    print(charts(*sys.argv[1:2]))
