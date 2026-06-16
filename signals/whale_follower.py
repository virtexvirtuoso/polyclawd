#!/usr/bin/env python3
"""
Whale-follow PAPER executor — informed-flow following, shadow-only.

Thesis under test: when an informed bettor hits a market with size that is
large RELATIVE TO THE MARKET (not absolute), the post-impact price still
under-reflects the information, and a follower entering within minutes
captures the residual repricing until it decays.

The honest prior is that this edge is ZERO (we pay the whale's impact, fees
are 350bps round-trip at mid prices, and most flagged flow is reactive).
This module is therefore primarily a DATA COLLECTOR: every follow stores its
INFO components and a dense price grid so the event study (design doc
2026-06-12, vault) can estimate the decay curve and kill or promote cells.

PAPER ONLY: no Simmer, no order placement, no imports from api/routes.
Reads whale_scanner.db read-only; writes ONLY whale_follows in whale_meta.db.

Pre-registered kill criteria K1-K6 live in the design doc; first read at
N>=150/cell, verdict at N>=500/cell.

CLI:
    python3 signals/whale_follower.py --run        # entry + manage pass
    python3 signals/whale_follower.py --summary    # round-trip P&L by cell
"""

import argparse
import json
import logging
import sqlite3
import sys
import os
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.whale_scanner import _ticker_event_date, _today_et  # noqa: E402
from signals.whale_outcomes import (  # noqa: E402
    get_meta_db,
    kalshi_lookup,
    pm_lookup,
    direction_from_alert,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
ALERTS_DB_PATH = BASE_DIR / "storage" / "whale_scanner.db"

# ── Strategy parameters (priors — the event study replaces these) ───────────
SIZE_USD = 1000.0  # per Mr. V 2026-06-12 (was 100)  # fixed paper notional per follow
INFO_THRESHOLD = 0.55

# Per-archetype INFO thresholds (weather lower because it's more data-driven,
# less reactive noise)
INFO_THRESHOLDS = {
    "weather": 0.45,
    "econ": 0.55,
    "policy": 0.55,
    "sports": 0.55,
    "crypto": 0.55,
    "entertainment": 0.55,
    "other": 0.55,
}
# Paper phase: allow marginally NEGATIVE modeled edge so the study accrues
# data across cells — pnl_net records the true cost either way. Tighten to
# +0.005 at live promotion (post-verdict, K1-K6).
FEE_MARGIN = -0.01
DOLLAR_FLOOR = 750.0  # G1 (lower than the alert gate's 2k: study needs data)
PM_FEE_BPS = 0.0
MAX_SPREAD = 0.10  # G4
GIVE_UP_AFTER = 35 * 24 * 3600
MAX_ENTRY_LAG_S = 600  # alert older than this = missed; never enter late
ENTRY_BATCH = 2000
CONSENSUS_WINDOW_S = 3600  # 1-hour window for consensus detection
MANAGE_CAP = 300  # price lookups per manage pass

# Per-archetype priors: expected residual move E_raw (price units) and
# decay half-life (seconds). Deliberately conservative; study-tunable.
ARCH_PRIORS = {
    "weather": {"e_raw": 0.04, "halflife": 7200, "f_arch": 1.0},
    "econ": {"e_raw": 0.04, "halflife": 7200, "f_arch": 1.0},
    "policy": {"e_raw": 0.03, "halflife": 14400, "f_arch": 1.0},
    "sports": {"e_raw": 0.02, "halflife": 1800, "f_arch": 0.6},
    "crypto": {"e_raw": 0.01, "halflife": 900, "f_arch": 0.3},
    "entertainment": {"e_raw": 0.02, "halflife": 3600, "f_arch": 0.5},
    "other": {"e_raw": 0.02, "halflife": 3600, "f_arch": 0.3},
}

# INFO weights (kalshi redistributes wallet weight into size)
W_PM = {"f_size": 0.35, "f_arch": 0.20, "f_side": 0.15, "f_timing": 0.10, "f_wallet": 0.15, "f_impact": 0.05}
W_KALSHI = {"f_size": 0.50, "f_arch": 0.20, "f_side": 0.15, "f_timing": 0.10, "f_wallet": 0.0, "f_impact": 0.05}

_SPORTS_PREFIXES = (
    "KXMLB",
    "KXNHL",
    "KXNBA",
    "KXWNBA",
    "KXNFL",
    "KXNCAA",
    "KXATP",
    "KXWTA",
    "KXITF",
    "KXWC",
    "KXUFC",
    "KXPGA",
    "KXEPL",
    "KXUCL",
    "KXMLS",
    "KXSOCCER",
)
_WEATHER_PREFIXES = (
    "KXHIGH",
    "KXLOWT",
    "KXRAIN",
    "KXSNOW",
    "HIGHMIA",
    "SNOWNY",
    "HURC",
    "KXHUR",
    "KXNEXTHUR",
    "KXEARTHQUAKE",
    "KXMICHTEMP",
    "MEAD",
    "KXMEAD",
)
_ECON_PREFIXES = (
    "KXCPI",
    "KXNGDP",
    "KXGDP",
    "KXUSPPI",
    "KXFED",
    "FEDHIKE",
    "KXRATECUT",
    "RATECUTS",
    "DOTPLOT",
    "NFPDELAY",
    "CPIDELAY",
    "PCECORE",
    "KXGAS",
    "KXAAAGAS",
    "GASD",
    "DIESELM",
    "KXEGGS",
)
_POLICY_PREFIXES = (
    "KXCRYPTOSTRUCTURE",
    "KXCLARITY",
    "KXTARIFF",
    "KXINFRALW",
    "KXFUNDINGBILLS",
    "KXSENATEBILLS",
    "KXRECNCH",
    "SENATE",
    "GOVPARTY",
    "REPHOUSE",
    "KXHOUSE",
    "KXPRES",
    "POWER",
)
_CRYPTO_PREFIXES = ("KXBTC", "KXETH", "KXSOL", "KXXRP")

_ENTERTAINMENT_PREFIXES = (
    "KXOSCAR", "KXGRAMMY", "KXEMMY", "KXTONY",
    "KXLOVEISLAND", "KXBACHELOR", "KXSURVIVOR",
    "KXBB", "KXBILLBOARD",
)
_CRYPTO_PREFIXES_BROADER = (
    "KXBTC", "KXETH", "KXSOL", "KXXRP",
    "KXCRYPTO", "KXDEFI", "KXNFT", "KXWEB3",
)


def classify_archetype(platform: str, market: str, title: str = "") -> str:
    if platform == "kalshi":
        for prefixes, arch in (
            (_WEATHER_PREFIXES, "weather"),
            (_ECON_PREFIXES, "econ"),
            (_POLICY_PREFIXES, "policy"),
            (_SPORTS_PREFIXES, "sports"),
            (_CRYPTO_PREFIXES_BROADER, "crypto"),
            (_ENTERTAINMENT_PREFIXES, "entertainment"),
        ):
            if market.startswith(prefixes):
                return arch
        return "other"
    t = (title or market).lower()
    # sports BEFORE weather: "Carolina Hurricanes" matches "hurricane"
    # (live bug 2026-06-12: NHL follows tagged weather)
    if any(k in t for k in (" vs", "win the", "score", "goals", "match",
                            "nhl", "nba", "wnba", "mlb", "nfl", "ufc")):
        return "sports"
    if any(k in t for k in ("temperature", "rain", "snow", "hurricane")):
        return "weather"
    if any(k in t for k in ("fed ", "cpi", "gdp", "rate cut", "inflation")):
        return "econ"
    if any(k in t for k in ("election", "senate", "president", "bill", "act ")):
        return "policy"
    # New: entertainment keywords
    if any(k in t for k in ("oscar", "grammy", "emmy", "love island",
                            "bachelor", "survivor", "billboard",
                            "box office", "movie", "album")):
        return "entertainment"
    # New: broader crypto keywords
    if any(k in t for k in ("bitcoin", "ethereum", "solana", "crypto",
                            "defi", "nft", "token", "blockchain")):
        return "crypto"
    return "other"


def kalshi_fee_pc(p: float) -> float:
    """Kalshi trading fee per contract, one side: 0.07 * p * (1-p)."""
    return 0.07 * max(0.0, min(1.0, p)) * (1.0 - max(0.0, min(1.0, p)))


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ── Consensus detection ────────────────────────────────────────────────────────


def consensus_score(meta, market: str, direction: int, ts: float) -> float:
    """Count how many other smart wallets entered the same market on the same
    side within the consensus window. Returns -0.1 to +0.3 boost."""
    same_side = meta.execute(
        "SELECT COUNT(*) FROM whale_follows"
        " WHERE market=? AND direction=? AND ts_entry BETWEEN ? AND ?",
        (market, direction, ts - CONSENSUS_WINDOW_S, ts + CONSENSUS_WINDOW_S)
    ).fetchone()[0]
    opp_side = meta.execute(
        "SELECT COUNT(*) FROM whale_follows"
        " WHERE market=? AND direction=? AND ts_entry BETWEEN ? AND ?",
        (market, -direction, ts - CONSENSUS_WINDOW_S, ts + CONSENSUS_WINDOW_S)
    ).fetchone()[0]

    if same_side + opp_side == 0:
        return 0.0

    # Net consensus: more same-side = boost, more opposite = penalty
    net = same_side - opp_side
    if net <= 0:
        return -0.1  # conflict detected
    return min(0.3, net * 0.1)  # +0.1 per additional aligned wallet, max +0.3


# ── INFO score ───────────────────────────────────────────────────────────────


def info_score(meta, platform: str, market: str, reasons: str, p: dict, direction: int = 1) -> tuple:
    """(score 0-1, components dict). Hard gates zero the score; components
    record which gate failed so the study can audit gate leakage."""
    flow_usd = p.get("flow_dollars") or 0.0
    bid, ask = p.get("best_bid"), p.get("best_ask")
    last = p.get("last_yes_price") or p.get("current_price")

    # Hard gates
    if flow_usd < DOLLAR_FLOOR:
        return 0.0, {"gate_fail": "G1_dollar_floor"}
    if (
        (bid is not None and bid >= 0.95)
        or (ask is not None and 0 < ask <= 0.05)
        or (last is not None and not 0.03 < last < 0.97)
    ):
        return 0.0, {"gate_fail": "G2_near_settled"}
    arch = classify_archetype(platform, market, p.get("title", ""))
    if platform == "kalshi" and arch == "sports":
        ev = _ticker_event_date(market)
        if ev is not None and ev == _today_et():
            return 0.0, {"gate_fail": "G3_reactive_sports"}
    if "first_sight" in reasons:
        return 0.0, {"gate_fail": "G5_first_sight"}
    if platform == "kalshi":
        if ask is None or bid is None:
            return 0.0, {"gate_fail": "G4_no_book"}
        if ask - bid > MAX_SPREAD or (p.get("ask_depth") or 0) <= 0:
            return 0.0, {"gate_fail": "G4_unexecutable"}
    else:
        if last is None:
            return 0.0, {"gate_fail": "G4_no_price"}

    # Continuous factors
    mid = ((bid + ask) / 2) if (bid is not None and ask is not None) else last
    if platform == "kalshi":
        oi_usd = (p.get("open_interest") or 0.0) * (mid or 0.5)
        depth_usd = ((p.get("bid_depth") or 0.0) + (p.get("ask_depth") or 0.0)) * (mid or 0.5)
        eff_liq = max(oi_usd, depth_usd, 1.0)
    else:
        eff_liq = max(p.get("open_interest") or 0.0, 1.0)  # liquidityNum sits in oi slot
    f_size = _clip(flow_usd / (0.25 * eff_liq))

    fy, fn = p.get("flow_yes") or 0.0, p.get("flow_no") or 0.0
    if fy + fn > 0:
        f_side = _clip((max(fy, fn) / (fy + fn) - 0.5) / 0.5)
    elif p.get("flow_desc"):
        f_side = 0.8  # pm_flow_desc already requires >=67% dominance
    else:
        f_side = 0.0

    f_impact = _clip(
        0.5 * ("level_jump" in reasons) + 0.5 * ("spread_collapse" in reasons or "imbalance_flip" in reasons)
    )

    f_timing = 1.0
    if platform == "kalshi" and arch == "sports":
        f_timing = 0.5  # pregame, non-today (today gated by G3)

    f_wallet = 0.0
    if platform == "polymarket":
        share = p.get("top_wallet_share") or 0.0
        base = 1.0 if "smart_wallet" in reasons else 0.5
        # Boost if this wallet has strong P&L in this archetype
        wallet = p.get("top_wallet") or ""
        if wallet:
            wallet_arch = meta.execute(
                "SELECT wins, trades, concentration FROM wallet_archetype_pnl WHERE wallet=? AND archetype=?",
                (wallet, arch)).fetchone()
            if wallet_arch and wallet_arch["trades"] >= 5:
                wr = wallet_arch["wins"] / wallet_arch["trades"]
                if wr > 0.55:
                    base *= 1.2  # 20% boost for wallets strong in this archetype
                elif wr < 0.40:
                    base *= 0.5  # 50% penalty for wallets weak in this archetype
            # Concentration boost: multiply f_wallet by (1 + concentration)
            # e.g., 80% concentration = 1.8x boost
            if wallet_arch and wallet_arch["concentration"]:
                conc = wallet_arch["concentration"]
                if conc > 0:
                    base *= (1.0 + conc)
        f_wallet = _clip(share) * base

    f_arch = ARCH_PRIORS[arch]["f_arch"]

    # Consensus boost: how many other smart wallets entered the same market
    # on the same side within the consensus window
    consensus = consensus_score(meta, market, direction, time.time())

    f = {
        "f_size": f_size,
        "f_side": f_side,
        "f_impact": f_impact,
        "f_timing": f_timing,
        "f_arch": f_arch,
        "f_wallet": f_wallet,
    }
    w = W_KALSHI if platform == "kalshi" else W_PM
    score = sum(w[k] * f[k] for k in f) / sum(w.values())
    f["archetype"] = arch
    f["consensus"] = consensus
    score = round(score + consensus, 4)
    return score, f


# ── Persistence ──────────────────────────────────────────────────────────────


def ensure_table(meta: sqlite3.Connection):
    meta.execute("""
        CREATE TABLE IF NOT EXISTS whale_follows (
            follow_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id    INTEGER NOT NULL UNIQUE,
            ts_alert    REAL NOT NULL,
            ts_entry    REAL NOT NULL,
            platform    TEXT NOT NULL,
            market      TEXT NOT NULL,
            condition_id TEXT,
            wallet      TEXT,
            archetype   TEXT,
            info_score  REAL NOT NULL,
            info_components TEXT,
            direction   INTEGER NOT NULL,
            size_usd    REAL NOT NULL,
            alert_mid   REAL, alert_ask REAL, alert_bid REAL,
            entry_px    REAL NOT NULL,
            entry_fee   REAL NOT NULL,
            exit_policy TEXT,
            target_px   REAL, stop_px REAL, halflife_s REAL,
            px_5m REAL, px_15m REAL, px_30m REAL, px_1h REAL, px_4h REAL,
            ts_exit     REAL, exit_px REAL, exit_reason TEXT, exit_fee REAL,
            pnl_gross   REAL, pnl_net REAL,
            result      TEXT, correct_res INTEGER,
            done        INTEGER DEFAULT 0,
            updated     REAL
        )""")
    meta.execute("CREATE INDEX IF NOT EXISTS idx_follows_open ON whale_follows(done, ts_entry)")
    # Migration: add wallet column if missing (existing tables from before 2026-06-15)
    try:
        meta.execute("ALTER TABLE whale_follows ADD COLUMN wallet TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    meta.execute("CREATE TABLE IF NOT EXISTS follower_kv (key TEXT PRIMARY KEY, value TEXT)")
    meta.commit()


def _kv_get(meta, key: str, default: str = "") -> str:
    row = meta.execute("SELECT value FROM follower_kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _kv_set(meta, key: str, value: str):
    meta.execute("INSERT INTO follower_kv (key, value) VALUES (?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ── Entry pass ───────────────────────────────────────────────────────────────


def _entry_price(
    direction: int,
    bid: Optional[float],
    ask: Optional[float],
    last: Optional[float],
    ask_depth: Optional[float],
    mid: Optional[float],
) -> Optional[float]:
    """Executable YES-space price for the follower, including our own slippage.
    d=+1 pays the ask (+slip); d=-1 hits the bid (-slip). No book -> last±1 tick."""
    if direction > 0:
        base = ask if ask is not None else (last + 0.01 if last is not None else None)
    else:
        base = bid if bid is not None else (last - 0.01 if last is not None else None)
    if base is None:
        return None
    depth_usd = (ask_depth or 0.0) * (mid or 0.5)
    slip = _clip(SIZE_USD / (depth_usd + 1e-6), 0.0, 0.03) if depth_usd else 0.01
    px = base + direction * slip
    return round(_clip(px, 0.01, 0.99), 4)


def open_new_follows(meta: sqlite3.Connection, alerts_db_path: Optional[Path] = None) -> dict:
    """Scan new alerts, qualify via INFO, record synthetic entries."""
    now = time.time()
    src = sqlite3.connect(f"file:{alerts_db_path or ALERTS_DB_PATH}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    # Cursor must advance past stale rows even when nothing is entered, or the
    # scan window pins to the oldest ENTRY_BATCH alerts forever (bug found
    # 2026-06-12: empty table -> last_id 0 -> same ancient batch every pass).
    last_id = int(_kv_get(meta, "last_alert_id", "0") or 0)
    rows = src.execute(
        "SELECT * FROM whale_alerts WHERE id > ? AND ts > ? ORDER BY id LIMIT ?",
        (last_id, now - MAX_ENTRY_LAG_S, ENTRY_BATCH)).fetchall()
    stale_max = src.execute(
        "SELECT COALESCE(MAX(id), 0) FROM whale_alerts WHERE ts <= ?",
        (now - MAX_ENTRY_LAG_S,)).fetchone()[0]
    src.close()
    cursor = max(last_id, stale_max, rows[-1]["id"] if rows else 0)
    if cursor != last_id:
        _kv_set(meta, "last_alert_id", str(cursor))
        meta.commit()
    if not rows:
        return {"scanned": 0, "entered": 0}

    pm_dirs = {}  # slug -> outcomes, fetched lazily once per pass
    entered = skipped_info = skipped_dir = skipped_edge = skipped_stale = 0

    for r in rows:
        try:
            p = json.loads(r["payload"] or "{}")
        except json.JSONDecodeError:
            p = {}
        reasons = r["reasons"] or ""
        # Real-time discipline: entering long after the alert is not a follow —
        # the decay already happened (look-ahead). Study also filters on lag.
        if now - r["ts"] > MAX_ENTRY_LAG_S:
            skipped_stale += 1
            continue

        # Determine direction BEFORE info_score so consensus can use it
        if r["platform"] == "kalshi":
            direction = direction_from_alert("kalshi", reasons, p)
        else:
            slug = r["market"]
            if slug not in pm_dirs:
                pm_dirs[slug] = (pm_lookup([slug]).get(slug) or {}).get("outcomes") or []
            p["_outcomes"] = pm_dirs[slug]
            direction = direction_from_alert("polymarket", reasons, p)
        if direction is None:
            skipped_dir += 1
            continue

        info, comps = info_score(meta, r["platform"], r["market"], reasons, p, direction)
        arch = comps.get("archetype", "other")
        info_threshold = INFO_THRESHOLDS.get(arch, 0.55)
        if info < info_threshold:
            skipped_info += 1
            continue

        arch = comps.get("archetype", "other")
        prior = ARCH_PRIORS[arch]
        bid, ask = p.get("best_bid"), p.get("best_ask")
        last_px = p.get("last_yes_price") or p.get("current_price")
        mid = ((bid + ask) / 2) if (bid is not None and ask is not None) else last_px
        entry_px = _entry_price(direction, bid, ask, last_px, p.get("ask_depth"), mid)
        if entry_px is None or mid is None:
            skipped_dir += 1
            continue

        # Edge-survival gate: modeled residual must clear fees + margin.
        e_raw = prior["e_raw"] * (0.5 + info)  # scale prior by conviction
        entry_fee = kalshi_fee_pc(entry_px) if r["platform"] == "kalshi" else PM_FEE_BPS / 1e4
        exit_fee_est = kalshi_fee_pc(mid + direction * e_raw) if r["platform"] == "kalshi" else PM_FEE_BPS / 1e4
        impact_paid = abs(entry_px - mid)
        comps["modeled_edge"] = round(e_raw - entry_fee - exit_fee_est - impact_paid, 4)
        if comps["modeled_edge"] < FEE_MARGIN:
            skipped_edge += 1
            continue

        ttr = None
        try:
            from datetime import datetime as _dt

            ct = p.get("close_time") or ""
            if ct:
                ttr = _dt.fromisoformat(ct.replace("Z", "+00:00")).timestamp() - now
        except (ValueError, AttributeError):
            pass
        policy = "resolution" if (ttr is not None and 0 < ttr < prior["halflife"]) else "convergence"
        target = round(_clip(mid + direction * e_raw, 0.01, 0.99), 4)
        stop = round(_clip(mid - direction * max(0.05, 1.5 * e_raw), 0.01, 0.99), 4)

        meta.execute(
            """INSERT OR IGNORE INTO whale_follows
            (alert_id, ts_alert, ts_entry, platform, market, condition_id,
             wallet, archetype, info_score, info_components, direction, size_usd,
             alert_mid, alert_ask, alert_bid, entry_px, entry_fee,
             exit_policy, target_px, stop_px, halflife_s, done, updated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
            (
                r["id"],
                r["ts"],
                now,
                r["platform"],
                r["market"],
                p.get("condition_id"),
                p.get("top_wallet", ""),
                arch,
                info,
                json.dumps(comps),
                direction,
                SIZE_USD,
                mid,
                ask,
                bid,
                entry_px,
                entry_fee,
                policy,
                target,
                stop,
                prior["halflife"],
                now,
            ),
        )
        entered += 1

    meta.commit()
    return {
        "scanned": len(rows),
        "entered": entered,
        "skip_info": skipped_info,
        "skip_stale": skipped_stale,
        "skip_dir": skipped_dir,
        "skip_edge": skipped_edge,
    }


# ── Manage pass ──────────────────────────────────────────────────────────────

_GRID = (("px_5m", 300), ("px_15m", 900), ("px_30m", 1800), ("px_1h", 3600), ("px_4h", 14400))


def _close(meta, r, exit_px: Optional[float], reason: str, result: str = "", correct_res=None):
    now = time.time()
    if exit_px is None:
        meta.execute(
            "UPDATE whale_follows SET done=1, exit_reason=?, updated=? WHERE follow_id=?", (reason, now, r["follow_id"])
        )
        return
    d = r["direction"]
    cost = r["entry_px"] if d > 0 else (1.0 - r["entry_px"])
    n = r["size_usd"] / max(cost, 1e-6)
    exit_fee = kalshi_fee_pc(exit_px) if r["platform"] == "kalshi" else PM_FEE_BPS / 1e4
    if reason == "resolution":
        exit_fee = 0.0  # settlement is feeless
    gross = d * (exit_px - r["entry_px"]) * n
    net = gross - (r["entry_fee"] + exit_fee) * n
    meta.execute(
        """UPDATE whale_follows SET ts_exit=?, exit_px=?, exit_reason=?,
                    exit_fee=?, pnl_gross=?, pnl_net=?, result=?, correct_res=?,
                    done=1, updated=? WHERE follow_id=?""",
        (now, exit_px, reason, exit_fee, round(gross, 4), round(net, 4), result, correct_res, now, r["follow_id"]),
    )


def manage_open_follows(meta: sqlite3.Connection) -> dict:
    now = time.time()
    open_rows = meta.execute(
        "SELECT * FROM whale_follows WHERE done=0 ORDER BY ts_entry LIMIT ?", (MANAGE_CAP,)
    ).fetchall()
    if not open_rows:
        return {"open": 0}

    k_tickers = sorted({r["market"] for r in open_rows if r["platform"] == "kalshi"})
    p_slugs = sorted({r["market"] for r in open_rows if r["platform"] == "polymarket"})
    prices = kalshi_lookup(k_tickers) if k_tickers else {}
    prices.update(pm_lookup(p_slugs[:80]) if p_slugs else {})

    closed = 0
    for r in open_rows:
        info = prices.get(r["market"])
        if not info or info.get("mid") is None:
            if now - r["ts_entry"] > GIVE_UP_AFTER:
                _close(meta, r, None, "giveup")
                closed += 1
            continue
        mid, age, d = info["mid"], now - r["ts_entry"], r["direction"]

        # fill the path grid (study substrate)
        sets, vals = [], []
        for col, h in _GRID:
            if r[col] is None and age >= h:
                sets.append(f"{col}=?")
                vals.append(mid)
        if sets:
            meta.execute(
                f"UPDATE whale_follows SET {', '.join(sets)}, updated=? WHERE follow_id=?", vals + [now, r["follow_id"]]
            )

        result = info.get("result") or ""
        if result:
            if r["platform"] == "kalshi":
                settle = 1.0 if result == "yes" else 0.0
                win = 1 if (result == "yes") == (d > 0) else 0
            else:
                outs = info.get("outcomes") or []
                won0 = bool(outs) and result == outs[0]
                settle = 1.0 if won0 else 0.0
                win = 1 if won0 == (d > 0) else 0
            _close(meta, r, settle, "resolution", result, win)
            closed += 1
        elif d * (mid - r["target_px"]) >= 0:
            _close(meta, r, mid, "convergence")
            closed += 1
        elif d * (mid - r["stop_px"]) <= 0:
            _close(meta, r, mid, "stop")
            closed += 1
        elif r["exit_policy"] != "resolution" and age >= 2 * r["halflife_s"]:
            _close(meta, r, mid, "halflife")
            closed += 1

    meta.commit()
    return {"open": len(open_rows), "closed": closed}


_FUNNEL_KEYS = ("scanned", "entered", "skip_info", "skip_dir",
                "skip_edge", "skip_stale")


def bump_funnel(meta: sqlite3.Connection, stats: dict):
    """Accumulate today's entry-funnel counters (UTC-keyed so restarts and
    purges can't silently zero history — one key per day, additive)."""
    from datetime import datetime as _dt, timezone as _tz
    key = "funnel:" + _dt.now(_tz.utc).strftime("%Y-%m-%d")
    try:
        cur = json.loads(_kv_get(meta, key, "{}") or "{}")
    except json.JSONDecodeError:
        cur = {}
    for k in _FUNNEL_KEYS:
        cur[k] = cur.get(k, 0) + (stats.get(k) or 0)
    _kv_set(meta, key, json.dumps(cur))
    meta.commit()


def run_pass(meta: Optional[sqlite3.Connection] = None) -> dict:
    """Scheduler entry point: qualify new alerts, then manage open follows."""
    conn = meta or get_meta_db()
    try:
        ensure_table(conn)
        stats = open_new_follows(conn)
        bump_funnel(conn, stats)
        stats.update(manage_open_follows(conn))
        return stats
    finally:
        if meta is None:
            conn.close()


def summary(conn) -> str:
    lines = ["whale-follow paper P&L by archetype x INFO tercile (net of fees):"]
    q = """SELECT archetype,
                  CASE WHEN info_score >= 0.7 THEN 'hi'
                       WHEN info_score >= 0.6 THEN 'mid' ELSE 'lo' END tercile,
                  COUNT(*) n,
                  SUM(done) closed,
                  ROUND(SUM(pnl_net), 2) pnl,
                  ROUND(AVG(pnl_net), 3) avg_pnl,
                  ROUND(AVG(CASE WHEN pnl_net > 0 THEN 1.0 ELSE 0.0 END), 3) wr
           FROM whale_follows
           GROUP BY archetype, tercile ORDER BY archetype, tercile"""
    for r in conn.execute(q):
        lines.append(
            f"  {r['archetype']:8s} {r['tercile']:4s} n={r['n']:<5d}"
            f" closed={r['closed'] or 0:<5d} pnl=${r['pnl'] or 0:<9} "
            f"avg=${r['avg_pnl'] or 0} wr={r['wr'] if r['wr'] is not None else '—'}"
        )
    open_n = conn.execute("SELECT COUNT(*) FROM whale_follows WHERE done=0").fetchone()[0]
    lines.append(f"  open positions: {open_n}")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    conn = get_meta_db()
    ensure_table(conn)
    if args.run:
        print(run_pass(conn))
    print(summary(conn))
    conn.close()
