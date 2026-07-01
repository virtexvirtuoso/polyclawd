#!/usr/bin/env python3
"""Senate LDA lobbying filings for crypto industry orgs, filtered by CLARITY Act.

The Senate LDA (Lobbying Disclosure Act) database is the only dataset that
directly ties dollars to specific bills. Unlike FEC data (which tracks candidate
contributions and independent expenditures), LDA quarterly filings list each
bill the client lobbied on, making it possible to answer "how much did the
crypto industry spend lobbying on CLARITY?"

Public JSON API, no auth required.
Docs: https://lda.senate.gov/api/
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from loguru import logger

LDA_API = "https://lda.senate.gov/api/v1"
CACHE_DIR = Path(__file__).parent.parent / "storage" / "lda_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 12 * 3600  # 12 hours — LDA updates on quarterly cadence, no point refreshing fast

# LDA documented rate limits (https://lda.senate.gov/api/):
#   anonymous  → 15 req/min  (≈ 4.0s between requests)
#   registered → 120 req/min (≈ 0.5s between requests)
# We add a small safety margin and coordinate across uvicorn workers via a
# file-based timestamp lock so two processes can't both blow past the limit.
LDA_API_KEY = os.environ.get("LDA_API_KEY", "").strip()
_LDA_MIN_INTERVAL = 4.5 if not LDA_API_KEY else 0.6  # seconds between requests
_LDA_LOCK_FILE = CACHE_DIR / ".rate_lock"
_lda_lock = threading.Lock()  # in-process serialization

# Process-wide overlay cache so multiple workers don't both rebuild on cold start.
# 1h TTL is a separate, shorter-lived layer above the per-request 12h cache.
_OVERLAY_CACHE_FILE = CACHE_DIR / "_overlay_cache.json"
_OVERLAY_CACHE_TTL = 3600  # 1 hour

# Crypto industry orgs that file LDA. Ordered roughly by prominence in CLARITY
# lobbying. Stand With Crypto is deliberately NOT in this list — they're a
# 501(c)(4) and don't file LDA directly (their advocacy is grassroots/education).
# Surfaced separately in the UI as a caveat.
CRYPTO_CLIENTS = [
    "Coinbase",
    "Blockchain Association",
    "Crypto Council for Innovation",
    "Ripple",
    "Kraken",
    "Circle",
    "Chamber of Digital Commerce",
    "Digital Chamber",
    "Paradigm",
    "Andreessen Horowitz",
    "a16z",
    "Consensys",
    "Uniswap",
    "Crypto.com",
    "Gemini Trust",
    "Robinhood",
]

# Regex patterns that signal a filing is lobbying on the CLARITY Act or its
# predecessor/sibling market-structure bills. Case-insensitive match against
# lobbying_activities[].description.
CLARITY_PATTERNS = [
    re.compile(r"\bclarity\s+act\b", re.I),
    re.compile(r"\bdigital\s+asset\s+market\s+clarity\b", re.I),
    re.compile(r"\bh\.?\s*r\.?\s*3633\b", re.I),
    re.compile(r"\bhr\s*3633\b", re.I),
    re.compile(r"\bfit\s*21\b", re.I),
    re.compile(r"\bfinancial\s+innovation\s+and\s+technology\s+for\s+the\s+21st\b", re.I),
    re.compile(r"\bdigital\s+asset\s+market\s+structure\b", re.I),
    re.compile(r"\bcrypto\s+market\s+structure\b", re.I),
]

# Broader pattern to flag filings that touched market structure / clarity concepts
# even without naming the bill explicitly.
CLARITY_SOFT_PATTERNS = [
    re.compile(r"regulatory\s+(structure|clarity)\s+for\s+digital\s+assets", re.I),
    re.compile(r"market\s+structure\s+legislation", re.I),
]

QUARTER_ORDER = {"first_quarter": 1, "second_quarter": 2, "third_quarter": 3, "fourth_quarter": 4, "mid_year": 2, "year_end": 4}


def _acquire_throttle_slot() -> None:
    """Cross-process throttle: ensure at least _LDA_MIN_INTERVAL has elapsed
    since the last LDA request from ANY worker on this host.

    Uses a flock-protected file containing the monotonic-equivalent (wall-clock)
    timestamp of the last request. flock serializes across processes; the
    in-process lock serializes across threads in the same process.
    """
    import fcntl  # POSIX only — VPS is Linux

    _lda_lock.acquire()
    try:
        # Touch the lock file so flock has something to grab.
        if not _LDA_LOCK_FILE.exists():
            _LDA_LOCK_FILE.touch()
        with open(_LDA_LOCK_FILE, "r+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                content = fh.read().strip()
                last = float(content) if content else 0.0
                now = time.time()
                wait = _LDA_MIN_INTERVAL - (now - last)
                if wait > 0:
                    time.sleep(wait)
                    now = time.time()
                fh.seek(0)
                fh.truncate()
                fh.write(f"{now:.6f}")
                fh.flush()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        _lda_lock.release()


def _lda_get(path: str, params: dict) -> dict:
    """GET request to LDA API with file cache, cross-process throttling, and
    429 retry that honors the Retry-After header.
    """
    qs = urllib.parse.urlencode(params)
    url = f"{LDA_API}{path}?{qs}"
    cache_key = url.replace("/", "_").replace("?", "_").replace("&", "_").replace(":", "")[:150]
    cache_path = CACHE_DIR / f"{cache_key}.json"

    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            with open(cache_path) as f:
                return json.load(f)

    headers = {
        "Accept": "application/json",
        "User-Agent": "polyclawd/1.0 (+https://virtuosocrypto.com/polyclawd)",
    }
    if LDA_API_KEY:
        headers["Authorization"] = f"Token {LDA_API_KEY}"

    # Retry on 429 / network errors with exponential backoff (up to 5 tries).
    last_error: Exception | None = None
    for attempt in range(5):
        _acquire_throttle_slot()
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
            with open(cache_path, "w") as f:
                json.dump(data, f)
            return data
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 429:
                # LDA sets Retry-After (seconds). Honor it; fall back to backoff.
                try:
                    retry_after = float(e.headers.get("Retry-After", "") or 0)
                except (TypeError, ValueError):
                    retry_after = 0
                backoff = max(retry_after, 5.0 * (2 ** attempt))  # 5, 10, 20, 40, 80
                if attempt < 4:
                    logger.info("LDA 429, backing off {:.1f}s (attempt {}/5, retry-after={})",
                                backoff, attempt + 1, retry_after)
                    time.sleep(backoff)
                    continue
            elif e.code >= 500 and attempt < 4:
                backoff = 3.0 * (2 ** attempt)
                logger.info("LDA {} on attempt {}/5, backing off {}s", e.code, attempt + 1, backoff)
                time.sleep(backoff)
                continue
            logger.warning("LDA API error on {}: {}", url[:120], e)
            break
        except Exception as e:
            last_error = e
            if attempt < 4:
                time.sleep(2.0 * (2 ** attempt))
                continue
            logger.warning("LDA API error on {}: {}", url[:120], e)
            break

    if cache_path.exists():
        with open(cache_path) as f:
            return json.load(f)
    logger.warning("LDA returning empty for {} (no cache, last_error={})", url[:120], last_error)
    return {"count": 0, "results": []}


def _detect_clarity(description: str) -> tuple[bool, bool]:
    """Return (is_hard_match, is_soft_match) for a lobbying_activities description."""
    if not description:
        return False, False
    if any(p.search(description) for p in CLARITY_PATTERNS):
        return True, True
    if any(p.search(description) for p in CLARITY_SOFT_PATTERNS):
        return False, True
    return False, False


def fetch_client_filings(client_name: str, year: int) -> list[dict]:
    """Fetch all LDA filings for a client in a year."""
    all_results = []
    page = 1
    while page <= 10:  # safety cap
        data = _lda_get("/filings/", {
            "client_name": client_name,
            "filing_year": year,
            "page_size": 25,
            "page": page,
        })
        results = data.get("results", []) or []
        if not results:
            break
        all_results.extend(results)
        if not data.get("next"):
            break
        page += 1
    return all_results


def summarize_filing(filing: dict) -> dict | None:
    """Extract CLARITY-relevant summary from one LDA filing. Returns None if no match."""
    activities = filing.get("lobbying_activities") or []
    hard_hit = False
    soft_hit = False
    matched_descriptions: list[str] = []

    for act in activities:
        desc = act.get("description") or ""
        hard, soft = _detect_clarity(desc)
        if hard or soft:
            if hard:
                hard_hit = True
            else:
                soft_hit = True
            # Trim to keep payload light
            matched_descriptions.append(desc[:400])

    if not (hard_hit or soft_hit):
        return None

    client = filing.get("client") or {}
    registrant = filing.get("registrant") or {}

    # Income = what the registrant (lobby firm) billed the client.
    # Expenses = what the client spent if they self-lobby. One or the other is set.
    income = filing.get("income")
    expenses = filing.get("expenses")
    try:
        income_val = float(income) if income not in (None, "") else 0.0
    except (ValueError, TypeError):
        income_val = 0.0
    try:
        expenses_val = float(expenses) if expenses not in (None, "") else 0.0
    except (ValueError, TypeError):
        expenses_val = 0.0
    amount = income_val or expenses_val

    return {
        "client": client.get("name", ""),
        "registrant": registrant.get("name", ""),
        "filing_uuid": filing.get("filing_uuid", ""),
        "filing_type": filing.get("filing_type_display") or filing.get("filing_type") or "",
        "filing_period": filing.get("filing_period_display") or filing.get("filing_period") or "",
        "filing_year": filing.get("filing_year"),
        "amount": round(amount, 2),
        "income": round(income_val, 2),
        "expenses": round(expenses_val, 2),
        "posted_date": filing.get("dt_posted", "")[:10],
        "clarity_direct_mention": hard_hit,
        "clarity_soft_match": soft_hit and not hard_hit,
        "matched_descriptions": matched_descriptions[:3],
        "url": filing.get("url", ""),
    }


def build_lda_overlay(years: list[int] | None = None) -> dict:
    """Build the CLARITY Act lobbying overlay across all tracked crypto orgs.

    Returns per-org totals, matched filings, quarterly breakdown.

    Cached at the overlay level (1h TTL) so multiple uvicorn workers don't both
    rebuild on cold start. The per-request cache (12h) underneath this still
    handles longer-term reuse.
    """
    years = years or [2025, 2026]

    # Overlay cache check (cross-process via mtime).
    if _OVERLAY_CACHE_FILE.exists():
        try:
            age = time.time() - _OVERLAY_CACHE_FILE.stat().st_mtime
            if age < _OVERLAY_CACHE_TTL:
                with open(_OVERLAY_CACHE_FILE) as f:
                    cached = json.load(f)
                if cached.get("years") == years:
                    logger.debug("LDA overlay cache hit (age={:.0f}s)", age)
                    return cached
        except Exception as e:
            logger.debug("LDA overlay cache read failed: {}", e)

    matched_filings: list[dict] = []
    seen_uuids: set[str] = set()

    for client_name in CRYPTO_CLIENTS:
        for year in years:
            try:
                filings = fetch_client_filings(client_name, year)
            except Exception as e:
                logger.warning("LDA fetch failed for {} {}: {}", client_name, year, e)
                continue
            for f in filings:
                uuid = f.get("filing_uuid", "")
                if uuid in seen_uuids:
                    continue
                summary = summarize_filing(f)
                if summary:
                    seen_uuids.add(uuid)
                    matched_filings.append(summary)

    # Aggregate per-client
    by_client: dict[str, dict] = {}
    total_spend = 0.0
    direct_mention_count = 0

    for f in matched_filings:
        client_norm = (f["client"] or "").strip().upper()
        if not client_norm:
            continue
        slot = by_client.setdefault(client_norm, {
            "client": f["client"],
            "total_spend": 0.0,
            "filing_count": 0,
            "direct_clarity_filings": 0,
            "soft_match_filings": 0,
            "quarters": set(),
            "registrants": set(),
            "latest_filing_date": "",
        })
        slot["total_spend"] += f["amount"]
        slot["filing_count"] += 1
        if f["clarity_direct_mention"]:
            slot["direct_clarity_filings"] += 1
            direct_mention_count += 1
        else:
            slot["soft_match_filings"] += 1
        period = f["filing_period"] or ""
        slot["quarters"].add(f"{f['filing_year']}-{period}")
        if f["registrant"]:
            slot["registrants"].add(f["registrant"])
        if f["posted_date"] > slot["latest_filing_date"]:
            slot["latest_filing_date"] = f["posted_date"]
        total_spend += f["amount"]

    clients_out = []
    for slot in by_client.values():
        clients_out.append({
            "client": slot["client"],
            "total_spend": round(slot["total_spend"], 2),
            "filing_count": slot["filing_count"],
            "direct_clarity_filings": slot["direct_clarity_filings"],
            "soft_match_filings": slot["soft_match_filings"],
            "quarters_covered": sorted(slot["quarters"]),
            "registrants": sorted(slot["registrants"])[:5],
            "latest_filing_date": slot["latest_filing_date"],
        })
    clients_out.sort(key=lambda c: -c["total_spend"])

    # Top matched filings by amount (for detail table)
    top_filings = sorted(
        [f for f in matched_filings if f["amount"] > 0],
        key=lambda f: -f["amount"],
    )[:25]

    logger.info(
        "LDA CLARITY: {} crypto-org filings mention CLARITY/market structure ({} direct, ${:,.0f} total)",
        len(matched_filings), direct_mention_count, total_spend,
    )

    result = {
        "years": years,
        "clients": clients_out,
        "matched_filing_count": len(matched_filings),
        "direct_clarity_mentions": direct_mention_count,
        "total_spend": round(total_spend, 2),
        "top_filings": top_filings,
        "caveats": {
            "stand_with_crypto_note": (
                "Stand With Crypto Alliance is a 501(c)(4) and does not file LDA disclosures directly. "
                "Its advocacy footprint is primarily grassroots mobilization and candidate endorsements, "
                "not federal lobbying reportable to the Senate."
            ),
            "total_caveat": (
                "LDA income/expense fields are filed quarterly totals for all issues combined, not "
                "earmarked to a single bill. A filing that lobbied on CLARITY + 5 other bills counts "
                "its full quarterly amount here — an upper bound on CLARITY-specific spend."
            ),
        },
    }

    # Persist overlay-level cache for sibling workers.
    try:
        with open(_OVERLAY_CACHE_FILE, "w") as f:
            json.dump(result, f)
    except Exception as e:
        logger.debug("LDA overlay cache write failed: {}", e)

    return result


if __name__ == "__main__":
    import sys
    r = build_lda_overlay()
    json.dump(r, sys.stdout, indent=2, default=list)
    print()
