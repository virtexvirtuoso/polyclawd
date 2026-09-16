# Portfolio Dashboard — Mobile Improvements Plan

**Date:** 2026-02-23
**Page:** `static/portfolio.html`
**URL:** https://virtuosocrypto.com/polyclawd/portfolio.html

## Bug Fix Applied (2026-02-23)

**Root cause of blank dashboard:** Escaped template literals in `manualClose()` function (lines 533, 537, 539) caused `Invalid or unexpected token` JS error, killing all JavaScript execution. The page loaded but no render functions ever ran — showing default empty state ($500.00 bankroll, 0 positions).

**Fixes applied:**
1. Unescaped `\`` → `` ` `` and `\${}` → `${}` in `manualClose()` function
2. Added `.catch()` fallbacks to 2 unprotected fetches in `Promise.all` (status + history) — a single 502 from nginx would previously blank the entire page
3. All 6 fetches now have graceful degradation

## Current Mobile State

| Aspect | Status | Score |
|--------|--------|:-----:|
| Viewport meta tag | Correct | 5/5 |
| Media query breakpoints | Only 768px, no phone breakpoint | 2/5 |
| Table responsiveness | Zero mobile handling (6-col + 8-col overflow) | 1/5 |
| Tap target sizes | Buttons OK (40px+), Mark Won/Lost too small (28px) | 2/5 |
| Touch events | None — only `:hover`, no `:active` | 0/5 |
| Padding/spacing on mobile | Minimal reduction | 2/5 |
| Font sizes on mobile | Minimal scaling | 2/5 |

**Single existing `@media` query (768px):**
- `h1` 42px → 28px
- Stats grid → 2-column
- Positions grid → 1-column
- Controls → flex-wrap
- Section body padding → 16px

## Consensus Matrix

3 agents evaluated 10 potential improvements: UI Designer, Frontend Developer, UX Researcher.

| # | Improvement | UI Designer | Frontend Dev | UX Researcher | Verdict |
|---|------------|:-----------:|:------------:|:-------------:|:-------:|
| 1 | Responsive tables | 9 PRIORITY | 9 | 9 PRIORITY | **DO NOW** |
| 2 | Phone breakpoint (480px) | 9 PRIORITY | 9 | 10 PRIORITY | **DO NOW** |
| 3 | Bigger tap targets + `:active` | 8 PRIORITY | 9 | 9 PRIORITY | **DO NOW** |
| 4 | Collapsible sections | 7 DEFER | 6 DEFER | 7 PRIORITY | QUEUE |
| 5 | Swipe gestures | 3 SKIP | 3 SKIP | 3 SKIP | **SKIP** |
| 6 | Bottom sticky nav | 6 DEFER | 9 | 8 PRIORITY | QUEUE |
| 7 | Touch-optimized chart | 5 DEFER | 4 DEFER | 4 DEFER | DEFER |
| 8 | Progressive disclosure | 7 DEFER | 5 DEFER | 8 PRIORITY | QUEUE |
| 9 | Pull-to-refresh | 2 SKIP | 9 | 4 DEFER | **SKIP** |
| 10 | OLED black mode | 2 SKIP | 7 | 2 SKIP | **SKIP** |

## DO NOW — Priority Implementations

### P0: Phone Breakpoint (480px)

**Why:** Zero CSS below 768px. At 375px width (iPhone), stat cards are cramped in 2-col grid, container wastes 40px padding on each side.

**Changes:**
```css
@media(max-width:480px) {
  .container { padding: 20px 12px }
  h1 { font-size: 22px }
  .stats-row { grid-template-columns: 1fr }
  .stat-value { font-size: 20px }
  .stat-label { font-size: 9px }
  .section-body { padding: 12px }
  .section-header { padding: 14px 16px }
  .section-title h2 { font-size: 15px }
  .pos-grid { grid-template-columns: repeat(2, 1fr) }
  .signal-row { flex-wrap: wrap; gap: 6px }
  .subtitle { font-size: 13px }
  .chart-container { height: 200px }
}
```

### P1: Responsive Tables → Card Layout

**Why:** Trade History (6 columns) and Archetype Breakdown (8 columns) overflow horizontally on every phone. No amount of font reduction fixes 8 columns in 375px.

**Approach:** CSS-only conversion using `display:block` on table elements at 480px, with `data-label` attributes for column headers.

```css
@media(max-width:480px) {
  .arch-table thead { display: none }
  .arch-table tr { display: block; margin-bottom: 12px; border: 1px solid var(--border); border-radius: 8px; padding: 12px }
  .arch-table td { display: flex; justify-content: space-between; padding: 4px 0; border: none }
  .arch-table td::before { content: attr(data-label); color: var(--text2); font-size: 10px; text-transform: uppercase }

  /* Same pattern for trade history table */
  table thead { display: none }
  table tr { display: block; margin-bottom: 12px; border: 1px solid var(--border); border-radius: 8px; padding: 12px }
  table td { display: flex; justify-content: space-between; padding: 4px 0; border: none }
  table td::before { content: attr(data-label); color: var(--text2); font-size: 10px }
}
```

**JS change:** Add `data-label` attributes when rendering table cells.

### P2: Bigger Tap Targets + `:active` States

**Why:** Mark Won/Lost buttons are `padding:4px 10px` (~28px tall) — below 44px WCAG minimum. `:hover` effects do nothing on touch screens.

**Changes:**
```css
@media(max-width:768px) {
  .pos-btn { padding: 10px 16px; min-height: 44px; font-size: 13px }
  .btn { padding: 12px 24px; min-height: 44px }
  .nav-link { padding: 12px 20px; min-height: 44px }
}

/* Touch-specific: replace hover with active */
@media(hover: none) and (pointer: coarse) {
  .pos-btn.won:active { background: rgba(0,230,118,0.2) }
  .pos-btn.lost:active { background: rgba(255,82,82,0.2) }
  .btn-primary:active { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(108,92,231,0.3) }
  .pos-card:active { border-color: var(--accent) }
  .stat-card:active { border-color: var(--accent) }
}
```

## QUEUE NEXT

### Collapsible Sections
- 7 sections = 15+ screen-heights of scrolling on mobile
- Collapse Archetype Breakdown, Resolve Log, and Equity Curve by default on mobile
- Tap section header to toggle
- ~15 lines JS + CSS `max-height` transition

### Bottom Sticky Nav
- Fixed bar with Process Signals + Refresh buttons
- Always within thumb reach
- Main dashboard (`virtuoso.css` lines 707-765) already has the pattern with `safe-area-inset-bottom`
- ~20 lines CSS + move button onclick handlers

### Progressive Disclosure
- Show only Bankroll, P&L, Win Rate, Positions on load
- "Show all stats" toggle reveals remaining 5 stat cards
- Reduces cognitive load for quick-glance use case
- ~10 lines JS

## SKIP — Rejected Items

| Item | Reason |
|------|--------|
| **Swipe gestures** | High complexity in vanilla JS, conflicts with scroll/back-navigation, risks accidental position closures on financial actions. The fix is bigger buttons, not different interaction patterns. |
| **Pull-to-refresh** | Already auto-refreshes every 60s + manual Refresh button exists. Conflicts with native browser pull-to-refresh (Safari/Chrome). Would add accidental-refresh risk on a data-heavy page. |
| **OLED black mode** | Background is already `#0a0a0f` (98% black). Going to `#000000` saves imperceptible battery and introduces AMOLED smearing artifacts during scroll. |

## Implementation Effort

| Item | Effort | Type |
|------|--------|------|
| Phone breakpoint | ~30 lines CSS | CSS only |
| Responsive tables | ~40 lines CSS + JS `data-label` attrs | CSS + minor JS |
| Bigger tap targets | ~20 lines CSS | CSS only |
| **Total DO NOW** | **~90 lines CSS, ~10 lines JS** | **No backend changes** |
