"""Fed divergence research log: Kalshi KXFED vs Atlanta Fed Market Probability Tracker.

RECONSTRUCTED 2026-05-27 from data-research/fed_divergence_history.csv schema,
fed_divergence_log.txt signatures, and sibling data-research/intraday_lead_lag.py.
Previous version was clobbered before recovery; this rebuild is behavior-compatible
with the prior CSV/log outputs but exact line-for-line parity is not guaranteed.

QUANT-REVIEW FIXES 2026-05-28 (verdict FLAWED → addressed):
  - atlfed_as_of column added: records WHICH AtlFed snapshot was used (the run can
    fall back to yesterday's publish), killing a look-ahead bug in any future backtest.
  - AtlFed prob normalized by total bucket mass (buckets sum ~99.98, not 100).
  - Grid-alignment guard: target_bps must be a 25bp multiple or the bucket join is undefined.
  - Liquidity gate on alerts: wide bid/ask or thin OI no longer trips a warning.
  - Risk-premium detrending: AtlFed is risk-neutral (SOFR-options-implied), Kalshi mid is
    near-real-world. The RAW spread is a premium, not edge. Alerts now use a per-(meeting,
    target) rolling z-score once >=MIN_ZSCORE_OBS history exists; until then a fixed-threshold
    fallback fires but is explicitly labelled premium-contaminated (NOT a tradeable signal).

Standalone research log — not a Polyclawd-coupled auto-trader.
Vault context:
  02-Projects/Polyclawd/Research/Alpaca-Integration-Research.md
  02-Projects/Polyclawd/Infrastructure/AtlantaFed-Tracker-Ingestion.md
  02-Projects/Polyclawd/Investigations/2026-05-27-fed-divergence-script-clobber.md

Wired to launchd: ~/Library/LaunchAgents/com.virtuoso.fed-divergence.plist (daily 18:30 ET).

Match semantics:
  Kalshi KXFED contract "upper bound > X%" (strike_type=greater) <-> AtlFed P(rate > X bps),
  estimated as (sum of Prob: <Y>bps - <Z>bps buckets where Y >= target_bps) / total_mass.
  AtlFed only forecasts quarterly FOMC meetings; non-quarterly Kalshi meetings stay
  unmatched and write atlfed_prob_pct=NULL.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

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

DATA_DIR = Path.home() / "Projects" / "polyclawd" / "data-research"
CSV_OUT = DATA_DIR / "fed_divergence_history.csv"
LOG_OUT = DATA_DIR / "fed_divergence_log.txt"

# Alerting. The RAW spread (kalshi - atlfed) is a risk-premium level, not edge, so the
# fixed threshold is only a fallback for contracts without enough history to z-score.
ALERT_THRESHOLD_PP = 15.0      # fallback only; labelled premium-contaminated when used
ZSCORE_THRESHOLD = 2.0         # preferred: |z| of detrended spread
MIN_ZSCORE_OBS = 30            # per-(meeting,target) trailing obs before z-score activates (ideal: ~12mo)
MAX_ALERT_SPREAD_WIDTH_PP = 15.0  # skip alerts on illiquid wide Kalshi books
MIN_ALERT_OI = 100.0           # skip alerts below this open interest

CSV_FIELDS = [
    "date",            # run date (when this row was logged)
    "atlfed_as_of",    # AtlFed snapshot date actually used (may lag `date` by 1+ days)
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
        # close_time is mid-afternoon ET (~18-19 UTC) for FOMC markets, so the UTC date
        # slice equals the ET meeting date. This holds only because FOMC closes are never
        # near midnight UTC; do not reuse this slice for other (e.g. crypto) series.
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


def atlfed_prob_above(
    atlfed: pd.DataFrame, as_of: pd.Timestamp, meeting_date: str, target_bps: int
) -> float | None:
    """P(rate > target_bps) at the AtlFed snapshot for (as_of, meeting_date), in percent.

    Estimator = (mass in buckets with lower >= target_bps) / (total bucket mass) * 100.
    Normalizing by total mass removes the residual-sum effect (AtlFed buckets sum to
    ~99.98, not exactly 100). Requires target_bps on the 25bp grid the buckets use.
    """
    if target_bps % 25 != 0:
        # buckets are half-open [lo, hi) on a 25bp grid; a non-aligned target would
        # split the straddling bucket and the lower>=target rule would misclassify it.
        return None
    md = pd.Timestamp(meeting_date)
    sub = atlfed[(atlfed.date == as_of) & (atlfed.reference_start == md)]
    if sub.empty:
        return None
    total_above = 0.0
    total_all = 0.0
    matched = 0
    for _, row in sub.iterrows():
        m = ATLFED_BUCKET_RE.match(str(row.field))
        if not m:
            continue
        lower = int(m.group(1))
        val = float(row.value)
        total_all += val
        if lower >= target_bps:
            total_above += val
            matched += 1
    if matched == 0 or total_all <= 0:
        return None
    return total_above / total_all * 100.0


# ----------------------------------------------------------------- detrending


def trailing_stats(
    hist: pd.DataFrame, meeting_date: str, target_bps: int, before: str
) -> tuple[int, float, float]:
    """Trailing (n, mean, std) of spread_pp for this (meeting, target) strictly before `before`.

    Used to detrend the risk premium: alert on deviation from the contract's own history,
    not on the raw level. Returns (n, nan, nan) when below MIN_ZSCORE_OBS.
    """
    if hist.empty:
        return 0, math.nan, math.nan
    sub = hist[
        (hist["meeting_date"].astype(str) == str(meeting_date))
        & (pd.to_numeric(hist["target_bps"], errors="coerce") == target_bps)
        & (hist["date"].astype(str) < before)
        & (pd.to_numeric(hist["spread_pp"], errors="coerce").notna())
    ]
    n = len(sub)
    if n < MIN_ZSCORE_OBS:
        return n, math.nan, math.nan
    vals = pd.to_numeric(sub["spread_pp"], errors="coerce").dropna()
    return n, float(vals.mean()), float(vals.std())


# ----------------------------------------------------------------- IO


def migrate_csv_header() -> None:
    """One-time: if the existing CSV predates a column (e.g. atlfed_as_of), rewrite it
    with the full header, backfilling missing cells as empty. Idempotent."""
    if not CSV_OUT.exists():
        return
    with open(CSV_OUT, newline="") as f:
        reader = csv.DictReader(f)
        existing = reader.fieldnames or []
        if set(CSV_FIELDS).issubset(set(existing)):
            return
        rows = list(reader)
    with open(CSV_OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def append_csv(rows: list[dict]) -> int:
    """Append rows, skipping any (date, meeting_date, target_bps) already present.

    Idempotent: re-running on the same day will not create duplicate rows.
    Returns the number of rows actually written.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    migrate_csv_header()
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
    as_of_str = atlfed_as_of.date().isoformat()
    a_meetings = sorted({d.date().isoformat() for d in today_at.reference_start.unique()})
    log_line(f"AtlFed: {len(today_at)} entries (as_of {as_of_str}), meetings: {a_meetings[:4]}")

    # Prior history (for per-contract z-score detrending of the risk premium).
    migrate_csv_header()
    prior = pd.read_csv(CSV_OUT) if CSV_OUT.exists() else pd.DataFrame(columns=CSV_FIELDS)

    rows: list[dict] = []
    matched = 0
    alerts_z = 0
    alerts_raw = 0
    for r in parsed:
        r["date"] = today.isoformat()
        r["atlfed_as_of"] = as_of_str
        prob = atlfed_prob_above(atlfed, atlfed_as_of, r["meeting_date"], r["target_bps"])
        if prob is None:
            r["atlfed_prob_pct"] = None
            r["spread_pp"] = None
            rows.append(r)
            continue
        r["atlfed_prob_pct"] = prob
        r["spread_pp"] = r["kalshi_mid_pct"] - prob
        matched += 1

        # Liquidity gate — do not alert off a wide/illiquid Kalshi book.
        width = r["kalshi_ask"] - r["kalshi_bid"]
        if width > MAX_ALERT_SPREAD_WIDTH_PP or r["kalshi_oi"] < MIN_ALERT_OI:
            rows.append(r)
            continue

        # Preferred: z-score vs the contract's own trailing spread (premium-detrended).
        n, mu, sigma = trailing_stats(prior, r["meeting_date"], r["target_bps"], r["date"])
        if n >= MIN_ZSCORE_OBS and sigma is not None and not math.isnan(sigma) and sigma > 0:
            z = (r["spread_pp"] - mu) / sigma
            if abs(z) > ZSCORE_THRESHOLD:
                alerts_z += 1
                log_line(
                    f"  ⚠️ z={z:+.1f} {r['meeting_date']} {r['target_rate_pct']}%: "
                    f"K={r['kalshi_mid_pct']:.1f}% A={prob:.1f}% Δ={r['spread_pp']:+.1f}pp "
                    f"(μ={mu:+.1f} σ={sigma:.1f} n={n})"
                )
        else:
            # Fallback: raw threshold. This is the risk-PREMIUM, not tradeable edge.
            if abs(r["spread_pp"]) > ALERT_THRESHOLD_PP:
                alerts_raw += 1
                log_line(
                    f"  ⚠️ [raw/premium-contaminated, n={n}<{MIN_ZSCORE_OBS}] "
                    f"{r['meeting_date']} {r['target_rate_pct']}%: "
                    f"K={r['kalshi_mid_pct']:.1f}% A={prob:.1f}% Δ={r['spread_pp']:+.1f}pp"
                )
        rows.append(r)

    written = append_csv(rows)
    skipped = len(rows) - written
    suffix = f", {skipped} skipped as dupes" if skipped else ""
    log_line(
        f"Wrote {written} rows ({matched} matched{suffix}); "
        f"alerts: {alerts_z} z-score, {alerts_raw} raw-fallback"
    )

    hist = pd.read_csv(CSV_OUT)
    s = pd.to_numeric(hist["spread_pp"], errors="coerce").dropna()
    n, mu, sigma = len(s), (s.mean() if len(s) else 0.0), (s.std() if len(s) > 1 else 0.0)
    log_line(f"  History: {n} obs, μ={mu:.1f}pp, σ={sigma:.1f}pp (raw spread = premium, see header)")
    log_line("Done.\n")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        sys.exit(main())
    except Exception as e:
        log_line(f"ERROR: {type(e).__name__}: {e}")
        raise
