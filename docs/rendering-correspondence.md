# Master correspondence and native long composition (2h parts 13/14)

The plain recipe approves a draft before masters. Correspondence is the measurement
prerequisite for that delivery: an adapter-supplied long, full vertical or short
must preserve the approved voice and picture independently. Native long composition uses that same proof; native vertical and short composition
remain refused; shorts-only still measures its supplied media without encoding it.

```mermaid
flowchart LR
  A[measuring: bind approved draft bytes and readings] --> B[Validate voice and picture maps]
  B --> C[Map voice to unique draft frame intervals]
  C --> D[Compare picture at those same draft frames]
  D --> E[Compare decoded picture and complete mapped audio]
  E --> F[delivered: publish measured adapter output]
  A & B & C & D & E --> G[measure_refused: restore accepted artifacts]
```

Full outputs preserve every voice frame, including patch identity and silent
expressions. Picture identity follows actual sampled source indices, opaque cover
identity and timing, independently of voice boundaries. Different push crop-run
partitions are compared frame by frame. Long outputs also preserve crops; vertical
outputs retain their own independently measured native crops and subject coverage.

Shorts require one unambiguous approved voice occurrence per kept piece. Integer
output-frame offsets locate the draft intervals used for both picture and sound.
A J-cut cannot borrow a picture occurrence from another part of the draft, and a
repeated cold open cannot silently choose between two voice occurrences. Silent
retimed short correspondence refuses by name because its local zero-based voice
clock cannot identify an excerpt. Full silent expressions remain supported.
Existing partial-patch and partial incoming J-cut refusals remain in the clock owner.

Draft media, readings and request identities must agree. Long correspondence compares
the decoded output with the viewed draft; tall correspondence combines the accepted
draft frame proof with every output frame's independently measured source mapping.
The complete output sound is compared with the same approved draft intervals. Missing,
stale or failed evidence never substitutes for a pass. No correspondence helper writes
media or records; publication and interrupted-write recovery remain lifecycle-owned.

## Native long composition (part 14)

The plain recipe's first master output is the long file. Its native compositor now
runs the same voice, captions, bed, card and picture pipeline used by the approved
draft. The lifecycle permits encoding only after an explicit current draft approval;
complete decoded voice/picture correspondence is required before publication.
Long-specific encoding settings remain content-bound. Generated voice, mix, caption
record and sidecar use the long output's identity and existing recovery transaction.

```mermaid
flowchart LR
  A[Resolve native long request] --> B{Current draft explicitly approved?}
  B -->|no| C[waiting_owner]
  B -->|yes| D[rendering: voice, captions, mix, picture]
  D --> E[measuring: decoded output and draft correspondence]
  E --> F[delivered: publish long and bound artifacts]
  D --> G[render_failed: restore accepted artifacts]
  E --> H[measure_refused: restore accepted artifacts]
```

This enables only native long masters. Native vertical/short masters and whole-short
context remain the next delivery pieces; unsupported master outputs keep their named
refusal. Shorts-only continues to measure supplied media unchanged. This does not
claim the plain recipe's complete delivery or final-manifest integration.

The renderer identity advances to invalidate earlier cached requests. Contracts
independently decode every frame of a changing-gray source at 60/30 and
60000/1001-to-30000/1001 rates, calculate the J-cut's expected source indices and
compare complete prepared voice samples with independent source slices. They also
check negative approval choices, sidecar delivery, reuse, named refusals and recovery.

Contracts independently encode the J-cut fixture, decode every delivered frame and
check the expected source indices. A changing-gray source proves 60-to-30 sampling
and wide/tall pushes against independently decoded RGB, including different crop-run
partitions. They also exercise short offsets, repeated-occurrence refusal, altered
picture/audio, stale identities, symlinks, non-finite input and interrupted publication.
