# Pirillo Studio

The clean rebuild of the owner's working studio, ported one domain at a time; each port deletes the old code in the same change. Every rule here is enforced by a check; prose that no check backs is the bug.

- Every path sits where `repo.json` `layout` says [dev/check.py:layout].
- No file over `repo.json` `max_lines` [dev/check.py:sizes].
- A test process that runs past `repo.json` `suite_seconds` of wall time is refused as hung; a passing suite is never refused for its cost [dev/land.py:suite].
- Three layers (`docs/architecture.md`): the product, `pirillo_studio/`, never imports the dev kit, `dev/` [dev/check.py:product_imports]; the owner's team machine stays in their working repository, so every queue item's layer is `product` or `dev-kit` [dev/check.py:queue].
- Work lives in `dev/queue.json`; each item's state is a state of its loop in `dev/loops/` [dev/check.py:queue]. No GitHub issues, labels or Actions.
- Only `dev/land.py` lands. It tests the exact staged tree and needs a review naming that tree [dev/land.py:main].
- This repository is the public-release core: generic code, defaults and docs only. The owner's values, names, paths, videos, accounts and work items stay in the owner's working repository; a port moves only the generic code here. Private patterns live outside this repo, in the file `PIRILLO_PRIVATE_PATTERNS` names, which `dev/land.py` requires [dev/check.py:private]; it refuses a commit message that matches one and commits as repo.json `identity`, so history stays the product's [dev/land.py:main].
- No fallbacks: a missing or broken input raises; a protection fails closed.
