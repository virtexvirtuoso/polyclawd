#!/usr/bin/env python3
"""
backtest_stop_curves.py — Read-only stop-loss curve backtest.

Replays logged price trajectories from `position_price_log` against
candidate stop-loss curves and reports PnL delta vs actual.

No writes, no live API calls. Ingests from a local copy of shadow_trades.db.

USAGE
-----
    # 1) Grab a fresh snapshot from the VPS (read-only)
    rsync -av vps:/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db \\
          /tmp/shadow_trades.db

    # 2) Run the backtest
    python scripts/backtest_stop_curves.py --db /tmp/shadow_trades.db

    # Extras:
    #   --archetype weather         restrict to one archetype
    #   --since 2026-03-20          drop positions opened before a date
    #   --csv /tmp/stops.csv        write per-position results
    #   --verbose                   dump per-curve per-position lines

CURVE SEMANTICS
---------------
A "curve" is a time-to-resolution → max-loss-pct mapping. At each snapshot
we compute:
    hours_to_resolve  = (closed_at - snapshot_ts) / 3600
    hours_past_lock   = (snapshot_ts - info_lock_at) / 3600
    loss_pct_now      = abs(unrealized_pnl) / bet_size  (if unrealized < 0)

and match the current regime (`normal`, `urgent`, `post_lock`) to pull the
threshold. The first snapshot that breaches the threshold exits the trade
at that snapshot's price. Otherwise the trade resolves at its actual
`exit_price` (so winners get to keep their wins).

KNOWN LIMITATIONS
-----------------
* Positions that were *actually* stopped by the live system don't have a
  terminal outcome; we score them at their actual exit price regardless of
  new curve (i.e. we don't pretend to know the counterfactual). These rows
  are reported separately under "held-stopped".
* `info_lock_at` is approximated as `closed_at - info_lock_before_close_h`
  per archetype. Real station-timezone lookup comes next iteration.
* Price snapshots are 5-min cadence — a stop that should trigger mid-
  interval will fire at the next snapshot (conservative, matches prod).
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


# ─── Archetype → info-lock window (hours before closed_at) ─────────────────
# Research-backed seed values; tune with the backtest itself.
# weather = 3h is a rough approximation for "21:00 local high lock on US
# stations"; will be refined per-station in resolution_windows().
ARCHETYPE_INFO_LOCK_H = {
    "weather": 3.0,            # ~21:00 station-local lock before 00:00 UTC+off
    "financial_price": 0.25,   # 15 min before market close
    "daily_updown": 0.25,
    "price_above": 0.50,
    "game_total": 0.10,        # final whistle
    "sports_single_game": 0.10,
    "deadline_binary": 0.0,    # resolution = lock, no window
    "geopolitical": 0.0,
    "election": 0.0,
    "entertainment": 0.0,
    "ai_model": 0.0,
    "social_count": 0.0,
}


# ─── Curve types ───────────────────────────────────────────────────────────
@dataclass
class StopRegime:
    """A single regime's threshold (e.g. 'normal', 'urgent', 'post_lock')."""
    max_loss_pct: float        # 0.50 = exit if loss ≥ 50% of bet
    edge_floor: float = -0.02  # reserved (unused in pure-price backtest)


@dataclass
class StopCurve:
    """A named set of regime thresholds keyed by time-to-resolution."""
    name: str
    regimes: dict[str, StopRegime]          # name -> regime
    # urgency_hours[(archetype | '_default')] -> hours_before_resolve
    urgency_hours: dict[str, float] = field(default_factory=lambda: {"_default": 6.0})

    def regime_for(self, archetype: str, hours_to_resolve: float, hours_past_lock: float) -> StopRegime:
        if hours_past_lock >= 0 and "post_lock" in self.regimes:
            return self.regimes["post_lock"]
        urgent_h = self.urgency_hours.get(archetype, self.urgency_hours["_default"])
        if hours_to_resolve <= urgent_h and "urgent" in self.regimes:
            return self.regimes["urgent"]
        return self.regimes["normal"]


# ─── Candidate curves ──────────────────────────────────────────────────────
def _candidate_curves() -> list[StopCurve]:
    weather_normal = StopRegime(max_loss_pct=0.30)  # matches current prod
    default_normal = StopRegime(max_loss_pct=0.50)

    return [
        StopCurve(
            name="no_stops",
            regimes={"normal": StopRegime(max_loss_pct=10.0)},  # effectively disabled
        ),
        StopCurve(
            name="prod_flat",   # current prod behaviour, no time awareness
            regimes={"normal": default_normal},
        ),
        StopCurve(
            name="prod_flat_weather_30",  # weather tier of current prod
            regimes={"normal": weather_normal},
        ),
        StopCurve(
            name="prod_urgent",  # current prod + 6h urgent tier
            regimes={
                "normal":  default_normal,
                "urgent":  StopRegime(max_loss_pct=0.30),
            },
            urgency_hours={"_default": 6.0, "weather": 6.0},
        ),
        StopCurve(
            name="decay_mild",
            regimes={
                "normal":    StopRegime(max_loss_pct=0.50),
                "urgent":    StopRegime(max_loss_pct=0.25),
                "post_lock": StopRegime(max_loss_pct=0.10),
            },
            urgency_hours={"_default": 6.0, "weather": 6.0},
        ),
        StopCurve(
            name="decay_aggressive",
            regimes={
                "normal":    StopRegime(max_loss_pct=0.40),
                "urgent":    StopRegime(max_loss_pct=0.20),
                "post_lock": StopRegime(max_loss_pct=0.08),
            },
            urgency_hours={"_default": 6.0, "weather": 6.0},
        ),
        StopCurve(
            name="lock_aware_mild",
            regimes={
                "normal":    StopRegime(max_loss_pct=0.50),
                "post_lock": StopRegime(max_loss_pct=0.15),
            },
        ),
        StopCurve(
            name="lock_aware_aggressive",
            regimes={
                "normal":    StopRegime(max_loss_pct=0.40),
                "post_lock": StopRegime(max_loss_pct=0.08),
            },
        ),
    ]


# ─── Data model ────────────────────────────────────────────────────────────
@dataclass
class Snapshot:
    ts: datetime
    price: float


@dataclass
class Position:
    id: int
    archetype: str
    status: str
    side: str
    entry_price: float
    exit_price: float | None
    bet_size: float
    actual_pnl: float
    opened_at: datetime
    closed_at: datetime | None
    market_title: str
    strategy: str
    snapshots: list[Snapshot]

    @property
    def info_lock_at(self) -> datetime | None:
        if self.closed_at is None:
            return None
        hours = ARCHETYPE_INFO_LOCK_H.get(self.archetype, 0.0)
        if hours <= 0:
            return self.closed_at
        return self.closed_at - _hours(hours)


def _hours(h: float):
    from datetime import timedelta
    return timedelta(hours=h)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ─── Load ──────────────────────────────────────────────────────────────────
def load_positions(db_path: Path, archetype: str | None, since: datetime | None) -> list[Position]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT id, archetype, status, side, entry_price, exit_price, bet_size,
               pnl, opened_at, closed_at, market_title, strategy
        FROM paper_positions
        WHERE status IN ('won','lost','stopped','closed_manual')
        ORDER BY opened_at ASC
        """
    ).fetchall()

    positions: list[Position] = []
    for r in rows:
        arc = r["archetype"] or "other"
        if archetype and arc != archetype:
            continue
        opened = _parse_dt(r["opened_at"])
        closed = _parse_dt(r["closed_at"])
        if opened is None or closed is None:
            continue
        if since and opened < since:
            continue

        log_rows = conn.execute(
            "SELECT timestamp, market_price FROM position_price_log "
            "WHERE position_id = ? ORDER BY timestamp ASC",
            (r["id"],),
        ).fetchall()
        snaps: list[Snapshot] = []
        for lr in log_rows:
            ts = _parse_dt(lr["timestamp"])
            if ts is None or lr["market_price"] is None:
                continue
            snaps.append(Snapshot(ts=ts, price=float(lr["market_price"])))

        positions.append(Position(
            id=int(r["id"]),
            archetype=arc,
            status=r["status"],
            side=r["side"],
            entry_price=float(r["entry_price"]),
            exit_price=float(r["exit_price"]) if r["exit_price"] is not None else None,
            bet_size=float(r["bet_size"]),
            actual_pnl=float(r["pnl"]) if r["pnl"] is not None else 0.0,
            opened_at=opened,
            closed_at=closed,
            market_title=r["market_title"] or "",
            strategy=r["strategy"] or "",
            snapshots=snaps,
        ))

    conn.close()
    return positions


# ─── PnL math ──────────────────────────────────────────────────────────────
def unrealized_pnl(side: str, entry: float, current: float, bet: float) -> float:
    if side == "YES":
        return bet * (current / entry - 1.0) if entry > 0 else 0.0
    no_entry = 1.0 - entry
    no_current = 1.0 - current
    return bet * (no_current / no_entry - 1.0) if no_entry > 0 else 0.0


# ─── Simulate a single position under a curve ──────────────────────────────
@dataclass
class SimResult:
    position_id: int
    archetype: str
    status: str                # new status under curve
    exit_price: float
    exit_ts: datetime | None
    pnl: float
    stopped_early: bool
    held_stopped: bool         # actual was 'stopped', so counterfactual is unknown


def simulate(pos: Position, curve: StopCurve) -> SimResult:
    # Held-stopped case: we can't know the counterfactual, so score at actual.
    if pos.status == "stopped":
        return SimResult(
            position_id=pos.id,
            archetype=pos.archetype,
            status="stopped_actual",
            exit_price=pos.exit_price or pos.entry_price,
            exit_ts=pos.closed_at,
            pnl=pos.actual_pnl,
            stopped_early=False,
            held_stopped=True,
        )

    lock_at = pos.info_lock_at
    closed = pos.closed_at

    for snap in pos.snapshots:
        if closed is None:
            break
        hours_to_resolve = (closed - snap.ts).total_seconds() / 3600.0
        hours_past_lock = ((snap.ts - lock_at).total_seconds() / 3600.0) if lock_at else -1e9
        regime = curve.regime_for(pos.archetype, hours_to_resolve, hours_past_lock)

        pnl_now = unrealized_pnl(pos.side, pos.entry_price, snap.price, pos.bet_size)
        if pnl_now >= 0:
            continue
        loss_pct = abs(pnl_now) / pos.bet_size if pos.bet_size > 0 else 0.0
        if loss_pct >= regime.max_loss_pct:
            return SimResult(
                position_id=pos.id,
                archetype=pos.archetype,
                status="stopped_sim",
                exit_price=snap.price,
                exit_ts=snap.ts,
                pnl=round(pnl_now, 2),
                stopped_early=True,
                held_stopped=False,
            )

    # Never triggered — use actual resolution outcome.
    return SimResult(
        position_id=pos.id,
        archetype=pos.archetype,
        status=f"held_{pos.status}",
        exit_price=pos.exit_price or 0.0,
        exit_ts=pos.closed_at,
        pnl=pos.actual_pnl,
        stopped_early=False,
        held_stopped=False,
    )


# ─── Aggregate reporting ───────────────────────────────────────────────────
@dataclass
class CurveReport:
    curve: str
    n_total: int
    n_scored: int           # excludes held_stopped (unknown counterfactual)
    n_stopped_sim: int
    n_held_wins: int
    n_held_losses: int
    total_pnl: float
    pnl_scored: float       # excluding held_stopped
    actual_pnl_scored: float
    delta_vs_actual: float


def report(curve: StopCurve, positions: list[Position]) -> tuple[CurveReport, list[SimResult]]:
    results = [simulate(p, curve) for p in positions]

    total_pnl = sum(r.pnl for r in results)
    scored = [r for r in results if not r.held_stopped]
    pnl_scored = sum(r.pnl for r in scored)

    # Actual PnL for the same "scored" set (i.e. excluding held_stopped rows)
    scored_ids = {r.position_id for r in scored}
    actual_scored = sum(p.actual_pnl for p in positions if p.id in scored_ids)

    n_stopped_sim = sum(1 for r in scored if r.stopped_early)
    n_held_wins = sum(1 for r in scored if r.status == "held_won")
    n_held_losses = sum(1 for r in scored if r.status == "held_lost")

    return CurveReport(
        curve=curve.name,
        n_total=len(positions),
        n_scored=len(scored),
        n_stopped_sim=n_stopped_sim,
        n_held_wins=n_held_wins,
        n_held_losses=n_held_losses,
        total_pnl=round(total_pnl, 2),
        pnl_scored=round(pnl_scored, 2),
        actual_pnl_scored=round(actual_scored, 2),
        delta_vs_actual=round(pnl_scored - actual_scored, 2),
    ), results


# ─── CLI ───────────────────────────────────────────────────────────────────
def _fmt_money(x: float) -> str:
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(x):,.2f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=Path("storage/shadow_trades.db"),
                    help="Path to a local copy of shadow_trades.db (read-only).")
    ap.add_argument("--archetype", type=str, default=None,
                    help="Restrict to a single archetype (e.g. weather).")
    ap.add_argument("--since", type=str, default=None,
                    help="Drop positions opened before this ISO date (e.g. 2026-03-20).")
    ap.add_argument("--csv", type=Path, default=None,
                    help="Write per-position per-curve results to this CSV.")
    ap.add_argument("--verbose", action="store_true",
                    help="Print per-position debug lines.")
    ap.add_argument("--sweep", action="store_true",
                    help="Grid-sweep (post_lock_pct × info_lock_hours) for weather.")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"ERROR: DB not found at {args.db}", file=sys.stderr)
        print("Hint: rsync -av vps:/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db /tmp/shadow_trades.db", file=sys.stderr)
        sys.exit(2)

    since_dt = None
    if args.since:
        try:
            since_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"ERROR: --since must be ISO 8601 (e.g. 2026-03-20)", file=sys.stderr)
            sys.exit(2)

    positions = load_positions(args.db, args.archetype, since_dt)
    if not positions:
        print("No positions matched filters.")
        return

    # Quick inventory
    from collections import Counter
    arc_counts = Counter(p.archetype for p in positions)
    status_counts = Counter(p.status for p in positions)
    snap_total = sum(len(p.snapshots) for p in positions)
    n_with_snaps = sum(1 for p in positions if p.snapshots)

    print("=" * 78)
    print("STOP-LOSS CURVE BACKTEST")
    print("=" * 78)
    print(f"DB:           {args.db}")
    print(f"Positions:    {len(positions)} total  |  {n_with_snaps} with price-log trajectory")
    print(f"Snapshots:    {snap_total:,} rows")
    print(f"By archetype: " + ", ".join(f"{k}={v}" for k, v in arc_counts.most_common()))
    print(f"By status:    " + ", ".join(f"{k}={v}" for k, v in status_counts.most_common()))
    if since_dt:
        print(f"Filter:       opened >= {since_dt.date()}")
    if args.archetype:
        print(f"Filter:       archetype == {args.archetype}")
    print()

    curves = _candidate_curves()
    reports: list[CurveReport] = []
    all_results: list[tuple[str, SimResult]] = []

    for curve in curves:
        rep, results = report(curve, positions)
        reports.append(rep)
        for r in results:
            all_results.append((curve.name, r))

    # Header
    print(f"{'curve':<28} {'scored':>7} {'stops':>6} {'wins':>6} {'losses':>7}  "
          f"{'actual_pnl':>14} {'sim_pnl':>14} {'Δ':>14}")
    print("-" * 108)
    for rep in reports:
        delta_str = _fmt_money(rep.delta_vs_actual)
        actual_str = _fmt_money(rep.actual_pnl_scored)
        sim_str = _fmt_money(rep.pnl_scored)
        print(f"{rep.curve:<28} {rep.n_scored:>7} {rep.n_stopped_sim:>6} "
              f"{rep.n_held_wins:>6} {rep.n_held_losses:>7}  "
              f"{actual_str:>14} {sim_str:>14} {delta_str:>14}")

    print()
    print("Notes:")
    print(" * `scored` excludes positions actually stopped live — counterfactual unknown.")
    print(" * `Δ` is sim_pnl − actual_pnl over the scored set (positive = curve wins).")
    print(" * `post_lock` regime uses ARCHETYPE_INFO_LOCK_H seed values (see top of file).")

    # ─── Winner drawdown profile ─────────────────────────────────────────
    print("\n" + "=" * 78)
    print("WINNER DRAWDOWN PROFILE")
    print("=" * 78)
    print("For positions that actually won: the *deepest* loss_pct reached at any point")
    print("during the trajectory. A stop tighter than this value would have cut the win.\n")

    from statistics import median

    winners = [p for p in positions if p.status == "won"]
    winners_by_arc: dict[str, list[tuple[int, float, float, float]]] = {}  # arc → list[(pid, max_dd, max_dd_normal, max_dd_post_lock)]
    for p in winners:
        if not p.snapshots or p.closed_at is None:
            continue
        max_dd_total = 0.0
        max_dd_normal = 0.0  # excludes post-lock window
        max_dd_post = 0.0
        lock_at = p.info_lock_at
        for s in p.snapshots:
            pnl = unrealized_pnl(p.side, p.entry_price, s.price, p.bet_size)
            if pnl >= 0:
                continue
            loss_pct = abs(pnl) / p.bet_size
            if loss_pct > max_dd_total:
                max_dd_total = loss_pct
            in_post_lock = lock_at is not None and s.ts >= lock_at
            if in_post_lock:
                if loss_pct > max_dd_post:
                    max_dd_post = loss_pct
            else:
                if loss_pct > max_dd_normal:
                    max_dd_normal = loss_pct
        winners_by_arc.setdefault(p.archetype, []).append(
            (p.id, max_dd_total, max_dd_normal, max_dd_post)
        )

    def _pct(xs: list[float], q: float) -> float:
        if not xs:
            return 0.0
        xs_sorted = sorted(xs)
        idx = min(int(q * (len(xs_sorted) - 1)), len(xs_sorted) - 1)
        return xs_sorted[idx]

    print(f"{'archetype':<20} {'n':>4} {'region':<10} {'min':>6} {'p50':>6} {'p75':>6} {'p90':>6} {'p95':>6} {'max':>6}")
    print("-" * 78)
    for arc, rows in winners_by_arc.items():
        total_dds = [r[1] for r in rows]
        normal_dds = [r[2] for r in rows]
        post_dds = [r[3] for r in rows]
        for region, xs in (("total", total_dds), ("normal", normal_dds), ("post_lock", post_dds)):
            if not any(xs):
                continue
            print(f"{arc:<20} {len(xs):>4} {region:<10} "
                  f"{min(xs):>6.1%} {_pct(xs, 0.50):>6.1%} {_pct(xs, 0.75):>6.1%} "
                  f"{_pct(xs, 0.90):>6.1%} {_pct(xs, 0.95):>6.1%} {max(xs):>6.1%}")
    print()
    print("Reading:")
    print(" * Use p90/p95 as the LOWER bound for a safe stop — tighter cuts ~5-10% of winners.")
    print(" * `post_lock` region is from info_lock_at → closed_at (ARCHETYPE_INFO_LOCK_H).")
    print(" * If post_lock dd ≪ normal dd, a lock-aware curve is safe.")

    # Show top-5 deepest winner drawdowns for visibility
    all_winner_rows = [(arc, *row) for arc, rows in winners_by_arc.items() for row in rows]
    all_winner_rows.sort(key=lambda r: -r[2])  # by max_dd_total desc
    print("\nTop-5 deepest winner drawdowns (the positions any tight curve would kill):")
    for arc, pid, total_dd, normal_dd, post_dd in all_winner_rows[:5]:
        print(f"    pos={pid:<4} arc={arc:<20} max_dd={total_dd:.1%}  "
              f"normal={normal_dd:.1%}  post_lock={post_dd:.1%}")

    # ─── Stopped-position replay ─────────────────────────────────────────
    print("\n" + "=" * 78)
    print("STOPPED-POSITION REPLAY")
    print("=" * 78)
    print("For positions the live system actually stopped: would a different curve")
    print("have fired EARLIER (and at what price)? Only TIGHTER curves are scored —")
    print("looser curves have unknown counterfactual (market state after stop unknown).\n")

    stopped_positions = [p for p in positions if p.status == "stopped" and p.snapshots]
    prod_loose = StopCurve(
        name="_prod",
        regimes={"normal": StopRegime(max_loss_pct=0.50), "urgent": StopRegime(max_loss_pct=0.30)},
        urgency_hours={"_default": 6.0, "weather": 6.0},
    )
    prod_weather = StopCurve(
        name="_prod_weather",
        regimes={"normal": StopRegime(max_loss_pct=0.30), "urgent": StopRegime(max_loss_pct=0.15)},
        urgency_hours={"_default": 6.0, "weather": 6.0},
    )
    # Pick prod-equivalent per position archetype
    def _prod_for(arc: str) -> StopCurve:
        return prod_weather if arc == "weather" else prod_loose

    print(f"{'curve':<28} {'n_fires_earlier':>16} {'avg_savings':>14} {'total_savings':>16}")
    print("-" * 78)
    for curve in curves:
        if curve.name in ("no_stops", "prod_flat", "prod_flat_weather_30", "prod_urgent"):
            continue
        fires_earlier = 0
        total_savings = 0.0
        for p in stopped_positions:
            sim = simulate(Position(**{**p.__dict__, "status": "lost"}), curve)
            # simulate returns stopped_sim if curve fires, else held_lost using actual exit
            if not sim.stopped_early:
                continue
            actual_exit = p.exit_price or p.entry_price
            if p.side == "NO" and sim.exit_price < actual_exit:
                fires_earlier += 1
                # Savings: live stopped at actual_exit, sim would have stopped at sim.exit_price.
                # For NO bet, lower YES price = better (smaller loss).
                savings = unrealized_pnl(p.side, p.entry_price, sim.exit_price, p.bet_size) - p.actual_pnl
                total_savings += savings
            elif p.side == "YES" and sim.exit_price > actual_exit:
                fires_earlier += 1
                savings = unrealized_pnl(p.side, p.entry_price, sim.exit_price, p.bet_size) - p.actual_pnl
                total_savings += savings
        avg = total_savings / fires_earlier if fires_earlier else 0.0
        print(f"{curve.name:<28} {fires_earlier:>16} {_fmt_money(avg):>14} {_fmt_money(total_savings):>16}")

    print("\nNote: `savings` = (sim exit price PnL) − (actual stopped PnL). Positive = better.")

    if args.verbose:
        print("\nPer-position deltas (non-zero only, first 40):")
        baseline = {r.position_id: r for cname, r in all_results if cname == "no_stops"}
        for curve_name in [c.name for c in curves if c.name != "no_stops"]:
            sub = [r for cname, r in all_results if cname == curve_name and r.stopped_early]
            if not sub:
                continue
            print(f"\n  [{curve_name}] stopped {len(sub)} positions:")
            for r in sub[:40]:
                bl = baseline.get(r.position_id)
                bl_pnl = bl.pnl if bl else 0.0
                delta = r.pnl - bl_pnl
                print(f"    pos={r.position_id:<4} arc={r.archetype:<18} "
                      f"exit={r.exit_price:.3f}  sim={r.pnl:+.2f}  "
                      f"baseline={bl_pnl:+.2f}  Δ={delta:+.2f}")

    # ─── Sensitivity sweep ───────────────────────────────────────────────
    if args.sweep:
        print("\n" + "=" * 78)
        print("SWEEP: (post_lock_pct × info_lock_hours) for weather")
        print("=" * 78)
        print("Fixes normal=50%. Scores winners (held) + stopped-replay savings, naive sum.\n")

        sweep_pcts = [0.08, 0.10, 0.12, 0.15, 0.18, 0.20, 0.25]
        sweep_hours = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]

        print(f"{'hours \\ pct':<12} " + "  ".join(f"{p*100:>5.0f}%" for p in sweep_pcts))
        print("-" * 78)
        weather_positions = [p for p in positions if p.archetype == "weather"]
        for h in sweep_hours:
            row = [f"{h:>4.1f}h      "]
            for pct in sweep_pcts:
                # Temporarily patch the lock window
                ARCHETYPE_INFO_LOCK_H["weather"] = h
                curve = StopCurve(
                    name=f"sweep_{h}h_{int(pct*100)}",
                    regimes={
                        "normal":    StopRegime(max_loss_pct=0.50),
                        "post_lock": StopRegime(max_loss_pct=pct),
                    },
                )
                # Held: winners/losses damage
                held = [p for p in weather_positions if p.status in ("won","lost") and p.snapshots]
                held_delta = 0.0
                for p in held:
                    sim = simulate(p, curve)
                    held_delta += sim.pnl - p.actual_pnl
                # Stopped: savings
                stopped = [p for p in weather_positions if p.status == "stopped" and p.snapshots]
                stopped_gain = 0.0
                for p in stopped:
                    sim = simulate(Position(**{**p.__dict__, "status": "lost"}), curve)
                    if not sim.stopped_early:
                        continue
                    actual_exit = p.exit_price or p.entry_price
                    if (p.side == "NO" and sim.exit_price < actual_exit) or \
                       (p.side == "YES" and sim.exit_price > actual_exit):
                        savings = unrealized_pnl(p.side, p.entry_price, sim.exit_price, p.bet_size) - p.actual_pnl
                        stopped_gain += savings
                net = held_delta + stopped_gain
                row.append(f"{'+' if net >= 0 else ''}${net:>5.0f}")
            print(" ".join(row))
        # Restore default
        ARCHETYPE_INFO_LOCK_H["weather"] = 3.0
        print("\nPick the cell with max net $. Watch for held_delta wins (good) vs damage (bad).")

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["curve", "position_id", "archetype", "status", "exit_price",
                        "exit_ts", "pnl", "stopped_early", "held_stopped"])
            for cname, r in all_results:
                w.writerow([cname, r.position_id, r.archetype, r.status, r.exit_price,
                            r.exit_ts.isoformat() if r.exit_ts else "",
                            r.pnl, int(r.stopped_early), int(r.held_stopped)])
        print(f"\nWrote {len(all_results):,} rows to {args.csv}")


if __name__ == "__main__":
    main()
