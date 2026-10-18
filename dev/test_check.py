import json, os, shutil, tempfile, unittest
from unittest import mock

import check

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Refusals(unittest.TestCase):
    """guard: dev/check.py refuses an undeclared path, an oversized file, a test folder discovery skips,
    a queue state its loop lacks, a queue item outside the core's layers, a product import of the dev kit;
    and the owner's private data, in a file's name or content, without echoing the pattern."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PIRILLO_PRIVATE_PATTERNS", None)
        for n in ("repo.json", "dev/queue.json", "dev/loops/build.json"):
            os.makedirs(os.path.dirname(os.path.join(self.root, n)), exist_ok=True)
            shutil.copy(os.path.join(ROOT, n), os.path.join(self.root, n))

    def write(self, rel, text=""):
        os.makedirs(os.path.dirname(os.path.join(self.root, rel)), exist_ok=True)
        with open(os.path.join(self.root, rel), "w") as f:
            f.write(text)

    def test_clean_copy_passes(self):
        self.assertEqual(check.problems(self.root), [])

    def test_undeclared_path_even_if_git_would_ignore_it(self):
        self.write("__pycache__/x.pyc")
        self.assertEqual(check.problems(self.root), ["__pycache__: not in repo.json layout"])

    def test_oversized_file_counts_an_unterminated_last_line(self):
        max_lines = check.load(os.path.join(self.root, "repo.json"))["max_lines"]
        self.write("dev/big.py", "\n" * max_lines + "x")
        self.assertEqual(len(check.problems(self.root)), 1)

    def test_test_folder_without_init(self):
        self.write("pirillo_studio/domain/test_x.py")
        self.assertEqual(check.problems(self.root), ["pirillo_studio/domain: holds Python but no __init__.py, so its tests never run"])

    def test_test_folder_whose_parent_lacks_init(self):
        self.write("pirillo_studio/outer/inner/__init__.py")
        self.write("pirillo_studio/outer/inner/test_x.py")
        self.assertEqual(check.problems(self.root), ["pirillo_studio/outer: holds Python but no __init__.py, so its tests never run"])

    def test_loop_declared_twice(self):
        shutil.copy(os.path.join(self.root, "dev/loops/build.json"), os.path.join(self.root, "dev/loops/zz.json"))
        self.assertEqual(check.problems(self.root), ["loop build: declared twice (zz.json)"])

    def test_unknown_state_and_layer(self):
        self.write("dev/queue.json", json.dumps([{"id": 1, "loop": "build", "state": "merged", "layer": "product"},
                                                 {"id": 2, "loop": "build", "state": "queued", "layer": "team"},
                                                 {"id": 3, "loop": "build", "state": "queued"}]))
        self.assertEqual([m.split(":")[0] for m in check.problems(self.root)], ["queue 1", "queue 2", "queue 3"])

    def test_product_never_imports_dev(self):
        self.write("pirillo_studio/__init__.py")
        self.write("pirillo_studio/a.py", "import dev.check\nfrom dev import land\nimport runpy, importlib\n"
                   "__import__('dev.port')\nimportlib.import_module('dev')\nrunpy.run_path(os.path.join(R, 'dev', 'x.py'))\n"
                   "import devices\nfrom importlib import import_module as im\nim('dev.land')\n")
        self.assertEqual([m.split(":")[0] for m in check.problems(self.root)],
                         [f"pirillo_studio/a.py line {n}" for n in (1, 2, 4, 5, 6, 9)])

    def test_assert_outside_tests_refused(self):
        self.write("dev/x.py", "assert False\n")
        self.assertEqual(check.problems(self.root), ["dev/x.py line 1: assert vanishes under python -O; raise instead"])

    def test_duplicate_key_refused(self):
        self.write("dev/queue.json", '[{"id": 11, "loop": "build", "state": "building", "state": "landed"}]')
        self.assertRaisesRegex(ValueError, "duplicate key", check.problems, self.root)

    def patterns(self, text):
        fd, path = tempfile.mkstemp(suffix=".patterns")   # outside the checked root
        self.addCleanup(os.remove, path)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.environ["PIRILLO_PRIVATE_PATTERNS"] = path

    def test_private_path_and_content_refused_and_no_problem_shows_them(self):
        self.patterns("# comment\nowner-secret\n^dev/name\\.txt$\n")
        self.assertEqual(check.problems(self.root), [])
        max_lines = check.load(os.path.join(self.root, "repo.json"))["max_lines"]
        self.write("dev/notes.txt", "owner-secret")
        self.write("dev/name.txt", "\n" * (max_lines + 1))
        self.write("dev/owner-secret.txt")
        with open(os.path.join(self.root, "dev/queue.json")) as f:
            items = json.load(f) + [{"id": "owner-secret", "loop": "build", "state": "invalid"}]
        self.write("dev/queue.json", json.dumps(items))
        found = "\n".join(check.problems(self.root))
        for line in ("dev/notes.txt: content matches private pattern on line 2",
                     "<private>: path matches private pattern on line 3", "<private>: path matches private pattern on line 2",
                     f"<private>: {max_lines + 1} lines", "queue <private>: state invalid"):
            self.assertIn(line, found)
        self.assertNotIn("owner-secret", found)
        self.assertNotIn("name.txt", found)

    def test_empty_or_invalid_patterns_refuse_without_echoing(self):
        self.patterns("# only a comment\n")
        self.assertRaises(SystemExit, check.problems, self.root)
        self.patterns("(?P<owner-secret>x)\n")
        with self.assertRaises(SystemExit) as e:
            check.problems(self.root)
        self.assertNotIn("owner-secret", str(e.exception))


class HostBoundary(unittest.TestCase):
    """guard: the core reads only declared host names, never hands its view to host code, and imports no host module."""

    def test_undeclared_read_unused_name_handed_view_and_host_import(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        os.makedirs(os.path.join(root, "pirillo_studio"))
        for rel, text in (("pirillo_studio/host.py", 'NEEDS = {"ledger": ("load", "save"), "callbacks": ("approvals",)}\n'
                                                      'HOST_IMPORTS = ()\n'),
                          ("pirillo_studio/a.py", "import no_such_host_module\n\ndef f(s):\n"
                                                  "    s.approvals.opened(s)\n    return s.load(), s.extra, __import__('x')\n")):
            with open(os.path.join(root, rel), "w") as f:
                f.write(text)
        found = check.needs(root, ["pirillo_studio/host.py", "pirillo_studio/a.py"])
        self.assertEqual([m.split(":")[0] for m in found], ["s.extra", "save", "pirillo_studio/a.py line 1",
                                                            "pirillo_studio/a.py line 4", "pirillo_studio/a.py line 5"])
