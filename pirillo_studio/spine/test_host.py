"""The fake host the engine's and the domains' spine tests drive (no tests here)."""
from pathlib import Path

from pirillo_studio.spine import engine as spine


class Host:
    """The host's adapters as fakes that keep every call. The posted check is the posting domain's (docs/spine.md
    section 2): here it answers what a test sets, posted by default."""

    def __init__(self, verdicts=None):
        self.started, self.asked, self.told = [], [], []
        self.verdicts = verdicts or {}
        self.seen = {}
        self.fail_tell = self.crash_after = 0

    def start(self, actor, run, folder, stage, key):
        self.started.append((stage, key))
        return f"due 2026-10-23T09:00Z:{key}" if stage == "posting" else f"started:{key}"

    def ask(self, task):
        self.asked.append(task)
        return f"asked:{task['id']}"

    def tell(self, actor, key, handoff):
        if self.fail_tell:
            self.fail_tell -= 1
            raise OSError("crash before the inbox wrote")
        if (actor, key) not in self.told:  # the inbox reconciles by key
            self.told.append((actor, key))
        if self.crash_after:
            self.crash_after -= 1
            raise OSError("crash after the inbox wrote")
        return f"told:{key}"

    def check(self, c):
        def fn(evidence):
            self.seen.setdefault(c, []).append(evidence)
            v = self.verdicts.get(c)
            return v(evidence) if callable(v) else v or {"state": "posted" if c == "posted" else "pass", "words": []}
        return fn

    def video(self, folder, version="1", **kw):
        catalog = kw.pop("catalog", spine.data("catalog.json"))

        def ready(stage, *a):  # writes its record, as the core preflight does
            f = Path(folder, "records/ready", f"{stage['id']}.json")
            f.parent.mkdir(exist_ok=True)
            f.write_text('{"state": "ready"}')
            return {"state": "ready", "key": "sha256:ready"}
        return spine.Video(folder, start=self.start, ask=self.ask, tell=self.tell, href=lambda p: "/served/" + p,
                           checks={c: (version, self.check(c)) for c in catalog["checks"]}, catalog=catalog, **{"preflight": ready, **kw})
