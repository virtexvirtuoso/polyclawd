# Whale Flow — Mobile UX Batch D: make the orb heatmap tappable on phones

**Status:** ✅ IMPLEMENTED & DEPLOYED 2026-06-16 (`deploy-snapshot-20260617-025638`) · **File:** `static/whale-flow.html`

> **Implementation note:** shipped the simpler **per-frame** relaxation (mobile-gated, runs in `draw()` on the visible orbs each frame) rather than the cached variant below — n is tiny (≤ dozens) so per-frame cost is negligible and it avoids the stale-cache-after-`rebuildOrbs` bug. Added: anti-coincidence seeding (`o.phase` offset + deterministic degenerate-direction fallback), **balanced 2D** push (not the X-biased 0.8/0.2 — that crammed orbs into a column that couldn't hold them), 24 passes, per-pass clamp. Verified at 390px: 14-orb burst → min pitch ~43px (was 4px), desktop (≥980px) byte-identical (relaxation skipped). The cached design below is kept as the reference rationale.

---
**Original spec (design-of-record):** · **Owner:** TBD
**Context:** Tier-2 mobile audit, 2026-06-16. Batches A+B+C shipped (segmented Tape/Chart toggle, bottom-sheet detail card, rAF battery gating, contrast/sevtag/landscape, stale banner). This is the remaining chart-redesign item.

---

## Problem

On a phone the bubble "tank" (`<canvas id="cv">`) is the page's signature view but is **functionally un-tappable**.

**Measured evidence (390×844, live):**
- 12 orbs rendered into a **2% horizontal slice** of the canvas (all at x≈343–351 of 372px wide).
- **Minimum center-to-center distance 4px**; **39 overlapping pairs**.
- Touch hit-radius is `max(r+14, 24)` ≈ 24–28px (after batch C). One thumb tap covers ~6 orbs.

**Why orbs collapse into a column:** the X axis encodes *time* (`xOf(ts)`), and almost all alerts arrive within a narrow recent window, so they pile onto nearly the same X. The Y axis is `log10(flow$)`, which also clusters. Result: a near-vertical stack.

Batch C mitigated the *consequence* (a tap on a cluster now opens a disambiguation picker — "N alerts here, pick one" — instead of opening a random market). **D fixes the root cause** so the heatmap is directly usable, not just survivable.

---

## Goal

On viewports ≤980px, lay orbs out so any orb can be individually tapped: **minimum 44px center-to-center pitch** between rendered orb centers, while preserving the read of the visualization (severity = colour, magnitude = size/Y, recency = X order). Desktop layout unchanged.

Non-goal: changing what the chart *means* on desktop. D is mobile-only layout.

---

## Approach options

### Option A — Collision-relaxation pass (recommended)
Keep the existing axes and semantics; after computing each orb's `(sx, sy)` in `draw()`, run a lightweight force-relaxation that pushes overlapping orbs apart to a minimum pitch, biasing displacement along X (time axis is the less semantically precise one here, since everything is "recent").

- **Pros:** keeps the brand visual (bioluminescent orbs, rings, glow); smallest conceptual change; reversible behind a width check.
- **Cons:** relaxation must run each frame or be cached; needs damping to avoid jitter; precise time-position is sacrificed on mobile (acceptable — already unreadable).

### Option B — Binned magnitude strip
On mobile, replace the pixel-tank with a simpler tappable layout: bucket alerts (e.g. by severity row × magnitude, or by 1h time bucket) into a grid/strip of fixed-size, severity-coloured, individually-tappable cells.

- **Pros:** guaranteed tap pitch; trivially accessible; no per-frame physics.
- **Cons:** loses the signature aesthetic; effectively a second visualization to maintain; larger build.

**Recommendation:** **Option A.** Less work, keeps the identity, and the disambiguation picker from batch C remains as a safety net for any residual overlap.

---

## Detailed design — Option A

### Where
`draw(now)` in the inline script, in the orb loop where `o.sx`/`o.sy` are assigned (currently:
```js
const x = xOf(o.a.ts) + Math.sin(now/1400*o.wob+o.phase)*3;
const y = yOf(o.usd)  + Math.cos(now/1700*o.wob+o.phase)*2.5;
o.sx=x; o.sy=y;
```
)

### Algorithm
1. Gate on mobile: `const SPREAD = window.innerWidth <= 980;` (recompute on resize; cheap).
2. Compute base positions for all visible orbs first (current `xOf/yOf` + wobble) into `o.sx/o.sy`.
3. If `SPREAD`, run **k passes** (start k=6) of pairwise relaxation over the visible set:
   - For each overlapping pair closer than `MIN_PITCH` (44px, or `(rA+rB+GAP)` if larger):
     - push apart along the connecting vector, but **weight the X component higher** (e.g. 0.8 X / 0.2 Y) so the time axis absorbs most of the spread and the magnitude (Y) read is mostly preserved.
   - Clamp to canvas padding (`PAD.l..W-PAD.r`, `PAD.t..H-PAD.b`).
4. Cache the relaxed offset per orb (`o.spreadDx/o.spreadDy`) and **re-relax only when the visible set or canvas size changes**, not every frame — apply cached offset + the small live wobble each frame. This keeps it cheap and removes per-frame jitter. Invalidate cache in `rebuildOrbs()` and `resize()`.
5. Hit-testing already uses `o.sx/o.sy` (post-spread), so taps line up with rendered positions automatically — no change needed in the `touchend` handler.

### Visual aids (optional, recommended)
- When spread displaces an orb materially from its true time-X, draw a faint 1px "leader" from the orb to its true X baseline, so the spread doesn't lie about recency. Skip under `prefers-reduced-motion` / if it adds clutter.
- Keep the batch-C disambiguation picker for any pair the relaxation can't fully separate (dense bursts).

### Pseudocode
```js
const MIN_PITCH = 44, GAP = 6;
function relaxOrbs(orbs){              // orbs: visible, with base sx/sy
  for(let pass=0; pass<6; pass++){
    for(let i=0;i<orbs.length;i++){
      for(let j=i+1;j<orbs.length;j++){
        const a=orbs[i], b=orbs[j];
        let dx=b.sx-a.sx, dy=b.sy-a.sy;
        let dist=Math.hypot(dx,dy)||0.01;
        const need=Math.max(MIN_PITCH, a.r+b.r+GAP);
        if(dist<need){
          const push=(need-dist)/2;
          const ux=dx/dist, uy=dy/dist;
          // bias along X (time axis is least precise here)
          a.sx-=ux*push*0.8; b.sx+=ux*push*0.8;
          a.sy-=uy*push*0.2; b.sy+=uy*push*0.2;
        }
      }
    }
  }
  for(const o of orbs){                 // clamp into plot area
    o.sx=Math.min(W-PAD.r, Math.max(PAD.l, o.sx));
    o.sy=Math.min(H-PAD.b, Math.max(PAD.t, o.sy));
  }
}
```
Complexity is O(n²·passes); n is the *visible* orb count (filtered ≤ a few dozen). At n=40, 6 passes ≈ 4,800 pair checks — negligible, and only when the set changes.

---

## Edge cases
- **n very large** (busy day): cap relaxed set or fall back to the binned strip (Option B) above a threshold; `log()`-style note if capped.
- **Reduced motion:** D is layout, not animation — runs once per data change; fine. Don't add the live wobble re-jitter in reduced mode.
- **Orientation:** in landscape the chart is hidden (batch A), so D only matters in portrait. No extra work.
- **Desktop:** `SPREAD` false → identical to today. Verify desktop pixel positions unchanged.

## Verification (mirror the A+B+C method)
- Local serve + Playwright at 360/390/430. Inject ≥12 mock alerts clustered in time.
- Assert: post-layout, **min pairwise center distance ≥ 44px** for visible orbs; `pageOverflowPx==0`; every orb individually tappable (tap each `o.sx/o.sy`, confirm the sheet opens the *expected* alert).
- Confirm desktop (≥1024px) orb coordinates byte-for-byte match pre-change (regression guard).
- Confirm rAF still parks in Tape mode and resumes in Chart mode (batch B interaction).

## Effort
~Medium. One self-contained function + a cache-invalidation hook in `rebuildOrbs()`/`resize()` + a width gate. No HTML/CSS changes required. Highest risk is per-frame cost (mitigated by caching) and jitter (mitigated by relax-on-change-only).

## Deploy note (IMPORTANT)
nginx serves `/polyclawd/whale-flow.html` from the **polyclawd root**, but `polyclawd-deploy` copies to `.../polyclawd/static/`. After `polyclawd-deploy static/whale-flow.html`, you MUST also `cp` static→root on the VPS (see how A/B/C shipped) or the change won't go live. Consider fixing the deploy path mapping as a separate task.
