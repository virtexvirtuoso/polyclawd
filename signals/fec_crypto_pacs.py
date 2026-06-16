#!/usr/bin/env python3
"""FEC Schedule E independent expenditures by crypto-aligned super PACs.

Tracks cycle-level IE spending by Fairshake and affiliates, plus crypto industry
PACs. Separate from fec_spending.py (which is candidate/state-centric) because
we want committee-centric rollups to answer "how much did the crypto industry
spend on candidates this cycle."
"""

import json
import os
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from loguru import logger

FEC_API = "https://api.open.fec.gov/v1"
FEC_KEY = os.environ.get("FEC_API_KEY", "DEMO_KEY")
CACHE_DIR = Path(__file__).parent.parent / "storage" / "fec_crypto_pacs_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 6 * 3600  # 6 hours

# Crypto-aligned federal committees (verified via FEC /committees/?q= lookup).
# Fairshake + affiliates form the dominant 2024/2026 crypto super PAC network.
CRYPTO_PACS = [
    {"id": "C00835959", "name": "Fairshake",                    "group": "fairshake_network", "type": "super_pac"},
    {"id": "C00836221", "name": "Defend American Jobs",          "group": "fairshake_network", "type": "super_pac"},
    {"id": "C00848440", "name": "Protect Progress",              "group": "fairshake_network", "type": "super_pac"},
    {"id": "C00876631", "name": "Stand With Crypto Alliance PAC","group": "advocacy",          "type": "connected_pac"},
    {"id": "C00804179", "name": "Coinbase Innovation PAC",       "group": "corporate",         "type": "corporate_pac"},
    # C00680355 (Coinbase Inc. PAC) removed — defunct since 2019, superseded by C00804179
    {"id": "C00824896", "name": "Blockchain Association PAC",    "group": "advocacy",          "type": "connected_pac"},
    {"id": "C00942318", "name": "Crypto Council for Innovation PAC", "group": "advocacy",      "type": "connected_pac"},
]

DEFAULT_CYCLE = 2026


def _fec_get(endpoint: str, params: dict | None = None, timeout: int = 20) -> dict:
    """GET request to FEC API with file-based caching."""
    params = dict(params or {})
    params["api_key"] = FEC_KEY
    url = f"{FEC_API}{endpoint}?{urlencode(params)}"

    cache_key = url.replace("/", "_").replace("?", "_").replace("&", "_")[:150]
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
        logger.warning("FEC crypto PAC API error on {}: {}", endpoint, e)
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f)
        return {"results": [], "pagination": {"count": 0, "pages": 1}}


_PARTY_SUFFIX_TOKENS = {"DEM", "REP", "IND", "LIB", "GRN", "DEM.", "REP.", "IND."}
_HONORIFICS = {"HON", "SEN", "REP", "DR", "MR", "MRS", "MS", "REV", "PROF", "JUDGE"}
_NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}


def _clean_name(name: str) -> str:
    """Convert FEC 'LAST, FIRST MIDDLE REP.' → 'First Middle Last'.

    Also strips honorifics ("Hon.", "Sen.", "Rep.") that FEC occasionally
    inserts for state-level elected officials, and moves generational suffixes
    ("Jr.", "III") to the end of the rendered name rather than the middle.
    """
    if not name:
        return ""
    if "," not in name:
        return name.title()
    last, first_field = name.split(",", 1)
    last = last.strip().title()

    raw_tokens = first_field.strip().split()
    first_tokens: list[str] = []
    suffix: str | None = None
    for t in raw_tokens:
        bare = t.upper().strip(".")
        if bare in {"DEM", "REP", "IND", "LIB", "GRN"}:
            # Party marker — but "REP" can collide with "Rep." honorific.
            # Keep dropping (party marker interpretation wins at end of name).
            continue
        if bare in _HONORIFICS:
            continue
        if bare in _NAME_SUFFIXES:
            # Title-case Jr/Sr, keep roman numerals uppercase
            suffix = bare.title() if bare in {"JR", "SR"} else bare
            continue
        first_tokens.append(t)

    first = " ".join(first_tokens).title()
    full = f"{first} {last}".strip()
    if suffix:
        full = f"{full} {suffix}"
    return full


def fetch_pac_ie_spending(committee_id: str, cycle: int = DEFAULT_CYCLE, max_pages: int = 10) -> list[dict]:
    """Fetch all Schedule E expenditures for a single committee in a cycle.

    Paginates until exhausted or max_pages reached. Uses sort=-expenditure_amount
    so biggest-first, letting us bail early if we hit max_pages on a huge list.
    """
    all_results = []
    page = 1
    while page <= max_pages:
        data = _fec_get("/schedules/schedule_e/", {
            "committee_id": committee_id,
            "cycle": cycle,
            "sort": "-expenditure_amount",
            "per_page": 100,
            "page": page,
        })
        results = data.get("results", []) or []
        if not results:
            break
        all_results.extend(results)
        pages = data.get("pagination", {}).get("pages", 1)
        if page >= pages:
            break
        page += 1
    return all_results


def fetch_committee_contributors(committee_id: str, cycle: int = DEFAULT_CYCLE, max_pages: int = 5) -> list[dict]:
    """Fetch Schedule A contributions INTO a committee (who funds the funders).

    Note FEC Schedule A uses `two_year_transaction_period` not `cycle`. We ask
    for the biggest contributions first so max_pages=5 captures the megadonors
    even if the full list is thousands long. Sleeps 1s between pages to stay
    under FEC rate limits.
    """
    all_results: list[dict] = []
    page = 1
    while page <= max_pages:
        data = _fec_get("/schedules/schedule_a/", {
            "committee_id": committee_id,
            "two_year_transaction_period": cycle,
            "sort": "-contribution_receipt_amount",
            "per_page": 100,
            "page": page,
        })
        results = data.get("results", []) or []
        if not results:
            break
        all_results.extend(results)
        pages = data.get("pagination", {}).get("pages", 1)
        if page >= pages:
            break
        page += 1
        time.sleep(1.0)  # avoid FEC rate limiting (1000/hr shared budget)
    return all_results


# Known contributor name variants → canonical display name.
# FEC filings spell the same entity many ways; collapse them.
_CONTRIBUTOR_ALIASES = {
    "A16Z": "Andreessen Horowitz (a16z)",
    "AH CAPITAL MANAGEMENT": "Andreessen Horowitz (a16z)",
    "AH CAPITAL MANAGEMENT, L.L.C.": "Andreessen Horowitz (a16z)",
    "ANDREESSEN HOROWITZ": "Andreessen Horowitz (a16z)",
    "COINBASE": "Coinbase",
    "COINBASE, INC.": "Coinbase",
    "COINBASE INC": "Coinbase",
    "COINBASE INC.": "Coinbase",
    "RIPPLE": "Ripple Labs",
    "RIPPLE LABS": "Ripple Labs",
    "RIPPLE LABS INC.": "Ripple Labs",
    "RIPPLE LABS, INC.": "Ripple Labs",
    "JUMP CRYPTO": "Jump Crypto",
    "JUMP TRADING": "Jump Crypto",
    "KRAKEN": "Kraken",
    "PAYWARD INC.": "Kraken",
    "CIRCLE INTERNET FINANCIAL": "Circle",
    "CIRCLE": "Circle",
}

# Individual donors often appear as "LASTNAME, FIRSTNAME" — titlecase them
# but keep well-known names mapped cleanly.
_INDIVIDUAL_ALIASES = {
    "HOROWITZ, BEN": "Ben Horowitz (a16z)",
    "ANDREESSEN, MARC": "Marc Andreessen (a16z)",
    "WINKLEVOSS, CAMERON": "Cameron Winklevoss (Gemini)",
    "WINKLEVOSS, TYLER": "Tyler Winklevoss (Gemini)",
    "ARMSTRONG, BRIAN": "Brian Armstrong (Coinbase)",
    "ALLAIRE, JEREMY": "Jeremy Allaire (Circle)",
    "EHRSAM, FRED": "Fred Ehrsam (Coinbase/Paradigm)",
}


def _canonicalize_contributor(name: str) -> str:
    """Map a raw FEC contributor_name to a canonical display name."""
    if not name:
        return "(Unknown)"
    name_up = name.strip().upper()
    if name_up in _INDIVIDUAL_ALIASES:
        return _INDIVIDUAL_ALIASES[name_up]
    if name_up in _CONTRIBUTOR_ALIASES:
        return _CONTRIBUTOR_ALIASES[name_up]
    # Partial matches for common entity tokens
    for key, val in _CONTRIBUTOR_ALIASES.items():
        if key in name_up:
            return val
    # Individual name "LAST, FIRST" → "First Last"
    if "," in name and not any(tok in name_up for tok in ("LLC", "INC", "CORP", "LP", "LTD", "FUND", "PAC")):
        parts = [p.strip() for p in name.split(",", 1)]
        if len(parts) == 2:
            return f"{parts[1].title()} {parts[0].title()}"
    return name.title()


def summarize_contributors(raw: list[dict], top_n: int = 15, exclude_committee_ids: set[str] | None = None) -> list[dict]:
    """Dedupe by transaction_id, roll up by canonicalized contributor, return top N.

    exclude_committee_ids: set of FEC committee IDs to filter out as contributors
    (used to strip intra-network transfers, e.g. Fairshake → Defend American Jobs).
    """
    exclude_committee_ids = exclude_committee_ids or set()

    # Dedupe by transaction_id (handles amended filings that re-report the same txn)
    seen_txn: set[str] = set()
    deduped: list[dict] = []
    for r in raw:
        # Drop intra-network transfers
        contrib_cid = (r.get("contributor_id") or "").upper()
        if contrib_cid and contrib_cid in exclude_committee_ids:
            continue
        txn = r.get("transaction_id") or ""
        if txn and txn in seen_txn:
            continue
        if txn:
            seen_txn.add(txn)
        deduped.append(r)

    totals: dict[str, dict] = {}
    for r in deduped:
        name = r.get("contributor_name") or ""
        amt = float(r.get("contribution_receipt_amount") or 0)
        if amt <= 0:
            continue
        canonical = _canonicalize_contributor(name)
        slot = totals.setdefault(canonical, {
            "contributor": canonical,
            "total": 0.0,
            "count": 0,
            "latest_date": "",
            "raw_variants": set(),
        })
        slot["total"] += amt
        slot["count"] += 1
        slot["raw_variants"].add(name)
        date = (r.get("contribution_receipt_date") or "")[:10]
        if date and date > slot["latest_date"]:
            slot["latest_date"] = date

    ranked = sorted(totals.values(), key=lambda x: -x["total"])[:top_n]
    return [
        {
            "contributor": v["contributor"],
            "total": round(v["total"], 2),
            "count": v["count"],
            "latest_date": v["latest_date"],
            "variants": sorted(v["raw_variants"])[:3],
        }
        for v in ranked
    ]


def build_fairshake_funders_overlay(cycle: int = DEFAULT_CYCLE) -> dict:
    """Aggregate top contributors across the Fairshake network (C00835959, C00836221, C00848440).

    Returns a dashboard-friendly summary showing who funds the crypto super PACs.
    """
    fairshake_ids = [p["id"] for p in CRYPTO_PACS if p["group"] == "fairshake_network"]
    exclude_cids = set(fairshake_ids)  # strip intra-network transfers
    all_raw: list[dict] = []
    per_committee: dict[str, float] = {}
    for cid in fairshake_ids:
        try:
            raw = fetch_committee_contributors(cid, cycle=cycle, max_pages=5)
            all_raw.extend(raw)
            per_committee[cid] = sum(
                float(r.get("contribution_receipt_amount") or 0) for r in raw
                if (r.get("contribution_receipt_amount") or 0) > 0
            )
        except Exception as e:
            logger.warning("Fairshake funders fetch for {} failed: {}", cid, e)

    top_funders = summarize_contributors(all_raw, top_n=12, exclude_committee_ids=exclude_cids)
    grand_total = sum(f["total"] for f in top_funders)

    logger.info(
        "Fairshake funders: top {} contributors totaling ${:,.0f} across {} committees",
        len(top_funders), grand_total, len(fairshake_ids),
    )

    return {
        "cycle": cycle,
        "committees_included": fairshake_ids,
        "per_committee_total": {k: round(v, 2) for k, v in per_committee.items()},
        "top_funders": top_funders,
        "top_funders_total": round(grand_total, 2),
    }


def summarize_committee(committee: dict, raw: list[dict]) -> dict:
    """Roll up raw Schedule E rows into a per-committee summary."""
    total_spend = 0.0
    support_spend = 0.0
    oppose_spend = 0.0
    by_candidate: dict[str, dict] = {}
    by_state: dict[str, float] = {}

    for r in raw:
        amount = float(r.get("expenditure_amount") or 0)
        if amount <= 0:
            continue
        # Filter obvious outliers (>$50M single filings are bogus)
        if amount > 50_000_000:
            continue

        total_spend += amount
        sup_opp = (r.get("support_oppose_indicator") or "").upper()
        if sup_opp == "S":
            support_spend += amount
        elif sup_opp == "O":
            oppose_spend += amount

        cand_name = _clean_name(r.get("candidate_name", "") or "")
        cand_id = r.get("candidate_id") or cand_name
        if cand_id:
            slot = by_candidate.setdefault(cand_id, {
                "candidate": cand_name,
                "state": r.get("candidate_office_state", ""),
                "office": r.get("candidate_office", ""),
                "party": (r.get("candidate_party") or "").upper()[:3],
                "support": 0.0,
                "oppose": 0.0,
                "total": 0.0,
            })
            slot["total"] += amount
            if sup_opp == "S":
                slot["support"] += amount
            elif sup_opp == "O":
                slot["oppose"] += amount

        state = r.get("candidate_office_state", "")
        if state:
            by_state[state] = by_state.get(state, 0) + amount

    # Top recipients (by absolute total, support or oppose)
    top_recipients = sorted(
        by_candidate.values(),
        key=lambda x: -x["total"],
    )[:10]

    return {
        "committee_id": committee["id"],
        "committee_name": committee["name"],
        "group": committee["group"],
        "type": committee["type"],
        "total_spend": round(total_spend, 2),
        "support_spend": round(support_spend, 2),
        "oppose_spend": round(oppose_spend, 2),
        "filing_count": len(raw),
        "candidates_touched": len(by_candidate),
        "top_recipients": [
            {
                "candidate": r["candidate"],
                "state": r["state"],
                "office": r["office"],
                "party": r["party"],
                "support": round(r["support"], 2),
                "oppose": round(r["oppose"], 2),
                "total": round(r["total"], 2),
                "net_direction": "support" if r["support"] > r["oppose"] else ("oppose" if r["oppose"] > 0 else "?"),
            }
            for r in top_recipients
        ],
        "top_states": sorted(
            [{"state": s, "amount": round(a, 2)} for s, a in by_state.items()],
            key=lambda x: -x["amount"],
        )[:10],
    }


def build_crypto_pac_overlay(cycle: int = DEFAULT_CYCLE) -> dict:
    """Full overlay: iterate all tracked crypto PACs, return unified summary.

    Safe to call from thread pool — all I/O is sync urllib + file cache.
    """
    committees = []
    grand_total = 0.0
    grand_support = 0.0
    grand_oppose = 0.0

    for pac in CRYPTO_PACS:
        try:
            raw = fetch_pac_ie_spending(pac["id"], cycle=cycle)
            summary = summarize_committee(pac, raw)
        except Exception as e:
            logger.warning("FEC crypto PAC {} failed: {}", pac["name"], e)
            summary = {
                "committee_id": pac["id"],
                "committee_name": pac["name"],
                "group": pac["group"],
                "type": pac["type"],
                "total_spend": 0, "support_spend": 0, "oppose_spend": 0,
                "filing_count": 0, "candidates_touched": 0,
                "top_recipients": [], "top_states": [],
                "error": str(e),
            }
        committees.append(summary)
        grand_total += summary["total_spend"]
        grand_support += summary["support_spend"]
        grand_oppose += summary["oppose_spend"]

    # Sort by total spend desc
    committees.sort(key=lambda c: -c["total_spend"])

    logger.info(
        "FEC crypto PACs: {} committees, ${:,.0f} total IE spend ({} cycle)",
        len(committees), grand_total, cycle,
    )
    return {
        "cycle": cycle,
        "committees": committees,
        "grand_total_spend": round(grand_total, 2),
        "grand_support_spend": round(grand_support, 2),
        "grand_oppose_spend": round(grand_oppose, 2),
        "committee_count": len(committees),
    }


if __name__ == "__main__":
    import sys
    result = build_crypto_pac_overlay()
    json.dump(result, sys.stdout, indent=2)
    print()
