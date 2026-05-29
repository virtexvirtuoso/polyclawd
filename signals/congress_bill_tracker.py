#!/usr/bin/env python3
"""Congressional bill tracker for CLARITY Act and crypto-related legislation.

Uses GovTrack.us (free, no key) which aggregates Congress.gov data. Returns
live status, sponsor, action history, and links for tracked bills so the
dashboard can show legislative momentum alongside market odds.
"""

import json
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from loguru import logger

GOVTRACK_API = "https://www.govtrack.us/api/v2"
CACHE_DIR = Path(__file__).parent.parent / "storage" / "congress_bills_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 6 * 3600  # 6 hours

CURRENT_CONGRESS = 119  # 2025-2026

# Bills to track. Uses exact search strings — GovTrack's relevance ranking
# surfaces them at #1 when the query is specific enough.
TRACKED_BILLS = [
    {
        "key": "clarity_house",
        "label": "CLARITY Act (House)",
        "query": "Digital Asset Market Clarity",
        "bill_type": "house_bill",
        "priority": 1,
    },
    {
        "key": "clarity_senate",
        "label": "Digital Commodity Intermediaries Act (Senate)",
        "query": "Digital Commodity Intermediaries",
        "bill_type": "senate_bill",
        "priority": 2,
    },
]

# Human-readable labels for govtrack status codes
STATUS_LABELS = {
    "introduced": "Introduced",
    "referred": "Referred to Committee",
    "reported": "Reported by Committee",
    "pass_over_house": "Passed House",
    "pass_over_senate": "Passed Senate",
    "passed_bill": "Passed Both Chambers",
    "pass_back_senate": "Returned to Senate",
    "pass_back_house": "Returned to House",
    "enacted_signed": "Enacted — Signed",
    "enacted_veto_override": "Enacted — Veto Override",
    "conference": "Conference Committee",
    "vetoed_pocket": "Pocket Vetoed",
    "vetoed_override_pass_over_house": "Vetoed — House Override",
    "vetoed_override_pass_over_senate": "Vetoed — Senate Override",
    "fail_originating_house": "Failed in House",
    "fail_originating_senate": "Failed in Senate",
    "fail_second_house": "Failed in Second Chamber",
    "fail_second_senate": "Failed in Second Chamber",
    "prov_kill_suspensionfailed": "Failed Suspension",
    "prov_kill_cloturefailed": "Failed Cloture",
    "prov_kill_pingpongfail": "Failed Ping-Pong",
    "prov_kill_veto": "Vetoed",
}


def _govtrack_get(endpoint: str, params: dict | None = None, timeout: int = 15) -> dict:
    """GET request to GovTrack with file-based caching."""
    params = dict(params or {})
    url = f"{GOVTRACK_API}{endpoint}?{urlencode(params)}"

    cache_key = url.replace("/", "_").replace("?", "_").replace("&", "_").replace(":", "_")[:180]
    cache_path = CACHE_DIR / f"{cache_key}.json"
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            try:
                with open(cache_path) as f:
                    return json.load(f)
            except Exception:
                pass

    req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        with open(cache_path, "w") as f:
            json.dump(data, f)
        return data
    except Exception as e:
        logger.warning("GovTrack API error on {}: {}", endpoint, e)
        if cache_path.exists():
            try:
                with open(cache_path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"objects": [], "meta": {"total_count": 0}}


def _summarize_bill(obj: dict, label: str) -> dict:
    """Flatten one GovTrack bill object into a dashboard-friendly summary."""
    sponsor = obj.get("sponsor") or {}
    sponsor_display = sponsor.get("name") or ""
    # Sponsor comes as "Sen. John Boozman [R-AR]" — already display-ready

    status_code = obj.get("current_status") or "introduced"
    status_label = STATUS_LABELS.get(status_code, status_code.replace("_", " ").title())

    # Cosponsor count isn't in the list endpoint but link is — detail fetch if needed
    cosponsors_count = obj.get("cosponsors_count")

    return {
        "label": label,
        "display_number": obj.get("display_number") or "",
        "title": obj.get("title") or "",
        "congress": obj.get("congress"),
        "introduced_date": obj.get("introduced_date"),
        "current_status": status_code,
        "current_status_label": status_label,
        "current_status_date": obj.get("current_status_date"),
        "sponsor": sponsor_display,
        "sponsor_party": (sponsor.get("name") or "").split("[")[-1].rstrip("]")[:1] if "[" in (sponsor.get("name") or "") else "",
        "link": obj.get("link") or "",
        "cosponsors_count": cosponsors_count,
    }


def _fetch_cosponsor_count(bill_link: str) -> int | None:
    """Fetch cosponsor count from GovTrack bill detail page.

    GovTrack's list endpoint omits cosponsor counts; the detail endpoint has them.
    """
    if not bill_link:
        return None
    # Extract numeric bill id from link like ".../congress/bills/119/hr3633"
    # GovTrack bill detail JSON: append ?format=json isn't reliable; use API by searching
    # Actually the API supports fetching by display number directly.
    return None  # leave as None for now; list endpoint is sufficient for v1


def fetch_tracked_bill(query: str, bill_type: str | None = None, congress: int = CURRENT_CONGRESS) -> dict | None:
    """Search GovTrack for a tracked bill, return the most recent match."""
    params = {
        "q": query,
        "congress": congress,
        "sort": "-current_status_date",
        "limit": 10,
    }
    if bill_type:
        params["bill_type"] = bill_type

    data = _govtrack_get("/bill/", params)
    objects = data.get("objects") or []
    if not objects:
        return None

    # GovTrack relevance isn't perfect — prefer the one whose title contains
    # the query verbatim, fallback to the first result.
    q_lower = query.lower()
    for obj in objects:
        title = (obj.get("title") or "").lower()
        if q_lower in title:
            return obj
    return objects[0]


def build_clarity_bills_overlay(congress: int = CURRENT_CONGRESS) -> dict:
    """Build the dashboard overlay of tracked crypto bills.

    Returns a dict suitable for injection into the clarity signal payload:
        {
          "congress": 119,
          "bills": [ { label, display_number, title, current_status_label, sponsor, link, ... } ],
          "last_updated": iso-timestamp,
        }
    """
    bills_out: list[dict] = []
    for spec in TRACKED_BILLS:
        try:
            obj = fetch_tracked_bill(spec["query"], spec.get("bill_type"), congress)
            if obj is None:
                bills_out.append({
                    "label": spec["label"],
                    "display_number": "",
                    "title": "",
                    "current_status": "not_found",
                    "current_status_label": "Not Found",
                    "error": "no matching bill",
                })
                continue
            summary = _summarize_bill(obj, spec["label"])
            summary["priority"] = spec.get("priority", 99)
            summary["key"] = spec["key"]
            bills_out.append(summary)
        except Exception as e:
            logger.warning("Congress bill fetch failed for {}: {}", spec["key"], e)
            bills_out.append({
                "label": spec["label"],
                "display_number": "",
                "current_status": "error",
                "current_status_label": "Error",
                "error": str(e),
            })

    bills_out.sort(key=lambda b: b.get("priority", 99))

    logger.info("Congress bill tracker: {} bills fetched for {}th Congress", len(bills_out), congress)

    return {
        "congress": congress,
        "bills": bills_out,
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


if __name__ == "__main__":
    import sys
    result = build_clarity_bills_overlay()
    json.dump(result, sys.stdout, indent=2)
    print()
