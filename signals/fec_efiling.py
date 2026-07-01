#!/usr/bin/env python3
"""FEC real-time eFiling monitor — minutes-latency Super PAC spending alerts."""

import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from loguru import logger

FEC_API = "https://api.open.fec.gov/v1"
FEC_KEY = os.environ.get("FEC_API_KEY", "DEMO_KEY")
CACHE_DIR = Path(__file__).parent.parent / "storage" / "fec_efiling_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 900  # 15 minutes — eFiling data is time-sensitive
SEEN_FILE = CACHE_DIR / "seen_filings.json"

# Only surface filings above this threshold
MIN_FILING_AMOUNT = 100_000


def _fec_get(endpoint: str, params: dict = None, timeout: int = 15) -> dict:
    """GET request to FEC API with short-lived cache for eFiling data."""
    params = params or {}
    params["api_key"] = FEC_KEY
    url = f"{FEC_API}{endpoint}?{urlencode(params)}"

    cache_key = url.replace("/", "_").replace("?", "_").replace("&", "_")[:120]
    cache_path = CACHE_DIR / f"{cache_key}.json"
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            with open(cache_path) as f:
                return json.load(f)

    req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        with open(cache_path, "w") as f:
            json.dump(data, f)
        return data
    except Exception as e:
        logger.warning("FEC eFiling API error on {}: {}", endpoint, e)
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f)
        return {"results": [], "pagination": {"count": 0}}


def _load_seen() -> set:
    """Load set of previously seen filing IDs."""
    if SEEN_FILE.exists():
        try:
            with open(SEEN_FILE) as f:
                data = json.load(f)
            # Prune entries older than 7 days
            cutoff = time.time() - 7 * 86400
            return {k for k, ts in data.items() if ts > cutoff}
        except Exception:
            return set()
    return set()


def _save_seen(seen: dict):
    """Save seen filing IDs with timestamps."""
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f)


def _clean_name(name: str) -> str:
    """Clean FEC name format (LAST, FIRST) to readable."""
    if not name:
        return ""
    if "," not in name:
        return name.title()
    parts = name.split(",", 1)
    last = parts[0].strip().title()
    first = parts[1].strip().title().split(" ")[0] if len(parts) > 1 else ""
    return f"{first} {last}".strip()


def _party_code(party: str) -> str:
    p = (party or "").upper()
    if "DEM" in p:
        return "D"
    if "REP" in p:
        return "R"
    return "?"


OFFICE_MAP = {"S": "senate", "H": "house", "P": "presidential"}


def fetch_efiling_spending(hours: int = 24) -> list[dict]:
    """Fetch recent eFiled independent expenditures (Schedule E).

    These are the real-time filings — minutes after a Super PAC spends money,
    the filing appears here. This is the fastest public campaign finance signal.
    """
    min_date = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d")
    all_results = []
    page = 1

    while page <= 5:  # Cap at 5 pages to avoid burning rate limit
        params = {
            "cycle": 2026,
            "sort": "-receipt_date",
            "min_date": min_date,
            "per_page": 100,
            "page": page,
        }
        data = _fec_get("/schedules/schedule_e/efile/", params)
        results = data.get("results", [])
        if not results:
            break

        for r in results:
            amount = float(r.get("expenditure_amount") or 0)
            if amount <= 0 or amount > 50_000_000:
                continue

            sup_opp = r.get("support_oppose_indicator", "")
            all_results.append({
                "filing_id": str(r.get("file_number", r.get("sub_id", ""))),
                "committee_name": r.get("committee_name", "") or r.get("payee_name", ""),
                "candidate_name": _clean_name(r.get("candidate_name", "")),
                "candidate_state": r.get("candidate_office_state", ""),
                "candidate_office": r.get("candidate_office", ""),
                "candidate_party": _party_code(r.get("candidate_party", "")),
                "support_oppose": "S" if sup_opp == "S" else ("O" if sup_opp == "O" else "?"),
                "amount": amount,
                "date": r.get("expenditure_date", r.get("receipt_date", "")),
                "description": r.get("expenditure_description", "")[:200],
            })

        pages = data.get("pagination", {}).get("pages", 1)
        if page >= pages:
            break
        page += 1

    logger.info("FEC eFiling: fetched {} expenditures ({}h window)", len(all_results), hours)
    return all_results


def detect_new_filings(filings: list[dict]) -> list[dict]:
    """Diff against previously seen filings to find NEW ones.

    Returns only filings we haven't seen before, above MIN_FILING_AMOUNT.
    Updates the seen set on disk.
    """
    seen = _load_seen()
    seen_with_ts = {}
    # Reload full dict for timestamps
    if SEEN_FILE.exists():
        try:
            with open(SEEN_FILE) as f:
                seen_with_ts = json.load(f)
        except Exception:
            seen_with_ts = {}

    new_filings = []
    now = time.time()

    for f in filings:
        fid = f["filing_id"]
        if not fid or fid in seen:
            continue
        if f["amount"] < MIN_FILING_AMOUNT:
            continue
        new_filings.append(f)
        seen_with_ts[fid] = now

    # Mark all filings as seen (even small ones)
    for f in filings:
        fid = f["filing_id"]
        if fid and fid not in seen_with_ts:
            seen_with_ts[fid] = now

    _save_seen(seen_with_ts)

    new_filings.sort(key=lambda x: -x["amount"])
    if new_filings:
        logger.info("FEC eFiling: {} NEW filings above ${:,.0f}", len(new_filings), MIN_FILING_AMOUNT)
    return new_filings


def build_efiling_overlay() -> dict:
    """Build eFiling overlay for election report.

    Returns recent filings + new filing alerts.
    """
    filings = fetch_efiling_spending(hours=48)
    new = detect_new_filings(filings)

    # Aggregate by state/office for summary
    by_race = {}
    for f in filings:
        if f["amount"] < MIN_FILING_AMOUNT:
            continue
        state = f["candidate_state"]
        office = f["candidate_office"]
        if not state or not office:
            continue
        key = f"{state}_{office}"
        if key not in by_race:
            by_race[key] = {
                "state": state,
                "office": OFFICE_MAP.get(office, office),
                "total_spend": 0,
                "filings": 0,
                "top_committee": "",
                "top_amount": 0,
            }
        by_race[key]["total_spend"] += f["amount"]
        by_race[key]["filings"] += 1
        if f["amount"] > by_race[key]["top_amount"]:
            by_race[key]["top_amount"] = f["amount"]
            by_race[key]["top_committee"] = f["committee_name"]

    return {
        "efiling_alerts": new[:10],
        "efiling_recent": filings[:20],
        "efiling_by_race": list(by_race.values()),
        "efiling_count": len(filings),
    }
