# whale-flow emoji card-feed WIP (salvaged, corrupted)

`whale-flow-wip-corrupted.html` is a salvaged 706-line work-in-progress of `static/whale-flow.html`, rescued 2026-06-16 before the file was overwritten with the clean live+mobile-fixes build.

## What it is
An **unfinished, better mobile design** for whale-flow: instead of shrinking the desktop chart+tape, it adds a purpose-built `#feed` mobile **card layout** (`@media max-width:768px`) with:
- emoji category icons per market via `catOf()` (⚾🏀🏈🥊🏛️⚽₿🌡️ …)
- severity-badged cards, YES/NO flow bars, score, time, "Open ↗"
- a richer `#tip` with pinned/close + action buttons

## Why it's corrupted
A botched paste **duplicated the entire JS tail** (two copies of `catOf`/`renderFeed`/utils/boot, two `</html>`) and **split the `minUsd` handler mid-statement** (`el('minUsdLbl').textContent='` … continuation stranded after a `</html>`). As-is the JS does not parse and the page would not render — it was **never deployed** (live never had `catOf`).

## To finish it (if desired)
1. De-corrupt: keep ONE copy of the tail (the first/Block-A copy has the newer `catOf` + `applyMobileLayout`); delete the duplicated Block-B copy; reassemble the `minUsd` handler to `el('minUsdLbl').textContent='$'+fmtUsd(state.minUsd);`.
2. Reconcile with the **current** live `static/whale-flow.html`, which now has the round-1 + A/B/C mobile work (segmented Tape/Chart toggle, bottom-sheet detail, rAF gating, etc.). Decide: port the card-feed design onto the current file, or graft the current JS improvements onto this one.
3. `node --check` the inline JS, test at 360/390/430 + landscape, then deploy with the **fixed** `polyclawd-deploy static/whale-flow.html` (now publishes to the served root automatically).

This card-feed approach is arguably a stronger mobile UX than the current shrink-to-fit chart+tape; the current shipped version chose the lower-risk "heal live" path instead.
