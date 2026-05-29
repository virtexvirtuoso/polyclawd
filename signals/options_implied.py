"""Single-name options-implied probability signal (NVDA/META/MSFT/AAPL/AMZN).
Alpaca OPRA IV -> N(d2) implied prob vs Polymarket weekly-close ladders.
VPS cron, post-close ET. Display-only (not wired to the trade engine).
Scope: vault 02-Projects/Polyclawd/Research/2026-05-29-Scope-Single-Name-Options-Implied-Signal.md
"""

from __future__ import annotations
import math, sqlite3, pathlib, json, re, os, statistics
from datetime import date, datetime, timezone

import requests

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
  PRIMARY KEY (date, poly_market_id, strike)
);
"""


def init_db(db_path):
    pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
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
]


def upsert_rows(db_path, rows):
    """Insert rows, skipping existing (date,poly_market_id,strike). Returns # written."""
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
UA = {"User-Agent": "Mozilla/5.0 polyclawd-options"}
NAMES = ["NVDA", "META", "MSFT", "AAPL", "AMZN"]
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
        if "between" in ql and nums and nums[0][1]:
            lo, hi, mtype = _money(nums[0][0]), _money(nums[0][1]), "bracket"
        elif ("above" in ql or "higher" in ql) and nums:
            lo, hi, mtype = _money(nums[0][0]), None, "above"
        elif ("below" in ql or "lower" in ql) and nums:
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
    for tk in NAMES:
        try:
            S = underlying_price(tk)
            for ev in fetch_poly_close_events(tk):
                pev = parse_poly_event(ev, tk)
                exp = pev["resolution_date"]
                if not exp or exp <= today:  # skip 0DTE/expired (IV=0)
                    continue
                snaps = fetch_alpaca_snapshot(tk, exp)
                T = _years_to(exp, now)
                for m in pev["markets"]:
                    if (m["poly_liquidity"] or 0) < MIN_LIQ:  # liquidity gate
                        continue
                    K = m["bracket_lo"]
                    iv = pick_iv(snaps, exp, K, right="C")
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
                        }
                    )
        except Exception as e:
            print(f"[options_implied] {tk} failed: {type(e).__name__}: {e}")
    written = upsert_rows(db_path, rows)
    print(f"[options_implied] {today}: {len(rows)} computed, {written} written")
    return written


if __name__ == "__main__":
    run()
