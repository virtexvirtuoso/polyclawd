"""
HF Spread Scanner — cross-asset spread anomaly detector

Polls all 7 HF crypto assets (BTC/ETH/SOL/XRP/DOGE/BNB/HYPE) across 1h and 4h
markets and flags when:
  1. Intra-asset spread: abs(YES_price - 0.50) > threshold (market pricing non-50/50)
  2. Cross-asset divergence: assets with similar beta but divergent PM pricing
  3. Duration spread: same asset, 1h vs 4h pricing diverges beyond expected term-structure

When a spread anomaly is detected, sends a Telegram alert with trade suggestion.

Runs every 1h via scheduler (30min_gated every_n=2).
"""

import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "storage" / "shadow_trades.db"
GAMMA_API = "https://gamma-api.polymarket.com"

# Binance is primary (highest liquidity, major Chainlink source, accessible from VPS).
# HYPE is not listed on Binance — falls back to Bybit.
_BINANCE_SYMBOLS: Dict[str, str] = {
    "BTC":  "BTCUSDT",
    "ETH":  "ETHUSDT",
    "SOL":  "SOLUSDT",
    "XRP":  "XRPUSDT",
    "DOGE": "DOGEUSDT",
    "BNB":  "BNBUSDT",
}
_BYBIT_FALLBACK: Dict[str, str] = {
    "HYPE": "HYPEUSDT",   # not on Binance
}
# Binance interval strings; Bybit uses integer minutes
_BINANCE_INTERVAL: Dict[str, str] = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h"}
_BYBIT_INTERVAL:   Dict[str, str] = {"5m": "5",  "15m": "15",  "1h": "60", "4h": "240"}


def _fetch_candle_context(asset: str, duration: str) -> Optional[Dict]:
    """Fetch live kline for the current candle window.

    Primary: Binance (all assets except HYPE). Fallback: Bybit (HYPE).
    PM resolves against Chainlink Data Streams which aggregate from Binance,
    Coinbase, Kraken, etc. — Binance is the closest single-exchange proxy.
    HYPE is not on Binance; Bybit difference vs Chainlink is ~8 bps (negligible).

    Returns {open, current, high, low, pct_move, direction, source} or None.
    """
    def _parse(candle, fmt):
        if fmt == "binance":
            # [openTime, open, high, low, close, ...]
            return float(candle[1]), float(candle[2]), float(candle[3]), float(candle[4])
        else:
            # Bybit: [startTime, open, high, low, close, ...]
            return float(candle[1]), float(candle[2]), float(candle[3]), float(candle[4])

    # Try Binance first
    if asset in _BINANCE_SYMBOLS:
        sym = _BINANCE_SYMBOLS[asset]
        interval = _BINANCE_INTERVAL.get(duration, "15m")
        url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={interval}&limit=2"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                klines = json.loads(resp.read().decode())
            if klines:
                o, h, l, c = _parse(klines[-1], "binance")  # last = current candle
                pct = (c - o) / o * 100 if o else 0.0
                return {"open": o, "current": c, "high": h, "low": l,
                        "pct_move": pct, "direction": "UP" if pct >= 0 else "DOWN",
                        "source": "Binance"}
        except Exception:
            pass  # fall through to Bybit

    # Bybit fallback (HYPE or if Binance fails)
    if asset in _BYBIT_FALLBACK or asset in _BINANCE_SYMBOLS:
        sym = _BYBIT_FALLBACK.get(asset) or (_BINANCE_SYMBOLS.get(asset))
        if not sym:
            return None
        interval = _BYBIT_INTERVAL.get(duration, "15")
        url = (f"https://api.bybit.com/v5/market/kline"
               f"?category=spot&symbol={sym}&interval={interval}&limit=2")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
            candles = data.get("result", {}).get("list", [])
            if candles:
                o, h, l, c = _parse(candles[0], "bybit")  # newest first
                pct = (c - o) / o * 100 if o else 0.0
                return {"open": o, "current": c, "high": h, "low": l,
                        "pct_move": pct, "direction": "UP" if pct >= 0 else "DOWN",
                        "source": "Bybit"}
        except Exception:
            pass

    return None


def _fmt_pct(pct: float) -> str:
    """Format a percentage, avoiding the -0.00% display artifact."""
    if abs(pct) < 0.005:
        return "~0.00%"
    return f"{pct:+.2f}%"


# Minimum spot move (%) required to call DIVERGENCE.
# Flat candles (<0.10% on 15m, <0.15% on 1h) are noise, not real divergence.
_DIVERGENCE_MIN_MOVE = {
    "15m": 0.10,
    "1h":  0.15,
    "4h":  0.20,
}
_DIVERGENCE_MIN_MOVE_DEFAULT = 0.12


def _spot_bias(pm_direction: str, pm_price: float, spot: Dict, duration: str = "15m") -> Tuple[str, str]:
    """Derive a trading bias from PM price vs spot candle data.

    Returns (bias_line, bet_hint).
    """
    spot_dir = spot["direction"]
    pct = spot["pct_move"]
    agree = pm_direction == spot_dir
    pct_str = _fmt_pct(pct)

    if not agree:
        min_move = _DIVERGENCE_MIN_MOVE.get(duration, _DIVERGENCE_MIN_MOVE_DEFAULT)
        if abs(pct) < min_move:
            # Flat candle — direction is noise, not real divergence
            return (
                f"✅ Spot flat ({pct_str}) — PM {pm_price:.0%} near-neutral, no clear edge",
                "No clear edge — candle too flat to call divergence",
            )
        # Best case: PM is pricing the wrong direction — clearest edge
        fade_side = "NO" if pm_direction == "UP" else "YES"
        fade_price = (1 - pm_price) if pm_direction == "UP" else pm_price
        return (
            f"🔥 DIVERGENCE — spot {spot_dir} {pct_str} vs PM says {pm_direction}",
            f"Edge: {fade_side} @ {fade_price*100:.0f}¢  (spot contradicts PM)",
        )
    # Same direction — check if price is warranted by the move size.
    # For intramarket candle markets, typical crypto vol is 0.3–0.8% per 15m.
    # A PM price >72¢ warrants at least a 0.4% move; below that it's overconfident.
    if abs(pct) < 0.40 and pm_price > 0.72:
        no_price = 1 - pm_price
        return (
            f"⚠️ Small spot move ({pct_str}) but PM at {pm_price:.0%} — likely overpriced",
            f"Edge: NO @ {no_price*100:.0f}¢  (move too small to warrant {pm_price:.0%})",
        )
    if abs(pct) < 0.40 and pm_price < 0.28:
        return (
            f"⚠️ Small spot move ({pct_str}) but PM at {pm_price:.0%} — likely underpriced",
            f"Edge: YES @ {pm_price*100:.0f}¢  (move too small to warrant {pm_price:.0%})",
        )
    # Move and price broadly agree
    return (
        f"✅ Spot {spot_dir} {pct_str} — PM {pm_price:.0%} is fair",
        "No clear edge — price reflects the move",
    )


def _send_telegram(msg: str):
    try:
        import sys
        sys.path.insert(0, str(PROJECT_ROOT))
        from scripts.alert_formatter import send_telegram
        send_telegram(msg)
    except Exception as e:
        logger.debug(f"Telegram send error: {e}")

# ─── Per-timeframe config ────────────────────────────────────────────────────
# Shorter timeframes are noisier → require more extreme prices to be meaningful.
# Dedup cooldown = 4× the timeframe so the same signal can't re-fire each cycle.
#
# intramarket_threshold: min distance from 0.50 to flag a market as "having a view"
# term_spread_threshold: min |p_short - p_long| to flag divergence
# cross_asset_threshold: min spread within a beta group to flag divergence
# dedup_secs:           how long to suppress a repeated alert

TF_CONFIG: Dict[str, Dict] = {
    #           alert thresholds              dedup     window (secs)   min elapsed  min remaining  settled zone
    "5m":  {"intramarket": 0.35, "term_spread": 0.20, "cross_asset": 0.30,
            "dedup_secs": 20 * 60,  "duration_secs": 300,   "min_elapsed": 120, "min_remaining": 90,
            "settled_floor": 0.12,  "settled_ceil": 0.88},
    "15m": {"intramarket": 0.25, "term_spread": 0.18, "cross_asset": 0.25,
            "dedup_secs": 60 * 60,  "duration_secs": 900,   "min_elapsed": 300, "min_remaining": 120,
            "settled_floor": 0.10,  "settled_ceil": 0.90},
    "1h":  {"intramarket": 0.08, "term_spread": 0.12, "cross_asset": 0.18,
            "dedup_secs": 4 * 3600, "duration_secs": 3600,  "min_elapsed": 600, "min_remaining": 300,
            "settled_floor": 0.12,  "settled_ceil": 0.88},
    "4h":  {"intramarket": 0.08, "term_spread": 0.10, "cross_asset": 0.15,
            "dedup_secs": 16 * 3600,"duration_secs": 14400, "min_elapsed": 1800,"min_remaining": 600,
            "settled_floor": 0.15,  "settled_ceil": 0.85},
}
# settled_floor / settled_ceil: prices outside this band are "market concluded" —
# the crowd has converged on a result and there's nothing informative left to signal.
# Shorter timeframes can reach more extreme prices mid-window (legitimate signal),
# so their floors are lower. Longer timeframes settling near 0/1 are simply done.

# Legacy constants kept for any external callers
INTRAMARKET_THRESHOLD       = TF_CONFIG["1h"]["intramarket"]
TERM_SPREAD_THRESHOLD       = TF_CONFIG["1h"]["term_spread"]
CROSS_ASSET_DIVERGENCE_THRESHOLD = TF_CONFIG["1h"]["cross_asset"]

# Beta groups: assets that tend to move together
BETA_GROUPS = {
    "high_beta": ["ETH", "SOL", "DOGE"],
    "mid_beta":  ["XRP", "BNB"],
    "solo":      ["BTC", "HYPE"],
}

# ─── Market discovery ────────────────────────────────────────────────────────

def _fetch_json(url: str, timeout: int = 10) -> Optional[Dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd-SpreadScan/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"Fetch error {url}: {e}")
        return None


def _parse_end_date(end_date_str: str) -> Optional[float]:
    """Parse ISO end_date string → unix timestamp. Returns None on failure."""
    if not end_date_str:
        return None
    try:
        s = str(end_date_str)[:19].replace(" ", "T")
        if not s.endswith("Z"):
            s += "Z"
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _discover_markets(duration: str) -> Dict[str, Dict]:
    """
    Discover active crypto markets for a timeframe.
    Returns {asset: {market_id, yes_price, question, end_date, elapsed_secs, remaining_secs}}

    Filters applied:
      1. API-level: end_date_min=now, end_date_max=now+(2×duration) — eliminates stale/future markets
      2. Window-position: skip if elapsed < min_elapsed OR remaining < min_remaining — eliminates
         early-window noise (first-minute overextrapolation) and near-expired markets
      3. Tie-break: when multiple valid markets exist per asset, pick soonest end_date (active window)
    """
    cfg = TF_CONFIG.get(duration, TF_CONFIG["1h"])
    dur_secs      = cfg["duration_secs"]
    min_elapsed   = cfg["min_elapsed"]
    min_remaining = cfg["min_remaining"]

    now = time.time()
    now_iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    max_iso = datetime.fromtimestamp(now + 2 * dur_secs, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    url = (f"{GAMMA_API}/events?active=true&closed=false&limit=100"
           f"&tag_slug={duration}&end_date_min={now_iso}&end_date_max={max_iso}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd-SpreadScan/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            events = json.loads(resp.read().decode())
    except Exception as e:
        logger.error(f"Market discovery failed ({duration}): {e}")
        return {}

    ASSET_KEYWORDS = [
        ("BTC",  ["bitcoin", "btc"]),
        ("ETH",  ["ethereum", "eth"]),
        ("SOL",  ["solana", "sol"]),
        ("XRP",  ["xrp", "ripple"]),
        ("DOGE", ["dogecoin", "doge"]),
        ("BNB",  ["bnb", "binance coin"]),
        ("HYPE", ["hype", "hyperliquid"]),
    ]

    # Collect all qualifying candidates per asset
    candidates: Dict[str, list] = {}

    for event in events:
        for market in event.get("markets", []):
            question = market.get("question", "")
            q_lower  = question.lower()

            asset = None
            for a, keywords in ASSET_KEYWORDS:
                if any(kw in q_lower for kw in keywords):
                    asset = a
                    break
            if not asset:
                continue

            end_ts = _parse_end_date(market.get("endDate", ""))
            if end_ts is None:
                continue

            start_ts  = end_ts - dur_secs
            elapsed   = now - start_ts
            remaining = end_ts - now

            # Window-position filter: skip too-early and too-late
            if elapsed < min_elapsed or remaining < min_remaining:
                continue

            try:
                prices    = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                yes_price = float(prices[0]) if prices else 0.5
            except Exception:
                yes_price = 0.5

            candidates.setdefault(asset, []).append({
                "market_id":     market.get("conditionId") or market.get("id", ""),
                "yes_price":     yes_price,
                "question":      question,
                "end_date":      market.get("endDate", ""),
                "end_ts":        end_ts,
                "elapsed_secs":  int(elapsed),
                "remaining_secs":int(remaining),
            })

    # Tie-break: pick soonest end_date per asset (currently active window)
    result: Dict[str, Dict] = {}
    for asset, markets in candidates.items():
        result[asset] = min(markets, key=lambda m: m["end_ts"])

    return result


# ─── Spread analysis ─────────────────────────────────────────────────────────

_1H_CONFIRM_GATE = 0.70   # 1h price > this contradicts 5m DOWN; < (1-this) contradicts 5m UP


def _check_intramarket(markets_by_tf: Dict[str, Dict]) -> List[Dict]:
    """Flag markets where YES price is far from 0.50 per timeframe threshold.

    Settled guard: prices below settled_floor or above settled_ceil are excluded.
    These near-terminal prices represent a concluded market — the crowd has already
    seen the outcome. They look like strong signals (50¢ from center) but there's
    no information and no time to act.

    1h confirmation gate (5m only): suppress 5m intramarket alerts when the 1h
    market for the same asset strongly contradicts the direction. Prevents micro
    noise from firing against the hourly trend.
      5m DOWN + 1h > 0.70 → suppress
      5m UP   + 1h < 0.30 → suppress
    """
    alerts = []
    h1_markets = markets_by_tf.get("1h", {})
    for tf, markets in markets_by_tf.items():
        cfg           = TF_CONFIG.get(tf, TF_CONFIG["1h"])
        threshold     = cfg["intramarket"]
        settled_floor = cfg.get("settled_floor", 0.0)
        settled_ceil  = cfg.get("settled_ceil",  1.0)
        for asset, m in markets.items():
            p = m["yes_price"]
            # Settled guard: skip near-terminal prices — market has concluded
            if p <= settled_floor or p >= settled_ceil:
                logger.debug(f"  {asset} {tf}: settled at {p:.2f}, skipping")
                continue
            spread = abs(p - 0.50)
            if spread >= threshold:
                direction = "UP" if p > 0.50 else "DOWN"

                # 1h confirmation gate (5m only): suppress when 1h contradicts direction
                if tf == "5m" and asset in h1_markets:
                    p1h = h1_markets[asset]["yes_price"]
                    if direction == "DOWN" and p1h > _1H_CONFIRM_GATE:
                        logger.debug(
                            f"  {asset} 5m {direction} suppressed: 1h strongly UP at {p1h:.2f}"
                        )
                        continue
                    if direction == "UP" and p1h < (1.0 - _1H_CONFIRM_GATE):
                        logger.debug(
                            f"  {asset} 5m {direction} suppressed: 1h strongly DOWN at {p1h:.2f}"
                        )
                        continue

                elapsed   = m.get("elapsed_secs", 0)
                remaining = m.get("remaining_secs", 0)
                elapsed_str   = f"{elapsed // 60}m{elapsed % 60:02d}s"
                remaining_str = f"{remaining // 60}m{remaining % 60:02d}s"
                alerts.append({
                    "type": "intramarket",
                    "asset": asset,
                    "duration": tf,
                    "yes_price": p,
                    "direction": direction,
                    "spread_from_50": round(spread, 3),
                    "edge_pct": round(spread * 100, 1),
                    "elapsed_secs": elapsed,
                    "remaining_secs": remaining,
                    "elapsed_str": elapsed_str,
                    "remaining_str": remaining_str,
                    "market_id": m["market_id"],
                    "question": m["question"],
                    "note": f"PM pricing {direction} at {p:.2f} — {spread*100:.0f}¢ from 50",
                })
    return alerts


def _check_term_spread(markets_by_tf: Dict[str, Dict]) -> List[Dict]:
    """Flag divergence between adjacent timeframes for the same asset."""
    alerts = []
    tf_list = [tf for tf in ["5m", "15m", "1h", "4h"] if tf in markets_by_tf]
    # Compare adjacent pairs: (5m,15m), (15m,1h), (1h,4h)
    pairs = list(zip(tf_list, tf_list[1:]))
    for short_tf, long_tf in pairs:
        short_markets = markets_by_tf[short_tf]
        long_markets  = markets_by_tf[long_tf]
        threshold = TF_CONFIG.get(short_tf, TF_CONFIG["1h"])["term_spread"]
        short_cfg     = TF_CONFIG.get(short_tf, TF_CONFIG["1h"])
        settled_floor = short_cfg.get("settled_floor", 0.0)
        settled_ceil  = short_cfg.get("settled_ceil",  1.0)
        for asset in set(short_markets) & set(long_markets):
            p_short = short_markets[asset]["yes_price"]
            p_long  = long_markets[asset]["yes_price"]
            # Skip if either side is near-terminal — spread is meaningless when one market concluded
            if (p_short <= settled_floor or p_short >= settled_ceil or
                    p_long  <= settled_floor or p_long  >= settled_ceil):
                continue
            spread = abs(p_short - p_long)
            if spread >= threshold:
                if p_short > p_long:
                    note = f"{short_tf} bullish ({p_short:.2f}) vs {long_tf} neutral ({p_long:.2f})"
                else:
                    note = f"{long_tf} bullish ({p_long:.2f}) vs {short_tf} neutral ({p_short:.2f})"
                alerts.append({
                    "type": "term_spread",
                    "asset": asset,
                    "short_tf": short_tf,
                    "long_tf": long_tf,
                    "duration": short_tf,   # for dedup key
                    "p_short": p_short,
                    "p_long": p_long,
                    "spread": round(spread, 3),
                    "note": note,
                })
    return alerts


def _check_cross_asset(markets_by_tf: Dict[str, Dict]) -> List[Dict]:
    """Flag when assets in the same beta group have divergent PM pricing."""
    alerts = []
    for group_name, assets in BETA_GROUPS.items():
        if group_name == "solo":
            continue
        for tf, markets in markets_by_tf.items():
            cfg           = TF_CONFIG.get(tf, TF_CONFIG["1h"])
            threshold     = cfg["cross_asset"]
            settled_floor = cfg.get("settled_floor", 0.0)
            settled_ceil  = cfg.get("settled_ceil",  1.0)
            # Exclude near-terminal assets — their extreme prices skew the group spread
            prices = {
                a: markets[a]["yes_price"] for a in assets
                if a in markets and settled_floor < markets[a]["yes_price"] < settled_ceil
            }
            if len(prices) < 2:
                continue
            vals = list(prices.values())
            spread = max(vals) - min(vals)
            if spread >= threshold:
                max_asset = max(prices, key=prices.get)
                min_asset = min(prices, key=prices.get)
                alerts.append({
                    "type": "cross_asset",
                    "group": group_name,
                    "duration": tf,
                    "prices": prices,
                    "spread": round(spread, 3),
                    "bullish_asset": max_asset,
                    "bearish_asset": min_asset,
                    "note": (
                        f"{group_name}: {max_asset} tilted UP ({prices[max_asset]:.2f}) "
                        f"vs {min_asset} tilted DOWN ({prices[min_asset]:.2f}) — "
                        f"{spread*100:.0f}¢ spread within beta group"
                    ),
                })
    return alerts


# ─── Alert dedup ─────────────────────────────────────────────────────────────

_DEDUP_FILE = Path("/tmp/hf_spread_dedup.json")
_MAX_TTL = max(cfg["dedup_secs"] for cfg in TF_CONFIG.values())


def _load_cache() -> Dict[str, float]:
    try:
        with open(_DEDUP_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: Dict[str, float]) -> None:
    try:
        with open(_DEDUP_FILE, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass


def _dedup_key(alert: Dict) -> str:
    t = alert["type"]
    if t == "intramarket":
        return f"{t}:{alert['asset']}:{alert['duration']}:{alert['direction']}"
    elif t == "term_spread":
        return f"{t}:{alert['asset']}:{alert.get('short_tf','')}:{alert.get('long_tf','')}"
    else:
        return f"{t}:{alert['group']}:{alert['duration']}"


def _is_fresh(alert: Dict, cache: Dict[str, float]) -> bool:
    """Returns True if alert should fire (not seen within its timeframe's cooldown)."""
    key  = _dedup_key(alert)
    tf   = alert.get("duration", "1h")
    # term_spread uses the shorter of the two timeframes
    if alert.get("type") == "term_spread":
        tf = alert.get("short_tf", "1h")
    cooldown = TF_CONFIG.get(tf, TF_CONFIG["1h"])["dedup_secs"]
    now = time.time()
    if key in cache and now - cache[key] < cooldown:
        return False
    cache[key] = now
    return True


# ─── Telegram notify (via alert_formatter → polyclawd bot) ───────────────────


def _fmt_price(p: float) -> str:
    """Format a price cleanly — no scientific notation."""
    if p >= 1000:
        return f"{p:,.0f}"
    elif p >= 1:
        return f"{p:,.2f}"
    else:
        return f"{p:.5f}"


def _format_alert(alert: Dict) -> str:
    t = alert["type"]
    if t == "intramarket":
        remaining_secs = alert.get("remaining_secs", 9999)
        remaining_str  = alert.get("remaining_str", "")
        elapsed_str    = alert.get("elapsed_str", "")
        asset          = alert["asset"]
        duration       = alert["duration"]
        pm_dir         = alert["direction"]
        pm_price       = alert["yes_price"]

        # Live spot context — fetch every time alert is formatted
        spot = _fetch_candle_context(asset, duration)

        if spot:
            bias_line, bet_hint = _spot_bias(pm_dir, pm_price, spot, duration)
            is_fair = bias_line.startswith("✅")

            # Fair-price alerts: caller will suppress these — return None sentinel
            if is_fair:
                return ""   # signals caller to skip sending

            src = spot.get("source", "")
            fp_curr = _fmt_price(spot["current"])
            fp_open = _fmt_price(spot["open"])

            # Resolve threshold line
            # PM resolves UP if close >= open (Chainlink), DOWN otherwise
            direction_word = "≥" if pm_dir == "UP" else "<"
            threshold_note = (
                f"Resolves {pm_dir} if close {direction_word} {fp_open}  "
                f"(currently {fp_curr}, <b>{spot['pct_move']:+.2f}%</b>)"
            )

            # Urgency
            if remaining_secs < 120:
                urgency = "\n⏰ <b>< 2 min left — may be too late to act</b>"
            elif remaining_secs < 240:
                urgency = "\n⚡ Act fast — < 4 min remaining"
            else:
                urgency = ""

            # Signal-type header
            spot_dir = spot.get("direction", "")
            if bias_line.startswith("🔥"):
                header = f"🔥 <b>DIVERGENCE — {asset} {duration}</b>"
            elif bias_line.startswith("⚠️ Small") and pm_price > 0.72:
                header = f"⚠️ <b>OVERPRICED — {asset} {duration}</b>"
            else:
                header = f"📉 <b>UNDERPRICED — {asset} {duration}</b>"

            # Bet line stripped of redundant context (already in threshold line)
            bet_clean = bet_hint.split("(")[0].strip()

            # Plain-English "why" sentence
            if bias_line.startswith("🔥"):
                why = (
                    f"{asset} spot moved <b>{spot['pct_move']:+.2f}%</b> {spot_dir} "
                    f"but PM is pricing {pm_dir} at {pm_price:.0%}. "
                    f"Spot and PM disagree — that's the edge."
                )
            elif pm_price > 0.72:
                why = (
                    f"{asset} only moved <b>{spot['pct_move']:+.2f}%</b> this candle "
                    f"but PM is pricing {pm_price:.0%} {pm_dir}. "
                    f"Too confident for a move this small."
                )
            else:
                why = (
                    f"{asset} only moved <b>{spot['pct_move']:+.2f}%</b> this candle "
                    f"but PM is pricing {pm_dir} at only {pm_price:.0%}. "
                    f"Too cheap — the move justifies a higher probability."
                )

            return (
                f"{header}  <i>{elapsed_str} in · {remaining_str} left</i>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 <b>{bet_clean}</b>\n"
                f"{why}\n"
                f"\n"
                f"Spot [{src}] {spot['pct_move']:+.2f}%  ·  {threshold_note}"
                f"{urgency}"
            )
        else:
            # No spot data — fall back to old format
            emoji = "📈" if pm_dir == "UP" else "📉"
            timing = f"  <i>{elapsed_str} in · {remaining_str} left</i>" if elapsed_str else ""
            return (
                f"{emoji} <b>PM Spread — {asset} {duration}</b>{timing}\n"
                f"Direction: <b>{pm_dir}</b> @ {pm_price:.2f}\n"
                f"Edge vs 50/50: {alert['edge_pct']}¢\n"
                f"{alert['note']}"
            )
    elif t == "term_spread":
        short_tf = alert.get("short_tf", "short")
        long_tf  = alert.get("long_tf", "long")
        return (
            f"⏱ <b>Term Spread — {alert['asset']}</b>\n"
            f"{short_tf}: {alert['p_short']:.2f} | {long_tf}: {alert['p_long']:.2f}\n"
            f"Spread: {alert['spread']*100:.0f}¢\n"
            f"{alert['note']}"
        )
    else:
        prices_str = " | ".join(f"{a}: {p:.2f}" for a, p in sorted(alert["prices"].items()))
        return (
            f"🔀 <b>Cross-Asset Spread — {alert['group']} {alert['duration']}</b>\n"
            f"{prices_str}\n"
            f"Spread: {alert['spread']*100:.0f}¢\n"
            f"{alert['note']}"
        )


# ─── Persistence ─────────────────────────────────────────────────────────────

def _store_alerts(alerts: List[Dict]):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hf_spread_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type TEXT,
            asset TEXT,
            duration TEXT,
            spread REAL,
            payload TEXT,
            created_at TEXT
        )
    """)
    now = datetime.now(timezone.utc).isoformat()
    for a in alerts:
        conn.execute("""
            INSERT INTO hf_spread_alerts (alert_type, asset, duration, spread, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            a["type"],
            a.get("asset") or a.get("group", ""),
            a.get("duration", "multi"),
            a.get("spread_from_50") or a.get("spread", 0),
            json.dumps(a),
            now,
        ))
    conn.commit()
    conn.close()


# ─── Main entry ──────────────────────────────────────────────────────────────

def _detect_conflicts(actionable: List[tuple]) -> Dict[str, List[str]]:
    """
    Returns a dict mapping intramarket dedup_key -> list of conflict description strings.

    A conflict exists when an intramarket alert (bearish NO bet) on asset X at TF T
    is contradicted by:
      - A term_spread alert where asset X's short_tf == T AND p_short > p_long
        (PM is more bullish short-term — same direction we're fading)
      - A cross_asset alert where bullish_asset == X at the same TF
        (peer group says X is the high outlier — also bullish)
    """
    conflicts: Dict[str, List[str]] = {}

    # Index non-intramarket signals for fast lookup
    term_bullish: Dict[tuple, str] = {}   # (asset, short_tf) -> note
    cross_bullish: Dict[tuple, str] = {}  # (asset, tf)       -> note

    for a, _ in actionable:
        if a["type"] == "term_spread" and a.get("p_short", 0) > a.get("p_long", 0):
            term_bullish[(a["asset"], a["short_tf"])] = (
                f"Term spread: {a['asset']} {a['short_tf']} more bullish "
                f"({a['p_short']:.0%}) than {a['long_tf']} ({a['p_long']:.0%})"
            )
        if a["type"] == "cross_asset":
            cross_bullish[(a.get("bullish_asset", ""), a["duration"])] = (
                f"Cross-asset: {a.get('bullish_asset')} is the high outlier "
                f"in {a['group']} group ({a['duration']})"
            )

    # Check each intramarket NO bet against the indexes above
    for a, txt in actionable:
        if a["type"] != "intramarket":
            continue
        # Only flag when we're betting NO (fading an UP market or DIVERGENCE)
        pm_dir = a.get("direction", "")
        asset  = a["asset"]
        tf     = a["duration"]
        is_no_bet = (pm_dir == "UP")  # bet NO = fade UP
        if not is_no_bet:
            continue

        reasons = []
        if (asset, tf) in term_bullish:
            reasons.append(term_bullish[(asset, tf)])
        if (asset, tf) in cross_bullish:
            reasons.append(cross_bullish[(asset, tf)])

        if reasons:
            key = _dedup_key(a)
            conflicts[key] = reasons

    return conflicts


def scan_spreads(durations: Optional[List[str]] = None) -> List[Dict]:
    """
    Multi-timeframe spread scan. Returns list of fresh alerts (after dedup).

    Args:
        durations: list of timeframes to scan, e.g. ["5m","15m"] or ["1h","4h"].
                   Defaults to ["1h","4h"] to preserve original behaviour.

    Scheduler cadence:
        5m  markets  → called every 5min  (task_hf_spread_5m)
        15m markets  → called every 15min (task_hf_spread_15m)
        1h  markets  → called every 1h    (task_hf_spread_scan, existing)
        4h  markets  → called every 1h    (bundled with 1h scan)
    """
    if durations is None:
        durations = ["1h", "4h"]

    logger.info(f"HF spread scan [{','.join(durations)}]: discovering markets...")
    markets_by_tf: Dict[str, Dict] = {}
    for tf in durations:
        m = _discover_markets(tf)
        if m:
            markets_by_tf[tf] = m
            logger.debug(f"  {tf}: {len(m)} markets")

    if not markets_by_tf:
        return []

    all_alerts: List[Dict] = []
    all_alerts.extend(_check_intramarket(markets_by_tf))
    all_alerts.extend(_check_term_spread(markets_by_tf))
    all_alerts.extend(_check_cross_asset(markets_by_tf))

    cache = _load_cache()
    fresh = [a for a in all_alerts if _is_fresh(a, cache)]
    _save_cache({k: v for k, v in cache.items() if time.time() - v < _MAX_TTL})

    if fresh:
        _store_alerts(fresh)
        # Format each alert — fair-price intramarket alerts return "" and are suppressed
        formatted = [(a, _format_alert(a)) for a in fresh]
        actionable = [(a, txt) for a, txt in formatted if txt]
        if actionable:
            conflicts = _detect_conflicts(actionable)
            header = f"🔍 <b>HF Spread Scan</b> — {len(actionable)} actionable"
            lines = [header]
            for a, txt in actionable:
                lines.append("")
                key = _dedup_key(a)
                if key in conflicts:
                    reasons = "  |  ".join(conflicts[key])
                    lines.append(f"⚡ <i>Conflicting signals: {reasons}</i>")
                lines.append(txt)
            _send_telegram("\n".join(lines))
        suppressed = len(fresh) - len(actionable)
        logger.info(
            f"HF spread scan: {len(fresh)} fresh alerts, "
            f"{len(actionable)} actionable, {suppressed} fair-price suppressed"
        )
    else:
        logger.debug(f"HF spread scan: {len(all_alerts)} alerts, all deduped")

    return fresh


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    results = scan_spreads()
    print(f"\nAlerts: {len(results)}")
    for a in results:
        print(f"  [{a['type']}] {a.get('asset', a.get('group'))}: {a.get('note', '')}")
