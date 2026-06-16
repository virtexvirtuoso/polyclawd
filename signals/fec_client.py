#!/usr/bin/env python3
"""FEC OpenFEC API client — campaign finance data for election intelligence."""

import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

FEC_API = "https://api.open.fec.gov/v1"
FEC_KEY = os.environ.get("FEC_API_KEY", "DEMO_KEY")
CACHE_DIR = Path(__file__).parent.parent / "storage" / "fec_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 6 * 3600  # 6 hours
EFILING_CACHE_TTL = 3600  # 1 hour for eFiling data


def _fec_get(endpoint: str, params: dict = None, timeout: int = 15) -> dict:
    """GET request to FEC API with caching."""
    params = params or {}
    params["api_key"] = FEC_KEY
    from urllib.parse import urlencode
    url = f"{FEC_API}{endpoint}?{urlencode(params)}"

    # Check cache
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
        # Cache result
        with open(cache_path, "w") as f:
            json.dump(data, f)
        return data
    except Exception as e:
        logger.warning("FEC API error on {}: {}", endpoint, e)
        # Return cached data even if stale
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f)
        return {"results": [], "pagination": {"count": 0}}


def fetch_senate_fundraising(election_year: int = 2026) -> list[dict]:
    """Fetch fundraising totals for all Senate candidates in an election cycle."""
    all_candidates = []
    page = 1
    while True:
        data = _fec_get("/candidates/totals/", {
            "office": "S",
            "election_year": election_year,
            "sort": "-receipts",
            "per_page": 100,
            "page": page,
            "is_active_candidate": "true",
        })
        results = data.get("results", [])
        if not results:
            break
        for c in results:
            receipts = float(c.get("receipts") or 0)
            if receipts < 10_000:
                continue  # Skip non-serious candidates
            all_candidates.append({
                "name": _clean_name(c.get("name", "")),
                "party": _party_code(c.get("party_full", "")),
                "state": c.get("state", ""),
                "receipts": receipts,
                "cash_on_hand": float(c.get("cash_on_hand_end_period") or 0),
                "disbursements": float(c.get("disbursements") or 0),
                "individual_contributions": float(c.get("individual_itemized_contributions") or 0),
                "incumbent": c.get("incumbent_challenge_full", ""),
                "candidate_id": c.get("candidate_id", ""),
            })
        pages = data.get("pagination", {}).get("pages", 1)
        if page >= pages:
            break
        page += 1
    logger.info("FEC: fetched {} Senate candidates for {}", len(all_candidates), election_year)
    return all_candidates


def _fetch_committee_ids(candidate_ids: list[str]) -> dict:
    """Fetch principal committee IDs for a list of candidate IDs.

    Returns dict mapping candidate_id -> committee_id. Cached 24h.
    """
    cache_path = CACHE_DIR / "committee_ids.json"
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < 86400:
            with open(cache_path) as f:
                cached = json.load(f)
            # Return if we have most of the IDs
            if sum(1 for cid in candidate_ids if cid in cached) > len(candidate_ids) * 0.8:
                return cached

    mapping = {}
    if cache_path.exists():
        with open(cache_path) as f:
            mapping = json.load(f)

    for cand_id in candidate_ids:
        if cand_id in mapping:
            continue
        data = _fec_get(f"/candidate/{cand_id}/committees/", {
            "designation": "P",  # Principal campaign committee
            "per_page": 1,
        })
        results = data.get("results", [])
        if results:
            mapping[cand_id] = results[0].get("committee_id", "")
        time.sleep(0.3)

    with open(cache_path, "w") as f:
        json.dump(mapping, f)
    return mapping


def fetch_efiling_reports(committee_ids: list[str]) -> dict:
    """Fetch latest eFiled report summaries for Senate committees.

    Returns dict mapping committee_id -> {cash_on_hand, total_receipts,
    coverage_end, filed_date}. These are from the most recent eFiling,
    which can be days-fresh (vs quarterly processed totals which lag weeks).
    """
    results = {}

    for cid in committee_ids:
        if not cid:
            continue

        cache_key = f"efiling_report_{cid}"
        cache_path = CACHE_DIR / f"{cache_key}.json"
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < EFILING_CACHE_TTL:
                with open(cache_path) as f:
                    results[cid] = json.load(f)
                continue

        data = _fec_get("/efile/reports/house-senate/", {
            "committee_id": cid,
            "per_page": 3,
            "sort": "-receipt_date",
        })
        rows = data.get("results", [])

        # Find the most recent non-amended filing
        best = None
        for r in rows:
            if r.get("is_amended"):
                continue
            best = r
            break
        if not best and rows:
            best = rows[0]

        if best:
            entry = {
                "cash_on_hand": float(best.get("cash_on_hand_end_period") or 0),
                "total_receipts": float(best.get("total_receipts") or 0),
                "total_disbursements": float(best.get("total_disbursements") or 0),
                "individual_contributions": float(best.get("individual_itemized_contributions") or 0),
                "individual_unitemized": float(best.get("individual_unitemized_contributions") or 0),
                "coverage_start": best.get("coverage_start_date", ""),
                "coverage_end": best.get("coverage_end_date", ""),
                "filed_date": best.get("receipt_date", ""),
            }
        else:
            entry = {}

        results[cid] = entry
        if entry:
            with open(cache_path, "w") as f:
                json.dump(entry, f)

        time.sleep(0.5)

    filed_count = sum(1 for r in results.values() if r.get("filed_date"))
    logger.info("eFiling reports: {}/{} committees with fresh filings",
                filed_count, len(results))
    return results


def enrich_with_efiling(candidates: list[dict], days: int = 90) -> list[dict]:
    """Enrich quarterly candidate data with near-real-time eFiling report data.

    Updates cash_on_hand and adds filing freshness metadata from the latest
    eFiled report. Only enriches top R+D candidate per state to stay within
    API rate limits.
    """
    # Select top R and D candidate per state (max ~70 committees instead of 182)
    top_by_state = {}
    for c in candidates:
        key = (c["state"], c["party"])
        if c["party"] not in ("R", "D"):
            continue
        if key not in top_by_state or c["receipts"] > top_by_state[key]["receipts"]:
            top_by_state[key] = c
    enrich_ids = {c["candidate_id"] for c in top_by_state.values() if c.get("candidate_id")}

    cand_ids = [cid for cid in enrich_ids if cid]
    if not cand_ids:
        return candidates

    committee_map = _fetch_committee_ids(cand_ids)
    committee_ids = [committee_map.get(cid, "") for cid in cand_ids]
    committee_ids = [c for c in committee_ids if c]

    if not committee_ids:
        return candidates

    efiling = fetch_efiling_reports(committee_ids)

    # Merge back into candidates
    for c in candidates:
        cid = c.get("candidate_id", "")
        comm_id = committee_map.get(cid, "")
        if comm_id and comm_id in efiling and efiling[comm_id]:
            e = efiling[comm_id]
            # Update cash_on_hand if eFiling is fresher
            if e.get("cash_on_hand", 0) > 0:
                c["cash_on_hand"] = e["cash_on_hand"]
            if e.get("total_receipts", 0) > 0:
                c["recent_receipts"] = e["total_receipts"]
            else:
                c["recent_receipts"] = 0
            c["efiling_coverage_end"] = e.get("coverage_end", "")
            c["efiling_filed_date"] = e.get("filed_date", "")
        else:
            c["recent_receipts"] = 0
            c["efiling_coverage_end"] = ""
            c["efiling_filed_date"] = ""

    return candidates


def fetch_presidential_fundraising(election_year: int = 2028) -> list[dict]:
    """Fetch fundraising totals for presidential candidates."""
    data = _fec_get("/candidates/totals/", {
        "office": "P",
        "election_year": election_year,
        "sort": "-receipts",
        "per_page": 50,
        "is_active_candidate": "true",
    })
    candidates = []
    for c in data.get("results", []):
        receipts = float(c.get("receipts") or 0)
        if receipts < 5_000:
            continue
        candidates.append({
            "name": _clean_name(c.get("name", "")),
            "party": _party_code(c.get("party_full", "")),
            "state": c.get("state", ""),
            "receipts": receipts,
            "cash_on_hand": float(c.get("cash_on_hand_end_period") or 0),
            "disbursements": float(c.get("disbursements") or 0),
            "candidate_id": c.get("candidate_id", ""),
        })
    return candidates


def build_fundraising_overlay(senate_candidates: list[dict]) -> dict:
    """Build state-level fundraising summary for overlay on market data.

    Returns dict keyed by state with top R and D candidate fundraising.
    Includes eFiling recent_receipts for near-real-time fundraising signal.
    """
    by_state = {}
    for c in senate_candidates:
        st = c["state"]
        party = c["party"]
        if party not in ("R", "D"):
            continue
        key = (st, party)
        if key not in by_state or c["receipts"] > by_state[key]["receipts"]:
            by_state[key] = c

    # Merge into per-state summary
    states = {}
    for (st, party), c in by_state.items():
        if st not in states:
            states[st] = {"state": st}
        prefix = "dem" if party == "D" else "rep"
        states[st][f"{prefix}_candidate"] = c["name"]
        states[st][f"{prefix}_receipts"] = c["receipts"]
        states[st][f"{prefix}_cash"] = c["cash_on_hand"]
        states[st][f"{prefix}_incumbent"] = c["incumbent"]
        states[st][f"{prefix}_recent_receipts"] = c.get("recent_receipts", 0)
        states[st][f"{prefix}_filed_date"] = c.get("efiling_filed_date", "")
        states[st][f"{prefix}_coverage_end"] = c.get("efiling_coverage_end", "")

    # Compute fundraising advantage
    for st, info in states.items():
        d_cash = info.get("dem_cash", 0)
        r_cash = info.get("rep_cash", 0)
        total = d_cash + r_cash
        if total > 0:
            info["cash_advantage"] = "D" if d_cash > r_cash else "R"
            info["cash_ratio"] = round(max(d_cash, r_cash) / total, 3)
        else:
            info["cash_advantage"] = "?"
            info["cash_ratio"] = 0.5

        # Recent fundraising momentum (eFiling data)
        d_recent = info.get("dem_recent_receipts", 0)
        r_recent = info.get("rep_recent_receipts", 0)
        total_recent = d_recent + r_recent
        if total_recent > 0:
            info["recent_advantage"] = "D" if d_recent > r_recent else "R"
            info["recent_total"] = round(total_recent)
        else:
            info["recent_advantage"] = "?"
            info["recent_total"] = 0

    return states


def compute_money_vs_odds(fundraising: dict, market_races: dict) -> list[dict]:
    """Find divergences between campaign finance and market odds.

    fundraising: state-keyed dict from build_fundraising_overlay()
    market_races: state-keyed dict with r_price/d_price from _dedupe_state_races()

    Returns list of divergence signals sorted by magnitude.
    """
    divergences = []
    for st, fund in fundraising.items():
        mkt = market_races.get(st)
        if not mkt:
            continue

        d_cash = fund.get("dem_cash", 0)
        r_cash = fund.get("rep_cash", 0)
        total_cash = d_cash + r_cash
        if total_cash < 100_000:
            continue  # Not enough data

        # Cash share vs market odds
        d_cash_share = d_cash / total_cash if total_cash else 0.5
        d_mkt_odds = mkt.get("d_price", 0.5)

        # Divergence: money says one thing, markets say another
        divergence = d_cash_share - d_mkt_odds
        if abs(divergence) < 0.10:
            continue  # Less than 10pp divergence isn't notable

        # Determine signal
        if divergence > 0:
            signal = f"D outfunding market odds"
            detail = f"D has {d_cash_share:.0%} of cash but only {d_mkt_odds:.0%} market odds"
        else:
            signal = f"R outfunding market odds"
            r_cash_share = 1 - d_cash_share
            r_mkt_odds = mkt.get("r_price", 0.5)
            detail = f"R has {r_cash_share:.0%} of cash but only {r_mkt_odds:.0%} market odds"

        divergences.append({
            "state": st,
            "divergence_pp": round(abs(divergence) * 100, 1),
            "signal": signal,
            "detail": detail,
            "dem_cash": d_cash,
            "rep_cash": r_cash,
            "dem_candidate": fund.get("dem_candidate", "?"),
            "rep_candidate": fund.get("rep_candidate", "?"),
            "dem_market_odds": round(d_mkt_odds, 3),
            "rep_market_odds": round(mkt.get("r_price", 0.5), 3),
        })

    divergences.sort(key=lambda x: -x["divergence_pp"])
    return divergences


def _clean_name(name: str) -> str:
    """Clean FEC name format (LAST, FIRST MID) to readable."""
    if "," not in name:
        return name.title()
    parts = name.split(",", 1)
    last = parts[0].strip().title()
    first = parts[1].strip().title() if len(parts) > 1 else ""
    # Remove suffixes like 'Dr.', 'Jr', 'Sr', 'Mr.'
    first = first.split(" ")[0] if first else ""
    return f"{first} {last}".strip()


def _party_code(party_full: str) -> str:
    """Convert party name to R/D/I code."""
    p = party_full.lower()
    if "republican" in p:
        return "R"
    if "democratic" in p:
        return "D"
    return "I"


def _fmt_money(amount: float) -> str:
    """Format dollar amount."""
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.0f}K"
    return f"${amount:.0f}"
