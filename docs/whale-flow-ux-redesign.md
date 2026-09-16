# Whale // Flow — UX Redesign Proposal

> Date: 2026-06-16 | Status: PROPOSAL

## Problem Statement

The current bubble chart plots alerts on **time × flow $**. Because alerts fire in bursts (multiple markets scanned simultaneously every ~5 min), all bubbles cluster on the right side of the chart regardless of window size. The left 80%+ of the chart is dead space. On mobile, the tape panel is hidden entirely and touch interaction with canvas orbs is imprecise.

## Design Principles

1. **Score is king** — the most important dimension. A $5K flow at score 10 matters more than $50K at score 3.
2. **Glanceability** — within 2 seconds, answer: "are there any actionable signals right now?"
3. **Mobile-first** — 60%+ of Mr. V's usage is Telegram → tap link → mobile browser. Desktop is secondary.
4. **Preserve the aesthetic** — the bioluminescent deep-sea theme is distinctive. Don't flatten it into a generic dashboard.

---

## Recommendation: Hybrid Layout

### Mobile (< 768px): **Signal Feed**

A vertically scrollable card list, sorted by score descending. Each card is a self-contained signal.

```
┌─────────────────────────────────┐
│ 🔴 CRITICAL · 10/10        ⚽  │
│ Argentina (-1.5) · Spread       │
│ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░ 99% YES     │
│ $149K flow · 37¢/38¢ · PM      │
│ OI $769K · Closes 01:00 UTC    │
│                    [Open ↗]     │
└─────────────────────────────────┘

┌─────────────────────────────────┐
│ 🟠 HIGH · 8/10             🎾  │
│ Kenin vs Golubic · R32          │
│ ▓▓▓▓▓▓▓▓▓▓▓▓░░░░░ 85% YES     │
│ $51K flow · 62¢/64¢ · KX       │
│ OI $125K · 2.5h left           │
│                    [Open ↗]     │
└─────────────────────────────────┘
```

**Card anatomy:**
- Left border color = severity (red/amber/teal)
- Line 1: severity badge + score + category emoji (right-aligned)
- Line 2: short title (via `_short_title()`)
- Line 3: flow direction bar (visual, not text — colored fill bar showing YES/NO ratio)
- Line 4: flow $ + price + platform badge
- Line 5: OI + close time
- Bottom-right: "Open ↗" button → links to Kalshi/Polymarket

**Interaction:**
- Scroll to browse
- Tap card → expands to show full details (reasons, depth, wallet data)
- Tap "Open ↗" → navigates to market
- Pull-to-refresh → re-fetch alerts
- Filter chips at top (same as current: CRITICAL/HIGH/LOW, Kalshi/PM, min flow)

**Why card feed > chart on mobile:**
- Native scroll is the most natural mobile interaction
- No touch precision issues (no 12px bubble targets)
- Information density per pixel is 3-5x higher than bubbles
- Sorted by score = immediate priority ranking
- Works at any screen width

### Desktop (≥ 768px): **Score × Flow Scatter + Feed**

Replace the time axis with score. Keep the bioluminescent aesthetic.

```
┌──────────────────────────────────────┬──────────────┐
│          SCORE × FLOW SCATTER        │  LIVE TAPE   │
│                                      │              │
│  $100K ─ ·                    ● ●    │  [card 1]    │
│          ·                  ●        │  [card 2]    │
│   $10K ─ ·        ○ ○    ●          │  [card 3]    │
│          ·      ○    ○              │  [card 4]    │
│    $1K ─ ·  ○  ○                    │  ...         │
│          ├────┼────┼────┼────┤      │              │
│          3    5    7    8   10       │              │
│              SCORE →                 │              │
└──────────────────────────────────────┴──────────────┘
```

**X-axis: Score (3–10)** — natural spread, no clustering.
**Y-axis: Flow $ (log scale)** — same as current, keeps whales at top.
**Bubble size: unchanged** (log flow).
**Bubble color: severity** (same as current).

**Why this works:**
- Score distributes 3–10 = 7 buckets of spread vs. time which clusters
- The top-right quadrant (high score + high flow) is the "action zone" — immediately visible
- Low-score noise stays bottom-left, out of attention
- Desktop hover tooltip still works for details
- Click → pin card (already implemented)

**Bonus: add jitter** — small random vertical offset within each score band so overlapping orbs spread slightly.

### Shared: Filter Bar

Keep current filter chips but add:
- **Sort toggle** (mobile only): Score ↓ | Flow ↓ | Recent
- **Category filter**: ⚽ 🎾 🏀 ₿ 🏛️ (tap to toggle)

---

## Information Hierarchy

### At-a-glance (visible without interaction):
1. Score + Severity (is this worth looking at?)
2. Direction (YES/NO — which side is the money on?)
3. Flow size (how much conviction?)
4. Short title (what market?)

### On drill-down (tap/hover):
5. Price spread (bid/ask)
6. OI + volume
7. Close time
8. Trigger reasons
9. Depth (bid/ask walls)
10. Wallet data (if available)

---

## Migration Path

**Phase 1 (quick win, ~2h):**
- Add `@media (max-width: 768px)` block that hides the canvas and shows a card-feed div
- Card feed reads from the same `state.alerts` array
- Desktop unchanged (except x-axis swap from time → score)

**Phase 2 (polish, ~2h):**
- Score × flow scatter on desktop with jitter
- Category filter chips
- Card expand/collapse animation on mobile
- Pull-to-refresh

**Phase 3 (optional):**
- Swipe-to-dismiss on mobile cards
- Persistent notification dot for new alerts since last view
- WebSocket for real-time updates (currently polling every 45s)

---

## Rejected Alternatives

| Alternative | Why rejected |
|---|---|
| Treemap | Good for proportions, bad for individual signal drill-down. Categories are too uneven (90% sports). |
| Heatmap grid | Requires a lot of markets to look meaningful. With 10-40 active alerts, it's mostly empty. |
| Force-directed | Beautiful but unpredictable layout — bubbles move every frame, hard to tap on mobile. No stable spatial meaning. |
| Swimlanes | Good idea but adds complexity without solving the core problem (mobile card feed is simpler and better). |

---

## Summary

| Viewport | Current | Proposed |
|---|---|---|
| Mobile | Canvas bubble chart (unusable) | Card feed sorted by score |
| Desktop | Time × Flow scatter (clusters) | Score × Flow scatter + tape |
| Interaction | Hover tooltip + click navigate | Tap → pin card → action buttons |
