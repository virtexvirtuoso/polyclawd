"""Fed divergence research log: Kalshi KXFED vs Atlanta Fed Market Probability Tracker.

RECONSTRUCTED 2026-05-27 from data-research/fed_divergence_history.csv schema,
fed_divergence_log.txt signatures, and sibling data-research/intraday_lead_lag.py.
Previous version was clobbered before recovery; this rebuild is behavior-compatible
with the prior CSV/log outputs but exact line-for-line parity is not guaranteed.

Standalone research log — not a Polyclawd-coupled auto-trader.
Vault context:
  02-Projects/Polyclawd/Research/Alpaca-Integration-Research.md
  02-Projects/Polyclawd/Infrastructure/AtlantaFed-Tracker-Ingestion.md
  02-Projects/Polyclawd/05-Decisions/2026-05-27-Alpaca-vs-CME-MVP-Scope.md

Wired to launchd: ~/Library/LaunchAgents/com.virtuoso.fed-divergence.plist (daily 18:30 ET).

Match semantics:
  Kalshi KXFED contract "rate above X%" (strike_type=greater) <-> AtlFed Prob(rate > X bps),
  computed as sum of Prob: <Y>bps - <Z>bps fields where Y >= target_bps.
  AtlFed only forecasts quarterly FOMC meetings; non-quarterly Kalshi meetings stay
  unmatched and write atlfed_prob_pct=NULL.
"""

from __future__ import annotations

import csv
import io
import logging
import pathlib
import re
import sys
import time
from datetime import date, datetime

import pandas as pd
import requests

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_SERIES = "KXFED"
ATLFED_URL = (
    "https://www.atlantafed.org/-/media/Project/Atlanta/FRBA/Documents/"
    "cenfis/market-probability-tracker/mpt_histdata.xlsx"
)
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DATA_DIR = pathlib.Path.home() / "Projects" / "polyclawd" / "data-research"
CSV_OUT = DATA_DIR / "fed_divergence_history.csv"
LOG_OUT = DATA_DIR / "fed_divergence_log.txt"

ALERT_THRESHOLD_PP = 15.0  # |spread| > this triggers a warning line in the log

CSV_FIELDS = [
    "date",
    "meeting_date",
    "target_rate_pct",
    "target_bps",
    "kalshi_bid",
    "kalshi_ask",
    "kalshi_mid_pct",
    "atlfed_prob_pct",
    "spread_pp",
    "kalshi_oi",
    "kalshi_vol_24h",
]

ATLFED_BUCKET_RE = re.compile(r"Prob:\s*(\d+)bps\s*-\s*(\d+)bps")


# ----------------------------------------------------------------- Kalshi


def fetch_kalshi_kxfed() -> list[dict]:
    """Page through open KXFED markets."""
    markets: list[dict] = []
    cursor = ""
    while True:
        params = {"series_ticker": KALSHI_SERIES, "limit": 200, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{KALSHI_API}/markets", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        markets.extend(data.get("markets", []))
        cursor = data.get("cursor", "")
        if not cursor:
            break
        time.sleep(0.5)
    return markets


def parse_kalshi(market: dict) -> dict | None:
    """Pull the divergence-relevant fields off a KXFED market doc."""
    try:
        floor = float(market.get("floor_strike", 0) or 0)
        if floor <= 0:
            return None
        meeting_date = str(market["close_time"])[:10]
        bid = float(market.get("yes_bid_dollars", 0) or 0)
        ask = float(market.get("yes_ask_dollars", 0) or 0)
        if (bid + ask) == 0:
            return None
        return {
            "meeting_date": meeting_date,
            "target_rate_pct": floor,
            "target_bps": int(round(floor * 100)),
            "kalshi_bid": bid * 100,
            "kalshi_ask": ask * 100,
            "kalshi_mid_pct": (bid + ask) / 2 * 100,
            "kalshi_oi": float(market.get("open_interest_fp", 0) or 0),
            "kalshi_vol_24h": float(market.get("volume_24h_fp", 0) or 0),
        }
    except (KeyError, ValueError, TypeError):
        return None


# ----------------------------------------------------------------- AtlFed


def fetch_atlfed() -> pd.DataFrame:
    r = requests.get(ATLFED_URL, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    df = pd.read_excel(io.BytesIO(r.content), sheet_name="DATA")
    df["date"] = pd.to_datetime(df["date"])
    df["reference_start"] = pd.to_datetime(df["reference_start"])
    return df


def atlfed_prob_above(atlfed: pd.DataFrame, as_of: pd.Timestamp, meeting_date: str, target_bps: int) -> float | None:
    """P(rate > target_bps) at the AtlFed snapshot for as_of + meeting_date."""
    md = pd.Timestamp(meeting_date)
    sub = atlfed[(atlfed.date == as_of) & (atlfed.reference_start == md)]
    if sub.empty:
        return None
    total = 0.0
    matched = 0
    for _, row in sub.iterrows():
        m = ATLFED_BUCKET_RE.match(str(row.field))
        if not m:
            continue
        lower = int(m.group(1))
        if lower >= target_bps:
            total += float(row.value)
            matched += 1
    return total if matched else None


# ----------------------------------------------------------------- IO


def append_csv(rows: list[dict]) -> int:
    """Append rows, skipping any (date, meeting_date, target_bps) already present.

    Idempotent: re-running on the same day will not create duplicate rows.
    Returns the number of rows actually written.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not CSV_OUT.exists()

    seen: set[tuple] = set()
    if not new_file:
        with open(CSV_OUT, newline="") as f:
            for existing in csv.DictReader(f):
                seen.add((existing["date"], existing["meeting_date"], existing["target_bps"]))

    written = 0
    with open(CSV_OUT, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            w.writeheader()
        for row in rows:
            key = (str(row.get("date")), str(row.get("meeting_date")), str(row.get("target_bps")))
            if key in seen:
                continue
            w.writerow({k: row.get(k) for k in CSV_FIELDS})
            seen.add(key)
            written += 1
    return written


def log_line(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_OUT, "a") as f:
        f.write(line + "\n")


# ----------------------------------------------------------------- main


def main() -> int:
    today = date.today()
    log_line(f"=== Fed Divergence — {today} ===")

    raw = fetch_kalshi_kxfed()
    parsed = [r for r in (parse_kalshi(m) for m in raw) if r is not None]
    k_meetings = sorted({r["meeting_date"] for r in parsed})
    log_line(f"Kalshi: {len(parsed)} contracts, meetings: {k_meetings[:4]}")

    atlfed = fetch_atlfed()
    today_ts = pd.Timestamp(today)
    today_at = atlfed[atlfed.date == today_ts]
    if today_at.empty:
        most_recent = atlfed.date.max()
        today_at = atlfed[atlfed.date == most_recent]
        log_line(f"AtlFed: today not yet published, using {most_recent.date()}")
        atlfed_as_of = most_recent
    else:
        atlfed_as_of = today_ts
    a_meetings = sorted({d.date().isoformat() for d in today_at.reference_start.unique()})
    log_line(f"AtlFed: {len(today_at)} entries, meetings: {a_meetings[:4]}")

    rows: list[dict] = []
    matched = 0
    for r in parsed:
        r["date"] = today.isoformat()
        prob = atlfed_prob_above(atlfed, atlfed_as_of, r["meeting_date"], r["target_bps"])
        if prob is not None:
            r["atlfed_prob_pct"] = prob
            r["spread_pp"] = r["kalshi_mid_pct"] - prob
            matched += 1
            if abs(r["spread_pp"]) > ALERT_THRESHOLD_PP:
                log_line(
                    f"  ⚠️ {r['meeting_date']} {r['target_rate_pct']}%: "
                    f"K={r['kalshi_mid_pct']:.1f}% A={prob:.1f}% Δ={r['spread_pp']:+.1f}pp"
                )
        else:
            r["atlfed_prob_pct"] = None
            r["spread_pp"] = None
        rows.append(r)
    written = append_csv(rows)
    skipped = len(rows) - written
    suffix = f", {skipped} skipped as dupes" if skipped else ""
    log_line(f"Wrote {written} rows ({matched} matched{suffix})")

    hist = pd.read_csv(CSV_OUT)
    s = hist["spread_pp"].dropna()
    n, mu, sigma = len(s), (s.mean() if len(s) else 0.0), (s.std() if len(s) > 1 else 0.0)
    log_line(f"  History: {n} obs, μ={mu:.1f}pp, σ={sigma:.1f}pp")
    log_line("Done.\n")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        sys.exit(main())
    except Exception as e:
        log_line(f"ERROR: {type(e).__name__}: {e}")
        raise
