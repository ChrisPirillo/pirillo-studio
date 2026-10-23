"""The only way a change lands: checks and the whole suite run on the exact staged tree,
a review must name that tree, and the commit is made from that tree, never from the index.

usage: git add <files>; python3 dev/land.py --review <file> "<message>"
The review file's first line is `APPROVE <tree> <reviewer>`, where <tree> is `git write-tree`.
PIRILLO_PRIVATE_PATTERNS names the owner's private-pattern file (kept outside this repo); without it nothing lands,
and a message matching it is refused. The commit is made as repo.json `identity`, never the machine's git user.
"""
import json, os, resource, subprocess, sys, tempfile
from check import patterns as private_patterns
from pirillo_pipes import landing

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def git(*args):
    return subprocess.run(["git", "-C", ROOT, *args], check=True, capture_output=True, text=True).stdout.strip()


def export(tree, dest):
    """Write the tree's stored bytes exactly (no filters, attributes or line-ending rules); refuse links and submodules."""
    for entry in git("ls-tree", "-r", "-z", tree).split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        mode, _, sha = meta.split()
        if mode not in ("100644", "100755"):
            raise SystemExit(f"land refused: {path} is mode {mode}; only plain files land")
    try:
        landing.export(ROOT, tree, dest)
    except ValueError as exc:
        raise SystemExit(f"land refused: {exc}") from exc


def suite(tree_dir, budget):
    """budget is the suite's CPU seconds (user and system, every test process): wall time waits on the machine's
    load (271 s wall for 28 s of CPU under other landings' verifies), so it only bounds a hang, at HANG times."""
    start = cpu()
    for d in ("dev", "pirillo_studio"):
        if os.path.isdir(os.path.join(tree_dir, d)):
            try:
                r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", d], cwd=tree_dir,
                                   timeout=HANG * budget)
            except subprocess.TimeoutExpired:
                raise SystemExit(f"land refused: {d} tests hung past {HANG * budget} s") from None
            if r.returncode:
                raise SystemExit(f"land refused: {d} tests failed")
    took = cpu() - start
    if took > budget:
        raise SystemExit(f"land refused: suite took {took:.0f} CPU s, over suite_seconds {budget}")


HANG = 10


def cpu():
    used = resource.getrusage(resource.RUSAGE_CHILDREN)
    return used.ru_utime + used.ru_stime


def main(argv):
    if "-h" in argv or "--help" in argv or len(argv) != 3 or argv[0] != "--review":
        print(__doc__.strip())
        return 0 if "-h" in argv or "--help" in argv else 2
    review, message = argv[1], argv[2]
    patterns, root = os.path.realpath(os.environ.get("PIRILLO_PRIVATE_PATTERNS", "")), os.path.realpath(ROOT)
    if not os.path.isfile(patterns) or os.path.commonpath([patterns, root]) == root:
        raise SystemExit("land refused: PIRILLO_PRIVATE_PATTERNS must name a patterns file outside this repository")
    branch = git("symbolic-ref", "HEAD")  # taken first, so a commit made during the run makes update-ref refuse
    parent = subprocess.run(["git", "-C", ROOT, "rev-parse", "-q", "--verify", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    tree = git("write-tree")
    with open(review) as f:
        first = f.readline().split()
    if len(first) != 3 or first[:2] != ["APPROVE", tree]:
        raise SystemExit(f"land refused: the review's first line must be `APPROVE {tree} <reviewer>` (the reviewer "
                         f"in one word); it reads {' '.join(first)!r}")
    message = f"{message}\n\nReviewed-by: {first[2]}\nTree: {tree}"
    hit = [n for n, r in private_patterns(patterns) if r.search(message)]
    if hit:
        raise SystemExit(f"land refused: the message matches private pattern on line {hit[0]}; the core's history is the product's")
    with tempfile.TemporaryDirectory() as tmp:
        export(tree, tmp)
        if subprocess.run([sys.executable, "dev/check.py", tmp], cwd=tmp).returncode:
            raise SystemExit("land refused: dev/check.py found problems")
        with open(os.path.join(tmp, "repo.json")) as f:
            repo = json.load(f)
        suite(tmp, repo["suite_seconds"])
        try:
            landing.check_export(ROOT, tree, tmp)
        except ValueError as exc:
            raise SystemExit(f"land refused: {exc}") from exc
    name, email = repo["identity"].rsplit(" <", 1)
    os.environ.update(GIT_AUTHOR_NAME=name, GIT_COMMITTER_NAME=name, GIT_AUTHOR_EMAIL=email.rstrip(">"),
                      GIT_COMMITTER_EMAIL=email.rstrip(">"))
    commit = git("commit-tree", tree, *(["-p", parent] if parent else []),
                 "-m", message)
    git("update-ref", branch, commit, parent or "0" * 40)  # refuses if the branch moved meanwhile
    if "origin" in git("remote").split():
        git("push", "-q", "origin", branch)
    print(git("log", "--oneline", "-1"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
