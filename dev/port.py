"""Port host code into the core mechanically, and land both halves (docs/architecture.md, "How a domain ports").

usage: python3 dev/port.py move <host file> <core module> <name> ...
       python3 dev/port.py land <host root> <who> <review> <core patch> <host patch> <host base> "<core message>" "<host message>"
move: the named top-level functions, constants and classes leave <host file> for <core module> (pirillo_studio/x.py)
      as their exact source, except that each host global becomes s.<name> (added to pirillo_studio/host.py NEEDS),
      each moved function takes s first, and calls between them pass it. The host keeps one-line forwarders through
      core(). Anything it cannot resolve statically is refused, every case listed, and nothing is written.
land: <core patch> (it must change dev/queue.json) staged index-only and landed through dev/land.py; then <host patch>
      landed in <host root> through `studio.py land begin <who>` and `land commit --patch --base <host base>` (the host
      commit the patch was diffed from) with a Core-change line,
      its tests importing the new core from an export (PYTHONPATH); only then are the core's live files written, and
      both pushed. Nothing goes live before both commits exist, so the live host never runs a core whose NEEDS it
      lacks. Prints both commits and the callback count.
"""
import ast, builtins, os, re, shlex, subprocess, symtable, sys, tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST_PY = os.path.join(ROOT, "pirillo_studio", "host.py")
DEBT = ("imports", "callbacks")      # NEEDS groups the release must empty; the rest is the public host interface


def read_needs():
    with open(HOST_PY) as f:
        text = f.read()
    node = next(n for n in ast.parse(text).body if isinstance(n, ast.Assign) and n.targets[0].id == "NEEDS")
    return text, node, ast.literal_eval(node.value)


def render_needs(needs):
    out = ["NEEDS = {"]
    for group, names in needs.items():
        line, rows = f'    "{group}": (', []
        for n in sorted(names, key=str.lower):
            item = f'"{n}", '
            if len(line) + len(item) > 116:
                rows.append(line.rstrip())
                line = " " * 8
            line += item
        rows.append(line.rstrip().rstrip(",") + ("," if len(names) == 1 else "") + "),")
        out += rows
    return "\n".join(out + ["}"])


def callbacks(needs):
    with open(HOST_PY) as f:
        imports = next(n for n in ast.parse(f.read()).body if isinstance(n, ast.Assign) and n.targets[0].id == "HOST_IMPORTS")
    return sum(len(needs[g]) for g in DEBT) + len(ast.literal_eval(imports.value))


def host_top(tree):
    """{name: node} for the host's top-level defs and single-name assignments, every top-level name, and imports."""
    nodes, names, imports, twice = {}, set(), {}, set()
    def visit(body):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound = [n.name]
            elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                bound = [x.id for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
                         for x in ast.walk(t) if isinstance(x, ast.Name)]
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                bound = []
                for a in n.names:
                    alias = a.asname or a.name.split(".")[0]
                    names.add(alias)
                    root = "." if getattr(n, "level", 0) else (a.name if isinstance(n, ast.Import) else n.module).split(".")[0]
                    stmt = (f"import {a.name}" if isinstance(n, ast.Import) else f"from {'.' * n.level}{n.module or ''} import {a.name}")
                    imports[alias] = (root, stmt + (f" as {a.asname}" if a.asname else ""))
            else:
                bound = []
                for f in ("body", "orelse", "finalbody"):
                    visit(getattr(n, f, []))
                for h in getattr(n, "handlers", []):
                    visit(h.body)
            for b in bound:
                if body is tree.body and b in nodes:
                    twice.add(b)
                names.add(b)
                if body is tree.body and len(bound) == 1:
                    nodes[b] = n
    visit(tree.body)
    return nodes, names, imports, twice


def scopes(table):
    yield table
    for c in table.get_children():
        yield from scopes(c)


def move(host, target, names):
    with open(host, "rb") as f:
        raw = f.read()
    text, lines = raw.decode(), raw.split(b"\n")
    tree = ast.parse(text)
    nodes, top, imports, twice = host_top(tree)
    here = os.path.dirname(os.path.abspath(host))
    local = lambda root: os.path.exists(os.path.join(here, root + ".py")) or os.path.isdir(os.path.join(here, root))
    tables = {t.get_name(): t for t in symtable.symtable(text, host, "exec").get_children()}
    mod = os.path.splitext(os.path.basename(target))[0]
    pkg = os.path.relpath(os.path.dirname(os.path.abspath(target)), ROOT).replace(os.sep, ".")
    refused = [f"{n}: no top-level function, class or single-name assignment in {host}" for n in names if n not in nodes]
    refused += [f"{n}: defined twice at top level; move refuses to guess which" for n in names if n in twice]
    refused += [] if "core" in top else [f"{host} has no core(); add `def core(): return host.View(<this module>)` first"]
    if refused:
        raise SystemExit("move refused:\n  " + "\n  ".join(refused))
    moved = {n: nodes[n] for n in sorted(names, key=lambda n: nodes[n].lineno)}
    funcs = {n for n, x in moved.items() if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))}
    edits, need_imports, to_host = [], set(), set()
    for name, node in moved.items():
        parent = {id(c): p for p in ast.walk(node) for c in ast.iter_child_nodes(p)}
        glob, bound = set(), set()
        if name in funcs:
            a = node.args
            outer = node.decorator_list + a.defaults + [d for d in a.kw_defaults if d] + \
                [x.annotation for x in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg] if x and x.annotation] + \
                ([node.returns] if node.returns else [])
        else:
            outer = node.bases + [k.value for k in node.keywords] + node.decorator_list \
                if isinstance(node, ast.ClassDef) else [node]
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in scopes(tables[name]):
                for sym in t.get_symbols():
                    if sym.is_global() and sym.is_assigned():
                        refused.append(f"{name}: writes the module global {sym.get_name()}")
                    (glob if sym.is_global() else bound if sym.is_local() or sym.is_parameter() else set()).add(sym.get_name())
            refused += [f"{name}: {n} is local in one scope and global in another" for n in sorted(glob & bound)]
            refused += [f"{name}: already uses the name s, which the move adds as the host" for _ in [1] if "s" in glob | bound]
        outer_ids = {id(y) for o in outer for y in ast.walk(o)}
        skip = set()
        for x in ast.walk(node):
            at = f"{name} line {getattr(x, 'lineno', node.lineno)}"
            if isinstance(x, ast.Subscript) and isinstance(x.value, ast.Attribute) and x.value.attr == "modules" \
                    and isinstance(x.value.value, ast.Name) and x.value.value.id == "sys":
                if isinstance(x.slice, ast.Name) and x.slice.id == "__name__" and x.lineno == x.end_lineno:
                    edits.append((x.lineno, x.col_offset, x.end_col_offset, "s", b"sys.modules[__name__]"))
                    skip |= {id(y) for y in ast.walk(x)}
                else:
                    refused.append(f"{at}: a sys.modules lookup other than sys.modules[__name__]")
            elif isinstance(x, ast.Attribute) and x.attr == "modules" and isinstance(x.value, ast.Name) \
                    and x.value.id == "sys" and id(x) not in skip:
                refused.append(f"{at}: sys.modules used other than as sys.modules[__name__]")
            elif isinstance(x, ast.Call) and isinstance(x.func, ast.Name):
                f, arg0 = x.func.id, x.args[0] if x.args else None
                if f in ("exec", "eval", "globals", "vars", "locals"):
                    refused.append(f"{at}: {f}() cannot be resolved statically")
                elif f == "getattr" and not (len(x.args) > 1 and isinstance(x.args[1], ast.Constant)):
                    refused.append(f"{at}: getattr with a computed name")
                elif f == "__import__" and not (isinstance(arg0, ast.Constant) and not local(arg0.value.split(".")[0])):
                    refused.append(f"{at}: __import__ of a host module or a computed name")
            elif isinstance(x, (ast.Import, ast.ImportFrom)) and x is not node:
                roots = [a.name.split(".")[0] for a in x.names] if isinstance(x, ast.Import) else \
                    ["."] if x.level else [x.module.split(".")[0]]
                refused += [f"{at}: imports the host module {r}" for r in roots if r != "." and local(r)]
        for x in ast.walk(node):
            if not isinstance(x, ast.Name) or id(x) in skip or (x.id not in glob and id(x) not in outer_ids):
                continue
            at, n = f"{name} line {x.lineno}", x.id
            if n in moved:
                if n not in funcs:
                    continue
                call = parent.get(id(x))
                if not (isinstance(call, ast.Call) and call.func is x) or id(x) in outer_ids:
                    refused.append(f"{at}: the moved function {n} is used as a value, not called")
                elif lines[x.end_lineno - 1][x.end_col_offset:x.end_col_offset + 1] != b"(":
                    refused.append(f"{at}: the call to {n} is not written {n}(...)")
                else:
                    edits.append((x.end_lineno, x.end_col_offset + 1, x.end_col_offset + 1,
                                  "s, " if call.args or call.keywords else "s", b""))
            elif n in imports and not local(imports[n][0]):
                need_imports.add(imports[n][1])
            elif n in top and name in funcs and id(x) not in outer_ids:
                edits.append((x.lineno, x.col_offset, x.end_col_offset, "s." + n, n.encode()))
                to_host.add(n)
            elif n in top:
                refused.append(f"{at}: {n} is a host name evaluated outside a function, where no s exists")
            elif not hasattr(builtins, n):
                refused.append(f"{at}: {n} is defined nowhere in {host}")
        if name in funcs:
            m = re.search(rb"\bdef\s+" + name.encode() + rb"\s*\(", lines[node.lineno - 1])
            has_args = any([a.posonlyargs, a.args, a.vararg, a.kwonlyargs, a.kwarg])
            edits.append((node.lineno, m.end(), m.end(), "s, " if has_args else "s", b""))
    if os.path.exists(target):
        with open(target) as f:
            old = f.read()
        refused += [f"{n}: already defined in {target}" for n in moved if n in host_top(ast.parse(old))[1]]
    else:
        old = f'"""{", ".join(moved)}: moved from the host by dev/port.py; `s` is the host (pirillo_studio.host.View)."""\n'
    if refused:
        raise SystemExit("move refused, each needs a hand line first:\n  " + "\n  ".join(sorted(set(refused))))
    blocks = []
    for name, node in moved.items():
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        block = lines[start - 1:node.end_lineno]
        for ln, c0, c1, new, was in sorted((e for e in edits if start <= e[0] <= node.end_lineno), reverse=True):
            if block[ln - start][c0:c1] != was:
                raise SystemExit(f"{name} line {ln}: ast position does not hold {was!r}")
            block[ln - start] = block[ln - start][:c0] + new.encode() + block[ln - start][c1:]
        blocks.append(b"\n".join(block).decode())
    tree_old = ast.parse(old)
    have = {ast.get_source_segment(old, n) for n in tree_old.body if isinstance(n, (ast.Import, ast.ImportFrom))}
    add = sorted(need_imports - have, key=lambda s: (s.startswith("from"), s))
    anchor = max([n.end_lineno for n in tree_old.body if isinstance(n, (ast.Import, ast.ImportFrom))] or
                 [tree_old.body[0].end_lineno if tree_old.body and isinstance(tree_old.body[0], ast.Expr) else 0])
    old_lines = old.rstrip("\n").split("\n")
    core_text = "\n".join(old_lines[:anchor] + add + old_lines[anchor:]) + "\n\n\n" + "\n\n\n".join(blocks) + "\n"
    # the host: moved spans (and the blank lines after them) out, forwarders where the first one stood
    drop = set()
    for node in moved.values():
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        end = node.end_lineno
        while end < len(lines) and not lines[end].strip():
            end += 1
        drop |= set(range(start - 1, end))
    first = min(drop)
    fwd = [f"# {', '.join(moved)}: moved to {pkg.replace('.', '/')}/{mod}.py by dev/port.py; these forward to it."]
    fwd += [f"def {n}(*a, **k): return {mod}.{n}(core(), *a, **k)" for n in moved if n in funcs]
    rest = [n for n in moved if n not in funcs]
    fwd += [f"from {pkg}.{mod} import {', '.join(rest)}"] if rest else []
    out = [l.decode() for i, l in enumerate(lines) if i not in drop or i == first]
    at = sum(1 for i in range(first) if i not in drop)
    out[at:at + 1] = fwd + ["", ""]
    if mod not in top:
        last = max(n.end_lineno for n in tree.body if isinstance(n, ast.ImportFrom) and n.module in (pkg, pkg.split(".")[0]))  # a moved constant's `from pkg.mod import X` sits by its old place; a new subpackage has none yet
        if last > first:
            raise SystemExit(f"{host}: a moved name sits above the {pkg} imports")
        out.insert(last, f"from {pkg} import {mod}")
    users = {}
    for o in tree.body:
        if o not in moved.values():
            for y in ast.walk(o):
                if isinstance(y, ast.Name) and y.id in to_host and getattr(o, "name", None) != y.id:
                    users[y.id] = True
    only = sorted(to_host - set(users))
    out_tables = {t.get_name(): t for t in symtable.symtable(core_text, target, "exec").get_children()}
    out_top = host_top(ast.parse(core_text))[1]
    left = sorted({f"{n}: {sym.get_name()}" for n in moved if n in out_tables for t in scopes(out_tables[n])
                   for sym in t.get_symbols() if sym.is_global() and sym.get_name() not in out_top
                   and not hasattr(builtins, sym.get_name())})
    if left:
        raise SystemExit(f"moved code still reads names the core module lacks: {left}")
    htext, hnode, needs = read_needs()
    new = sorted(to_host - {n for g in needs.values() for n in g})
    needs["callbacks"] = tuple(needs["callbacks"]) + tuple(new)
    hlines = htext.split("\n")
    hlines[hnode.lineno - 1:hnode.end_lineno] = render_needs(needs).split("\n")
    with open(target, "w") as f:
        f.write(core_text)
    with open(host, "w") as f:
        f.write("\n".join(out))
    with open(HOST_PY, "w") as f:
        f.write("\n".join(hlines))
    print(f"moved {', '.join(moved)} to {target}; {len(to_host)} host names read through s, {len(new)} new in NEEDS"
          f" ({', '.join(new) or 'none'}); callbacks now {callbacks(needs)}")
    if only:
        print(f"used only by the moved code in {host} (move them too, or keep them as callbacks): {', '.join(only)}")


def run(*cmd, cwd=ROOT, env=None):
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"land refused at `{' '.join(cmd[:4])}`:\n{r.stdout}{r.stderr}".rstrip())
    return r.stdout.strip()


def changed(rev, paths):
    """Whether the live core differs from rev at any of paths, judged by git through an index that holds rev."""
    env = dict(os.environ, GIT_INDEX_FILE=os.path.join(ROOT, run("git", "rev-parse", "--git-path", "port-base-index")))
    run("git", "read-tree", rev, env=env)
    return subprocess.run(["git", "diff", "--quiet", rev, "--", *paths], cwd=ROOT, env=env).returncode or \
        run("git", "ls-files", "--others", "--", *paths, env=env)


def land(host_root, who, review, core, patch, base, core_msg, host_msg):
    core, patch, q = os.path.abspath(core), os.path.abspath(patch), shlex.quote
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode:
        raise SystemExit("land refused: the core's index holds staged work, which would land with this change")
    head = run("git", "rev-parse", "HEAD")
    run("git", "apply", "--cached", core)  # index only: dev/land.py tests an export, and the live files wait for the commit
    try:
        tree = run("git", "write-tree")
        paths = run("git", "diff", "--cached", "--no-renames", "--name-only").split("\n")
        if "dev/queue.json" not in paths:
            raise SystemExit("land refused: the core patch must change dev/queue.json; the item's state flip rides in it")
        if changed(head, paths):
            lag = next((c for c in run("git", "rev-list", "-n", "10", "--skip", "1", "HEAD").split() if not changed(c, paths)), None)
            raise SystemExit(f"land refused: the live core holds {lag}'s files at a path this patch changes: the landing "
                             f"after it is still in flight (its host half writes them); wait for it" if lag else
                             "land refused: the core has uncommitted work at a path this patch changes")
        run("git", "-C", host_root, "apply", "--check", patch)
        print(run(sys.executable, "dev/land.py", "--review", review, core_msg))
    except SystemExit as e:
        now, now_tree = run("git", "rev-parse", "HEAD", "HEAD^{tree}").split("\n")
        if now == head or now_tree != tree:  # refused before its commit
            subprocess.run(["git", "reset", "-q"], cwd=ROOT)  # the index back to HEAD, as checked; no working file changed
            raise SystemExit(f"{e}\nNothing is live: the core's and the host's working files are unchanged.")
        print(f"{e}\ndev/land.py committed the reviewed tree; only its push failed, which is retried below.")
    sha = run("git", "rev-parse", "HEAD")
    if changed(head, paths):
        raise SystemExit("land refused: a landed path changed in the live core during the suite\n"
                         f"The core landed at {sha}, but its live files and the host are unchanged: nothing is live.")
    studio = [sys.executable, os.path.join(host_root, "tools", "studio.py"), "land"]
    body, sep, trailers = host_msg.rstrip().partition("\n\nCo-Authored-By:")
    msg, env = f"{body}\n\nCore-change: {sha}{sep}{trailers}", dict(os.environ)
    live = f"git -C {q(ROOT)} apply --reverse --check {q(core)} 2>/dev/null || git -C {q(ROOT)} apply {q(core)}"
    with tempfile.TemporaryDirectory() as tmp:
        new = os.path.join(tmp, "core")  # a detached worktree, not an archive: the host's Core-change check proves sha from its git
        run("git", "worktree", "add", "-q", "--detach", new, sha)
        env["PYTHONPATH"] = new  # ahead of the host's .pth, so its export tests import the new core, not the live one
        try:  # land commit --patch tests in an export and writes the host's live files only inside its commit
            run("git", "push", "-q", "origin", "main")  # up to date unless dev/land.py's own push failed
            env["STUDIO_LAND_TOKEN"] = re.search(r"STUDIO_LAND_TOKEN=(\S+)", run(*studio, "begin", who, cwd=host_root)).group(1)
            print(run(*studio, "commit", msg, "--patch", patch, "--base", base, cwd=host_root, env=env))
        except SystemExit as e:
            cli = " ".join(map(q, studio))
            raise SystemExit(f"{e}\nThe core landed at {sha}; neither its live files nor the host half are live. Finish "
                             f"with this command (safe to run twice: it skips a host commit that already names the core "
                             f"and live files already written):\ngit -C {q(ROOT)} push origin main && cd {q(host_root)} && "
                             f"{{ git log -F --grep {q('Core-change: ' + sha)} --format=%h | grep -q . || {{ d=\"$(mktemp -d)/core\" "
                             f"&& git -C {q(ROOT)} worktree add -q --detach \"$d\" {sha} && t=\"$({cli} begin {q(who)} | grep "
                             f"'^export STUDIO_LAND_TOKEN=')\" && eval \"$t\" && PYTHONPATH=\"$d\" {cli} commit {q(msg)} "
                             f"--patch {q(patch)} --base {q(base)}; r=$?; PYTHONPATH=\"$d\" {cli} end; e=$?; git -C {q(ROOT)} worktree remove --force \"$d\"; [ $r = 0 ] && [ $e = 0 ]; }}; }} "
                             f"&& {{ {live}; }} && git push origin main")  # live before the push: a failed push never strands the host
        finally:
            subprocess.run(studio + ["end"], cwd=host_root, env=env, capture_output=True)
            subprocess.run(["git", "worktree", "remove", "--force", new], cwd=ROOT, capture_output=True)
    if changed(head, paths) or subprocess.run(["git", "apply", core], cwd=ROOT).returncode:
        raise SystemExit(f"land refused: the live core changed at a landed path during the host half. The core landed at "
                         f"{sha} and the host's commit is live; once that edit is resolved, make the core live and push "
                         f"the host: {live} && git -C {q(host_root)} push origin main")
    run("git", "-C", host_root, "push", "-q", "origin", "main")
    print(f"core {sha}\nhost {run('git', '-C', host_root, 'rev-parse', 'HEAD')}\ncallbacks {callbacks(read_needs()[2])}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["move"] and len(a) >= 4:
        move(a[1], a[2], a[3:])
    elif a[:1] == ["land"] and len(a) == 9:
        land(*a[1:])
    else:
        print(__doc__.strip())
        sys.exit(0 if "-h" in a or "--help" in a else 2)
