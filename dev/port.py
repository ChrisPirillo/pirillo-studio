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
import os, re, shlex, subprocess, sys, tempfile
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
