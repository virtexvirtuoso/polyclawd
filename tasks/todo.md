# Polyclawd — Pending Tasks

> Updated 2026-06-11. Scope: Kalshi weather fade strategy + shadows.
> Strategy registry: vault `02-Projects/Polyclawd/Strategy/Kalshi-Fade-Optimization-Knobs.md`
> Evidence base: vault `02-Projects/Polyclawd/Research/Weather-Edge-Analysis-Jun2026.md` (§10 + QA corrections)

## Dated decisions (evidence accumulates automatically — just decide on the day)

- [ ] **Jun 12** — First realized verdict: did the 4 taker positions win; were maker fills profitable or adverse-selected (join-mode `ev_per_dollar`, both venues). Arrives in the 10:00 Telegram report; one-shot Claude check at 10:23.
- [ ] **~Jun 13** — Flip knob 1 (ranked fill) after 2–3 clean baseline nights: `POST /api/engine/weather-strategies?kalshi_fade_ranked_fill=true` (no deploy needed). Consider the ET-eats-the-cap observation (vault knobs doc §3b) when judging week-one results.
- [ ] **~Jun 17** — Maker read (1 week of fills): decides knob 7 (PM maker) build-or-skip AND whether to design knob 2 implementation.
- [ ] **~Jun 24** — Favorite tier (0.50–0.70 buy-YES): resize ($50→$100) or kill, using executable-price EV from collected sheets. Mid-based +12% is UNVERIFIED.
- [ ] **~Jun 24–28** — Two-week gate review: realized taker EV vs the QA band [+0.4%, +2.0%]/$1. Pass → live-money discussion gated on the compliance checklist below. Fail → extend or kill.

## Pre-live compliance gate (none blocks paper; ALL block live capital)

- [ ] **Mr. V reads Kalshi Developer Agreement + Member Agreement** (~30 min; bot-blocked pages, human-only). kalshi.com/developer-agreement, kalshi.com/docs/kalshi-member-agreement.pdf
- [x] Maker blackout windows — DONE 2026-06-11: ±10min around 00/06/12/18Z synoptic times in both maker shadows (deployed; live maker logic inherits the same rule)
- [ ] Tax record pipeline: trade-level export (Kalshi issues NO 1099-B for trading P&L); CPA question on §1256 vs ordinary
- [ ] Real-time fill monitor (Kalshi erroneous-trade review window = 15 minutes)
- [ ] Encode $25k/contract position-limit hard ceiling (bids count toward it)
- [ ] Live funds policy: minimal float on exchange, scheduled profit sweeps (segregation ≠ insurance)
- [ ] If maker validates: apply to Kalshi Liquidity Incentive Program (paid resting orders)

## Engineering / research (this week, unblocked)

- [x] Knob 6 backfill spike — DONE 2026-06-11, verdict SPLIT: ensemble MEMBERS not recoverable (archive keeps members for today only; previous-runs serves none) → distribution engine trains on forward-collected snapshots. Deterministic lead-1 history IS recoverable and was archived same-day before the 90-day window rolls (`scratch/data/deterministic_lead1_archive_20260611.json`, 20 cities × 2 models, Apr 2 →) → bias-correction layer can be fitted against the 26k labels now
- [x] Knob 4 — DONE 2026-06-11, NULL RESULT: tail mispricing is FLAT across the whole evening (gap −4.5 to −5.1¢ at local 16/18/20/22/24, all sig, all CIs overlapping; spreads flat 2.2–2.5¢). Entry hour is not a lever — 19:30–20:30 window stays. Removes knob 4 from the board. Side hypothesis for later: window could be WIDENED for capacity (more candidates, same edge) — post knob-1, own validation. `scratch/kalshi_timing_curve.py`
- [ ] Watch: MLB-props 429 rate-limit bursts share Kalshi's API budget with the fade scanner — if a night shows mass `no_quotes` skips, this is suspect #1
- [ ] Watch: Sunday Jun 15 — repaired Weekly Signal + IC Calibration cron proves itself (timeout was the failure)

## Housekeeping (awaiting explicit go from Mr. V)

- [x] Formal `/decision-log-entry`: weather kill-trigger decision — logged 2026-06-11 in `00-Dashboard/Decision Log.md` + daily note
- [x] Skills Backlog: `kalshi-market-data` added 2026-06-11
- [x] Broken wikilinks fixed 2026-06-11: both evidence notes reconstructed (provenance-marked) in Polyclawd/Research/

## Done (this build, for context)

- [x] Two-venue tail-calibration audit + QA corrections (§10)
- [x] Kalshi fade paper strategy deployed (entries live 2026-06-10, cap bound night one)
- [x] Maker shadows live both venues; first fills measured (K 43%/74%, PM 29%/46%)
- [x] Ensemble recorder live (knob 6 training data)
- [x] Dashboards replaced; crons updated; void/zombie resolver fixes; blindspots audit
