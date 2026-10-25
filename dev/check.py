"""The repository's shape checks: layout, file sizes, test packages, the queue against its loops and layers, the
host boundary (NEEDS), no assert outside tests, no product import of dev, private data; a JSON file with a duplicate
key refuses.

usage: python3 dev/check.py [root] [--json]   exit 0 clean, 1 with problems listed
In a git checkout it checks the files git would commit; elsewhere (land.py's export) every file.
With PIRILLO_PRIVATE_PATTERNS set (land.py requires it), no file name or content matches a pattern in that file.
"""
import ast, importlib.util, json, os, re, sys
from pirillo_pipes import repository_checks as shared_checks
from pirillo_pipes.repository_checks import unique, load, files, packages, asserts, patterns, private, LOADERS


def layout(paths, repo):
    return shared_checks.layout(paths, repo["layout"], "repo.json")


def sizes(root, paths, repo):
    return shared_checks.sizes(root, paths, repo["max_lines"])


def queue(root):
    """The shared queue checks, and a finished item (a state its loop never leaves) on one line: the queue grows by an
    item a landing, and only open items need their lines (queue item 44: it reached max_lines)."""
    path, loops = os.path.join(root, "dev", "queue.json"), os.path.join(root, "dev", "loops")
    out = shared_checks.queue(loops, path, ("product", "dev-kit"))
    ends = {(loop["name"], s) for loop in (load(os.path.join(loops, n)) for n in os.listdir(loops))
            for s in loop["states"] if all(t["from"] != s for t in loop["transitions"])}
    with open(path) as f:
        text = f.read()
    dec, at = json.JSONDecoder(), text.index("[") + 1
    while (at := at + len(text[at:]) - len(text[at:].lstrip(" \t\r\n,"))) < len(text) and text[at] != "]":
        item, end = dec.raw_decode(text, at)
        if (item.get("loop"), item.get("state")) in ends and "\n" in text[at:end]:
            out.append(f"queue {item['id']}: finished ({item['state']}), so one line")
        at = end
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


DEV = re.compile(r"(^|[/\\])dev($|[./\\])")


def product_imports(root, paths):
    selected = [p for p in paths if p.startswith("pirillo_studio" + os.sep) and p.endswith(".py")]
    return shared_checks.dependency_imports(root, selected, DEV, "the product imports the dev kit")


def problems(root):
    """Every problem; with PIRILLO_PRIVATE_PATTERNS set, private paths and content too, and no message shows private text."""
    repo, paths = load(os.path.join(root, "repo.json")), files(root)
    bundle = os.path.join(root, "dev", "pirillo_pipes")
    if os.path.isdir(bundle) or os.path.isfile(os.path.join(root, "dev", "check.py")):
        shared_checks.vendor_pin(bundle)
    found = layout(paths, repo) + sizes(root, paths, repo) + packages(paths) + queue(root) + needs(root, paths) + asserts(root, paths) \
        + product_imports(root, paths)
    if "PIRILLO_PRIVATE_PATTERNS" not in os.environ:
        return found
    pats = patterns(os.environ["PIRILLO_PRIVATE_PATTERNS"])
    found += private(root, paths, pats)
    return shared_checks.redact(found, paths, pats)


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
