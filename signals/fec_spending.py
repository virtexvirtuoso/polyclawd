#!/usr/bin/env python3
"""FEC Independent Expenditure (Schedule E) client — 24-hour spending surge alerts."""

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
CACHE_DIR = Path(__file__).parent.parent / "storage" / "fec_spending_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 3600  # 1 hour

# Map FEC office codes to readable names
OFFICE_MAP = {"S": "senate", "H": "house", "P": "presidential"}


def _fec_get(endpoint: str, params: dict = None, timeout: int = 15) -> dict:
    """GET request to FEC API with file-based caching."""
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
        logger.warning("FEC API error on {}: {}", endpoint, e)
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f)
        return {"results": [], "pagination": {"count": 0}}


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


def _party_from_candidate(candidate_name: str, party_field: str = "") -> str:
    """Infer party code from explicit field or name context."""
    if party_field:
        p = party_field.upper()
        if "DEM" in p:
            return "D"
        if "REP" in p:
            return "R"
    return "?"


def fetch_recent_ie_spending(days: int = 7) -> list[dict]:
    """Fetch recent independent expenditures (24/48-hour filings).

    These are the mandatory filings made within 24 or 48 hours of spending,
    representing the most time-sensitive campaign finance data available.
    """
    min_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    all_results = []
    page = 1

    while True:
        params = {
            "cycle": 2026,
            "sort": "-expenditure_date",
            "min_date": min_date,
            "per_page": 100,
            "page": page,
        }
        # Try 24/48-hour notice filings first; fall back to all IE filings
        # (notices only appear near elections, regular filings exist year-round)
        data = _fec_get("/schedules/schedule_e/", {**params, "is_notice": "true"})
        if not data.get("results") and page == 1:
            data = _fec_get("/schedules/schedule_e/", params)
        results = data.get("results", [])
        if not results:
            break

        for r in results:
            amount = float(r.get("expenditure_amount") or 0)
            if amount <= 0:
                continue
            # Filter obvious data entry errors (single filings >$50M are bogus)
            if amount > 50_000_000:
                logger.debug("FEC IE: skipping outlier filing ${:,.0f} from {}", amount,
                            r.get("committee", {}).get("name", ""))
                continue

            sup_opp = r.get("support_oppose_indicator", "")
            if sup_opp == "S":
                support_oppose = "support"
            elif sup_opp == "O":
                support_oppose = "oppose"
            else:
                support_oppose = sup_opp.lower() if sup_opp else "unknown"

            candidate_office = r.get("candidate_office", "")
            all_results.append({
                "committee_name": r.get("committee", {}).get("name", "") or r.get("committee_name", ""),
                "candidate_name": _clean_name(r.get("candidate_name", "")),
                "candidate_state": r.get("candidate_office_state", ""),
                "candidate_office": candidate_office,
                "candidate_party": _party_from_candidate(
                    r.get("candidate_name", ""),
                    r.get("candidate_party", ""),
                ),
                "support_oppose": support_oppose,
                "amount": amount,
                "date": r.get("expenditure_date", ""),
            })

        pages = data.get("pagination", {}).get("pages", 1)
        if page >= pages:
            break
        page += 1

    logger.info("FEC IE: fetched {} recent expenditures ({}d window)", len(all_results), days)
    return all_results


def aggregate_ie_by_race(spending: list[dict]) -> dict:
    """Group independent expenditures by race (state + office).

    Returns dict keyed by "{STATE}_{OFFICE}" with support/oppose totals per party,
    plus net advantage indicator.
    """
    races = {}

    for s in spending:
        state = s.get("candidate_state", "")
        office_code = s.get("candidate_office", "")
        if not state or not office_code:
            continue

        office = OFFICE_MAP.get(office_code, office_code.lower())
        key = f"{state}_{office_code}"
        if key not in races:
            races[key] = {
                "state": state,
                "office": office,
                "dem_support": 0,
                "dem_oppose": 0,
                "rep_support": 0,
                "rep_oppose": 0,
                "total": 0,
            }

        party = s.get("candidate_party", "?")
        action = s.get("support_oppose", "")
        amount = s.get("amount", 0)
        races[key]["total"] += amount

        # IE spending supporting D candidate or opposing R candidate = pro-D money
        if party == "D" and action == "support":
            races[key]["dem_support"] += amount
        elif party == "D" and action == "oppose":
            # Spending to oppose a D candidate = pro-R money
            races[key]["rep_oppose"] += amount
        elif party == "R" and action == "support":
            races[key]["rep_support"] += amount
        elif party == "R" and action == "oppose":
            # Spending to oppose an R candidate = pro-D money
            races[key]["dem_oppose"] += amount

    # Compute net advantage
    for key, race in races.items():
        # Pro-D total: support D + oppose R
        pro_d = race["dem_support"] + race.get("dem_oppose", 0)
        # Wait — dem_oppose here means "spending opposing D", which is pro-R.
        # Let me reclarify: the fields track spending BY party alignment.
        # dem_support = money supporting D candidates
        # dem_oppose = money opposing D candidates (actually tracked as rep_oppose above)
        # The way we assigned above:
        #   oppose D → rep_oppose (correctly)
        #   oppose R → dem_oppose (correctly)
        # So pro-D = dem_support + dem_oppose (oppose R = helps D)
        # And pro-R = rep_support + rep_oppose (oppose D = helps R)
        pro_d = race["dem_support"] + race["dem_oppose"]
        pro_r = race["rep_support"] + race["rep_oppose"]

        if pro_d > pro_r:
            race["net_advantage"] = "D"
        elif pro_r > pro_d:
            race["net_advantage"] = "R"
        else:
            race["net_advantage"] = "?"

    logger.info("FEC IE: aggregated {} races", len(races))
    return races


def detect_spending_surges(spending: list[dict], threshold: float = 500_000) -> list[dict]:
    """Find single IE filings exceeding threshold amount.

    These are "smart money just dropped $5M on this race" alerts — large single
    expenditures that may signal a shift in campaign dynamics.
    """
    surges = []
    for s in spending:
        amount = s.get("amount", 0)
        if amount < threshold:
            continue

        office = OFFICE_MAP.get(s.get("candidate_office", ""), s.get("candidate_office", ""))
        surges.append({
            "committee": s.get("committee_name", ""),
            "candidate": s.get("candidate_name", ""),
            "state": s.get("candidate_state", ""),
            "office": office,
            "party": s.get("candidate_party", "?"),
            "support_oppose": s.get("support_oppose", ""),
            "amount": amount,
            "date": s.get("date", ""),
        })

    surges.sort(key=lambda x: -x["amount"])
    logger.info("FEC IE: detected {} spending surges (>${})", len(surges), f"{threshold:,.0f}")
    return surges
