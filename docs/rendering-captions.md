# Seam 2e: caption rasters and accepted artifact recovery

Built: one public item 18 artifact-name helper, clocks derived once from integer frames, sample-based
voice trimming at fractional frame rates, and recovery of accepted voice/caption/SRT bytes with the video.
`previous.artifacts` records generated paths and their pre-attempt hashes (or null when absent);
`artifact_work` records an interrupted artifact backup for recovery. Backups use the existing content-addressed
render lifecycle, are restored on failure/retry, and retired on success.
Caption burns supplied by a renderer adapter are measured against their bound raster, including its alpha
edges and the profile's safe rectangle. Native wide composition still makes sidecars; native tall burns wait.

A burning adapter supplies `caption_binding.raster = {file, sha256, burn_sha256}` beside the existing
caption `record` and `sha256`. `burn_sha256` names that caption record's exact ASS burn. The raster is a
full-output, straight-alpha video on the output frame grid, with one frame per output frame and no scaling.
It includes transparent frames outside cue intervals. Its colors and alpha are trusted resolved glyph pixels,
not a claim that rendering can independently shape text from ASS. All assets enter the request identity.
A burning profile must supply `caption_safe: [x, y, width, height]` in output pixels; no safe area is inferred.
Every nonzero-alpha pixel must be inside it. Visible raster frames must exactly match cue intervals on the
output grid (a frame is active when its timestamp is at or after cue start and before cue end).
Readings retain `captions.value.raster` with the raster hash, safe rectangle, and per-frame alpha bounds.
Every delivered RGB frame is compared to the bound source/cover plus that raster, proving burn presence,
placement and timing. ASS-only burns, missing/unbound/stale rasters, missing alpha, unsafe pixels and
mismatched cue edges refuse by the `captions` reading. Missing or unsupported coverage never passes.

```mermaid
flowchart LR
  A[Resolve caption record and raster identity] --> B[Decode alpha and cue edges]
  B --> C[Compare delivered pixels with source plus raster]
  C --> D[measuring: record caption bounds and pixel evidence]
  D --> E[waiting_owner: publish draft]
  A & B & C --> F[measure_refused: restore accepted artifacts]
```

Seam 2f owns pushes, retiming/source-rate and color conversion, grades, anchored non-cover cards/context
and their raster edges/safe bounds, processed voice/mix/bed bindings, split-clock master correspondence,
and native tall/master composition. Missing/differing source color matrices, ASS-only burns and missing
bed evidence still refuse. Text-only cover cards require a resolved raster asset. Partial patch excerpts/cold
opens, J-cuts into or after hidden picture, and excerpt J-cuts without the settled predecessor still refuse.
Default profiles retain every required check; this seam does not claim complete plain-recipe delivery.
