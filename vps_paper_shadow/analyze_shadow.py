"""
Post-shadow analysis: compute IC of each hypothesis against bet_won.

Runs after the paper-shadow window has accumulated rows in theta_shadow_log.
Joins with signal_predictions to get resolved outcomes. Emits a JSON report
with per-hypothesis IC + rolling-window stability + verdict on whether H2
out-performs H0 by a meaningful margin.

Usage:
  python3 analyze_shadow.py [--db /path/to/shadow_trades.db]
                            [--min-n 100]
                            [--out report.json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
from scipy import stats


def fetch_paired(conn: sqlite3.Connection, source: str = "mispriced_category") -> list[dict]:
    """Pull every theta_shadow_log row that has a resolved market outcome."""
    conn.row_factory = sqlite3.Row
    for required in ("theta_shadow_log", "signal_predictions"):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (required,),
        ).fetchone()
        if not exists:
            raise RuntimeError(
                f"required table '{required}' missing — "
                "run shadow_runner.py first or verify DB path"
            )
    rows = conn.execute(
        """
        SELECT tsl.snapshot_id, tsl.market_id, tsl.snapshot_ts,
               tsl.h0_confidence, tsl.h1_confidence, tsl.h2_confidence,
               tsl.theta_score, sp.outcome
        FROM theta_shadow_log tsl
        JOIN (
            SELECT market_id, MAX(outcome) AS outcome
            FROM signal_predictions
            WHERE source = ? AND resolved = 1
            GROUP BY market_id
        ) sp ON tsl.market_id = sp.market_id
        ORDER BY tsl.snapshot_ts
        """,
        (source,),
    ).fetchall()
    return [dict(r) for r in rows]


def hypothesis_ic(rows: list[dict], conf_col: str) -> dict:
    """Spearman IC against bet_won = 1 - outcome."""
    if not rows:
        return {"n": 0, "ic": None}
    conf = np.array([r[conf_col] for r in rows])
    bet_won = np.array([1 - r["outcome"] for r in rows])
    if np.unique(bet_won).size < 2 or np.unique(conf).size < 2:
        return {"n": len(rows), "ic": None, "reason": "constant input"}
    rho = stats.spearmanr(conf, bet_won).statistic
    return {"n": len(rows), "ic": float(rho) if np.isfinite(rho) else None}


def verdict(h0: dict, h2: dict, min_delta: float = 0.10) -> str:
    """Pass H2 if its IC is at least min_delta above H0's IC."""
    if h0["ic"] is None or h2["ic"] is None:
        return "insufficient_data"
    delta = h2["ic"] - h0["ic"]
    if delta >= min_delta and h2["ic"] > 0:
        return f"H2 wins by {delta:+.3f} — deploy candidate"
    if h2["ic"] > 0 and h0["ic"] < 0:
        return f"H2 positive, H0 negative — deploy candidate ({delta:+.3f})"
    return f"H2 - H0 = {delta:+.3f} (below {min_delta} threshold)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--min-n", type=int, default=100, help="Minimum paired rows to compute IC")
    ap.add_argument("--out", default=None, help="Write JSON report here")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db, timeout=30)
    try:
        rows = fetch_paired(conn)
    finally:
        conn.close()

    report = {
        "n_paired_rows": len(rows),
        "n_distinct_markets": len({r["market_id"] for r in rows}),
        "first_ts": rows[0]["snapshot_ts"] if rows else None,
        "last_ts": rows[-1]["snapshot_ts"] if rows else None,
    }

    if len(rows) < args.min_n:
        report["status"] = f"insufficient_data — need {args.min_n}, have {len(rows)}"
        print(json.dumps(report, indent=2))
        if args.out:
            Path(args.out).write_text(json.dumps(report, indent=2))
        return 0

    report["hypotheses"] = {
        "H0_original": hypothesis_ic(rows, "h0_confidence"),
        "H1_drop_theta": hypothesis_ic(rows, "h1_confidence"),
        "H2_invert_theta": hypothesis_ic(rows, "h2_confidence"),
    }
    report["verdict"] = verdict(report["hypotheses"]["H0_original"], report["hypotheses"]["H2_invert_theta"])

    print(json.dumps(report, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
