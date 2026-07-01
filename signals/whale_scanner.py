#!/usr/bin/env python3
"""
Whale Shark Scanner v3 — full-exchange asymmetric-bet detection, Kalshi + Polymarket.

Two detection layers, both change-based (v2 lesson: absolute thresholds alert
on 98% of books because DMM quoting is the steady state):

1. SWEEP (every cycle, ALL markets): Kalshi's exchange-wide public trades
   feed (/markets/trades since the last cycle) covers every executed bet on
   all 400k+ open markets in a handful of calls — full-pagination diffing is
   infeasible (>400 pages, ~13 min from the VPS). Aggregate per market with
   taker direction, enrich candidates via batch /markets?tickers=..., and
   gate relative AND absolute so busy in-game markets don't flood. Polymarket:
   top markets by 24h volume from Gamma, volume/liquidity deltas vs state.
2. BOOKS (budgeted): order books for sweep-flagged markets immediately, plus a
   deterministic rotation over the watchlist (all weather series + thin-active
   markets) for resting-wall detection (level jumps, imbalance flips, spread
   collapse). First sight of a market/book = baseline, never alerts.

Persistence (storage/whale_scanner.db — dedicated file; shadow_trades.db has
too many concurrent writers):
  market_state    latest sweep value per market (upsert)
  whale_snapshots book snapshots, 48h retention
  whale_alerts    every alert >= score 3 (shadow-validation substrate;
                  Telegram drain reads CRITICAL/HIGH via whale_alert_drain.py)
  kv              rotation cursor, bootstrap flags

Kalshi field gotchas (cost v1 its life): orderbook_fp uses yes_dollars /
no_dollars (NO bids = YES asks at 1-p); market fields are open_interest_fp /
volume_fp decimal strings.

Runs on its own scheduler loop (tick_whale) so the multi-minute scan never
delays stop evaluation in tick_5min. Standalone CLI:

    python3 signals/whale_scanner.py                    # full scan
    python3 signals/whale_scanner.py --platform kalshi
    python3 signals/whale_scanner.py --min-score 5 --json
"""

import argparse
import json
import logging
import math
import re
import sqlite3
import sys
import os
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
STORAGE_DIR = BASE_DIR / "storage"
DB_PATH = STORAGE_DIR / "whale_scanner.db"

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_API = "https://gamma-api.polymarket.com"
PM_DATA_API = "https://data-api.polymarket.com"

# ── Per-category thresholds from whale_threshold_study.py ───────────────────
# Loaded at init from research/results/whale_thresholds.json if available,
# otherwise falls back to flat defaults below.

_DEFAULT_THRESHOLDS = {
    "whale_alert": 3000,
    "mega_whale": 40000,
    "noise_floor": 300,
}

_THRESHOLDS_PATH = BASE_DIR / "research" / "results" / "whale_thresholds.json"

# Category classification for Polymarket (slug prefix → category)
_PM_SLUG_TO_CAT = {
    "mlb": "mlb", "baseball": "mlb",
    "soccer": "soccer", "epl": "soccer", "ucl": "soccer", "laliga": "soccer",
    "seriea": "soccer", "bundesliga": "soccer", "fifwc": "soccer",
    "nba": "nba", "basketball": "nba", "wnba": "nba",
    "nfl": "nfl", "football": "nfl", "cfb": "nfl",
    "ufc": "ufc", "mma": "ufc",
    "crypto": "crypto", "btc": "crypto", "eth": "crypto", "sol": "crypto",
    "politics": "politics", "elections": "politics",
    "policy": "policy", "tariffs": "policy", "congress": "policy",
    "science": "science", "technology": "science",
}

# Category classification for Kalshi (series prefix → category)
_KX_PREFIX_TO_CAT = {
    "KXMLB": "mlb",
    "KXNBA": "nba", "KXWNBA": "nba", "KXNCAA": "nba",
    "KXNFL": "nfl",
    "KXITF": "soccer", "KXATP": "soccer", "KXWTA": "soccer",
    "KXEPL": "soccer", "KXCL": "soccer", "KXSOCCER": "soccer", "KXWC": "soccer",
    "KXUFC": "ufc", "KXMMA": "ufc",
    "KXBTC": "crypto", "KXETH": "crypto", "KXCRYPTO": "crypto", "KXSOL": "crypto",
    "KXPRES": "politics", "SENATE": "politics", "KXHOUSE": "politics",
    "KXMAY": "politics", "KXGOV": "politics", "POWER": "politics",
    "KXINFL": "macro", "KXFED": "macro", "KXGDP": "macro",
    "KXUNEMP": "macro", "KXCPI": "macro", "KXJOBS": "macro",
    "KXRAIN": "weather", "KXTEMP": "weather", "KXSNOW": "weather",
    "KXHURR": "weather", "KXSTORM": "weather", "KXHEAT": "weather",
}

# Loaded thresholds: {platform: {category: {whale_alert, mega_whale, noise_floor}}}
_CAT_THRESHOLDS: dict = {}


def _load_thresholds():
    """Load per-category thresholds from whale_threshold_study output."""
    global _CAT_THRESHOLDS
    try:
        with open(_THRESHOLDS_PATH) as f:
            data = json.load(f)
        _CAT_THRESHOLDS = data.get("config", {})
        logger.info("Loaded per-category whale thresholds from %s (%d PM cats, %d KX cats)",
                     _THRESHOLDS_PATH,
                     len(_CAT_THRESHOLDS.get("polymarket", {})),
                     len(_CAT_THRESHOLDS.get("kalshi", {})))
    except Exception as e:
        logger.warning("Could not load whale thresholds from %s: %s — using defaults",
                       _THRESHOLDS_PATH, e)
        _CAT_THRESHOLDS = {}


def classify_market_category(platform: str, market: str) -> str:
    """Map a market identifier to a threshold category."""
    if platform == "kalshi":
        series = market.split("-")[0]
        for prefix, cat in _KX_PREFIX_TO_CAT.items():
            if series.startswith(prefix):
                return cat
        return "other"
    # Polymarket: slug prefix
    slug = market.split("-")[0].lower()
    return _PM_SLUG_TO_CAT.get(slug, "other")


def get_category_thresholds(platform: str, category: str) -> dict:
    """Get whale_alert / mega_whale / noise_floor for a platform+category."""
    plat_key = "polymarket" if platform != "kalshi" else "kalshi"
    cat_config = _CAT_THRESHOLDS.get(plat_key, {})
    return cat_config.get(category, _DEFAULT_THRESHOLDS)


def get_market_thresholds(platform: str, market: str) -> dict:
    """Convenience: classify then look up thresholds for a specific market."""
    cat = classify_market_category(platform, market)
    return get_category_thresholds(platform, cat)


# ── Sweep thresholds (executed flow; relative AND absolute) ─────────────────
# These flat constants are FALLBACKS only — used when no category threshold
# is available. Category-aware code should call get_market_thresholds().
VOL_SPIKE_ABS    = 500    # contracts ($ on PM) traded since last sweep
VOL_SPIKE_REL    = 0.30   # ... and >= 30% of prior lifetime volume
VOL_MOVE_ABS     = 250
VOL_MOVE_REL     = 0.15
OI_SPIKE_ABS     = 500    # new open interest (liquidity $ on PM)
OI_SPIKE_REL     = 0.30
THIN_FLOW_OI     = 2000   # market this small...
THIN_FLOW_DELTA  = 150    # ...trading this much in one cycle = note

# ── Book thresholds (resting orders; deltas vs previous snapshot) ───────────
LEVEL_WHALE      = 1000   # level jumping to >=1K from <100 = whale entry
LEVEL_BIG        = 500
LEVEL_BASELINE   = 100
DEPTH_SURGE_MULT = 3.0
DEPTH_SURGE_MIN  = 500
IMBAL_MILD       = 3.0
IMBAL_EXTREME    = 5.0
SPREAD_WIDE      = 0.05
SPREAD_TIGHT     = 0.01

CRITICAL_SCORE   = 8
HIGH_SCORE       = 5
ALERT_MIN_SCORE  = 3

# ── Scan budgets ────────────────────────────────────────────────────────────
TRADES_PAGE_CAP    = 30     # 1000 trades/page since last cycle
TRADES_MAX_LOOKBACK = 3600  # don't replay more than 1h after downtime
PM_SWEEP_PAGES     = 15     # x100 markets, ordered by volume24hr desc
PM_TRADES_PAGE_CAP = 20     # x500 taker trades from data-api (desc by ts)
FLAG_BOOK_CAP      = 80     # immediate books for sweep-flagged markets
ROTATE_BOOK_CAP    = 250    # rotation books per cycle (book fetches are small/fast)
BOOK_DEADLINE_S    = 180    # wall-clock budget for the BOOK phase (starts after sweep)
WATCH_OI_MIN       = 100    # thin-active band for the rotation watchlist
WATCH_OI_MAX       = 20000
ALERT_DEDUP_S      = 1800   # don't re-alert the same market within 30 min

# ── Wallet accumulation thresholds ──────────────────────────────────────────
ACCUM_WINDOW_S       = 3600     # 60-min rolling window
ACCUM_MEGA_USD       = 200_000  # CRITICAL: wallet accumulated $200K+ in window
ACCUM_WHALE_USD      = 50_000   # HIGH: wallet accumulated $50K+ in window
ACCUM_NOTABLE_USD    = 10_000   # DB-only: wallet accumulated $10K+ in window
ACCUM_DEDUP_S        = 4 * 3600 # 4h cooldown per wallet+market
ACCUM_RETENTION_S    = 2 * 3600 # prune fills older than 2h
ACCUM_KALSHI_MEGA    = 50_000   # Kalshi per-market (anonymous): CRITICAL
ACCUM_KALSHI_WHALE   = 20_000   # Kalshi per-market: HIGH
ACCUM_TG_RATE_LIMIT  = 10       # max accumulation alerts per hour
EXCLUDE_SERIES_PREFIXES = ("KXMVE", "KXBTC15M", "KXBTCD", "KXETH15M", "KXETHD",
                           "KXHIGHNYD")  # parlay builders + short-cycle series that mint fresh tickers
# Live-game/live-event market classes: during play, flow bursts + book churn
# fire every signal mechanically (betting churn, not informed whales) — they
# produced ~80% of CRITICALs on 2026-06-12 (MLB props, ITF/ATP, NHL/WC goals,
# mention markets). They cap at HIGH; CRITICAL is reserved for quiet markets.
LIVE_GAME_KALSHI_PREFIXES = (
    "KXMLB", "KXNBA", "KXNHL", "KXNFL", "KXWNBA", "KXNCAA", "KXCS2", "KXLOL",
    "KXDOTA", "KXVAL", "KXUFC", "KXMMA", "KXITF", "KXATP", "KXWTA",
    "KXWC", "KXPGA", "KXGOLF", "KXSOCCER", "KXR6", "KXLIU", "KXKF", "KXBOXING")
LIVE_GAME_KALSHI_SUBSTR = ("MENTION", "MATCH", "GOAL", "SPREAD", "TOTAL",
                           "GAME", "ELIMINATION", "15M")
LIVE_GAME_PM_PREFIXES = (
    "mlb-", "nba-", "nhl-", "nfl-", "wnba-", "cfb-", "cbb-", "atp-", "wta-",
    "itf-", "ufc-", "mma-", "lol-", "cs2-", "csgo-", "val-", "dota-",
    "fifwc-", "epl-", "ucl-", "laliga-", "seriea-", "bundesliga-")


_KALSHI_GAMEDATE = re.compile(r"-\d{2}[A-Z]{3}\d{2}")   # -26JUN11 = per-game/per-day
_PM_GAMEDATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Tracker/churn classes cap unconditionally; SPORTS classes cap only for
# per-game (game-dated) markets — championship/season FUTURES stay
# CRITICAL-eligible (a whale hitting World Series futures off-game is exactly
# the informed-positioning signal; per-game flow is betting churn).
TRACKER_KALSHI_PREFIXES = (
    "KXINX", "KXNASDAQ", "KXDJI", "KXBTC", "KXETH", "KXSOL", "KXXRP",
    "KXBRENT", "KXGOLD", "KXSILVER", "KXWTI", "KXAAAGAS", "KXUSD", "KXEUR",
    "KXNATGAS", "KXBNB", "KXCOPPER", "KXPLATINUM", "KXPALLADIUM")


def live_game_class(platform: str, market: str) -> Optional[str]:
    """'game' (per-game sports), 'tracker' (financial bracket), or None."""
    if platform == "kalshi":
        series = market.split("-")[0]
        if series.startswith(TRACKER_KALSHI_PREFIXES) or "15M" in series:
            return "tracker"
        sporty = (series.startswith(LIVE_GAME_KALSHI_PREFIXES)
                  or any(s in series for s in LIVE_GAME_KALSHI_SUBSTR))
        return "game" if sporty and _KALSHI_GAMEDATE.search(market) else None
    sporty = market.startswith(LIVE_GAME_PM_PREFIXES) or "-vs-" in market
    return "game" if sporty and _PM_GAMEDATE.search(market) else None


def is_live_game_market(platform: str, market: str) -> bool:
    return live_game_class(platform, market) is not None


def class_key(platform: str, market: str) -> str:
    """Distribution bucket for class-relative outlier thresholds."""
    if platform == "kalshi":
        return market.split("-")[0]
    return "pm:" + market.split("-")[0]


# ── Pierce rules: conditions that punch through the live-game ceiling ──────
CLASS_OUTLIER_PCTL    = 0.995   # burst must beat its own class's p99.5...
CLASS_OUTLIER_MIN_USD = 1000.0  # ...and an absolute floor
CLASS_OUTLIER_MIN_N   = 200     # min class sample, else fall back to _global
SMART_PIERCE_MIN_USD  = 1000.0  # proven wallet putting this much in pierces
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}


def _extract_game_date(platform, market):
    """Game day from the ticker/slug (Kalshi -26JUN11, PM 2026-06-11)."""
    from datetime import date
    if platform == "kalshi":
        mt = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", market)
        if mt and mt.group(2) in _MONTHS:
            return date(2000 + int(mt.group(1)), _MONTHS[mt.group(2)], int(mt.group(3)))
        return None
    mt = _PM_GAMEDATE.search(market)
    if mt:
        try:
            y, mo, d = mt.group(0).split("-")
            return date(int(y), int(mo), int(d))
        except ValueError:
            return None
    return None

PREGAME_MAX_PRIOR_VOL  = 2000       # ...into a barely-traded game market = steam
CLASS_THRESH_TTL_S     = 3600

_CLASS_P995: dict = {"_ts": 0.0}


def refresh_class_thresholds(conn):
    """Hourly: per-class p99.5 of burst dollars from our own 7d alert history.
    Self-calibrating — adapts as class volumes shift."""
    now = time.time()
    if now - _CLASS_P995.get("_ts", 0) < CLASS_THRESH_TTL_S:
        return
    buckets: dict = {}
    for plat, market, fd in conn.execute(
            "SELECT platform, market, "
            " COALESCE(json_extract(payload, '$.flow_dollars'), 0)"
            " FROM whale_alerts WHERE ts > ?", (now - 7 * 86400,)):
        if fd and fd > 0:
            buckets.setdefault(class_key(plat, market), []).append(fd)
    fresh = {"_ts": now}
    all_vals = []
    for k, vals in buckets.items():
        all_vals.extend(vals)
        if len(vals) >= CLASS_OUTLIER_MIN_N:
            vals.sort()
            fresh[k] = vals[int(len(vals) * CLASS_OUTLIER_PCTL)]
    if all_vals:
        all_vals.sort()
        fresh["_global"] = all_vals[int(len(all_vals) * CLASS_OUTLIER_PCTL)]
    _CLASS_P995.clear()
    _CLASS_P995.update(fresh)
    logger.info("Class outlier thresholds refreshed: %d classes, global p99.5 $%.0f",
                len(fresh) - 2 if "_global" in fresh else len(fresh) - 1,
                fresh.get("_global", 0))


def apply_livegame_ceiling(alert: dict, platform: str, market: str,
                           meta: Optional[dict], now: Optional[float] = None):
    """Quiet-market rule with pierce conditions. Mutates alert in place.

    Live-game/tracker classes cap at HIGH UNLESS the alert is distinguishable
    from its class's churn: (1) class-relative size outlier, (2) proven
    smart wallet behind the flow, (3) pre-game steam (game class only).
    """
    if alert.get("severity") != "CRITICAL":
        return
    cls = live_game_class(platform, market)
    if cls is None:
        return
    m = meta or {}
    now = now or time.time()
    fd = m.get("flow_dollars") or 0.0

    # Pierce 1: size outlier vs the class's own burst distribution
    thr = _CLASS_P995.get(class_key(platform, market)) or _CLASS_P995.get("_global")
    if thr and fd >= max(thr, CLASS_OUTLIER_MIN_USD):
        alert["reasons"] += f",class_outlier_{fd:.0f}vs{thr:.0f}"
        return

    # Pierce 2: proven winner behind the flow (PM only — wallet identity)
    if "smart_wallet" in alert.get("reasons", "") and             (m.get("top_wallet_usd") or 0) >= SMART_PIERCE_MIN_USD:
        alert["reasons"] += ",smart_pierce"
        return

    # Pierce 3: pre-game steam — sports only. close_time is NOT a usable
    # game-start proxy on Kalshi (settlement buffers run days past the match;
    # a close_time gate pierced 400/day in backtest). The game date is in the
    # ticker itself: steam = burst on a calendar day BEFORE the game day,
    # into a barely-traded market.
    if cls == "game" and (m.get("volume") or 0) <= PREGAME_MAX_PRIOR_VOL and fd > 0:
        gd = _extract_game_date(platform, market)
        if gd and gd > datetime.fromtimestamp(now, tz=timezone.utc).date():
            alert["reasons"] += ",pregame_steam"
            return

    # Pierce 4: whale-sized flow bypasses live-game ceiling entirely.
    # Game-day churn is typically $50-$5K; $25K+ is a genuine whale signal
    # regardless of game state (e.g. $157K on Uruguay -1.5 goals).
    if fd >= CRITICAL_FLOW_USD:
        alert["reasons"] += f",whale_flow_pierce_{fd:.0f}"
        return

    alert["severity"] = "HIGH"
    alert["reasons"] += ",livegame_capped"


PM_EXCLUDE_SLUG_PREFIXES = ("btc-updown-", "eth-updown-", "sol-updown-",
                            "xrp-updown-")  # PM 5-min crypto churn, same class
SMART_WALLET_MIN_USD = 250.0   # proven wallet trading this much = candidate regardless
SMART_WALLET_SCORE   = 4

# ── Alert-quality gates (2026-06-12 calibration audit) ──────────────────────
# Day-1 produced 1,732 CRITICALs/24h: game-day sports markets are born at ~0
# volume so every relative gate passes trivially, and contract counts hide
# tiny dollar flow ($88 scored 10/10). Gates DEMOTE to LOW + tag gated_* in
# reasons — the row still lands in whale_alerts for shadow validation; only
# Telegram/CRITICAL delivery is suppressed.
MIN_ALERT_FLOW_USD = 2000.0  # sweep alerts need real money behind them
CRITICAL_FLOW_USD  = 25000.0 # Telegram-delivered CRITICAL needs genuine whale
                             # size; smaller high-asymmetry flows stay HIGH
NEAR_SETTLED_BID   = 0.95    # bid >= this: market effectively resolved YES
NEAR_SETTLED_ASK   = 0.05    # ask <= this: effectively resolved NO
NEAR_SETTLED_LAST  = 0.97    # last-trade fallback when no book snapshot

_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _ticker_event_date(ticker: str) -> Optional[date]:
    """Event date embedded in a Kalshi ticker's event segment, or None.
    KXMLBHR-26JUN111905SEABAL-... -> 2026-06-11 (trailing 1905 = start time).
    Undated series (SENATEGA-26) and month-only ladders (…-26JAN-FEB1) -> None.
    """
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    m = re.match(r"(\d{2})([A-Z]{3})(\d{2})", parts[1])
    if not m or m.group(2) not in _MONTHS:
        return None
    try:
        return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


def _today_et() -> date:
    """Sports event dates in tickers follow US Eastern day boundaries."""
    return datetime.now(ZoneInfo("America/New_York")).date()


def alert_gate(platform: str, market: str, det: Optional[dict],
               cur: Optional[dict], first_sight: bool = False,
               smart: bool = False) -> Optional[str]:
    """Quality gate for alert DELIVERY (never for DB logging). Returns the
    name of the gate that fired, or None when the alert is deliverable.
    Callers demote gated alerts to LOW and append a gated_<name> reason."""
    if first_sight:
        return "first_sight"
    det = det or {}
    flow_usd = det.get("flow_dollars") or 0
    max_single = det.get("max_single_trade_usd") or 0
    effective_flow = max(flow_usd, max_single)

    if not smart and flow_usd is not None and flow_usd < MIN_ALERT_FLOW_USD:
        return "usd_floor"

    # Whale-sized flow pierces near_settled gate — a $662K bet at 95% is
    # still meaningful signal regardless of market state. Uses category-aware
    # thresholds: soccer mega_whale=$207K, MLB=$90K, politics=$40K, etc.
    if _CAT_THRESHOLDS and effective_flow > 0:
        cat_t = get_market_thresholds(platform, market)
        whale_pierce = cat_t.get("mega_whale", CRITICAL_FLOW_USD)
        if effective_flow >= whale_pierce:
            return None  # whale pierces all gates

    # Fallback: any flow >= CRITICAL_FLOW_USD pierces near_settled
    if effective_flow >= CRITICAL_FLOW_USD:
        return None

    bid = (cur or {}).get("best_bid")
    ask = (cur or {}).get("best_ask")
    last = det.get("last_yes_price")
    if ((bid is not None and bid >= NEAR_SETTLED_BID)
            or (ask is not None and ask <= NEAR_SETTLED_ASK)
            or (last is not None and not (1 - NEAR_SETTLED_LAST) < last < NEAR_SETTLED_LAST)):
        return "near_settled"
    return None

SNAPSHOT_RETENTION_HOURS = 48
STATE_RETENTION_HOURS    = 48


# ── Database ────────────────────────────────────────────────────────────────

def get_db(path: Optional[Path] = None) -> sqlite3.Connection:
    """SQLite connection (WAL) with whale tables ensured."""
    db_path = Path(path) if path else DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS whale_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            platform TEXT NOT NULL,
            market TEXT NOT NULL,
            bid_depth REAL, ask_depth REAL,
            best_bid REAL, best_ask REAL,
            oi REAL, volume REAL,
            levels TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_whale_snap_market_ts"
                 " ON whale_snapshots(market, ts)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS whale_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            platform TEXT NOT NULL,
            market TEXT NOT NULL,
            severity TEXT NOT NULL,
            score INTEGER NOT NULL,
            reasons TEXT,
            raw_score REAL,
            payload TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_state (
            platform TEXT NOT NULL,
            market TEXT NOT NULL,
            ts REAL NOT NULL,
            oi REAL, volume REAL, title TEXT, sub_title TEXT,
            PRIMARY KEY (platform, market)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
    # ── Wallet accumulation tables ──────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wallet_accumulations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            platform TEXT NOT NULL,
            wallet TEXT NOT NULL,
            wallet_name TEXT,
            market TEXT NOT NULL,
            slug TEXT,
            title TEXT,
            side TEXT,
            fill_usd REAL NOT NULL,
            fill_count INTEGER DEFAULT 1
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_wallet_market"
                 " ON wallet_accumulations(wallet, market, ts)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accumulation_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            platform TEXT NOT NULL,
            wallet TEXT NOT NULL,
            market TEXT NOT NULL,
            rolling_usd REAL,
            level TEXT,
            fill_count INTEGER,
            title TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_aa_dedup"
                 " ON accumulation_alerts(wallet, market, ts)")
    conn.commit()
    return conn


def kv_get(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn, key: str, value: str):
    conn.execute("INSERT INTO kv (key, value) VALUES (?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


def save_snapshot(conn, platform: str, market: str, summary: dict,
                  ts: Optional[float] = None):
    conn.execute(
        "INSERT INTO whale_snapshots"
        " (ts, platform, market, bid_depth, ask_depth, best_bid, best_ask,"
        "  oi, volume, levels)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts or time.time(), platform, market,
         summary["bid_depth"], summary["ask_depth"],
         summary["best_bid"], summary["best_ask"],
         summary["oi"], summary["volume"], json.dumps(summary["levels"])))


def load_prev_snapshot(conn, market: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM whale_snapshots WHERE market=? ORDER BY ts DESC LIMIT 1",
        (market,)).fetchone()
    if not row:
        return None
    return {
        "bid_depth": row["bid_depth"], "ask_depth": row["ask_depth"],
        "best_bid": row["best_bid"], "best_ask": row["best_ask"],
        "oi": row["oi"], "volume": row["volume"],
        "levels": json.loads(row["levels"] or "{}"),
    }


def _fire_alert_live(alert: dict):
    """Fire a single CRITICAL alert to Telegram immediately via subprocess."""
    import subprocess
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "scripts" / "whale_alert_tg.py"), "--single"],
            input=json.dumps(alert), capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0 and "Sent: True" in proc.stdout:
            logger.info("Live fire: %s", alert.get("market", "?")[:40])
        elif proc.returncode != 0:
            logger.debug("Live fire skipped (dedup/gate): %s", proc.stdout[:100])
    except Exception as e:
        logger.debug("Live fire error: %s", e)


def log_alert(conn, alert: dict):
    conn.execute(
        "INSERT INTO whale_alerts (ts, platform, market, severity, score,"
        " reasons, payload, raw_score) VALUES (?,?,?,?,?,?,?,?)",
        (time.time(), alert["platform"], alert["market"], alert["severity"],
         alert["score"], alert.get("reasons", ""), json.dumps(alert),
         alert.get("raw_score", alert["score"])))
    # Live fire: CRITICAL always; HIGH for accumulation alerts only
    is_accum = alert.get("type") == "accumulation"
    if alert.get("severity") == "CRITICAL" or (is_accum and alert.get("severity") == "HIGH"):
        _fire_alert_live(alert)


def recently_alerted(conn, window_s: int = ALERT_DEDUP_S) -> set:
    cutoff = time.time() - window_s
    return {r["market"] for r in conn.execute(
        "SELECT DISTINCT market FROM whale_alerts WHERE ts > ?", (cutoff,))}


def prune_snapshots(conn, max_age_hours: int = SNAPSHOT_RETENTION_HOURS):
    cutoff = time.time() - max_age_hours * 3600
    conn.execute("DELETE FROM whale_snapshots WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM market_state WHERE ts < ?",
                 (time.time() - STATE_RETENTION_HOURS * 3600,))
    # Prune wallet accumulation fills older than retention window
    conn.execute("DELETE FROM wallet_accumulations WHERE ts < ?",
                 (time.time() - ACCUM_RETENTION_S,))
    conn.commit()


# ── HTTP ────────────────────────────────────────────────────────────────────

def _fetch_json(url: str, timeout: int = 20):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def kalshi_fetch(path: str):
    return _fetch_json(f"{KALSHI_API}{path}")


def _fp_float(value) -> Optional[float]:
    """Kalshi *_fp fields are decimal strings; tolerate absence."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── Parsing ─────────────────────────────────────────────────────────────────

def parse_kalshi_book(fp: dict) -> tuple:
    """orderbook_fp -> (bids, asks) on the YES side, best price first.

    yes_dollars are resting YES bids. no_dollars are resting NO bids,
    which are YES asks at (1 - p).
    """
    bids = sorted(((float(p), float(q)) for p, q in fp.get("yes_dollars") or []),
                  key=lambda x: -x[0])
    asks = sorted(((round(1.0 - float(p), 4), float(q)) for p, q in fp.get("no_dollars") or []),
                  key=lambda x: x[0])
    return bids, asks


def book_summary(bids: list, asks: list, oi: Optional[float] = None,
                 volume: Optional[float] = None) -> dict:
    """Reduce a (bids, asks) book to the snapshot we persist and diff."""
    levels = {}
    for p, q in bids:
        levels[f"B:{p:.4f}"] = levels.get(f"B:{p:.4f}", 0.0) + q
    for p, q in asks:
        levels[f"A:{p:.4f}"] = levels.get(f"A:{p:.4f}", 0.0) + q
    return {
        "bid_depth": sum(q for _, q in bids),
        "ask_depth": sum(q for _, q in asks),
        "best_bid": bids[0][0] if bids else None,
        "best_ask": asks[0][0] if asks else None,
        "max_level": max(levels.values()) if levels else 0.0,
        "oi": oi,
        "volume": volume,
        "levels": levels,
    }


# ── Scoring: sweep (executed flow) ──────────────────────────────────────────

def sweep_score(prev: Optional[dict], cur: dict,
                vol_label: str = "vol", oi_label: str = "oi",
                platform: str = "", market: str = "") -> tuple:
    """Score volume/OI deltas between sweeps. prev=None -> unseen market:
    deltas measured from zero (a market trading 500 contracts out of nowhere
    IS the signal), but caller must suppress on platform bootstrap.

    When platform+market are provided, uses per-category thresholds from the
    whale threshold study (noise_floor as vol/oi spike abs minimum)."""
    p_vol = (prev or {}).get("volume") or 0.0
    p_oi = (prev or {}).get("oi") or 0.0
    c_vol = cur.get("volume") or 0.0
    c_oi = cur.get("oi") or 0.0

    # Category-aware thresholds: use noise_floor as the absolute minimum
    # for what counts as a meaningful volume/OI move in this category.
    if platform and market and _CAT_THRESHOLDS:
        cat_t = get_market_thresholds(platform, market)
        nf = cat_t.get("noise_floor", VOL_SPIKE_ABS)
        vol_spike_abs = max(nf, VOL_SPIKE_ABS)
        vol_move_abs = max(nf // 2, VOL_MOVE_ABS)
        oi_spike_abs = max(nf, OI_SPIKE_ABS)
    else:
        vol_spike_abs = VOL_SPIKE_ABS
        vol_move_abs = VOL_MOVE_ABS
        oi_spike_abs = OI_SPIKE_ABS

    score = 0
    reasons = []

    d_vol = c_vol - p_vol
    if d_vol >= vol_spike_abs and d_vol >= VOL_SPIKE_REL * p_vol:
        score += 3
        reasons.append(f"{vol_label}_spike_{d_vol:.0f}")
    elif d_vol >= vol_move_abs and d_vol >= VOL_MOVE_REL * p_vol:
        score += 1
        reasons.append(f"{vol_label}_move_{d_vol:.0f}")

    d_oi = c_oi - p_oi
    if d_oi >= oi_spike_abs and d_oi >= OI_SPIKE_REL * p_oi:
        score += 3
        reasons.append(f"{oi_label}_spike_{d_oi:.0f}")
    elif d_oi >= oi_spike_abs / 2 and d_oi >= OI_SPIKE_REL / 2 * p_oi:
        score += 1
        reasons.append(f"{oi_label}_move_{d_oi:.0f}")

    if p_oi < THIN_FLOW_OI and d_vol >= THIN_FLOW_DELTA:
        score += 2
        reasons.append(f"thin_flow_{d_vol:.0f}")

    return min(score, 10), reasons


# ── Scoring: books (resting orders) ─────────────────────────────────────────

def _ratio(summary: dict) -> float:
    if summary["ask_depth"] > 0:
        return summary["bid_depth"] / summary["ask_depth"]
    return float("inf") if summary["bid_depth"] > 0 else 1.0


def _spread(summary: dict) -> Optional[float]:
    if summary["best_bid"] is not None and summary["best_ask"] is not None:
        return round(summary["best_ask"] - summary["best_bid"], 4)
    return None


def score_change(prev: Optional[dict], cur: dict,
                 platform: str = "", market: str = "") -> tuple:
    """Score the book diff between snapshots. prev=None means first sight:
    establish baseline, never alert. OI/volume deltas are NOT scored here —
    the sweep layer owns executed flow (no double counting).

    When platform+market are provided, uses per-category thresholds from the
    whale threshold study for level jump detection."""
    if prev is None:
        return 0, ["baseline"]

    # Category-aware book thresholds
    if platform and market and _CAT_THRESHOLDS:
        cat_t = get_market_thresholds(platform, market)
        whale_alert = cat_t.get("whale_alert", LEVEL_WHALE)
        # LEVEL_BIG = halfway between noise_floor and whale_alert
        nf = cat_t.get("noise_floor", LEVEL_BASELINE)
        level_big = max(nf, LEVEL_BIG)
        level_baseline = max(nf // 2, LEVEL_BASELINE)
        depth_surge_min = max(nf, DEPTH_SURGE_MIN)
    else:
        whale_alert = LEVEL_WHALE
        level_big = LEVEL_BIG
        level_baseline = LEVEL_BASELINE
        depth_surge_min = DEPTH_SURGE_MIN

    score = 0
    reasons = []

    # 1. Level jump: a price level going from "empty" to whale-sized.
    best_jump, jump_side = 0.0, ""
    for key, qty in cur["levels"].items():
        prev_qty = prev["levels"].get(key, 0.0)
        if prev_qty < level_baseline and qty >= level_big and qty > best_jump:
            best_jump = qty
            jump_side = "bid" if key.startswith("B:") else "ask"
    if best_jump >= whale_alert:
        score += 4
        reasons.append(f"level_jump_{jump_side}_{best_jump:.0f}")
    elif best_jump >= level_big:
        score += 3
        reasons.append(f"level_jump_{jump_side}_{best_jump:.0f}")

    # 2. Depth surge on either side.
    for side in ("bid_depth", "ask_depth"):
        p, c = prev[side] or 0.0, cur[side] or 0.0
        if c >= p * DEPTH_SURGE_MULT and (c - p) >= depth_surge_min:
            score += 2
            reasons.append(f"depth_surge_{side[0].upper()}_{c - p:.0f}")
            break

    # 3. Imbalance flip: balanced book -> extreme one-sidedness.
    pr, cr = _ratio(prev), _ratio(cur)
    prev_mild = (1.0 / IMBAL_MILD) <= pr <= IMBAL_MILD
    cur_extreme = cr >= IMBAL_EXTREME or cr <= (1.0 / IMBAL_EXTREME)
    if prev_mild and cur_extreme:
        score += 2
        reasons.append(f"imbalance_flip_{cr:.1f}x" if cr != float("inf")
                       else "imbalance_flip_inf")

    # 4. Spread collapse: wide market suddenly quoted tight.
    ps, cs = _spread(prev), _spread(cur)
    if ps is not None and cs is not None and ps >= SPREAD_WIDE and cs <= SPREAD_TIGHT:
        score += 2
        reasons.append(f"spread_collapse_{ps:.2f}->{cs:.2f}")

    return min(score, 10), reasons


def _compute_raw_score(sw_score: int, bk_score: int, meta: dict, reasons_list: list) -> tuple:
    """Compute unbounded raw score from sweep + book + market context.

    Returns (raw_score, extra_reasons) where raw_score is unbounded float.
    Applies: log-scaled flow magnitude, flow intensity, taker aggressiveness,
    market maturity penalty, bilateral flow penalty, thin-market penalty.
    """
    raw = float(sw_score + bk_score)
    extra = []

    flow_d = meta.get("flow_dollars") or 0
    flow_yes = meta.get("flow_yes") or 0
    flow_no = meta.get("flow_no") or 0
    total_flow = flow_yes + flow_no
    volume = meta.get("volume") or 0
    close_time = meta.get("close_time") or ""

    # Thin-market penalty: if lifetime volume < $5K, halve the sweep score
    # (relative gates pass trivially on brand-new markets)
    if 0 < volume < 5000:
        raw = float(sw_score - bk_score) * 0.5 + float(bk_score)
        extra.append("thin_market")

    # Flow magnitude: log-scaled bonus
    if flow_d > 0:
        mag = math.log10(max(flow_d, 100) / 100) * 1.5
        if mag > 0:
            raw += mag
            extra.append(f"flow_mag_{flow_d:.0f}")

    # Flow intensity: % of lifetime volume
    if volume > 0 and total_flow > 0:
        intensity = total_flow / volume
        if intensity >= 0.80:
            raw += 2
            extra.append(f"intensity_{intensity:.0%}")
        elif intensity >= 0.50:
            raw += 1
            extra.append(f"intensity_{intensity:.0%}")

    # Taker aggressiveness (from reasons list)
    taker_pct = 0
    for r in reasons_list:
        m = re.search(r'(\d+)%', r) if 'taker_' in r else None
        if m:
            taker_pct = int(m.group(1))
            break
    if taker_pct >= 95:
        raw += 3
        extra.append(f"aggressive_taker_{taker_pct}%")
    elif taker_pct >= 80:
        raw += 2
        extra.append(f"taker_{taker_pct}%")
    elif taker_pct >= 60:
        raw += 1
        extra.append(f"taker_{taker_pct}%")

    # Wallet concentration bonus: single wallet driving >50% of flow = more conviction
    top_wallet_usd = meta.get("top_wallet_usd") or 0
    if flow_d > 0 and top_wallet_usd > 0:
        wallet_share = top_wallet_usd / flow_d
        if wallet_share >= 0.80:
            raw += 3
            extra.append(f"wallet_concentrated_{wallet_share:.0%}")
        elif wallet_share >= 0.50:
            raw += 2
            extra.append(f"wallet_concentrated_{wallet_share:.0%}")
        elif wallet_share >= 0.30:
            raw += 1
            extra.append(f"wallet_{wallet_share:.0%}")

    # Market maturity: penalize markets far from settlement
    if close_time:
        try:
            ct_s = close_time.replace("Z", "+00:00") if close_time.endswith("Z") else close_time
            ct = datetime.fromisoformat(ct_s)
            remaining_days = (ct - datetime.now(timezone.utc)).total_seconds() / 86400
            if remaining_days > 30:
                raw -= 3
                extra.append("immature_30d+")
            elif remaining_days > 14:
                raw -= 1
                extra.append("immature_14d+")
        except Exception:
            pass

    # Bilateral flow penalty: both sides getting hit = less conviction
    if flow_yes > 0 and flow_no > 0:
        total = flow_yes + flow_no
        minority = min(flow_yes, flow_no) / total
        if minority >= 0.30:
            raw *= 0.5
            extra.append("bilateral_heavy")
        elif minority >= 0.10:
            raw *= 0.7
            extra.append("bilateral_moderate")

    # Book-only penalty (no executed flow)
    if flow_d <= 0:
        raw *= 0.5
        extra.append("book_only")

    return max(0.0, round(raw, 1)), extra


def severity_for(score: float) -> str:
    """Map raw_score to severity. Dynamic thresholds."""
    if score >= 10.0:
        return "CRITICAL"
    if score >= 6.0:
        return "HIGH"
    if score >= 3.0:
        return "LOW"
    return "SUPPRESSED"


def load_state(conn, platform: str) -> dict:
    return {r["market"]: {"oi": r["oi"], "volume": r["volume"]}
            for r in conn.execute(
                "SELECT market, oi, volume FROM market_state WHERE platform=?",
                (platform,))}


def upsert_state(conn, platform: str, items: list, ts: Optional[float] = None):
    """items: [(market, oi, volume, title, sub_title)]"""
    now = ts or time.time()
    conn.executemany(
        "INSERT INTO market_state (platform, market, ts, oi, volume, title, sub_title)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(platform, market) DO UPDATE SET"
        "  ts=excluded.ts, oi=excluded.oi, volume=excluded.volume, title=excluded.title, sub_title=excluded.sub_title",
        [(platform, m, now, oi, vol, title, sub) for m, oi, vol, title, sub in items])


def get_weather_series_set() -> set:
    """Series tickers in Kalshi's Climate and Weather category."""
    data = kalshi_fetch("/series?limit=200")
    if not data:
        return set()
    return {s["ticker"] for s in data.get("series", [])
            if s.get("category") == "Climate and Weather"}


# ── Kalshi sweep (exchange-wide trades feed) ───────────────────────────────

def fetch_trades_since(min_ts: int) -> list:
    """All public trades on the exchange since min_ts (epoch seconds)."""
    out, cursor, pages = [], "", 0
    while pages < TRADES_PAGE_CAP:
        path = f"/markets/trades?limit=1000&min_ts={min_ts}" + (f"&cursor={cursor}" if cursor else "")
        d = kalshi_fetch(path)
        if not d:
            break
        trades = d.get("trades", [])
        out.extend(trades)
        pages += 1
        cursor = d.get("cursor") or ""
        if not cursor or not trades:
            break
    if pages >= TRADES_PAGE_CAP:
        logger.warning("Kalshi trades feed hit page cap (%d) — burst truncated", pages)
    return out


def aggregate_trades(trades: list) -> dict:
    """ticker -> {vol, yes_vol, no_vol, dollars, last_yes_price,
    max_single_trade_usd}."""
    agg = {}
    for t in trades:
        ticker = t.get("ticker")
        count = _fp_float(t.get("count_fp")) or 0.0
        if not ticker or count <= 0:
            continue
        a = agg.setdefault(ticker, {"vol": 0.0, "yes_vol": 0.0, "no_vol": 0.0,
                                    "dollars": 0.0, "last_yes_price": None,
                                    "max_single_trade_usd": 0.0})
        a["vol"] += count
        yes_px = _fp_float(t.get("yes_price_dollars")) or 0.0
        if t.get("taker_side") == "yes":
            a["yes_vol"] += count
            trade_usd = count * yes_px
            a["dollars"] += trade_usd
        else:
            a["no_vol"] += count
            trade_usd = count * (_fp_float(t.get("no_price_dollars")) or 0.0)
            a["dollars"] += trade_usd
        if trade_usd > a["max_single_trade_usd"]:
            a["max_single_trade_usd"] = trade_usd
        a["last_yes_price"] = yes_px
    return agg


def fetch_market_details(tickers: list) -> dict:
    """Batch market lookup (100/call): oi, total volume, title, close_time."""
    details = {}
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        d = kalshi_fetch(f"/markets?limit=100&tickers={','.join(chunk)}")
        for m in (d or {}).get("markets", []):
            details[m["ticker"]] = {
                "oi": _fp_float(m.get("open_interest_fp")) or 0.0,
                "volume": _fp_float(m.get("volume_fp")) or 0.0,
                "title": (m.get("title") or "").replace("**", "")[:90],
                "sub_title": m.get("yes_sub_title") or "",
                "close_time": m.get("close_time") or "",
            }
    return details


def flow_direction(flow: dict) -> tuple:
    """(side, pct) of taker flow if one side dominates 2:1, else (None, 0)."""
    y, n = flow.get("yes_vol", 0.0), flow.get("no_vol", 0.0)
    total = y + n
    if total <= 0:
        return None, 0
    if y >= 2 * n:
        return "YES", round(100 * y / total)
    if n >= 2 * y:
        return "NO", round(100 * n / total)
    return None, 0


def kalshi_sweep(conn) -> tuple:
    """Executed-flow detection from the trades feed. Returns (flagged, details).

    flagged: [(score, reasons, ticker)] sorted by score desc.
    details: ticker -> {oi, volume, title, sub_title, close_time, flow...}
    The relative volume gate needs no stored state: prior lifetime volume =
    current total minus this window's flow.
    """
    now = int(time.time())
    since = int(float(kv_get(conn, "kalshi_trades_since", "0") or 0))
    if since <= 0:
        since = now - 300   # first run: one normal window, not the full lookback
    since = max(since, now - TRADES_MAX_LOOKBACK)

    trades = fetch_trades_since(since)
    if trades:
        last_ts = max(int(datetime.fromisoformat(
            t["created_time"].replace("Z", "+00:00")).timestamp())
            for t in trades if t.get("created_time"))
        kv_set(conn, "kalshi_trades_since", str(last_ts))
        conn.commit()
    # empty fetch: keep the old cursor — the feed may lag; the max-lookback
    # clamp at read time bounds the replay window

    agg = aggregate_trades(trades)
    candidates = {t: a for t, a in agg.items()
                  if a["vol"] >= THIN_FLOW_DELTA
                  and not t.startswith(EXCLUDE_SERIES_PREFIXES)}
    logger.info("Kalshi flow sweep: %d trades, %d markets traded, %d candidates",
                len(trades), len(agg), len(candidates))
    if not candidates:
        return [], {}

    details = fetch_market_details(sorted(candidates))
    prev_state = load_state(conn, "kalshi")

    flagged, to_store = [], []
    for ticker, flow in candidates.items():
        det = details.get(ticker)
        if not det:
            continue
        max_single = flow.get("max_single_trade_usd") or 0
        det.update({"flow_yes": flow["yes_vol"], "flow_no": flow["no_vol"],
                    "flow_dollars": flow["dollars"],
                    "max_single_trade_usd": max_single,
                    "last_yes_price": flow["last_yes_price"]})
        prev_oi = (prev_state.get(ticker) or {}).get("oi")
        prev = {"volume": max(det["volume"] - flow["vol"], 0.0),
                "oi": prev_oi if prev_oi is not None else det["oi"]}
        score, reasons = sweep_score(prev, {"volume": det["volume"], "oi": det["oi"]},
                                     platform="kalshi", market=ticker)
        side, pct = flow_direction(flow)
        if side:
            reasons.append(f"taker_{side}_{pct}%")
        # Mega single-trade boost (Kalshi)
        if max_single > 0 and _CAT_THRESHOLDS:
            cat_t = get_market_thresholds("kalshi", ticker)
            if max_single >= cat_t.get("mega_whale", CRITICAL_FLOW_USD):
                score = max(score, 8)
                reasons.append(f"mega_single_trade_${max_single:,.0f}")
            elif max_single >= cat_t.get("whale_alert", CRITICAL_FLOW_USD):
                score = max(score, 5)
                reasons.append(f"whale_single_trade_${max_single:,.0f}")
        if ticker not in prev_state:
            # Sweep-layer twin of the book rule (first sight = baseline):
            # a market we've never tracked trivially passes the relative
            # gates. Tag it; scan_kalshi demotes and skips its book fetch.
            reasons.append("first_sight")
        to_store.append((ticker, det["oi"], det["volume"], det["title"], det.get("sub_title", "")))
        if score >= ALERT_MIN_SCORE:
            flagged.append((score, reasons, ticker))

    upsert_state(conn, "kalshi", to_store)
    conn.commit()
    flagged.sort(key=lambda f: -f[0])
    return flagged, details


# ── Kalshi books (flagged + rotation) ───────────────────────────────────────

def get_kalshi_orderbook(ticker: str) -> Optional[dict]:
    data = kalshi_fetch(f"/markets/{ticker}/orderbook")
    return data.get("orderbook_fp") if data else None


def inspect_kalshi_book(conn, ticker: str, meta: dict) -> tuple:
    """Fetch + diff one book. Returns (book_score, reasons, cur_summary) or
    (None, [], None) on fetch failure. Always persists the snapshot."""
    fp = get_kalshi_orderbook(ticker)
    if fp is None:
        return None, [], None
    bids, asks = parse_kalshi_book(fp)
    cur = book_summary(bids, asks, oi=meta.get("oi"), volume=meta.get("volume"))
    prev = load_prev_snapshot(conn, ticker)
    score, reasons = score_change(prev, cur, platform="kalshi", market=ticker)
    save_snapshot(conn, "kalshi", ticker, cur)
    return score, reasons, cur




def inspect_pm_book(conn, condition_id: str, token_id: str, meta: dict) -> tuple:
    """Fetch Polymarket CLOB orderbook and diff against previous snapshot.
    Returns (book_score, reasons, cur_summary) or (None, [], None) on failure.
    Mirrors inspect_kalshi_book but uses the PM CLOB API."""
    try:
        data = _fetch_json(f"https://clob.polymarket.com/book?token_id={token_id}",
                           timeout=10)
        if not data or "error" in data:
            return None, [], None
    except Exception:
        return None, [], None
    # Convert CLOB format to (price, size_dollars) tuples
    bids = sorted(((float(b["price"]), float(b["size"]) * float(b["price"]))
                   for b in data.get("bids", []) if float(b.get("size", 0)) > 0),
                  key=lambda x: -x[0])
    asks = sorted(((float(a["price"]), float(a["size"]) * float(a["price"]))
                   for a in data.get("asks", []) if float(a.get("size", 0)) > 0),
                  key=lambda x: x[0])
    cur = book_summary(bids, asks,
                       oi=meta.get("oi"),
                       volume=meta.get("volume"))
    # Use condition_id as snapshot key (stable across scans)
    snap_key = f"pm:{condition_id[:16]}"
    prev = load_prev_snapshot(conn, snap_key)
    slug = meta.get("slug", condition_id)
    score, reasons = score_change(prev, cur, platform="polymarket", market=slug)
    save_snapshot(conn, "polymarket", snap_key, cur)
    return score, reasons, cur


def get_weather_watchlist(conn) -> list:
    """All open weather-series market tickers, kv-cached for 1h (53 series
    lookups are too slow to repeat every cycle from the VPS)."""
    ts = float(kv_get(conn, "weather_watchlist_ts", "0") or 0)
    if time.time() - ts < 3600:
        try:
            return json.loads(kv_get(conn, "weather_watchlist", "[]"))
        except json.JSONDecodeError:
            pass
    tickers = []
    for series in sorted(get_weather_series_set()):
        d = kalshi_fetch(f"/markets?series_ticker={series}&status=open&limit=50")
        tickers.extend(m["ticker"] for m in (d or {}).get("markets", []))
    kv_set(conn, "weather_watchlist", json.dumps(tickers))
    kv_set(conn, "weather_watchlist_ts", str(time.time()))
    conn.commit()
    logger.info("Weather watchlist refreshed: %d markets", len(tickers))
    return tickers


def build_watchlist(conn) -> list:
    """Deterministic book-rotation universe: every weather market + every
    thin-active market we've seen trade (resting walls only show in books;
    the trades feed only shows executions)."""
    thin = [r["market"] for r in conn.execute(
        "SELECT market FROM market_state WHERE platform='kalshi'"
        " AND oi BETWEEN ? AND ?", (WATCH_OI_MIN, WATCH_OI_MAX))]
    return sorted(set(get_weather_watchlist(conn)) | set(thin))


def _mk_alert(platform: str, market: str, score: int, reasons: list,
              cur: Optional[dict], meta: Optional[dict]) -> dict:
    # Compute raw_score from sweep + book + market context
    raw_score, raw_reasons = _compute_raw_score(
        min(score, 20), 0, meta or {}, reasons
    )
    all_reasons = reasons + raw_reasons
    alert = {
        "platform": platform,
        "market": market,
        "title": (meta or {}).get("title", ""),
        "score": min(score, 10),
        "raw_score": raw_score,
        "severity": severity_for(raw_score),
        "reasons": ",".join(all_reasons),
        "scan_time": datetime.now(timezone.utc).isoformat(),
    }
    # Cap book-only alerts to HIGH -- CRITICAL requires directional trades
    _meta = meta or {}
    _has_flow = (_meta.get("flow_yes") or 0) + (_meta.get("flow_no") or 0) > 0
    if not _has_flow and alert["severity"] == "CRITICAL":
        alert["severity"] = "HIGH"
    # CRITICAL dollar floor: severity is score (asymmetry) driven, so small
    # but lopsided flows on illiquid novelty markets ($2-7k) were minting
    # CRITICALs. Reserve Telegram-delivered CRITICAL for genuine whale size;
    # smaller high-asymmetry flows stay HIGH (dashboard-visible, not pushed).
    _flow_usd = (meta or {}).get("flow_dollars") or 0
    if alert["severity"] == "CRITICAL" and _flow_usd < CRITICAL_FLOW_USD:
        alert["severity"] = "HIGH"
        all_reasons.append("crit_usd_floor")
        alert["reasons"] = ",".join(all_reasons)
    # Quiet-market rule with pierce conditions (class outlier / smart wallet /
    # pre-game steam) — see apply_livegame_ceiling
    apply_livegame_ceiling(alert, platform, market, meta)
    if cur:
        ratio = _ratio(cur)
        alert.update({
            "best_bid": cur["best_bid"] or 0.0,
            "best_ask": cur["best_ask"] or 0.0,
            "mid": ((cur["best_bid"] or 0.0) + (cur["best_ask"] or 0.0)) / 2,
            "bid_depth": cur["bid_depth"],
            "ask_depth": cur["ask_depth"],
            "max_level": cur["max_level"],
            "ratio": round(ratio, 2) if ratio != float("inf") else -1,
        })
    if meta:
        alert.setdefault("open_interest", meta.get("oi"))
        alert.setdefault("volume", meta.get("volume"))
        for k in ("sub_title", "close_time", "flow_yes", "flow_no",
                  "flow_dollars", "last_yes_price"):
            if meta.get(k) not in (None, ""):
                alert[k] = meta[k]
    return alert


def scan_kalshi(conn) -> list:
    """Trades-feed sweep, then books: flagged first, rotation second."""
    alerts = []
    dedup = recently_alerted(conn)

    flagged, details = kalshi_sweep(conn)
    deadline = time.time() + BOOK_DEADLINE_S   # budget the BOOK phase only

    # ── Per-market accumulation check (Kalshi is anonymous, no wallet IDs) ──
    # details contains the aggregated trade data from kalshi_sweep
    if details:
        accum_alerts = check_kalshi_market_accumulations(conn, details)
        if accum_alerts:
            logger.info("Kalshi market accumulation: %d alerts fired", len(accum_alerts))
            alerts.extend(accum_alerts)

    # 1. Flagged markets: book now, alert on sweep + book combined score.
    gated_n = 0
    for i, (sw_score, sw_reasons, ticker) in enumerate(flagged[:FLAG_BOOK_CAP]):
        if time.time() > deadline:
            logger.warning("Whale book budget exhausted with %d flagged pending",
                           len(flagged) - i)
            break
        det = details.get(ticker, {})
        first_sight = "first_sight" in sw_reasons
        if first_sight:
            # don't spend book budget establishing baselines for births
            bk_score, bk_reasons, cur = 0, [], None
        else:
            bk_score, bk_reasons, cur = inspect_kalshi_book(conn, ticker, det)
        total = sw_score + (bk_score or 0)
        reasons = sw_reasons + [r for r in bk_reasons if r != "baseline"]
        if total >= ALERT_MIN_SCORE and ticker not in dedup:
            gate = alert_gate("kalshi", ticker, det, cur, first_sight=first_sight)
            if gate:
                reasons = reasons + [f"gated_{gate}"]
            alert = _mk_alert("kalshi", ticker, total, reasons, cur, det)
            if gate:
                alert["severity"] = "LOW"   # DB keeps it; drain/CRITICAL skip it
                gated_n += 1
            log_alert(conn, alert)
            if not gate:
                alerts.append(alert)
            dedup.add(ticker)   # rotation phase must see this scan's alerts too
    if gated_n:
        logger.info("Whale gates demoted %d flagged alerts to LOW", gated_n)
    conn.commit()

    # 2. Rotation: cycle the watchlist for resting-wall changes.
    watch = build_watchlist(conn)
    if watch:
        pos = int(kv_get(conn, "kalshi_rotation_pos", "0") or 0) % len(watch)
        inspected = 0
        while inspected < ROTATE_BOOK_CAP and time.time() < deadline:
            ticker = watch[pos]
            pos = (pos + 1) % len(watch)
            inspected += 1
            bk_score, bk_reasons, cur = inspect_kalshi_book(conn, ticker,
                                                            details.get(ticker, {}))
            if bk_score and bk_score >= ALERT_MIN_SCORE and ticker not in dedup:
                gate = alert_gate("kalshi", ticker, details.get(ticker), cur)
                reasons = bk_reasons + ([f"gated_{gate}"] if gate else [])
                alert = _mk_alert("kalshi", ticker, bk_score, reasons, cur,
                                  details.get(ticker))
                if gate:
                    alert["severity"] = "LOW"
                log_alert(conn, alert)
                if not gate:
                    alerts.append(alert)
                dedup.add(ticker)
        kv_set(conn, "kalshi_rotation_pos", str(pos))
        logger.info("Whale rotation: %d books this cycle (watchlist %d)",
                    inspected, len(watch))
    conn.commit()
    return alerts


# ── Polymarket flow sweep (exchange-wide trades feed) ──────────────────────

def fetch_pm_trades_since(since_ts: int) -> list:
    """Taker trades exchange-wide from the Data API, newest first, until we
    pass since_ts. Fields include title/slug/side/outcome/size/price inline."""
    out, offset = [], 0
    for _ in range(PM_TRADES_PAGE_CAP):
        d = _fetch_json(f"{PM_DATA_API}/trades?limit=500&takerOnly=true&offset={offset}")
        if not d:
            break
        out.extend(t for t in d if (t.get("timestamp") or 0) >= since_ts)
        if len(d) < 500 or (d[-1].get("timestamp") or 0) < since_ts:
            break
        offset += 500
    else:
        logger.warning("PM trades feed hit page cap (%d) — burst truncated",
                       PM_TRADES_PAGE_CAP)
    return out


def aggregate_pm_trades(trades: list) -> dict:
    """conditionId -> {dollars, shares, slug, title, flows: {(side,outcome): $},
    last_price, max_single_trade_usd}. PM is multi-outcome, so direction is
    the dominant (side, outcome) pair by dollars.

    max_single_trade_usd tracks the largest individual fill — critical for
    detecting whale bets that get absorbed between sweep cycles (the net
    volume delta may be small but a $662K single trade is always signal)."""
    agg = {}
    for t in trades:
        cid = t.get("conditionId")
        size = t.get("size") or 0
        price = t.get("price") or 0
        if not cid or size <= 0:
            continue
        a = agg.setdefault(cid, {"dollars": 0.0, "shares": 0.0,
                                 "slug": t.get("slug") or "",
                                 "title": (t.get("title") or "")[:80],
                                 "flows": {}, "last_price": price,
                                 "wallets": {}, "wallet_names": {},
                                 "max_single_trade_usd": 0.0})
        usd = size * price
        a["dollars"] += usd
        a["shares"] += size
        if usd > a["max_single_trade_usd"]:
            a["max_single_trade_usd"] = usd
        key = (t.get("side") or "?", (t.get("outcome") or "?")[:20])
        a["flows"][key] = a["flows"].get(key, 0.0) + usd
        a["last_price"] = price
        w = t.get("proxyWallet")
        if w:
            a["wallets"][w] = a["wallets"].get(w, 0.0) + usd
            a["wallet_names"][w] = t.get("name") or t.get("pseudonym") or ""
    return agg


def pm_flow_desc(flow: dict) -> tuple:
    """(reason_tag, human_desc) for the dominant (side, outcome) flow if it
    carries >= 2/3 of dollars, else (None, None)."""
    flows = flow.get("flows") or {}
    total = sum(flows.values())
    if total <= 0:
        return None, None
    (side, outcome), usd = max(flows.items(), key=lambda kv: kv[1])
    pct = round(100 * usd / total)
    if pct < 67:
        return None, None
    tag = f"taker_{side}_{outcome.replace(' ', '_')}_{pct}%"
    return tag, f"{side} {outcome} ${usd:,.0f} ({pct}%)"


def fetch_gamma_by_condition(cids: list) -> dict:
    """conditionId -> gamma market (volumeNum, liquidityNum, question, endDate)."""
    out = {}
    for i in range(0, len(cids), 20):
        chunk = cids[i:i + 20]
        qs = "&".join(f"condition_ids={c}" for c in chunk)
        d = _fetch_json(f"{GAMMA_API}/markets?{qs}&limit=20")
        for m in d or []:
            out[m.get("conditionId")] = m
    return out


def scan_polymarket_flow(conn) -> list:
    """Exchange-wide executed-flow detection on Polymarket (all markets, not
    just the top-1500 window). Same relative+absolute gates as Kalshi; the
    volume baseline = gamma lifetime volumeNum minus this window's dollars."""
    now = int(time.time())
    since = int(float(kv_get(conn, "pm_trades_since", "0") or 0))
    if since <= 0:
        since = now - 900   # data-api indexer can lag several minutes
    since = max(since, now - TRADES_MAX_LOOKBACK)

    trades = fetch_pm_trades_since(since)
    if trades:
        kv_set(conn, "pm_trades_since", str(max(t.get("timestamp") or now for t in trades)))
        conn.commit()
    # empty fetch: keep the old cursor (indexer lag) — see kalshi_sweep

    agg = aggregate_pm_trades(trades)

    # Wallet ledger: a market entered by a PROVEN winner is a candidate even
    # below the anonymous-flow gates, and scores a flat boost.
    try:
        from signals.whale_wallets import get_meta_db, get_smart_wallets, queue_wallet_seen
        meta = get_meta_db()
        smart = get_smart_wallets(meta)
    except Exception as e:
        logger.warning("wallet ledger unavailable: %s", e)
        meta, smart = None, {}

    candidates = {c: a for c, a in agg.items()
                  if (a["dollars"] >= THIN_FLOW_DELTA
                      or any(w in smart and usd >= SMART_WALLET_MIN_USD
                             for w, usd in a["wallets"].items()))
                  and not (a["slug"] or "").startswith(PM_EXCLUDE_SLUG_PREFIXES)}
    logger.info("PM flow sweep: %d trades, %d markets traded, %d candidates",
                len(trades), len(agg), len(candidates))
    if not candidates:
        if meta is not None:
            meta.close()
        return []

    gamma = fetch_gamma_by_condition(sorted(candidates))
    prev_state = load_state(conn, "polymarket")
    dedup = recently_alerted(conn)
    alerts = []

    for cid, flow in candidates.items():
        g = gamma.get(cid)
        if not g:
            continue
        slug = g.get("slug") or flow["slug"]
        vol = g.get("volumeNum") or 0.0
        liq = g.get("liquidityNum") or 0.0
        prev_liq = (prev_state.get(slug) or {}).get("oi")
        prev = {"volume": max(vol - flow["dollars"], 0.0),
                "oi": prev_liq if prev_liq is not None else liq}
        score, reasons = sweep_score(prev, {"volume": vol, "oi": liq},
                                     vol_label="vol$", oi_label="liq$",
                                     platform="polymarket", market=slug)
        tag, desc = pm_flow_desc(flow)
        if tag:
            reasons.append(tag)

        top_wallet, top_usd, top_name = None, 0.0, ""
        if flow["wallets"]:
            top_wallet, top_usd = max(flow["wallets"].items(), key=lambda kv: kv[1])
            top_name = flow["wallet_names"].get(top_wallet, "")
        if meta is not None and top_wallet:
            queue_wallet_seen(meta, top_wallet, top_name, top_usd)
        sw = smart.get(top_wallet) if top_wallet else None
        if sw and top_usd >= SMART_WALLET_MIN_USD:
            score = min(score + SMART_WALLET_SCORE, 10)
            reasons.append(
                f"smart_wallet_{(sw['name'] or top_wallet[:8]).replace(' ', '_')}"
                f"_{sw['win_rate']:.0%}wr")

        # Mega single-trade boost: a single fill above the category's whale_alert
        # threshold is inherently significant — boost score so it passes ALERT_MIN_SCORE
        max_single = flow.get("max_single_trade_usd") or 0
        if max_single > 0 and _CAT_THRESHOLDS:
            cat_t = get_market_thresholds("polymarket", slug)
            if max_single >= cat_t.get("mega_whale", CRITICAL_FLOW_USD):
                score = max(score, 8)
                reasons.append(f"mega_single_trade_${max_single:,.0f}")
            elif max_single >= cat_t.get("whale_alert", CRITICAL_FLOW_USD):
                score = max(score, 5)
                reasons.append(f"whale_single_trade_${max_single:,.0f}")

        if score >= ALERT_MIN_SCORE and slug not in dedup:
            is_smart = bool(sw and top_usd >= SMART_WALLET_MIN_USD)
            gate = alert_gate("polymarket", slug,
                              {"flow_dollars": flow["dollars"],
                               "max_single_trade_usd": max_single,
                               "last_yes_price": flow["last_price"]},
                              None, smart=is_smart)
            if gate:
                reasons = reasons + [f"gated_{gate}"]
            # Compute per-side flow: BUY = buying some outcome = net YES direction,
            # SELL = selling = net NO direction on that outcome
            _flows = flow.get("flows") or {}
            _buy_usd = sum(v for (s, _), v in _flows.items() if s == "BUY")
            _sell_usd = sum(v for (s, _), v in _flows.items() if s == "SELL")
            alert_meta = {"oi": liq, "volume": vol,
                          "title": (g.get("question") or flow["title"])[:80],
                          "close_time": g.get("endDate") or "",
                          "flow_dollars": flow["dollars"],
                          "max_single_trade_usd": max_single,
                          "top_wallet_usd": top_usd if top_wallet else 0,
                          "flow_yes": _buy_usd,
                          "flow_no": _sell_usd}
            # Fetch PM CLOB book and merge book score (parity with Kalshi)
            book_score, book_reasons, book_cur = 0, [], None
            clob_tokens = (g.get("clobTokenIds") or "[]")
            if isinstance(clob_tokens, str):
                import ast
                try: clob_tokens = ast.literal_eval(clob_tokens)
                except Exception: clob_tokens = []
            if clob_tokens:
                book_score, book_reasons, book_cur = inspect_pm_book(
                    conn, cid, clob_tokens[0], alert_meta)
                book_score = book_score or 0
                if book_score > 0:
                    score += book_score
                    reasons = reasons + book_reasons
            alert = _mk_alert("polymarket", slug, score, reasons, book_cur, alert_meta)
            if gate:
                alert["severity"] = "LOW"
            alert["current_price"] = flow["last_price"]
            alert["flow_dollars"] = flow["dollars"]
            alert["condition_id"] = cid
            if desc:
                alert["flow_desc"] = desc
            if top_wallet:
                alert["top_wallet"] = top_wallet
                alert["top_wallet_name"] = top_name
                alert["top_wallet_usd"] = round(top_usd, 2)
                alert["top_wallet_share"] = round(top_usd / flow["dollars"], 2) if flow["dollars"] else 0
            log_alert(conn, alert)
            if not gate:
                alerts.append(alert)
            dedup.add(slug)

    conn.commit()

    # ── Wallet accumulation check (piggybacking on already-fetched trades) ──
    if trades:
        accum_alerts = check_wallet_accumulations(conn, trades)
        if accum_alerts:
            logger.info("Wallet accumulation: %d alerts fired", len(accum_alerts))
            alerts.extend(accum_alerts)

    # Smart-wallet entry/exit alert (shadow-first) — fires independently of the
    # anonymous-flow gates above when a GRADUATED wallet accumulates >=$500 in a
    # 4h window. Distinct from check_wallet_accumulations (anonymous $50K+ flow).
    # Never raises into the scan. See scripts/smart_wallet_alert.py.
    if meta is not None and smart:
        try:
            from scripts.smart_wallet_alert import scanner_hook
            sw_fired = scanner_hook(meta, trades, gamma, smart)
            # Route entry-type alerts to live executor (no-op in PAPER mode)
            if sw_fired:
                try:
                    from scripts.smart_wallet_fast_poll import _route_live_smart_wallet
                    _route_live_smart_wallet(sw_fired, gamma)
                except Exception as _re:
                    logger.warning("smart_wallet_alert live routing failed: %s", _re)
        except Exception as e:  # noqa: BLE001
            logger.warning("smart_wallet_alert hook failed: %s", e)

    if meta is not None:
        meta.commit()
        meta.close()
    return alerts


# ── Polymarket sweep ────────────────────────────────────────────────────────

def fetch_pm_markets() -> list:
    """Top markets by 24h volume from Gamma (100/page). A whale hitting a thin
    market spikes its volume24hr, which ranks it into this window."""
    out = []
    for page in range(PM_SWEEP_PAGES):
        d = _fetch_json(
            f"{GAMMA_API}/markets?closed=false&active=true&limit=100"
            f"&order=volume24hr&ascending=false&offset={page * 100}")
        if not d:
            break
        out.extend(d)
        if len(d) < 100:
            break
    return out


def scan_polymarket(conn) -> list:
    """Executed-flow sweep on Polymarket. Book-level wall detection on PM is
    whale_wall_scanner's job; this layer catches volume/liquidity shocks.
    Units are dollars; liquidityNum stands in the OI slot."""
    alerts = []
    markets = fetch_pm_markets()
    if not markets:
        return []

    prev_state = load_state(conn, "polymarket")
    bootstrap = not prev_state and kv_get(conn, "pm_sweep_bootstrapped") != "1"
    dedup = recently_alerted(conn)

    to_store = []
    for m in markets:
        slug = m.get("slug")
        if not slug:
            continue
        vol = m.get("volumeNum") or 0.0
        liq = m.get("liquidityNum") or 0.0
        title = (m.get("question") or slug)[:80]
        to_store.append((slug, liq, vol, title, ""))
        if bootstrap or slug not in prev_state:
            continue  # entering the volume24hr window is not a delta

        score, reasons = sweep_score(
            {**prev_state[slug], "volume": vol}, {"oi": liq, "volume": vol},
            vol_label="vol$", oi_label="liq$",
            platform="polymarket", market=slug)  # liq deltas only; flow sweep owns volume
        if score >= ALERT_MIN_SCORE and slug not in dedup:
            alert = _mk_alert("polymarket", slug, score, reasons, None,
                              {"oi": liq, "volume": vol, "title": title})
            alert["current_price"] = m.get("lastTradePrice") or 0.0
            log_alert(conn, alert)
            alerts.append(alert)

    upsert_state(conn, "polymarket", to_store)
    kv_set(conn, "pm_sweep_bootstrapped", "1")
    conn.commit()
    if bootstrap:
        logger.info("Polymarket sweep bootstrap: %d markets stored, no alerts",
                    len(to_store))
    return alerts


# ── Entry points ────────────────────────────────────────────────────────────

# ── Wallet accumulation tracking ─────────────────────────────────────────────

def _accum_recently_alerted(conn, wallet: str, market: str) -> bool:
    """Check if this wallet+market combo was alerted within the cooldown."""
    cutoff = time.time() - ACCUM_DEDUP_S
    row = conn.execute(
        "SELECT 1 FROM accumulation_alerts WHERE wallet=? AND market=? AND ts>?",
        (wallet, market, cutoff)).fetchone()
    return row is not None


def _accum_hour_count(conn) -> int:
    """Count accumulation alerts in the last hour (rate limiting)."""
    cutoff = time.time() - 3600
    row = conn.execute(
        "SELECT COUNT(*) FROM accumulation_alerts WHERE ts>?", (cutoff,)).fetchone()
    return row[0] if row else 0


def check_wallet_accumulations(conn, trades: list) -> list:
    """Per-wallet-per-market accumulation over rolling window.
    Called after aggregate_pm_trades() with the raw trades list.
    Returns list of alerts for wallets crossing thresholds."""
    now = time.time()
    alerts = []

    # 1. Group current trades by (wallet, conditionId) and insert fills
    wallet_fills = {}  # (wallet, cid) -> {usd, count, side, slug, title, name}
    for t in trades:
        wallet = t.get("proxyWallet")
        cid = t.get("conditionId")
        size = t.get("size") or 0
        price = t.get("price") or 0
        if not wallet or not cid or size <= 0:
            continue
        usd = size * price
        key = (wallet, cid)
        if key not in wallet_fills:
            wallet_fills[key] = {
                "usd": 0, "count": 0,
                "side": t.get("side", "?"),
                "slug": t.get("slug", ""),
                "title": (t.get("title") or "")[:80],
                "name": t.get("name") or t.get("pseudonym") or "",
            }
        wallet_fills[key]["usd"] += usd
        wallet_fills[key]["count"] += 1

    if not wallet_fills:
        return alerts

    # 2. Batch insert fills
    rows = []
    for (wallet, cid), info in wallet_fills.items():
        rows.append((now, "polymarket", wallet, info["name"], cid,
                      info["slug"], info["title"], info["side"],
                      info["usd"], info["count"]))
    conn.executemany(
        "INSERT INTO wallet_accumulations"
        " (ts, platform, wallet, wallet_name, market, slug, title, side,"
        "  fill_usd, fill_count) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()

    # 3. For each wallet that traded this cycle, sum the rolling window
    wallets_seen = set(w for (w, _) in wallet_fills)
    cutoff = now - ACCUM_WINDOW_S

    for wallet in wallets_seen:
        # Sum all fills for this wallet across ALL markets in the window
        wallet_markets = conn.execute(
            "SELECT market, slug, title, wallet_name,"
            " SUM(fill_usd) as total_usd, SUM(fill_count) as total_fills,"
            " MIN(ts) as first_ts, MAX(ts) as last_ts"
            " FROM wallet_accumulations"
            " WHERE wallet=? AND ts>?"
            " GROUP BY market",
            (wallet, cutoff)).fetchall()

        for row in wallet_markets:
            total_usd = row["total_usd"]
            market = row["market"]
            fills = row["total_fills"]
            title = row["title"] or ""
            slug = row["slug"] or ""
            name = row["wallet_name"] or wallet[:12]
            duration_min = (row["last_ts"] - row["first_ts"]) / 60

            # Determine level
            if total_usd >= ACCUM_MEGA_USD:
                level = "mega"
                severity = "CRITICAL"
            elif total_usd >= ACCUM_WHALE_USD:
                level = "whale"
                severity = "HIGH"
            elif total_usd >= ACCUM_NOTABLE_USD:
                level = "notable"
                severity = None  # DB only, no TG
            else:
                continue

            # Dedup check
            if _accum_recently_alerted(conn, wallet, market):
                continue

            # Rate limit check
            if severity and _accum_hour_count(conn) >= ACCUM_TG_RATE_LIMIT:
                severity = None  # demote to DB only

            # Log to DB
            conn.execute(
                "INSERT INTO accumulation_alerts"
                " (ts, platform, wallet, market, rolling_usd, level,"
                "  fill_count, title)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (now, "polymarket", wallet, market, total_usd, level,
                 fills, title))

            if severity:
                # Determine dominant side from recent fills
                side_rows = conn.execute(
                    "SELECT side, SUM(fill_usd) as s FROM wallet_accumulations"
                    " WHERE wallet=? AND market=? AND ts>? GROUP BY side"
                    " ORDER BY s DESC LIMIT 1",
                    (wallet, market, cutoff)).fetchone()
                dom_side = side_rows["side"] if side_rows else "?"

                alert = {
                    "type": "accumulation",
                    "platform": "polymarket",
                    "market": slug or market,
                    "title": title,
                    "severity": severity,
                    "score": 9 if level == "mega" else 6,
                    "reasons": f"wallet_accum_{level},${total_usd:,.0f}_in_{duration_min:.0f}min,{fills}_fills",
                    "flow_dollars": total_usd,
                    "flow_yes": total_usd if dom_side == "BUY" else 0,
                    "flow_no": total_usd if dom_side == "SELL" else 0,
                    "top_wallet": wallet,
                    "top_wallet_name": name,
                    "top_wallet_usd": total_usd,
                    "accum_fills": fills,
                    "accum_duration_min": round(duration_min, 1),
                    "accum_level": level,
                    "scan_time": datetime.now(timezone.utc).isoformat(),
                }
                log_alert(conn, alert)


                alerts.append(alert)

    conn.commit()
    return alerts


def check_kalshi_market_accumulations(conn, trade_agg: dict) -> list:
    """Per-market accumulation for Kalshi (anonymous trades — no wallet IDs).
    If a single market receives heavy flow within the rolling window, alert.
    Called after aggregate_trades() with the aggregated dict."""
    now = time.time()
    alerts = []

    # Insert per-market fills (wallet='_anonymous_' since Kalshi is anonymous)
    rows = []
    for ticker, agg in trade_agg.items():
        flow_usd = agg.get("flow_dollars") or agg.get("dollars") or 0
        if flow_usd < 100:  # skip noise
            continue
        flow_yes = agg.get("flow_yes") or agg.get("yes_vol", 0)
        flow_no = agg.get("flow_no") or agg.get("no_vol", 0)
        # Estimate fill count from total contracts (each trade = ~1-100 contracts)
        est_fills = max(1, int((flow_yes + flow_no) / 50)) if (flow_yes + flow_no) > 0 else 1
        rows.append((now, "kalshi", "_anonymous_", "", ticker,
                      ticker, (agg.get("title") or ticker)[:80],
                      "YES" if flow_yes > flow_no else "NO",
                      flow_usd, est_fills))
    if rows:
        conn.executemany(
            "INSERT INTO wallet_accumulations"
            " (ts, platform, wallet, wallet_name, market, slug, title, side,"
            "  fill_usd, fill_count) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()

    # Check rolling window per market
    cutoff = now - ACCUM_WINDOW_S
    # Only check markets that had flow this cycle
    for ticker in trade_agg:
        row = conn.execute(
            "SELECT SUM(fill_usd) as total_usd, SUM(fill_count) as total_fills,"
            " MIN(ts) as first_ts, MAX(ts) as last_ts"
            " FROM wallet_accumulations"
            " WHERE platform='kalshi' AND market=? AND ts>?",
            (ticker, cutoff)).fetchone()

        if not row or not row["total_usd"]:
            continue
        total_usd = row["total_usd"]
        fills = row["total_fills"]
        duration_min = (row["last_ts"] - row["first_ts"]) / 60

        if total_usd >= ACCUM_KALSHI_MEGA:
            level = "mega"
            severity = "CRITICAL"
        elif total_usd >= ACCUM_KALSHI_WHALE:
            level = "whale"
            severity = "HIGH"
        else:
            continue

        if _accum_recently_alerted(conn, "_anonymous_", ticker):
            continue
        if _accum_hour_count(conn) >= ACCUM_TG_RATE_LIMIT:
            severity = None

        title_str = trade_agg[ticker].get("title", ticker)[:80]
        conn.execute(
            "INSERT INTO accumulation_alerts"
            " (ts, platform, wallet, market, rolling_usd, level,"
            "  fill_count, title)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (now, "kalshi", "_anonymous_", ticker, total_usd, level,
             fills, title_str))

        if severity:
            agg = trade_agg.get(ticker, {})
            alert = {
                "type": "accumulation",
                "platform": "kalshi",
                "market": ticker,
                "title": title_str,
                "severity": severity,
                "score": 9 if level == "mega" else 6,
                "reasons": f"market_accum_{level},${total_usd:,.0f}_in_{duration_min:.0f}min,{fills}_fills",
                "flow_dollars": total_usd,
                "flow_yes": agg.get("flow_yes") or agg.get("yes_vol", 0),
                "flow_no": agg.get("flow_no") or agg.get("no_vol", 0),
                "accum_fills": fills,
                "accum_duration_min": round(duration_min, 1),
                "accum_level": level,
                "scan_time": datetime.now(timezone.utc).isoformat(),
            }
            log_alert(conn, alert)
            alerts.append(alert)

    conn.commit()
    return alerts


def run_scan(platform: str = "all") -> list:
    """Full scan. Persists state/snapshots/alerts; returns alerts by score.

    Called by services/scheduler.py task_whale_scanner() on its own loop.
    """
    _load_thresholds()
    conn = get_db()
    alerts = []
    try:
        prune_snapshots(conn)  # also prunes wallet_accumulations
        refresh_class_thresholds(conn)
        if platform in ("all", "polymarket"):
            alerts.extend(scan_polymarket_flow(conn))
            alerts.extend(scan_polymarket(conn))
        if platform in ("all", "kalshi"):
            alerts.extend(scan_kalshi(conn))
    finally:
        conn.commit()
        conn.close()

    alerts.sort(key=lambda a: a.get("raw_score", a["score"]), reverse=True)
    return alerts


def format_alert(a: dict) -> str:
    sev_icon = "🚨" if a["severity"] == "CRITICAL" else "⚠️" if a["severity"] == "HIGH" else "🐟"
    plat_icon = "📊" if a["platform"] == "kalshi" else "🔵"

    raw = a.get("raw_score", a["score"])
    lines = [
        f"{sev_icon} {a['severity']} | {plat_icon} {a['platform'].upper()}",
        f"Market: {a['market']}" + (f" — {a['title']}" if a.get("title") else ""),
        f"Score: {a['score']}/10 | raw: {raw:.1f} | {a['reasons']}",
    ]
    if "best_bid" in a:
        lines.append(f"Bid/Ask: {a['best_bid']:.3f} / {a['best_ask']:.3f} (mid={a['mid']:.3f})")
    if "current_price" in a:
        lines.append(f"Price: {a['current_price']:.3f}")
    if "bid_depth" in a:
        lines.append(f"Depth: {a['bid_depth']:.0f}B / {a['ask_depth']:.0f}A ({a['ratio']:.1f}x)")

    if a["platform"] == "kalshi":
        lines.append(f"Link: https://kalshi.com/markets/{a['market'].split('-')[0]}")
    else:
        lines.append(f"Link: https://polymarket.com/market/{a['market']}")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-score", type=int, default=ALERT_MIN_SCORE)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--platform", choices=["kalshi", "polymarket", "all"],
                        default="all")
    args = parser.parse_args()

    results = run_scan(platform=args.platform)
    filtered = [a for a in results if a["score"] >= args.min_score]

    if args.json:
        print(json.dumps(filtered, indent=2))
    else:
        print(f"\n=== WHALE SCAN: {len(filtered)} alerts (score >= {args.min_score}) ===\n")
        for a in filtered:
            print(format_alert(a))
            print()
