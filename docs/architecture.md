# Architecture

Pirillo Studio is the public core of a video studio: the code that plans, posts, checks and reports, with no owner's
values in it. An owner runs it from their own working studio (the host), which holds their settings, data, accounts
and mods. This page is the shape the core is rebuilt into and the cutover it works toward; `dev/queue.json` is the order.

**What it is for** (the owner, 2026-10-22): a recipe is the kind of thing people share as a skill that makes a video.
The studio makes recipes easy to run and manage, and wraps the rest of the value around them: editing, checks,
scheduling, posting and measuring. "The less we have to rely on external costs, the better": a stage wraps a free or
already-paid tool, and a new paid service comes in only when the owner asks.

## Layers

Three layers, and every package and queue item is in one:

- **Product** (`pirillo_studio/`): what runs a studio, the domains below. It never imports `dev/` (`dev/check.py
  product_imports`), and reaches the host only through `host.py` (`dev/check.py needs`).
- **Dev kit** (`dev/`): how anyone safely changes a studio: the checks, the landing gate (`dev/land.py`), the port
  tool, the queue and the loop charts. It is generic and ships beside the product as an optional kit; it may import
  the product, never the other way.
- **Team machine**: an owner's own build crew (who builds and reviews, how work is routed, their gates, ledgers and
  scores). It stays in the owner's working studio and never enters the core. A piece of it moves into the dev kit only
  once it is generic, as its own queue item with the owner's values removed.

Every queue item names its layer, `product` or `dev-kit`; `dev/check.py queue` refuses any other, so team work is
queued in the owner's studio. The Board is where the layers meet. Its product half is what the owner acts on: asks,
posts and the owner's turn; item 13 ports that, and panels in its backlog that show the team machine (agents, models,
lanes, runs) stay in the host. The loop charts, each queue item on its state, are the dev kit's: item 3 makes
`dev/` render them and the host puts them on its Board, so the product Board never imports them.

## Domains

One package per domain under `pirillo_studio/`, each with its tests beside it: `posting/` (the clock, its sends,
adapters, checks and calendar), `board/`, `research/`, `rendering/`, `audio/`, `growth/`. A domain module imports
other core modules and the standard library, never the host. Today's flat modules are posting except `settings.py`;
the 19 move into `posting/` in one rename landing once the clock is in (one `git mv`, one import edit per host
line).

## The boundary: one host context

Every ported function takes the host as its first argument, `s`, and reads `s.<name>`. Today `s` is the whole host
module, so the core may read any of its 31,000 lines and nobody can say what the core depends on.

The replacement keeps the call shape and narrows what crosses it:

- `pirillo_studio/host.py` declares `NEEDS`: every host name the core may read, grouped by what it is: the ledger
  (`load`, `save`, `Lock`, `log`, `assets`, `move`), paths (`DATA_DIR`, `BOARD`, `content_dir`), the clock (`now`,
  `parse`, `tz`), the adapter registry (`ADAPTERS`), and **callbacks**, host code the core still calls because its
  domain is not ported yet. A callback leaves `NEEDS` when its domain lands here; the list's length is the
  migration's remaining coupling, readable in one file.
- `host.View(module)` is the context: attribute reads go to the live host (so a test's patch is seen) and a name not
  in `NEEDS` raises `AttributeError` naming it. Building one checks that the host has every `NEEDS` name and raises
  naming the missing ones, so a rarely run send path never meets a missing name on a live post. The host builds it
  in one place, `core()`, and passes it wherever it passes `sys.modules[__name__]` today. `dev/check.py needs` keeps
  the list exact both ways: every `s.<name>` read under `pirillo_studio/` is in `NEEDS`, and every entry is read.
- The core never hands `s` to host code, and imports no host module: host code that needs the host is a one-line
  host callback that supplies itself (`approval_opened(a, path, cfg)`), and the one host module still imported is
  declared in `HOST_IMPORTS` and counted as debt. `dev/check.py needs` refuses both.
- The owner's values reach the core only as data at run time: the merged settings `cfg` (`settings.merge(DEFAULTS,
  studio.json, rules.json)`) passed to every entry point, and whatever the ledger and the named paths hold. No owner
  value appears in core source; `dev/check.py private` refuses one.
- Nothing changes all at once: a View answers every name a module already reads, so today's `s`-modules keep working
  and a host call site switches from `sys.modules[__name__]` to `core()` when it is next touched. The clock passes
  `s` down to the rest of posting, so porting it puts all of posting behind the View in one step.
- The end state, and the release bar: `NEEDS` minus callbacks is the public host interface (ledger, paths, clock,
  adapters, plus `cfg`), small and documented. Release needs zero callbacks and a minimal starter host in the core's
  examples that runs the core through that interface alone. The callback count is the port's progress number.
- Forwarders are debt with an exit: each is deleted when its last host caller moves to the core, and new host code
  calls the core through `core()`, never a forwarder.

## Cutover: the core runs a video without the host's legacy code

The owner, 2026-10-22: "We can't abandon legacy but a goal should be to get off it sooner rather than later ... the
baseline that's required to be able to cut over and eliminate legacy and take our learnings and failings and correct
them in each of the critical domains." Moving legacy code unchanged moved its problems with it: by 2026-10-22 the
core held about 4,200 lines against roughly 400,000 in the host, and the Board still drew each kind of task its own
way. So the core is now **rebuilt domain by domain to a spec**, and the queue is only what the cutover needs.

**Cutover** is two gates, in order:

1. **C1, the owner's studio runs on the core.** One `plain` video and one `shorts-only` video go from footage to
   posted with no host legacy module on the path, and the owner works it from the new Board alone.
2. **C2, testers (item 31, R3).** A fresh second studio installs from the artifact and makes one finished video.

The baseline both gates need, and nothing else before them:

- **The spine** (item 37): a recipe is data (stages, each with its inputs, its run, its check, its outputs and who
  acts next); one engine says what is next, runs it, records it and hands off. This replaces the host's next,
  dispatch, holds and per-recipe exceptions such as `recipe_absent`, which keep breaking because every check must
  remember every recipe.
  The recipe data, engine answers and acceptance contracts are in [the spine spec](spine.md).
- **The Board as one task model** (item 13): every task is one shape (what, why, where it came from, the thing to
  judge, one set of actions each undoable, "can't, here's why", and its loop from filed to done); a kind that needs
  its own layout is a spec change, never a new renderer.
  The task shape, source mapping and acceptance contracts are in [the Board spec](board.md).
- **The video domains the two recipes use**, in path order: media in (item 6: probe, transcript, faces), cutting
  (17), rendering with shorts (14), captions (18), QC as one check engine whose checks each recipe declares (15),
  the release (39: titles, copy and the release file the posting clock reads), and posting, which is already here (its TikTok route still needs
  the host's browser fit and is rebuilt without it).
  The media and disk preflight's probes, API and acceptance contracts are in [the preflight spec](preflight.md).
  The cut plan, its checks and acceptance contracts are in [the cutting spec](cutting.md).
- **Settings and voice** (items 5 and 12) only as far as these need them.

Everything else waits until C1: research, images, digest, growth, community, morning run, tips, vault, crew rules,
the other recipes (animated explainer, claymation, review roundup) and the host's team machinery.

## How a domain is rebuilt

1. **Spec first**, in this file or beside the domain: its flowchart (fewest boxes reaching every end), its data in
   and out, its expectation list, and the host's failings it ends, each with the issue that showed it. The owner
   reads specs that change what he sees.
2. **Build it new** to the spec. Legacy code is a reference, not a source: copy a function only where it is already
   right, and say so in the review.
3. **Contract tests**: the spec's inputs to its outputs, one test per end of the flowchart, plus one run on real
   media named as a `Live check:`. A legacy test is not carried over; what it guarded is either a spec line or gone.
4. **Switch and delete**: the host calls the core behind one setting, the owner sees both side by side where it
   changes what he sees, then the legacy code, its tests and its settings are deleted in one landing.

**Legacy freeze** (until C1): the host gets fixes that keep the owner's videos posting and the Board answering, and
nothing new. A bug in a domain on the cutover list is fixed in its rebuild. An issue about the host's own machinery is frozen,
not closed; each is read once at cutover and either becomes a spec line or goes with the legacy.

`dev/port.py move` stays for the few places where the legacy code is right as written; its rules (exact source,
`s.<name>` reads in `NEEDS`, host forwarders, one review per item) still hold when it is used.

### dev/port.py move

`dev/port.py move <host file> <names...> --to <core module>` moves the exact source of whole sections (functions,
constants, classes) and mechanically rewrites only what crossing the boundary needs: each host global becomes
`s.<name>` (added to `NEEDS`), each moved function gains `s` first, and the host keeps one-line forwarders so
callers are untouched. It refuses, and lists for a hand line, anything it cannot resolve statically: a name it
cannot find, a host module the core would import, a global the moved code writes, a name local in one scope and host
in another, `getattr` with a computed name, `globals()`, any `sys.modules` lookup but `sys.modules[__name__]`,
`exec` and `eval`. A move adds no behaviour: its proof is the existing tests of those names passing before and
after, and its review checks that the diff is the mover's output plus the few hand lines it names. `dev/port.py
land` lands the core half through `dev/land.py` (with the queue state flip) and then the host half through the
host's own landing, citing the core commit, and prints the callback count. One item, one review, one commit per
repo.

Later, as a domain settles, its `s.<callback>` reads become proper arguments or move with their domain; that is
ordinary refactoring inside the core, done when the code is next worked on, never a sweep.

### Board ask writes (item 13, seam 3)

`board/asks.py` owns option parsing, permission/state transitions and matching an answered question.
`board/writes.py` owns filing an ask, recording the action carried out and idempotently delivering committed image
rejection events to their append-only mirror. The ledger remains authoritative; its host lock serializes writes.
The acted validator has one Core home; words without an option never prove that an option ran.

Owner identity, decision adapters, team/question and image/title policy, path validation and image/digest effects
remain explicit host callbacks. Existing Core readers are called directly. The host keeps command dispatch,
locked save, stage echo, pending-run tracking and withdrawal effects. Free text is feedback, never permission.
The host forwarders call this pair live. Cards, turn projection, the content contract and backlog outcomes remain open.

```mermaid
flowchart TD
    A[Find open ask] --> B{Host handles this answer?}
    B -->|yes| C[Return host result]
    B -->|no| D[Match option and run image preflight]
    D --> E{Decision gate}
    E -->|refused| F[Raise refusal]
    E -->|owner required| G[Reopen and log]
    E -->|Hold| H[Return held answer]
    E -->|allowed or disabled| I{Host holds image feedback?}
    I -->|yes| H
    I -->|no| J[Run host effects]
    J --> K[Record answer unless decision owner already did]
    K --> L[Incident and retry effects, log, return]
```
