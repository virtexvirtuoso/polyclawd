"""Single-name options-implied probability signal (NVDA/META/MSFT/AAPL/AMZN).
Alpaca OPRA IV -> N(d2) implied prob vs Polymarket weekly-close ladders.
VPS cron, post-close ET. Display-only (not wired to the trade engine).
Scope: vault 02-Projects/Polyclawd/Research/2026-05-29-Scope-Single-Name-Options-Implied-Signal.md
"""

from __future__ import annotations
import math, sqlite3, pathlib, json, re, os, statistics
from datetime import date, datetime, timezone

import requests
from loguru import logger

RISK_FREE = 0.045  # short T-bill proxy


def implied_prob_above(S, K, T_years, sigma, r=RISK_FREE):
    """Risk-neutral P(S_T > K) = N(d2) via Black-Scholes. None on degenerate input."""
    if not (S and S > 0) or not (K and K > 0) or T_years is None or T_years <= 0 or not sigma or sigma <= 0:
        return None
    d2 = (math.log(S / K) + (r - 0.5 * sigma * sigma) * T_years) / (sigma * math.sqrt(T_years))
    return 0.5 * math.erfc(-d2 / math.sqrt(2.0))  # Phi(d2)


def prob_in_bracket(S, lo, hi, T_years, sigma, r=RISK_FREE):
    """P(lo <= S_T < hi) = N(d2@lo) - N(d2@hi). hi=None => open-ended top bracket."""
    p_lo = implied_prob_above(S, lo, T_years, sigma, r)
    if p_lo is None:
        return None
    p_hi = implied_prob_above(S, hi, T_years, sigma, r) if hi else 0.0
    return max(0.0, p_lo - (p_hi or 0.0))


SCHEMA = """
CREATE TABLE IF NOT EXISTS options_implied (
  date TEXT NOT NULL, options_as_of TEXT, poly_market_id TEXT NOT NULL,
  ticker TEXT, expiry TEXT, strike REAL NOT NULL,
  bracket_lo REAL, bracket_hi REAL, market_type TEXT,
  poly_price REAL, implied_prob REAL, spread_pp REAL,
  underlying REAL, iv REAL, poly_liquidity REAL, poly_vol_24h REAL,
  iv_rv_ratio REAL,
  executable_price REAL, executable_edge_pp REAL, book_spread_pp REAL,
  slippage_bps REAL, tradeable INTEGER,
  PRIMARY KEY (date, poly_market_id, strike)
);
"""


def init_db(db_path):
    pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    # Migration: add columns that may not exist in older tables
    for col in ["iv_rv_ratio", "executable_price", "executable_edge_pp",
                "book_spread_pp", "slippage_bps"]:
        try:
            con.execute(f"ALTER TABLE options_implied ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass
    try:
        con.execute("ALTER TABLE options_implied ADD COLUMN tradeable INTEGER")
    except sqlite3.OperationalError:
        pass
    con.commit()
    con.close()


_FIELDS = [
    "date",
    "options_as_of",
    "poly_market_id",
    "ticker",
    "expiry",
    "strike",
    "bracket_lo",
    "bracket_hi",
    "market_type",
    "poly_price",
    "implied_prob",
    "spread_pp",
    "underlying",
    "iv",
    "poly_liquidity",
    "poly_vol_24h",
    "iv_rv_ratio",
    "executable_price",
    "executable_edge_pp",
    "book_spread_pp",
    "slippage_bps",
    "tradeable",
]


def upsert_rows(db_path, rows):
    """Insert rows, skipping existing (date,poly_market_id,strike). Returns # written."""
    init_db(db_path)  # ensure schema + ALTER-TABLE column migrations applied (existing tables too)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    written = 0
    for r in rows:
        cur = con.execute(
            f"INSERT OR IGNORE INTO options_implied ({','.join(_FIELDS)}) VALUES ({','.join('?' for _ in _FIELDS)})",
            [r.get(k) for k in _FIELDS],
        )
        written += cur.rowcount
    con.commit()
    con.close()
    return written


GAMMA = "https://gamma-api.polymarket.com"
try:  # shared order-book executable-edge enrichment
    from odds import poly_executable_edge as pee
except Exception:  # pragma: no cover
    pee = None
UA = {"User-Agent": "Mozilla/5.0 polyclawd-options"}
NAMES = ["NVDA", "META", "MSFT", "AAPL", "AMZN"]

# ── Auto-Discovery Cache ────────────────────────────────────────────
# Discovered tickers cache (refreshed once per day, not every scan)
_DISCOVERED_TICKERS = {"date": "", "tickers": []}
_HP = {"APCA-API-KEY-ID": "", "APCA-API-SECRET-KEY": ""}  # populated lazily


def discover_active_tickers(force_refresh: bool = False) -> List[str]:
    """Auto-discover stock tickers that have both active Polymarket close
    markets AND Alpaca options chains.

    Queries Polymarket public-search once per day for events matching
    "close above", extracts unique tickers, then tests each against
    Alpaca's options API. Caches results for the day.

    Returns list of ticker strings, e.g. ["NVDA", "TSLA", ...].
    """
    global _DISCOVERED_TICKERS
    today = date.today().isoformat()

    # Use cache if already fetched today and not forced
    if _DISCOVERED_TICKERS["date"] == today and not force_refresh:
        return _DISCOVERED_TICKERS["tickers"]

    # 1. Search Polymarket for close-above events
    found = set()
    try:
        # Search for common stock ticker patterns
        search_url = f"{GAMMA}/public-search"
        params = {"q": "close above", "limit_per_type": 50, "events_status": "active"}
        r = requests.get(search_url, params=params, headers=UA, timeout=30)
        r.raise_for_status()
        data = r.json()
        events = data.get("events", []) if isinstance(data, dict) else data

        for ev in events:
            title = ev.get("title", "")
            slug = ev.get("slug", "")
            # Extract ticker from "(TICKER)" pattern
            m = re.search(r'\(([A-Z]{2,5})\)', title)
            if m:
                tk = m.group(1)
                # Skip non-stock tickers (commodities, indices with different option chains)
                if tk in ("WTI", "BTC", "ETH", "SOL"):
                    continue
                # Check it's actually a close market (not futures, not exotic)
                if "close" in slug.lower() or "close" in title.lower():
                    found.add(tk)
    except Exception as e:
        logger.warning(f"Ticker discovery failed: {e}")
        # Fall back to hardcoded NAMES
        _DISCOVERED_TICKERS = {"date": today, "tickers": list(NAMES)}
        return list(NAMES)

    if not found:
        _DISCOVERED_TICKERS = {"date": today, "tickers": list(NAMES)}
        return list(NAMES)

    # 2. Validate against Alpaca options chain (does this ticker have options?)
    validated = []
    hp = _alpaca_headers()
    for tk in sorted(found):
        try:
            url = f"https://data.alpaca.markets/v1beta1/options/snapshots/{tk}"
            params = {"expiration_date": "2026-06-12", "limit": 1}
            r2 = requests.get(url, params=params, headers=hp, timeout=8)
            if r2.status_code == 200:
                d2 = r2.json()
                snaps = d2.get("snapshots", {})
                if isinstance(snaps, dict) and len(snaps) > 0:
                    validated.append(tk)
        except Exception:
            continue

    if not validated:
        validated = list(NAMES)

    logger.info(f"Auto-discovered {len(validated)} tickers: {validated}")
    _DISCOVERED_TICKERS = {"date": today, "tickers": validated}
    return validated


_RANGE_RE = re.compile(r"\$?(\d[\d,]*)(?:\s*(?:and|-|to)\s*\$?(\d[\d,]*))?", re.I)


def _money(s):
    return float(s.replace(",", "")) if s else None


def parse_poly_event(event, ticker):
    """Normalize one Gamma event -> {ticker, resolution_date, markets:[...]}.
    resolution_date = endDate calendar date (the Friday weekly close)."""
    res_date = str(event.get("endDate", ""))[:10]
    out = []
    for m in event.get("markets", []):
        q = m.get("question", "")
        prices = m.get("outcomePrices")
        prices = json.loads(prices) if isinstance(prices, str) else (prices or [])
        yes = float(prices[0]) if prices else None
        ql = q.lower()
        nums = _RANGE_RE.findall(q)
        # Standard patterns: "above $X", "between $X and $Y", "below $X"
        if "between" in ql and nums and nums[0][1]:
            lo, hi, mtype = _money(nums[0][0]), _money(nums[0][1]), "bracket"
        elif ("above" in ql or "higher" in ql) and nums:
            lo, hi, mtype = _money(nums[0][0]), None, "above"
        elif ("below" in ql or "lower" in ql) and nums:
            lo, hi, mtype = _money(nums[0][0]), None, "below"
        # Weekly patterns: "at $200-$205", "at <$190", "at >$235"
        elif nums and nums[0][1]:
            # "at $200-$205" → range, treated as bracket
            lo, hi, mtype = _money(nums[0][0]), _money(nums[0][1]), "bracket"
        elif nums and ">" in q or "above" in q:
            lo, hi, mtype = _money(nums[0][0]), None, "above"
        elif nums and "<" in q or "below" in q:
            lo, hi, mtype = _money(nums[0][0]), None, "below"
        else:
            continue
        out.append(
            {
                "conditionId": m.get("conditionId"),
                "question": q,
                "market_type": mtype,
                "bracket_lo": lo,
                "bracket_hi": hi,
                "poly_price": yes,
                "poly_liquidity": float(m.get("liquidityNum") or 0),
                "poly_vol_24h": float(m.get("volume24hr") or 0),
            }
        )
    return {"ticker": ticker, "resolution_date": res_date, "markets": out}


def fetch_poly_close_events(ticker):
    r = requests.get(
        f"{GAMMA}/public-search",
        params={"q": f"{ticker} close", "limit_per_type": 20, "events_status": "active"},
        headers=UA,
        timeout=25,
    )
    r.raise_for_status()
    j = r.json()
    evs = j.get("events", []) if isinstance(j, dict) else []
    return [
        e
        for e in evs
        if ticker.lower() in e.get("slug", "").lower()
        and ("week" in e.get("slug", "").lower() or "close" in e.get("slug", "").lower())
    ]


_OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def _occ(sym):
    m = _OCC.match(sym)
    if not m:
        return None
    t, ymd, cp, k8 = m.groups()
    return t, f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:6]}", cp, int(k8) / 1000.0


def pick_iv(snaps, expiry, strike, right="C"):
    """IV at the strike nearest `strike` for expiry/right, skipping IV<=0 (0DTE)."""
    best, best_d = None, 1e18
    for sym, snap in snaps.items():
        p = _occ(sym)
        if not p:
            continue
        _, exp, cp, k = p
        if exp != expiry or cp != right:
            continue
        iv = snap.get("impliedVolatility")
        if not iv or iv <= 0:
            continue
        d = abs(k - strike)
        if d < best_d:
            best, best_d = iv, d
    return best


def _alpaca_headers():
    key = os.environ.get("ALPACA_API_KEY")
    sec = os.environ.get("ALPACA_API_SECRET")
    if not key:  # local fallback: macOS keychain
        import subprocess

        key = subprocess.run(
            ["bash", "-lc", "~/bin/secret get alpaca-key"], capture_output=True, text=True
        ).stdout.strip()
        sec = subprocess.run(
            ["bash", "-lc", "~/bin/secret get alpaca-secret"], capture_output=True, text=True
        ).stdout.strip()
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}


def fetch_alpaca_snapshot(ticker, expiry):
    H = _alpaca_headers()
    url = f"https://data.alpaca.markets/v1beta1/options/snapshots/{ticker}"
    snaps, tok = {}, None
    while True:
        params = {"feed": "opra", "limit": 1000, "expiration_date": expiry}
        if tok:
            params["page_token"] = tok
        r = requests.get(url, params=params, headers=H, timeout=30)
        r.raise_for_status()
        d = r.json()
        snaps.update(d.get("snapshots", {}))
        tok = d.get("next_page_token")
        if not tok:
            break
    return snaps


def underlying_price(ticker):
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{ticker}/trades/latest",
        params={"feed": "sip"},
        headers=_alpaca_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return float(r.json()["trade"]["p"])


MIN_OBS = 30
MIN_LIQ = 500.0  # liquidity gate (scope §5): skip thin Polymarket books
ALERT_PP = 15.0  # raw fallback threshold (premium-contaminated until z-score active)
Z_THRESH = 2.0
DEFAULT_DB = pathlib.Path(
    os.environ.get("OPTIONS_DB", str(pathlib.Path.home() / "polyclawd-data" / "options_implied.db"))
)


def trailing_z(db_path, market_id, strike, before):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT spread_pp FROM options_implied WHERE poly_market_id=? AND strike=? "
        "AND date < ? AND spread_pp IS NOT NULL",
        (market_id, strike, before),
    ).fetchall()
    con.close()
    vals = [r["spread_pp"] for r in rows]
    if len(vals) < MIN_OBS:
        return len(vals), None, None
    return len(vals), statistics.mean(vals), (statistics.pstdev(vals) or None)


def _years_to(expiry_date, asof):
    exp = datetime.fromisoformat(expiry_date).date()
    base = asof.date() if isinstance(asof, datetime) else asof
    return max((exp - base).days, 0) / 365.0


def run(db_path=DEFAULT_DB):
    """One pass: poly ladders + alpaca chain -> implied prob, spread, write. Idempotent."""
    init_db(db_path)
    today = date.today().isoformat()
    now = datetime.now(timezone.utc)
    rows = []

    # Auto-discover tickers with active Polymarket + Alpaca coverage
    active_tickers = discover_active_tickers()
    logger.info(f"Options scan: {len(active_tickers)} active tickers: {active_tickers}")

    for tk in active_tickers:
        try:
            S = underlying_price(tk)
            for ev in fetch_poly_close_events(tk):
                pev = parse_poly_event(ev, tk)
                exp = pev["resolution_date"]
                if not exp or exp <= today:  # skip 0DTE/expired (IV=0)
                    continue
                snaps = fetch_alpaca_snapshot(tk, exp)
                T = _years_to(exp, now)
                # Dedup: same (strike, market_type) can appear from different condition IDs
                seen_keys = set()
                for m in pev["markets"]:
                    if (m["poly_liquidity"] or 0) < MIN_LIQ:  # liquidity gate
                        continue
                    K = m["bracket_lo"]
                    market_type = m["market_type"]
                    
                    # Skip strikes too far from money — N(d2) becomes 0% or 100%, no edge
                    if S and S > 0:
                        if market_type == "above" and K < S * 0.8:
                            continue  # too deep ITM, implied prob ~100%
                        if market_type == "below" and K > S * 1.2:
                            continue  # too deep ITM, implied prob ~0%
                    
                    # Dedup by (strike, market_type)
                    dedup_key = (K, market_type)
                    if dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)
                    
                    iv = pick_iv(snaps, exp, K, right="C")
                    if iv is not None:
                        from signals.vol_spread import get_iv_rv_ratio
                        iv_rv_ratio_val = get_iv_rv_ratio(tk, iv)
                    else:
                        iv_rv_ratio_val = None
                    if iv is None or m["poly_price"] is None:
                        continue
                    if m["market_type"] == "bracket":
                        ip = prob_in_bracket(S, m["bracket_lo"], m["bracket_hi"], T, iv)
                    elif m["market_type"] == "below":
                        pa = implied_prob_above(S, K, T, iv)
                        ip = (1 - pa) if pa is not None else None
                    else:
                        ip = implied_prob_above(S, K, T, iv)
                    if ip is None:
                        continue
                    spread = (m["poly_price"] - ip) * 100.0
                    # Executable-edge enrichment (order-book reality check).
                    # Side is direction-aware: fade overpriced YES = buy NO.
                    # Gated to >=3pp edges to bound CLOB calls in this batch logger.
                    ex_price = ex_edge_pp = ex_spread_pp = ex_slip = None
                    ex_tradeable = 0
                    if pee is not None and abs(spread) >= 3.0 and m.get("conditionId"):
                        if ip >= m["poly_price"]:
                            _side, _oi, _tp = "YES", 0, ip
                        else:
                            _side, _oi, _tp = "NO", 1, 1.0 - ip
                        try:
                            _ex = pee.executable_edge(_tp, _side, condition_id=m["conditionId"],
                                                      outcome_index=_oi, target_usd=100.0)
                        except Exception:
                            _ex = {"available": False}
                        if _ex.get("available"):
                            ex_price = _ex["executable_price"]
                            ex_edge_pp = (round(_ex["executable_edge"] * 100, 2)
                                          if _ex["executable_edge"] is not None else None)
                            ex_spread_pp = (round(_ex["spread"] * 100, 2)
                                            if _ex["spread"] is not None else None)
                            ex_slip = _ex["slippage_bps"]
                            ex_tradeable = 1 if _ex["tradeable"] else 0
                    rows.append(
                        {
                            "date": today,
                            "options_as_of": today,
                            "poly_market_id": m["conditionId"],
                            "ticker": tk,
                            "expiry": exp,
                            "strike": K,
                            "bracket_lo": m["bracket_lo"],
                            "bracket_hi": m["bracket_hi"],
                            "market_type": m["market_type"],
                            "poly_price": m["poly_price"],
                            "implied_prob": round(ip, 4),
                            "spread_pp": round(spread, 2),
                            "underlying": S,
                            "iv": iv,
                            "poly_liquidity": m["poly_liquidity"],
                            "poly_vol_24h": m["poly_vol_24h"],
                            "iv_rv_ratio": iv_rv_ratio_val,
                            "executable_price": ex_price,
                            "executable_edge_pp": ex_edge_pp,
                            "book_spread_pp": ex_spread_pp,
                            "slippage_bps": ex_slip,
                            "tradeable": ex_tradeable,
                        }
                    )
        except Exception as e:
            print(f"[options_implied] {tk} failed: {type(e).__name__}: {e}")
    written = upsert_rows(db_path, rows)
    print(f"[options_implied] {today}: {len(rows)} computed, {written} written")
    return written


# ── Trade-signal layer (paper-trades via paper_portfolio, like weather) ──────
# Trades the cross-sectionally-detrended, z-scored DEVIATION of each contract's
# options-implied spread (two-stage detrend, per quant spec):
#   1. same-day cross-sectional: residual = spread - mean(spread over that day's
#      same (ticker, market_type) strikes). Removes the per-name/per-day premium
#      and IV-regime shift instantly (works day 1, no accumulation wait).
#   2. trailing scale: z = (residual - mu) / max(sd, SD_FLOOR), where (mu, sd) are
#      the trailing distribution of residuals keyed on
#      (ticker, market_type, log-moneyness bucket) -- ROTATION-STABLE, so obs
#      accumulate across weekly conditionId rotation. (The original (market_id,
#      strike) key could NEVER reach the obs floor because weekly markets rotate
#      conditionId and resolve in days -- the QA bug this replaces.)
# Side: z>0 (richer than peers+norm) -> NO; z<0 -> YES. Gate |z| >= Z_THRESH.
OPTIONS_MIN_OBS = int(os.environ.get("OPTIONS_MIN_OBS", "20"))  # full-confidence obs floor
OPTIONS_MIN_OBS_LOWCONF = 10  # 10-19 trailing obs: emit but size down (low confidence)
OPTIONS_MIN_XS = 3            # min same-day strikes per (ticker,market_type) to detrend
MONEYNESS_W = 0.025           # log-moneyness bucket width
SD_FLOOR = 0.5                # pp; stops tiny-sample sd from manufacturing |z|>=2


def _moneyness_bucket(strike, underlying, width=MONEYNESS_W):
    """Integer log-moneyness bin: round(ln(strike/underlying)/width). None if invalid."""
    if not strike or not underlying or strike <= 0 or underlying <= 0:
        return None
    return int(round(math.log(strike / underlying) / width))


def _xs_mean(spreads):
    return (sum(spreads) / len(spreads)) if spreads else None


def trailing_residual_stats(db_path, ticker, market_type, bucket, before):
    """(#distinct dates, mean, sd) of trailing cross-sectional residuals for the
    rotation-stable (ticker, market_type, log-moneyness bucket) key, dates < before.

    Each historical date's residual = spread - that date's cross-sectional mean over
    the same (ticker, market_type) strikes (>= OPTIONS_MIN_XS strikes required)."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT date, poly_market_id, strike, underlying, spread_pp FROM options_implied "
        "WHERE ticker=? AND market_type=? AND date < ? AND spread_pp IS NOT NULL",
        (ticker, market_type, before))]
    con.close()
    by_date = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)
    vals, dates = [], set()
    for d, drows in by_date.items():
        sps = [x["spread_pp"] for x in drows]
        if len(sps) < OPTIONS_MIN_XS:
            continue
        m = _xs_mean(sps)
        for x in drows:
            if _moneyness_bucket(x["strike"], x["underlying"]) == bucket:
                vals.append(x["spread_pp"] - m)
                dates.add(d)
    n = len(dates)
    if len(vals) < 2:
        return n, (vals[0] if vals else None), None
    return n, statistics.mean(vals), statistics.pstdev(vals)


def build_trade_signals(db_path=DEFAULT_DB, z_thresh=Z_THRESH, min_obs=None):
    """Latest day's rows -> paper_portfolio signal dicts via the two-stage detrend.
    Returns [] when no contract clears the obs floor + |z| gate."""
    min_obs = OPTIONS_MIN_OBS if min_obs is None else min_obs
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    last = con.execute("SELECT MAX(date) d FROM options_implied").fetchone()["d"]
    today_rows = [dict(r) for r in con.execute(
        "SELECT * FROM options_implied WHERE date=?", (last,))] if last else []
    con.close()
    groups = {}
    for r in today_rows:
        if r.get("spread_pp") is None:
            continue
        groups.setdefault((r["ticker"], r["market_type"]), []).append(r)
    out = []
    for (tk, mt), grp in groups.items():
        sps = [r["spread_pp"] for r in grp]
        if len(sps) < OPTIONS_MIN_XS:  # not enough same-day strikes to detrend
            continue
        xs = _xs_mean(sps)
        for r in grp:
            bucket = _moneyness_bucket(r.get("strike"), r.get("underlying"))
            if bucket is None:
                continue
            residual = r["spread_pp"] - xs
            n, mu, sd = trailing_residual_stats(db_path, tk, mt, bucket, before=last)
            if n < OPTIONS_MIN_OBS_LOWCONF or mu is None or sd is None:
                continue
            z = (residual - mu) / max(sd, SD_FLOOR)
            if abs(z) < z_thresh:
                continue
            lowconf = n < min_obs
            side = "NO" if z > 0 else "YES"
            strike = r.get("strike")
            title = f"{tk} {mt} ${strike} ({r.get('expiry')})"
            try:
                dtc = max(0.1, (date.fromisoformat(r["expiry"]) - date.fromisoformat(last)).days)
            except Exception:
                dtc = 1
            conf = min(0.9, 0.5 + 0.08 * abs(z))
            if lowconf:
                conf *= 0.7
            out.append({
                "market_id": r["poly_market_id"],
                "market": title,
                "market_title": title,
                "side": side,
                "entry_price": r.get("poly_price"),
                "market_price": r.get("poly_price"),
                "confidence": round(conf, 3),
                "edge_pct": round(abs(residual - mu), 2),
                "strategy": "options_implied",
                "archetype": "options",
                "platform": "polymarket",
                "source": "options_implied",
                "days_to_close": dtc,
                "z_score": round(z, 2),
                "trailing_obs": n,
                "low_confidence": lowconf,
                "implied_prob": r.get("implied_prob"),
            })
    return out


def get_options_portfolio_signals():
    """Signals formatted for paper_portfolio.process_signals() (weather pattern)."""
    return build_trade_signals(DEFAULT_DB)


def open_trades():
    """Open paper positions for z-gated options signals via the shared engine path."""
    from signals.paper_portfolio import process_signals
    sigs = get_options_portfolio_signals()
    if not sigs:
        print("[options_implied] trade: 0 signals cleared the z-gate (need "
              f">= {OPTIONS_MIN_OBS_LOWCONF} trailing obs in the moneyness bucket and "
              f"|z| >= {Z_THRESH})")
        return {"opened": 0, "signals": 0}
    res = process_signals(sigs)
    print(f"[options_implied] trade: {len(sigs)} signals -> {res.get('opened', 0)} opened")
    return res


# ── Mid-Week Position Monitoring ────────────────────────────────────
# Re-evaluates open options paper positions Mon-Thu for price movement
# and edge decay. Take profit at >50% edge shrink, stop loss on z-flip.
# Mirrors weather_scanner.reeval_weather_positions() pattern.

from loguru import logger as _options_logger


def _fetch_poly_current_price(condition_id: str) -> float | None:
    """Fetch current YES price from Polymarket CLOB for a condition.
    Returns None if market closed or unreachable."""
    import urllib.request as _ur
    url = f"https://clob.polymarket.com/markets/{condition_id}"
    try:
        req = _ur.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with _ur.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
            tokens = data.get("tokens", [])
            if tokens:
                price = float(tokens[0].get("price", 0))
                return price if price > 0 else None
            # Fallback: try outcomePrices
            prices_raw = data.get("outcomePrices")
            if prices_raw:
                if isinstance(prices_raw, str):
                    prices = json.loads(prices_raw)
                else:
                    prices = prices_raw
                if prices and len(prices) > 0:
                    return float(prices[0])
            return None
    except Exception as e:
        _options_logger.debug(f"CLOB fetch failed for {condition_id[:16]}: {e}")
        return None


def reeval_options_positions() -> dict:
    """Check open options paper positions against current Polymarket prices.

    Closes positions when:
    1. Take profit: edge has shrunk >50% from entry
       (current_price moved toward fair value significantly)
    2. Stop loss: edge flipped (entry_z > 0 and price went up, or vice versa)
       Signal is gone or reversed.

    Returns dict with checked/closed/kept/errors counts.
    """
    import sqlite3
    from pathlib import Path as _Path

    results = {"checked": 0, "closed": 0, "kept": 0, "errors": 0, "details": []}
    base_dir = _Path(__file__).parent.parent

    # Connect to paper portfolio db
    db_path = base_dir / "storage" / "shadow_trades.db"
    if not db_path.exists():
        return results

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Get open options positions
    positions = conn.execute(
        "SELECT id, market_title, market_id, side, entry_price, bet_size, "
        "edge_pct, confidence, opened_at "
        "FROM paper_positions "
        "WHERE status='open' AND strategy='options_implied'"
    ).fetchall()

    if not positions:
        conn.close()
        return results

    conn.close()

    for pos in positions:
        results["checked"] += 1
        position_id = pos["id"]
        condition_id = pos["market_id"]
        entry_price = pos["entry_price"] or 0.5
        entry_side = pos["side"]
        entry_edge_pct = pos["edge_pct"] or 0
        bet_size = pos["bet_size"] or 0

        # 1. Fetch current price from CLOB
        current_price = _fetch_poly_current_price(condition_id)
        if current_price is None:
            results["kept"] += 1
            continue

        # 2.5 IV/RV overlay: if options expensive, reduce edge threshold (take profit sooner)
        iv_rv_factor = 1.0
        try:
            c3 = sqlite3.connect(str(DEFAULT_DB))
            ticker_row = c3.execute(
                "SELECT ticker, iv FROM options_implied WHERE poly_market_id=? ORDER BY date DESC LIMIT 1",
                (condition_id,),
            ).fetchone()
            c3.close()
            if ticker_row and ticker_row[1]:
                tk = ticker_row[0]
                iv = ticker_row[1]
                from signals.vol_spread import get_iv_rv_ratio
                ratio = get_iv_rv_ratio(tk, iv)
                if ratio and ratio > 1.5:
                    iv_rv_factor = 0.7  # 30% more eager to take profit
        except Exception:
            pass

        # 2. Determine if edge has degraded
        close_reason = None

        if entry_side == "YES":
            # We bet YES. Edge = implied_prob (at scanner) - entry_price
            # Edge positive means we bought below fair value
            # If current_price > entry_price, edge has shrunk
            price_move = current_price - entry_price
            edge_at_entry = entry_edge_pct / 100.0 if entry_edge_pct > 0 else 0.05

            if price_move > 0:
                # Price moved up — edge is shrinking
                remaining_edge = max(0, edge_at_entry - price_move)
                edge_shrink_pct = 1 - (remaining_edge / max(edge_at_entry, 0.001))
                if edge_shrink_pct > 0.5 * iv_rv_factor:
                    close_reason = f"take_profit: edge_decayed_{edge_shrink_pct:.0%}"
            elif price_move < 0:
                # Price moved down — we're losing money, edge increased
                # This is fine if we hold — signal is even stronger
                pass

        elif entry_side == "NO":
            # We bet NO. Edge = (1 - entry_price) - (1 - implied)
            # Same logic inverted
            price_move = current_price - entry_price
            edge_at_entry = entry_edge_pct / 100.0 if entry_edge_pct > 0 else 0.05

            if price_move < 0:
                # Price moved down — NO is winning, edge shrinks
                remaining_edge = max(0, edge_at_entry - abs(price_move))
                edge_shrink_pct = 1 - (remaining_edge / max(edge_at_entry, 0.001))
                if edge_shrink_pct > 0.5 * iv_rv_factor:
                    close_reason = f"take_profit: edge_decayed_{edge_shrink_pct:.0%}"
            elif price_move > 0:
                # Price moved up — NO losing, edge increased
                pass

        # 3. Execute close if triggered
        if close_reason:
            try:
                # Calculate PnL
                if entry_side == "YES":
                    pnl = bet_size * (current_price / entry_price - 1) if entry_price > 0 else 0
                else:
                    pnl = bet_size * (entry_price / current_price - 1) if current_price > 0 else 0

                # Direct DB update
                c2 = sqlite3.connect(str(db_path))
                c2.execute(
                    "UPDATE paper_positions SET status='stopped', closed_at=?, "
                    "exit_price=?, pnl=?, close_reason=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(),
                     round(current_price, 4), round(pnl, 2), close_reason, position_id)
                )
                c2.commit()
                c2.close()

                _options_logger.info(
                    f"Options position {position_id}: {close_reason} "
                    f"(entry={entry_price}, current={current_price}, "
                    f"side={entry_side}, pnl=${pnl:+.2f})"
                )
                results["closed"] += 1
                results["details"].append({
                    "id": position_id,
                    "reason": close_reason,
                    "entry_price": entry_price,
                    "exit_price": current_price,
                    "pnl": round(pnl, 2),
                })
            except Exception as e:
                _options_logger.warning(f"Options close failed for {position_id}: {e}")
                results["errors"] += 1
        else:
            results["kept"] += 1

    if results["closed"] > 0:
        _options_logger.info(
            f"Options reeval: {results['checked']} checked, "
            f"{results['closed']} closed, {results['errors']} errors"
        )

    return results


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Options-implied signal: scan and optionally paper-trade")
    ap.add_argument("--trade", action="store_true",
                    help="after scanning, open paper positions for z-gated signals")
    args = ap.parse_args()
    run()
    if args.trade:
        open_trades()
