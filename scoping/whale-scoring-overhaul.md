# Whale Alert Scoring Overhaul — Scoping Document
**Author:** Maestro (Quant Research)  
**Date:** 2026-06-16  
**Status:** DRAFT — awaiting Mr. V review before implementation

---

## 1. Current System Audit

### Architecture (as-built)

The whale system has two independent scoring layers that are often confused:

**Layer 1 — `whale_scanner.py` (integer score, stored in DB)**  
`_compute_raw_score()` returns an unbounded float (≥0) from:
- Base = sweep_score + book_score (Kalshi contract counts / PM volume delta)
- Bonuses: log-scaled flow magnitude, flow intensity (% of lifetime vol), taker aggressiveness %, wallet concentration
- Penalties: thin market (<$5k lifetime vol), bilateral flow (both sides getting hit), book-only (no executed flow), market maturity (>14d to settle)
- `severity_for(raw)`: CRITICAL ≥ 10.0, HIGH ≥ 6.0, LOW ≥ 3.0
- Gates in `alert_gate()`: first_sight, usd_floor ($2k), near_settled
- Hard caps in `_mk_alert()`: no-flow → HIGH max; <$25k → HIGH max (CRITICAL_FLOW_USD)
- Live-game ceiling (LIVE_GAME_KALSHI_PREFIXES): per-game sports cap at HIGH unless class-outlier, smart-wallet pierce, or pregame steam

**Layer 2 — `api/routes/whale.py::_score_alert()` (composite 0-1, sent to Telegram)**  
Reads from `whale_outcomes` table (VPS). Five components, each 0-1 sub-scored, then weighted sum:
```
score = flow_size×0.30 + wallet_rep×0.25 + spread×0.15 + urgency×0.15 + archetype_bonus×0.15
```
Thresholds in `whale_alert_tg.py`:
- `MIN_SCORE = 0.48`, `MIN_FLOW = $5,000`, `MIN_WALLET_WR = 0.45`, `MAX_HTR = 72h`

**The Telegram bot reads Layer 2 scores**, not Layer 1 integer scores.

---

## 2. Current Problems — Specific, With Data

### Problem 1: OI is not carried into `whale_outcomes`, so flow/market-size is unbounded

From live API audit (2026-06-16):
```
CRITICAL | flow=$750    | OI=$0 | Greenland by-election (UK)
CRITICAL | flow=$9,733  | OI=$0 | Iran peace deal
CRITICAL | flow=$32,368 | OI=$0 | Trump/Greenland 2027
CRITICAL | flow=$498,524| OI=$0 | Belgium WC match
```
**All OI = 0 in the API response.** The `whale_outcomes` table schema does not have an `open_interest` column. `market_state` has `oi` (Kalshi) and `volume` (PM) but they're never joined into `whale_outcomes`. The Layer 2 scorer (`_score_alert`) never sees market size — it treats $750 on a $50k market the same as $42k on a $1M market.

Market size distribution from local `market_state` (11,145 markets):
- Kalshi: avg OI = $927, max = $227k. 7,990 markets (72%) have OI < $1k.
- Polymarket: avg OI = $457k, max = $18M. 218 markets (14%) under $10k.

**Impact:** A $5,001 flow on a Kalshi market with $800 OI passes `MIN_FLOW` while representing 6.25× the market's entire open interest — clearly a market-mover. A $5,001 flow on a Polymarket market with $2M OI is 0.25% of liquidity — statistical noise.

### Problem 2: 65% of CRITICAL alerts have `direction = None`

From live data:
```
direction=1: 7 / 20 CRITICAL alerts (35%)
direction=None: 13 / 20 CRITICAL alerts (65%)
```
Direction=None means the scoring system couldn't determine whether the whale is betting YES or NO. This is actionable information precisely because of the direction: without it, the alert says "something happened" but not "buy what." These alerts should be demoted, not sent at CRITICAL.

**Root cause:** PM multi-outcome markets (spread/totals/draw) aggregate flow across all outcomes without resolving net direction. Kalshi direction is available from the taker side of trade flow.

### Problem 3: `hours_to_resolve` is None for 15/20 CRITICAL alerts

HTR is computed from `close_time` in `whale_scanner.py` but not always stored in `whale_outcomes`. Without HTR:
- Urgency sub-score defaults to 0.3 (the "missing" fallback in `_score_alert`) — neutral, not a penalty
- The urgency pathway in the proposed formula can't fire

**Most egregious example:** "Will Norway win on 2026-06-16?" (today) should have htr ≈ 3-6h but shows `htr=None`. Alert fires and ranks as CRITICAL with no urgency boost, same as a 4,802h Greenland market.

### Problem 4: `MIN_FLOW = $5,000` is too low as an absolute gate

From the live CRITICAL batch:
- `$750` flow alert reached CRITICAL — passed `MIN_FLOW = $5,000` at the Telegram gate? No — let's check. `whale_alert_tg.py` requires `MIN_FLOW = 5000` but the API `/whale/top` returns it. The issue is the API layer doesn't filter by MIN_FLOW; the Telegram script does filter, but only AFTER fetching from `/whale/top`. The bot fetches 20 CRITICALs + 10 HIGHs and then applies `is_actionable()`.

So the $750 alert is in the batch but should be filtered at `is_actionable()`. **But it's ranked #14 in the CRITICAL list anyway**, meaning it scored high enough on the wallet_rep sub-score (WR=0.91 from 24 trades) to survive. The 0.51 score just barely passes `MIN_SCORE = 0.48`.

### Problem 5: Wallet WR weighting is too coarse and mixes signal

Current wallet sub-score:
```python
ws = 1.0 if wr >= 0.65 else 0.7 if wr >= 0.55 else 0.4 if wr >= 0.45 else 0.2
```
This is a step function with large gaps. A wallet at 64.9% WR scores ws=0.7; at 65.0% it jumps to 1.0. At 45% it scores 0.4; at 44.9% it scores 0.2 — a 50% penalty for 0.1 percentage point. Meanwhile, `wallet_n` (number of trades behind the WR estimate) is ignored entirely in the scoring. A 0.90 WR from 5 trades (high variance) scores the same as 0.90 WR from 200 trades (low variance).

### Problem 6: `flow_size` sub-score ignores market context — absolute, not relative

```python
fs = 1.0 if fd >= 100k else 0.8 if fd >= 50k else 0.6 if fd >= 25k else 0.4 if fd >= 10k else 0.2
```
$32k on Greenland (OI unknown, price=0.938 → near-settled) → fs=0.6. Same score as $32k on a $10M election market. The absolute dollar amount is a proxy for "big bet" but ignores that on different platforms/markets, "big" means different things.

### Problem 7: `archetype_bonus` penalizes sports at 0.15, same as live-game noise

Sports archetype → ab=0.15. This is the correct direction, but it applies to all sports markets — including the World Cup futures ($421k Saudi Arabia WC winner) and per-game live markets ($51k Colorado vs Chicago daily). These have fundamentally different signal-to-noise ratios. The live-game ceiling in the scanner handles this partially, but the Layer 2 scorer doesn't distinguish futures from per-game.

---

## 3. Proposed Scoring Formula

### Design principles

1. **Market-relative, not absolute** — flow should be normalized by market size
2. **Direction required for CRITICAL** — ambiguous alerts cap at HIGH
3. **Wallet confidence-weighted** — sample size matters, not just WR point estimate
4. **Time urgency on closing markets, not open-ended ones** — Greenland 2027 and a WC match closing in 3h are not comparable
5. **Actionability gate** — if we can't give a direction, the alert has lower value

### Formula

```
final_score = (flow_component + wallet_component + spread_component 
               + urgency_component + archetype_component) × direction_multiplier
```

Where:
```
flow_component    = flow_intensity_score × 0.35
wallet_component  = wallet_confidence_score × 0.25
spread_component  = spread_score × 0.10
urgency_component = urgency_score × 0.20
archetype_component = archetype_score × 0.10
direction_multiplier = 1.0 if direction != None else 0.65
```

The direction_multiplier replaces the hard gate on ambiguous alerts — they still appear but score 35% lower, naturally falling below CRITICAL threshold.

### Sub-score definitions

**`flow_intensity_score` (0–1):**
```python
# Normalize flow against market open interest/liquidity
market_size = max(open_interest or liquidity or 10_000, 10_000)
flow_pct = flow_dollars / market_size  # ratio, can exceed 1.0

# Log-scaled intensity
if flow_pct >= 0.50:     intensity = 1.0   # >= 50% of market
elif flow_pct >= 0.20:   intensity = 0.85  # 20-50%
elif flow_pct >= 0.10:   intensity = 0.70  # 10-20%
elif flow_pct >= 0.05:   intensity = 0.55  # 5-10%
elif flow_pct >= 0.02:   intensity = 0.40  # 2-5%
elif flow_pct >= 0.005:  intensity = 0.25  # 0.5-2%
else:                    intensity = 0.10  # <0.5% — noise

# Absolute floor: no matter the ratio, sub-$5k absolute = cap at 0.40
if flow_dollars < 5_000:
    intensity = min(intensity, 0.40)
# Absolute boost: >$100k absolute = floor at 0.60 (big whale regardless of market size)
if flow_dollars >= 100_000:
    intensity = max(intensity, 0.60)
```

**`wallet_confidence_score` (0–1):**
```python
if wallet_win_rate is None or wallet_n is None:
    ws = 0.30  # unknown wallet, slight positive (may be new)
elif wallet_n < 5:
    ws = 0.35  # too few trades to trust
else:
    # Wilson lower confidence bound (95% CI lower bound on binomial)
    # z=1.645 for 90% CI (one-sided):
    import math
    n, p, z = wallet_n, wallet_win_rate, 1.645
    lower = (p + z**2/(2*n) - z*math.sqrt(p*(1-p)/n + z**2/(4*n**2))) / (1 + z**2/n)
    # Scale the Wilson lower bound:
    ws = 1.0 if lower >= 0.70 else \
         0.80 if lower >= 0.60 else \
         0.60 if lower >= 0.50 else \
         0.40 if lower >= 0.40 else \
         0.20
```

The key insight: a WR=0.90 from 8 trades has Wilson lower bound ≈ 0.57, scoring ws=0.60. A WR=0.70 from 200 trades has Wilson lower bound ≈ 0.64, scoring ws=0.80. The more reliable track record wins, as it should.

**`spread_score` (0–1):** Unchanged from current:
```python
# Tight spread = informed market; wide spread = illiquid/uncertain
ss = 1.0 if sp < 20 else 0.7 if sp < 50 else 0.4 if sp < 100 else 0.2 if sp < 200 else 0.1
```

**`urgency_score` (0–1):**
```python
if htr is None:
    us = 0.20  # unknown — penalize, not neutral (was 0.30)
elif htr <= 0:
    us = 0.0   # resolving now — skip
elif htr < 1:
    us = 1.0   # <1h = highest urgency
elif htr < 4:
    us = 0.85
elif htr < 12:
    us = 0.65
elif htr < 48:
    us = 0.40
elif htr < 168:
    us = 0.20  # 2-7 days = low urgency
else:
    us = 0.05  # >1 week = near zero urgency
```

Key change: `htr=None` now penalizes (0.20 vs current 0.30), and long-dated markets (>168h) get near-zero urgency instead of 0.10-0.30. This deprioritizes the Greenland 2027 (4,802h) and UK by-election alerts naturally.

**`archetype_score` (0–1):**
```python
# Keep current map but add per-game vs futures distinction:
arch_score = {
    "weather":          1.0,   # high hit rate
    "election":         0.70,  # slower but actionable
    "deadline_binary":  0.60,  # binary events, clean resolution
    "other":            0.50,
    "index":            0.30,
    "sports_futures":   0.25,  # season-level, low churn
    "sports":           0.10,  # per-game: same as current (churn-y)
}
```
Requires adding "sports_futures" archetype detection (see §5).

**`direction_multiplier`:**
```python
# Ambiguous direction = alert is harder to act on
direction_multiplier = 1.0 if direction in (1, -1) else 0.65
```
At 0.65× multiplier, an alert at the current MAX_SCORE (≈0.87) with ambiguous direction → 0.57, still above `MIN_SCORE=0.48` but demoted. A borderline alert (0.55) with no direction → 0.36, filtered. This is softer than a hard gate — preserves very strong ambiguous signals while cleaning up the borderline noise.

### Severity thresholds (revised)

| Tier | Composite Score | Description |
|------|----------------|-------------|
| CRITICAL | ≥ 0.68 | Directional, large relative flow, proven wallet, timebound |
| HIGH | 0.52–0.67 | Missing one signal pillar (direction OR wallet OR urgency) |
| LOW | 0.38–0.51 | Dashboard-visible, not Telegram-pushed |
| SUPPRESSED | < 0.38 | Don't store (or archive-only) |

**Telegram gate (revised `is_actionable()`):**
- `MIN_SCORE = 0.52` (up from 0.48 — aligns with new HIGH floor)
- `MIN_FLOW = 7_500` (up from 5,000 — $5k absolute is too low even on Kalshi)
- `MIN_WALLET_WR = 0.45` → unchanged, but now Wilson-adjusted in score
- `MAX_HTR = 120` (up from 72 — allow 5-day window for political events, but urgency score naturally ranks them lower)
- Add `REQUIRE_DIRECTION_FOR_CRITICAL = True` — hard gate at `is_actionable()`: if severity == "CRITICAL" and direction is None, send as HIGH

---

## 4. Where OI/Liquidity Comes From

### Kalshi
`market_state.oi` = `open_interest_fp` from the `/markets` batch enrichment call. Already stored per-market. **Available now.**

### Polymarket
`market_state.volume` = `volumeNum` from Gamma API (proxy). True liquidity = `liquidityNum` from Gamma (sum of CLOB book depth). PM `market_state` currently stores `volume` not `liquidityNum`.

**Recommendation:** Use whichever is available:
```python
def get_market_size(platform, market, meta):
    if platform == "kalshi":
        return meta.get("oi") or 10_000   # from market_state
    # polymarket: prefer liquidity, fall back to volume/5 (rough proxy)
    liq = meta.get("liquidityNum") or 0
    vol = meta.get("volume") or 0
    return max(liq, vol * 0.05, 10_000)   # vol/20 ≈ typical PM liq/vol ratio
```

The `10_000` floor prevents division-by-zero and keeps a minimum meaningful denominator.

### Schema change needed

Add `open_interest REAL` column to `whale_outcomes` table in `whale_meta.db`. The alert metadata already has `oi` from `market_state` — it's just not being written. See §5 implementation plan.

---

## 5. Implementation Plan

### Files to change

| File | Change | Effort |
|------|--------|--------|
| `api/routes/whale.py` | Replace `_score_alert()` with new formula; add OI lookup from scanner.market_state | 2h |
| `api/routes/whale.py` | Update `whale_top()` to join market_state for OI | 1h |
| `scripts/whale_alert_tg.py` | Update `is_actionable()` thresholds + direction gate for CRITICAL | 30m |
| `scripts/whale_alert_tg.py` | Update `MIN_SCORE=0.52`, `MIN_FLOW=7500`, `MAX_HTR=120` | 15m |
| `whale_meta.db` (migration) | `ALTER TABLE whale_outcomes ADD COLUMN open_interest REAL` | 5m |
| `signals/whale_scanner.py` | Write `oi` to `whale_outcomes` when upserting alert outcome row | 1h |
| `signals/whale_scanner.py` | Add "sports_futures" archetype detection | 30m |

**No changes needed to:**
- Sweep detection logic (`_compute_raw_score`, sweep thresholds)
- Live-game ceiling logic — it already works correctly upstream
- Book scanner thresholds — those are raw signal, not scoring
- Dedup logic in `whale_alert_tg.py` — 4h dedup window is correct

### Step-by-step order

1. **Migrate schema** — add `open_interest` to `whale_outcomes`; backfill from `market_state` where market matches (partial, best-effort)
2. **Update `_score_alert()`** in `whale.py` — swap in new formula; test against the 20 current CRITICALs, verify ranking changes match expectations
3. **Update `whale_top()` JOIN** — attach scanner DB, left-join `market_state` for OI on each alert
4. **Update `is_actionable()` and constants** in `whale_alert_tg.py`
5. **Test on VPS** — run `whale_alert_tg.py` in `--dry-run` mode for one full scanner cycle, compare old vs new alert list
6. **Deploy** — `scp` + `systemctl restart polyclawd-api`

### OI join in `whale_top()` (concrete code sketch)

```python
# After attaching scanner DB, add to the entry loop:
try:
    oi_row = conn.execute(
        "SELECT oi FROM scanner.market_state WHERE platform=? AND market=?",
        (r["platform"], r["market"])
    ).fetchone()
    oi = oi_row["oi"] if oi_row else None
except Exception:
    oi = None
entry["open_interest"] = oi
```

Then pass `oi` into `_score_alert()`:
```python
def _score_alert(row, oi=None):
    ...
    market_size = max(oi or 0, 10_000)
    flow_pct = fd / market_size
    ...
```

---

## 6. Threshold Recommendations by Severity Tier

### Expected score ranges (calibrated against current live data)

Using today's 20 CRITICALs as reference, applying new formula mentally:

| Alert | flow | OI (est) | flow/OI | WR | htr | direction | Old score | New score (est) |
|-------|------|----------|---------|-----|-----|-----------|-----------|-----------------|
| Belgium WC ($498k) | $498k | PM ~$500k | ~100% | ? | None | None | 0.52 | 0.55–0.62 (no direction penalty) |
| Saudi WC ($421k) | $421k | KX ~$50k | ~840% | ? | None | +1 | 0.54 | 0.72–0.78 ✓ CRITICAL |
| Spain/CaboVerde O/U ($205k) | $205k | PM ~$100k | ~205% | ? | None | None | 0.52 | 0.45–0.52 (no direction → demotion) |
| Trump/Greenland ($32k) | $32k | PM ~$5M | ~0.6% | 0.917/24 | 4802h | None | 0.62 | 0.30–0.38 SUPPRESSED ✓ |
| UK by-election ($750) | $750 | PM ~$5k? | ~15% | 0.917/24 | None | None | 0.51 | 0.25–0.30 SUPPRESSED ✓ |
| Iran peace deal ($42k) | $42k | PM ~$200k | ~21% | ? | 26h | None | 0.59 | 0.42–0.48 LOW (no direction) |

The new formula:
- Kills the Greenland 2027 long-dated alert (4,802h urgency penalty)
- Kills the $750 UK by-election (tiny flow, no direction, no urgency)
- Keeps Saudi WC winner (massive flow/OI ratio, directional bet = YES)
- Demotes Spain/Uruguay spreads (no direction on multi-outcome markets)
- Allows Iran peace deal as LOW (newsworthy $42k with 26h left but no direction)

**Target: ≤4 CRITICAL per batch, ≤6 HIGH per batch (max 10 total Telegram sends per cycle)**

### Revised gate summary

```python
# whale_alert_tg.py
MIN_SCORE       = 0.52   # was 0.48
MIN_FLOW        = 7_500  # was 5_000
MIN_WALLET_WR   = 0.45   # unchanged
MIN_WALLET_N    = 5      # unchanged
MAX_HTR         = 120    # was 72 (allow 5-day events, score sorts them lower)
MIN_HTR         = 0.5    # unchanged
```

---

## 7. Risk Analysis

### Risk 1: OI data gap on Polymarket
**What:** Polymarket `market_state.oi` column is often 0 or None (PM uses `volume` not `oi`; the PM sweep writes volume deltas, not liquidity). The `market_size` fallback of 10,000 may under-represent $1M+ markets, inflating flow_pct.

**Mitigation:** Use `max(liq, vol*0.05, 10_000)` as described. For PM alerts, we already have `liquidityNum` in the Gamma fetch — confirm it's being written to `market_state` or `alert_meta`. If not, add it (1h work).

**Worst case:** PM market_size = 10,000 fallback → a $32k flow always → 320% ratio → intensity=1.0. This creates a false floor at full intensity for large PM markets with missing OI. The absolute boost floor (`>$100k → intensity ≥ 0.60`) also fires here.

### Risk 2: Wilson lower bound suppresses new proven wallets
**What:** A wallet with 5 trades at 100% WR has Wilson lower bound ≈ 0.52, scoring ws=0.40. This is mathematically correct (small sample) but could suppress a genuinely sharp new wallet.

**Mitigation:** Add a smart-wallet flag bypass: if `smart=True` (set by scanner when wallet is in the pre-approved smart list), use the raw WR instead of the Wilson bound. Currently the scanner already tracks `smart` wallets via `pm_wallets.smart=1`.

### Risk 3: Direction_multiplier=0.65 may be too aggressive on PM markets
**What:** Most Polymarket multi-outcome markets (spreads, totals) never have direction set. At 0.65×, a genuine $500k WC match signal scores: best case 0.85 → 0.55 — still HIGH, but just barely. Fine for HIGH; but if the spread changes, could flip to filtered.

**Mitigation:** Only apply direction_multiplier=0.65 when `direction is None` AND flow < $100k. Whale-sized flows (>$100k) in multi-outcome PM markets are inherently interesting regardless of ambiguous direction.
```python
direction_multiplier = 1.0 if direction in (1,-1) else \
                       0.80 if flow_dollars >= 100_000 else 0.65
```

### Risk 4: Removing HTR=None neutral default breaks markets without close_time
**What:** Some Kalshi markets don't have a parseable close_time (e.g. open-ended "Will X happen?" type). Currently these score urgency=0.30 (neutral). Under new formula they'd score 0.20 (penalty).

**Mitigation:** Check market series: if the market is an open-ended policy market (no date in ticker), treat as `htr=None → 0.20`. If it's a dated event (ticker has date), this is a data quality issue and should be fixed upstream by parsing the event date from the ticker (already partially done in `_ticker_event_date()`). Add: if `htr is None` and `_ticker_event_date()` returns a date, compute HTR from that.

### Risk 5: Regression in alert volume may hide edge
**What:** If we cut from 12 alerts to 4-6 per batch, we may miss 1-2 genuine signals that fall in the 0.52–0.55 gray zone. Current batch sends ~8-12 every cycle; cutting by 50-60% will reduce both noise AND signal in proportion.

**Mitigation:** Continue writing all alerts to `whale_alerts` DB regardless of score gate. The Telegram gate is display-only; DB logging stays at score ≥ 3 (Layer 1) or any score (Layer 2). Shadow-validate the new formula for 7 days before applying it to Telegram. Use `/dry-run` mode.

---

## 8. What NOT to Change

- **Sweep detection thresholds** (`VOL_SPIKE_ABS`, `OI_SPIKE_ABS`, `THIN_FLOW_*`) — those determine what enters the pipeline, not how it's ranked
- **Live-game ceiling logic** — the class-outlier / smart-wallet / pregame-steam pierce rules work correctly upstream
- **`ALERT_DEDUP_S = 1800`** in scanner — 30min dedup on DB writes is correct; Telegram has its own 4h dedup
- **Book scanner rotation** (`ROTATE_BOOK_CAP`, `WATCH_OI_MIN/MAX`) — not related to scoring
- **`CRITICAL_FLOW_USD = 25_000`** in scanner — this is a Layer 1 gate that still makes sense

---

## 9. Open Questions for Mr. V

1. **OI source for Polymarket:** Should we write `liquidityNum` from the Gamma enrichment into `market_state`? Currently only `volume` is stored for PM markets. This is the single biggest data gap for the liquidity_factor.

2. **Direction gate severity:** Should `direction=None` → hard cap at HIGH (never CRITICAL) vs the softer 0.65× multiplier? The hard cap is simpler but may lose some genuine large-flow PM signals.

3. **Sports futures archetype:** The scanner tags markets by `market_archetype` but doesn't yet distinguish "sports futures" (season/championship) from "sports" (per-game). Worth adding? It's a one-regex check against the market ticker (no game date = futures).

4. **Batch size target:** Is 4 CRITICAL + 6 HIGH per batch the right target? Or should the goal be even stricter — e.g. top 3 only?

5. **Wilson WR vs raw WR:** Comfortable with the statistical adjustment, or prefer to keep the current step-function but add `wallet_n` as a divider?

---

## 10. Summary

**Root cause of noise:** Three flat absolute thresholds (`MIN_SCORE=0.48`, `MIN_FLOW=$5k`, `MIN_WALLET_WR=0.45`) applied without market context. `OI` is not available in the scoring layer. `direction=None` doesn't penalize. Long-dated markets (4,800h) score the same urgency as closing-in-3h markets.

**The fix:** Market-relative flow intensity (`flow/OI` instead of absolute `$flow`), Wilson-adjusted wallet confidence (sample size matters), urgency decay on long-dated markets (0.05 score for >1 week), and a direction multiplier (0.65×) that naturally demotes ambiguous multi-outcome signals.

**Estimated effort:** ~5h total. Zero risk to pipeline throughput. All changes are in `api/routes/whale.py` and `scripts/whale_alert_tg.py` (read-only layers on the scanner output). The scanner itself (`signals/whale_scanner.py`) only needs the OI write into `whale_outcomes` and the `open_interest` schema migration.
