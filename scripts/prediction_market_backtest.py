#!/usr/bin/env python3
"""Prediction market backtest harness — replays the actual signal pipeline on historical data.

Produces the archetype x side x price_zone win rate table needed for
Phase 1 of the confidence redesign (see docs/CONFIDENCE_REDESIGN.md).

Usage:
    python scripts/prediction_market_backtest.py                          # Default: Kalshi NO-only
    python scripts/prediction_market_backtest.py --both-sides             # Test both YES and NO
    python scripts/prediction_market_backtest.py --no-kill-rules          # Disable kill rules
    python scripts/prediction_market_backtest.py --kalshi-only --no-plots # Fast smoke test
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "signals"))

import argparse
import json
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional

import duckdb
import numpy as np
import pandas as pd

# Import ACTUAL signal pipeline — not reimplemented
from mispriced_category_signal import (
    classify_archetype,
    _check_kill_rules,
    _is_subdaily_noise,
    calculate_signal_confidence,
    extract_category,
    MISPRICED_CATEGORIES,
    EFFICIENT_CATEGORIES,
    MIN_VOLUME_KALSHI,
    MIN_VOLUME_POLYMARKET,
    CONTESTED_LOW,
    CONTESTED_HIGH,
    MIN_ENTRY_PRICE,
    MAX_DAYS_TO_CLOSE,
    WHALE_VOLUME_KALSHI,
    WHALE_VOLUME_POLYMARKET,
)
from empirical_confidence import (
    price_zone,
    BECKER_NO_WIN_RATES,
    PRICE_ZONE_MODIFIERS,
    classify_duration,
)

logging.basicConfig(level=logging.WARNING, format="%(message)s")
log = logging.getLogger(__name__)


# ============================================================================
# Section 1: Configuration
# ============================================================================

@dataclass
class BacktestConfig:
    data_dir: Path = PROJECT_ROOT / "data"
    include_kalshi: bool = True
    include_polymarket: bool = True

    # Match production signal filters
    min_volume_kalshi: int = MIN_VOLUME_KALSHI
    min_volume_polymarket: int = MIN_VOLUME_POLYMARKET
    contested_low: int = CONTESTED_LOW
    contested_high: int = CONTESTED_HIGH
    min_entry_price: int = MIN_ENTRY_PRICE
    max_days_to_close: int = MAX_DAYS_TO_CLOSE

    no_only: bool = True
    apply_kill_rules: bool = True
    track_killed: bool = True

    # Sizing
    initial_bankroll: float = 10_000.0
    kelly_fraction: float = 0.25
    max_position_pct: float = 0.05
    max_position_dollar: float = 500.0  # Hard cap per trade (realistic liquidity)

    # Output
    output_dir: Path = PROJECT_ROOT / "output" / "backtest"
    export_csv: bool = True
    show_plots: bool = True
    save_plots: bool = True


# ============================================================================
# Section 2: Data Classes
# ============================================================================

@dataclass
class Trade:
    market_id: str
    platform: str
    title: str
    archetype: str
    side: str
    entry_price: float
    price_zone: str
    resolution: str
    won: bool
    pnl: float
    position_size: float
    dollar_pnl: float
    confidence: float
    category: str
    volume: int
    duration_days: float
    duration_bucket: str
    close_time: str
    kill_rule: str  # "" if traded, "K1: ..." if killed


@dataclass
class BacktestResult:
    config: BacktestConfig
    trades: List[Trade] = field(default_factory=list)
    killed_trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    poly_archetype_table: Optional[pd.DataFrame] = None

    total_pnl: float = 0.0
    win_rate: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    profit_factor: float = 0.0
    total_trades: int = 0


# ============================================================================
# Section 3: Data Loading (DuckDB)
# ============================================================================

def load_kalshi_markets(config: BacktestConfig) -> pd.DataFrame:
    """Load resolved Kalshi markets with entry prices from actual trades.

    Uses trades table to find the first trade in our entry window (55-92c YES)
    as entry price. Markets without a trade in our window are still included
    for population-level analysis but have price_cents=NaN.
    """
    markets_path = config.data_dir / "kalshi" / "markets" / "*.parquet"
    trades_path = config.data_dir / "kalshi" / "trades" / "*.parquet"
    con = duckdb.connect()
    try:
        df = con.execute(f"""
            WITH resolved AS (
                SELECT
                    ticker AS market_id,
                    event_ticker,
                    title,
                    CAST(volume AS INTEGER) AS volume,
                    LOWER(result) AS result,
                    close_time,
                    open_time
                FROM read_parquet('{markets_path}')
                WHERE LOWER(result) IN ('yes', 'no')
                  AND volume >= {config.min_volume_kalshi}
                  AND close_time IS NOT NULL
                  AND open_time IS NOT NULL
            ),
            first_entry AS (
                SELECT
                    t.ticker,
                    t.yes_price AS entry_cents,
                    ROW_NUMBER() OVER (
                        PARTITION BY t.ticker ORDER BY t.created_time
                    ) AS rn
                FROM read_parquet('{trades_path}') t
                SEMI JOIN resolved r ON t.ticker = r.market_id
                WHERE t.yes_price BETWEEN {config.contested_low} AND {config.contested_high}
            )
            SELECT r.*, fe.entry_cents AS price_cents
            FROM resolved r
            LEFT JOIN first_entry fe
                ON r.market_id = fe.ticker AND fe.rn = 1
        """).fetchdf()
    finally:
        con.close()

    if df.empty:
        return df

    df["platform"] = "kalshi"
    df["category"] = df["event_ticker"].str.replace(r"-.*$", "", regex=True)
    df["resolved_yes"] = df["result"] == "yes"

    # Duration
    df["close_time"] = pd.to_datetime(df["close_time"], utc=True, errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True, errors="coerce")
    df["duration_days"] = (df["close_time"] - df["open_time"]).dt.total_seconds() / 86400
    df = df[(df["duration_days"] > 0) & (df["duration_days"] <= config.max_days_to_close)]

    # Drop efficient categories
    df = df[~df["category"].isin(EFFICIENT_CATEGORIES)]

    has_entry = df["price_cents"].notna().sum()
    print(f"  Kalshi: {len(df):,} resolved markets ({has_entry:,} with entry price in {config.contested_low}-{config.contested_high}c)")
    return df


def load_polymarket_markets(config: BacktestConfig) -> pd.DataFrame:
    """Load resolved Polymarket markets (resolution-only, no entry price)."""
    path = config.data_dir / "polymarket" / "markets" / "*.parquet"
    con = duckdb.connect()
    try:
        df = con.execute(f"""
            SELECT
                COALESCE(condition_id, CAST(id AS VARCHAR)) AS market_id,
                question AS title,
                outcome_prices,
                CAST(volume AS DOUBLE) AS volume,
                end_date,
                created_at
            FROM read_parquet('{path}')
            WHERE closed = true
              AND volume >= {config.min_volume_polymarket}
              AND end_date IS NOT NULL
              AND outcome_prices IS NOT NULL
        """).fetchdf()
    finally:
        con.close()

    if df.empty:
        return df

    def _parse_resolution(op):
        try:
            if isinstance(op, str):
                prices = json.loads(op)
            elif isinstance(op, list):
                prices = op
            else:
                return None
            yes_price = float(prices[0])
            if yes_price >= 0.9:
                return "yes"
            elif yes_price <= 0.1:
                return "no"
            return None  # voided / unresolved
        except Exception:
            return None

    df["result"] = df["outcome_prices"].apply(_parse_resolution)
    df = df.dropna(subset=["result"])

    df["platform"] = "polymarket"
    df["resolved_yes"] = df["result"] == "yes"
    df["category"] = ""
    df["event_ticker"] = ""
    df["price_cents"] = np.nan  # No entry price available

    # Duration
    df["end_date"] = pd.to_datetime(df["end_date"], utc=True, errors="coerce")
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True, errors="coerce")
    df["duration_days"] = (df["end_date"] - df["created_at"]).dt.total_seconds() / 86400
    df = df[(df["duration_days"] > 0) & (df["duration_days"] <= config.max_days_to_close)]

    # Rename for unification
    df["close_time"] = df["end_date"]
    df["open_time"] = df["created_at"]

    print(f"  Polymarket: {len(df):,} resolved, filtered markets")
    return df


# ============================================================================
# Section 4: Signal Replay
# ============================================================================

def replay_signal(title: str, price_cents: int, volume: int,
                  duration_days: float, category: str, platform: str,
                  config: BacktestConfig) -> Dict:
    """Replay the production signal pipeline on a single market."""
    # Sub-daily noise filter
    if _is_subdaily_noise(title):
        return {"action": "skip", "reason": "subdaily_noise"}

    # Classify archetype
    archetype = classify_archetype(title)

    # Kill rules
    should_kill, kill_reason, _ = _check_kill_rules(title, price_cents)

    # Side decision
    if config.no_only:
        if price_cents < config.min_entry_price:
            return {"action": "skip", "reason": f"below_min_entry_{price_cents}c"}
        side = "NO"
    else:
        # Archetype-informed side: take whichever side has higher Becker WR
        becker_no_wr = BECKER_NO_WIN_RATES.get(archetype, 0.593)
        if becker_no_wr >= 0.5:
            side = "NO"
        else:
            side = "YES"

    # Category edge from mispriced categories table
    cat_info = MISPRICED_CATEGORIES.get(category)
    category_edge = cat_info["error"] if cat_info else 0.15

    # Confidence
    whale_threshold = WHALE_VOLUME_KALSHI if platform == "kalshi" else WHALE_VOLUME_POLYMARKET
    conf = calculate_signal_confidence(
        category_edge=category_edge,
        volume=volume,
        price_cents=price_cents,
        days_to_close=duration_days,
        avg_category_volume=1000,
        whale_threshold=whale_threshold,
        category=category,
    )

    # Price zone + duration bucket
    entry_decimal = price_cents / 100.0
    zone = price_zone(entry_decimal)
    dur_bucket = classify_duration(duration_days)

    return {
        "action": "kill" if (should_kill and config.apply_kill_rules) else "trade",
        "archetype": archetype,
        "side": side,
        "kill_rule": kill_reason if should_kill else "",
        "confidence": conf["confidence"],
        "confirmations": conf["confirmations"],
        "price_zone": zone,
        "duration_bucket": dur_bucket,
        "was_killable": should_kill,  # Track even when kill rules disabled
    }


# ============================================================================
# Section 5: Trade Simulation
# ============================================================================

def simulate_trades(markets_df: pd.DataFrame, config: BacktestConfig) -> BacktestResult:
    """Run chronological trade simulation with Kelly sizing."""
    result = BacktestResult(config=config)
    bankroll = config.initial_bankroll
    result.equity_curve = [bankroll]
    peak = bankroll

    # Sort chronologically by resolution time
    markets_df = markets_df.sort_values("close_time").reset_index(drop=True)
    total = len(markets_df)

    for i, row in markets_df.iterrows():
        if bankroll <= 0:
            break

        # Skip Polymarket (no entry price for position-sized backtest)
        if pd.isna(row.get("price_cents")):
            continue

        price_cents = int(row["price_cents"])
        title = str(row.get("title", ""))
        volume = int(row.get("volume", 0))
        duration = float(row.get("duration_days", 7))
        category = str(row.get("category", ""))
        platform = str(row.get("platform", "kalshi"))

        sig = replay_signal(title, price_cents, volume, duration, category, platform, config)

        if sig["action"] == "skip":
            continue

        # Build trade
        entry_price = price_cents / 100.0
        resolution = "YES" if row["resolved_yes"] else "NO"
        side = sig["side"]
        won = side == resolution

        # P&L per unit (matching shadow_tracker.py)
        if side == "NO":
            cost_basis = 1.0 - entry_price  # Cost to buy NO
        else:
            cost_basis = entry_price  # Cost to buy YES

        if cost_basis <= 0:
            continue

        pnl_per_unit = (1.0 - cost_basis) if won else -cost_basis

        # Kelly sizing
        conf_decimal = min(sig["confidence"], 95) / 100.0
        b = (1.0 - cost_basis) / cost_basis if cost_basis > 0 else 0
        if b > 0 and conf_decimal > 0:
            kelly_f = (b * conf_decimal - (1 - conf_decimal)) / b
            kelly_f = max(0.0, kelly_f) * config.kelly_fraction
        else:
            kelly_f = 0.0
        position_pct = min(kelly_f, config.max_position_pct)
        position_size = min(bankroll * position_pct, config.max_position_dollar)
        if position_size <= 0:
            continue

        # Dollar P&L: contracts = position_size / cost_basis, each earns pnl_per_unit
        contracts = position_size / cost_basis
        dollar_pnl = contracts * pnl_per_unit

        trade = Trade(
            market_id=str(row["market_id"]),
            platform=platform,
            title=title[:200],
            archetype=sig["archetype"],
            side=side,
            entry_price=entry_price,
            price_zone=sig["price_zone"],
            resolution=resolution,
            won=won,
            pnl=round(pnl_per_unit, 4),
            position_size=round(position_size, 2),
            dollar_pnl=round(dollar_pnl, 2),
            confidence=round(sig["confidence"], 1),
            category=category,
            volume=volume,
            duration_days=round(duration, 1),
            duration_bucket=sig["duration_bucket"],
            close_time=str(row.get("close_time", "")),
            kill_rule=sig["kill_rule"],
        )

        if sig["action"] == "kill":
            if config.track_killed:
                result.killed_trades.append(trade)
            continue

        # Execute
        result.trades.append(trade)
        bankroll += dollar_pnl
        result.equity_curve.append(bankroll)
        peak = max(peak, bankroll)

        # Progress
        if (i + 1) % 50_000 == 0:
            print(f"  ... processed {i+1:,}/{total:,} markets, {len(result.trades):,} trades, bankroll ${bankroll:,.0f}")

    result.total_trades = len(result.trades)
    result.max_drawdown = (peak - min(result.equity_curve)) / peak if peak > 0 else 0
    return result


# ============================================================================
# Section 6: Metrics
# ============================================================================

def compute_wr_table(trades: List[Trade]) -> pd.DataFrame:
    """Build archetype x side x price_zone win rate pivot table."""
    if not trades:
        return pd.DataFrame()

    records = [
        {
            "archetype": t.archetype,
            "side": t.side,
            "price_zone": t.price_zone,
            "won": t.won,
            "pnl": t.pnl,
            "platform": t.platform,
            "duration_bucket": t.duration_bucket,
            "confidence": t.confidence,
        }
        for t in trades
    ]
    df = pd.DataFrame(records)

    pivot = (
        df.groupby(["archetype", "side", "price_zone"])
        .agg(n=("won", "count"), wins=("won", "sum"), avg_pnl=("pnl", "mean"))
        .reset_index()
    )
    pivot["wr"] = (pivot["wins"] / pivot["n"] * 100).round(1)
    pivot = pivot.sort_values(["archetype", "side", "wr"], ascending=[True, True, False])
    return pivot


def compute_kill_report(result: BacktestResult) -> Dict:
    """Counterfactual analysis: what happened to killed trades?"""
    report = {}
    for t in result.killed_trades:
        rule = t.kill_rule.split(":")[0].strip() if t.kill_rule else "unknown"
        if rule not in report:
            report[rule] = {"total": 0, "would_won": 0, "total_pnl": 0.0}
        report[rule]["total"] += 1
        if t.won:
            report[rule]["would_won"] += 1
        report[rule]["total_pnl"] += t.pnl

    for stats in report.values():
        n = stats["total"]
        stats["counterfactual_wr"] = round(stats["would_won"] / n * 100, 1) if n else 0
        wr = stats["counterfactual_wr"]
        stats["verdict"] = (
            "CORRECT KILL" if wr <= 55
            else "QUESTIONABLE" if wr <= 65
            else "BAD KILL"
        )
    return report


def compute_portfolio_metrics(result: BacktestResult):
    """Compute aggregate portfolio metrics."""
    trades = result.trades
    if not trades:
        return

    pnls = [t.dollar_pnl for t in trades]
    wins = [t for t in trades if t.won]
    losses = [t for t in trades if not t.won]

    result.total_pnl = sum(pnls)
    result.win_rate = len(wins) / len(trades) if trades else 0

    # Sharpe (annualized, assuming ~1 trade/day)
    if len(pnls) > 1 and np.std(pnls) > 0:
        result.sharpe = round(np.mean(pnls) / np.std(pnls) * np.sqrt(252), 2)

    # Profit factor
    gross_win = sum(t.dollar_pnl for t in wins) if wins else 0
    gross_loss = abs(sum(t.dollar_pnl for t in losses)) if losses else 1
    result.profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf")


def polymarket_archetype_analysis(poly_df: pd.DataFrame) -> pd.DataFrame:
    """Population-level archetype NO win rate for Polymarket (validates Becker priors)."""
    if poly_df.empty:
        return pd.DataFrame()

    records = []
    for _, row in poly_df.iterrows():
        arch = classify_archetype(str(row.get("title", "")))
        records.append({
            "archetype": arch,
            "resolved_no": not row["resolved_yes"],
            "volume": row.get("volume", 0),
        })

    df = pd.DataFrame(records)
    table = (
        df.groupby("archetype")
        .agg(n=("resolved_no", "count"), no_wins=("resolved_no", "sum"), avg_volume=("volume", "mean"))
        .reset_index()
    )
    table["no_wr"] = (table["no_wins"] / table["n"] * 100).round(1)
    table["becker_wr"] = table["archetype"].map(
        lambda a: round(BECKER_NO_WIN_RATES.get(a, 0.593) * 100, 1)
    )
    table["delta"] = (table["no_wr"] - table["becker_wr"]).round(1)
    table = table.sort_values("n", ascending=False)
    return table


def kalshi_population_analysis(kalshi_df: pd.DataFrame) -> pd.DataFrame:
    """Population-level archetype resolution rates for ALL Kalshi markets.

    Uses every resolved market regardless of entry price availability.
    This validates Becker priors against our actual data.
    """
    if kalshi_df.empty:
        return pd.DataFrame()

    records = []
    for _, row in kalshi_df.iterrows():
        arch = classify_archetype(str(row.get("title", "")))
        records.append({
            "archetype": arch,
            "resolved_no": not row["resolved_yes"],
            "volume": row.get("volume", 0),
        })

    df = pd.DataFrame(records)
    table = (
        df.groupby("archetype")
        .agg(n=("resolved_no", "count"), no_wins=("resolved_no", "sum"),
             avg_volume=("volume", "mean"))
        .reset_index()
    )
    table["no_wr"] = (table["no_wins"] / table["n"] * 100).round(1)
    table["becker_wr"] = table["archetype"].map(
        lambda a: round(BECKER_NO_WIN_RATES.get(a, 0.593) * 100, 1)
    )
    table["delta"] = (table["no_wr"] - table["becker_wr"]).round(1)
    table = table.sort_values("n", ascending=False)
    return table


# ============================================================================
# Section 7: Output
# ============================================================================

def print_console_report(result: BacktestResult, kill_report: Dict,
                         wr_table: pd.DataFrame, poly_table: Optional[pd.DataFrame],
                         kalshi_pop: Optional[pd.DataFrame] = None):
    """Print formatted console report."""
    cfg = result.config
    mode = "NO-only" if cfg.no_only else "Both sides"
    kills = "ON" if cfg.apply_kill_rules else "OFF"

    print("\n" + "=" * 60)
    print("  PREDICTION MARKET BACKTEST RESULTS")
    print("=" * 60)
    print(f"  Mode: {mode} | Kill rules: {kills}")
    print(f"  Bankroll: ${cfg.initial_bankroll:,.0f} | Kelly: {cfg.kelly_fraction:.0%}")
    print(f"  Volume filter: Kalshi >= {cfg.min_volume_kalshi:,} | Poly >= {cfg.min_volume_polymarket:,}")

    print(f"\n  PORTFOLIO METRICS")
    print(f"  {'Trades:':<22} {result.total_trades:,}")
    print(f"  {'Win rate:':<22} {result.win_rate:.1%}")
    print(f"  {'Total P&L:':<22} ${result.total_pnl:,.2f}")
    print(f"  {'Sharpe ratio:':<22} {result.sharpe}")
    print(f"  {'Max drawdown:':<22} {result.max_drawdown:.1%}")
    print(f"  {'Profit factor:':<22} {result.profit_factor}")
    if result.equity_curve:
        print(f"  {'Final bankroll:':<22} ${result.equity_curve[-1]:,.2f}")

    # WR by archetype
    if not wr_table.empty:
        arch_summary = (
            wr_table.groupby("archetype")
            .agg(n=("n", "sum"), wins=("wins", "sum"))
            .reset_index()
        )
        arch_summary["wr"] = (arch_summary["wins"] / arch_summary["n"] * 100).round(1)
        arch_summary = arch_summary.sort_values("n", ascending=False)

        print(f"\n  ARCHETYPE WIN RATES")
        for _, r in arch_summary.iterrows():
            becker = BECKER_NO_WIN_RATES.get(r["archetype"], 0.593) * 100
            delta = r["wr"] - becker
            marker = "+" if delta > 0 else ""
            print(f"    {r['archetype']:<24} {r['wr']:5.1f}% (n={int(r['n']):,})  becker={becker:.0f}% {marker}{delta:.0f}pp")

    # WR by price zone
    if not wr_table.empty:
        zone_summary = (
            wr_table.groupby("price_zone")
            .agg(n=("n", "sum"), wins=("wins", "sum"))
            .reset_index()
        )
        zone_summary["wr"] = (zone_summary["wins"] / zone_summary["n"] * 100).round(1)
        zone_order = ["garbage", "cheap", "mid_low", "mid", "sweet", "premium", "expensive"]
        zone_summary["sort_key"] = zone_summary["price_zone"].map(
            {z: i for i, z in enumerate(zone_order)}
        )
        zone_summary = zone_summary.sort_values("sort_key")

        print(f"\n  WIN RATE BY PRICE ZONE")
        for _, r in zone_summary.iterrows():
            modifier = PRICE_ZONE_MODIFIERS.get(r["price_zone"], 1.0)
            print(f"    {r['price_zone']:<12} {r['wr']:5.1f}% (n={int(r['n']):,})  modifier={modifier}")

    # Kill rule effectiveness
    if kill_report:
        print(f"\n  KILL RULE EFFECTIVENESS")
        for rule, stats in sorted(kill_report.items()):
            print(
                f"    {rule:<6} killed {stats['total']:,} trades | "
                f"counterfactual WR={stats['counterfactual_wr']:.1f}% | "
                f"{stats['verdict']}"
            )

    # Kalshi population-level analysis (ALL markets, no price filter)
    if kalshi_pop is not None and not kalshi_pop.empty:
        total_pop = kalshi_pop["n"].sum()
        print(f"\n  KALSHI POPULATION WR (ALL {total_pop:,} resolved markets)")
        for _, r in kalshi_pop.head(15).iterrows():
            marker = "+" if r["delta"] > 0 else ""
            print(
                f"    {r['archetype']:<24} NO WR={r['no_wr']:5.1f}% (n={int(r['n']):,}) "
                f"becker={r['becker_wr']:.0f}% {marker}{r['delta']:.0f}pp"
            )

    # Polymarket validation
    if poly_table is not None and not poly_table.empty:
        print(f"\n  POLYMARKET ARCHETYPE VALIDATION (population-level)")
        for _, r in poly_table.head(15).iterrows():
            marker = "+" if r["delta"] > 0 else ""
            print(
                f"    {r['archetype']:<24} NO WR={r['no_wr']:5.1f}% (n={int(r['n']):,}) "
                f"becker={r['becker_wr']:.0f}% {marker}{r['delta']:.0f}pp"
            )

    print("\n" + "=" * 60)


def export_results(result: BacktestResult, wr_table: pd.DataFrame):
    """Export trades and WR table to CSV."""
    out = result.config.output_dir
    out.mkdir(parents=True, exist_ok=True)

    # Trade-level CSV
    if result.trades or result.killed_trades:
        all_trades = result.trades + result.killed_trades
        rows = []
        for t in all_trades:
            rows.append({
                "market_id": t.market_id,
                "platform": t.platform,
                "title": t.title,
                "archetype": t.archetype,
                "side": t.side,
                "entry_price": t.entry_price,
                "price_zone": t.price_zone,
                "resolution": t.resolution,
                "won": t.won,
                "pnl": t.pnl,
                "position_size": t.position_size,
                "dollar_pnl": t.dollar_pnl,
                "confidence": t.confidence,
                "category": t.category,
                "volume": t.volume,
                "duration_days": t.duration_days,
                "duration_bucket": t.duration_bucket,
                "close_time": t.close_time,
                "kill_rule": t.kill_rule,
                "status": "killed" if t.kill_rule else "traded",
            })
        trades_df = pd.DataFrame(rows)
        trades_path = out / "backtest_trades.csv"
        trades_df.to_csv(trades_path, index=False)
        print(f"\n  Exported {len(trades_df):,} trades -> {trades_path}")

    # WR table CSV
    if not wr_table.empty:
        wr_path = out / "backtest_wr_table.csv"
        wr_table.to_csv(wr_path, index=False)
        print(f"  Exported WR table -> {wr_path}")


def plot_dashboard(result: BacktestResult, wr_table: pd.DataFrame,
                   kill_report: Dict, poly_table: Optional[pd.DataFrame]):
    """6-panel matplotlib dashboard."""
    try:
        import matplotlib.pyplot as plt
        from src.common.chart_theme import apply_theme, style_ax, summary_box, COLORS
    except ImportError:
        print("  matplotlib or chart_theme not available, skipping plots")
        return None

    apply_theme()
    fig, axes = plt.subplots(3, 2, figsize=(18, 16))
    fig.suptitle("Prediction Market Backtest", fontsize=18, fontweight="bold", y=0.98)

    # Panel 1: Equity curve
    ax = axes[0, 0]
    eq = result.equity_curve
    ax.plot(eq, color=COLORS["accent"], linewidth=1.5)
    ax.axhline(result.config.initial_bankroll, color=COLORS["text2"], linestyle="--", alpha=0.5)
    ax.fill_between(range(len(eq)), eq, result.config.initial_bankroll,
                     where=[e >= result.config.initial_bankroll for e in eq],
                     color=COLORS["green"], alpha=0.15)
    ax.fill_between(range(len(eq)), eq, result.config.initial_bankroll,
                     where=[e < result.config.initial_bankroll for e in eq],
                     color=COLORS["red"], alpha=0.15)
    style_ax(ax, "Equity Curve")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("Bankroll ($)")

    # Panel 2: WR by archetype
    ax = axes[0, 1]
    if not wr_table.empty:
        arch = (
            wr_table.groupby("archetype")
            .agg(n=("n", "sum"), wins=("wins", "sum"))
            .reset_index()
        )
        arch["wr"] = arch["wins"] / arch["n"] * 100
        arch = arch.sort_values("wr", ascending=True)
        colors = [
            COLORS["green"] if w > 60 else COLORS["orange"] if w > 50 else COLORS["red"]
            for w in arch["wr"]
        ]
        bars = ax.barh(arch["archetype"], arch["wr"], color=colors, alpha=0.85)
        for bar, n in zip(bars, arch["n"]):
            ax.text(bar.get_width() + 1, bar.get_y() + bar.get_height() / 2,
                    f"n={int(n):,}", va="center", fontsize=8, color=COLORS["text2"])
        ax.axvline(50, color=COLORS["red"], linestyle="--", alpha=0.5, label="breakeven")
    style_ax(ax, "Win Rate by Archetype")
    ax.set_xlabel("Win Rate (%)")

    # Panel 3: WR by price zone
    ax = axes[1, 0]
    if not wr_table.empty:
        zones = (
            wr_table.groupby("price_zone")
            .agg(n=("n", "sum"), wins=("wins", "sum"))
            .reset_index()
        )
        zones["wr"] = zones["wins"] / zones["n"] * 100
        zone_order = ["garbage", "cheap", "mid_low", "mid", "sweet", "premium", "expensive"]
        zones["sort_key"] = zones["price_zone"].map({z: i for i, z in enumerate(zone_order)})
        zones = zones.sort_values("sort_key")
        colors = [
            COLORS["green"] if w > 60 else COLORS["orange"] if w > 50 else COLORS["red"]
            for w in zones["wr"]
        ]
        bars = ax.bar(zones["price_zone"], zones["wr"], color=colors, alpha=0.85)
        for bar, n in zip(bars, zones["n"]):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
                    f"n={int(n):,}", ha="center", fontsize=7, color=COLORS["text2"])
        ax.axhline(50, color=COLORS["red"], linestyle="--", alpha=0.5)
    style_ax(ax, "Win Rate by Price Zone")
    ax.set_ylabel("Win Rate (%)")
    ax.tick_params(axis="x", rotation=30)

    # Panel 4: Kill rule effectiveness
    ax = axes[1, 1]
    if kill_report:
        rules = sorted(kill_report.keys())
        cwr = [kill_report[r]["counterfactual_wr"] for r in rules]
        counts = [kill_report[r]["total"] for r in rules]
        colors = [
            COLORS["green"] if w <= 55 else COLORS["orange"] if w <= 65 else COLORS["red"]
            for w in cwr
        ]
        bars = ax.bar(rules, cwr, color=colors, alpha=0.85)
        for bar, n in zip(bars, counts):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
                    f"n={n:,}", ha="center", fontsize=8, color=COLORS["text2"])
        ax.axhline(50, color=COLORS["red"], linestyle="--", alpha=0.5, label="breakeven")
        ax.axhline(result.win_rate * 100, color=COLORS["cyan"], linestyle="--",
                   alpha=0.5, label=f"traded WR ({result.win_rate:.0%})")
        ax.legend(fontsize=8)
    style_ax(ax, "Kill Rule Effectiveness (lower = correct kill)")
    ax.set_ylabel("Counterfactual WR (%)")

    # Panel 5: Archetype x price zone heatmap
    ax = axes[2, 0]
    if not wr_table.empty and len(wr_table) > 1:
        heatmap_data = wr_table.pivot_table(
            index="archetype", columns="price_zone", values="wr", aggfunc="first"
        )
        zone_order = ["garbage", "cheap", "mid_low", "mid", "sweet", "premium", "expensive"]
        heatmap_data = heatmap_data.reindex(columns=[z for z in zone_order if z in heatmap_data.columns])
        if not heatmap_data.empty:
            im = ax.imshow(heatmap_data.values, cmap="RdYlGn", aspect="auto", vmin=30, vmax=90)
            ax.set_xticks(range(len(heatmap_data.columns)))
            ax.set_xticklabels(heatmap_data.columns, fontsize=8, rotation=30)
            ax.set_yticks(range(len(heatmap_data.index)))
            ax.set_yticklabels(heatmap_data.index, fontsize=8)
            # Annotate cells
            for i in range(len(heatmap_data.index)):
                for j in range(len(heatmap_data.columns)):
                    val = heatmap_data.values[i, j]
                    if not np.isnan(val):
                        ax.text(j, i, f"{val:.0f}%", ha="center", va="center",
                                fontsize=7, color="black" if val > 60 else "white")
            fig.colorbar(im, ax=ax, shrink=0.8)
    style_ax(ax, "WR Heatmap: Archetype x Price Zone")

    # Panel 6: Summary stats
    ax = axes[2, 1]
    ax.axis("off")
    stats_text = (
        f"SUMMARY\n"
        f"{'─' * 36}\n"
        f"Trades:          {result.total_trades:,}\n"
        f"Killed:          {len(result.killed_trades):,}\n"
        f"Win Rate:        {result.win_rate:.1%}\n"
        f"Total P&L:       ${result.total_pnl:,.2f}\n"
        f"Sharpe:          {result.sharpe}\n"
        f"Max Drawdown:    {result.max_drawdown:.1%}\n"
        f"Profit Factor:   {result.profit_factor}\n"
        f"Final Bankroll:  ${result.equity_curve[-1]:,.2f}\n"
        f"{'─' * 36}\n"
        f"Mode:            {'NO-only' if result.config.no_only else 'Both'}\n"
        f"Kill Rules:      {'ON' if result.config.apply_kill_rules else 'OFF'}\n"
        f"Kelly:           {result.config.kelly_fraction:.0%}\n"
        f"Vol Filter (K):  {result.config.min_volume_kalshi:,}\n"
    )
    summary_box(ax, stats_text, loc="center right", fontsize=10)

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    return fig


# ============================================================================
# Section 8: CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Prediction market backtest — replays signal pipeline on historical data"
    )
    parser.add_argument("--both-sides", action="store_true", help="Test both YES and NO")
    parser.add_argument("--no-kill-rules", action="store_true", help="Disable kill rules")
    parser.add_argument("--kalshi-only", action="store_true", help="Kalshi data only")
    parser.add_argument("--polymarket-only", action="store_true", help="Polymarket data only")
    parser.add_argument("--min-volume-kalshi", type=int, help="Override Kalshi volume filter")
    parser.add_argument("--min-volume-poly", type=int, help="Override Polymarket volume filter")
    parser.add_argument("--bankroll", type=float, default=10_000, help="Starting bankroll")
    parser.add_argument("--kelly", type=float, default=0.25, help="Kelly fraction (0-1)")
    parser.add_argument("--max-position", type=float, default=500, help="Max position size in dollars")
    parser.add_argument("--no-plots", action="store_true", help="Skip matplotlib output")
    parser.add_argument("--no-csv", action="store_true", help="Skip CSV export")
    args = parser.parse_args()

    config = BacktestConfig(
        no_only=not args.both_sides,
        apply_kill_rules=not args.no_kill_rules,
        include_kalshi=not args.polymarket_only,
        include_polymarket=not args.kalshi_only,
        initial_bankroll=args.bankroll,
        kelly_fraction=args.kelly,
        max_position_dollar=args.max_position,
        show_plots=not args.no_plots,
        save_plots=not args.no_plots,
        export_csv=not args.no_csv,
    )
    if args.min_volume_kalshi:
        config.min_volume_kalshi = args.min_volume_kalshi
    if args.min_volume_poly:
        config.min_volume_polymarket = args.min_volume_poly

    # ── Load data ──
    print("Loading market data...")
    kalshi_df = load_kalshi_markets(config) if config.include_kalshi else pd.DataFrame()
    poly_df = load_polymarket_markets(config) if config.include_polymarket else pd.DataFrame()

    # ── Population-level analysis (ALL resolved markets) ──
    kalshi_pop = None
    if not kalshi_df.empty:
        print("\nRunning Kalshi population-level archetype analysis...")
        kalshi_pop = kalshi_population_analysis(kalshi_df)

    # ── Kalshi backtest (position-sized, markets with entry prices only) ──
    print("\nRunning signal replay + trade simulation...")
    result = simulate_trades(kalshi_df, config)
    compute_portfolio_metrics(result)
    wr_table = compute_wr_table(result.trades)
    kill_report = compute_kill_report(result) if config.track_killed else {}

    # ── Polymarket validation (population-level) ──
    poly_table = None
    if not poly_df.empty:
        print("\nRunning Polymarket archetype analysis...")
        poly_table = polymarket_archetype_analysis(poly_df)
        result.poly_archetype_table = poly_table

    # ── Output ──
    print_console_report(result, kill_report, wr_table, poly_table,
                         kalshi_pop=kalshi_pop)

    if config.export_csv:
        export_results(result, wr_table)
        if kalshi_pop is not None and not kalshi_pop.empty:
            pop_path = config.output_dir / "kalshi_population_wr.csv"
            kalshi_pop.to_csv(pop_path, index=False)
            print(f"  Exported Kalshi population WR -> {pop_path}")
        if poly_table is not None and not poly_table.empty:
            poly_path = config.output_dir / "polymarket_archetype_validation.csv"
            poly_table.to_csv(poly_path, index=False)
            print(f"  Exported Polymarket validation -> {poly_path}")

    if config.show_plots and result.trades:
        fig = plot_dashboard(result, wr_table, kill_report, poly_table)
        if fig and config.save_plots:
            config.output_dir.mkdir(parents=True, exist_ok=True)
            fig_path = config.output_dir / "backtest_dashboard.png"
            fig.savefig(fig_path, dpi=150)
            print(f"  Saved dashboard -> {fig_path}")
        if fig:
            import matplotlib.pyplot as plt
            plt.show()


if __name__ == "__main__":
    main()
