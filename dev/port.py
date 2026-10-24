"""Port host code into the core mechanically, and land both halves (docs/architecture.md, "How a domain ports").

usage: python3 dev/port.py move <host file> <core module> <name> ...
       python3 dev/port.py land <host root> <review> <core patch> <host patch> <host base> "<core message>" "<host message>"
       python3 dev/port.py bind <host root> <host base> <host patch> <core checkout>   (land's step, and its finish command's)
move: the named top-level functions, constants and classes leave <host file> for <core module> (pirillo_studio/x.py)
      as their exact source, except that each host global becomes s.<name> (added to pirillo_studio/host.py NEEDS),
      each moved function takes s first, and calls between them pass it. The host keeps one-line forwarders through
      core(). Anything it cannot resolve statically is refused, every case listed, and nothing is written.
land: <core patch> (it must change dev/queue.json) staged index-only and landed through dev/land.py; then <host patch>
      landed in <host root> through `studio.py land commit --patch --base <host base>` (the host
      commit the patch was diffed from) with a Core-change line (Core-queue when the core patch changes no core code)
      and, when the host has tools/package_core.py, its packaged-core fixture rebound to the new core commit,
      its tests importing the new core from an export (PYTHONPATH), which pushes the host half itself; only once the
      live host holds that commit are the core's live files written, so the live host never runs a core whose NEEDS it
      lacks. Prints both commits and the callback count.
"""
import os, shlex, subprocess, sys, tempfile
from pirillo_pipes import source_move
from pirillo_pipes.source_move import render_needs, host_top, scopes


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST_PY = os.path.join(ROOT, "pirillo_studio", "host.py")
DEBT = ("imports", "callbacks")      # NEEDS groups the release must empty; the rest is the public host interface


def read_needs():
    return source_move.read_needs(HOST_PY)


def callbacks(needs):
    return source_move.callbacks(needs, HOST_PY, DEBT)


def move(host, target, names):
    return source_move.move(host, target, names, ROOT, HOST_PY, DEBT, "dev/port.py")


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


def bind(host_root, base, patch, new):
    """The host patch with the host's packaged-core fixture rebound to the core commit checked out at `new` (its
    tools/package_core.py <core>), which exists only once this landing commits it; written beside the patch."""
    if not os.path.exists(os.path.join(host_root, "tools", "package_core.py")):
        return patch
    sha = run("git", "-C", new, "rev-parse", "HEAD")
    out = f"{patch[:-6] if patch.endswith('.patch') else patch}-core-{sha[:8]}.patch"
    with tempfile.TemporaryDirectory() as tmp:
        h = os.path.join(tmp, "host")
        run("git", "-C", host_root, "worktree", "add", "-q", "--detach", h, base)
        try:
            run("git", "apply", patch, cwd=h)
            run(sys.executable, "package_core.py", new, cwd=os.path.join(h, "tools"))
            run("git", "add", "-A", cwd=h)
            diff = run("git", "diff", "--cached", "--binary", base, cwd=h)
            if sha not in diff:  # the base's package_core.py may predate the fixture it binds, and exit 0 having bound nothing
                raise SystemExit(f"bind refused: tools/package_core.py at host {base[:9]} bound nothing to core {sha[:9]}; "
                                 "re-make the host patch on a host commit whose package_core.py binds its fixture")
            with open(out, "w") as f:
                f.write(diff + "\n")
        finally:
            run("git", "-C", host_root, "worktree", "remove", "--force", h)
    return out


def land(host_root, review, core, patch, base, core_msg, host_msg):
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
        at_base = dict(os.environ, GIT_INDEX_FILE=os.path.join(host_root, run("git", "-C", host_root, "rev-parse", "--git-path", "port-base-index")))
        run("git", "-C", host_root, "read-tree", base, env=at_base)   # land.py applies it at base; the live files lag and carry others' edits
        run("git", "-C", host_root, "apply", "--cached", "--check", patch, env=at_base)
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
    code = any(p.startswith(("pirillo_studio/", "dev/")) and p.endswith(".py") and not os.path.basename(p).startswith("test_") for p in paths)
    link = f"Core-{'change' if code else 'queue'}: {sha}"  # the host proves a Core-change sha changed core code (workflow_rules)
    msg, env = f"{body}\n\n{link}{sep}{trailers}", dict(os.environ)
    live = f"git -C {q(ROOT)} apply --reverse --check {q(core)} 2>/dev/null || git -C {q(ROOT)} apply {q(core)}"
    follows = f"git -C {q(host_root)} merge-base --is-ancestor \"$(git -C {q(host_root)} log origin/main -1 -F --grep {q(link)} --format=%H)\" HEAD"
    with tempfile.TemporaryDirectory() as tmp:
        new = os.path.join(tmp, "core")  # a detached worktree, not an archive: the host's Core-change check proves sha from its git
        run("git", "worktree", "add", "-q", "--detach", new, sha)
        env["PYTHONPATH"] = new  # ahead of the host's .pth, so its export tests import the new core, not the live one
        try:  # land commit --patch tests in an export and writes the host's live files only inside its commit
            run("git", "push", "-q", "origin", "main")  # up to date unless dev/land.py's own push failed
            bound = bind(host_root, base, patch, new)
            print(run(*studio, "commit", msg, "--patch", bound, "--base", base, cwd=host_root, env=env))
        except (SystemExit, OSError) as e:
            cli, port = " ".join(map(q, studio)), " ".join(map(q, (sys.executable, os.path.abspath(__file__), "bind")))
            raise SystemExit(f"{e}\nThe core landed at {sha}; neither its live files nor the host half are live. Finish "
                             f"with this command (safe to run twice: it skips a host commit that already names the core "
                             f"and live files already written, and writes those only once the live host holds its commit):\ngit -C {q(ROOT)} push origin main && cd {q(host_root)} && git fetch -q origin main && "
                             f"{{ git log origin/main -F --grep {q(link)} --format=%h | grep -q . || {{ d=\"$(mktemp -d)/core\" "
                             f"&& git -C {q(ROOT)} worktree add -q --detach \"$d\" {sha} && p=\"$({port} {q(host_root)} {q(base)} {q(patch)} \"$d\")\" && PYTHONPATH=\"$d\" {cli} commit {q(msg)} "
                             f"--patch \"$p\" --base {q(base)}; r=$?; git -C {q(ROOT)} worktree remove --force \"$d\"; [ $r = 0 ]; }}; }} "
                             f"&& {follows} && {{ {live}; }}")
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", new], cwd=ROOT, capture_output=True)
    host = run("git", "-C", host_root, "log", "origin/main", "-1", "-F", "--grep", link, "--format=%H")
    if not host:
        raise SystemExit(f"land refused: no host commit on origin/main names {link}, though land commit exited 0")
    if subprocess.run(["git", "-C", host_root, "merge-base", "--is-ancestor", host, "HEAD"]).returncode:  # land.py's follow waits for a render or a live edit
        raise SystemExit(f"land refused: the live host has not followed its pushed commit {host}. The core landed at {sha}; "
                         f"this makes the core live once the live host holds it: {follows} && {{ {live}; }}")
    if changed(head, paths) or subprocess.run(["git", "apply", core], cwd=ROOT).returncode:
        raise SystemExit(f"land refused: the live core changed at a landed path during the host half. The core landed at "
                         f"{sha} and the host's commit is pushed and live; once that edit is resolved, make the core live: {live}")
    print(f"core {sha}\nhost {host}\ncallbacks {callbacks(read_needs()[2])}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["bind"] and len(a) == 5:
        print(bind(*a[1:]))
    elif a[:1] == ["move"] and len(a) >= 4:
        move(a[1], a[2], a[3:])
    elif a[:1] == ["land"] and len(a) == 8:
        land(*a[1:])
    else:
        print(__doc__.strip())
        sys.exit(0 if "-h" in a or "--help" in a else 2)
