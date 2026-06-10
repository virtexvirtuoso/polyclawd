#!/usr/bin/env python3
"""
Kalshi Weather Fade — PAPER strategy scanner.

Validated 2026-06-10 (vault: Weather-Edge-Analysis-Jun2026 §10, holdout-replicated):
Kalshi daily-temperature brackets are miscalibrated at the DAY-AHEAD EVENING
snapshot in the classic favorite-longshot pattern:
  - Longshots (YES mid < 0.15 eve-before) are overpriced -> BUY NO
    (+1.6-3.0% per $1 net of Kalshi taker fee)
  - Favorites (YES mid 0.50-0.70 eve-before) are underpriced -> BUY YES
    (~+12% net; second-confidence tier, half size)
The effect decays by morning — entries happen ONLY in the local 19:30-20:30
evening window per city.

Paper-only by construction: no auth, no order placement; writes simulated
fills directly to paper_positions (archetype='kalshi_weather_fade').
Fills are simulated at the EXECUTABLE ask of the side bought (never mid),
with the Kalshi taker fee 0.07*p*(1-p) capitalized into entry_price so the
generic resolver in paper_portfolio.resolve_open_positions() nets fees with
no changes to its P&L math:
  NO  buy at q=no_ask : cost=q+fee(q), stored entry_price = 1-cost
  YES buy at p=yes_ask: stored entry_price = p+fee(p)

Risk rule (non-negotiable): DATE_EXPOSURE_CAP sums open bet_size across ALL
cities for the same event calendar date — correlated extreme-weather days hit
many cities' longshots at once (the June 2026 47%-drawdown shape).

Every scan writes static/kalshi_fade_sheet.json (all candidates, entered and
skipped with reasons + book depth = the live depth census) and appends
data/kalshi_fade_scans.jsonl for audit.

CLI: python -m signals.kalshi_weather_fade --dry-run --force-window
"""

import argparse
import json
import logging
import math
import re
import time
import urllib.request
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHEET_PATH = PROJECT_ROOT / "static" / "kalshi_fade_sheet.json"
SCAN_LOG_PATH = PROJECT_ROOT / "data" / "kalshi_fade_scans.jsonl"

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "polyclawd-kalshi-fade/1.0 (paper scanner)"}

ARCHETYPE = "kalshi_weather_fade"
STRATEGY_NO = "kalshi_fade_longshot_no"
STRATEGY_YES = "kalshi_fade_favorite_yes"

# Canonical live series (one per city/kind, verified against settled-market
# census 2026-06-10 — see scratch/data/kalshi_tail_cal_cache.json).
SERIES = {
    # daily HIGH temperature
    "KXHIGHNY": ("nyc", "America/New_York"),
    "KXHIGHTDC": ("dc", "America/New_York"),
    "KXHIGHPHIL": ("phil", "America/New_York"),
    "KXHIGHMIA": ("mia", "America/New_York"),
    "KXHIGHCHI": ("chi", "America/Chicago"),
    "KXHIGHAUS": ("aus", "America/Chicago"),
    "KXHIGHTHOU": ("hou", "America/Chicago"),
    "KXHIGHTMIN": ("min", "America/Chicago"),
    "KXHIGHTOKC": ("okc", "America/Chicago"),
    "KXHIGHDEN": ("den", "America/Denver"),
    "KXHIGHTPHX": ("phx", "America/Phoenix"),
    "KXHIGHLAX": ("lax", "America/Los_Angeles"),
    "KXHIGHTSFO": ("sfo", "America/Los_Angeles"),
    # daily LOW temperature
    "KXLOWTNYC": ("nyc", "America/New_York"),
    "KXLOWTDC": ("dc", "America/New_York"),
    "KXLOWTPHIL": ("phil", "America/New_York"),
    "KXLOWTMIA": ("mia", "America/New_York"),
    "KXLOWTATL": ("atl", "America/New_York"),
    "KXLOWTBOS": ("bos", "America/New_York"),
    "KXLOWTCHI": ("chi", "America/Chicago"),
    "KXLOWTAUS": ("aus", "America/Chicago"),
    "KXLOWTHOU": ("hou", "America/Chicago"),
    "KXLOWTDAL": ("dal", "America/Chicago"),
    "KXLOWTSATX": ("satx", "America/Chicago"),
    "KXLOWTNOLA": ("nola", "America/Chicago"),
    "KXLOWTOKC": ("okc", "America/Chicago"),
    "KXLOWTDEN": ("den", "America/Denver"),
    "KXLOWTPHX": ("phx", "America/Phoenix"),
    "KXLOWTLAX": ("lax", "America/Los_Angeles"),
    "KXLOWTSFO": ("sfo", "America/Los_Angeles"),
    "KXLOWTSEA": ("sea", "America/Los_Angeles"),
    "KXLOWTLV": ("lv", "America/Los_Angeles"),
}

# Strategy parameters (validated values — do not loosen without re-validation)
LONGSHOT_MAX_YES = 0.15
FAVORITE_MIN, FAVORITE_MAX = 0.50, 0.70
MAX_SPREAD = 0.05  # on the traded side
MIN_DEPTH = 50  # contracts at the executable level
BET_NO = 100.0
BET_YES = 50.0  # second-confidence tier: half size
DATE_EXPOSURE_CAP = 400.0  # across ALL cities, per event calendar date
MAX_PER_CITY_DATE = 2
WINDOW_START = (19, 30)  # local
WINDOW_END = (20, 30)

_MON = {
    m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])
}
_TICKER_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-")


def kalshi_fee(p: float) -> float:
    """Kalshi taker fee per contract at price p."""
    return 0.07 * p * (1.0 - p)


def ticker_event_date(ticker: str):
    """KXHIGHNY-26JUN09-T85 -> date(2026, 6, 9). None if unparseable."""
    m = _TICKER_DATE_RE.search(ticker or "")
    if not m or m.group(2) not in _MON:
        return None
    try:
        return date(2000 + int(m.group(1)), _MON[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


def series_in_window(now_utc: datetime) -> list:
    """Series whose city local time is inside the evening entry window.

    Returns [(series, city, tz_name, target_event_date)] where the target is
    tomorrow in that city's local calendar.
    """
    out = []
    for series, (city, tz_name) in SERIES.items():
        local = now_utc.astimezone(ZoneInfo(tz_name))
        hm = (local.hour, local.minute)
        if WINDOW_START <= hm <= WINDOW_END:
            out.append((series, city, tz_name, local.date() + timedelta(days=1)))
    return out


def _jget(url: str, timeout: int = 20, retries: int = 3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.load(urllib.request.urlopen(req, timeout=timeout))
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(0.8 * (attempt + 1))
    return None


def fetch_open_markets(series: str) -> list:
    data = _jget(f"{KALSHI_API}/markets?series_ticker={series}&status=open&limit=200")
    return (data or {}).get("markets", [])


def fetch_orderbook(ticker: str) -> dict:
    """Kalshi v2 returns `orderbook_fp` with `yes_dollars`/`no_dollars` sides
    (string prices in dollars, string quantities); older payloads used
    `orderbook` with `yes`/`no` in cents. Handle both."""
    data = _jget(f"{KALSHI_API}/markets/{ticker}/orderbook")
    data = data or {}
    return data.get("orderbook_fp") or data.get("orderbook") or {}


def _to_dollars(v):
    """Defensive price normalization: cents (1-99) -> dollars (0.01-0.99)."""
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v > 1.0:
        v = v / 100.0
    return v if 0.0 <= v <= 1.0 else None


def quotes_from_market(m: dict) -> dict:
    """Extract yes/no bid/ask in dollars; *_dollars fields first, cents fallback."""
    out = {}
    for key in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
        v = m.get(f"{key}_dollars")
        if v is None:
            v = m.get(key)
        out[key] = _to_dollars(v)
    # derive missing no-side from yes-side (Kalshi identity)
    if out["no_ask"] is None and out["yes_bid"] is not None:
        out["no_ask"] = round(1.0 - out["yes_bid"], 4)
    if out["no_bid"] is None and out["yes_ask"] is not None:
        out["no_bid"] = round(1.0 - out["yes_ask"], 4)
    return out


def _best_bid_qty(levels) -> tuple:
    """(best_price_dollars, qty) from an orderbook side: list of [price, qty]."""
    best_p, best_q = None, 0
    for lvl in levels or []:
        try:
            p, q = _to_dollars(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if p is None:
            continue
        if best_p is None or p > best_p:
            best_p, best_q = p, q
    return best_p, best_q


def depth_for_buy(ob: dict, buy_side: str) -> float:
    """Contracts available at the executable level for a taker buy.

    Buying NO crosses the best YES bid (no_ask = 1 - yes_bid), so depth is the
    qty resting at the best yes-bid level — and vice versa.
    """
    side = "yes" if buy_side == "NO" else "no"
    levels = ob.get(side) or ob.get(f"{side}_dollars") or []
    _, qty = _best_bid_qty(levels)
    return qty


def _get_db():
    from signals.paper_portfolio import _get_db as _ppdb

    return _ppdb()


def _market_exists(conn, market_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM paper_positions WHERE market_id=? LIMIT 1", (market_id,)).fetchone()
    return row is not None


def _open_fade_rows(conn) -> list:
    return conn.execute(
        "SELECT market_id, bet_size, entry_forecast_json FROM paper_positions WHERE status='open' AND archetype=?",
        (ARCHETYPE,),
    ).fetchall()


def _date_exposure(open_rows, event_date: date) -> float:
    total = 0.0
    for r in open_rows:
        if ticker_event_date(r["market_id"]) == event_date:
            total += float(r["bet_size"] or 0)
    return total


def _count_city_date(open_rows, city: str, event_date: date) -> int:
    n = 0
    for r in open_rows:
        if ticker_event_date(r["market_id"]) != event_date:
            continue
        try:
            meta = json.loads(r["entry_forecast_json"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        if meta.get("city") == city:
            n += 1
    return n


def simulate_fill(side: str, exec_price: float, budget: float) -> dict:
    """Fee-capitalized paper fill. Returns {} if not fillable."""
    fee = kalshi_fee(exec_price)
    cost = exec_price + fee
    if not (0.01 <= cost <= 0.99):
        return {}
    contracts = math.floor(budget / cost)
    if contracts < 1:
        return {}
    # full precision: entry_price/bet_size feed the resolver's P&L formulas,
    # which amplify entry rounding by 1/(1-entry)^2 — round only for display
    bet_size = contracts * cost
    entry_price = (1.0 - cost) if side == "NO" else cost
    if side == "NO":
        potential_payout = round(bet_size * (1.0 / (1.0 - entry_price) - 1.0), 2)
    else:
        potential_payout = round(bet_size * (1.0 / entry_price - 1.0), 2)
    return {
        "contracts": contracts,
        "fee_per_contract": round(fee, 5),
        "cost_per_contract": round(cost, 5),
        "bet_size": bet_size,
        "entry_price": entry_price,
        "potential_payout": potential_payout,
    }


def evaluate_market(m: dict, city: str, series: str, event_date: date) -> dict:
    """Classify + gate one market. Returns a sheet candidate dict."""
    ticker = m.get("ticker", "")
    title = (m.get("title") or "").replace("**", "")
    q = quotes_from_market(m)
    cand = {
        "ticker": ticker,
        "series": series,
        "city": city,
        "event_date": str(event_date),
        "title": title,
        "yes_bid": q["yes_bid"],
        "yes_ask": q["yes_ask"],
        "no_bid": q["no_bid"],
        "no_ask": q["no_ask"],
        "action": "skipped",
        "skip_reason": None,
        "tier": None,
    }
    if q["yes_bid"] is None or q["yes_ask"] is None:
        cand["skip_reason"] = "no_quotes"
        return cand
    mid = (q["yes_bid"] + q["yes_ask"]) / 2.0
    cand["mid"] = round(mid, 4)

    if mid < LONGSHOT_MAX_YES and mid > 0:
        side, tier, budget = "NO", STRATEGY_NO, BET_NO
        exec_price, spread = q["no_ask"], (q["no_ask"] - q["no_bid"]) if q["no_bid"] is not None else None
    elif FAVORITE_MIN <= mid <= FAVORITE_MAX:
        side, tier, budget = "YES", STRATEGY_YES, BET_YES
        exec_price, spread = q["yes_ask"], q["yes_ask"] - q["yes_bid"]
    else:
        cand["skip_reason"] = "outside_tiers"
        return cand

    cand["tier"] = tier
    cand["side"] = side
    cand["spread"] = round(spread, 4) if spread is not None else None
    if exec_price is None:
        cand["skip_reason"] = "no_executable_ask"
        return cand
    cand["exec_price"] = round(exec_price, 4)
    if spread is None or spread > MAX_SPREAD:
        cand["skip_reason"] = "spread_too_wide"
        return cand

    ob = fetch_orderbook(ticker)
    time.sleep(0.1)
    depth = depth_for_buy(ob, side)
    cand["depth"] = depth
    if depth < MIN_DEPTH:
        cand["skip_reason"] = "depth_too_thin"
        return cand

    fill = simulate_fill(side, exec_price, budget)
    if not fill:
        cand["skip_reason"] = "not_fillable"
        return cand
    cand.update(fill)
    cand["action"] = "candidate"  # caps checked by caller against live exposure
    return cand


def _insert_position(conn, cand: dict, local_snapshot_time: str):
    edge_est = 0.025 if cand["tier"] == STRATEGY_NO else 0.12
    confidence = 0.70 if cand["tier"] == STRATEGY_NO else 0.50
    meta = {
        "type": ARCHETYPE,
        "tier": cand["tier"],
        "series": cand["series"],
        "city": cand["city"],
        "event_date": cand["event_date"],
        "yes_bid": cand["yes_bid"],
        "yes_ask": cand["yes_ask"],
        "no_bid": cand["no_bid"],
        "no_ask": cand["no_ask"],
        "mid": cand["mid"],
        "spread": cand["spread"],
        "depth": cand["depth"],
        "exec_price": cand["exec_price"],
        "contracts": cand["contracts"],
        "fee_per_contract": cand["fee_per_contract"],
        "cost_per_contract": cand["cost_per_contract"],
        "local_snapshot_time": local_snapshot_time,
        "paper": True,
    }
    conn.execute(
        """INSERT INTO paper_positions
        (opened_at, market_id, market_title, platform, side, entry_price,
         bet_size, potential_payout, confidence, edge_pct, status, archetype,
         strategy, market_slug, entry_forecast_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            cand["ticker"],
            cand["title"],
            "kalshi",
            cand["side"],
            cand["entry_price"],
            cand["bet_size"],
            cand["potential_payout"],
            confidence,
            edge_est,
            ARCHETYPE,
            cand["tier"],
            "",
            json.dumps(meta),
        ),
    )
    conn.commit()
    try:
        from signals.discord_alerts import alert_position_opened

        alert_position_opened(
            cand["title"],
            cand["side"],
            cand["entry_price"],
            cand["bet_size"],
            cand["tier"],
            edge_est * 100,
            market_url=f"https://kalshi.com/markets/{cand['series']}",
            confidence=confidence,
            archetype=ARCHETYPE,
            potential_payout=cand["potential_payout"],
        )
    except Exception:
        pass


def _write_sheet(payload: dict):
    SHEET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHEET_PATH.write_text(json.dumps(payload, indent=1, default=str))


def _append_scan_log(payload: dict):
    try:
        SCAN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SCAN_LOG_PATH, "a") as f:
            f.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        logger.exception("kalshi-fade: scan log append failed")


def run_evening_scan(now: datetime = None, dry_run: bool = False, force_window: bool = False) -> dict:
    """Scheduler + CLI entrypoint. Safe to call any time — no-ops outside window."""
    now = now or datetime.now(timezone.utc)
    in_window = series_in_window(now)
    if force_window and not in_window:
        in_window = [(s, c, tz, datetime.now(ZoneInfo(tz)).date() + timedelta(days=1)) for s, (c, tz) in SERIES.items()]

    candidates, entered = [], 0
    config = {
        "longshot_max_yes": LONGSHOT_MAX_YES,
        "favorite_range": [FAVORITE_MIN, FAVORITE_MAX],
        "max_spread": MAX_SPREAD,
        "min_depth": MIN_DEPTH,
        "bet_no": BET_NO,
        "bet_yes": BET_YES,
        "date_exposure_cap": DATE_EXPOSURE_CAP,
        "max_per_city_date": MAX_PER_CITY_DATE,
        "paper": True,
        "dry_run": dry_run,
    }

    # knob 1 (2026-06-10 /optimize): ranked fill — spend the date cap on the
    # best candidates first (QA-measured: executable EV in the 0.05-0.15 mid
    # bin with tight spread is ~2x the sub-0.05 bin). Default OFF so the
    # baseline shadow stays clean; flip via engine_state kalshi_fade_ranked_fill.
    ranked = False
    try:
        from api.routes.engine import load_engine_state
        ranked = bool(load_engine_state().get("kalshi_fade_ranked_fill", False))
    except Exception:
        pass
    config["ranked_fill"] = ranked

    if in_window:
        conn = _get_db()
        try:
            open_rows = _open_fade_rows(conn)
            # Phase 1: evaluate everything in-window (no inserts yet)
            pending = []  # (cand, city, target_date, local_ts)
            for series, city, tz_name, target_date in in_window:
                local_ts = now.astimezone(ZoneInfo(tz_name)).isoformat()
                for m in fetch_open_markets(series):
                    if ticker_event_date(m.get("ticker", "")) != target_date:
                        continue
                    cand = evaluate_market(m, city, series, target_date)
                    candidates.append(cand)
                    if cand["action"] == "candidate":
                        pending.append((cand, city, target_date, local_ts))
                time.sleep(0.1)

            # Phase 2: caps + inserts. Flag off = scan order (baseline-identical).
            if ranked:
                def _rank(item):
                    c = item[0]
                    preferred = 0 if (c["tier"] == STRATEGY_NO and 0.05 <= (c.get("mid") or 0) < 0.15) else 1
                    spread = c.get("spread") if c.get("spread") is not None else 9
                    return (preferred, spread)
                pending.sort(key=_rank)
            for cand, city, target_date, local_ts in pending:
                if _market_exists(conn, cand["ticker"]):
                    cand["action"], cand["skip_reason"] = "skipped", "already_traded"
                elif _count_city_date(open_rows, city, target_date) >= MAX_PER_CITY_DATE:
                    cand["action"], cand["skip_reason"] = "skipped", "city_date_cap"
                elif _date_exposure(open_rows, target_date) + cand["bet_size"] > DATE_EXPOSURE_CAP:
                    cand["action"], cand["skip_reason"] = "skipped", "date_exposure_cap"
                elif dry_run:
                    cand["action"], cand["skip_reason"] = "dry_run", None
                else:
                    _insert_position(conn, cand, local_ts)
                    open_rows = _open_fade_rows(conn)  # refresh caps
                    cand["action"] = "entered"
                    entered += 1
        finally:
            conn.close()

    by_date = {}
    for c in candidates:
        if c["action"] == "entered":
            by_date[c["event_date"]] = round(by_date.get(c["event_date"], 0) + c["bet_size"], 2)
    summary = {
        "generated_at": now.isoformat(),
        "window_series": [s for s, *_ in in_window],
        "scanned": len(candidates),
        "entered": entered,
        "skipped": sum(1 for c in candidates if c["action"] == "skipped"),
        "exposure_entered_this_scan": by_date,
        "config": config,
        "candidates": candidates,
    }
    _write_sheet(summary)
    if candidates or in_window:
        _append_scan_log({k: v for k, v in summary.items() if k != "config"})
    if entered:
        logger.info("kalshi-fade: entered %d paper positions (%d scanned)", entered, len(candidates))
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="Kalshi weather fade paper scanner")
    ap.add_argument("--dry-run", action="store_true", help="evaluate but do not insert")
    ap.add_argument("--force-window", action="store_true", help="scan all series regardless of local time window")
    args = ap.parse_args()
    result = run_evening_scan(dry_run=args.dry_run, force_window=args.force_window)
    printable = {k: v for k, v in result.items() if k != "candidates"}
    print(json.dumps(printable, indent=2, default=str))
    for c in result["candidates"]:
        print(
            f"{c['action']:<9} {c.get('skip_reason') or '':<20} "
            f"{c['ticker']:<26} mid={c.get('mid')} side={c.get('side', '')} "
            f"spread={c.get('spread')} depth={c.get('depth')} "
            f"bet={c.get('bet_size', '')}"
        )
