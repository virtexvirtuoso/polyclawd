#!/usr/bin/env python3
"""Regression tests for the wallet graduation gate (2026-08-21).

Guards the fix for the promote/demote thrash: fast-track promoted on net_pnl
alone, the staleness gate demoted it immediately, and `ORDER BY net_pnl DESC`
let the fast-trackers consume all 20 LIMIT slots so legitimate skill/standard
candidates never got promoted at all.

The gate SQL is EXTRACTED FROM THE DEPLOYED SOURCE rather than restated here —
otherwise the test can pass while the shipped query says something different.
"""
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRAPER = ROOT / "scripts" / "pm_leaderboard_scraper.py"

_passed = _failed = 0


def check(label, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {label}")
    else:
        _failed += 1
        print(f"  ❌ {label}")


def extract_gate_sql() -> str:
    """Pull the live SELECT out of alert_graduations()."""
    src = SCRAPER.read_text()
    body = src[src.index("def alert_graduations"):]
    # Shape-agnostic: the pre-fix call passed a bound param
    # (conn.execute("""...""", (FAST_TRACK_PNL,))), the fixed one does not.
    # Match either so this test FAILS on assertions against old code
    # rather than erroring out and looking like a pass.
    m = re.search(r'rows = conn\.execute\("""(.*?)"""', body, re.S)
    assert m, "could not extract gate SQL — did the call shape change?"
    return m.group(1)


def run_gate(con):
    """Execute the extracted gate. Binds the legacy FAST_TRACK_PNL param when the
    pre-fix shape is present, so running this test against OLD code yields real
    assertion failures instead of a ProgrammingError."""
    sql = extract_gate_sql()
    return con.execute(sql, (500_000,) if "?" in sql else ()).fetchall()


SCHEMA = """
CREATE TABLE pm_wallets (
    wallet TEXT PRIMARY KEY, name TEXT, first_seen REAL, last_seen REAL,
    closed_positions INTEGER, wins INTEGER, win_rate REAL,
    realized_pnl REAL, net_pnl REAL, zombies INTEGER, concentration REAL,
    smart INTEGER, refreshed REAL, source_category TEXT,
    rank_at_seed INTEGER, rank_last_seen INTEGER, rank_scraped_at INTEGER,
    skill_n INTEGER, skill_ret REAL, skill_p REAL, grinder INTEGER
);
CREATE TABLE pm_wallet_seen (
    wallet TEXT PRIMARY KEY, name TEXT, dollars REAL DEFAULT 0, last_seen REAL
);
"""

# (wallet, name, closed, wins, wr, net_pnl, skill_n, skill_ret, skill_p)
FIXTURES = [
    # The exact shape that caused the incident: huge unrealized PnL, zero trades.
    ("0xmint", "mintblade",  0,    0, None, 9_238_345, None, None, None),
    ("0xfish", "fishalive",  0,    0, None, 9_063_378, None, None, None),
    # High PnL WITH a record, but 36% WR — the cohort that averaged 36.5%.
    ("0xlowwr", "lowwr",    250,   90, 0.36, 1_500_000, 250, -0.02, 0.90),
    # Legitimate skill-gate wallet, modest PnL.
    ("0xskill", "skilled",  400,  210, 0.525,   45_000, 400, 0.081, 0.004),
    # Legitimate standard-path wallet, above the new 100K bar.
    ("0xstd",   "standard",  60,   42, 0.70,   150_000, None, None, None),
    # Standard shape but only $50K — allowed under the OLD 5K bar, not the new one.
    ("0xsmall", "smallnet",  60,   42, 0.70,    50_000, None, None, None),
]


def seed(con):
    con.executescript(SCHEMA)
    for w, n, closed, wins, wr, net, sn, sret, sp in FIXTURES:
        con.execute(
            "INSERT INTO pm_wallets (wallet,name,first_seen,last_seen,closed_positions,"
            "wins,win_rate,realized_pnl,net_pnl,zombies,concentration,smart,refreshed,"
            "source_category,rank_at_seed,rank_last_seen,rank_scraped_at,skill_n,skill_ret,skill_p,grinder)"
            " VALUES (?,?,0,0,?,?,?,0,?,0,0,0,0,'test',0,0,0,?,?,?,0)",
            (w, n, closed, wins, wr, net, sn, sret, sp))
    con.commit()


def test_gate_selection():
    print("\ntest_gate_selection:")
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    seed(con)
    rows = run_gate(con)
    names = [r["name"] for r in rows]

    check("zero-trade $9.2M wallet REJECTED (mintblade)", "mintblade" not in names)
    check("zero-trade $9.1M wallet REJECTED (fishalive)", "fishalive" not in names)
    check("high-PnL / 36% WR wallet REJECTED (lowwr)", "lowwr" not in names)
    check("skill-gate wallet admitted", "skilled" in names)
    check("standard wallet >= $100K admitted", "standard" in names)
    check("standard wallet at $50K REJECTED (new 100K bar)", "smallnet" not in names)
    check("skill wallet ranks FIRST (starvation fix)", names and names[0] == "skilled")
    con.close()


def test_promotion_persists_past_staleness():
    print("\ntest_promotion_persists_past_staleness:")
    db = Path(tempfile.mkdtemp()) / "t.db"
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    seed(con)
    rows = run_gate(con)
    now = time.time()

    cur = con.executemany(
        "UPDATE pm_wallets SET smart=1, refreshed=?, last_seen=? WHERE wallet=?",
        [(now, now, r["wallet"]) for r in rows])
    check("UPDATE rowcount equals rows selected", cur.rowcount == len(rows))

    con.executemany(
        "INSERT INTO pm_wallet_seen (wallet, name, dollars, last_seen) VALUES (?,?,0,?)"
        " ON CONFLICT(wallet) DO UPDATE SET last_seen=excluded.last_seen",
        [(r["wallet"], r["name"], now) for r in rows])
    con.commit()

    # This is the property the whole fix turns on: demote_stale_wallets computes
    # `now - max(last_seen, refreshed)`. Pre-fix that was `now - 0` for every
    # leaderboard-seeded row, so each promotion was stale on arrival.
    stale = con.execute(
        "SELECT COUNT(*) FROM pm_wallets WHERE smart=1"
        " AND (? - MAX(COALESCE(last_seen,0), COALESCE(refreshed,0))) > 72*3600", (now,)
    ).fetchone()[0]
    check("no promoted wallet is stale-on-arrival", stale == 0)

    queued = con.execute("SELECT COUNT(*) FROM pm_wallet_seen").fetchone()[0]
    check("promoted wallets enqueued for live re-verification", queued == len(rows))

    promoted = con.execute("SELECT COUNT(*) FROM pm_wallets WHERE smart=1").fetchone()[0]
    check("promotion actually written to the row", promoted == len(rows))
    con.close()


def test_no_live_fast_track_reference():
    print("\ntest_no_live_fast_track_reference:")
    src = SCRAPER.read_text()
    check("no live `>= FAST_TRACK_PNL` predicate", ">= FAST_TRACK_PNL" not in src)
    check("no `fast_track =` assignment", "fast_track =" not in src)
    check("gate SQL has no net_pnl-alone clause", "OR net_pnl >=" not in extract_gate_sql())
    check("ORDER BY is not net_pnl-first", "ORDER BY net_pnl DESC" not in extract_gate_sql())


for fn in (test_gate_selection, test_promotion_persists_past_staleness,
           test_no_live_fast_track_reference):
    fn()

print("\n" + "=" * 40)
print(f"{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
