#!/usr/bin/env python3
"""
phase0_prop_falsification.py — Phase 0 gate for the Player Prop Edge System.

Throwaway validation. Referenced by:
  02-Projects/Polyclawd/Development/prop-edge-system-spec.md §9 (Phase 0)

QUESTION IT ANSWERS
-------------------
The spec's edge calc anchors to a SINGLE sharp book (Pinnacle) and de-vigs with
the PROPORTIONAL method. The /blindspots review (B12, B13) says both are wrong
for low-liquidity longshot props:
  B12 — Pinnacle is not sharp on props (outsourced/low-limit). Anchor to a
        Betfair-weighted CONSENSUS instead.
  B13 — Proportional de-vig over-states longshots (favorite-longshot bias).
        Use SHIN de-vig instead.

This script recomputes every edge the OLD way and the NEW way on the SAME
captured odds, and reports how many "edges" SURVIVE the method change.

  Most edges evaporate  -> the premise is method artifacts. STOP. Rethink.
  A meaningful core holds -> proceed to build, you know the real base rate.

No match results are needed. Survival under correct math IS the falsification;
realized W/L and CLV are later phases.

DATA
----
Reads one JSON file per event in --data-dir, each in The Odds API
/events/{id}/odds shape (bookmakers[].markets[].outcomes[]).
Populate it offline (logged snapshots) or with --pull-historical (costs 10x
credits — player props only exist after 2023-05-03 in the historical API).

USAGE
-----
  # compare on captured snapshots
  python3 scripts/phase0_prop_falsification.py --data-dir ./phase0_data --min-edge 5

  # pull past WC snapshots first (needs ODDS_API_KEY; 10x credit cost), then compare
  python3 scripts/phase0_prop_falsification.py --pull-historical \
      --sport soccer_fifa_world_cup --date 2026-06-12T18:00:00Z --data-dir ./phase0_data
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict

# ── Reuse the repo's de-vig functions if importable; else inline copies ───────
# (Reusing proves B13's point: the Shin method the spec needs ALREADY exists in
#  odds/sports_edge_common.py — the spec's §1.3 _devig_yes reinvented the weaker
#  proportional one.)
try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from odds.sports_edge_common import (  # type: ignore
        american_to_implied_prob,
        devig_shin,
    )
    _SRC = "odds.sports_edge_common"
except Exception:  # standalone fallback — exact copies
    _SRC = "inlined fallback"

    def american_to_implied_prob(odds: int) -> float:
        odds = int(odds)
        return (100.0 / (odds + 100.0)) if odds > 0 else (abs(odds) / (abs(odds) + 100.0))

    def devig_shin(implied, iters: int = 50):
        s = sum(implied)
        if s <= 0 or len(implied) < 2:
            return [p / s for p in implied] if s > 0 else list(implied)
        n = len(implied)
        z = 0.0
        for _ in range(iters):
            roots = [(z * z + 4 * (1 - z) * (p * p) / s) ** 0.5 for p in implied]
            f = sum(roots) - 2.0 - z * (n - 2.0)
            df = sum(((2 * z - 4 * (p * p) / s) / (2 * r) if r > 0 else 0.0)
                     for p, r in zip(implied, roots)) - (n - 2.0)
            if abs(df) < 1e-12:
                break
            z = min(0.999, max(0.0, z - f / df))
        true = [(((z * z + 4 * (1 - z) * (p * p) / s) ** 0.5) - z) / (2 * (1 - z)) if z < 1 else p / s
                for p in implied]
        t = sum(true)
        return [x / t for x in true] if t > 0 else [p / s for p in implied]


# ── Config: the two competing world-views ────────────────────────────────────
# OLD (spec as written): single Pinnacle anchor.
OLD_SHARP_BOOK = "pinnacle"

# NEW (B12 fix): Betfair-weighted consensus; Pinnacle demoted, not trusted alone.
SOCCER_PROP_SHARP_WEIGHTS = {
    "betfair_ex_uk": 0.40,
    "betfair_ex_eu": 0.40,
    "pinnacle":      0.15,
    "williamhill":   0.05,
}

SOFT_BOOKS = {"draftkings", "fanduel", "betrivers", "betmgm",
              "caesars", "onexbet", "skybet", "williamhill_us"}

PROP_MARKETS = {"player_goal_scorer_anytime", "player_to_receive_card"}
SIDE_NAMES = {"yes", "no", "over", "under"}

ODDS_API_BASE = "https://api.the-odds-api.com/v4"


# ── Price handling: accept american OR decimal, return implied (with vig) ──────
def to_implied(price, odds_format: str) -> float | None:
    if price is None:
        return None
    try:
        v = float(price)
    except (TypeError, ValueError):
        return None
    fmt = odds_format
    if fmt == "auto":
        fmt = "american" if abs(v) >= 100 else "decimal"
    if fmt == "american":
        return american_to_implied_prob(int(round(v)))
    if v <= 1.0:
        return None
    return 1.0 / v  # decimal


def devig_proportional_yes(yes_imp: float, no_imp: float) -> float:
    t = yes_imp + no_imp
    return yes_imp / t if t > 0 else yes_imp


def devig_shin_yes(yes_imp: float, no_imp: float) -> float:
    return devig_shin([yes_imp, no_imp])[0]


# ── Parse one event JSON → {(player, market): {book: {"yes": imp, "no": imp}}} ─
def parse_event(ev: dict, odds_format: str):
    title = f"{ev.get('home_team','?')} vs {ev.get('away_team','?')}"
    book_data: dict = defaultdict(lambda: defaultdict(dict))
    for bk in ev.get("bookmakers", []):
        bkey = bk.get("key", "")
        for mk in bk.get("markets", []):
            mkey = mk.get("key", "")
            if mkey not in PROP_MARKETS:
                continue
            for out in mk.get("outcomes", []):
                name = (out.get("name") or "").strip()
                desc = (out.get("description") or "").strip()
                imp = to_implied(out.get("price"), odds_format)
                if imp is None:
                    continue
                low = name.lower()
                if low in SIDE_NAMES and desc:        # Over/Under/Yes/No + player in description
                    player, side = desc, ("yes" if low in ("yes", "over") else "no")
                elif low in SIDE_NAMES and not desc:  # malformed — skip
                    continue
                else:                                  # name = player, yes-implied market
                    player, side = name, "yes"
                book_data[(player, mkey)][bkey][side] = imp
    return title, book_data


def book_yes(sides: dict, devig_fn) -> float | None:
    """Devigged YES prob for one book. Two-way if both sides present, else raw YES."""
    y, n = sides.get("yes"), sides.get("no")
    if y is None:
        return None
    if n is not None:
        return devig_fn(y, n)
    return y  # yes-only market: no de-vig possible; anchor effect is still tested


def old_sharp_yes(books: dict):
    sides = books.get(OLD_SHARP_BOOK)
    if not sides:
        return None
    return book_yes(sides, devig_proportional_yes)


def new_sharp_yes(books: dict):
    """Betfair-weighted consensus of per-book SHIN-devigged YES."""
    num = den = 0.0
    for bkey, w in SOCCER_PROP_SHARP_WEIGHTS.items():
        sides = books.get(bkey)
        if not sides:
            continue
        yes = book_yes(sides, devig_shin_yes)
        if yes is None:
            continue
        num += w * yes
        den += w
    return (num / den) if den > 0 else None


def best_soft_yes(books: dict, devig_fn):
    """Lowest implied (= best odds for the buyer) across soft books."""
    best = None
    for bkey, sides in books.items():
        if bkey not in SOFT_BOOKS:
            continue
        yes = book_yes(sides, devig_fn)
        if yes is None:
            continue
        # yes-only soft markets carry vig; apply a 0.95 haircut (spec §1.3 convention)
        if sides.get("no") is None:
            yes *= 0.95
        if best is None or yes < best[1]:
            best = (bkey, yes)
    return best


# ── Historical puller (opt-in; costs 10x credits) ─────────────────────────────
def _get(url: str):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def pull_historical(sport: str, date: str, data_dir: str, markets: str):
    key = os.getenv("ODDS_API_KEY")
    if not key:
        sys.exit("ODDS_API_KEY not set — cannot pull historical data.")
    books = ",".join(list(SOCCER_PROP_SHARP_WEIGHTS) + sorted(SOFT_BOOKS))
    ev_url = (f"{ODDS_API_BASE}/historical/sports/{sport}/events?"
              f"apiKey={key}&date={urllib.parse.quote(date)}")
    print(f"[pull] historical events @ {date} …  (WARNING: props bill 10x credits)")
    events = _get(ev_url).get("data", [])
    os.makedirs(data_dir, exist_ok=True)
    saved = 0
    for ev in events:
        eid = ev.get("id")
        if not eid:
            continue
        od_url = (f"{ODDS_API_BASE}/historical/sports/{sport}/events/{eid}/odds?"
                  f"apiKey={key}&date={urllib.parse.quote(date)}"
                  f"&markets={markets}&bookmakers={books}&oddsFormat=american")
        try:
            snap = _get(od_url).get("data", {})
        except Exception as e:
            print(f"  [skip] {eid}: {e}")
            continue
        if snap.get("bookmakers"):
            path = os.path.join(data_dir, f"{eid}.json")
            with open(path, "w") as f:
                json.dump(snap, f)
            saved += 1
            print(f"  [save] {snap.get('home_team')} vs {snap.get('away_team')}")
    print(f"[pull] saved {saved} event snapshots to {data_dir}")


# ── Main comparison ───────────────────────────────────────────────────────────
def run(data_dir: str, min_edge: float, odds_format: str, market_filter: str | None):
    files = sorted(glob.glob(os.path.join(data_dir, "*.json")))
    if not files:
        sys.exit(f"No event JSON files in {data_dir}. Capture snapshots or use --pull-historical.")

    rows = []           # (title, player, market, edge_old, edge_new, pin_yes, cons_yes, soft_book)
    parse_skips = 0
    for fp in files:
        try:
            ev = json.load(open(fp))
        except Exception as e:
            print(f"[warn] bad json {fp}: {e}")
            continue
        if isinstance(ev, dict) and "data" in ev and "bookmakers" not in ev:
            ev = ev["data"]  # tolerate raw historical wrapper
        title, bd = parse_event(ev, odds_format)
        for (player, mkey), books in bd.items():
            if market_filter and market_filter not in mkey:
                continue
            pin = old_sharp_yes(books)
            cons = new_sharp_yes(books)
            soft_old = best_soft_yes(books, devig_proportional_yes)
            soft_new = best_soft_yes(books, devig_shin_yes)
            if pin is None or cons is None or soft_old is None or soft_new is None:
                parse_skips += 1
                continue
            edge_old = (pin - soft_old[1]) * 100.0
            edge_new = (cons - soft_new[1]) * 100.0
            rows.append((title, player, mkey, edge_old, edge_new, pin, cons, soft_new[0]))

    # ── classify ──
    flagged_old = [r for r in rows if r[3] >= min_edge]
    survived = [r for r in flagged_old if r[4] >= min_edge]
    evaporated = [r for r in flagged_old if r[4] < min_edge]
    new_only = [r for r in rows if r[4] >= min_edge and r[3] < min_edge]
    surv_rate = (len(survived) / len(flagged_old)) if flagged_old else 0.0

    def short(m):
        return "CARD" if "card" in m else ("SCORER" if "scorer" in m else m[:10])

    print("\n" + "═" * 74)
    print(f"  PHASE 0 — PROP EDGE FALSIFICATION   (de-vig source: {_SRC})")
    print(f"  events={len(files)}  candidate props={len(rows)}  threshold=±{min_edge:.1f}pp")
    print("═" * 74)
    print(f"  Edges flagged by OLD method (Pinnacle + proportional):  {len(flagged_old)}")
    print(f"  …still ≥{min_edge:.0f}pp under NEW (consensus + Shin):    {len(survived)}")
    print(f"  …EVAPORATED (method artifacts):                         {len(evaporated)}")
    print(f"  SURVIVAL RATE:                                          {surv_rate:5.1%}")
    print(f"  (New-method-only edges OLD missed: {len(new_only)};  unparseable/incomplete: {parse_skips})")

    # per-market
    if flagged_old:
        print("\n  By market:")
        bym = defaultdict(lambda: [0, 0])
        for r in flagged_old:
            bym[r[2]][0] += 1
            if r[4] >= min_edge:
                bym[r[2]][1] += 1
        for m, (f_, s_) in sorted(bym.items()):
            print(f"    {short(m):8} flagged={f_:3}  survived={s_:3}  ({(s_/f_ if f_ else 0):5.1%})")

    # evaporated detail
    if evaporated:
        print("\n  ── Evaporated edges (OLD said edge, NEW says none) ──")
        print(f"  {'match':28} {'player':20} {'mkt':6} {'old':>6} {'new':>6} {'pin→cons':>12}")
        for r in sorted(evaporated, key=lambda x: x[3] - x[4], reverse=True)[:25]:
            title, player, mkey, eo, en, pin, cons, _ = r
            print(f"  {title[:28]:28} {player[:20]:20} {short(mkey):6} "
                  f"{eo:+6.1f} {en:+6.1f} {pin:5.1%}→{cons:5.1%}")

    # verdict
    print("\n" + "═" * 74)
    if not flagged_old:
        verdict = "NO EDGES FLAGGED — widen data or lower --min-edge to test the method."
    elif surv_rate < 0.50:
        verdict = (f"PREMISE WEAK — {1-surv_rate:.0%} of edges are method artifacts. "
                   "STOP and rethink the anchor/de-vig before building (spec §9 Phase 0).")
    elif surv_rate < 0.80:
        verdict = (f"MIXED — {surv_rate:.0%} survive. Real core exists but proportional/Pinnacle "
                   "inflates count. Build on the consensus+Shin path only.")
    else:
        verdict = (f"ROBUST — {surv_rate:.0%} survive. Edges are not just method artifacts. "
                   "Proceed to build (still validate on CLV, not W/L).")
    print(f"  VERDICT: {verdict}")
    print("═" * 74 + "\n")
    return surv_rate


def main():
    ap = argparse.ArgumentParser(description="Phase 0 prop-edge falsification (spec §9).")
    ap.add_argument("--data-dir", default="./phase0_data")
    ap.add_argument("--min-edge", type=float, default=5.0, help="edge threshold in pp")
    ap.add_argument("--odds-format", choices=["auto", "american", "decimal"], default="auto")
    ap.add_argument("--market", default=None, help="substring filter, e.g. 'card' or 'scorer'")
    ap.add_argument("--pull-historical", action="store_true",
                    help="fetch past snapshots first (ODDS_API_KEY; 10x credit cost)")
    ap.add_argument("--sport", default="soccer_fifa_world_cup")
    ap.add_argument("--date", default=None, help="ISO UTC snapshot time for --pull-historical")
    ap.add_argument("--markets", default="player_goal_scorer_anytime,player_to_receive_card")
    args = ap.parse_args()

    if args.pull_historical:
        if not args.date:
            sys.exit("--pull-historical requires --date (ISO UTC, e.g. 2026-06-12T18:00:00Z)")
        pull_historical(args.sport, args.date, args.data_dir, args.markets)

    run(args.data_dir, args.min_edge, args.odds_format, args.market)


if __name__ == "__main__":
    main()
