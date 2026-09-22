#!/usr/bin/env python3
"""Refire re-audit + shadow backlog triage (durability plan Tasks 3-4, 2026-09-21).

Vault plan: Plans/Alert-Durability-And-Refire-Reaudit-2026-09-21.md

Pre-registered verdict rule (written 2026-09-21 BEFORE results were computed):
  LIFT the June refire exclusion iff
    (a) the wallet-clustered 95% CI of PnL/$ excludes 0, AND
    (b) >= 3 consecutive recent months have positive mean PnL/$.
  Otherwise UPHOLD the exclusion.
  PnL/$ formula (per contract, per share): WIN -> 1 - price_at_alert,
  LOSS -> -price_at_alert.

Usage:
    venv/bin/python3 scripts/refire_reaudit.py                  # stats + grader check + triage sample
    venv/bin/python3 scripts/refire_reaudit.py --grader-n 50
    venv/bin/python3 scripts/refire_reaudit.py --triage-full    # dry-run settler over ALL unresolved (slow, CLOB)
    venv/bin/python3 scripts/refire_reaudit.py --apply          # after triage: run the real resolver pass
"""
import argparse
import json
import random
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "storage" / "shadow_trades.db"


def pnl(price: float, result: str) -> float:
    return (1.0 - price) if result == "WIN" else -price


def refire_stats(con) -> dict:
    rows = con.execute(
        "SELECT id, wallet, direction, price_at_alert, outcome_result, clv, ts_alert "
        "FROM smart_wallet_shadows WHERE alert_type='refire' AND resolved=1"
    ).fetchall()
    if not rows:
        return {"error": "no resolved refires"}
    pnls = [pnl(r["price_at_alert"] or 0.0, r["outcome_result"]) for r in rows]
    wins = sum(1 for r in rows if r["outcome_result"] == "WIN")
    clvs = [r["clv"] for r in rows if r["clv"] is not None]
    by_wallet = defaultdict(list)
    for r, p in zip(rows, pnls):
        by_wallet[r["wallet"]].append(p)
    rng = random.Random(42)
    wallets = list(by_wallet)
    means = []
    for _ in range(10000):
        pooled = [p for w in rng.choices(wallets, k=len(wallets)) for p in by_wallet[w]]
        means.append(sum(pooled) / len(pooled))
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[min(len(means) - 1, int(0.975 * len(means)))]
    months = defaultdict(lambda: [0, 0.0, 0])  # wins, pnl, n
    for r, p in zip(rows, pnls):
        m = time.strftime("%Y-%m", time.gmtime(r["ts_alert"]))
        months[m][0] += 1 if r["outcome_result"] == "WIN" else 0
        months[m][1] += p
        months[m][2] += 1
    monthly = {
        m: {"n": v[2], "wr": round(v[0] / v[2], 3), "pnl_per_share": round(v[1] / v[2], 4)}
        for m, v in sorted(months.items())
    }
    return {
        "n": len(rows),
        "wins": wins,
        "wr": round(wins / len(rows), 4),
        "pnl_per_share_point": round(sum(pnls) / len(pnls), 4),
        "cluster_ci95": [round(lo, 4), round(hi, 4)],
        "mean_clv": round(sum(clvs) / len(clvs), 4) if clvs else None,
        "n_clv": len(clvs),
        "n_non_buy": sum(1 for r in rows if r["direction"] != "BUY"),
        "monthly": monthly,
    }


def grader_check(con, n_sample: int) -> dict:
    """Re-grade a random sample of resolved refires with the live CLOB settler
    and measure agreement with stored labels (validation-degenerate rule)."""
    from scripts.smart_wallet_alert import settle_via_market_resolution

    rows = con.execute(
        "SELECT * FROM smart_wallet_shadows WHERE alert_type='refire' AND resolved=1 "
        "ORDER BY RANDOM() LIMIT ?",
        (n_sample,),
    ).fetchall()
    checked = agree = 0
    disagreements = []
    for r in rows:
        d = dict(r)
        try:
            settled = settle_via_market_resolution(d)
        except Exception as exc:
            disagreements.append({"id": d["id"], "error": str(exc)[:80]})
            time.sleep(0.25)
            continue
        if settled is None:
            continue
        checked += 1
        fresh = "WIN" if settled > 0.5 else "LOSS"
        if fresh == d["outcome_result"]:
            agree += 1
        else:
            disagreements.append({"id": d["id"], "stored": d["outcome_result"], "fresh": fresh})
        time.sleep(0.25)
    rate = round(agree / checked, 3) if checked else None
    return {"sampled": len(rows), "checked": checked, "agree": agree, "agreement": rate, "disagreements": disagreements[:10]}


def triage(con, full: bool, sample_n: int) -> dict:
    """Dry-run the production settler over unresolved rows; count resolvable."""
    from scripts.smart_wallet_alert import settle_via_market_resolution

    if sample_n and not full:
        rows = con.execute(
            "SELECT * FROM smart_wallet_shadows WHERE resolved=0 ORDER BY RANDOM() LIMIT ?",
            (sample_n,),
        ).fetchall()
    else:
        rows = con.execute("SELECT * FROM smart_wallet_shadows WHERE resolved=0").fetchall()
    resolvable = []
    errors = 0
    for r in rows:
        d = dict(r)
        try:
            settled = settle_via_market_resolution(d)
        except Exception:
            errors += 1
            time.sleep(0.25)
            continue
        if settled is not None:
            resolvable.append({"id": d["id"], "alert_type": d["alert_type"], "settled": settled})
        time.sleep(0.25)
    return {
        "mode": "full" if full else f"sample({sample_n})",
        "examined": len(rows),
        "resolvable": len(resolvable),
        "errors": errors,
        "examples": resolvable[:10],
    }


def apply_resolver(con) -> dict:
    from scripts.smart_wallet_alert import resolve_shadows, settle_via_market_resolution

    before = con.execute("SELECT COUNT(*) FROM smart_wallet_shadows WHERE resolved=0").fetchone()[0]
    n = resolve_shadows(con, settle_via_market_resolution)
    after = con.execute("SELECT COUNT(*) FROM smart_wallet_shadows WHERE resolved=0").fetchone()[0]
    return {"before": before, "newly_resolved": n, "after": after}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grader-n", type=int, default=30)
    ap.add_argument("--triage-sample", type=int, default=30)
    ap.add_argument("--triage-full", action="store_true")
    ap.add_argument("--apply", action="store_true", help="run the real resolver pass (writes grades)")
    ap.add_argument("--skip-grader", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    out = {"refire_stats": refire_stats(con)}
    if not args.skip_grader:
        out["grader_check"] = grader_check(con, args.grader_n)
    out["backlog_triage"] = triage(con, args.triage_full, args.triage_n if hasattr(args, "triage_sample") else args.triage_n)
    if args.apply:
        out["resolver_pass"] = apply_resolver(con)
    con.close()
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()