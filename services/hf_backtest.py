"""
HF Monte Carlo Backtester — Phase 4B

Simulates HF strategy performance using collected data:
1. Historical market resolutions (Up/Down outcomes)
2. Divergence snapshots (latency distribution)
3. Signal accuracy (Virtuoso direction vs actual outcome)

Outputs: expected edge per cycle, Kelly sizing, drawdown profiles, win rate.
"""

import json
import logging
import math
import os
import random
import sqlite3
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("hf_backtest")

DB_PATH = os.getenv("HF_DB_PATH",
    str(Path(__file__).parent.parent / "storage" / "shadow_trades.db"))


# ============================================================================
# Data Loading
# ============================================================================

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_resolutions(asset: str = None, duration: str = None) -> List[Dict]:
    """Load market resolutions for backtesting."""
    db = _get_db()
    query = "SELECT * FROM hf_market_resolutions WHERE 1=1"
    params = []
    if asset:
        query += " AND asset = ?"
        params.append(asset)
    if duration:
        query += " AND duration = ?"
        params.append(duration)
    query += " ORDER BY end_time ASC"
    
    rows = db.execute(query, params).fetchall()
    db.close()
    return [dict(r) for r in rows]


def load_divergence_stats(asset: str = None) -> Dict:
    """Load divergence distribution stats for simulation."""
    db = _get_db()
    query = "SELECT divergence_pct, oracle_age_seconds FROM hf_divergence_snapshots WHERE 1=1"
    params = []
    if asset:
        query += " AND asset = ?"
        params.append(asset)
    
    rows = db.execute(query, params).fetchall()
    db.close()
    
    if not rows:
        return {"count": 0, "mean_div": 0, "max_div": 0, "std_div": 0,
                "pct_above_threshold": 0, "divergences": []}
    
    divs = [abs(r["divergence_pct"]) for r in rows]
    ages = [r["oracle_age_seconds"] for r in rows if r["oracle_age_seconds"] and r["oracle_age_seconds"] > 0]
    
    mean_div = sum(divs) / len(divs)
    max_div = max(divs)
    variance = sum((d - mean_div) ** 2 for d in divs) / len(divs)
    std_div = math.sqrt(variance)
    above_03 = sum(1 for d in divs if d >= 0.3) / len(divs) * 100
    
    return {
        "count": len(divs),
        "mean_div_pct": round(mean_div, 4),
        "max_div_pct": round(max_div, 4),
        "std_div_pct": round(std_div, 4),
        "pct_above_threshold": round(above_03, 2),
        "mean_oracle_age_sec": round(sum(ages) / len(ages), 1) if ages else 0,
        "divergences": divs,
    }


def load_signal_accuracy(asset: str = None) -> Dict:
    """Compare signal snapshots to market resolutions for accuracy."""
    db = _get_db()
    
    # Get signal snapshots with their direction
    sig_query = "SELECT * FROM hf_signal_snapshots WHERE fusion_direction != 'NEUTRAL'"
    params = []
    if asset:
        sig_query += " AND asset = ?"
        params.append(asset)
    
    signals = db.execute(sig_query, params).fetchall()
    
    # For each signal, find the closest resolution
    correct = 0
    incorrect = 0
    total_matched = 0
    
    for sig in signals:
        # Match by asset + closest resolution time
        res_query = """
            SELECT outcome FROM hf_market_resolutions 
            WHERE asset = ? AND outcome IS NOT NULL
            AND end_time >= ? 
            ORDER BY end_time ASC LIMIT 1
        """
        res = db.execute(res_query, (sig["asset"], sig["snapshot_at"])).fetchone()
        
        if res and res["outcome"]:
            total_matched += 1
            sig_dir = sig["fusion_direction"]
            actual = res["outcome"]
            
            # LONG → expect Up, SHORT → expect Down
            if (sig_dir == "LONG" and actual == "Up") or \
               (sig_dir == "SHORT" and actual == "Down"):
                correct += 1
            else:
                incorrect += 1
    
    db.close()
    
    accuracy = correct / total_matched * 100 if total_matched > 0 else 0
    
    return {
        "total_signals": len(signals),
        "matched_to_resolution": total_matched,
        "correct": correct,
        "incorrect": incorrect,
        "accuracy_pct": round(accuracy, 1),
    }


# ============================================================================
# Monte Carlo Simulation
# ============================================================================

@dataclass
class SimulationResult:
    """Result of a single MC simulation path."""
    final_balance: float
    max_balance: float
    min_balance: float
    max_drawdown_pct: float
    total_trades: int
    wins: int
    losses: int
    win_rate_pct: float
    total_return_pct: float


@dataclass
class BacktestResult:
    """Aggregated Monte Carlo backtest results."""
    # Input params
    starting_balance: float
    num_simulations: int
    trades_per_sim: int
    strategy: str
    
    # Distribution stats
    median_final_balance: float
    mean_final_balance: float
    p5_final_balance: float   # 5th percentile (worst case)
    p25_final_balance: float
    p75_final_balance: float
    p95_final_balance: float  # 95th percentile (best case)
    
    # Risk stats
    median_max_drawdown_pct: float
    p95_max_drawdown_pct: float  # Worst 5% of drawdowns
    ruin_probability_pct: float  # % of sims that went to 0
    
    # Edge stats
    median_win_rate_pct: float
    median_return_pct: float
    mean_return_pct: float
    sharpe_estimate: float
    
    # Kelly
    optimal_kelly_fraction: float
    half_kelly_fraction: float
    
    # Data quality
    data_points_used: int
    data_source: str
    
    timestamp: str


def run_monte_carlo(
    starting_balance: float = 134.0,
    num_simulations: int = 1000,
    trades_per_sim: int = 200,
    strategy: str = "latency_arb",
    asset: str = "BTC",
    kelly_fraction: float = 0.10,
    edge_per_trade: float = None,  # Override with estimated edge
    win_rate: float = None,  # Override with estimated win rate
) -> BacktestResult:
    """
    Run Monte Carlo simulation of HF strategy.
    
    If we have collected data, uses actual distributions.
    Otherwise, falls back to parameterized simulation.
    
    Strategies:
    - "latency_arb": Latency arbitrage (Binance leads oracle)
    - "neg_vig": Negative vig (buy both sides < $1)
    - "directional": Virtuoso signal → Polymarket direction
    - "combined": All three combined
    """
    
    # Load data if available
    div_stats = load_divergence_stats(asset)
    resolutions = load_resolutions(asset)
    signal_acc = load_signal_accuracy(asset)
    
    # Determine parameters based on strategy + data
    if strategy == "latency_arb":
        # Edge comes from latency divergence
        if div_stats["count"] > 100 and edge_per_trade is None:
            # Use actual divergence distribution
            usable_divs = [d for d in div_stats["divergences"] if d >= 0.1]
            if usable_divs:
                avg_edge = sum(usable_divs) / len(usable_divs) / 100
                freq = len(usable_divs) / div_stats["count"]
            else:
                avg_edge = 0.015
                freq = 0.05
            wr = win_rate or 0.58  # Slight edge from latency
            edge = edge_per_trade or avg_edge * freq
        else:
            # Parameterized estimate from $200K story
            wr = win_rate or 0.58
            edge = edge_per_trade or 0.025  # 2.5% per cycle average
    
    elif strategy == "neg_vig":
        wr = win_rate or 0.98  # Almost guaranteed (just needs both sides filled)
        edge = edge_per_trade or 0.015  # ~1.5% when it exists
    
    elif strategy == "directional":
        if signal_acc["accuracy_pct"] > 0 and win_rate is None:
            wr = signal_acc["accuracy_pct"] / 100
        else:
            wr = win_rate or 0.54
        edge = edge_per_trade or 0.02
    
    elif strategy == "combined":
        wr = win_rate or 0.60
        edge = edge_per_trade or 0.028
    
    else:
        wr = win_rate or 0.55
        edge = edge_per_trade or 0.02
    
    # Payoff structure for binary markets
    # Win: profit = stake * (1/price - 1) ≈ stake * edge_factor
    # Loss: lose stake
    # For ~50c markets: win pays ~1x, lose pays -1x
    
    # Run simulations
    all_results: List[SimulationResult] = []
    
    for _ in range(num_simulations):
        balance = starting_balance
        max_bal = balance
        min_bal = balance
        max_dd_pct = 0
        wins = 0
        losses = 0
        
        for t in range(trades_per_sim):
            if balance <= 0.01:
                losses += (trades_per_sim - t)
                break
            
            # Position size (Kelly-based)
            stake = balance * kelly_fraction
            
            # Random outcome based on win rate
            if random.random() < wr:
                # Win: earn edge-proportional profit
                # In binary markets at ~50c, profit ≈ stake * (payout_ratio)
                profit = stake * (edge / kelly_fraction) if kelly_fraction > 0 else 0
                # Cap at 2x stake (binary payout)
                profit = min(profit, stake * 1.0)
                balance += profit
                wins += 1
            else:
                # Loss: lose fraction of stake (not always full stake in HF)
                loss = stake * 0.8  # Partial loss — can exit early
                balance -= loss
                losses += 1
            
            max_bal = max(max_bal, balance)
            min_bal = min(min_bal, balance)
            
            if max_bal > 0:
                dd = (max_bal - balance) / max_bal * 100
                max_dd_pct = max(max_dd_pct, dd)
        
        total_trades = wins + losses
        all_results.append(SimulationResult(
            final_balance=round(balance, 2),
            max_balance=round(max_bal, 2),
            min_balance=round(min_bal, 2),
            max_drawdown_pct=round(max_dd_pct, 1),
            total_trades=total_trades,
            wins=wins,
            losses=losses,
            win_rate_pct=round(wins / total_trades * 100, 1) if total_trades > 0 else 0,
            total_return_pct=round((balance - starting_balance) / starting_balance * 100, 1),
        ))
    
    # Aggregate results
    final_balances = sorted([r.final_balance for r in all_results])
    drawdowns = sorted([r.max_drawdown_pct for r in all_results])
    returns = [r.total_return_pct for r in all_results]
    win_rates = [r.win_rate_pct for r in all_results]
    
    n = len(final_balances)
    
    mean_return = sum(returns) / n
    std_return = math.sqrt(sum((r - mean_return) ** 2 for r in returns) / n) if n > 1 else 1
    sharpe = mean_return / std_return if std_return > 0 else 0
    
    # Optimal Kelly: f* = (p * b - q) / b where p=win_rate, q=1-p, b=payout ratio
    b = 1.0  # Binary market payout ratio
    q = 1 - wr
    optimal_kelly = (wr * b - q) / b if b > 0 else 0
    
    ruin_count = sum(1 for r in all_results if r.final_balance < 1.0)
    
    data_points = div_stats["count"] + len(resolutions) + signal_acc.get("total_signals", 0)
    
    return BacktestResult(
        starting_balance=starting_balance,
        num_simulations=num_simulations,
        trades_per_sim=trades_per_sim,
        strategy=strategy,
        median_final_balance=final_balances[n // 2],
        mean_final_balance=round(sum(final_balances) / n, 2),
        p5_final_balance=final_balances[int(n * 0.05)],
        p25_final_balance=final_balances[int(n * 0.25)],
        p75_final_balance=final_balances[int(n * 0.75)],
        p95_final_balance=final_balances[int(n * 0.95)],
        median_max_drawdown_pct=drawdowns[n // 2],
        p95_max_drawdown_pct=drawdowns[int(n * 0.95)],
        ruin_probability_pct=round(ruin_count / n * 100, 1),
        median_win_rate_pct=round(sum(win_rates) / n, 1),
        median_return_pct=sorted(returns)[n // 2],
        mean_return_pct=round(mean_return, 1),
        sharpe_estimate=round(sharpe, 2),
        optimal_kelly_fraction=round(max(0, optimal_kelly), 3),
        half_kelly_fraction=round(max(0, optimal_kelly / 2), 3),
        data_points_used=data_points,
        data_source="collected" if data_points > 50 else "parameterized",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


# ============================================================================
# Full Backtest Report
# ============================================================================

def full_backtest_report(
    starting_balance: float = 134.0,
    num_simulations: int = 1000,
    trades_per_sim: int = 200,
) -> Dict:
    """
    Run MC backtests for all strategies and produce a comparison report.
    """
    strategies = ["latency_arb", "neg_vig", "directional", "combined"]
    results = {}
    
    for strat in strategies:
        result = run_monte_carlo(
            starting_balance=starting_balance,
            num_simulations=num_simulations,
            trades_per_sim=trades_per_sim,
            strategy=strat,
        )
        results[strat] = asdict(result)
    
    # Data quality assessment
    div_stats = load_divergence_stats()
    resolution_count = len(load_resolutions())
    signal_acc = load_signal_accuracy()
    
    return {
        "report": results,
        "data_quality": {
            "divergence_snapshots": div_stats["count"],
            "market_resolutions": resolution_count,
            "signal_snapshots": signal_acc["total_signals"],
            "assessment": "sufficient" if (div_stats["count"] > 100 and resolution_count > 50) 
                          else "collecting" if (div_stats["count"] > 0 or resolution_count > 0)
                          else "no_data",
            "note": "Results are parameterized estimates until sufficient data is collected. "
                    "Run collection cycles to improve accuracy."
        },
        "divergence_distribution": {
            k: v for k, v in div_stats.items() if k != "divergences"
        },
        "signal_accuracy": signal_acc,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import pprint
    logging.basicConfig(level=logging.INFO)
    
    print("=" * 60)
    print("HF Monte Carlo Backtester — Phase 4")
    print("=" * 60)
    
    report = full_backtest_report(starting_balance=134.0, num_simulations=500)
    
    for strat, result in report["report"].items():
        print(f"\n📊 Strategy: {strat}")
        print(f"  Median final: ${result['median_final_balance']:,.2f}")
        print(f"  P5-P95 range: ${result['p5_final_balance']:,.2f} — ${result['p95_final_balance']:,.2f}")
        print(f"  Win rate: {result['median_win_rate_pct']}%")
        print(f"  Sharpe: {result['sharpe_estimate']}")
        print(f"  Max DD (median): {result['median_max_drawdown_pct']}%")
        print(f"  Ruin prob: {result['ruin_probability_pct']}%")
        print(f"  Kelly: {result['optimal_kelly_fraction']} (half: {result['half_kelly_fraction']})")
    
    print(f"\n📦 Data quality: {report['data_quality']['assessment']}")
