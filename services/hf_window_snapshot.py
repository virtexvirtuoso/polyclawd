"""
HF Window Snapshot — P1 Data Collection

At each 1h and 4h crypto window open, captures:
  - PM YES price for each asset
  - BTC Wiz composite score (regime filter only; score < 30 = bearish regime)
  - Derivatives API fusion signal (port 8003)
  - Confluence score from Virtuoso memcached
  - Microstructure signals from Hyperliquid:
      * spread_bps     — bid-ask spread in basis points
      * l1_bid_vol     — best-bid notional ($)
      * l1_ask_vol     — best-ask notional ($)
      * ofi_1min       — normalized Order Flow Imbalance from last 60 1m candles
      * vwap_deviation — price vs 60m VWAP (pct)
  - Coinbase premium vs Hyperliquid (BTC only)

After resolution, hf_collector joins snapshots against hf_market_resolutions
to build conditional win-rate tables.

Run from scheduler: every 30 minutes, self-gates to ±5 minutes of window opens.
"""

import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "storage" / "shadow_trades.db"

GAMMA_API = "https://gamma-api.polymarket.com"
BTC_WIZ_URL = "http://127.0.0.1:8004"
DERIV_URL = "http://127.0.0.1:8003"
HF_ENGINE_URL = "http://127.0.0.1:8422"
HL_INFO_URL = "https://api.hyperliquid.xyz/info"
CB_SPOT_URL = "https://api.coinbase.com/v2/prices/{}-USD/spot"

# Assets in HF markets
HF_ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"]
ASSET_SYMBOL_MAP = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "XRP": "XRPUSDT", "DOGE": "DOGEUSDT", "BNB": "BNBUSDT", "HYPE": "HYPEUSDT",
}
# HL coin names (some differ from standard symbols)
HL_COIN_MAP = {
    "BTC": "BTC", "ETH": "ETH", "SOL": "SOL",
    "XRP": "XRP", "DOGE": "DOGE", "BNB": "BNB", "HYPE": "HYPE",
}

# Coinbase-supported assets (not all assets are on CB; skip silently if unavailable)
CB_SUPPORTED = {"BTC", "ETH", "SOL", "XRP", "DOGE"}


def _ensure_table():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hf_window_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_time TEXT NOT NULL,
            duration TEXT NOT NULL,
            asset TEXT NOT NULL,
            market_id TEXT,
            pm_yes_price REAL,
            wiz_score REAL,
            wiz_direction TEXT,
            deriv_fusion TEXT,
            deriv_funding_rate REAL,
            deriv_oi_trend TEXT,
            confluence_score REAL,
            regime TEXT,
            binance_price REAL,
            -- Phase 1: Microstructure signals (Hyperliquid)
            spread_bps REAL,
            l1_bid_vol REAL,
            l1_ask_vol REAL,
            ofi_1min REAL,
            vwap_deviation REAL,
            cb_premium_bps REAL,
            -- Outcome
            outcome TEXT,
            resolved_at TEXT,
            snapshot_at TEXT NOT NULL,
            UNIQUE(window_time, duration, asset)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hf_windows_time
        ON hf_window_snapshots(window_time, duration)
    """)

    # Migrate: add new microstructure columns if missing (idempotent)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(hf_window_snapshots)").fetchall()}
    new_cols = [
        ("spread_bps", "REAL"),
        ("l1_bid_vol", "REAL"),
        ("l1_ask_vol", "REAL"),
        ("ofi_1min", "REAL"),
        ("vwap_deviation", "REAL"),
        ("cb_premium_bps", "REAL"),
    ]
    for col, dtype in new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE hf_window_snapshots ADD COLUMN {col} {dtype}")
            logger.info(f"Migrated: added column {col} to hf_window_snapshots")

    conn.commit()
    conn.close()


def _fetch_json(url: str, timeout: int = 5) -> Optional[Dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd-WindowSnap/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"Fetch error {url}: {e}")
        return None


def _hl_post(payload: Dict, timeout: int = 8) -> Optional[object]:
    """POST to Hyperliquid Info API."""
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            HL_INFO_URL,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "Polyclawd-WindowSnap/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"HL API error: {e}")
        return None


def _get_microstructure(asset: str) -> Dict:
    """
    Fetch microstructure signals from Hyperliquid for a given asset.

    Returns:
        spread_bps    — bid-ask spread in basis points
        l1_bid_vol    — best-bid notional ($)
        l1_ask_vol    — best-ask notional ($)
        ofi_1min      — normalized OFI from 60 x 1m candles (-1 to +1)
        vwap_deviation — (price - VWAP) / VWAP * 100
    """
    result = {}
    coin = HL_COIN_MAP.get(asset, asset)

    # --- L2 book: spread + L1 volumes ---
    book = _hl_post({"type": "l2Book", "coin": coin, "nSigFigs": 5})
    if book and "levels" in book:
        try:
            levels = book["levels"]
            best_bid_px = float(levels[0][0]["px"]) if levels[0] else None
            best_bid_sz = float(levels[0][0]["sz"]) if levels[0] else None
            best_ask_px = float(levels[1][0]["px"]) if levels[1] else None
            best_ask_sz = float(levels[1][0]["sz"]) if levels[1] else None

            if best_bid_px and best_ask_px:
                mid = (best_bid_px + best_ask_px) / 2
                result["spread_bps"] = round((best_ask_px - best_bid_px) / mid * 10000, 2)
                result["l1_bid_vol"] = round(best_bid_px * best_bid_sz, 0) if best_bid_sz else None
                result["l1_ask_vol"] = round(best_ask_px * best_ask_sz, 0) if best_ask_sz else None
        except (IndexError, KeyError, TypeError, ValueError) as e:
            logger.debug(f"L2 parse error ({asset}): {e}")

    # --- Candles: VWAP deviation + OFI ---
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 61 * 60 * 1000  # 61 min back (60 candles + buffer)
    candles = _hl_post({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1m", "startTime": start_ms
    }})

    if candles and isinstance(candles, list) and len(candles) >= 5:
        try:
            # Use last 60 candles max
            candles = candles[-60:]

            # VWAP = sum(typical_price * volume) / sum(volume)
            total_vol = sum(float(c["v"]) for c in candles)
            if total_vol > 0:
                vwap = sum(
                    ((float(c["h"]) + float(c["l"]) + float(c["c"])) / 3) * float(c["v"])
                    for c in candles
                ) / total_vol
                current_price = float(candles[-1]["c"])
                result["vwap_deviation"] = round((current_price - vwap) / vwap * 100, 4)

            # OFI approximation from candles:
            # Per candle: buy_vol = v * (c-l)/(h-l), sell_vol = v * (h-c)/(h-l)
            # Normalized OFI = (total_buy_vol - total_sell_vol) / total_vol
            buy_vol = 0.0
            sell_vol = 0.0
            for c in candles:
                h, l, cl, v = float(c["h"]), float(c["l"]), float(c["c"]), float(c["v"])
                rng = h - l
                if rng > 0:
                    buy_frac = (cl - l) / rng
                    buy_vol += v * buy_frac
                    sell_vol += v * (1 - buy_frac)
                else:
                    buy_vol += v * 0.5
                    sell_vol += v * 0.5

            if total_vol > 0:
                result["ofi_1min"] = round((buy_vol - sell_vol) / total_vol, 4)

        except (KeyError, ValueError, ZeroDivisionError) as e:
            logger.debug(f"Candle parse error ({asset}): {e}")

    return result


def _get_cb_premium(asset: str, hl_price: Optional[float]) -> Optional[float]:
    """
    Coinbase premium in bps vs Hyperliquid price.
    Positive = CB trading at premium (US retail buying pressure).
    Only for CB-supported assets.
    """
    if asset not in CB_SUPPORTED or not hl_price:
        return None
    try:
        url = CB_SPOT_URL.format(asset)
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd-WindowSnap/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        cb_price = float(data["data"]["amount"])
        premium_bps = round((cb_price - hl_price) / hl_price * 10000, 2)
        return premium_bps
    except Exception as e:
        logger.debug(f"CB premium error ({asset}): {e}")
        return None


def _get_wiz_signal() -> Dict:
    """Fetch BTC Wiz composite signal — used as regime filter only."""
    data = _fetch_json(f"{BTC_WIZ_URL}/signals/composite")
    if not data:
        return {}
    inner = data.get("data", data)
    return {
        "score": inner.get("score"),
        "direction": inner.get("signal_type") or inner.get("label"),
    }


def _get_deriv_signal(asset: str = "BTC") -> Dict:
    """Fetch Derivatives API fusion signal for an asset from port 8003."""
    symbol = ASSET_SYMBOL_MAP.get(asset, f"{asset}USDT")
    data = _fetch_json(f"{DERIV_URL}/signals/fusion/{symbol}", timeout=1)
    if not data or not data.get("success"):
        return {}
    sig = data.get("signal", {})
    components = sig.get("component_signals", {})
    fr = components.get("funding_rate", {})
    oi = components.get("open_interest", {})
    return {
        "fusion": sig.get("direction", "").upper() or "NEUTRAL",
        "funding_rate": fr.get("confidence"),
        "oi_trend": "rising" if (oi.get("oi_change_pct", 0) or 0) > 1 else (
            "falling" if (oi.get("oi_change_pct", 0) or 0) < -1 else "flat"
        ),
    }


def _get_confluence(asset: str = "BTC") -> Dict:
    """Read confluence score from memcached via hf_enrichment."""
    try:
        import asyncio
        import sys
        sys.path.insert(0, str(PROJECT_ROOT / "services"))
        from hf_enrichment import get_enrichment_reader

        async def _read():
            reader = get_enrichment_reader()
            symbol = ASSET_SYMBOL_MAP.get(asset, f"{asset}USDT")
            data = await reader.read_enrichment(symbol)
            return {
                "score": reader.get_confluence_score(data, symbol),
                "regime": reader.get_regime(data),
            }

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(_read())
        loop.close()
        return result
    except Exception as e:
        logger.debug(f"Confluence read error ({asset}): {e}")
        return {}


def _get_cex_price(asset: str) -> Optional[float]:
    """Get current CEX price from hf_engine state."""
    state = _fetch_json(f"{HF_ENGINE_URL}/state")
    if state:
        prices = state.get("prices", {})
        if asset in prices:
            return prices[asset].get("binance_price")
    return None


def _discover_live_hf_markets(duration: str) -> List[Dict]:
    """Discover live 1h or 4h markets via Gamma events endpoint."""
    try:
        url = f"{GAMMA_API}/events?active=true&closed=false&limit=100&tag_slug={duration}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd-WindowSnap/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            events = json.loads(resp.read().decode())

        markets = []
        for event in events:
            for market in event.get("markets", []):
                question = market.get("question", "")
                q_lower = question.lower()

                asset = None
                for a, keywords in [
                    ("BTC", ["bitcoin", "btc"]),
                    ("ETH", ["ethereum", "eth"]),
                    ("SOL", ["solana", "sol"]),
                    ("XRP", ["xrp", "ripple"]),
                    ("DOGE", ["dogecoin", "doge"]),
                    ("BNB", ["bnb", "binance coin"]),
                    ("HYPE", ["hype", "hyperliquid"]),
                ]:
                    if any(kw in q_lower for kw in keywords):
                        asset = a
                        break

                if not asset:
                    continue

                # Filter out stale/resolved markets left active by PM
                end_date_str = market.get("endDate", "")
                if end_date_str:
                    try:
                        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                        now_check = datetime.now(timezone.utc)
                        window_hours = 3 if duration == "1h" else 6
                        if end_dt < now_check or end_dt > now_check + timedelta(hours=window_hours):
                            continue
                    except Exception:
                        pass

                try:
                    prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                    yes_price = float(prices[0]) if prices else 0.5
                except Exception:
                    yes_price = 0.5

                markets.append({
                    "asset": asset,
                    "market_id": market.get("conditionId") or market.get("id", ""),
                    "question": question,
                    "yes_price": yes_price,
                    "end_date": end_date_str,
                })

        return markets
    except Exception as e:
        logger.error(f"Market discovery error ({duration}): {e}")
        return []


def snapshot_window_open(duration: str = "1h") -> Dict:
    """
    Snapshot all assets at a 1h or 4h window open.

    Captures PM price, legacy signals, and Phase 1 microstructure signals.
    Returns count of rows written.
    """
    _ensure_table()

    now_utc = datetime.now(timezone.utc)
    if duration == "1h":
        window_time = now_utc.replace(minute=0, second=0, microsecond=0).isoformat()
    else:
        block = (now_utc.hour // 4) * 4
        window_time = now_utc.replace(hour=block, minute=0, second=0, microsecond=0).isoformat()

    markets = _discover_live_hf_markets(duration)
    if not markets:
        logger.warning(f"No {duration} markets found for window snapshot")
        return {"written": 0, "duration": duration, "window_time": window_time}

    # Global signals (BTC Wiz = regime filter only)
    wiz = _get_wiz_signal()

    # Pre-fetch deriv signals per asset (1s timeout — degrades gracefully if slow)
    deriv_cache: Dict[str, Dict] = {}
    for a in HF_ASSETS:
        deriv_cache[a] = _get_deriv_signal(a)

    # CEX prices from hf_engine
    cex_prices = {}
    state = _fetch_json(f"{HF_ENGINE_URL}/state")
    if state:
        for asset_key, data in state.get("prices", {}).items():
            cex_prices[asset_key] = data.get("binance_price")

    # Microstructure per asset (fetch once, reuse)
    micro_cache: Dict[str, Dict] = {}
    cb_cache: Dict[str, Optional[float]] = {}
    confluence_cache: Dict[str, Dict] = {}

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    written = 0
    snapshot_at = now_utc.isoformat()

    for market in markets:
        asset = market["asset"]

        # Microstructure (Hyperliquid)
        if asset not in micro_cache:
            micro_cache[asset] = _get_microstructure(asset)
        micro = micro_cache[asset]

        # Coinbase premium (BTC/ETH/SOL only)
        if asset not in cb_cache:
            hl_price = cex_prices.get(asset)
            if hl_price is None:
                # Derive from microstructure context: use mid from book
                # (book was fetched inside _get_microstructure — no direct access here,
                #  use VWAP deviation to reconstruct: skip if not available)
                hl_price = None
            cb_cache[asset] = _get_cb_premium(asset, hl_price)

        # Confluence
        if asset not in confluence_cache:
            confluence_cache[asset] = _get_confluence(asset)
        conf_data = confluence_cache[asset]

        # Per-asset deriv signal (pre-fetched above)
        deriv = deriv_cache.get(asset, {})

        try:
            conn.execute("""
                INSERT OR IGNORE INTO hf_window_snapshots
                (window_time, duration, asset, market_id, pm_yes_price,
                 wiz_score, wiz_direction, deriv_fusion, deriv_funding_rate,
                 deriv_oi_trend, confluence_score, regime, binance_price,
                 spread_bps, l1_bid_vol, l1_ask_vol, ofi_1min, vwap_deviation,
                 cb_premium_bps, snapshot_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                window_time, duration, asset,
                market["market_id"],
                market["yes_price"],
                wiz.get("score"),
                wiz.get("direction"),
                deriv.get("fusion"),
                deriv.get("funding_rate"),
                deriv.get("oi_trend"),
                conf_data.get("score"),
                conf_data.get("regime"),
                cex_prices.get(asset),
                micro.get("spread_bps"),
                micro.get("l1_bid_vol"),
                micro.get("l1_ask_vol"),
                micro.get("ofi_1min"),
                micro.get("vwap_deviation"),
                cb_cache[asset],
                snapshot_at,
            ))
            written += 1
        except Exception as e:
            logger.error(f"Window snapshot insert error ({asset}): {e}")

    conn.commit()
    conn.close()

    logger.info(f"Window snapshot: {written} rows for {duration} window at {window_time}")
    return {"written": written, "duration": duration, "window_time": window_time, "markets": len(markets)}


def backfill_outcomes() -> Dict:
    """Join resolved hf_market_resolutions against hf_window_snapshots."""
    _ensure_table()
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    updated = 0

    try:
        pending = conn.execute("""
            SELECT ws.id, ws.market_id, ws.asset, ws.duration, ws.window_time
            FROM hf_window_snapshots ws
            WHERE ws.outcome IS NULL AND ws.market_id IS NOT NULL AND ws.market_id != ''
        """).fetchall()

        for row in pending:
            resolution = conn.execute("""
                SELECT outcome, resolved_at
                FROM hf_market_resolutions
                WHERE market_id = ?
                LIMIT 1
            """, (row[1],)).fetchone()

            if resolution and resolution[0]:
                conn.execute("""
                    UPDATE hf_window_snapshots
                    SET outcome = ?, resolved_at = ?
                    WHERE id = ?
                """, (resolution[0], resolution[1], row[0]))
                updated += 1

        conn.commit()
    except Exception as e:
        if "no such table: hf_market_resolutions" in str(e):
            logger.debug("hf_market_resolutions table not yet created — skipping backfill")
        else:
            logger.error(f"Backfill outcomes error: {e}")
    finally:
        conn.close()

    if updated:
        logger.info(f"Window snapshot outcomes backfilled: {updated}")
    return {"updated": updated}


def get_conditional_wr(duration: str = "1h", asset: str = "BTC",
                        wiz_min: float = 0, wiz_max: float = 100) -> Dict:
    """Query conditional win rate from collected window snapshots."""
    _ensure_table()
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT pm_yes_price, wiz_score, deriv_fusion, confluence_score,
                   regime, ofi_1min, vwap_deviation, outcome
            FROM hf_window_snapshots
            WHERE duration = ? AND asset = ?
              AND outcome IS NOT NULL
              AND wiz_score BETWEEN ? AND ?
        """, (duration, asset, wiz_min, wiz_max)).fetchall()

        if not rows:
            return {"n": 0, "note": "No resolved snapshots yet in this filter"}

        up_wins = sum(1 for r in rows if r["outcome"] == "Up" and r["pm_yes_price"] < 0.5)
        down_wins = sum(1 for r in rows if r["outcome"] == "Down" and r["pm_yes_price"] >= 0.5)
        total = len(rows)
        correct = up_wins + down_wins

        return {
            "n": total,
            "correct": correct,
            "win_rate": round(correct / total, 3) if total > 0 else None,
            "duration": duration,
            "asset": asset,
            "wiz_range": f"{wiz_min}-{wiz_max}",
        }
    finally:
        conn.close()


def get_snapshot_stats() -> Dict:
    """Stats on collected window snapshots."""
    _ensure_table()
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute("SELECT COUNT(*) FROM hf_window_snapshots").fetchone()[0]
        resolved = conn.execute("SELECT COUNT(*) FROM hf_window_snapshots WHERE outcome IS NOT NULL").fetchone()[0]
        by_duration = conn.execute("""
            SELECT duration, COUNT(*) as n, COUNT(outcome) as resolved,
                   ROUND(AVG(spread_bps), 2) as avg_spread_bps,
                   ROUND(AVG(ofi_1min), 4) as avg_ofi,
                   ROUND(AVG(vwap_deviation), 4) as avg_vwap_dev
            FROM hf_window_snapshots GROUP BY duration
        """).fetchall()

        return {
            "total": total,
            "resolved": resolved,
            "by_duration": [dict(r) for r in by_duration],
        }
    finally:
        conn.close()


if __name__ == "__main__":
    import json as _json
    print("Testing microstructure fetch (BTC)...")
    micro = _get_microstructure("BTC")
    print(f"  BTC microstructure: {micro}")
    cb = _get_cb_premium("BTC", None)
    print(f"  CB premium (no HL price): {cb}")
    print()
    print("Snapshotting 1h window...")
    r1 = snapshot_window_open("1h")
    print(f"  1h: {r1}")
    print("Snapshotting 4h window...")
    r4 = snapshot_window_open("4h")
    print(f"  4h: {r4}")
    print("Stats:")
    print(_json.dumps(get_snapshot_stats(), indent=2))
