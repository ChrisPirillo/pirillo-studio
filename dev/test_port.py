import os, shlex, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

import port

HOST = '''import datetime as dt
import sys
import helper
from pirillo_studio import counts

LIMIT = 3


def core():
    return None


def now():
    return dt.datetime.now()


def tick(cfg):
    """Its docstring moves too."""
    t = now()
    me = sys.modules[__name__]
    helper.ping(me, f"{t} {cfg}")
    return prepare(cfg, LIMIT) + [x for x in range(LIMIT) if x]


def prepare(cfg, n):
    return [dt.timedelta(n)]


def bad():
    import helper
    return eval("1") + getattr(sys.modules[__name__], "x" + str(LIMIT))


def other():
    return tick({})


def shadows(x):
    s = 3
    return LIMIT + s
'''


class Move(unittest.TestCase):
    """e2e: dev/port.py move writes pirillo_studio/clock.py, the host's forwarders and NEEDS, and refuses what it cannot resolve."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        os.makedirs(os.path.join(self.root, "core", "pirillo_studio"))
        os.makedirs(os.path.join(self.root, "host"))
        self.host = os.path.join(self.root, "host", "studio.py")
        self.files = {self.host: HOST, os.path.join(self.root, "host", "helper.py"): "",
                      os.path.join(self.root, "core", "pirillo_studio", "host.py"):
                          'NEEDS = {\n    "ledger": ("load",),\n    "imports": (),\n    "callbacks": (),\n}\nHOST_IMPORTS = ()\n'}
        for p, text in self.files.items():
            with open(p, "w") as f:
                f.write(text)
        for k, v in (("ROOT", os.path.join(self.root, "core")),
                     ("HOST_PY", os.path.join(self.root, "core", "pirillo_studio", "host.py"))):
            patch = mock.patch.object(port, k, v)
            patch.start()
            self.addCleanup(patch.stop)
        self.target = os.path.join(self.root, "core", "pirillo_studio", "clock.py")

    def read(self, p):
        with open(p) as f:
            return f.read()

    def test_move_rewrites_only_the_boundary(self):
        port.move(self.host, self.target, ["tick", "prepare"])
        core = self.read(self.target)
        self.assertIn("import datetime as dt\n", core)
        for line in ("def tick(s, cfg):", "    t = s.now()", "    me = s", '    s.helper.ping(me, f"{t} {cfg}")',
                     "    return prepare(s, cfg, s.LIMIT) + [x for x in range(s.LIMIT) if x]", "def prepare(s, cfg, n):"):
            self.assertIn(line + "\n", core)
        compile(core, self.target, "exec")
        host = self.read(self.host)
        self.assertIn("from pirillo_studio import counts\nfrom pirillo_studio import clock\n", host)
        self.assertIn("def tick(*a, **k): return clock.tick(core(), *a, **k)\n", host)
        self.assertNotIn("dt.timedelta(n)", host)
        self.assertIn("def other():\n    return tick({})", host)
        self.assertIn('"callbacks": ("helper", "LIMIT", "now"),', self.read(port.HOST_PY))

    def test_move_into_a_new_subpackage_imports_after_the_core_imports(self):
        """defect: queue item 13 seam 1 (a first pirillo_studio/board/ module) raised ValueError in move's import anchor."""
        os.makedirs(os.path.join(self.root, "core", "pirillo_studio", "board"))
        port.move(self.host, os.path.join(self.root, "core", "pirillo_studio", "board", "asks.py"), ["prepare"])
        self.assertIn("from pirillo_studio import counts\nfrom pirillo_studio.board import asks\n", self.read(self.host))

    def test_refuses_everything_unresolved_and_writes_nothing(self):
        for names, whys in ((["tick", "missing"], ["missing: no top-level"]),
                            (["bad"], ["imports the host module helper", "eval() cannot", "getattr with a computed"]),
                            (["shadows"], ["already uses the name s"])):
            with self.assertRaises(SystemExit) as e:
                port.move(self.host, self.target, names)
            for why in whys:
                self.assertIn(why, str(e.exception))
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(self.read(self.host), HOST)


MEDDLE = """import os, unittest


class Meddle(unittest.TestCase):
    \"\"\"guard: fixture for test_port.Landing, which edits the live core far from the patch while the suite runs.\"\"\"

    def test_meddle(self):
        with open(os.environ["PORT_MEDDLE"]) as f:
            text = f.read()
        with open(os.environ["PORT_MEDDLE"], "w") as f:
            f.write(text.replace("\\n", "\\n\\n", 1))
"""

LAND_COMMIT = """\
    g = ['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', 'scratch']   # land.py: commit and push off the live checkout
    subprocess.run(['git', 'worktree', 'add', '-q', '--detach', 'scratch', 'origin/main'], check=True)
    subprocess.run(g + ['apply', '--index', os.path.abspath(sys.argv[5])], check=True)
    subprocess.run(g + ['commit', '-qm', sys.argv[3]], check=True)
    subprocess.run(g + ['push', '-q', 'origin', 'HEAD:main'], check=True)
    subprocess.run(['git', 'worktree', 'remove', '--force', 'scratch'], check=True)
    subprocess.run(['git', 'merge', '-q', '--ff-only', 'origin/main'])   # land.py's follow, which skips a conflicting live edit
"""


class Landing(unittest.TestCase):
    """guard: a core landing makes nothing live before both halves commit, tests the host half against the new core
    (the live host never imports a core whose NEEDS it lacks), and never pushes a host half that did not commit."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.repo, self.host = os.path.join(self.d, "core"), os.path.join(self.d, "host")
        for n in ("repo.json", "dev/check.py", "dev/land.py", "dev/port.py", "dev/queue.json", "dev/loops/build.json"):
            os.makedirs(os.path.dirname(os.path.join(self.repo, n)), exist_ok=True)
            shutil.copy(os.path.join(root, n), os.path.join(self.repo, n))
        shutil.copytree(os.path.join(root, "dev", "pirillo_pipes"), os.path.join(self.repo, "dev", "pirillo_pipes"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.write(self.repo, "dev/old.json", "{}\n")
        self.write(self.host, "tools/studio.py", "import sys\n"
                   "sys.exit('land commit refused: fixture' if sys.argv[2] == 'commit' else 0)\n")
        for d in ("core", "host"):
            self.git(self.d, "init", "-q", "--bare", d + ".git")
            self.git(os.path.join(self.d, d), "init", "-q", "-b", "main")
            self.git(os.path.join(self.d, d), "add", "-A")
            self.git(os.path.join(self.d, d), "commit", "-q", "-m", "base")
            self.git(os.path.join(self.d, d), "remote", "add", "origin", os.path.join(self.d, d + ".git"))
            self.git(os.path.join(self.d, d), "push", "-q", "origin", "main")
        self.git(self.host, "tag", "base")   # the host patch base, as land commit --base names it
        with open(os.path.join(self.repo, "dev/queue.json")) as f:
            self.before = f.read()
        self.patterns = self.write(self.d, "outside/patterns.txt", "zz-no-such-private-thing/\n")
        self.hostp = self.write(self.d, "host.patch", "diff --git a/x b/x\nnew file mode 100644\n--- /dev/null\n"
                                "+++ b/x\n@@ -0,0 +1 @@\n+x\n")
        self.patch("delete", lambda w: self.git(w, "rm", "-q", "dev/old.json"))

    def patch(self, name, change):
        """A reviewed core patch: the queue change and change(clone), as self.core and self.review."""
        work = os.path.join(self.d, name)
        self.git(self.d, "clone", "-q", self.repo, work)
        self.write(work, "dev/queue.json", self.before + "\n")
        change(work)
        self.git(work, "add", "-A")
        self.core = self.write(self.d, name + ".patch", self.git(work, "diff", "--cached", "--binary", "-M") + "\n")
        self.review = self.write(self.d, name + ".review", f"APPROVE {self.git(work, 'write-tree')} tester\n")
        return self.core

    def write(self, d, n, text):
        os.makedirs(os.path.dirname(os.path.join(d, n)), exist_ok=True)
        with open(os.path.join(d, n), "w") as f:
            f.write(text)
        return os.path.join(d, n)

    def git(self, d, *a):
        return subprocess.run(["git", "-C", d, "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True,
                              capture_output=True, text=True).stdout.strip()

    def land(self, patterns, base="base", **env):
        env = dict(os.environ, PIRILLO_PRIVATE_PATTERNS=patterns, **env)
        return subprocess.run([sys.executable, "dev/port.py", "land", self.host, self.review, self.core,
                               self.hostp, base, "c", "h"], cwd=self.repo, capture_output=True, text=True, env=env)

    def test_the_host_half_carries_its_fixture_rebound_to_the_new_core(self):
        """defect: item 13 seam 3's host half (Working 2026-10-21) was refused at host verify: its fresh-install fixture
        still held a core without the module it imports, and only the landing's own core commit could be bound."""
        self.write(self.host, "tools/package_core.py", "import subprocess, sys\nopen('fixture', 'w').write(subprocess.run("
                   "['git', '-C', sys.argv[1], 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout)\n")
        self.write(self.host, "tools/fixture", "old\n")
        self.git(self.host, "add", "-A")
        self.git(self.host, "commit", "-qm", "fixture")
        base = self.git(self.host, "rev-parse", "HEAD")
        r = self.land(self.patterns, base=base)
        sha = self.git(self.repo, "rev-parse", "HEAD")
        bound = self.hostp[:-6] + f"-core-{sha[:8]}.patch"
        with open(bound) as f:
            self.assertIn(f"+{sha}", f.read())
        os.remove(bound)  # the finish command makes it again: its bind step, as it runs it
        self.assertIn(f"bind {shlex.quote(self.host)} {base} {shlex.quote(self.hostp)} \"$d\"", r.stderr)
        b = subprocess.run([sys.executable, "dev/port.py", "bind", self.host, base, self.hostp, self.repo], cwd=self.repo,
                           capture_output=True, text=True)
        self.assertEqual(b.stdout.strip(), bound)
        with open(bound) as f:
            self.assertIn(f"+{sha}", f.read())
        self.assertEqual(self.git(self.host, "status", "--porcelain"), "")

    def test_a_base_whose_package_core_binds_nothing_is_refused(self):
        """defect: item 5 seam 2's host half (Working 2026-10-22) was diffed from a host commit whose package_core.py had
        no fixture binder; it exited 0, and bind sent the host half to verify with its fixture unbound."""
        self.write(self.host, "tools/package_core.py", "")
        self.git(self.host, "add", "-A")
        self.git(self.host, "commit", "-qm", "no binder")
        base = self.git(self.host, "rev-parse", "HEAD")
        b = subprocess.run([sys.executable, "dev/port.py", "bind", self.host, base, self.hostp, self.repo], cwd=self.repo,
                           capture_output=True, text=True)
        self.assertIn("bind refused: tools/package_core.py at host", b.stderr)
        self.assertEqual(self.git(self.host, "status", "--porcelain"), "")

    def test_refused_core_landing_changes_no_live_file(self):
        r = self.land("")  # dev/land.py refuses without a patterns file
        self.assertIn("PIRILLO_PRIVATE_PATTERNS must name", r.stderr)
        with open(os.path.join(self.repo, "dev/queue.json")) as f:
            self.assertEqual(f.read(), self.before)
        self.assertEqual(self.git(self.repo, "status", "--porcelain", "--untracked-files=no"), "")

    def test_uncommitted_work_at_a_landed_path_is_refused(self):
        delete, rename = self.core, self.patch("rename", lambda w: self.git(w, "mv", "dev/old.json", "dev/new.json"))
        old = os.path.join(self.repo, "dev/old.json")
        for edit, patch in ((lambda: self.write(self.repo, "dev/old.json", "{\"mine\": 1}\n"), rename),
                            (lambda: os.chmod(old, 0o755), delete), (lambda: self.write(self.repo, "dev/new.json", ""), rename)):
            self.core = patch
            edit()
            self.assertIn("uncommitted work at a path this patch changes", self.land("").stderr)
            self.git(self.repo, "checkout", "--", ".")
            self.assertEqual(self.git(self.repo, "status", "--porcelain", "--untracked-files=no"), "")

    def test_a_landing_in_flight_is_named_not_called_uncommitted_work(self):
        """defect: seam 19's first land (2026-10-17) was refused as uncommitted work while 39f7ba5's host half was in line."""
        self.write(self.repo, "dev/old.json", "{\"next\": 1}\n")
        self.git(self.repo, "add", "dev/old.json")
        self.git(self.repo, "commit", "-qm", "landed, host half in flight")
        self.git(self.repo, "checkout", "HEAD^", "--", "dev/old.json")  # the live file waits for its host commit
        self.git(self.repo, "reset", "-q")
        self.patch("flight", lambda w: self.git(w, "rm", "-q", "dev/old.json"))
        self.assertIn("still in flight", self.land("").stderr)

    def test_a_live_edit_during_the_suite_is_refused_and_kept(self):
        self.patch("meddle", lambda w: self.write(w, "dev/test_meddle.py", MEDDLE))
        queue = os.path.join(self.repo, "dev/queue.json")
        r = self.land(self.patterns, PORT_MEDDLE=queue)
        self.assertIn("changed in the live core during the suite", r.stderr)
        with open(queue) as f:
            self.assertEqual(f.read(), self.before.replace("\n", "\n\n", 1))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "dev/test_meddle.py")))

    def host_ok(self):
        """A host whose land commit passes only on the new core (dev/fresh.py) while the live core still lacks it."""
        self.write(self.host, "tools/studio.py", f"""import os, subprocess, sys
if sys.argv[2] == 'commit':
    from dev.fresh import NEW  # only the new core has it, through PYTHONPATH: the new host needs it from its commit on
    if sys.argv[6:8] != ['--base', 'base']:
        sys.exit('land commit refused: no --base naming the host patch base')
    if os.path.exists({os.path.join(self.repo, "dev/fresh.py")!r}):
        sys.exit('land commit refused: the core went live before the host commit')
""" + LAND_COMMIT)

    def landed(self):
        self.assertTrue(os.path.exists(os.path.join(self.repo, "dev/fresh.py")))
        self.assertIn("Core-change: " + self.git(self.repo, "rev-parse", "HEAD"), self.git(self.d, "--git-dir=host.git", "log", "-1", "--format=%B", "main"))
        self.assertEqual(self.git(self.d, "--git-dir=core.git", "rev-parse", "main"), self.git(self.repo, "rev-parse", "HEAD"))
        pushed = self.git(self.d, "--git-dir=host.git", "rev-parse", "main")
        self.assertEqual(pushed, self.git(self.host, "rev-parse", "origin/main"))
        self.assertEqual(pushed, self.git(self.host, "rev-parse", "HEAD"))

    def test_a_queue_only_core_is_linked_as_core_queue(self):
        """defect: item 5 seam 5's host half (Working, 2026-10-23) was refused: its core commit c4794cc changed only
        dev/queue.json, and the host refuses a Core-change sha that changes no core code."""
        self.write(self.host, "tools/studio.py", "import os, subprocess, sys\nif sys.argv[2] == 'commit':\n" + LAND_COMMIT)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        shutil.copy(os.path.join(root, "pirillo_studio/host.py"), self.write(self.repo, "pirillo_studio/host.py", ""))
        r = self.land(self.patterns)
        self.assertEqual(r.returncode, 0, r.stderr)
        body = self.git(self.d, "--git-dir=host.git", "log", "-1", "--format=%B", "main")
        self.assertIn("Core-queue: " + self.git(self.repo, "rev-parse", "HEAD"), body)
        self.assertNotIn("Core-change:", body)

    def test_a_refused_host_leaves_the_core_live_files_and_never_pushes(self):
        self.patch("fresh", lambda w: self.write(w, "dev/fresh.py", "NEW = 1\n"))
        self.git(self.repo, "remote", "set-url", "origin", os.path.join(self.d, "missing.git"))
        r = self.land(self.patterns)
        self.assertIn("only its push failed", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "dev/fresh.py")))  # committed, not live
        finish = r.stderr.split("holds its commit):\n")[1]
        self.git(self.repo, "remote", "set-url", "origin", os.path.join(self.d, "core.git"))
        for _ in range(2):
            self.assertNotEqual(subprocess.run(["bash", "-c", finish], capture_output=True).returncode, 0)
        self.assertEqual(self.git(self.d, "--git-dir=core.git", "rev-parse", "main"), self.git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(self.git(self.d, "--git-dir=host.git", "log", "--format=%s", "main"), "base")
        self.assertEqual(self.git(self.host, "log", "--format=%s"), "base")
        self.assertFalse(os.path.exists(os.path.join(self.host, "x")))  # port.py never writes the host half itself
        self.assertFalse(os.path.exists(os.path.join(self.repo, "dev/fresh.py")))  # a refused finish writes no live file
        self.host_ok()
        self.write(self.host, "x", "another session's edit\n")   # the live host's follow is refused, so the core waits
        self.assertNotEqual(subprocess.run(["bash", "-c", finish], capture_output=True).returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "dev/fresh.py")))
        os.remove(os.path.join(self.host, "x"))
        self.git(self.host, "merge", "-q", "--ff-only", "origin/main")
        for _ in range(2):  # the finish, run twice once the host passes, lands the host and then makes the core live
            f = subprocess.run(["bash", "-c", finish], capture_output=True, text=True)
            self.assertEqual(f.returncode, 0, f.stderr)
        self.landed()

    def test_the_host_patch_is_checked_at_its_base_and_the_core_waits_for_the_live_host(self):
        """defect: reviews of 59f5913 and deda3c5 (2026-10-23): the host patch was checked on the live host's files, which
        lag origin/main while a render runs and carry other sessions' edits, and the core went live while land.py's follow
        left the live host on the old commit."""
        self.patch("fresh", lambda w: self.write(w, "dev/fresh.py", "NEW = 1\n"))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        shutil.copy(os.path.join(root, "pirillo_studio/host.py"), self.write(self.repo, "pirillo_studio/host.py", ""))
        self.host_ok()
        self.write(self.host, "x", "another session's edit\n")
        r = self.land(self.patterns)
        self.assertIn("the live host has not followed", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "dev/fresh.py")))
        with open(os.path.join(self.host, "x")) as f:
            self.assertEqual(f.read(), "another session's edit\n")
        os.remove(os.path.join(self.host, "x"))
        self.git(self.host, "merge", "-q", "--ff-only", "origin/main")   # the next landing's follow
        subprocess.run(["bash", "-c", r.stderr.split("once the live host holds it: ")[1]], check=True)
        self.landed()

    def test_a_host_patch_that_does_not_apply_at_its_base_lands_nothing(self):
        self.patch("fresh", lambda w: self.write(w, "dev/fresh.py", "NEW = 1\n"))
        self.host_ok()
        head = self.git(self.repo, "rev-parse", "HEAD")
        self.write(self.d, "host.patch", "diff --git a/y b/y\n--- a/y\n+++ b/y\n@@ -1 +1 @@\n-a\n+b\n")
        r = self.land(self.patterns)
        self.assertIn("Nothing is live", r.stderr)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.d, "--git-dir=core.git", "rev-parse", "main"), head)

    def test_the_host_half_tests_the_new_core_before_it_goes_live(self):
        self.patch("fresh", lambda w: self.write(w, "dev/fresh.py", "NEW = 1\n"))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        shutil.copy(os.path.join(root, "pirillo_studio/host.py"), self.write(self.repo, "pirillo_studio/host.py", ""))
        self.host_ok()
        r = self.land(self.patterns)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.landed()
