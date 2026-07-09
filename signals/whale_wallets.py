#!/usr/bin/env python3
"""
Polymarket wallet ledger — graduate anonymous whale fingerprints into
identified, track-record-verified whales.

Every PM trade in the Data API carries proxyWallet + username. The public
positions endpoint returns that wallet's realized PnL per position. So:
queue wallets seen driving flow bursts, refresh their stats every 30 min,
and flag "smart" wallets (enough closed positions, high realized win rate,
positive PnL). The whale scanner boosts any market a smart wallet enters.

Lives in storage/whale_meta.db — separate file from whale_scanner.db because
the scanner holds long write transactions during book phases and same-file
writers recreate `database is locked`.

CLI:
    python3 signals/whale_wallets.py --refresh        # drain the seen-queue
    python3 signals/whale_wallets.py --summary        # ledger overview
"""

import argparse
import json
import logging
import sqlite3
import sys
import os
import time
import urllib.request
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import connect as db_connect  # noqa: E402

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
META_DB_PATH = BASE_DIR / "storage" / "whale_meta.db"
PM_DATA_API = "https://data-api.polymarket.com"

# Smart-wallet criteria (re-weighted 2026-06-12 after the phantom-winrate
# investigation): NET profit is the primary signal — "show me the money".
# Win rate is only a sanity floor at 50%: it filters longshot sprayers whose
# market entries are uninformative (they enter everything), without excluding
# big-net traders over a rounding-error percentage (RN1: 59.3% / +$1.04M net).
SMART_MIN_CLOSED   = 20
SMART_MIN_WIN_RATE = 0.62  # raised from 0.55 (2026-06-25): 500K+ fast-track wallets averaged 38.3% WR — not signal-quality
SMART_MIN_NET      = 100000.0  # $ net profit — elite only for TG alerts (2026-06-20)
REFRESH_TTL_S      = 24 * 3600
QUEUE_MIN_USD      = 100.0   # don't bother tracking wallets below this burst size

# Wallet decay criteria
WALLET_STALE_HOURS = 72        # No activity → demote
WALLET_SLIDING_WR = 0.45       # Last 50 trades below this → demote
WALLET_MIN_TRADES_30D = 5      # Fewer than 5 trades in 30 days → demote


def get_meta_db(path: Optional[Path] = None) -> sqlite3.Connection:
    db_path = Path(path) if path else META_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db_connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pm_wallets (
            wallet TEXT PRIMARY KEY,
            name TEXT,
            first_seen REAL, last_seen REAL,
            closed_positions INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            win_rate REAL,
            realized_pnl REAL,
            smart INTEGER DEFAULT 0,
            refreshed REAL DEFAULT 0
        )""")
    for col, typ in (
        ("net_pnl", "REAL"), ("zombies", "INTEGER"), ("concentration", "REAL"),
        ("source_category", "TEXT"), ("rank_at_seed", "INTEGER"),
        ("rank_last_seen", "INTEGER"), ("rank_scraped_at", "INTEGER"),
        ("is_bot", "INTEGER DEFAULT 0"),
        ("skill_n", "INTEGER"), ("skill_ret", "REAL"), ("skill_p", "REAL"),
    ):
        try:
            conn.execute(f"ALTER TABLE pm_wallets ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass   # column exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pm_wallet_seen (
            wallet TEXT PRIMARY KEY,
            name TEXT,
            dollars REAL DEFAULT 0,
            last_seen REAL
        )""")
    # Migration: add wallet column to whale_follows (existing tables from before 2026-06-15)
    try:
        conn.execute("ALTER TABLE whale_follows ADD COLUMN wallet TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wallet_correlation (
            wallet_a TEXT NOT NULL,
            wallet_b TEXT NOT NULL,
            overlap_pct REAL,
            pair_trades INTEGER,
            agreement_pct REAL,
            updated REAL,
            PRIMARY KEY (wallet_a, wallet_b)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wallet_archetype_pnl (
            wallet TEXT NOT NULL,
            archetype TEXT NOT NULL,
            trades INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            pnl REAL DEFAULT 0,
            concentration REAL DEFAULT 0,
            updated REAL,
            PRIMARY KEY (wallet, archetype)
        )""")
    # Migration: add concentration column to existing wallet_archetype_pnl
    try:
        conn.execute("ALTER TABLE wallet_archetype_pnl ADD COLUMN concentration REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # Exit tracking table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS exit_tracking (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            market TEXT NOT NULL,
            direction INTEGER,
            entry_px REAL,
            exit_px REAL,
            entry_value REAL,
            exit_value REAL,
            realized_pnl REAL,
            exit_type TEXT,
            ts_entry REAL,
            ts_exit REAL,
            updated REAL
        )""")
    conn.commit()
    return conn


def _fetch_json(url: str, timeout: int = 20):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def queue_wallet_seen(conn, wallet: str, name: str, dollars: float):
    """Called by the scanner: remember a wallet that drove a flow burst."""
    if not wallet or dollars < QUEUE_MIN_USD:
        return
    conn.execute(
        "INSERT INTO pm_wallet_seen (wallet, name, dollars, last_seen)"
        " VALUES (?,?,?,?)"
        " ON CONFLICT(wallet) DO UPDATE SET"
        "  dollars = dollars + excluded.dollars,"
        "  name = excluded.name, last_seen = excluded.last_seen",
        (wallet, name or "", dollars, time.time()))


def get_smart_wallets(conn) -> dict:
    """wallet -> {name, win_rate, closed, net_pnl, source_category, is_bot} for all currently-smart wallets."""
    return {r["wallet"]: {"name": r["name"], "win_rate": r["win_rate"],
                          "closed": r["closed_positions"], "net_pnl": r["net_pnl"],
                          "source_category": r["source_category"],
                          "is_bot": r["is_bot"] or 0}
            for r in conn.execute("SELECT * FROM pm_wallets WHERE smart=1")}


POSITIONS_PAGE_CAP = 6   # x500 rows; bounds refresh cost per wallet


def compute_stats(rows: list) -> dict:
    """Outcome-honest record from raw position rows.

    Polymarket only sets realizedPnl on SELL/REDEEM. A losing position held to
    worthless resolution is never redeemed, so its realizedPnl stays ~0 — pure
    realization-counting therefore hides most losses (longshot sprayers read as
    100% winners). Fix: count "zombies" (held positions whose currentValue
    collapsed vs cost basis, no realized pnl) as losses, and judge wallets on
    NET pnl = realized + unrealized cashPnl, not realized alone.
    """
    closed = [p for p in rows if abs(p.get("realizedPnl") or 0) > 0.01]
    wins = sum(1 for p in closed if (p.get("realizedPnl") or 0) > 0)
    realized = sum(p.get("realizedPnl") or 0 for p in rows)
    open_rows = [p for p in rows
                 if (p.get("size") or 0) > 0 and abs(p.get("realizedPnl") or 0) <= 0.01]
    zombies = sum(1 for p in open_rows
                  if (p.get("currentValue") or 0) < 0.01 * max(p.get("initialValue") or 0, 1)
                  and (p.get("initialValue") or 0) > 1)
    unrealized = sum(p.get("cashPnl") or 0 for p in open_rows)
    # Portfolio concentration: max_position_value / total_portfolio_value
    # Only meaningful when total portfolio > $1K
    open_positions = [p for p in rows if (p.get("size") or 0) > 0]
    if open_positions:
        total_value = sum(p.get("currentValue") or 0 for p in open_positions)
        max_value = max(p.get("currentValue") or 0 for p in open_positions)
        concentration = max_value / total_value if total_value > 1000 else 0.0
    else:
        concentration = 0.0
    return {"closed": len(closed) + zombies, "wins": wins,
            "realized": realized, "zombies": zombies,
            "net": realized + unrealized,
            "concentration": concentration}


def fetch_wallet_stats(wallet: str) -> Optional[dict]:
    """Full-history track record from the public positions endpoint."""
    rows = []
    for page in range(POSITIONS_PAGE_CAP):
        d = _fetch_json(f"{PM_DATA_API}/positions?user={wallet}&limit=500&offset={page * 500}")
        if d is None:
            return None if page == 0 else _stats_with_skill(rows)
        rows.extend(d)
        if len(d) < 500:
            break
    return _stats_with_skill(rows)


def _stats_with_skill(rows: list) -> dict:
    st = compute_stats(rows)
    st.update(skill_score(skill_returns(rows)))
    return st


def demote_stale_wallets(conn) -> dict:
    """Demote smart wallets that have gone stale or underperforming."""
    now = time.time()
    smart = conn.execute("SELECT wallet, name, last_seen, refreshed FROM pm_wallets WHERE smart=1").fetchall()
    demoted = 0
    reasons = []

    for row in smart:
        wallet = row["wallet"]
        name = row["name"] or wallet[:10]

        # Check staleness — use refreshed timestamp (when we last verified stats)
        # not last_seen (when we last saw them in flow). Wallets seeded from
        # leaderboard or bulk-promoted won't have organic flow detections.
        # sqlite3.Row doesn't have .get() — use try/except for optional columns
        try:
            refreshed = row["refreshed"] or 0
        except (IndexError, KeyError):
            refreshed = 0
        last_check = max(row["last_seen"] or 0, refreshed)
        if now - last_check > WALLET_STALE_HOURS * 3600:
            conn.execute("UPDATE pm_wallets SET smart=0 WHERE wallet=?", (wallet,))
            demoted += 1
            reasons.append(f"{name}: stale ({int((now - last_check)/3600)}h no activity)")
            continue

        # Fetch recent trades for sliding WR check
        stats = fetch_wallet_stats(wallet)
        if stats is None:
            continue

        # Check sliding WR — but never WR-demote a wallet passing the
        # sign-randomization skill gate: longshot specialists run low WR with
        # strongly positive per-$ returns; WR is the wrong metric for them.
        if stats["closed"] >= 20 and not skill_gate_ok(stats):
            wr = stats["wins"] / stats["closed"]
            if wr < WALLET_SLIDING_WR:
                conn.execute("UPDATE pm_wallets SET smart=0 WHERE wallet=?", (wallet,))
                demoted += 1
                reasons.append(f"{name}: WR {wr:.1%} below {WALLET_SLIDING_WR:.0%} (last {stats['closed']} trades)")
                continue

        # Flow-activity recency. NOTE: pm_wallet_seen holds ONE accumulating row
        # per wallet (PK=wallet), so COUNT(*) is always 0 or 1 — the previous
        # `COUNT(*) < WALLET_MIN_TRADES_30D(=5)` test was UNSATISFIABLE and
        # demoted every smart wallet the instant refresh_wallets promoted it,
        # leaving the roster permanently empty (diagnosed 2026-06-23). Demote
        # only when the last flow sighting is genuinely stale (>30d). Absence of
        # a row is NOT evidence of inactivity — refresh prunes just-processed
        # wallets from pm_wallet_seen.
        seen = conn.execute(
            "SELECT last_seen FROM pm_wallet_seen WHERE wallet=?", (wallet,)
        ).fetchone()
        if seen and seen[0] and (now - seen[0]) > 30 * 86400:
            conn.execute("UPDATE pm_wallets SET smart=0 WHERE wallet=?", (wallet,))
            demoted += 1
            reasons.append(f"{name}: no flow activity in 30d+")

    conn.commit()
    for r in reasons:
        logger.info(f"Demoted: {r}")
    return {"demoted": demoted, "reasons": reasons}


def compute_correlations(conn):
    """Compute pairwise market overlap between all smart wallets."""
    now = time.time()
    wallets = [r["wallet"] for r in conn.execute(
        "SELECT wallet FROM pm_wallets WHERE smart=1").fetchall()]

    for i, wa in enumerate(wallets):
        wa_markets = set(r["market"] for r in conn.execute(
            "SELECT DISTINCT market FROM whale_follows WHERE wallet=?",
            (wa,)).fetchall())
        if not wa_markets:
            continue
        for wb in wallets[i+1:]:
            wb_markets = set(r["market"] for r in conn.execute(
                "SELECT DISTINCT market FROM whale_follows WHERE wallet=?",
                (wb,)).fetchall())
            if not wb_markets:
                continue
            overlap = wa_markets & wb_markets
            if not overlap:
                continue
            overlap_pct = len(overlap) / len(wa_markets)
            if overlap_pct < 0.1:  # skip noise
                continue
            # Count agreement on direction
            agree = 0
            for mkt in overlap:
                a_dir = conn.execute(
                    "SELECT direction FROM whale_follows WHERE wallet=? AND market=? LIMIT 1",
                    (wa, mkt)).fetchone()
                b_dir = conn.execute(
                    "SELECT direction FROM whale_follows WHERE wallet=? AND market=? LIMIT 1",
                    (wb, mkt)).fetchone()
                if a_dir and b_dir and a_dir["direction"] == b_dir["direction"]:
                    agree += 1
            agreement = agree / len(overlap) if overlap else 0
            conn.execute(
                "INSERT INTO wallet_correlation (wallet_a, wallet_b, overlap_pct, pair_trades, agreement_pct, updated)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(wallet_a, wallet_b) DO UPDATE SET"
                "  overlap_pct=excluded.overlap_pct, pair_trades=excluded.pair_trades,"
                "  agreement_pct=excluded.agreement_pct, updated=excluded.updated",
                (wa, wb, round(overlap_pct, 4), len(overlap), round(agreement, 4), now))
    conn.commit()


def compute_wallet_archetype_pnl(conn):
    """Aggregate per-wallet, per-archetype P&L from whale_follows."""
    now = time.time()
    conn.execute("DELETE FROM wallet_archetype_pnl")
    rows = conn.execute("""
        SELECT wallet, archetype, COUNT(*) as trades,
               SUM(CASE WHEN pnl_net > 0 THEN 1 ELSE 0 END) as wins,
               ROUND(SUM(pnl_net), 2) as pnl
        FROM whale_follows
        WHERE done=1 AND wallet IS NOT NULL AND wallet != ''
        GROUP BY wallet, archetype
    """).fetchall()
    for r in rows:
        # Get concentration from pm_wallets
        conc = conn.execute(
            "SELECT concentration FROM pm_wallets WHERE wallet=?",
            (r["wallet"],)
        ).fetchone()
        concentration = conc["concentration"] if conc and conc["concentration"] else 0.0
        conn.execute(
            "INSERT INTO wallet_archetype_pnl (wallet, archetype, trades, wins, pnl, concentration, updated)"
            " VALUES (?,?,?,?,?,?,?)",
            (r["wallet"], r["archetype"], r["trades"], r["wins"], r["pnl"], concentration, now))
    conn.commit()


def _alert_graduation(name: str, stats: dict, wr: float) -> None:
    """Fire Telegram alert when a wallet graduates to smart status."""
    try:
        from scripts.alert_formatter import send_telegram
        net = stats.get("net", stats.get("realized", 0))
        msg = (
            f"🎓 <b>NEW SMART WALLET</b>\n\n"
            f"<b>{name}</b> graduated:\n"
            f"  {stats['closed']} closed | {wr*100:.1f}% WR | ${net:+,.0f} net\n\n"
            f"This wallet now boosts signal scores on markets they enter."
        )
        send_telegram(msg)
    except Exception:
        pass  # Don't crash refresh on alert failure


# ── Sign-randomization skill scoring (Gomez-Cram et al. 2026, SSRN 6617059) ──
# Raw win-rate/PnL graduation is luck-confounded (longshot sprayers, hot streaks).
# The randomization test scores a wallet's actual total probability-point return
# against a null where every bet's side is a coin flip — a wallet's own variance
# widens its own null instead of inflating its score.
SKILL_MIN_N = 30     # resolved positions needed before the skill gate can pass
SKILL_P_MAX = 0.05   # sign-randomization p-value threshold
SKILL_SIMS = 10_000


def skill_returns(rows: list) -> list:
    """Per-position probability-point returns (settled − avgPrice) for RESOLVED
    positions, using the same outcome-honest complete-rule as compute_stats.
    realizedPnl>0 counts as a directional win even when sold early — consistent
    with compute_stats. Open/ambiguous positions and rows without a usable
    avgPrice are skipped."""
    rets = []
    for p in rows:
        try:
            px = float(p.get("avgPrice"))
        except (TypeError, ValueError):
            continue
        if not (0.0 < px < 1.0):
            continue
        rp = p.get("realizedPnl") or 0
        cur = p.get("currentValue") or 0
        init = p.get("initialValue") or 0
        if abs(rp) > 0.01:
            # Realized (sold or redeemed): ACTUAL per-share return in probability
            # points = realizedPnl x avgPrice / cost. Counting any realized win as
            # a full s=1.0 booked a +3c scalp as +0.60 and inflated every
            # high-volume wallet ~+0.4/bet (QA probe 2026-07-06) — that rule would
            # have graduated scalpers wholesale. Redemption still books 1-avgPrice
            # exactly; the sign-flip null stays valid (mirror side = -ret).
            if init <= 0:
                continue
            rets.append(max(-1.0, min(1.0, rp * px / init)))
            continue
        if init > 1 and cur < 0.01 * init:
            rets.append(0.0 - px)   # zombie: held to worthless resolution
        elif init > 1 and cur >= 0.5 * init and p.get("redeemable"):
            rets.append(1.0 - px)   # unredeemed winner
        # else: still open / ambiguous — skip
    return rets


def skill_score(rets: list, sims: int = SKILL_SIMS) -> dict:
    """p = P(null total ≥ actual total) under random sign flips. Deterministic
    (fixed seed). Falls back to the normal approximation if numpy is missing."""
    n = len(rets)
    if n == 0:
        return {"skill_n": 0, "skill_ret": None, "skill_p": None}
    actual = float(sum(rets))
    mean = actual / n
    try:
        import numpy as np

        r = np.asarray(rets, dtype=np.float64)
        # Deterministic per wallet but DECORRELATED across wallets — a single
        # shared seed reuses one null sample fleet-wide, so its Monte-Carlo
        # error biases every wallet's p the same direction (QA 2026-07-06
        # measured 12% empirical FP at nominal 5% with 2k shared sims).
        seed = (n * 1_000_003 + int(abs(actual) * 1e9)) % (2**63 - 1)
        rng = np.random.default_rng(seed)
        ge = done = 0
        block = max(1, min(sims, 20_000_000 // max(n, 1)))
        while done < sims:
            b = min(block, sims - done)
            signs = rng.integers(0, 2, size=(b, n)) * 2 - 1
            ge += int((signs @ r >= actual).sum())
            done += b
        p = (ge + 1) / (sims + 1)
    except Exception:
        import math

        sd = math.sqrt(sum(x * x for x in rets)) or 1e-9
        p = 0.5 * math.erfc((actual / sd) / math.sqrt(2))
    return {"skill_n": n, "skill_ret": mean, "skill_p": p}


def skill_gate_ok(stats: dict) -> bool:
    """True when the wallet passes the luck-controlled skill gate."""
    return ((stats.get("skill_n") or 0) >= SKILL_MIN_N
            and stats.get("skill_p") is not None
            and stats["skill_p"] <= SKILL_P_MAX
            and (stats.get("skill_ret") or 0) > 0)


def is_smart(stats: dict) -> bool:
    if skill_gate_ok(stats):
        return True  # sign-randomization path — WR/PnL thresholds don't apply
    if stats["closed"] < SMART_MIN_CLOSED:
        return False
    wr = stats["wins"] / stats["closed"]
    return wr >= SMART_MIN_WIN_RATE and stats.get("net", stats["realized"]) >= SMART_MIN_NET


def refresh_wallets(conn, cap: int = 60) -> dict:
    """Drain the seen-queue (biggest flow first) + re-refresh stale smart
    wallets. Returns counters for logging."""
    now = time.time()
    refreshed = promoted = demoted = 0

    queued = conn.execute(
        "SELECT s.wallet, s.name FROM pm_wallet_seen s"
        " LEFT JOIN pm_wallets w ON w.wallet = s.wallet"
        " WHERE w.wallet IS NULL OR w.refreshed < ?"
        " ORDER BY s.dollars DESC LIMIT ?", (now - REFRESH_TTL_S, cap)).fetchall()
    stale_smart = conn.execute(
        "SELECT wallet, name FROM pm_wallets WHERE smart=1 AND refreshed < ?"
        " LIMIT ?", (now - REFRESH_TTL_S, max(0, cap - len(queued)))).fetchall()

    for row in list(queued) + list(stale_smart):
        stats = fetch_wallet_stats(row["wallet"])
        if stats is None:
            continue
        wr = stats["wins"] / stats["closed"] if stats["closed"] else None
        smart = 1 if is_smart(stats) else 0
        prev = conn.execute("SELECT smart FROM pm_wallets WHERE wallet=?",
                            (row["wallet"],)).fetchone()
        was_smart = prev["smart"] if prev is not None else 0
        if prev is not None:
            promoted += 1 if (smart and not was_smart) else 0
            demoted += 1 if (was_smart and not smart) else 0
        elif smart:
            promoted += 1
        # Graduation alerts silenced (2026-06-20) — Mr. V only wants elite move/consensus alerts
        # if smart and not was_smart:
        #     _alert_graduation(row["name"] or row["wallet"][:12], stats, wr)
        conn.execute(
            "INSERT INTO pm_wallets (wallet, name, first_seen, last_seen,"
            " closed_positions, wins, win_rate, realized_pnl, net_pnl, zombies,"
            " concentration, smart, refreshed, skill_n, skill_ret, skill_p)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(wallet) DO UPDATE SET"
            "  name=excluded.name, last_seen=excluded.last_seen,"
            "  closed_positions=excluded.closed_positions, wins=excluded.wins,"
            "  win_rate=excluded.win_rate, realized_pnl=excluded.realized_pnl,"
            "  net_pnl=excluded.net_pnl, zombies=excluded.zombies,"
            "  concentration=excluded.concentration,"
            "  smart=excluded.smart, refreshed=excluded.refreshed,"
            "  skill_n=excluded.skill_n, skill_ret=excluded.skill_ret,"
            "  skill_p=excluded.skill_p",
            (row["wallet"], row["name"] or "", now, now,
             stats["closed"], stats["wins"], wr, stats["realized"],
             stats.get("net"), stats.get("zombies"),
             stats.get("concentration", 0.0), smart, now,
             stats.get("skill_n"), stats.get("skill_ret"), stats.get("skill_p")))
        refreshed += 1

    conn.execute("DELETE FROM pm_wallet_seen WHERE wallet IN"
                 " (SELECT wallet FROM pm_wallets WHERE refreshed >= ?)",
                 (now - 60,))

    # Demote stale/underperforming wallets
    demotion = demote_stale_wallets(conn)

    # Compute wallet correlations and archetype P&L
    compute_correlations(conn)
    compute_wallet_archetype_pnl(conn)

    # Track exits for smart wallets
    exits_logged = track_exits(conn)

    conn.commit()
    return {"refreshed": refreshed, "promoted": promoted, "demoted": demoted,
            "queue_left": conn.execute("SELECT COUNT(*) FROM pm_wallet_seen").fetchone()[0],
            "stale_demoted": demotion["demoted"]}


def track_exits(conn) -> dict:
    """Track position exits for smart wallets.

    On each refresh cycle, fetches current positions for all smart wallets
    and compares against previous snapshot stored in exit_tracking.
    Logs profit_taken, zombie, or holding status.
    """
    now = time.time()
    smart_wallets = [r["wallet"] for r in conn.execute(
        "SELECT wallet FROM pm_wallets WHERE smart=1").fetchall()]
    if not smart_wallets:
        return {"tracked": 0, "exits": 0}

    exits_logged = 0
    tracked = 0

    for wallet in smart_wallets:
        stats = fetch_wallet_stats(wallet)
        if stats is None:
            continue

        # Get all positions from the API (open + closed)
        rows = []
        for page in range(POSITIONS_PAGE_CAP):
            d = _fetch_json(f"{PM_DATA_API}/positions?user={wallet}&limit=500&offset={page * 500}")
            if d is None:
                break
            rows.extend(d)
            if len(d) < 500:
                break

        if not rows:
            continue

        tracked += 1

        # Get previously tracked positions for this wallet
        prev_positions = {
            r["market"]: r for r in conn.execute(
                "SELECT * FROM exit_tracking WHERE wallet=? AND exit_type='holding'",
                (wallet,)
            ).fetchall()
        }

        current_markets = set()
        for p in rows:
            market = p.get("market") or p.get("condition_id") or ""
            if not market:
                continue
            current_markets.add(market)

            # Check if this is a closed position (size=0, realizedPnl set)
            realized_pnl = p.get("realizedPnl") or 0
            size = p.get("size") or 0

            if size == 0 and abs(realized_pnl) > 0.01:
                # This position was closed — check if we already logged it
                existing = conn.execute(
                    "SELECT id FROM exit_tracking WHERE wallet=? AND market=? AND exit_type IN ('profit_taken', 'zombie')",
                    (wallet, market)
                ).fetchone()
                if existing:
                    continue  # already logged

                entry_value = p.get("initialValue") or 0
                exit_value = p.get("currentValue") or 0
                entry_px = p.get("avgEntryPrice") or 0
                exit_px = p.get("avgExitPrice") or 0
                direction = 1 if (p.get("side") or "BUY") == "BUY" else -1

                exit_type = "profit_taken" if realized_pnl > 0 else "zombie"
                conn.execute(
                    "INSERT INTO exit_tracking (wallet, market, direction, entry_px, exit_px,"
                    " entry_value, exit_value, realized_pnl, exit_type, ts_entry, ts_exit, updated)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (wallet, market, direction, entry_px, exit_px,
                     entry_value, exit_value, realized_pnl, exit_type,
                     p.get("openTimestamp") or (now - 86400), now, now))
                exits_logged += 1

            elif size > 0:
                # Open position — update holding status
                existing = conn.execute(
                    "SELECT id FROM exit_tracking WHERE wallet=? AND market=? AND exit_type='holding'",
                    (wallet, market)
                ).fetchone()
                if not existing:
                    direction = 1 if (p.get("side") or "BUY") == "BUY" else -1
                    conn.execute(
                        "INSERT INTO exit_tracking (wallet, market, direction, entry_px, exit_px,"
                        " entry_value, exit_value, realized_pnl, exit_type, ts_entry, ts_exit, updated)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (wallet, market, direction,
                         p.get("avgEntryPrice") or 0, 0,
                         p.get("initialValue") or 0, p.get("currentValue") or 0,
                         0, "holding",
                         p.get("openTimestamp") or now, now, now))
                else:
                    # Update current value
                    conn.execute(
                        "UPDATE exit_tracking SET exit_value=?, updated=? WHERE id=?",
                        (p.get("currentValue") or 0, now, existing["id"]))

        # Mark positions that were holding but are no longer in current set
        for market, prev in prev_positions.items():
            if market not in current_markets:
                # Position disappeared without realizedPnl — likely zombie
                conn.execute(
                    "UPDATE exit_tracking SET exit_type='zombie', ts_exit=?, updated=? WHERE id=?",
                    (now, now, prev["id"]))
                exits_logged += 1

    conn.commit()
    return {"tracked": tracked, "exits": exits_logged}


def summary(conn) -> str:
    total = conn.execute("SELECT COUNT(*) FROM pm_wallets").fetchone()[0]
    smart = conn.execute("SELECT COUNT(*) FROM pm_wallets WHERE smart=1").fetchone()[0]
    q = conn.execute("SELECT COUNT(*) FROM pm_wallet_seen").fetchone()[0]
    lines = [f"ledger: {total} wallets tracked, {smart} smart, {q} queued"]
    for r in conn.execute(
            "SELECT name, wallet, win_rate, closed_positions, realized_pnl"
            " FROM pm_wallets WHERE smart=1 ORDER BY realized_pnl DESC LIMIT 15"):
        lines.append(f"  {r['name'] or r['wallet'][:10]}: "
                     f"{r['win_rate']:.0%} over {r['closed_positions']} closed, "
                     f"${r['realized_pnl']:,.0f} realized")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--correlations", action="store_true")
    parser.add_argument("--wallet-archetypes", action="store_true")
    parser.add_argument("--exits", action="store_true")
    args = parser.parse_args()
    conn = get_meta_db()
    if args.refresh:
        print(refresh_wallets(conn))
    if args.correlations:
        rows = conn.execute(
            "SELECT * FROM wallet_correlation WHERE overlap_pct > 0.3 ORDER BY overlap_pct DESC LIMIT 20"
        ).fetchall()
        if rows:
            print("\nWallet correlations (overlap > 30%):")
            for r in rows:
                print(f"  {r['wallet_a'][:12]} ↔ {r['wallet_b'][:12]}: {r['overlap_pct']:.0%} overlap, {r['agreement_pct']:.0%} agreement")
        else:
            print("No correlations computed yet. Run --refresh first.")
    if args.wallet_archetypes:
        rows = conn.execute(
            "SELECT w.name, a.archetype, a.trades, a.wins, a.pnl"
            " FROM wallet_archetype_pnl a JOIN pm_wallets w ON w.wallet = a.wallet"
            " WHERE a.trades >= 5 ORDER BY a.pnl DESC LIMIT 30"
        ).fetchall()
        if rows:
            print("\nWallet archetype P&L (min 5 trades):")
            for r in rows:
                wr = r["wins"] / r["trades"] if r["trades"] else 0
                print(f"  {r['name'] or r['wallet'][:10]:20s} | {r['archetype']:15s} | {r['trades']:>4} trades | {wr:.0%} WR | ${r['pnl']:>8,.2f}")
        else:
            print("No wallet archetype P&L data yet. Run --refresh first.")
    if args.exits:
        rows = conn.execute(
            "SELECT * FROM exit_tracking WHERE exit_type IN ('profit_taken', 'zombie') ORDER BY ts_exit DESC LIMIT 20"
        ).fetchall()
        if rows:
            print("\nRecent exits (last 20):")
            for r in rows:
                print(f"  {r['wallet'][:12]} | {str(r['market'])[:35]:35s} | {r['exit_type']:15s} | realized_pnl=${r['realized_pnl']:>+8,.2f} | entry=${r['entry_value']:>8,.2f} | exit=${r['exit_value']:>8,.2f}")
        else:
            print("No exits tracked yet. Run --refresh first.")
    print(summary(conn))
    conn.close()
