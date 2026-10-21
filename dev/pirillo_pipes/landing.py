"""Local, plain-file Git landing primitives; the caller owns serialization and trust.

No command here approves a candidate, runs its code, pushes, or resets a checkout.
A crash after the ref update can leave a held, partial materialization: outcome()
refuses it rather than overwriting edits to manufacture a clean receipt.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile


_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _run(repo, args, input=None, env=None):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    clean.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                 GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1")
    clean.update(env or {})
    command = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
               "-c", "core.untrackedCache=false", "-c", "commit.gpgsign=false", "-C", str(repo), *args]
    try:
        return subprocess.run(command, input=input, capture_output=True, check=True,
                              timeout=60, env=clean).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Git operation refused: " + " ".join(args[:2])) from exc


def git(repo, *args, input=None, env=None) -> str:
    """Bounded Git with ambient Git overrides, hooks and global configuration disabled."""
    if isinstance(input, str):
        input = input.encode("utf-8", "surrogateescape")
    return _run(repo, args, input, env).decode("utf-8", "surrogateescape").rstrip("\n")


def _object(repo, oid, kind):
    if not isinstance(oid, str) or not _OID.fullmatch(oid) or git(repo, "cat-file", "-t", oid) != kind:
        raise ValueError("Expected exact Git " + kind + " object")


def _relative(name):
    p = PurePosixPath(name)
    if (not name or p.is_absolute() or str(p) != name or "\\" in name or "\0" in name
            or any(s in (".", "..") or s.casefold() == ".git" for s in p.parts)):
        raise ValueError("Unsafe Git path")
    return p


def _entries(repo, tree):
    _object(repo, tree, "tree")
    result = {}
    for item in git(repo, "ls-tree", "-r", "-z", tree).split("\0"):
        if item:
            meta, name = item.split("\t", 1)
            mode, kind, oid = meta.split()
            _relative(name)
            if mode not in ("100644", "100755") or kind != "blob":
                raise ValueError("Only plain files land: " + name)
            result[name] = (mode, oid)
    return result


def _path(root, name):
    p = root.joinpath(*_relative(name).parts)
    for parent in (p, *p.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("Symbolic link refused: " + name)
    return p


def export(repo, tree, dest) -> None:
    """Export stored bytes, ignoring checkout/export attributes; destination must be empty."""
    entries = _entries(repo, tree)
    root = Path(dest)
    if root.is_symlink() or not root.is_dir() or any(root.iterdir()):
        raise ValueError("Export destination must be an empty plain directory")
    root = root.resolve()
    for name, (mode, oid) in entries.items():
        target = _path(root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(_run(repo, ["cat-file", "blob", oid]))
        target.chmod(0o755 if mode == "100755" else 0o644)
    check_export(repo, tree, root)


def check_export(repo, tree, directory) -> None:
    """Checks may add outputs, but cannot mutate or shadow the frozen candidate."""
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Candidate export root changed")
    root = root.resolve()
    for name, (mode, oid) in _entries(repo, tree).items():
        target = _path(root, name)
        if not target.is_file():
            raise ValueError("Candidate export file missing: " + name)
        actual_mode = "100755" if target.stat().st_mode & 0o111 else "100644"
        if actual_mode != mode or target.read_bytes() != _run(repo, ["cat-file", "blob", oid]):
            raise ValueError("Candidate export changed: " + name)


def capture(workspace, index_path) -> str:
    """Freeze actual tracked/new unignored bytes without changing the worker's index."""
    root = Path(workspace).resolve()
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("Workspace must be the Git checkout root")
    index = Path(index_path)
    if (not index.is_absolute() or index.exists() or index.is_symlink()
            or Path(str(index) + ".lock").exists() or index.resolve().is_relative_to(root)):
        raise ValueError("A new private index outside the workspace is required")
    env = {"GIT_INDEX_FILE": str(index)}
    names = set(filter(None, git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")))
    for entry in git(root, "ls-files", "--stage", "-z").split("\0"):
        if entry and (entry.split(" ", 1)[0] not in ("100644", "100755")
                      or entry.split("\t", 1)[0].rsplit(" ", 1)[1] != "0"):
            raise ValueError("Linked or conflicted index entry refused")
    try:
        git(root, "read-tree", "--empty", env=env)
        records = []
        for name in sorted(names):
            path = _path(root, name)
            if not path.exists():
                continue  # tracked deletion
            before = path.stat()
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Only plain workspace files can be captured: " + name)
            with path.open("rb") as stream:
                data = stream.read()
            after = path.stat()
            if any(getattr(before, k) != getattr(after, k) for k in
                   ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")):
                raise ValueError("Workspace changed during capture: " + name)
            oid = git(root, "hash-object", "-w", "--no-filters", "--stdin", input=data)
            mode = "100755" if before.st_mode & 0o111 else "100644"
            records.append((mode + " " + oid + "\t" + name + "\0").encode("utf-8", "surrogateescape"))
        git(root, "update-index", "-z", "--index-info", input=b"".join(records), env=env)
        return git(root, "write-tree", env=env)
    finally:
        if index.exists():
            index.unlink()


def prepare_commit(repo, base, tree, message, identity, transaction) -> str:
    """Create an uninstalled exact-tree commit; identity and transaction are explicit."""
    _object(repo, base, "commit")
    _entries(repo, tree)
    if (not isinstance(identity, str) or not re.fullmatch(r"[^<>\r\n]+ <[^<>\s]+>", identity)
            or not isinstance(transaction, str) or not transaction.strip()
            or any(c in transaction for c in "\r\n\0") or not isinstance(message, str)
            or "\0" in message or "Pipes-Transaction:" in message):
        raise ValueError("Explicit identity, message and single-line transaction required")
    name, email = identity.rsplit(" <", 1)
    env = {"GIT_AUTHOR_NAME": name, "GIT_COMMITTER_NAME": name,
           "GIT_AUTHOR_EMAIL": email[:-1], "GIT_COMMITTER_EMAIL": email[:-1]}
    return git(repo, "commit-tree", tree, "-p", base, "-m",
               message + "\n\nPipes-Transaction: " + transaction, env=env)


def _binding(repo, branch, base, tree, commit):
    if not isinstance(branch, str) or not branch.startswith("refs/heads/"):
        raise ValueError("Exact local branch required")
    git(repo, "check-ref-format", branch)
    for oid, kind in ((base, "commit"), (tree, "tree"), (commit, "commit")):
        _object(repo, oid, kind)
    if (git(repo, "rev-parse", commit + "^{tree}") != tree
            or git(repo, "rev-list", "--parents", "-n", "1", commit).split() != [commit, base]):
        raise ValueError("Prepared commit parent/tree mismatch")
    if git(repo, "symbolic-ref", "HEAD") != branch:
        raise ValueError("Checkout branch changed")


def _clean(repo, tree):
    expected = _entries(repo, tree)
    indexed = {}
    for entry in git(repo, "ls-files", "--stage", "-z").split("\0"):
        if entry:
            meta, name = entry.split("\t", 1)
            mode, oid, stage = meta.split()
            if stage != "0":
                raise ValueError("Conflicted source index")
            indexed[name] = (mode, oid)
    if indexed != expected:
        raise ValueError("Source index differs from expected tree")
    with tempfile.TemporaryDirectory(prefix="pipes-capture-") as folder:
        if capture(repo, Path(folder) / "index") != tree:
            raise ValueError("Source files differ from expected tree")


def outcome(repo, branch, base, tree, commit) -> str:
    """Readback only: partial/dirty/changed destinations remain held."""
    _binding(repo, branch, base, tree, commit)
    head = git(repo, "rev-parse", "--verify", branch)
    if head == base:
        _clean(repo, git(repo, "rev-parse", base + "^{tree}"))
        return "base"
    if head == commit:
        _clean(repo, tree)
        return "installed"
    raise ValueError("Destination branch conflicts with landing intent")


def install(repo, branch, base, tree, commit) -> None:
    """Caller holds its repository lock; uncertain materialization is held, never reset."""
    if outcome(repo, branch, base, tree, commit) == "installed":
        return
    root = Path(repo).resolve()
    old = _entries(repo, git(repo, "rev-parse", base + "^{tree}"))
    new = _entries(repo, tree)
    # Refuse ignored collisions too: capture deliberately excludes ignored new files.
    for name in new.keys() - old.keys():
        target = _path(root, name)
        if target.exists():
            raise ValueError("New destination already exists: " + name)
    with tempfile.TemporaryDirectory(prefix="pipes-export-") as folder:
        export(repo, tree, folder)
        if outcome(repo, branch, base, tree, commit) != "base":
            raise ValueError("Destination changed before ref update")
        git(repo, "update-ref", branch, commit, base)
        # The journal already holds commit intent. Failure from here requires operator
        # reconciliation; no reset can distinguish a fresh user edit from a crash.
        for name in old.keys() - new.keys():
            target = _path(root, name)
            target.unlink()
            parent = target.parent
            while parent != root:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        for name, (mode, oid) in new.items():
            if old.get(name) == (mode, oid):
                continue
            target = _path(root, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            data = (Path(folder) / name).read_bytes()
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".pipes-", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                    os.fchmod(stream.fileno(), 0o755 if mode == "100755" else 0o644)
                os.replace(temporary, target)  # replace the entry, never write through a hard link
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
        git(repo, "read-tree", tree)
        if outcome(repo, branch, base, tree, commit) != "installed":
            raise ValueError("Landing readback did not prove installation")
