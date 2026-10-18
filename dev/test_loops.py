import json, os, re, shutil, tempfile, unittest

import loops


class Chart(unittest.TestCase):
    """e2e: dev/loops.py's chart of the real dev/loops/build.json names every state and its moves, and places each
    dev/queue.json item on its own state; an item on a state the loop lacks, or on a loop with no file, raises instead of vanishing."""

    def test_build_chart(self):
        html = loops.charts()
        with open(os.path.join(loops.ROOT, "dev", "loops", "build.json")) as f:
            loop = json.load(f)
        with open(os.path.join(loops.ROOT, "dev", "queue.json")) as f:
            items = json.load(f)
        cols = dict(re.findall(r'<li class="state[^"]*" data-state="([^"]+)">(.*?)</ul></li>', html))
        self.assertEqual(list(cols), loop["states"])
        self.assertIn(f'class="state done" data-state="{loop["done"]}"', html)
        for t in loop["transitions"]:
            self.assertIn(f'→ {t["to"]}', cols[t["from"]])
        for i in items:
            self.assertIn(f'<li>#{i["id"]} ', cols[i["state"]])
        with self.assertRaisesRegex(ValueError, "unknown state shipped"):
            loops.render(loop, [{"loop": "build", "state": "shipped", "id": 0, "title": "x"}])
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        shutil.copytree(os.path.join(loops.ROOT, "dev", "loops"), os.path.join(root, "dev", "loops"))
        with open(os.path.join(root, "dev", "queue.json"), "w") as f:
            json.dump([{"loop": "ship", "state": "queued", "id": 0, "title": "x"}], f)
        with self.assertRaisesRegex(ValueError, "unknown loop ship"):
            loops.charts(root)


if __name__ == "__main__":
    unittest.main()
