"""Repository shape, dependency and private-source checks with caller-owned policy.

Inputs are explicit; malformed or missing input raises rather than producing a pass.
"""


import ast, hashlib, json, os, re, subprocess
from pathlib import Path


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


def layout(paths, allowed_roots, config_name):
    return [f"{n}: not in {config_name} layout" for n in sorted({p.split(os.sep)[0] for p in paths}) if n not in allowed_roots]


def sizes(root, paths, max_lines):
    out = []
    for p in paths:
        with open(os.path.join(root, p), "rb") as f:
            data = f.read()
        n = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        if n > max_lines:
            out.append(f"{p}: {n} lines, over max_lines {max_lines}")
    return out


def packages(paths):
    """unittest discovery skips a folder without __init__.py, so its tests would never run."""
    dirs = set()
    for p in paths:
        parts = p.split(os.sep)[1:-1]  # folders below each discovery root
        if p.endswith(".py"):
            dirs |= {os.path.join(p.split(os.sep)[0], *parts[:i + 1]) for i in range(len(parts))}
    return [f"{d}: holds Python but no __init__.py, so its tests never run" for d in sorted(dirs)
            if os.path.join(d, "__init__.py") not in paths]


def queue(loop_directory, queue_path, allowed_layers):
    if not allowed_layers:
        raise ValueError("Queue layers must be explicit and nonempty")
    d = loop_directory
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
    items = load(queue_path)
    ids = [i["id"] for i in items]
    if len(ids) != len(set(ids)):
        out.append("queue: duplicate ids")
    for i in items:
        loop = loops.get(i["loop"])
        if loop is None:
            out.append(f"queue {i['id']}: no loop {i['loop']}")
        elif i["state"] not in loop["states"]:
            out.append(f"queue {i['id']}: state {i['state']} is not a state of loop {i['loop']}")
        if i.get("layer") not in allowed_layers:
            out.append(f"queue {i['id']}: layer {i.get('layer')} is not {' or '.join(allowed_layers)}")
    return out


LOADERS = {'run_path', 'run_module', '__import__', 'spec_from_file_location', 'SourceFileLoader', 'import_module'}


def dependency_imports(root, paths, forbidden, description):
    """Find forbidden imports and named dynamic loaders in caller-selected Python files."""
    out = []
    for p in paths:
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
            out += [f"{p} line {x.lineno}: {description} ({n})" for n in names if forbidden.search(n)]
    return out


def asserts(root, paths):
    """python -O strips assert, so a refusal written as one would pass silently; tests may assert."""
    out = []
    for p in (p for p in paths if p.endswith(".py") and not os.path.basename(p).startswith("test_")):
        with open(os.path.join(root, p)) as stream:
            tree = ast.parse(stream.read())
        out += [f"{p} line {x.lineno}: assert vanishes under python -O; raise instead"
                for x in ast.walk(tree) if isinstance(x, ast.Assert)]
    return out


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


def redact(found, paths, pats):
    """Redact both anchored private paths and private patterns from diagnostics."""
    for p in (p for p in paths if any(r.search(p) for _, r in pats)):
        found = [m.replace(p, "<private>") for m in found]
    for _, r in pats:
        found = [r.sub("<private>", m) for m in found]
    return found


def vendor_pin(directory):
    """Refuse a missing, changed or unlisted vendored file; the consumer owns the pin's review."""
    root = Path(directory)
    pin_path = root / "PIN.json"
    if root.is_symlink() or pin_path.is_symlink():
        raise ValueError("Pipes vendor pin must use plain files")
    pin = load(pin_path)
    if (not isinstance(pin, dict) or pin.get("schema_version") != 1
            or not isinstance(pin.get("source_repository"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", str(pin.get("source_base_revision")))
            or pin.get("source_revision") is not None
            and not re.fullmatch(r"[0-9a-f]{40}", str(pin["source_revision"]))
            or not isinstance(pin.get("files"), dict) or not pin["files"]):
        raise ValueError("Pipes vendor pin is malformed")
    for name, entry in pin["files"].items():
        relative = Path(name)
        if (relative.is_absolute() or relative.as_posix() != name or "\\" in name
                or any(p in ("..", ".") for p in relative.parts) or not relative.parts
                or name == "PIN.json" or not isinstance(entry, dict)
                or not isinstance(entry.get("source"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256")))):
            raise ValueError("Pipes vendor pin contains an invalid file")
        path = root / relative
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ValueError("Pipes vendor file is a link: " + name)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("Pipes vendor hash mismatch: " + name)
    actual = set()
    for path in root.rglob("*"):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError("Pipes vendor file is a link: " + name)
        if path.is_file() and name != "PIN.json" and not ("__pycache__" in path.parts and path.suffix == ".pyc"):
            actual.add(name)
    if actual != set(pin["files"]):
        raise ValueError("Pipes vendor contains unpinned files: " + ", ".join(sorted(actual - set(pin["files"]))))
