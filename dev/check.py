"""The repository's shape checks: layout, file sizes, test packages, the queue against its loops and layers, the
host boundary (NEEDS), no assert outside tests, no product import of dev, private data; a JSON file with a duplicate
key refuses.

usage: python3 dev/check.py [root] [--json]   exit 0 clean, 1 with problems listed
In a git checkout it checks the files git would commit; elsewhere (land.py's export) every file.
With PIRILLO_PRIVATE_PATTERNS set (land.py requires it), no file name or content matches a pattern in that file.
"""
import ast, importlib.util, json, os, re, subprocess, sys


def unique(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):  # a later key would silently overwrite the earlier one
        raise ValueError(f"duplicate key in {keys}")
    return dict(pairs)


def load(path):
    with open(path) as f:
        return json.load(f, object_pairs_hook=unique)


def files(root):
    if os.path.exists(os.path.join(root, ".git")):      # a worktree's .git is a file
        out = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             check=True, capture_output=True, text=True).stdout
        return sorted(p for p in out.split("\0") if p and os.path.exists(os.path.join(root, p)))
    return sorted(os.path.relpath(os.path.join(d, n), root)
                  for d, dirs, names in os.walk(root) for n in names)


def layout(paths, repo):
    return [f"{n}: not in repo.json layout" for n in sorted({p.split(os.sep)[0] for p in paths}) if n not in repo["layout"]]


def sizes(root, paths, repo):
    out = []
    for p in paths:
        with open(os.path.join(root, p), "rb") as f:
            data = f.read()
        n = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        if n > repo["max_lines"]:
            out.append(f"{p}: {n} lines, over max_lines {repo['max_lines']}")
    return out


def packages(paths):
    """unittest discovery skips a folder without __init__.py, so its tests would never run."""
    dirs = set()
    for p in paths:
        parts = p.split(os.sep)[1:-1]  # folders below the discovery root (dev/, pirillo_studio/)
        if p.endswith(".py"):
            dirs |= {os.path.join(p.split(os.sep)[0], *parts[:i + 1]) for i in range(len(parts))}
    return [f"{d}: holds Python but no __init__.py, so its tests never run" for d in sorted(dirs)
            if os.path.join(d, "__init__.py") not in paths]


def queue(root):
    d = os.path.join(root, "dev", "loops")
    loops, out = {}, []
    for n in sorted(os.listdir(d)):
        loop = load(os.path.join(d, n))
        if loop["name"] in loops:
            out.append(f"loop {loop['name']}: declared twice ({n})")
        loops[loop["name"]] = loop
    for name, loop in loops.items():
        states = set(loop["states"])
        bad = {t[k] for t in loop["transitions"] for k in ("from", "to")} - states | {loop["done"]} - states
        out += [f"loop {name}: unknown state {s}" for s in sorted(bad)]
    items = load(os.path.join(root, "dev", "queue.json"))
    ids = [i["id"] for i in items]
    if len(ids) != len(set(ids)):
        out.append("queue: duplicate ids")
    for i in items:
        loop = loops.get(i["loop"])
        if loop is None:
            out.append(f"queue {i['id']}: no loop {i['loop']}")
        elif i["state"] not in loop["states"]:
            out.append(f"queue {i['id']}: state {i['state']} is not a state of loop {i['loop']}")
        if i.get("layer") not in ("product", "dev-kit"):   # a team item belongs to the owner's working repository
            out.append(f"queue {i['id']}: layer {i.get('layer')} is not product or dev-kit")
    return out


def needs(root, paths):
    """pirillo_studio/host.py NEEDS is exact: every s.<name> the core reads is declared, and every declared name is read."""
    host = os.path.join("pirillo_studio", "host.py")
    if host not in paths:
        return []
    def tree(p):
        with open(os.path.join(root, p)) as f:
            return ast.parse(f.read())
    node = next(n for n in tree(host).body if isinstance(n, ast.Assign) and n.targets[0].id == "NEEDS")
    declared = {n for group in ast.literal_eval(node.value).values() for n in group}
    read = {x.attr for p in paths if p.startswith("pirillo_studio" + os.sep) and p.endswith(".py")
            and not os.path.basename(p).startswith("test_") for x in ast.walk(tree(p))
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "s"}
    found = [f"s.{n}: read by the core but not in {host} NEEDS" for n in sorted(read - declared)] + \
        [f"{n}: in {host} NEEDS but no core module reads s.{n}" for n in sorted(declared - read)]
    callbacks = set(ast.literal_eval(node.value)["callbacks"])
    allowed = set(ast.literal_eval(next(n for n in tree(host).body if isinstance(n, ast.Assign)
                                        and n.targets[0].id == "HOST_IMPORTS").value))
    for p in (p for p in paths if p.startswith("pirillo_studio" + os.sep) and p.endswith(".py")
              and not os.path.basename(p).startswith("test_")):
        for x in ast.walk(tree(p)):
            if isinstance(x, (ast.Import, ast.ImportFrom)) and not getattr(x, "level", 0):
                roots = [a.name for a in x.names] if isinstance(x, ast.Import) else [x.module]
                found += [f"{p} line {x.lineno}: imports {r}, which resolves only in a host" for r in roots
                          if r not in allowed and importlib.util.find_spec(r.split(".")[0]) is None]
            if isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id == "__import__":
                found.append(f"{p} line {x.lineno}: __import__ reaches a module the check cannot name")
            f = x.func if isinstance(x, ast.Call) else None
            while isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute):
                f = f.value
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "s" \
                    and f.attr in callbacks and any(isinstance(a, ast.Name) and a.id == "s" for a in x.args):
                found.append(f"{p} line {x.lineno}: hands s to the host callback {f.attr}; the host supplies itself")
    return found


DEV = re.compile(r"(^|[/\\])dev($|[./\\])")   # the module dev, dev.x, or a path through dev/
LOADERS = {"__import__", "import_module", "run_module", "run_path", "spec_from_file_location", "SourceFileLoader"}


def product_imports(root, paths):
    """The product never imports the dev kit: no import or from dev, and no __import__, importlib or runpy of dev."""
    out = []
    for p in (p for p in paths if p.startswith("pirillo_studio" + os.sep) and p.endswith(".py")):
        with open(os.path.join(root, p)) as f:
            tree = ast.parse(f.read())
        loaders = LOADERS | {a.asname for x in ast.walk(tree) if isinstance(x, ast.ImportFrom)
                             for a in x.names if a.name in LOADERS and a.asname}   # from importlib import import_module as im
        for x in ast.walk(tree):
            names = [a.name for a in x.names] if isinstance(x, ast.Import) else \
                [x.module or ""] if isinstance(x, ast.ImportFrom) and not x.level else []
            if isinstance(x, ast.Call) and getattr(x.func, "id", getattr(x.func, "attr", None)) in loaders:
                names = [c.value for a in x.args + [k.value for k in x.keywords] for c in ast.walk(a)
                         if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            out += [f"{p} line {x.lineno}: the product imports the dev kit ({n})" for n in names if DEV.search(n)]
    return out


def asserts(root, paths):
    """python -O strips assert, so a refusal written as one would pass silently; tests may assert."""
    return [f"{p} line {x.lineno}: assert vanishes under python -O; raise instead"
            for p in paths if p.endswith(".py") and not os.path.basename(p).startswith("test_")
            for x in ast.walk(ast.parse(open(os.path.join(root, p)).read())) if isinstance(x, ast.Assert)]


def patterns(path):
    """The owner's private patterns, kept outside this public repository: one regex a line, # for a comment."""
    with open(path) as f:
        lines = [(n, l.strip()) for n, l in enumerate(f, 1) if l.strip() and not l.startswith("#")]
    if not lines:   # an empty list would protect nothing
        raise SystemExit(f"{path} holds no patterns")
    pats = []
    for n, l in lines:
        try:
            pats.append((n, re.compile(l)))
        except re.error:   # re's message quotes the pattern
            raise SystemExit(f"{path} line {n}: not a valid pattern") from None
    return pats


def private(root, paths, pats):
    out = []
    for p in paths:
        with open(os.path.join(root, p), "rb") as f:
            text = f.read().decode("utf-8", "replace")
        out += [f"{p}: path matches private pattern on line {n}" for n, r in pats if r.search(p)]
        out += [f"{p}: content matches private pattern on line {n}" for n, r in pats if r.search(text)]
    return out


def problems(root):
    """Every problem; with PIRILLO_PRIVATE_PATTERNS set, private paths and content too, and no message shows private text."""
    repo, paths = load(os.path.join(root, "repo.json")), files(root)
    found = layout(paths, repo) + sizes(root, paths, repo) + packages(paths) + queue(root) + needs(root, paths) + asserts(root, paths) \
        + product_imports(root, paths)
    if "PIRILLO_PRIVATE_PATTERNS" not in os.environ:
        return found
    pats = patterns(os.environ["PIRILLO_PRIVATE_PATTERNS"])
    found += private(root, paths, pats)
    for p in (p for p in paths if any(r.search(p) for _, r in pats)):   # an anchored path pattern misses a message
        found = [m.replace(p, "<private>") for m in found]
    for _, r in pats:
        found = [r.sub("<private>", m) for m in found]
    return found


def main(argv):
    if "-h" in argv or "--help" in argv:
        print(__doc__.strip())
        return 0
    args = [a for a in argv if a != "--json"]
    root = args[0] if args else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    found = problems(root)
    if "--json" in argv:
        print(json.dumps({"ok": not found, "state": "clean" if not found else "problems",
                          "next_action": None if not found else "fix each problem in detail", "detail": found}))
    else:
        print("\n".join(found) or "check: clean")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
