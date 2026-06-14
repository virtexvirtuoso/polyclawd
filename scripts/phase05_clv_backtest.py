#!/usr/bin/env python3
"""
phase05_clv_backtest.py — Phase 0.5 CLV gate for the Player Prop Edge System.

Throwaway validation. Referenced by:
  02-Projects/Polyclawd/Development/prop-edge-system-spec.md §9 (Phase 0.5)

QUESTION IT ANSWERS
-------------------
Phase 0 (phase0_prop_falsification.py) proved the spec's single-Pinnacle anchor
manufactures ~half its edges. The ~46% that survive a Betfair-weighted consensus
anchor are better candidates — but surviving a better PRICE is not the same as
having predictive EDGE.

This script asks the only question that matters before building anything:
  Do the consensus-anchored survivor edges BEAT THE CLOSING LINE?

For each prop flagged at ENTRY (~3-6h pre-kickoff), it compares the price you'd
have bet (best soft book, raw, vig included) against the CLOSING sharp consensus
(~5 min pre-kickoff). Positive CLV = the sharp market moved toward your side =
you got a good number. CLV — not realized W/L — is the luck-free validator
(spec §9.5); on small samples it is the ONLY meaningful signal.

  Beat-rate >> 50% + positive mean CLV -> real edge -> proceed to build.
  Beat-rate ~ 50%                      -> no edge   -> the prop thesis is dead.
  Beat-rate << 50%                     -> anti-edge -> you are the closing-line sucker.

DATA
----
ENTRY snapshots: reuse the Phase 0 pull (--entry-dir, default /tmp/phase0_data).
CLOSE snapshots: --pull-close fetches one per event at commence_time - N min
                 (--close-offset-min, default 5) into --close-dir. 10x credits.

USAGE
-----
  # pull closing snapshots for every event already in the entry dir, then score
  python3 scripts/phase05_clv_backtest.py --pull-close \
      --entry-dir /tmp/phase0_data --close-dir /tmp/phase0_close --min-edge 5

  # score only (snapshots already on disk)
  python3 scripts/phase05_clv_backtest.py \
      --entry-dir /tmp/phase0_data --close-dir /tmp/phase0_close --min-edge 5
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import unicodedata
import urllib.parse
from collections import defaultdict
from datetime import timedelta

# reuse the committed Phase 0 helpers (consensus anchor, parsing, fetch)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase0 as P  # noqa: E402


def canon(name: str) -> str:
    """Spec §1.7 _canonical_player — match the same player across two snapshots/books."""
    name = unicodedata.normalize("NFKD", name or "")
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower().strip()
    name = re.sub(r"[^\w\s]", "", name)
    parts = name.split()
    if len(parts) == 2 and len(parts[0]) == 1:
        return parts[1]
    return name


def best_soft_raw(books: dict):
    """Best (lowest-implied = highest-odds) soft-book YES, RAW (vig included).
    This is the actual price you'd bet — no de-vig haircut (none possible on
    YES-only data anyway). Returns (book, raw_implied) or None."""
    best = None
    for bkey, sides in books.items():
        if bkey not in P.SOFT_BOOKS:
            continue
        y = sides.get("yes")
        if y is None:
            continue
        if best is None or y < best[1]:
            best = (bkey, y)
    return best


def event_props(path: str, odds_format: str):
    """Return (title, {(canon_player, market): (cons_yes, soft_raw, raw_player)})."""
    ev = json.load(open(path))
    if isinstance(ev, dict) and "data" in ev and "bookmakers" not in ev:
        ev = ev["data"]
    title, bd = P.parse_event(ev, odds_format)
    out = {}
    for (player, mkt), books in bd.items():
        cons = P.new_sharp_yes(books)  # Betfair-weighted consensus (raw YES)
        soft = best_soft_raw(books)  # actual entry price (raw YES)
        out[(canon(player), mkt)] = (cons, soft, player)
    return title, out


# ── Closing-snapshot puller ───────────────────────────────────────────────────
def pull_close(entry_dir, close_dir, sport, markets, offset_min):
    key = os.getenv("ODDS_API_KEY")
    if not key:
        sys.exit("ODDS_API_KEY not set — cannot pull closing snapshots.")
    books = ",".join(list(P.SOCCER_PROP_SHARP_WEIGHTS) + sorted(P.SOFT_BOOKS))
    os.makedirs(close_dir, exist_ok=True)
    saved = 0
    for ef in sorted(glob.glob(os.path.join(entry_dir, "*.json"))):
        ev = json.load(open(ef))
        if isinstance(ev, dict) and "data" in ev and "bookmakers" not in ev:
            ev = ev["data"]
        eid = ev.get("id") or os.path.splitext(os.path.basename(ef))[0]
        ct = P._parse_iso(ev.get("commence_time"))
        if ct is None:
            print(f"  [skip] {eid}: no commence_time")
            continue
        if os.path.exists(os.path.join(close_dir, f"{eid}.json")):
            print(f"  [cached] {ev.get('home_team')} vs {ev.get('away_team')} — close already pulled")
            continue
        close_dt = ct - timedelta(minutes=offset_min)
        date = close_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        url = (
            f"{P.ODDS_API_BASE}/historical/sports/{sport}/events/{eid}/odds?"
            f"apiKey={key}&date={urllib.parse.quote(date)}"
            f"&markets={markets}&bookmakers={books}&oddsFormat=american"
        )
        try:
            snap = P._get(url).get("data", {})
        except Exception as e:
            print(f"  [skip] {eid}: {e}")
            continue
        if snap.get("bookmakers"):
            with open(os.path.join(close_dir, f"{eid}.json"), "w") as f:
                json.dump(snap, f)
            saved += 1
            print(f"  [save] close T-{offset_min}m  {snap.get('home_team')} vs {snap.get('away_team')}")
        else:
            print(f"  [empty] {ev.get('home_team')} vs {ev.get('away_team')} — no closing prop lines")
    print(f"[pull-close] saved {saved} closing snapshots to {close_dir}")


# ── CLV scoring ───────────────────────────────────────────────────────────────
def run(entry_dir, close_dir, min_edge, odds_format):
    entry_files = sorted(glob.glob(os.path.join(entry_dir, "*.json")))
    if not entry_files:
        sys.exit(f"No entry snapshots in {entry_dir}.")

    rows = []  # (title, player, mkt, entry_edge, clv_pp, soft_entry, cons_close, soft_move)
    line_gone = 0
    no_close_event = 0
    for ef in entry_files:
        eid = os.path.splitext(os.path.basename(ef))[0]
        cf = os.path.join(close_dir, f"{eid}.json")
        if not os.path.exists(cf):
            no_close_event += 1
            continue
        title, ep = event_props(ef, odds_format)
        _, cp = event_props(cf, odds_format)
        for key, (cons_e, soft_e, pname) in ep.items():
            if cons_e is None or soft_e is None:
                continue
            entry_edge = (cons_e - soft_e[1]) * 100.0
            if entry_edge < min_edge:  # only consensus-anchored SURVIVORS
                continue
            close = cp.get(key)
            if not close or close[0] is None:
                line_gone += 1  # bet flagged at entry, no sharp close to grade
                rows.append((title, pname, key[1], entry_edge, None, soft_e[1], None, None))
                continue
            cons_c, soft_c, _ = close
            clv_pp = (cons_c - soft_e[1]) * 100.0  # sharp close vs your entry price
            soft_move = (soft_c[1] - soft_e[1]) * 100.0 if soft_c else None
            rows.append((title, pname, key[1], entry_edge, clv_pp, soft_e[1], cons_c, soft_move))

    # PRIMARY (non-circular): did the SOFT line you'd actually bet move toward you by close?
    lm = [r for r in rows if r[7] is not None]
    lm_beat = [r for r in lm if r[7] > 0]
    lm_rate = (len(lm_beat) / len(lm)) if lm else 0.0
    lm_mean = (sum(r[7] for r in lm) / len(lm)) if lm else 0.0
    lm_med = sorted(r[7] for r in lm)[len(lm) // 2] if lm else 0.0

    # SECONDARY (circular): sharp consensus close vs entry price — diagnostic only.
    measured = [r for r in rows if r[4] is not None]
    beat = [r for r in measured if r[4] > 0]
    rate = (len(beat) / len(measured)) if measured else 0.0
    mean_clv = (sum(r[4] for r in measured) / len(measured)) if measured else 0.0
    mean_entry = (sum(r[3] for r in measured) / len(measured)) if measured else 0.0

    def short(m):
        return "CARD" if "card" in m else ("SCORER" if "scorer" in m else m[:8])

    print("\n" + "═" * 78)
    print("  PHASE 0.5 — CLV BACKTEST  (do consensus survivors beat the closing line?)")
    print(
        f"  entry snaps={len(entry_files)}  survivor edges≥{min_edge:.0f}pp={len(rows)}  "
        f"gradable (had sharp close)={len(measured)}"
    )
    print("═" * 78)
    print("  PRIMARY — soft line movement (NON-circular: did your bet's price shorten?)")
    print(f"    line-move beat-rate (soft_close>soft_entry):  {lm_rate:6.1%}  ({len(lm_beat)}/{len(lm)})")
    print(f"    mean soft move:   {lm_mean:+6.2f} pp     median: {lm_med:+6.2f} pp")
    print("  SECONDARY — vs sharp consensus close (CIRCULAR — same family as entry signal):")
    print(
        f"    consensus beat-rate: {rate:5.1%}   mean consensus-CLV: {mean_clv:+5.2f}pp"
        f"   vs mean entry edge: {mean_entry:+5.2f}pp"
    )
    if abs(mean_clv - mean_entry) < 1.5:
        print("    -> consensus-CLV ≈ entry edge: consensus barely moved entry→close, so it")
        print("       carries NO independent confirmation. Trust PRIMARY (soft movement).")

    if lm:
        print("\n  By market (PRIMARY soft line-move):")
        bym = defaultdict(lambda: [0, 0, 0.0])
        for r in lm:
            bym[r[2]][0] += 1
            if r[7] > 0:
                bym[r[2]][1] += 1
            bym[r[2]][2] += r[7]
        for m, (n, b, s) in sorted(bym.items()):
            print(f"    {short(m):7} n={n:3}  move-beat-rate={(b / n if n else 0):5.1%}  mean move={s / n:+5.2f}pp")

        print("\n  ── Top survivor edges by entry edge (soft entry → soft close) ──")
        print(f"  {'match':24} {'player':17} {'mkt':6} {'entry':>6} {'softMove':>8} {'soft→softClose':>16}")
        for r in sorted(lm, key=lambda x: x[3], reverse=True)[:20]:
            t, pl, mk, ee, clv, se, cc, sm = r
            mark = "✓" if sm > 0 else "✗"
            sc = se + sm / 100.0
            print(f"  {t[:24]:24} {pl[:17]:17} {short(mk):6} {ee:+6.1f} {sm:+6.1f}{mark} {se:5.1%}→{sc:5.1%}")

    # verdict — on the PRIMARY (non-circular) metric
    print("\n" + "═" * 78)
    conf = f"LOW CONFIDENCE (only {len(lm)} gradable) — widen sample. " if len(lm) < 20 else ""
    if not lm:
        verdict = "NO GRADABLE EDGES — soft line absent at entry or close."
    elif lm_rate >= 0.58 and lm_mean >= 1.0:
        verdict = (
            f"{conf}EDGE CONFIRMED — soft lines converge to your side "
            f"({lm_rate:.0%}, {lm_mean:+.1f}pp). Real CLV. Proceed to build (spec §9.7)."
        )
    elif lm_rate >= 0.52:
        verdict = (
            f"{conf}MARGINAL — weak soft convergence ({lm_rate:.0%}, {lm_mean:+.1f}pp). "
            "Expand sample before committing."
        )
    elif lm_rate >= 0.45:
        verdict = (
            f"{conf}NO EDGE — soft lines DON'T converge ({lm_rate:.0%}, {lm_mean:+.1f}pp). "
            "The soft-vs-sharp gap is a structural longshot-vig spread that never closes, "
            "NOT predictive alpha. Prop thesis dead as specced."
        )
    else:
        verdict = f"{conf}ANTI-EDGE — soft lines move AGAINST you ({lm_rate:.0%}, {lm_mean:+.1f}pp). Kill it."
    print(f"  VERDICT: {verdict}")
    print("  WHY PRIMARY: survivors were SELECTED on consensus_entry−soft_entry, so consensus-CLV")
    print("  is circular. Soft line MOVEMENT (your bet's price shortening) is the honest test.")
    print("═" * 78 + "\n")
    return lm_rate


def main():
    ap = argparse.ArgumentParser(description="Phase 0.5 CLV backtest (spec §9).")
    ap.add_argument("--entry-dir", default="/tmp/phase0_data")
    ap.add_argument("--close-dir", default="/tmp/phase0_close")
    ap.add_argument("--min-edge", type=float, default=5.0)
    ap.add_argument("--odds-format", choices=["auto", "american", "decimal"], default="auto")
    ap.add_argument(
        "--pull-close", action="store_true", help="fetch closing snapshots for every event in --entry-dir (10x credits)"
    )
    ap.add_argument("--sport", default="soccer_fifa_world_cup")
    ap.add_argument("--markets", default="player_goal_scorer_anytime,player_to_receive_card")
    ap.add_argument("--close-offset-min", type=int, default=5, help="minutes before kickoff for the closing snapshot")
    args = ap.parse_args()

    if args.pull_close:
        pull_close(args.entry_dir, args.close_dir, args.sport, args.markets, args.close_offset_min)

    run(args.entry_dir, args.close_dir, args.min_edge, args.odds_format)


if __name__ == "__main__":
    main()
