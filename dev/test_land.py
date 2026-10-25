import json, os, shutil, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ("repo.json", "dev/check.py", "dev/land.py", "dev/queue.json", "dev/loops/build.json")


class Landing(unittest.TestCase):
    """guard: dev/land.py never lands an unreviewed tree, a tree whose tests fail, or a commit other than the reviewed tree."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo)
        for n in FILES:
            os.makedirs(os.path.dirname(os.path.join(self.repo, n)), exist_ok=True)
            shutil.copy(os.path.join(ROOT, n), os.path.join(self.repo, n))
        shutil.copytree(os.path.join(ROOT, "dev", "pirillo_pipes"), os.path.join(self.repo, "dev", "pirillo_pipes"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside)
        self.patterns = os.path.join(outside, "patterns.txt")
        with open(self.patterns, "w") as f:
            f.write("secret-home/\n")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True, text=True).stdout.strip()

    def land(self, approve, patterns=True, message="m"):
        self.git("add", "-A")
        review = os.path.join(self.repo, "..", os.path.basename(self.repo) + ".review")
        self.addCleanup(lambda: os.path.exists(review) and os.remove(review))
        with open(review, "w") as f:
            f.write(f"APPROVE {approve or self.git('write-tree')} tester\n")
        env = {k: v for k, v in os.environ.items() if k != "PIRILLO_PRIVATE_PATTERNS"}
        if patterns:
            env["PIRILLO_PRIVATE_PATTERNS"] = self.patterns
        return subprocess.run([sys.executable, "dev/land.py", "--review", review, message], cwd=self.repo,
                              capture_output=True, text=True, env=env)

    def test_unreviewed_tree_refused(self):
        r = self.land("0" * 40)
        self.assertIn("first line must be `APPROVE ", r.stderr)
        self.assertNotEqual(subprocess.run(["git", "-C", self.repo, "rev-parse", "-q", "--verify", "HEAD"]).returncode, 0)

    def test_no_patterns_file_refused(self):
        r = self.land(None, patterns=False)
        self.assertIn("PIRILLO_PRIVATE_PATTERNS", r.stderr)
        self.assertNotEqual(subprocess.run(["git", "-C", self.repo, "rev-parse", "-q", "--verify", "HEAD"]).returncode, 0)

    def test_patterns_file_inside_the_repo_refused(self):
        os.rename(self.patterns, os.path.join(self.repo, "dev", "patterns.txt"))
        self.patterns = os.path.join(self.repo, "dev", "patterns.txt")
        r = self.land(None)
        self.assertIn("outside this repository", r.stderr)
        self.assertNotEqual(subprocess.run(["git", "-C", self.repo, "rev-parse", "-q", "--verify", "HEAD"]).returncode, 0)

    def test_private_content_refused(self):
        with open(os.path.join(self.repo, "dev", "notes.json"), "w") as f:
            f.write('"secret-home/x"')
        r = self.land(None)
        self.assertIn("dev/check.py found problems", r.stderr)
        self.assertNotEqual(subprocess.run(["git", "-C", self.repo, "rev-parse", "-q", "--verify", "HEAD"]).returncode, 0)

    def test_private_message_refused(self):
        r = self.land(None, message="moved from secret-home/tools")
        self.assertIn("message matches private pattern on line 1", r.stderr)
        self.assertNotIn("secret-home", r.stderr)
        self.assertNotEqual(subprocess.run(["git", "-C", self.repo, "rev-parse", "-q", "--verify", "HEAD"]).returncode, 0)

    def test_commit_is_the_reviewed_tree(self):
        tree = (self.git("add", "-A"), self.git("write-tree"))[1]
        self.assertEqual(self.land(None).returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}"), tree)
        self.assertEqual(self.git("log", "-1", "--format=%an <%ae> %cn <%ce>"), " ".join([json.load(open(
            os.path.join(self.repo, "repo.json")))["identity"]] * 2))  # the product's name, never the machine's git user

    def test_attributes_and_filters_cannot_hide_a_failing_test(self):
        with open(os.path.join(self.repo, "dev/test_fail.py"), "w") as f:
            f.write("import unittest\nclass F(unittest.TestCase):\n    def test(self):\n        self.fail()\n")
        with open(os.path.join(self.repo, "dev/.gitattributes"), "w") as f:
            f.write("test_fail.py export-ignore filter=hide\n")
        self.git("config", "filter.hide.smudge", "sed s/self.fail..//")
        self.assertIn("dev tests failed", self.land(None).stderr)

    def test_a_slow_printing_suite_lands_and_a_silent_one_is_refused(self):  # defect: core item 39 seam 4b, refused 3 times at 1200 s
        with open(os.path.join(self.repo, "repo.json")) as f:
            repo = json.load(f)
        with open(os.path.join(self.repo, "repo.json"), "w") as f:
            json.dump(dict(repo, suite_seconds=5), f)
        with open(os.path.join(self.repo, "dev/test_slow.py"), "w") as f:  # 10 s in all, never 5 s silent
            f.write("import time, unittest\nclass S(unittest.TestCase):\n" +
                    "".join(f"    def test_{i}(self):\n        time.sleep(0.5)\n" for i in range(20)))
        r = self.land(None)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.repo, "dev/test_silent.py"), "w") as f:
            f.write("import time, unittest\nclass H(unittest.TestCase):\n    def test(self):\n        time.sleep(30)\n")
        self.assertIn("dev tests were silent past 5 s", self.land(None).stderr)

    def test_package_tests_import_as_a_makers_run_does(self):  # defect: core 6b289e9's first landing refused
        for n, text in (("__init__.py", ""), ("a/__init__.py", ""), ("b/__init__.py", "x = 1\n"),
                        ("a/test_rel.py", "import unittest\nfrom ..b import x\nclass R(unittest.TestCase):\n"
                                          "    def test(self):\n        self.assertEqual(x, 1)\n")):
            os.makedirs(os.path.dirname(os.path.join(self.repo, "pirillo_studio", n)), exist_ok=True)
            with open(os.path.join(self.repo, "pirillo_studio", n), "w") as f:
                f.write(text)
        r = self.land(None)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_checks_cannot_rewrite_the_reviewed_export(self):
        with open(os.path.join(self.repo, "dev/test_mutation.py"), "w") as f:
            f.write("from pathlib import Path\nimport unittest\nclass Mutation(unittest.TestCase):\n"
                    "    def test(self):\n        Path('repo.json').write_text('{}')\n")
        r = self.land(None)
        self.assertIn("Candidate export changed: repo.json", r.stderr)
        self.assertNotEqual(subprocess.run(["git", "-C", self.repo, "rev-parse", "-q", "--verify", "HEAD"],
                                          capture_output=True).returncode, 0)

    def test_commit_made_during_the_run_is_not_overwritten(self):
        with open(os.path.join(self.repo, "dev/test_race.py"), "w") as f:
            f.write("import subprocess, unittest\nclass R(unittest.TestCase):\n    def test(self):\n"
                    f"        subprocess.run(['git', '-C', {self.repo!r}, 'commit', '-q', '--allow-empty', '-m', 'meanwhile'], check=True)\n")
        r = self.land(None)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "meanwhile")
