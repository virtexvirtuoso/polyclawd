"""
CV Kelly Haircut — Monte Carlo Uncertainty Adjustment

Adds empirical uncertainty quantification to Kelly position sizing.
Based on: Roan's "How Hedge Funds Use Prediction Market Data" (Feb 2026)

Formula: f_empirical = f_kelly × (1 - CV_edge)
Where CV_edge = stdev(edge) / mean(edge) from bootstrap resampling

Also includes Monte Carlo drawdown analysis:
- Simulate 10,000 equity paths from historical returns
- Ensure 95th percentile max drawdown < threshold
- If exceeded, further reduce position sizing
"""

import math
import random
import sqlite3
import time
from typing import Dict, Any, Optional, List, Tuple


# Configuration
BOOTSTRAP_ITERATIONS = 1000      # Bootstrap resamples for CV estimation
MONTE_CARLO_PATHS = 10000        # Equity curve simulations
MAX_DRAWDOWN_95TH = 0.20         # 95th percentile max DD threshold (20%)
MIN_RESOLVED_FOR_CV = 15         # Minimum resolved trades before CV kicks in
CV_FLOOR = 0.10                  # Minimum CV (always at least 10% haircut)
CV_CAP = 0.60                    # Maximum CV haircut (never more than 60%)


def get_historical_returns(db_path: str = "storage/shadow_trades.db") -> List[float]:
    """Extract historical returns from resolved shadow trades."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT pnl, entry_price FROM shadow_trades 
            WHERE resolved = 1 AND pnl IS NOT NULL
            ORDER BY resolved_at
        """).fetchall()
        conn.close()
        
        returns = []
        for r in rows:
            pnl = r["pnl"] or 0
            entry = r["entry_price"] or 0.5
            # Return as fraction of bet size (entry price is the risk)
            if entry > 0:
                returns.append(pnl / entry)
        return returns
    except Exception as e:
        print(f"Error loading historical returns: {e}")
        return []


def bootstrap_edge_cv(returns: List[float], n_bootstrap: int = BOOTSTRAP_ITERATIONS) -> Tuple[float, float, float]:
    """
    Bootstrap resample returns to estimate edge distribution.
    
    Returns:
        (cv_edge, mean_edge, std_edge)
    """
    if len(returns) < MIN_RESOLVED_FOR_CV:
        return 0.0, 0.0, 0.0
    
    bootstrap_means = []
    n = len(returns)
    
    for _ in range(n_bootstrap):
        # Resample with replacement
        sample = [returns[random.randint(0, n - 1)] for _ in range(n)]
        bootstrap_means.append(sum(sample) / len(sample))
    
    mean_edge = sum(bootstrap_means) / len(bootstrap_means)
    
    if len(bootstrap_means) >= 2:
        variance = sum((x - mean_edge) ** 2 for x in bootstrap_means) / (len(bootstrap_means) - 1)
        std_edge = math.sqrt(variance)
    else:
        std_edge = 0.0
    
    # CV = std / |mean| (use absolute mean to handle negative edges)
    cv_edge = std_edge / abs(mean_edge) if abs(mean_edge) > 0.001 else 1.0
    
    # Clamp CV to reasonable range
    cv_edge = max(CV_FLOOR, min(CV_CAP, cv_edge))
    
    return cv_edge, mean_edge, std_edge


def monte_carlo_drawdown(
    returns: List[float],
    kelly_fraction: float,
    n_paths: int = MONTE_CARLO_PATHS,
    initial_balance: float = 1.0,
) -> Dict[str, float]:
    """
    Simulate equity curves via Monte Carlo path resampling.
    
    Returns dict with drawdown statistics.
    """
    if len(returns) < MIN_RESOLVED_FOR_CV:
        return {"p50_dd": 0, "p95_dd": 0, "p99_dd": 0, "paths": 0}
    
    max_drawdowns = []
    n = len(returns)
    path_length = max(n, 50)  # Simulate at least 50 trades
    
    for _ in range(n_paths):
        balance = initial_balance
        peak = initial_balance
        max_dd = 0.0
        
        for _ in range(path_length):
            # Random return from historical distribution
            ret = returns[random.randint(0, n - 1)]
            # Apply Kelly-sized bet
            pnl = balance * kelly_fraction * ret
            balance += pnl
            balance = max(balance, 0.01)  # Floor at near-zero
            
            peak = max(peak, balance)
            dd = (peak - balance) / peak
            max_dd = max(max_dd, dd)
        
        max_drawdowns.append(max_dd)
    
    max_drawdowns.sort()
    
    p50_idx = int(n_paths * 0.50)
    p95_idx = int(n_paths * 0.95)
    p99_idx = int(n_paths * 0.99)
    
    return {
        "p50_dd": max_drawdowns[p50_idx],
        "p95_dd": max_drawdowns[p95_idx],
        "p99_dd": max_drawdowns[p99_idx],
        "mean_dd": sum(max_drawdowns) / len(max_drawdowns),
        "paths": n_paths,
    }


def calculate_cv_kelly_haircut(
    kelly_raw: float,
    db_path: str = "storage/shadow_trades.db",
) -> Dict[str, Any]:
    """
    Apply CV uncertainty haircut + Monte Carlo drawdown check to Kelly fraction.
    
    Returns:
        Dict with adjusted kelly, CV, drawdown stats, and reasoning
    """
    returns = get_historical_returns(db_path)
    n_resolved = len(returns)
    
    result = {
        "kelly_raw": kelly_raw,
        "kelly_adjusted": kelly_raw,
        "cv_edge": 0.0,
        "cv_haircut": 0.0,
        "n_resolved": n_resolved,
        "monte_carlo": None,
        "adjustments": [],
    }
    
    if n_resolved < MIN_RESOLVED_FOR_CV:
        result["adjustments"].append(
            f"Insufficient data ({n_resolved}/{MIN_RESOLVED_FOR_CV} resolved). "
            f"Using conservative 30% haircut as default."
        )
        result["cv_haircut"] = 0.30
        result["kelly_adjusted"] = kelly_raw * 0.70
        return result
    
    # Step 1: Bootstrap CV estimation
    cv_edge, mean_edge, std_edge = bootstrap_edge_cv(returns)
    result["cv_edge"] = round(cv_edge, 4)
    result["mean_edge"] = round(mean_edge, 4)
    result["std_edge"] = round(std_edge, 4)
    
    # Step 2: Apply CV haircut
    # f_empirical = f_kelly × (1 - CV_edge)
    cv_adjusted_kelly = kelly_raw * (1 - cv_edge)
    result["cv_haircut"] = round(cv_edge, 4)
    result["adjustments"].append(
        f"CV haircut: {cv_edge:.1%} (mean_edge={mean_edge:.4f}, std={std_edge:.4f}, "
        f"n={n_resolved}). Kelly {kelly_raw:.4f} → {cv_adjusted_kelly:.4f}"
    )
    
    # Step 3: Monte Carlo drawdown check
    mc = monte_carlo_drawdown(returns, cv_adjusted_kelly)
    result["monte_carlo"] = mc
    
    if mc["p95_dd"] > MAX_DRAWDOWN_95TH:
        # Further reduce sizing to bring 95th percentile DD under threshold
        # Binary search for safe Kelly
        lo, hi = 0.0, cv_adjusted_kelly
        safe_kelly = lo
        for _ in range(10):  # 10 iterations of binary search
            mid = (lo + hi) / 2
            mc_test = monte_carlo_drawdown(returns, mid, n_paths=1000)  # Fewer paths for speed
            if mc_test["p95_dd"] <= MAX_DRAWDOWN_95TH:
                safe_kelly = mid
                lo = mid
            else:
                hi = mid
        
        result["adjustments"].append(
            f"MC drawdown override: p95={mc['p95_dd']:.1%} > {MAX_DRAWDOWN_95TH:.0%} threshold. "
            f"Kelly {cv_adjusted_kelly:.4f} → {safe_kelly:.4f}"
        )
        cv_adjusted_kelly = safe_kelly
        # Rerun full MC with final kelly
        result["monte_carlo"] = monte_carlo_drawdown(returns, cv_adjusted_kelly)
    else:
        result["adjustments"].append(
            f"MC drawdown OK: p50={mc['p50_dd']:.1%}, p95={mc['p95_dd']:.1%}, "
            f"p99={mc['p99_dd']:.1%} (threshold={MAX_DRAWDOWN_95TH:.0%})"
        )
    
    result["kelly_adjusted"] = round(cv_adjusted_kelly, 4)
    
    return result


# Quick test
if __name__ == "__main__":
    print("CV Kelly Haircut Test")
    print("=" * 60)
    
    # Test with synthetic data
    random.seed(42)
    
    # Simulate a ~55% WR trader with variable returns
    fake_returns = []
    for _ in range(50):
        if random.random() < 0.55:
            fake_returns.append(random.uniform(0.3, 1.5))  # Win
        else:
            fake_returns.append(-1.0)  # Lose entry price
    
    cv, mean_e, std_e = bootstrap_edge_cv(fake_returns)
    print(f"CV: {cv:.4f}, Mean edge: {mean_e:.4f}, Std: {std_e:.4f}")
    
    raw_kelly = 0.20
    adjusted = raw_kelly * (1 - cv)
    print(f"Kelly: {raw_kelly:.4f} → {adjusted:.4f} ({cv:.1%} haircut)")
    
    mc = monte_carlo_drawdown(fake_returns, adjusted)
    print(f"MC Drawdown: p50={mc['p50_dd']:.1%}, p95={mc['p95_dd']:.1%}, p99={mc['p99_dd']:.1%}")
    
    print("\n--- Live data test ---")
    result = calculate_cv_kelly_haircut(0.20)
    for k, v in result.items():
        if k != "monte_carlo":
            print(f"  {k}: {v}")
    if result["monte_carlo"]:
        print(f"  MC: {result['monte_carlo']}")
