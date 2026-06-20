#!/usr/bin/env python3
"""Election Sentiment Tracker — fetches US election markets from Polymarket + Kalshi."""

import json
import re
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from loguru import logger

GAMMA_API = "https://gamma-api.polymarket.com"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

SNAPSHOT_DIR = Path(__file__).parent.parent / "storage" / "election_snapshots"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

# Polymarket tag slugs that contain US election markets
POLY_TAG_SLUGS = [
    "us-presidential-election", "elections", "senate-midterms", "midterms",
    "us-house", "house-races", "congressional-races",
]

# Additional candidate slugs for dynamic tag discovery
_CANDIDATE_TAG_SLUGS = [
    "2026-elections", "2028-elections", "us-elections",
    "governor-races", "us-senate", "us-governor",
    "midterms-2026", "senate-races",
]
_tag_discovery_cache = {"tags": [], "ts": 0}
_TAG_DISCOVERY_TTL = 3600 * 6  # 6 hours
_DISCOVERY_CACHE_DIR = Path(__file__).parent.parent / "storage"

# Kalshi ticker prefixes for US election races
KALSHI_ELECTION_PREFIXES = (
    "SENATE", "GOVPARTY", "KXPRES", "KXPRESPERSON", "POWER",
    "KXNEXTSPEAKER", "KXAMEND",
    # House district races
    "REPHOUSE", "KXHOUSE", "HOUSEPARTY",
    # Midterm-adjacent: state-level auxiliary races (AG/SoS/LtGov)
    "KXATTYGEN", "KXSECSTATE", "KXLTGOV",
    # Leading indicators: retirements, early departures, Musk primaries
    "KXMUSKPRIMARY", "KXMUSKCHALLENGERS",
    "KXDEMSEEKREELECTION", "KXLEAVE", "KXHOUSETURNOUT",
    # State-level House seat counts + statewide partisan sweeps
    "KXCAHOUSEDEM", "KXVAHOUSEDEM", "KXANYDEMWIN",
)

# Policy market tag slugs (Polymarket) — base set always fetched
POLY_POLICY_TAG_SLUGS = [
    "congress", "supreme-court", "tariffs", "trade-war",
    # Domestic policy
    "immigration", "abortion", "climate", "tiktok", "big-tech",
    # Foreign policy
    "ukraine", "israel", "iran", "nato", "foreign-policy",
]

# Keywords for filtering Polymarket /tags endpoint (auto-discovery)
_POLY_POLICY_TAG_KEYWORDS = frozenset({
    # SCOTUS / Congress / Government
    "congress", "supreme", "court", "scotus", "impeach", "shutdown", "legislation",
    "government", "governor", "judicial", "senate", "speaker",
    # Trade / Tariff
    "tariff", "trade", "import", "export", "custom",
    # Foreign policy
    "ukraine", "israel", "iran", "nato", "sanction", "military", "nuclear",
    "missile", "foreign", "war", "ceasefire", "troops",
    # Domestic policy
    "immigr", "deportat", "abortion", "climate", "tiktok", "big-tech",
    "regulat", "crypto", "gun", "health", "energy", "antitrust", "border",
    "asylum", "ai-", "doj", "fbi", "attorney",
    # Macro / economic
    "fed", "macro", "inflation", "cpi", "gdp", "recession", "unemploy",
    "budget", "debt", "deficit", "bitcoin", "fiscal", "monetary",
    "interest-rate", "nonfarm", "jobs",
    # v3 additions — 2026 Trump-era gaps
    "h1b", "visa", "doge", "pardon", "redistrict", "infrastructure",
    "student-debt", "executive-order", "jobless", "administration",
    "voting", "census",
    # v3.1 — policy tags confirmed on Polymarket
    "trump", "vance", "zelensk", "swing", "mayor", "special-election",
    "bannon", "taliban", "testimony", "declassif", "boj", "rba",
    "cdc", "tsa", "oil", "commodity", "prison", "vatican",
})

_policy_tag_cache = {"tags": [], "ts": 0}
_kalshi_policy_cache = {"series": [], "ts": 0}
_POLICY_TAG_TTL = 3600  # 1 hour (was 6h)

KALSHI_POLICY_SERIES = [
    # SCOTUS
    "KXSCOURT", "KXSCOTUSRESIGN", "KXSCOTUSPOWER", "KXSCOTUSCHANGE",
    "KXNEWSCOTUSCONF", "KXOBERGEFELL", "KXBANTRANS", "KXTRUMPVSLAUGHTER",
    # Congress
    "KXIMPEACH", "KXIMPEACHCABINET", "KXVETOOVERRIDE", "KXNEXTSPEAKER",
    "KXNUMSHUTDOWNS", "KXCONSTAMEND", "KXAMEND25", "KXTERMLIMITS",
    # Trade/Tariffs
    "KXFTA", "KXFTAPRC", "KXTARIFFREVENUE", "KXTRADEDEFICIT", "KXCNIMPORT",
    # Foreign Policy
    "KXSANCTIONRUSSIA", "KXUSAIRANAGREEMENT", "KXTRUMPIRAN", "KXELECTIRAN",
    "KXNEXTISRAELPM", "KXISRAELKNESSET", "KXPRESTAIWAN", "KXTAIWANLVL4",
    # Domestic Policy
    "KXDEPORTATIONS", "KXUSCLIMATE", "KXAILEGISLATION",
]

# US state abbreviations for state extraction
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}

# Keywords that indicate non-US elections (filter out)
NON_US_KEYWORDS = [
    "uk ", "united kingdom", "canada", "alberta", "israel", "eu ",
    "european", "spain", "pope", "xi jinping", "prime minister",
    "brexit", "referendum", "tate's party",
]


def _kalshi_auth_headers() -> dict:
    """Get auth headers for Kalshi API calls, importing from kalshi_edge if available."""
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from odds.kalshi_edge import _get_auth_headers
        return _get_auth_headers()
    except Exception:
        return {"User-Agent": "polyclawd/1.0"}


def _api_get(url: str, params: dict = None, timeout: int = 30) -> any:
    """Simple GET request returning parsed JSON. Injects Kalshi auth when needed."""
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    headers = {"User-Agent": "polyclawd/1.0"}
    if KALSHI_API in url:
        headers.update(_kalshi_auth_headers())
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _is_us_election(title: str) -> bool:
    """Filter for US-specific election markets."""
    t = title.lower()
    for kw in NON_US_KEYWORDS:
        if kw in t:
            return False
    # Must contain election-related keywords
    us_keywords = [
        "president", "senate", "house", "governor", "congress",
        "midterm", "primary", "nominee", "electoral", "party will win",
        "balance of power", "control", "2026", "2028", "democrat",
        "republican", "gop", "district",
        # State-level executive + auxiliary races
        "attorney general", "secretary of state", "lieutenant governor",
    ]
    return any(kw in t for kw in us_keywords)


def classify_race(title: str, ticker: str = "") -> str:
    """Classify an election market into race category."""
    t = title.lower()
    tk = ticker.upper()

    # Primary/nominee MUST be checked first — "Maine Democratic Senate nominee"
    # contains "senate" but is a primary market, not a general election market
    is_nominee = "nominee" in t or "primary" in t or "nomination" in t or "drop out" in t
    is_margin = "margin of victory" in t or "margin of" in t
    is_exact_outcome = "exact outcome" in t

    if is_nominee or is_margin or is_exact_outcome:
        return "primary"

    if tk.startswith("SENATE") or "senate" in t:
        return "senate"
    if tk.startswith("GOVPARTY") or ("governor" in t and "fed " not in t and "federal" not in t):
        return "governor"

    # Individual House district races (e.g., REPHOUSECA45-26, "CA-45 district")
    district_match = re.search(r'\b[A-Z]{2}-\d{1,2}\b', t.upper())
    if tk.startswith(("REPHOUSE", "KXHOUSE", "HOUSEPARTY")) or district_match or "district" in t:
        if "senate" not in t and "governor" not in t:
            return "house"

    if tk.startswith(("KXPRES", "POWER")) or "president" in t or "presidency" in t:
        return "presidential"
    if "house" in t or "speaker" in t or "congress" in t:
        return "house"
    return "other"


def _extract_state(title: str, ticker: str = "") -> str:
    """Extract US state abbreviation from title or ticker."""
    # Kalshi tickers: SENATEGA-28 → GA, GOVPARTYIN-28 → IN, REPHOUSECA45-26 → CA
    for prefix in ("SENATE", "GOVPARTY", "REPHOUSE", "KXHOUSE", "HOUSEPARTY"):
        if ticker.startswith(prefix):
            rest = ticker[len(prefix):].split("-")[0].upper()
            # For house tickers, state is first 2 chars (rest may include district num)
            state = rest[:2] if len(rest) >= 2 else rest
            if state in US_STATES:
                return state

    # Title patterns: "Georgia Senate", "New Hampshire Governor"
    # IMPORTANT: compound names (west virginia, north carolina, etc.) must be checked
    # before their simple counterparts (virginia, carolina) to avoid substring mismatches
    state_names = [
        ("west virginia", "WV"), ("north carolina", "NC"), ("south carolina", "SC"),
        ("north dakota", "ND"), ("south dakota", "SD"), ("new hampshire", "NH"),
        ("new jersey", "NJ"), ("new mexico", "NM"), ("new york", "NY"),
        ("rhode island", "RI"),
        ("alabama", "AL"), ("alaska", "AK"), ("arizona", "AZ"), ("arkansas", "AR"),
        ("california", "CA"), ("colorado", "CO"), ("connecticut", "CT"), ("delaware", "DE"),
        ("florida", "FL"), ("georgia", "GA"), ("hawaii", "HI"), ("idaho", "ID"),
        ("illinois", "IL"), ("indiana", "IN"), ("iowa", "IA"), ("kansas", "KS"),
        ("kentucky", "KY"), ("louisiana", "LA"), ("maine", "ME"), ("maryland", "MD"),
        ("massachusetts", "MA"), ("michigan", "MI"), ("minnesota", "MN"),
        ("mississippi", "MS"), ("missouri", "MO"), ("montana", "MT"), ("nebraska", "NE"),
        ("nevada", "NV"), ("ohio", "OH"), ("oklahoma", "OK"), ("oregon", "OR"),
        ("pennsylvania", "PA"), ("tennessee", "TN"), ("texas", "TX"), ("utah", "UT"),
        ("vermont", "VT"), ("virginia", "VA"), ("washington", "WA"),
        ("wisconsin", "WI"), ("wyoming", "WY"),
    ]
    t = title.lower()
    for name, abbr in state_names:
        if name in t:
            return abbr
    return ""


def _extract_district(title: str, ticker: str = "") -> str:
    """Extract congressional district from title or ticker (e.g., 'CA-45')."""
    # Kalshi tickers: REPHOUSECA45-26 → CA-45
    for prefix in ("REPHOUSE", "KXHOUSE", "HOUSEPARTY"):
        if ticker.startswith(prefix):
            rest = ticker[len(prefix):]
            match = re.match(r"([A-Z]{2})(\d{1,2})", rest)
            if match:
                return f"{match.group(1)}-{match.group(2)}"
    # Title: "CA-45", "California's 45th District"
    m = re.search(r'\b([A-Z]{2})-(\d{1,2})\b', title.upper())
    if m and m.group(1) in US_STATES:
        return f"{m.group(1)}-{m.group(2)}"
    # Ordinal pattern: "Texas 23rd district", "California's 45th congressional district"
    m = re.search(r'(\w+)(?:\'s)?\s+(\d{1,2})(?:th|st|nd|rd)\s+(?:congressional\s+)?district', title, re.I)
    if m:
        state = _extract_state(m.group(1))
        if state:
            return f"{state}-{m.group(2)}"
    return ""


def _discover_poly_election_tags() -> list[str]:
    """Discover election-related tag slugs from Polymarket by probing candidates."""
    import time as _time
    now = _time.time()
    if _tag_discovery_cache["tags"] and (now - _tag_discovery_cache["ts"]) < _TAG_DISCOVERY_TTL:
        return _tag_discovery_cache["tags"]

    known = set(POLY_TAG_SLUGS)

    # Load previously discovered tags from disk
    cache_file = _DISCOVERY_CACHE_DIR / "election_tag_cache.json"
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            known.update(cached.get("tags", []))
        except Exception:
            pass

    for slug in _CANDIDATE_TAG_SLUGS:
        if slug in known:
            continue
        try:
            events = _api_get(f"{GAMMA_API}/events", {
                "tag_slug": slug, "limit": 1, "active": "true",
            }, timeout=10)
            if isinstance(events, list) and events:
                known.add(slug)
                logger.info("Discovered new Polymarket election tag: {}", slug)
        except Exception:
            pass

    result = sorted(known)
    _tag_discovery_cache["tags"] = result
    _tag_discovery_cache["ts"] = now

    # Persist to disk for restart survival
    try:
        new_tags = sorted(known - set(POLY_TAG_SLUGS))
        if new_tags:
            cache_file.write_text(json.dumps({"tags": new_tags, "ts": now}))
    except Exception:
        pass

    return result


def _parse_outcomes(market: dict) -> list[dict]:
    """Parse outcomes from either Polymarket or Kalshi market data."""
    outcomes = []
    # Polymarket format
    if "outcomes" in market and "outcomePrices" in market:
        names = market.get("outcomes", [])
        if isinstance(names, str):
            try:
                names = json.loads(names)
            except (json.JSONDecodeError, TypeError):
                names = []
        prices = market.get("outcomePrices", [])
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except (json.JSONDecodeError, TypeError):
                prices = []
        for i, name in enumerate(names):
            price = float(prices[i]) if i < len(prices) else 0
            outcomes.append({"name": str(name), "price": round(price, 4)})
    # Kalshi format (v2 API uses _dollars suffix fields)
    elif "last_price_dollars" in market or "yes_bid_dollars" in market or "yes_bid" in market:
        yes_price = (
            market.get("last_price_dollars")
            or market.get("yes_bid_dollars")
            or market.get("yes_bid")
            or market.get("last_price")
            or 0
        )
        if isinstance(yes_price, str):
            yes_price = float(yes_price)
        # Kalshi v2 prices are already decimal (0-1)
        if yes_price > 1:
            yes_price = yes_price / 100
        # Use yes_sub_title as candidate/outcome name if available
        yes_name = market.get("yes_sub_title") or "Yes"
        no_name = market.get("no_sub_title") or "No"
        # Fix: when both names are identical (e.g. both "Democratic party"),
        # the no outcome should be the opposite or "No"
        if yes_name == no_name and yes_name != "Yes":
            no_name = "No"
        outcomes.append({"name": yes_name, "price": round(yes_price, 4)})
        outcomes.append({"name": no_name, "price": round(1 - yes_price, 4)})
    return outcomes


# ── Polymarket Fetcher ────────────────────────────────────────────────────

def fetch_polymarket_elections() -> list[dict]:
    """Fetch US election markets from Polymarket Gamma API."""
    seen_ids = set()
    markets = []

    # Use dynamic tag discovery (falls back to POLY_TAG_SLUGS if discovery fails)
    try:
        tag_slugs = _discover_poly_election_tags()
    except Exception:
        tag_slugs = POLY_TAG_SLUGS

    for tag_slug in tag_slugs:
        try:
            # limit=500 because the `midterms` tag alone has 500+ events
            # (all the individual House district winner markets). Dropping
            # to 50 silently hid ~400 markets including balance-of-power.
            events = _api_get(f"{GAMMA_API}/events", {
                "tag_slug": tag_slug,
                "limit": 500,
                "active": "true",
                "closed": "false",
            })
            if not isinstance(events, list):
                continue

            for event in events:
                title = event.get("title", "")
                if not _is_us_election(title):
                    continue

                slug = event.get("slug", "")
                for m in event.get("markets", []):
                    mid = m.get("id") or m.get("condition_id", "")
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)

                    question = m.get("question", title)
                    outcomes = _parse_outcomes(m)
                    # Skip markets with no meaningful prices
                    if not outcomes or all(o["price"] < 0.005 for o in outcomes):
                        continue

                    vol = m.get("volume", 0)
                    if isinstance(vol, str):
                        try:
                            vol = float(vol)
                        except (ValueError, TypeError):
                            vol = 0

                    race_cat = classify_race(question)
                    state = _extract_state(question)
                    mkt = {
                        "id": mid,
                        "platform": "polymarket",
                        "question": question,
                        "slug": slug,
                        "event_title": title,
                        "race_category": race_cat,
                        "state": state,
                        "outcomes": outcomes,
                        "volume": round(vol, 2),
                        "liquidity": float(m.get("liquidity", 0) or 0),
                        "end_date": m.get("endDate") or m.get("end_date_iso", ""),
                    }
                    dist = _extract_district(question)
                    if dist:
                        mkt["district"] = dist
                        if not state:
                            mkt["state"] = dist.split("-")[0]
                    markets.append(mkt)

        except Exception as e:
            logger.warning("Polymarket election fetch failed (tag={}): {}", tag_slug, e)

    logger.info("Polymarket elections: {} markets from {} tag queries", len(markets), len(tag_slugs))
    return markets


# ── Kalshi Dynamic Ticker Discovery ──────────────────────────────────────

_kalshi_ticker_cache = {"prefixes": set(), "ts": 0}
_KALSHI_DISCOVERY_TTL = 3600 * 6  # 6 hours


def _get_kalshi_election_prefixes() -> tuple:
    """Return known + dynamically discovered Kalshi election ticker prefixes."""
    import time as _time
    now = _time.time()

    # Load from disk if memory cache is empty
    if not _kalshi_ticker_cache["prefixes"]:
        cache_file = _DISCOVERY_CACHE_DIR / "election_ticker_cache.json"
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text())
                _kalshi_ticker_cache["prefixes"] = set(cached.get("prefixes", []))
                _kalshi_ticker_cache["ts"] = cached.get("ts", 0)
            except Exception:
                pass

    if _kalshi_ticker_cache["prefixes"]:
        return tuple(KALSHI_ELECTION_PREFIXES) + tuple(_kalshi_ticker_cache["prefixes"])
    return KALSHI_ELECTION_PREFIXES


def _learn_kalshi_ticker(event_ticker: str, title: str):
    """Learn new ticker prefixes from Kalshi events we successfully process."""
    if not event_ticker or not _is_us_election(title):
        return
    # Extract prefix: strip trailing -{year} suffix, then state+district digits
    # SENATEGA-28 → SENATE, REPHOUSECA45-26 → REPHOUSE, KXPRESPARTY-2028 → KXPRESPARTY
    prefix = re.sub(r'-\d+$', '', event_ticker)  # strip -28, -26, -2028
    # Strip trailing state abbreviation + optional digits (GA, CA45) only if it's a real state
    m = re.search(r'([A-Z]{2})(\d*)$', prefix)
    if m and m.group(1) in US_STATES:
        prefix = prefix[:m.start()]
    if not prefix or len(prefix) < 3:
        # Fallback: just strip numbers from end
        prefix = re.sub(r'\d+$', '', re.sub(r'-\d+$', '', event_ticker))
    if not prefix or len(prefix) < 3:
        return
    # Skip if already in known set
    if any(prefix == p or prefix.startswith(p) or p.startswith(prefix) for p in KALSHI_ELECTION_PREFIXES):
        return
    if prefix not in _kalshi_ticker_cache["prefixes"]:
        _kalshi_ticker_cache["prefixes"].add(prefix)
        import time as _time
        _kalshi_ticker_cache["ts"] = _time.time()
        logger.info("Discovered new Kalshi election ticker prefix: {} (from {})", prefix, event_ticker)
        # Persist to disk
        try:
            cache_file = _DISCOVERY_CACHE_DIR / "election_ticker_cache.json"
            cache_file.write_text(json.dumps({
                "prefixes": sorted(_kalshi_ticker_cache["prefixes"]),
                "ts": _kalshi_ticker_cache["ts"],
            }))
        except Exception:
            pass


# ── Kalshi Fetcher ────────────────────────────────────────────────────────

def fetch_kalshi_elections() -> list[dict]:
    """Fetch US election markets from Kalshi API."""
    markets = []
    cursor = None
    active_prefixes = _get_kalshi_election_prefixes()

    try:
        while True:
            params = {"status": "open", "limit": 200, "with_nested_markets": "true"}
            if cursor:
                params["cursor"] = cursor

            data = _api_get(f"{KALSHI_API}/events", params)
            events = data.get("events", [])

            for event in events:
                cat = event.get("category", "")
                if cat not in ("Elections", "Politics"):
                    continue

                event_ticker = event.get("event_ticker", "")
                title = event.get("title", "")

                if not _is_us_election(title):
                    continue

                # Check ticker prefix for known election series
                is_election_series = any(
                    event_ticker.startswith(p) for p in active_prefixes
                )
                if not is_election_series and cat != "Elections":
                    continue

                # Learn new prefixes from events in the Elections category
                if cat == "Elections":
                    _learn_kalshi_ticker(event_ticker, title)

                for m in event.get("markets", []):
                    ticker = m.get("ticker", "")
                    subtitle = m.get("subtitle") or m.get("title") or title
                    # Clean up question: avoid redundant "title: subtitle" when subtitle
                    # repeats title info or has garbled formatting (:: prefixes, "Will  become")
                    if subtitle == title or not subtitle.strip():
                        question = title
                    elif subtitle.startswith("::"):
                        # Kalshi uses ":: D-House, D-Senate" for combo outcomes
                        question = f"{title} — {subtitle.lstrip(': ').strip()}"
                    elif title.endswith("?") and subtitle[0:1].isupper() and any(subtitle.lower().startswith(w) for w in ("will ", "who ", "what ", "which ")):
                        # Subtitle is a full question — use it directly, it's more specific
                        question = subtitle
                    else:
                        question = f"{title}: {subtitle}"

                    outcomes = _parse_outcomes(m)
                    if not outcomes:
                        continue

                    vol = m.get("volume_fp") or m.get("volume", 0)
                    if isinstance(vol, str):
                        try:
                            vol = float(vol)
                        except (ValueError, TypeError):
                            vol = 0

                    race_cat = classify_race(title, event_ticker)
                    state = _extract_state(title, event_ticker)
                    mkt = {
                        "id": ticker or event_ticker,
                        "platform": "kalshi",
                        "question": question,
                        "slug": "",
                        "event_title": title,
                        "race_category": race_cat,
                        "state": state,
                        "outcomes": outcomes,
                        "volume": vol,
                        "liquidity": 0,
                        "end_date": m.get("close_time") or m.get("expiration_time", ""),
                    }
                    dist = _extract_district(title, event_ticker)
                    if dist:
                        mkt["district"] = dist
                        if not state:
                            mkt["state"] = dist.split("-")[0]
                    markets.append(mkt)

            cursor = data.get("cursor")
            if not cursor or not events:
                break

    except Exception as e:
        logger.warning("Kalshi election fetch failed: {}", e)

    logger.info("Kalshi elections: {} markets", len(markets))
    return markets


# ── Snapshot & Delta ──────────────────────────────────────────────────────

def _extract_party_control(markets: list[dict]) -> dict:
    """Extract party control probabilities from direct control markets."""
    control = {}
    for m in markets:
        q = m["question"].lower()
        outcomes = m.get("outcomes", [])
        if len(outcomes) < 2:
            continue

        body = None
        if "senate" in q and ("control" in q or "win the senate" in q):
            body = "senate"
        elif "house" in q and ("control" in q or "win the house" in q):
            body = "house"
        elif ("presidential" in q or "presidency" in q) and ("party" in q or "which party" in q):
            body = "presidency"
        elif "2028" in q and "presidential" in q and "party" in q:
            body = "presidency"

        if not body:
            continue

        probs = {}
        for o in outcomes:
            name = o["name"].lower()
            if "republican" in name or "gop" in name:
                probs["republican"] = o["price"]
            elif "democrat" in name:
                probs["democrat"] = o["price"]

        # Handle "Will the Republican/Democratic Party control..." with Yes/No outcomes
        if not probs and outcomes[0]["name"] in ("Yes", "No"):
            yes_price = next((o["price"] for o in outcomes if o["name"] == "Yes"), 0)
            if "republican" in q:
                probs["republican"] = yes_price
                probs["democrat"] = round(1 - yes_price, 4)
            elif "democrat" in q:
                probs["democrat"] = yes_price
                probs["republican"] = round(1 - yes_price, 4)

        # Only set if we got both parties from this single market
        if len(probs) == 2 and body:
            # Prefer the first complete pair we find per body
            if body not in control:
                control[body] = probs

    # Presidency from Kalshi or Polymarket "Which party wins" multi-outcome
    if "presidency" not in control:
        for m in markets:
            q = m["question"].lower()
            ticker = m.get("id", "")
            if ticker.startswith("KXPRESPARTY") or ("which party" in q and "presidential" in q):
                for o in m.get("outcomes", []):
                    name = o["name"].lower()
                    if "republican" in name:
                        control.setdefault("presidency", {})["republican"] = o["price"]
                    elif "democrat" in name:
                        control.setdefault("presidency", {})["democrat"] = o["price"]
                if "presidency" in control and len(control["presidency"]) >= 2:
                    break

    return control


def snapshot_elections() -> dict:
    """Fetch all election markets and build a snapshot."""
    poly = fetch_polymarket_elections()
    kalshi = fetch_kalshi_elections()
    all_markets = poly + kalshi

    by_race = {}
    for m in all_markets:
        cat = m["race_category"]
        by_race[cat] = by_race.get(cat, 0) + 1

    total_volume = sum(m.get("volume", 0) for m in all_markets)
    party_control = _extract_party_control(all_markets)

    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "markets": all_markets,
        "summary": {
            "total_markets": len(all_markets),
            "polymarket_count": len(poly),
            "kalshi_count": len(kalshi),
            "by_race": by_race,
            "total_volume": round(total_volume, 2),
            "party_control": party_control,
        },
    }
    return snapshot


def save_snapshot(snapshot: dict) -> Path:
    """Save snapshot to dated JSON file + SQLite trend DB."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = SNAPSHOT_DIR / f"{date_str}.json"
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2, default=str)
    logger.info("Election snapshot saved: {} ({} markets)", path.name, snapshot["summary"]["total_markets"])

    # Also store in SQLite for fast trend queries
    try:
        from signals.election_db import store_snapshot
        store_snapshot(snapshot, _dedupe_state_races)
    except Exception as e:
        logger.warning("SQLite trend store failed (non-fatal): {}", e)

    return path


def load_snapshot(date_str: str) -> dict | None:
    """Load a snapshot by date string (YYYY-MM-DD)."""
    path = SNAPSHOT_DIR / f"{date_str}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def compute_deltas(current: dict, previous: dict) -> dict:
    """Compute week-over-week price changes between snapshots."""
    if not previous:
        return {"deltas": {}, "top_movers": []}

    # Build lookup from previous snapshot
    prev_prices = {}
    for m in previous.get("markets", []):
        mid = m["id"]
        for o in m.get("outcomes", []):
            prev_prices[(mid, o["name"])] = o["price"]

    # Compute deltas
    deltas = {}
    movers = []

    for m in current.get("markets", []):
        mid = m["id"]
        market_deltas = []
        for o in m.get("outcomes", []):
            key = (mid, o["name"])
            prev_price = prev_prices.get(key)
            if prev_price is not None:
                delta = round(o["price"] - prev_price, 4)
                market_deltas.append({
                    "name": o["name"],
                    "current": o["price"],
                    "previous": prev_price,
                    "delta": delta,
                })
                if abs(delta) >= 0.01:  # 1pp+ move
                    movers.append({
                        "id": mid,
                        "question": m["question"][:80],
                        "outcome": o["name"],
                        "delta": delta,
                        "current": o["price"],
                        "previous": prev_price,
                        "platform": m["platform"],
                        "race_category": m["race_category"],
                    })

        if market_deltas:
            deltas[mid] = market_deltas

    # Sort movers by absolute delta
    movers.sort(key=lambda x: abs(x["delta"]), reverse=True)

    return {"deltas": deltas, "top_movers": movers[:15]}


def compute_trends(current_snapshot: dict, days: int = 30) -> dict:
    """Build time-series trends from SQLite trend DB.

    Returns:
        control_history: [{date, senate_r, senate_d, house_r, house_d, pres_r, pres_d}]
        race_trends: {state: [{date, r_price, d_price, volume}]} for key races
        momentum: [{state, race, d3_delta, d7_delta, direction, acceleration}]
        volume_surges: [{state, race, avg_volume, latest_volume, surge_ratio}]
        volatility: [{state, race, stdev_7d, classification}]
    """
    try:
        from signals.election_db import query_control_history, query_race_history, query_days_of_data
    except Exception:
        return {"control_history": [], "race_trends": {}, "momentum": [],
                "volume_surges": [], "volatility": [], "days_of_data": 0}

    days_of_data = query_days_of_data()
    if days_of_data < 2:
        return {"control_history": [], "race_trends": {}, "momentum": [],
                "volume_surges": [], "volatility": [], "days_of_data": days_of_data}

    # ── 1. CONTROL PROBABILITY HISTORY (single query) ──
    control_history = query_control_history(days)

    # ── 2. PER-RACE PRICE HISTORY ──
    race_trends = query_race_history(race="senate", days=days)

    # Also query primary trends
    primary_trends = query_race_history(race="primary", days=days)

    # ── 3. MOMENTUM — 3-day and 7-day price velocity ──
    momentum = []
    d3_cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    d7_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    for st, history in race_trends.items():
        if len(history) < 2:
            continue
        latest = history[-1]

        # Find entry closest to 3 days ago
        d3_entry = None
        for h in reversed(history):
            if h["date"] <= d3_cutoff:
                d3_entry = h
                break
        d3_delta = round(latest["d_price"] - d3_entry["d_price"], 4) if d3_entry else 0

        # Find entry closest to 7 days ago
        d7_entry = None
        for h in reversed(history):
            if h["date"] <= d7_cutoff:
                d7_entry = h
                break
        d7_delta = round(latest["d_price"] - d7_entry["d_price"], 4) if d7_entry else 0

        if abs(d3_delta) < 0.01 and abs(d7_delta) < 0.01:
            continue

        d7_daily = d7_delta / 7 if d7_delta else 0
        d3_daily = d3_delta / 3 if d3_delta else 0
        accel = round(d3_daily / d7_daily, 2) if d7_daily != 0 else 0
        direction = "D gaining" if d3_delta > 0 else "R gaining"

        momentum.append({
            "state": st, "race": "senate",
            "d3_delta": d3_delta, "d7_delta": d7_delta,
            "d3_daily": round(d3_daily, 4),
            "direction": direction, "acceleration": accel,
            "current_d": latest["d_price"], "current_r": latest["r_price"],
        })
    momentum.sort(key=lambda x: abs(x["d3_delta"]), reverse=True)

    # ── 4. VOLUME SURGES — races heating up ──
    volume_surges = []
    for st, history in race_trends.items():
        if len(history) < 3:
            continue
        volumes = [h["volume"] for h in history if h["volume"] > 0]
        if len(volumes) < 3:
            continue
        avg_vol = sum(volumes[:-1]) / len(volumes[:-1])
        latest_vol = volumes[-1]
        if avg_vol > 0 and latest_vol > avg_vol * 1.5:
            volume_surges.append({
                "state": st, "race": "senate",
                "avg_volume": round(avg_vol, 0),
                "latest_volume": round(latest_vol, 0),
                "surge_ratio": round(latest_vol / avg_vol, 2),
            })
    volume_surges.sort(key=lambda x: x["surge_ratio"], reverse=True)

    # ── 5. VOLATILITY — unstable races ──
    volatility = []
    for st, history in race_trends.items():
        if len(history) < 4:
            continue
        recent = history[-7:]
        d_prices = [h["d_price"] for h in recent]
        if len(d_prices) < 3:
            continue
        changes = [d_prices[i] - d_prices[i - 1] for i in range(1, len(d_prices))]
        mean_change = sum(changes) / len(changes)
        variance = sum((c - mean_change) ** 2 for c in changes) / len(changes)
        stdev = variance ** 0.5
        if stdev < 0.005:
            continue
        classification = "high" if stdev > 0.03 else "moderate" if stdev > 0.015 else "low"
        volatility.append({
            "state": st, "race": "senate",
            "stdev_7d": round(stdev, 4), "classification": classification,
            "recent_range": round(max(d_prices) - min(d_prices), 4),
        })
    volatility.sort(key=lambda x: x["stdev_7d"], reverse=True)

    return {
        "control_history": control_history,
        "race_trends": {st: pts for st, pts in race_trends.items() if len(pts) >= 2},
        "primary_trends": {st: pts for st, pts in primary_trends.items() if len(pts) >= 2},
        "momentum": momentum[:10],
        "volume_surges": volume_surges[:10],
        "volatility": volatility[:10],
        "days_of_data": days_of_data,
    }


def _compute_insights(markets: list[dict], party_control: dict) -> dict:
    """Compute operative-grade insights from market data."""
    insights = {}

    # ── 1. TIPPING POINT RACES ──
    # Senate races closest to 50/50 that could flip chamber control
    senate_races = _dedupe_state_races(markets, "senate")
    tipping = []
    for st, info in senate_races.items():
        margin = abs(info["r_price"] - info["d_price"])
        tipping.append({
            "state": st,
            "margin": round(margin, 4),
            "leader": "R" if info["r_price"] > info["d_price"] else "D",
            "r_price": info["r_price"],
            "d_price": info["d_price"],
            "platform": info["platform"],
        })
    tipping.sort(key=lambda x: x["margin"])
    insights["tipping_point_races"] = tipping[:8]

    # ── 2. CROSS-PLATFORM SPREADS (Polymarket vs Kalshi vs PredictIt) ──
    try:
        from signals.predictit_client import fetch_predictit_elections
        pi_markets = fetch_predictit_elections()
    except Exception as e:
        logger.warning("PredictIt fetch for spreads failed (non-fatal): {}", e)
        pi_markets = []
    spreads = _compute_cross_platform_spreads(markets, predictit_markets=pi_markets)
    insights["cross_platform_spreads"] = spreads[:10]

    # ── 3. TICKET SPLIT SIGNALS ──
    # States where presidential lean contradicts senate lean
    pres_lean = "R" if party_control.get("presidency", {}).get("republican", 0) > 0.5 else "D"
    splits = []
    for st, info in senate_races.items():
        sen_leader = "R" if info["r_price"] > info["d_price"] else "D"
        if sen_leader != pres_lean and abs(info["r_price"] - info["d_price"]) < 0.40:
            splits.append({
                "state": st,
                "pres_lean": pres_lean,
                "senate_leader": sen_leader,
                "senate_margin": round(abs(info["r_price"] - info["d_price"]), 4),
                "r_price": info["r_price"],
                "d_price": info["d_price"],
            })
    splits.sort(key=lambda x: x["senate_margin"])
    insights["ticket_splits"] = splits

    # ── 4. COMPETITIVE RACE CLUSTERING ──
    # Group competitive races by region
    regions = {
        "Sun Belt": ["AZ", "NV", "GA", "TX", "FL", "NC"],
        "Rust Belt": ["PA", "MI", "WI", "OH", "MN", "IA"],
        "Northeast": ["NH", "ME", "NY", "NJ", "CT", "MA", "RI", "VT"],
        "Mountain West": ["MT", "CO", "NM", "UT", "WY", "ID"],
        "Southeast": ["VA", "SC", "AL", "MS", "TN", "KY", "WV"],
        "Midwest": ["IN", "MO", "KS", "NE", "ND", "SD", "IL"],
        "Pacific": ["WA", "OR", "CA", "HI", "AK"],
        "Other": ["DE", "MD", "LA", "AR", "OK"],
    }
    state_to_region = {}
    for region, states in regions.items():
        for st in states:
            state_to_region[st] = region

    competitive_by_region = {}
    for st, info in senate_races.items():
        margin = abs(info["r_price"] - info["d_price"])
        if margin < 0.30:  # competitive = <30pp margin
            region = state_to_region.get(st, "Other")
            competitive_by_region.setdefault(region, []).append({
                "state": st,
                "margin": round(margin, 4),
                "leader": "R" if info["r_price"] > info["d_price"] else "D",
            })
    insights["competitive_clustering"] = competitive_by_region

    # ── 5. VOLUME-WEIGHTED CONVICTION ──
    # Separate competitive races by volume (high conviction vs low attention)
    all_competitive = []
    for m in markets:
        if m["race_category"] not in ("senate", "governor", "house", "primary"):
            continue
        outs = m.get("outcomes", [])
        if not outs:
            continue
        lead = max(outs, key=lambda o: o["price"])
        if 0.40 <= lead["price"] <= 0.60:
            vol = m.get("volume", 0) or 0
            all_competitive.append({
                "question": m["question"][:80],
                "platform": m["platform"],
                "state": m.get("state", ""),
                "race_category": m["race_category"],
                "lead_price": lead["price"],
                "volume": vol,
                "conviction": "high" if vol > 500_000 else "medium" if vol > 50_000 else "low",
            })
    all_competitive.sort(key=lambda x: -x["volume"])
    insights["conviction_signals"] = all_competitive[:15]

    # ── 6. CHAMBER CONTROL MATH ──
    # How many seats need to flip for control to change
    dem_states = sum(1 for info in senate_races.values() if info["d_price"] > info["r_price"])
    rep_states = sum(1 for info in senate_races.values() if info["r_price"] > info["d_price"])
    majority = 51
    control_math = {
        "dem_projected": dem_states,
        "rep_projected": rep_states,
        "tossup": len(senate_races) - dem_states - rep_states,
        "dem_need_to_flip": max(0, majority - dem_states),
        "rep_need_to_flip": max(0, majority - rep_states),
        "total_races": len(senate_races),
    }
    insights["control_math"] = control_math

    # ── 7. PRIMARY COMPETITIVE INDEX ──
    # Group binary yes/no primary markets by event_title to reconstruct
    # multi-candidate fields, then rank by competitiveness
    import re as _re
    primary_markets = [m for m in markets if m["race_category"] == "primary"]
    # Group by event_title
    from collections import defaultdict as _ddict
    by_event = _ddict(list)
    for m in primary_markets:
        et = m.get("event_title", "")
        if et:
            by_event[et].append(m)

    primary_index = []
    seen_races = set()  # Dedupe Polymarket vs Kalshi duplicates
    for event_title, group in by_event.items():
        if len(group) < 3:
            continue  # Need at least 3 candidates to be interesting
        # Skip non-candidate markets (margins, counts, who-will-run)
        et_lower = event_title.lower()
        if any(kw in et_lower for kw in ("margin of", "how many", "will run for", "advance from",
                                            "advancers", "drop out", "elon musk", "which state",
                                            "exact outcome", "scheduled first")):
            continue

        # Extract candidate names and yes-prices from binary markets
        candidates = []
        total_vol = 0
        platform = group[0].get("platform", "")
        state = ""
        for m in group:
            outs = m.get("outcomes", [])
            if not outs:
                continue
            yes_price = outs[0].get("price", 0)
            # Use outcome name if it's a real name (not "Yes"/"No")
            out_name = outs[0].get("name", "").strip()
            if out_name.lower() in ("yes", "no", ""):
                # Fall back to extracting from question
                q = m["question"]
                match = _re.search(r"Will (.+?) (?:win|be the)", q)
                if not match:
                    match = _re.search(r":\s*Will (.+?) be", q)
                out_name = match.group(1).strip() if match else q[:30]
            if len(out_name) > 35:
                out_name = out_name[:32] + "..."
            candidates.append({"name": out_name, "price": yes_price})
            total_vol += m.get("volume", 0) or 0
            if not state:
                state = m.get("state", "")

        candidates.sort(key=lambda c: c["price"], reverse=True)
        if len(candidates) < 2:
            continue

        # Skip events where leader has 0% price (no trading activity)
        if candidates[0]["price"] <= 0:
            continue

        # Dedupe: normalize race name for cross-platform matching
        race_key = _re.sub(r"[^a-z0-9]", "", event_title.lower())[:40]
        if race_key in seen_races:
            continue
        seen_races.add(race_key)

        leader = candidates[0]
        runner_up = candidates[1]
        spread = leader["price"] - runner_up["price"]

        # Classify competitiveness
        if spread < 0.10:
            status = "toss-up"
        elif spread < 0.25:
            status = "competitive"
        elif spread < 0.50:
            status = "leaning"
        else:
            status = "safe"

        primary_index.append({
            "question": event_title[:80],
            "platform": platform,
            "state": state,
            "leader": leader["name"],
            "leader_price": leader["price"],
            "runner_up": runner_up["name"],
            "runner_up_price": runner_up["price"],
            "spread": round(spread, 4),
            "status": status,
            "volume": total_vol,
            "candidates": len(candidates),
            "top_3": candidates[:3],
        })
    primary_index.sort(key=lambda x: x["spread"])  # Most competitive first
    # Ensure platform diversity: if only one platform in top 25, add best from the other
    top = primary_index[:25]
    top_platforms = {p["platform"] for p in top}
    if len(top_platforms) < 2:
        added = 0
        for p in primary_index[25:]:
            if p["platform"] not in top_platforms:
                top.append(p)
                added += 1
                if added >= 5:
                    break
        if added:
            top_platforms = {p["platform"] for p in top}
            top.sort(key=lambda x: x["spread"])
    insights["primary_index"] = top

    # ── 8. PRESIDENTIAL CANDIDATE INDEX ──
    pres_candidates = _group_presidential_candidates(markets)
    insights["presidential_candidates"] = pres_candidates

    return insights


def _dedupe_state_races(markets: list[dict], category: str) -> dict:
    """Deduplicate markets per race, picking best data source.

    Senate/Governor: one entry per state (state-level races).
    House: one entry per district (CA-13, CA-22 are different races — do NOT
    collapse to a single CA entry, or 400+ Polymarket district markets
    silently become 1 row per state).
    """
    by_key = {}
    for m in markets:
        if m["race_category"] != category or not m.get("state"):
            continue
        # For House races key by district (e.g. "CA-13"); fall back to state
        # when district isn't extractable (e.g. state-wide seat-count markets).
        if category == "house":
            key = m.get("district") or m["state"]
        else:
            key = m["state"]
        outs = m.get("outcomes", [])
        q = m["question"].lower()
        has_party = any(
            re.search(r"republican|democrat", o.get("name", ""), re.I) for o in outs
        )
        vol = m.get("volume", 0) or 0
        # Prefer "winner" markets over specific matchup or margin markets
        is_winner_mkt = "winner" in q or "control" in q or ("will" in q and "win" in q)
        score = (2e9 if (has_party and is_winner_mkt) else 1e9 if has_party else 0) + vol
        if key not in by_key or score > by_key[key]["score"]:
            r_price, d_price = _extract_rd_prices(outs, q)
            # Only store if we got meaningful prices
            if r_price > 0 or d_price > 0:
                by_key[key] = {
                    "score": score, "r_price": r_price, "d_price": d_price,
                    "platform": m["platform"], "volume": vol,
                    "state": m.get("state", ""),
                    "district": m.get("district", ""),
                }
    return by_key


def _extract_rd_prices(outcomes: list[dict], question: str) -> tuple[float, float]:
    """Extract Republican and Democrat prices from outcomes."""
    r_price, d_price = 0.0, 0.0
    q = question.lower() if question else ""

    # Nominee/primary/margin markets don't represent D vs R general election odds
    if any(kw in q for kw in ("nominee", "primary", "nomination", "drop out", "margin of victory", "exact outcome")):
        return 0.0, 0.0

    # Kalshi quirk: both outcomes can have the same party name (e.g., "Democratic party")
    # First outcome is Yes price, second is No price. Detect this upfront.
    if len(outcomes) == 2:
        nm0 = (outcomes[0].get("name") or "").lower()
        nm1 = (outcomes[1].get("name") or "").lower()
        same_name = nm0 == nm1 or (
            ("democrat" in nm0 and "democrat" in nm1) or
            ("republican" in nm0 and "republican" in nm1)
        )
        if same_name:
            # First outcome = Yes price for that party
            yes_price = outcomes[0].get("price", 0)
            if "democrat" in nm0:
                d_price = yes_price
                r_price = round(1 - yes_price, 4)
            elif "republican" in nm0 or "gop" in nm0:
                r_price = yes_price
                d_price = round(1 - yes_price, 4)
            return r_price, d_price

    for i, o in enumerate(outcomes):
        nm = (o.get("name") or "").lower()
        price = o.get("price", 0)
        if "republican" in nm or "gop" in nm:
            r_price = price
        elif "democrat" in nm:
            d_price = price
        elif nm in ("yes", "no"):
            if "democrat" in q:
                if nm == "yes":
                    d_price = price
                else:
                    r_price = price
            elif "republican" in q:
                if nm == "yes":
                    r_price = price
                else:
                    d_price = price
    # If we only got one side, compute complement
    if r_price > 0 and d_price == 0:
        d_price = round(1 - r_price, 4)
    elif d_price > 0 and r_price == 0:
        r_price = round(1 - d_price, 4)
    return r_price, d_price


def _compute_cross_platform_spreads(markets: list[dict], predictit_markets: list[dict] | None = None) -> list[dict]:
    """Find races where platforms disagree most (Polymarket, Kalshi, PredictIt)."""
    from signals.predictit_client import fee_adjusted_prob

    # Group by (state, race_category)
    by_key = {}
    for m in markets:
        st = m.get("state", "")
        cat = m["race_category"]
        if not st or cat not in ("senate", "governor", "presidential", "primary"):
            continue
        key = (st, cat)
        outs = m.get("outcomes", [])
        r_price, d_price = _extract_rd_prices(outs, m["question"])
        if r_price or d_price:
            by_key.setdefault(key, []).append({
                "platform": m["platform"],
                "r_price": r_price, "d_price": d_price,
                "volume": m.get("volume", 0) or 0,
            })

    # Inject PredictIt markets with fee-adjusted prices
    if predictit_markets:
        for m in predictit_markets:
            if m.get("thin_market"):
                continue
            st = m.get("state", "")
            cat = m.get("race_category", "")
            if not st or cat not in ("senate", "governor", "presidential", "primary"):
                continue
            key = (st, cat)
            outs = m.get("outcomes", [])
            r_price, d_price = _extract_rd_prices(outs, m["question"])
            if r_price or d_price:
                # Fee-adjust PredictIt prices for fair comparison
                by_key.setdefault(key, []).append({
                    "platform": "predictit",
                    "r_price": fee_adjusted_prob(r_price),
                    "d_price": fee_adjusted_prob(d_price),
                    "volume": 0,
                    "bid_ask_spread": m.get("bid_ask_spread"),
                    "intraday_delta": m.get("intraday_delta"),
                })

    spreads = []
    for (st, cat), entries in by_key.items():
        platforms = {e["platform"] for e in entries}
        # Require at least 2 platforms (any combination)
        if len(platforms) < 2:
            continue
        # Must have Polymarket (execution target) and at least one other
        if "polymarket" not in platforms:
            continue

        # Get best entry per platform
        def _best(plat):
            valid = [e for e in entries if e["platform"] == plat and e["r_price"] > 0 and e["d_price"] > 0]
            if not valid:
                return None
            return max(valid, key=lambda e: e["volume"])

        poly = _best("polymarket")
        kalshi = _best("kalshi")
        pi = _best("predictit")

        if not poly:
            continue
        if not kalshi and not pi:
            continue

        # Sanity check each entry
        def _sane(entry):
            if not entry:
                return False
            if entry["r_price"] > 0.8 and entry["d_price"] > 0.8:
                return False
            if entry["r_price"] < 0.05 and entry["d_price"] < 0.05:
                return False
            return True

        if not _sane(poly):
            continue

        # Collect D prices from all sane platforms
        d_prices = {"polymarket": poly["d_price"]}
        if _sane(kalshi):
            d_prices["kalshi"] = kalshi["d_price"]
        if _sane(pi):
            d_prices["predictit"] = pi["d_price"]

        if len(d_prices) < 2:
            continue

        all_d = list(d_prices.values())
        max_spread = max(all_d) - min(all_d)
        if max_spread < 0.03:
            continue

        result = {
            "state": st,
            "race_category": cat,
            "spread_pp": round(max_spread * 100, 1),
            "poly_r": poly["r_price"], "poly_d": poly["d_price"],
        }
        if kalshi and _sane(kalshi):
            result["kalshi_r"] = kalshi["r_price"]
            result["kalshi_d"] = kalshi["d_price"]
        if pi and _sane(pi):
            result["pi_d"] = pi["d_price"]
            result["pi_r"] = pi["r_price"]
            result["pi_bid_ask_spread"] = pi.get("bid_ask_spread")
            result["pi_intraday_delta"] = pi.get("intraday_delta")
        result["platforms"] = len(d_prices)

        spreads.append(result)

    spreads.sort(key=lambda x: -x["spread_pp"])
    return spreads


# Known 2028 presidential candidates and their party
KNOWN_2028_CANDIDATES = {
    "jd vance": "R", "ron desantis": "R", "vivek ramaswamy": "R",
    "nikki haley": "R", "tucker carlson": "R", "ted cruz": "R",
    "tim scott": "R", "tom cotton": "R", "glenn youngkin": "R",
    "josh hawley": "R", "greg abbott": "R", "mike pompeo": "R",
    "marco rubio": "R", "sarah huckabee": "R", "kristi noem": "R",
    "elise stefanik": "R", "doug burgum": "R", "tulsi gabbard": "R",
    "ben carson": "R", "pete hegseth": "R", "thomas massie": "R",
    "gavin newsom": "D", "gretchen whitmer": "D", "josh shapiro": "D",
    "pete buttigieg": "D", "kamala harris": "D", "wes moore": "D",
    "andy beshear": "D", "jb pritzker": "D", "jon ossoff": "D",
    "ro khanna": "D", "james talarico": "D", "mark kelly": "D",
    "raphael warnock": "D", "john fetterman": "D", "elizabeth warren": "D",
    "bernie sanders": "D", "cory booker": "D", "amy klobuchar": "D",
    "hillary clinton": "D", "michelle obama": "D", "tim walz": "D",
    "alexandria ocasio-cortez": "D", "aoc": "D",
}


def _group_presidential_candidates(markets: list[dict]) -> dict:
    """Group individual 'Will X win the 2028 presidential election?' markets by candidate."""
    candidates = []
    seen_names = set()

    for m in markets:
        q = m.get("question", "")
        # Match "Will X win the 2028 US presidential election?" pattern
        match = re.search(r"Will (.+?) win the 2028", q, re.I)
        if not match:
            continue
        if m.get("race_category") not in ("presidential", "primary"):
            continue

        name = match.group(1).strip()
        name_lower = name.lower()
        if name_lower in seen_names:
            continue
        seen_names.add(name_lower)

        # Get yes price
        yes_price = 0
        for o in m.get("outcomes", []):
            if o.get("name", "").lower() in ("yes",):
                yes_price = o.get("price", 0)
                break
        if yes_price < 0.005:
            continue

        # Assign party
        party = KNOWN_2028_CANDIDATES.get(name_lower, "")
        if not party:
            # Fuzzy: check if last name matches
            last = name.split()[-1].lower() if " " in name else name_lower
            for known, p in KNOWN_2028_CANDIDATES.items():
                if last in known:
                    party = p
                    break
        if not party:
            party = "?"

        candidates.append({
            "name": name,
            "price": yes_price,
            "party": party,
            "platform": m.get("platform", ""),
            "volume": m.get("volume", 0),
            "market_id": m.get("id", ""),
        })

    candidates.sort(key=lambda c: c["price"], reverse=True)

    r_candidates = [c for c in candidates if c["party"] == "R"]
    d_candidates = [c for c in candidates if c["party"] == "D"]
    other_candidates = [c for c in candidates if c["party"] == "?"]

    return {
        "all_candidates": candidates,
        "r_candidates": r_candidates,
        "d_candidates": d_candidates,
        "other_candidates": other_candidates,
        "frontrunner_r": r_candidates[0] if r_candidates else None,
        "frontrunner_d": d_candidates[0] if d_candidates else None,
        "total": len(candidates),
        "r_total_prob": round(sum(c["price"] for c in r_candidates), 4),
        "d_total_prob": round(sum(c["price"] for c in d_candidates), 4),
    }


def _fetch_fec_overlay(snapshot_markets):
    """Fetch FEC campaign finance data (runs in thread pool).

    Enriches quarterly totals with near-real-time eFiling Schedule A receipts.
    """
    try:
        from signals.fec_client import (
            fetch_senate_fundraising, build_fundraising_overlay,
            compute_money_vs_odds, enrich_with_efiling,
        )
        senate_funds = fetch_senate_fundraising(2026)
        # Enrich with near-real-time eFiling donations (last 90 days)
        try:
            senate_funds = enrich_with_efiling(senate_funds, days=90)
        except Exception as e:
            logger.warning("eFiling enrichment failed (non-fatal): {}", e)
        fundraising = build_fundraising_overlay(senate_funds)
        senate_races = _dedupe_state_races(snapshot_markets, "senate")
        money_signals = compute_money_vs_odds(fundraising, senate_races)
        return {
            "fundraising": fundraising,
            "money_vs_odds": money_signals,
            "fec_candidates_tracked": len(senate_funds),
        }
    except Exception as e:
        logger.warning("FEC overlay failed (non-fatal): {}", e)
        return {"fundraising": {}, "money_vs_odds": [], "fec_candidates_tracked": 0}


def _fetch_predictit_overlay(snapshot_markets):
    """Fetch PredictIt cross-platform spreads (runs in thread pool)."""
    try:
        from signals.predictit_client import fetch_predictit_elections, compute_predictit_spreads
        pi_markets = fetch_predictit_elections()
        poly_markets = [m for m in snapshot_markets if m["platform"] == "polymarket"]
        kalshi_markets = [m for m in snapshot_markets if m["platform"] == "kalshi"]
        pi_spreads = compute_predictit_spreads(pi_markets, poly_markets, kalshi_markets)
        return {"predictit_spreads": pi_spreads[:10], "predictit_count": len(pi_markets)}
    except Exception as e:
        logger.warning("PredictIt overlay failed (non-fatal): {}", e)
        return {"predictit_spreads": [], "predictit_count": 0}


def _fetch_manifold_overlay(snapshot_markets):
    """Fetch Manifold Markets divergence (runs in thread pool)."""
    try:
        from signals.manifold_client import fetch_manifold_elections, compute_manifold_spreads
        mf_markets = fetch_manifold_elections()
        poly_markets = [m for m in snapshot_markets if m["platform"] == "polymarket"]
        mf_spreads = compute_manifold_spreads(mf_markets, poly_markets)
        return {"manifold_spreads": mf_spreads[:10], "manifold_count": len(mf_markets)}
    except Exception as e:
        logger.warning("Manifold overlay failed (non-fatal): {}", e)
        return {"manifold_spreads": [], "manifold_count": 0}


def _fetch_ie_spending_overlay():
    """Fetch FEC IE spending data (runs in thread pool)."""
    try:
        from signals.fec_spending import fetch_recent_ie_spending, aggregate_ie_by_race, detect_spending_surges
        ie_spending = fetch_recent_ie_spending(days=30)
        return {
            "ie_spending": aggregate_ie_by_race(ie_spending),
            "spending_surges": detect_spending_surges(ie_spending, threshold=100_000)[:10],
        }
    except Exception as e:
        logger.warning("FEC IE overlay failed (non-fatal): {}", e)
        return {"ie_spending": {}, "spending_surges": []}


def _fetch_gdelt_overlay():
    """Read the precomputed GDELT overlay from disk.

    The scheduler (task_gdelt_refresh) refreshes storage/gdelt_cache.json every 6h
    from a single process. The request path NEVER fetches GDELT live — live fetching
    from both uvicorn workers simultaneously caused 429 rate-limit storms.
    """
    try:
        gdelt_cache = Path(__file__).parent.parent / "storage" / "gdelt_cache.json"
        if gdelt_cache.exists():
            import json as _json
            return _json.loads(gdelt_cache.read_text())
    except Exception as e:
        logger.warning("GDELT cache read failed (non-fatal): {}", e)
    return {"candidate_sentiment": [], "state_sentiment": [], "narrative_shifts": []}


def _fetch_rcp_overlay(markets):
    """Fetch RealClearPolling data (runs in thread pool)."""
    try:
        from signals.rcp_client import build_rcp_overlay
        return build_rcp_overlay(markets)
    except Exception as e:
        logger.warning("RCP overlay failed (non-fatal): {}", e)
        return {"poll_data": {}, "poll_shifts": [], "poll_market_divergences": []}


def _fetch_efiling_overlay():
    """Fetch FEC real-time eFiling data (runs in thread pool)."""
    try:
        from signals.fec_efiling import build_efiling_overlay
        return build_efiling_overlay()
    except Exception as e:
        logger.warning("FEC eFiling overlay failed (non-fatal): {}", e)
        return {"efiling_alerts": [], "efiling_recent": [], "efiling_by_race": [], "efiling_count": 0}


def _fetch_wiki_overlay():
    """Fetch Wikipedia pageview data (runs in thread pool)."""
    try:
        from signals.wiki_pageviews import build_wiki_overlay
        return build_wiki_overlay()
    except Exception as e:
        logger.warning("Wiki pageviews overlay failed (non-fatal): {}", e)
        return {"wiki_pageviews": [], "wiki_spikes": [], "wiki_tracked": 0}


def _fetch_structural_overlay():
    """Fetch Ballotpedia structural data (race ratings, deadlines, calendar)."""
    try:
        from signals.ballotpedia_client import build_structural_overlay
        return build_structural_overlay()
    except Exception as e:
        logger.warning("Structural overlay failed (non-fatal): {}", e)
        return {"filing_deadlines": [], "primary_calendar": [], "race_ratings": {},
                "tossup_races": [], "candidate_changes": [], "upcoming_primaries": [],
                "imminent_deadlines": []}


def _fetch_fred_overlay():
    """Fetch FRED economic indicators."""
    try:
        from signals.fred_client import fetch_economic_indicators
        return fetch_economic_indicators()
    except Exception as e:
        logger.warning("FRED overlay failed (non-fatal): {}", e)
        return {"available": False, "indicators": {}, "incumbent_score": {}}


def _fetch_gtrends_overlay():
    """Fetch Google Trends data (runs in thread pool)."""
    try:
        from signals.google_trends import build_gtrends_overlay
        return build_gtrends_overlay()
    except Exception as e:
        logger.warning("Google Trends overlay failed (non-fatal): {}", e)
        return {"gtrends_candidates": [], "gtrends_spikes": [], "gtrends_tracked": 0}


def _fetch_crypto_money_overlay():
    """Fetch crypto-industry money overlay: FEC super PACs + LDA CLARITY lobbying.

    Two independent sources joined under insights.crypto_money:
      - fec_crypto_pacs: Schedule E IE spend by Fairshake network + industry PACs
      - lda_crypto_clarity: LDA filings mentioning CLARITY Act / market structure
    """
    result = {"fec_pacs": None, "lda_clarity": None, "fairshake_funders": None}
    try:
        from signals.fec_crypto_pacs import build_crypto_pac_overlay
        result["fec_pacs"] = build_crypto_pac_overlay()
    except Exception as e:
        logger.warning("FEC crypto PACs overlay failed (non-fatal): {}", e)
        result["fec_pacs"] = {"committees": [], "grand_total_spend": 0, "cycle": 2026}
    try:
        from signals.lda_crypto_lobbying import build_lda_overlay
        result["lda_clarity"] = build_lda_overlay()
    except Exception as e:
        logger.warning("LDA crypto lobbying overlay failed (non-fatal): {}", e)
        result["lda_clarity"] = {"clients": [], "total_spend": 0, "matched_filing_count": 0}
    try:
        from signals.fec_crypto_pacs import build_fairshake_funders_overlay
        result["fairshake_funders"] = build_fairshake_funders_overlay()
    except Exception as e:
        logger.warning("Fairshake funders overlay failed (non-fatal): {}", e)
        result["fairshake_funders"] = {"top_funders": [], "top_funders_total": 0, "cycle": 2026}
    # Vote alignment overlay (joins FEC top_recipients → 118th Congress crypto roll calls)
    try:
        from signals.crypto_vote_scorer import build_vote_alignment_overlay
        committees = (result.get("fec_pacs") or {}).get("committees") or []
        vote_stats = build_vote_alignment_overlay(committees)
        result["vote_alignment"] = vote_stats
    except Exception as e:
        logger.warning("Crypto vote alignment overlay failed (non-fatal): {}", e)
        result["vote_alignment"] = {"matched_recipients": 0, "vote_meta": {}}
    return result


def _classify_policy_category(title: str, ticker: str = "", tag_slug: str = "") -> str:
    """Classify a policy market into one of 6 categories.

    Categories: scotus, congress, trade_tariffs, foreign_policy,
                domestic_policy, macro_economic.
    """
    t = title.lower()
    tk = ticker.upper()

    # SCOTUS keywords and tickers
    scotus_tickers = {"KXOBERGEFELL", "KXBANTRANS", "KXTRUMPVSLAUGHTER", "KXNEWSCOTUSCONF"}
    if (any(kw in t for kw in ("supreme court", "scotus", "justice"))
            or tk.startswith(("KXSCOT", "KXSCOURT"))
            or tk in scotus_tickers):
        return "scotus"

    # Macro / economic policy — check before trade_tariffs (overlap on "deficit")
    macro_tags = {"fed", "macro", "macro-indicators", "macro-unemployment"}
    if (tag_slug in macro_tags
            or any(kw in t for kw in ("federal reserve", "fed fund", "fed rate",
                                       "fed chair", "fed meeting", "dot plot",
                                       "rate cut", "rate hike", "fomc",
                                       " cpi", "inflation", "pce ",
                                       "unemployment", "nonfarm", "jobs report",
                                       " gdp", "recession", "nber",
                                       "debt ceiling", "budget resolution",
                                       "fiscal", "monetary"))
            or tk.startswith(("KXFED", "KXCPI", "KXACPI", "KXU3",
                              "KXGDP", "KXNFP", "KXRATE", "KXDOTPLOT",
                              "KXPCECORE", "KXRECSS", "KXNBERR",
                              "KXDCEIL", "KXLOWEST", "KXTERMINAL",
                              "KXBALANCE", "KXBUDGET", "KXDEFICIT",
                              "KXHBUDGET", "KXSBUDGET", "KXRESCISSION",
                              "KXEFFR", "KXECONSTAT", "KXCBDECISION",
                              "KXNGDP", "KXIMFRECESS"))):
        return "macro_economic"

    # Trade/tariff keywords and tickers
    if (any(kw in t for kw in ("tariff", "trade war", "trade deal", "trade deficit",
                                "import ban", "export", "custom dut"))
            or tk.startswith(("KXFTA", "KXTARIFF", "KXTRADE", "KXCNIMPORT"))):
        return "trade_tariffs"

    # Foreign policy — must be checked before domestic (some overlap on "iran", "china")
    foreign_tags = {"ukraine", "israel", "iran", "nato", "foreign-policy"}
    if (tag_slug in foreign_tags
            or any(kw in t for kw in ("ukraine", "russia", "nato", "ceasefire",
                                       "israel", "iran", "gaza", "hezbollah",
                                       "north korea", "foreign policy", "war "))
            or tk.startswith(("KXSANCTIONRUS", "KXUSAIRAN", "KXTRUMPIRAN",
                              "KXELECTIRAN", "KXNEXTISRAEL", "KXISRAEL",
                              "KXPRESTAIWN", "KXTAIWAN", "KXUSAMBNATO",
                              "KXNATOTURKEY", "KXUSAMBISRAEL", "KXFOREIGNAID",
                              "KXMILSPEND", "KXVENZUELA", "KXIPCGAZA",
                              "KXTRUMPGAZA", "KXZELENSKYRUSSIA"))):
        return "foreign_policy"

    # Domestic policy
    domestic_tags = {"immigration", "abortion", "climate", "tiktok", "big-tech"}
    if (tag_slug in domestic_tags
            or any(kw in t for kw in ("immigration", "deportation", "border wall",
                                       "daca", "visa", "asylum",
                                       "abortion", "roe v", "reproductive",
                                       "climate", "emission", "carbon", "epa",
                                       "tiktok", "big tech", "antitrust",
                                       "ai regulation", "crypto regulation",
                                       "gun control", "second amendment",
                                       "deepfake", "executive order"))
            or tk.startswith(("KXTIKTOK", "KXABORTION", "KXDEPORT",
                              "KXBORDER", "KXUSCLIMATE", "KXAILEGIS",
                              "KXASYLUM", "KXGASBAN", "KXCRYPTO",
                              "KXAGENCY", "KXEOCOUN"))):
        return "domestic_policy"

    # Default: congress
    return "congress"


def _discover_policy_tags() -> list[str]:
    """Discover active Polymarket policy tag slugs via /tags endpoint.

    Fetches ALL tags from Gamma API (single call), then keyword-filters
    for policy-relevant ones. Zero candidate list maintenance needed.
    """
    import time as _time
    now = _time.time()
    if _policy_tag_cache["tags"] and (now - _policy_tag_cache["ts"]) < _POLICY_TAG_TTL:
        return _policy_tag_cache["tags"]

    known = set(POLY_POLICY_TAG_SLUGS)

    # Load disk cache (survives restarts)
    cache_file = _DISCOVERY_CACHE_DIR / "policy_tag_cache.json"
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            known.update(cached.get("tags", []))
        except Exception:
            pass

    # Fetch ALL tags from Gamma API and keyword-filter
    try:
        all_tags = _api_get(f"{GAMMA_API}/tags", {"limit": 2000}, timeout=15)
        if isinstance(all_tags, list):
            all_valid_slugs = {(t.get("slug") or "").lower() for t in all_tags if isinstance(t, dict)}
            before = len(known)
            for tag in all_tags:
                if not isinstance(tag, dict):
                    continue
                slug = (tag.get("slug") or "").lower()
                label = (tag.get("label") or "").lower()
                text = f"{slug} {label}"
                if any(kw in text for kw in _POLY_POLICY_TAG_KEYWORDS):
                    known.add(slug)
            new_count = len(known) - before
            if new_count:
                logger.info("Discovered {} new Polymarket policy tags via /tags", new_count)
            # Prune stale entries: remove discovered tags no longer on platform
            if all_valid_slugs:
                stale = known - all_valid_slugs - set(POLY_POLICY_TAG_SLUGS)
                if stale:
                    logger.info("Pruning {} stale Polymarket tags: {}", len(stale), sorted(stale)[:5])
                    known -= stale
    except Exception as e:
        logger.warning("Polymarket /tags fetch failed, using cache: {}", e)

    result = sorted(known)
    _policy_tag_cache["tags"] = result
    _policy_tag_cache["ts"] = now

    # Persist discovered tags to disk
    try:
        new_tags = sorted(known - set(POLY_POLICY_TAG_SLUGS))
        if new_tags:
            cache_file.write_text(json.dumps({"tags": new_tags, "ts": now}))
    except Exception:
        pass

    return result


_KALSHI_POLICY_KEYWORDS = [
    # Foreign policy
    "ukraine", "russia", "iran", "israel", "gaza", "taiwan", "nato",
    "ceasefire", "sanction", "troops", "military", "nuclear", "missile",
    "foreign", "ambassador",
    # Domestic policy
    "tiktok", "abortion", "deportat", "immigrat", "border", "asylum",
    "climate", "carbon", "emission", "gun control", "ai regulat",
    "ai legislat", "executive order", "attorney general", "deepfake",
    "agency", "crypto",
    # SCOTUS / Congress / Trade
    "scotus", "supreme court", "impeach", "speaker", "shutdown",
    "tariff", "trade deal", "trade deficit",
    # Macro / economic policy
    "fed ", "fed fund", "federal reserve", "cpi", "inflation", "unemploy",
    "gdp", "recession", "debt ceiling", "budget", "deficit", "fiscal",
    "monetary", "interest rate", "nonfarm", "bitcoin", "jobs report",
    "dot plot", "rate cut", "rate hike", "pce",
    # v3 additions — 2026 Trump-era gaps
    "doge", "h1b", "pardon", "visa", "student debt", "infrastructure",
    "voting rights", "redistrict", "census", "jobless claim",
]


def _discover_kalshi_policy_series() -> list[str]:
    """Discover Kalshi policy series by scanning /series and keyword-filtering.

    Uses a negative cache (rejected_series) to avoid re-verifying ~500 series
    that match keywords but have no open markets. Only truly new candidates
    (not in accepted or rejected cache) get verified via API calls.
    """
    import time as _time
    now = _time.time()
    if _kalshi_policy_cache["series"] and (now - _kalshi_policy_cache["ts"]) < _POLICY_TAG_TTL:
        return _kalshi_policy_cache["series"]

    known = set(KALSHI_POLICY_SERIES)
    rejected = set()

    # Load previously discovered + rejected series from disk
    cache_file = _DISCOVERY_CACHE_DIR / "kalshi_policy_series_cache.json"
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            known.update(cached.get("series", []))
            rejected.update(cached.get("rejected", []))
        except Exception:
            pass

    # Fetch full series list and keyword-filter for new candidates
    candidates = set()
    all_valid_tickers = set()
    try:
        data = _api_get(f"{KALSHI_API}/series", timeout=30)
        for s in data.get("series", []):
            ticker = s.get("ticker", "")
            title = (s.get("title", "") or "").lower()
            all_valid_tickers.add(ticker)
            if ticker in known or ticker in rejected:
                continue
            for kw in _KALSHI_POLICY_KEYWORDS:
                if kw in title:
                    candidates.add(ticker)
                    break
        # Prune stale entries
        if all_valid_tickers:
            stale = known - all_valid_tickers - set(KALSHI_POLICY_SERIES)
            if stale:
                logger.info("Pruning {} stale Kalshi series: {}", len(stale), sorted(stale)[:5])
                known -= stale
            rejected -= (rejected - all_valid_tickers)  # prune stale rejections too
    except Exception as e:
        logger.warning("Kalshi series listing failed: {}", e)

    # Only verify truly new candidates (not in accepted or rejected)
    new_found = 0
    for ticker in sorted(candidates):
        _time.sleep(0.15)
        try:
            data = _api_get(f"{KALSHI_API}/events", {
                "series_ticker": ticker,
                "with_nested_markets": "true",
                "status": "open",
            }, timeout=10)
            events = data.get("events", [])
            mkts = sum(len(e.get("markets", [])) for e in events)
            if mkts > 0:
                known.add(ticker)
                new_found += 1
            else:
                rejected.add(ticker)
        except Exception:
            rejected.add(ticker)

    if new_found:
        logger.info("Discovered {} new Kalshi policy series (verified {} candidates)", new_found, len(candidates))
    elif candidates:
        logger.info("Verified {} new candidates, none had open markets", len(candidates))

    result = sorted(known)
    _kalshi_policy_cache["series"] = result
    _kalshi_policy_cache["ts"] = now

    # Persist to disk (accepted + rejected)
    try:
        new_series = sorted(known - set(KALSHI_POLICY_SERIES))
        cache_file.write_text(json.dumps({
            "series": new_series,
            "rejected": sorted(rejected),
            "ts": now,
        }))
    except Exception:
        pass

    return result


# Search-based enrichment queries for policy markets that don't appear under
# any discoverable Polymarket tag (e.g. "Clarity Act signed into law in 2026"
# is tagged 'crypto'/'trump'/'us-law'/'politics' — none of which are returned
# by /tags?limit=2000, so auto-discovery never finds them).
#
# Each query is run via gamma-api/public-search; results are content-filtered
# to drop pure price/launch/airdrop noise from the broad 'crypto' surface area.
POLY_POLICY_SEARCH_QUERIES = [
    "Clarity Act",
    "crypto regulation",
    "crypto bill",
    "AI regulation",
    "AI safety bill",
    "stablecoin bill",
    "CFTC",
    "SEC vs",
    "crypto tax",
    "digital asset",
    "market structure bill",
]

# Title must contain at least one of these to be a candidate policy market.
_POLY_SEARCH_POLICY_TOKENS = (
    "bill", "act ", " act", "law", "regulat", "sign", "vote", "veto",
    "congress", "senate", "house ", "committee", "scotus", "supreme",
    " sec ", "sec ", "cftc", "court", "approv", "executive order",
    "tax", "ban ", "enact", "pass", "ratif",
)

# Skip these even if a policy token matched — they're price/launch/market noise.
_POLY_SEARCH_NOISE_TOKENS = (
    "ipo", "launch", "airdrop", "price", "hit ", "reach", "all time high",
    " ath", "fdv", "market cap", "tvl", "breakdown", "pump", "dump", "rally",
    "attack", "hack", "liquidat", "blow up", "mindshare", "fund flow",
    "etf inflow", "etf outflow", "open interest", "funding rate",
)


def _is_policy_relevant_title(title: str) -> bool:
    """Heuristic policy filter for search-based enrichment results."""
    t = (title or "").lower()
    if not any(tok in t for tok in _POLY_SEARCH_POLICY_TOKENS):
        return False
    if any(tok in t for tok in _POLY_SEARCH_NOISE_TOKENS):
        return False
    return True


def _fetch_polymarket_policy_search(
    queries: list[str], seen_event_ids: set, seen_event_slugs: set
) -> list[dict]:
    """Search-based enrichment: surface policy markets not reachable via tags.

    Uses gamma-api/public-search (the same endpoint backing polymarket.com's
    search bar) to find Clarity Act, AI regulation, etc. — markets tagged only
    under broad surfaces like 'crypto' that aren't returned by /tags listings.
    """
    enriched: list[dict] = []
    for q in queries:
        try:
            url = f"{GAMMA_API}/public-search?q={urllib.parse.quote(q)}&limit_per_type=10&events_status=active"
            req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read())
        except Exception as e:
            logger.warning("Polymarket public-search failed (q={}): {}", q, e)
            continue

        for event in (d.get("events") or []):
            eid = event.get("id", "")
            slug = event.get("slug", "")
            if eid and eid in seen_event_ids:
                continue
            if slug and slug in seen_event_slugs:
                continue
            title = event.get("title", "")
            if not _is_policy_relevant_title(title):
                continue
            if eid:
                seen_event_ids.add(eid)
            if slug:
                seen_event_slugs.add(slug)

            for mkt in (event.get("markets") or []):
                mid = mkt.get("id") or mkt.get("condition_id", "")
                question = mkt.get("question", title)
                cat = _classify_policy_category(question)
                try:
                    vol = float(mkt.get("volume", 0) or 0)
                except (ValueError, TypeError):
                    vol = 0
                outcomes = _parse_outcomes(mkt)
                if not outcomes:
                    continue
                enriched.append({
                    "id": mid,
                    "platform": "polymarket",
                    "question": question,
                    "slug": slug,
                    "policy_category": cat,
                    "outcomes": outcomes,
                    "volume": vol,
                    "end_date": mkt.get("endDate") or mkt.get("end_date_iso", ""),
                    "source": "search_enrichment",
                })
    return enriched


def _fetch_policy_polymarket() -> list[dict]:
    """Fetch policy markets from Polymarket (congress, SCOTUS, tariffs)."""
    seen_ids = set()
    seen_slugs = set()
    markets = []

    try:
        tag_slugs = _discover_policy_tags()
    except Exception:
        tag_slugs = POLY_POLICY_TAG_SLUGS

    for slug in tag_slugs:
        try:
            events = _api_get(f"{GAMMA_API}/events", {
                "tag_slug": slug,
                "active": "true",
                "closed": "false",
            })
            if not isinstance(events, list):
                continue

            for event in events:
                eid = event.get("id", "")
                event_slug = event.get("slug", "")
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                if event_slug:
                    seen_slugs.add(event_slug)

                for mkt in event.get("markets", []):
                    mid = mkt.get("id") or mkt.get("condition_id", "")
                    question = mkt.get("question", event.get("title", ""))
                    cat = _classify_policy_category(question, tag_slug=slug)
                    vol_str = mkt.get("volume", 0)
                    try:
                        vol = float(vol_str or 0)
                    except (ValueError, TypeError):
                        vol = 0
                    end_date = mkt.get("endDate") or mkt.get("end_date_iso", "")
                    markets.append({
                        "id": mid,
                        "platform": "polymarket",
                        "question": question,
                        "slug": event_slug,
                        "policy_category": cat,
                        "outcomes": _parse_outcomes(mkt),
                        "volume": vol,
                        "end_date": end_date,
                    })

        except Exception as e:
            logger.warning("Policy Polymarket fetch failed (tag={}): {}", slug, e)

    tag_count = len(markets)

    # Enrichment: search-based pickup for markets that don't appear under any
    # discoverable tag (Clarity Act, AI regulation, etc.).
    enriched = _fetch_polymarket_policy_search(
        POLY_POLICY_SEARCH_QUERIES, seen_ids, seen_slugs
    )
    markets.extend(enriched)

    logger.info(
        "Policy Polymarket: {} markets from {} tags + {} via search enrichment",
        len(markets), len(tag_slugs), len(enriched),
    )
    return markets


_kalshi_markets_cache = {"markets": [], "ts": 0}
_KALSHI_MARKETS_TTL = 3600  # 1 hour — cache fetched markets to avoid repeated 429s


def _fetch_policy_kalshi() -> list[dict]:
    """Fetch policy markets from Kalshi (all 6 policy categories).

    Sequential with 200ms sleep between requests. Results cached 1h
    in memory to avoid hammering Kalshi's rate limiter on 200+ series.
    """
    import time as _time

    # Return cached markets if fresh
    now = _time.time()
    if _kalshi_markets_cache["markets"] and (now - _kalshi_markets_cache["ts"]) < _KALSHI_MARKETS_TTL:
        return _kalshi_markets_cache["markets"]

    try:
        all_series = _discover_kalshi_policy_series()
    except Exception:
        all_series = KALSHI_POLICY_SERIES

    markets = []
    for i, series in enumerate(all_series):
        if i > 0:
            _time.sleep(0.20)  # 200ms between requests — Kalshi-safe
        try:
            data = _api_get(f"{KALSHI_API}/events", {
                "series_ticker": series,
                "with_nested_markets": "true",
                "status": "open",
            })
            for event in data.get("events", []):
                title = event.get("title", "")
                event_ticker = event.get("event_ticker", "")
                for mkt in event.get("markets", []):
                    ticker = mkt.get("ticker", "")
                    subtitle = mkt.get("subtitle") or mkt.get("title") or title
                    if subtitle == title or not subtitle.strip():
                        question = title
                    else:
                        question = f"{title}: {subtitle}"

                    outcomes = _parse_outcomes(mkt)
                    if not outcomes:
                        continue

                    cat = _classify_policy_category(question, event_ticker)
                    vol = mkt.get("volume_fp") or mkt.get("volume", 0)
                    try:
                        vol = float(vol)
                    except (ValueError, TypeError):
                        vol = 0

                    markets.append({
                        "id": ticker or event_ticker,
                        "platform": "kalshi",
                        "question": question,
                        "slug": "",
                        # Per-market ticker (with ladder suffix like -FEB1/-MAY/-27).
                        # Using event_ticker here would collapse ladder rungs on
                        # downstream dedupe. Keep event_ticker in a separate field
                        # so the cross-platform matcher / UI can still group.
                        "ticker": ticker or event_ticker,
                        "event_ticker": event_ticker,
                        "event_title": title,
                        "rules_primary": mkt.get("rules_primary") or "",
                        "rules_secondary": mkt.get("rules_secondary") or "",
                        "policy_category": cat,
                        "outcomes": outcomes,
                        "volume": vol,
                        "end_date": mkt.get("close_time") or mkt.get("expiration_time", ""),
                    })

        except Exception as e:
            logger.warning("Policy Kalshi fetch failed (series={}): {}", series, e)

    _kalshi_markets_cache["markets"] = markets
    _kalshi_markets_cache["ts"] = _time.time()
    logger.info("Policy Kalshi: {} markets from {} series", len(markets), len(all_series))
    return markets


_STOP_WORDS = frozenset({
    "a", "an", "the", "will", "by", "before", "in", "of", "to", "and",
    "or", "on", "for", "at", "be", "is", "are", "was", "were", "it",
    "its", "this", "that", "with", "from", "as", "do", "does", "did",
    "has", "have", "had", "not", "no", "yes", "if", "than", "more",
    "most", "any", "all", "each", "every", "other", "there", "their",
    "what", "which", "who", "whom", "how", "when", "where", "about",
})


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase words with stop-word removal."""
    return [w for w in re.findall(r'\w+', text.lower()) if w not in _STOP_WORDS]


def _tfidf_similarity(a: str, b: str, corpus: list[list[str]]) -> float:
    """TF-IDF weighted cosine similarity between two texts given a corpus.

    corpus is a list of pre-tokenized documents (all market questions).
    """
    from math import log, sqrt

    tokens_a = _tokenize(a)
    tokens_b = _tokenize(b)
    if not tokens_a or not tokens_b:
        return 0

    # Document frequency from corpus
    n_docs = len(corpus) or 1
    df = {}
    for doc in corpus:
        for w in set(doc):
            df[w] = df.get(w, 0) + 1

    def _tfidf_vec(tokens):
        tf = {}
        for w in tokens:
            tf[w] = tf.get(w, 0) + 1
        vec = {}
        for w, count in tf.items():
            idf = log((n_docs + 1) / (df.get(w, 0) + 1)) + 1
            vec[w] = count * idf
        return vec

    va = _tfidf_vec(tokens_a)
    vb = _tfidf_vec(tokens_b)

    # Cosine similarity
    all_terms = set(va) | set(vb)
    dot = sum(va.get(t, 0) * vb.get(t, 0) for t in all_terms)
    mag_a = sqrt(sum(v * v for v in va.values())) or 1
    mag_b = sqrt(sum(v * v for v in vb.values())) or 1
    return dot / (mag_a * mag_b)


_POLICY_MATCH_THRESHOLD = 0.55
_MACRO_MATCH_THRESHOLD = 0.70  # Higher bar for macro (many similar-looking markets)

# Numeric tokens that indicate bracket/range markets (e.g. "between 800B and 900B")
_BRACKET_RE = re.compile(
    r'\d+[btmk]?\s*(?:to|and|or more|or less|above|below|between|\+)', re.I
)


def _is_bracket_mismatch(q1: str, q2: str) -> bool:
    """Detect when two questions are same topic but different numeric brackets.

    E.g. Poly "deficit between 800B and 900B" vs Kalshi "deficit above 1T"
    — same topic, different contracts, not a real arb.
    Also catches month/date mismatches (e.g. "CPI in Mar" vs "CPI in Apr").
    """
    nums1 = set(re.findall(r'\d+(?:\.\d+)?', q1))
    nums2 = set(re.findall(r'\d+(?:\.\d+)?', q2))
    has_brackets = bool(_BRACKET_RE.search(q1)) or bool(_BRACKET_RE.search(q2))
    # Different numeric brackets = not the same contract
    if has_brackets and nums1 and nums2 and not nums1.intersection(nums2):
        return True
    # Different months (e.g. "Mar 2026" vs "Apr 2026") = different contracts
    months_re = re.compile(r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*', re.I)
    m1 = set(m.lower()[:3] for m in months_re.findall(q1))
    m2 = set(m.lower()[:3] for m in months_re.findall(q2))
    if m1 and m2 and not m1.intersection(m2):
        return True
    # Different countries (e.g. "UK inflation" vs "US inflation")
    countries = re.compile(r'\b(?:uk|us|canada|china|euro|japan|germany|france|italy|spain|brazil|australia|mexico|korea|turkey|argentina|india|switzerland|iran|russia|ukraine|israel|north korea|taiwan|saudi|qatar|egypt|poland|sweden|norway|denmark|netherlands|belgium|austria|greece|ireland|portugal|czech|hungary|romania|colombia|chile|peru|indonesia|thailand|vietnam|philippines|pakistan|bangladesh|nigeria|south africa|kenya)\b', re.I)
    c1 = set(c.lower() for c in countries.findall(q1))
    c2 = set(c.lower() for c in countries.findall(q2))
    if c1 and c2 and not c1.intersection(c2):
        return True
    return False


def _match_policy_cross_platform(poly: list[dict], kalshi: list[dict]) -> list[dict]:
    """Find cross-platform policy market pairs with meaningful spread.

    Uses TF-IDF weighted cosine similarity with stop-word removal.
    Same-category gate + 0.45 threshold + 3pp spread minimum.
    Filters out bracket mismatches (same topic, different numeric ranges).

    Optimized: pre-computes IDF once, groups by category to reduce O(n*m).
    """
    from math import log, sqrt

    # Pre-tokenize all markets
    all_markets = poly + kalshi
    tokenized = {id(m): _tokenize(m.get("question", "")) for m in all_markets}

    # Build IDF from full corpus (once)
    n_docs = len(all_markets) or 1
    df = {}
    for tokens in tokenized.values():
        for w in set(tokens):
            df[w] = df.get(w, 0) + 1

    def _tfidf_vec(tokens):
        tf = {}
        for w in tokens:
            tf[w] = tf.get(w, 0) + 1
        vec = {}
        for w, count in tf.items():
            idf = log((n_docs + 1) / (df.get(w, 0) + 1)) + 1
            vec[w] = count * idf
        return vec

    def _cosine(va, vb):
        all_terms = set(va) | set(vb)
        dot = sum(va.get(t, 0) * vb.get(t, 0) for t in all_terms)
        mag_a = sqrt(sum(v * v for v in va.values())) or 1
        mag_b = sqrt(sum(v * v for v in vb.values())) or 1
        return dot / (mag_a * mag_b)

    # Pre-compute TF-IDF vectors for all markets
    vectors = {id(m): _tfidf_vec(tokenized[id(m)]) for m in all_markets}

    # Group by category for efficient matching
    poly_by_cat = {}
    for m in poly:
        cat = m.get("policy_category", "congress")
        poly_by_cat.setdefault(cat, []).append(m)
    kalshi_by_cat = {}
    for m in kalshi:
        cat = m.get("policy_category", "congress")
        kalshi_by_cat.setdefault(cat, []).append(m)

    matches = []
    seen_pairs = set()

    for cat in poly_by_cat:
        if cat not in kalshi_by_cat:
            continue
        p_list = poly_by_cat[cat]
        k_list = kalshi_by_cat[cat]

        for p in p_list:
            p_outs = p.get("outcomes", [])
            p_yes = p_outs[0]["price"] if p_outs else 0
            p_vec = vectors[id(p)]

            for k in k_list:
                pair_key = (p.get("id", ""), k.get("id", ""))
                if pair_key in seen_pairs:
                    continue

                sim = _cosine(p_vec, vectors[id(k)])
                threshold = _MACRO_MATCH_THRESHOLD if cat == "macro_economic" else _POLICY_MATCH_THRESHOLD
                if sim < threshold:
                    continue

                if _is_bracket_mismatch(p["question"], k["question"]):
                    continue

                k_outs = k.get("outcomes", [])
                k_yes = k_outs[0]["price"] if k_outs else 0

                spread = abs(p_yes - k_yes)
                if spread < 0.03:
                    continue

                seen_pairs.add(pair_key)
                matches.append({
                    "poly_question": p["question"],
                    "kalshi_question": k["question"],
                    "policy_category": p["policy_category"],
                    "poly_yes": round(p_yes, 4),
                    "kalshi_yes": round(k_yes, 4),
                    "spread_pp": round(spread * 100, 1),
                    "similarity": round(sim, 3),
                })

    matches.sort(key=lambda x: -x["spread_pp"])
    return matches


def _fetch_policy_pulse_overlay() -> dict:
    """Fetch and assemble Policy Pulse data (SCOTUS, Congress, Trade/Tariffs).

    Polymarket and Kalshi fetches run in parallel to cut wall-clock time in half.
    """
    from concurrent.futures import ThreadPoolExecutor

    poly, kalshi = [], []
    with ThreadPoolExecutor(max_workers=2) as pool:
        poly_f = pool.submit(_fetch_policy_polymarket)
        kalshi_f = pool.submit(_fetch_policy_kalshi)
        try:
            poly = poly_f.result(timeout=120)
        except Exception as e:
            logger.warning("Policy Polymarket fetch failed: {}", e)
        try:
            kalshi = kalshi_f.result(timeout=480)
        except Exception as e:
            logger.warning("Policy Kalshi fetch failed: {}", e)

    cross = _match_policy_cross_platform(poly, kalshi)

    all_markets = poly + kalshi
    buckets = {"scotus": [], "congress": [], "trade_tariffs": [],
                "foreign_policy": [], "domestic_policy": [], "macro_economic": []}
    for m in all_markets:
        cat = m.get("policy_category", "congress")
        if cat in buckets:
            buckets[cat].append(m)

    return {"policy_pulse": {
        **buckets,
        "cross_spreads": cross,
        "total_markets": len(all_markets),
        "poly_count": len(poly),
        "kalshi_count": len(kalshi),
    }}


def generate_report(core_only: bool = False) -> dict:
    """Main entry point — generate full election report with deltas.

    Args:
        core_only: If True, skip slow overlays (GDELT, FEC, PredictIt, Manifold, RCP)
                   and return just markets + insights + deltas for fast initial load.

    Overlays (FEC, PredictIt, Manifold, IE spending) run in parallel
    via ThreadPoolExecutor for faster response times.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    snapshot = snapshot_elections()

    # Find previous snapshot — prefer weekly (7-14d), fallback to daily (1-6d)
    prev = None
    delta_period = "weekly"
    for days_back in range(7, 14):
        date_str = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        prev = load_snapshot(date_str)
        if prev:
            break

    if not prev:
        # No weekly snapshot yet — try daily (most recent 1-6 days back)
        delta_period = "daily"
        for days_back in range(1, 7):
            date_str = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
            prev = load_snapshot(date_str)
            if prev:
                break

    delta_data = compute_deltas(snapshot, prev)

    # Compute operative insights
    insights = _compute_insights(
        snapshot["markets"],
        snapshot["summary"]["party_control"],
    )

    # Add composite score to summary
    score, label, phase = _compute_composite_score(snapshot["summary"]["party_control"])
    snapshot["summary"]["composite_score"] = score
    snapshot["summary"]["composite_label"] = label
    snapshot["summary"]["composite_phase"] = phase

    if core_only:
        # Fast path — skip slow overlays, return core data immediately
        # Also try loading cached GDELT from disk
        gdelt_cache = Path(__file__).parent.parent / "storage" / "gdelt_cache.json"
        if gdelt_cache.exists():
            try:
                import json as _json
                cached = _json.loads(gdelt_cache.read_text())
                insights["candidate_sentiment"] = cached.get("candidate_sentiment", [])
                insights["state_sentiment"] = cached.get("state_sentiment", [])
                insights["narrative_shifts"] = cached.get("narrative_shifts", [])
            except Exception:
                pass

        # Load cached IE spending from disk
        ie_cache = Path(__file__).parent.parent / "storage" / "ie_spending_cache.json"
        if ie_cache.exists():
            try:
                import json as _json2
                cached_ie = _json2.loads(ie_cache.read_text())
                insights["ie_spending"] = cached_ie.get("ie_spending", {})
                insights["spending_surges"] = cached_ie.get("spending_surges", [])
                # Compute midterm with cached IE data
                try:
                    midterm = compute_midterm_analysis(
                        snapshot["markets"],
                        snapshot["summary"]["party_control"],
                        ie_spending=insights.get("ie_spending", {}),
                    )
                    insights["midterm"] = midterm
                except Exception:
                    insights["midterm"] = {}
            except Exception:
                pass

        # Load cached FEC fundraising from disk
        fec_cache = Path(__file__).parent.parent / "storage" / "fec_cache_overlay.json"
        if fec_cache.exists():
            try:
                cached_fec = json.loads(fec_cache.read_text())
                insights["fundraising"] = cached_fec.get("fundraising", {})
                insights["money_vs_odds"] = cached_fec.get("money_vs_odds", [])
                snapshot["summary"]["fec_candidates_tracked"] = cached_fec.get("fec_candidates_tracked", 0)
            except Exception:
                pass

        # Load cached PredictIt spreads from disk
        pi_cache = Path(__file__).parent.parent / "storage" / "predictit_cache_overlay.json"
        if pi_cache.exists():
            try:
                import json as _json5
                cached_pi = _json5.loads(pi_cache.read_text())
                insights["predictit_spreads"] = cached_pi.get("predictit_spreads", [])
            except Exception:
                pass

        # Load cached RCP poll data from disk
        rcp_cache = Path(__file__).parent.parent / "storage" / "rcp_cache.json"
        if rcp_cache.exists():
            try:
                import json as _json3
                cached_rcp = _json3.loads(rcp_cache.read_text())
                insights["poll_data"] = cached_rcp.get("poll_data", {})
                insights["poll_shifts"] = cached_rcp.get("poll_shifts", [])
                insights["poll_market_divergences"] = cached_rcp.get("poll_market_divergences", [])
            except Exception:
                pass

        # Load cached eFiling data from disk
        efiling_cache = Path(__file__).parent.parent / "storage" / "efiling_cache.json"
        if efiling_cache.exists():
            try:
                import json as _json4
                cached_ef = _json4.loads(efiling_cache.read_text())
                insights["efiling_alerts"] = cached_ef.get("efiling_alerts", [])
                insights["efiling_by_race"] = cached_ef.get("efiling_by_race", [])
            except Exception:
                pass

        # Load cached crypto money overlay (FEC super PACs + LDA CLARITY lobbying)
        crypto_money_cache = Path(__file__).parent.parent / "storage" / "crypto_money_cache.json"
        if crypto_money_cache.exists():
            try:
                import json as _json_cm
                cached_cm = _json_cm.loads(crypto_money_cache.read_text())
                insights["crypto_money"] = cached_cm
            except Exception:
                pass

        # Load cached Policy Pulse from disk
        policy_cache = Path(__file__).parent.parent / "storage" / "policy_pulse_cache.json"
        if policy_cache.exists():
            try:
                cached_policy = json.loads(policy_cache.read_text())
                insights["policy_pulse"] = cached_policy.get("policy_pulse", {})
            except Exception:
                pass

        trends = compute_trends(snapshot, days=30)
        insights["trends"] = trends

        return {
            **snapshot,
            "deltas": delta_data["deltas"],
            "top_movers": delta_data["top_movers"],
            "delta_period": delta_period if prev else None,
            "previous_snapshot_date": prev["timestamp"][:10] if prev else None,
            "insights": insights,
            "core_only": True,
        }

    # Run all overlays in parallel
    markets = snapshot["markets"]
    with ThreadPoolExecutor(max_workers=12, thread_name_prefix="election") as pool:
        fec_future = pool.submit(_fetch_fec_overlay, markets)
        pi_future = pool.submit(_fetch_predictit_overlay, markets)
        mf_future = pool.submit(_fetch_manifold_overlay, markets)
        ie_future = pool.submit(_fetch_ie_spending_overlay)
        gdelt_future = pool.submit(_fetch_gdelt_overlay)
        rcp_future = pool.submit(_fetch_rcp_overlay, markets)
        efiling_future = pool.submit(_fetch_efiling_overlay)
        wiki_future = pool.submit(_fetch_wiki_overlay)
        gtrends_future = pool.submit(_fetch_gtrends_overlay)
        structural_future = pool.submit(_fetch_structural_overlay)
        fred_future = pool.submit(_fetch_fred_overlay)
        policy_future = pool.submit(_fetch_policy_pulse_overlay)
        crypto_money_future = pool.submit(_fetch_crypto_money_overlay)

        def _safe_result(future, name, timeout, default):
            """Get future result or return default on timeout/error."""
            try:
                return future.result(timeout=timeout)
            except Exception as e:
                logger.warning("Overlay {} failed (non-fatal): {}", name, e)
                return default

        fec_result = _safe_result(fec_future, "FEC", 60,
            {"fundraising": {}, "money_vs_odds": [], "fec_candidates_tracked": 0})
        pi_result = _safe_result(pi_future, "PredictIt", 60,
            {"predictit_spreads": [], "predictit_count": 0})
        mf_result = _safe_result(mf_future, "Manifold", 60,
            {"manifold_spreads": [], "manifold_count": 0})
        ie_result = _safe_result(ie_future, "IE Spending", 60,
            {"ie_spending": {}, "spending_surges": []})
        gdelt_result = _safe_result(gdelt_future, "GDELT", 90,
            {"candidate_sentiment": [], "state_sentiment": [], "narrative_shifts": []})
        rcp_result = _safe_result(rcp_future, "RCP", 60,
            {"poll_data": {}, "poll_shifts": [], "poll_market_divergences": []})
        efiling_result = _safe_result(efiling_future, "eFiling", 60,
            {"efiling_alerts": [], "efiling_by_race": [], "efiling_count": 0})
        wiki_result = _safe_result(wiki_future, "Wiki", 60,
            {"wiki_spikes": [], "wiki_pageviews": [], "wiki_tracked": 0})
        gtrends_result = _safe_result(gtrends_future, "GTrends", 120,
            {"gtrends_spikes": [], "gtrends_election_topics": [], "gtrends_tracked": 0})
        structural_result = _safe_result(structural_future, "Structural", 30,
            {"filing_deadlines": [], "primary_calendar": [], "race_ratings": {},
             "tossup_races": [], "candidate_changes": [], "upcoming_primaries": [],
             "imminent_deadlines": []})
        fred_result = _safe_result(fred_future, "FRED", 30,
            {"available": False, "indicators": {}, "incumbent_score": {}})
        policy_result = _safe_result(policy_future, "PolicyPulse", 600,
            {"policy_pulse": {"scotus": [], "congress": [], "trade_tariffs": [], "foreign_policy": [], "domestic_policy": [], "macro_economic": [], "cross_spreads": [], "total_markets": 0, "poly_count": 0, "kalshi_count": 0}})
        crypto_money_result = _safe_result(crypto_money_future, "CryptoMoney", 90,
            {"fec_pacs": {"committees": [], "grand_total_spend": 0, "cycle": 2026},
             "lda_clarity": {"clients": [], "total_spend": 0, "matched_filing_count": 0}})

    # Merge FEC overlay + cache to disk for core_only fast path
    insights["fundraising"] = fec_result["fundraising"]
    insights["money_vs_odds"] = fec_result["money_vs_odds"]
    snapshot["summary"]["fec_candidates_tracked"] = fec_result["fec_candidates_tracked"]
    try:
        fec_cache = Path(__file__).parent.parent / "storage" / "fec_cache_overlay.json"
        fec_cache.write_text(json.dumps(fec_result))
    except Exception:
        pass

    # Merge PredictIt overlay + cache to disk for core_only fast path
    insights["predictit_spreads"] = pi_result["predictit_spreads"]
    snapshot["summary"]["predictit_count"] = pi_result["predictit_count"]
    try:
        pi_cache = Path(__file__).parent.parent / "storage" / "predictit_cache_overlay.json"
        pi_cache.write_text(json.dumps(pi_result))
    except Exception:
        pass

    # Merge Manifold overlay
    insights["manifold_spreads"] = mf_result["manifold_spreads"]
    snapshot["summary"]["manifold_count"] = mf_result["manifold_count"]

    # Merge IE spending overlay
    insights["ie_spending"] = ie_result["ie_spending"]
    insights["spending_surges"] = ie_result["spending_surges"]

    # Merge GDELT overlay + cache to disk for core_only fast path
    insights["candidate_sentiment"] = gdelt_result.get("candidate_sentiment", [])
    insights["state_sentiment"] = gdelt_result.get("state_sentiment", [])
    insights["narrative_shifts"] = gdelt_result.get("narrative_shifts", [])
    try:
        gdelt_cache = Path(__file__).parent.parent / "storage" / "gdelt_cache.json"
        # Never blank a good cache with an empty (rate-limited) result.
        if gdelt_result.get("candidate_sentiment") or gdelt_result.get("state_sentiment"):
            gdelt_cache.write_text(json.dumps(gdelt_result))
    except Exception:
        pass

    # Cache IE spending to disk for core_only fast path
    try:
        ie_cache = Path(__file__).parent.parent / "storage" / "ie_spending_cache.json"
        ie_cache.write_text(json.dumps(ie_result))
    except Exception:
        pass

    # Merge RCP overlay + cache to disk for core_only fast path
    insights["poll_data"] = rcp_result.get("poll_data", {})
    insights["poll_shifts"] = rcp_result.get("poll_shifts", [])
    insights["poll_market_divergences"] = rcp_result.get("poll_market_divergences", [])
    try:
        rcp_cache = Path(__file__).parent.parent / "storage" / "rcp_cache.json"
        rcp_cache.write_text(json.dumps(rcp_result))
    except Exception:
        pass

    # Merge FEC eFiling overlay + cache to disk for core_only fast path
    insights["efiling_alerts"] = efiling_result.get("efiling_alerts", [])
    insights["efiling_by_race"] = efiling_result.get("efiling_by_race", [])
    snapshot["summary"]["efiling_count"] = efiling_result.get("efiling_count", 0)
    try:
        efiling_cache = Path(__file__).parent.parent / "storage" / "efiling_cache.json"
        efiling_cache.write_text(json.dumps(efiling_result))
    except Exception:
        pass

    # Merge Wikipedia pageviews overlay
    insights["wiki_spikes"] = wiki_result.get("wiki_spikes", [])
    insights["wiki_pageviews"] = wiki_result.get("wiki_pageviews", [])
    snapshot["summary"]["wiki_tracked"] = wiki_result.get("wiki_tracked", 0)

    # Merge Google Trends overlay
    insights["gtrends_spikes"] = gtrends_result.get("gtrends_spikes", [])
    insights["gtrends_election_topics"] = gtrends_result.get("gtrends_election_topics", [])
    snapshot["summary"]["gtrends_tracked"] = gtrends_result.get("gtrends_tracked", 0)

    # Smart Money overlay (depends on FEC + IE results)
    try:
        from signals.smart_money import build_smart_money_overlay
        sm_result = build_smart_money_overlay(
            markets, insights.get("fundraising", {}), insights.get("ie_spending", {}))
        insights["smart_money"] = sm_result.get("smart_money", [])
        insights["fec_cross_signals"] = sm_result.get("fec_cross_signals", [])
        insights["whale_activity"] = sm_result.get("whale_activity", [])
    except Exception as e:
        logger.warning("Smart money overlay failed (non-fatal): {}", e)
        insights["smart_money"] = []
        insights["fec_cross_signals"] = []
        insights["whale_activity"] = []

    # Structural data overlay (Ballotpedia — race ratings, deadlines, calendar)
    insights["structural"] = structural_result

    # FRED economic indicators overlay
    insights["economic"] = fred_result

    # Merge Policy Pulse overlay + cache to disk
    insights["policy_pulse"] = policy_result.get("policy_pulse", {})
    try:
        policy_cache = Path(__file__).parent.parent / "storage" / "policy_pulse_cache.json"
        policy_cache.write_text(json.dumps(policy_result))
    except Exception:
        pass

    # Merge crypto money overlay (FEC super PACs + LDA CLARITY lobbying)
    insights["crypto_money"] = crypto_money_result
    try:
        crypto_money_cache = Path(__file__).parent.parent / "storage" / "crypto_money_cache.json"
        crypto_money_cache.write_text(json.dumps(crypto_money_result))
    except Exception:
        pass

    # Compute midterm analysis (depends on ie_spending result)
    try:
        midterm = compute_midterm_analysis(
            snapshot["markets"],
            snapshot["summary"]["party_control"],
            ie_spending=insights.get("ie_spending", {}),
        )
        insights["midterm"] = midterm
    except Exception as e:
        logger.warning("Midterm analysis failed (non-fatal): {}", e)
        insights["midterm"] = {}

    # Compute trends from daily snapshots
    trends = compute_trends(snapshot, days=30)
    insights["trends"] = trends

    return {
        **snapshot,
        "deltas": delta_data["deltas"],
        "top_movers": delta_data["top_movers"],
        "delta_period": delta_period if prev else None,
        "previous_snapshot_date": prev["timestamp"][:10] if prev else None,
        "insights": insights,
    }


# ── Midterm Analysis Engine ──────────────────────────────────────────────

# 2026 Class II Senate seats — incumbent party (as of 2025)
# Source: https://en.wikipedia.org/wiki/2026_United_States_Senate_elections
SENATE_2026_INCUMBENTS = {
    "AL": "R", "AK": "R", "AR": "R", "CO": "D", "DE": "D",
    "GA": "D", "ID": "R", "IL": "D", "IA": "R", "KS": "R",
    "KY": "R", "LA": "R", "ME": "D", "MA": "D", "MI": "D",
    "MN": "D", "MS": "R", "MT": "R", "NE": "R", "NH": "D",
    "NJ": "D", "NM": "D", "NC": "R", "OK": "R", "OR": "D",
    "RI": "D", "SC": "R", "SD": "R", "TN": "R", "TX": "R",
    "VA": "D", "WV": "R", "WY": "R",
    # Special elections may add more
}

# Current Senate composition (as of 2025, post-2024 election)
CURRENT_SENATE = {"R": 53, "D": 47}  # includes independents caucusing with D
CURRENT_HOUSE = {"R": 220, "D": 215}  # approximate
CURRENT_GOVERNORS = {"R": 28, "D": 22}  # approximate


def compute_midterm_analysis(markets: list, party_control: dict,
                             ie_spending: dict = None) -> dict:
    """Compute comprehensive midterm analysis across all chambers.

    Returns dict with:
      - scoreboard: unified control odds + seat projections
      - flipping: chamber flip probability math
      - battleground: competitive races across all chambers ranked
      - money_flow: IE spending totals by chamber and party
      - correlation: cross-chamber state-level correlation
    """
    senate_races = _dedupe_state_races(markets, "senate")
    governor_races = _dedupe_state_races(markets, "governor")
    house_races = _dedupe_state_races(markets, "house")
    ie_spending = ie_spending or {}

    # ── 1. MIDTERM SCOREBOARD ──
    def _chamber_projection(races, incumbent_map=None):
        """Project seat outcomes from market prices."""
        d_safe, d_likely, d_lean = 0, 0, 0
        r_safe, r_likely, r_lean = 0, 0, 0
        tossup = 0
        for st, info in races.items():
            d, r = info["d_price"], info["r_price"]
            margin = abs(d - r)
            leader = "D" if d > r else "R"
            incumbent = (incumbent_map or {}).get(st, "?")
            is_flip = incumbent != "?" and leader != incumbent

            if margin >= 0.40:
                cat = "safe"
            elif margin >= 0.20:
                cat = "likely"
            elif margin >= 0.06:
                cat = "lean"
            else:
                cat = "tossup"

            if cat == "tossup":
                tossup += 1
            elif leader == "D":
                if cat == "safe": d_safe += 1
                elif cat == "likely": d_likely += 1
                else: d_lean += 1
            else:
                if cat == "safe": r_safe += 1
                elif cat == "likely": r_likely += 1
                else: r_lean += 1

        return {
            "d_safe": d_safe, "d_likely": d_likely, "d_lean": d_lean,
            "r_safe": r_safe, "r_likely": r_likely, "r_lean": r_lean,
            "tossup": tossup,
            "d_total": d_safe + d_likely + d_lean,
            "r_total": r_safe + r_likely + r_lean,
            "total_races": len(races),
        }

    senate_proj = _chamber_projection(senate_races, SENATE_2026_INCUMBENTS)
    governor_proj = _chamber_projection(governor_races)
    house_proj = _chamber_projection(house_races)

    # Senate: 33 Class II seats up + any specials. Non-tracked seats keep incumbent.
    seats_not_up_d = CURRENT_SENATE["D"] - sum(
        1 for st, p in SENATE_2026_INCUMBENTS.items() if p == "D"
    )
    seats_not_up_r = CURRENT_SENATE["R"] - sum(
        1 for st, p in SENATE_2026_INCUMBENTS.items() if p == "R"
    )
    senate_proj["d_projected_total"] = seats_not_up_d + senate_proj["d_total"]
    senate_proj["r_projected_total"] = seats_not_up_r + senate_proj["r_total"]
    senate_proj["tossup_projected_d"] = seats_not_up_d + senate_proj["d_total"] + senate_proj["tossup"]
    senate_proj["tossup_projected_r"] = seats_not_up_r + senate_proj["r_total"] + senate_proj["tossup"]

    scoreboard = {
        "senate": {
            **senate_proj,
            "control_d": party_control.get("senate", {}).get("democrat", 0),
            "control_r": party_control.get("senate", {}).get("republican", 0),
        },
        "house": {
            **house_proj,
            "control_d": party_control.get("house", {}).get("democrat", 0),
            "control_r": party_control.get("house", {}).get("republican", 0),
        },
        "governor": {
            **governor_proj,
        },
        "presidency": {
            "control_d": party_control.get("presidency", {}).get("democrat", 0),
            "control_r": party_control.get("presidency", {}).get("republican", 0),
        },
    }

    # ── 2. CHAMBER FLIPPING ANALYSIS ──
    def _flip_analysis(races, incumbent_map):
        """Identify seats most likely to flip parties."""
        flips = []
        holds = []
        for st, info in races.items():
            d, r = info["d_price"], info["r_price"]
            leader = "D" if d > r else "R"
            incumbent = incumbent_map.get(st, "?")
            margin = abs(d - r)
            flip_prob = 0

            if incumbent == "?":
                continue

            if leader != incumbent:
                # Market projects this seat flips
                flip_prob = max(d, r)
                flips.append({
                    "state": st, "from": incumbent, "to": leader,
                    "flip_prob": round(flip_prob, 4),
                    "margin": round(margin, 4),
                    "d_price": d, "r_price": r,
                    "platform": info.get("platform", ""),
                })
            elif margin < 0.20:
                # Incumbent leads but narrowly — at risk
                challenger = "D" if incumbent == "R" else "R"
                flip_prob = min(d, r)  # challenger's probability
                holds.append({
                    "state": st, "incumbent": incumbent,
                    "hold_prob": round(max(d, r), 4),
                    "challenger_prob": round(flip_prob, 4),
                    "margin": round(margin, 4),
                    "d_price": d, "r_price": r,
                    "platform": info.get("platform", ""),
                })

        flips.sort(key=lambda x: -x["flip_prob"])
        holds.sort(key=lambda x: x["margin"])
        return {"projected_flips": flips, "at_risk_holds": holds[:8]}

    senate_flips = _flip_analysis(senate_races, SENATE_2026_INCUMBENTS)

    # Net flip count
    d_flips = sum(1 for f in senate_flips["projected_flips"] if f["to"] == "D")
    r_flips = sum(1 for f in senate_flips["projected_flips"] if f["to"] == "R")
    net_shift = d_flips - r_flips  # positive = net D gain

    flipping = {
        "senate": {
            **senate_flips,
            "d_flips": d_flips, "r_flips": r_flips,
            "net_shift": net_shift,
            "net_shift_label": f"Net D+{net_shift}" if net_shift > 0 else f"Net R+{abs(net_shift)}" if net_shift < 0 else "No net change",
            "current": CURRENT_SENATE.copy(),
            "projected": {
                "D": CURRENT_SENATE["D"] + net_shift,
                "R": CURRENT_SENATE["R"] - net_shift,
            },
            "majority_threshold": 51,
        },
    }

    # ── 3. BATTLEGROUND DASHBOARD ──
    battleground = []
    for chamber, races, label in [
        ("senate", senate_races, "Senate"),
        ("governor", governor_races, "Governor"),
        ("house", house_races, "House"),
    ]:
        for key, info in races.items():
            margin = abs(info["r_price"] - info["d_price"])
            if margin < 0.25:  # Competitive = <25pp
                leader = "R" if info["r_price"] > info["d_price"] else "D"
                # For senate key == state code; for house key may be district.
                lookup_state = info.get("state") or key
                incumbent = SENATE_2026_INCUMBENTS.get(lookup_state, "?") if chamber == "senate" else "?"
                is_flip = incumbent != "?" and leader != incumbent
                battleground.append({
                    "state": lookup_state,
                    "district": info.get("district", ""),
                    "chamber": label,
                    "margin": round(margin, 4),
                    "margin_pp": round(margin * 100, 1),
                    "leader": leader,
                    "d_price": info["d_price"],
                    "r_price": info["r_price"],
                    "incumbent": incumbent,
                    "flip": is_flip,
                    "platform": info.get("platform", ""),
                    "volume": info.get("volume", 0),
                })
    battleground.sort(key=lambda x: x["margin"])

    # ── 4. MONEY FLOW SUMMARY ──
    money_flow = {"senate": {"pro_d": 0, "pro_r": 0, "total": 0},
                  "house": {"pro_d": 0, "pro_r": 0, "total": 0},
                  "presidential": {"pro_d": 0, "pro_r": 0, "total": 0},
                  "total": {"pro_d": 0, "pro_r": 0, "total": 0}}

    for key, race in ie_spending.items():
        office = race.get("office", "")
        pro_d = (race.get("dem_support", 0) or 0) + (race.get("dem_oppose", 0) or 0)
        pro_r = (race.get("rep_support", 0) or 0) + (race.get("rep_oppose", 0) or 0)
        total = race.get("total", 0) or 0

        bucket = "senate" if office == "senate" else "house" if office == "house" else "presidential"
        money_flow[bucket]["pro_d"] += pro_d
        money_flow[bucket]["pro_r"] += pro_r
        money_flow[bucket]["total"] += total
        money_flow["total"]["pro_d"] += pro_d
        money_flow["total"]["pro_r"] += pro_r
        money_flow["total"]["total"] += total

    # ── 5. CROSS-CHAMBER CORRELATION ──
    # States with both senate AND governor races — do they lean same way?
    correlation = []
    common_states = set(senate_races.keys()) & set(governor_races.keys())
    for st in sorted(common_states):
        s = senate_races[st]
        g = governor_races[st]
        s_lean = "D" if s["d_price"] > s["r_price"] else "R"
        g_lean = "D" if g["d_price"] > g["r_price"] else "R"
        s_margin = s["d_price"] - s["r_price"]  # positive = D leads
        g_margin = g["d_price"] - g["r_price"]

        split = s_lean != g_lean
        correlation.append({
            "state": st,
            "senate_lean": s_lean,
            "senate_d": s["d_price"],
            "senate_r": s["r_price"],
            "senate_margin": round(s_margin, 4),
            "governor_lean": g_lean,
            "governor_d": g["d_price"],
            "governor_r": g["r_price"],
            "governor_margin": round(g_margin, 4),
            "split": split,
            "divergence_pp": round(abs(s_margin - g_margin) * 100, 1),
        })

    # Sort: splits first, then by divergence
    correlation.sort(key=lambda x: (-int(x["split"]), -x["divergence_pp"]))

    # Summary stats
    aligned = sum(1 for c in correlation if not c["split"])
    split_count = sum(1 for c in correlation if c["split"])

    return {
        "scoreboard": scoreboard,
        "flipping": flipping,
        "battleground": battleground[:20],
        "battleground_total": len(battleground),
        "money_flow": money_flow,
        "correlation": {
            "states": correlation[:15],
            "total_common": len(correlation),
            "aligned": aligned,
            "split": split_count,
        },
    }


# ── Vault Markdown Generator ─────────────────────────────────────────────

def _compute_composite_score(control: dict) -> tuple[float, str, str]:
    """Compute a composite election sentiment score (0-100).

    Score reflects how competitive the overall landscape is:
    - 50 = perfectly competitive (all races ~50/50)
    - Higher = one party dominating (less uncertainty)
    - Lower = highly contested (maximum uncertainty)

    Returns (score, label, phase).
    """
    if not control:
        return 50.0, "Insufficient Data", "unknown"

    # Average the dominant-party probability across bodies
    margins = []
    for body in ["presidency", "senate", "house"]:
        probs = control.get(body, {})
        if probs:
            dominant = max(probs.values())
            margins.append(abs(dominant - 0.5) * 2)  # 0 = coin flip, 1 = certain

    if not margins:
        return 50.0, "Insufficient Data", "unknown"

    avg_margin = sum(margins) / len(margins)
    # Map to 0-100: higher margin = higher certainty score
    score = round(50 + avg_margin * 50, 1)

    if score >= 75:
        label, phase = "One-Party Dominant", "low volatility"
    elif score >= 62:
        label, phase = "Leaning — Moderate Certainty", "trending"
    elif score >= 55:
        label, phase = "Competitive — Elevated Uncertainty", "contested"
    else:
        label, phase = "Toss-Up — Maximum Uncertainty", "volatile"

    return score, label, phase


def _format_volume(vol: float) -> str:
    """Format volume with abbreviations."""
    if vol >= 1_000_000_000:
        return f"${vol / 1_000_000_000:.1f}B"
    if vol >= 1_000_000:
        return f"${vol / 1_000_000:.1f}M"
    if vol >= 1_000:
        return f"${vol / 1_000:.0f}K"
    return f"${vol:.0f}"


def generate_vault_markdown(report: dict) -> str:
    """Generate Intelligence Brief-style markdown report for Obsidian vault.

    Framework modeled after Virtuoso Weekly Intelligence Brief:
    composite score → narrative boxes → signal dimensions →
    what changed / what to watch → key races → methodology.
    """
    ts = report["timestamp"][:10]
    summary = report["summary"]
    control = summary.get("party_control", {})
    markets = report.get("markets", [])
    movers = report.get("top_movers", [])
    prev_date = report.get("previous_snapshot_date")

    score, label, phase = _compute_composite_score(control)

    # Determine overall lean
    pres = control.get("presidency", {})
    sen = control.get("senate", {})
    hou = control.get("house", {})
    r_bodies = sum(1 for b in [pres, sen, hou] if b.get("republican", 0) > b.get("democrat", 0))
    d_bodies = sum(1 for b in [pres, sen, hou] if b.get("democrat", 0) > b.get("republican", 0))
    lean = "Republican" if r_bodies > d_bodies else "Democrat" if d_bodies > r_bodies else "Split"

    lines = [
        "---",
        f'title: "Election Intelligence Brief — Week of {ts}"',
        "tags: [polyclawd, election, intelligence-brief, weekly-report]",
        f"created: {ts}",
        f"updated: {ts}",
        "status: active",
        "---",
        "",
        "# VIRTUOSO",
        f"## ELECTION INTELLIGENCE BRIEF · WEEK OF {ts.upper()}",
        "",
        "---",
        "",
        f"## {score} — {label}",
        "",
        f"**{phase.upper()}** · {summary['total_markets']} markets tracked · {summary['polymarket_count']} Polymarket + {summary['kalshi_count']} Kalshi",
        "",
    ]

    # ── Three narrative boxes ─────────────────────────────────────────
    # THIS WEEK
    biggest_mover = movers[0] if movers else None
    this_week_lines = []
    for body in ["presidency", "senate", "house"]:
        probs = control.get(body, {})
        r = probs.get("republican", 0)
        d = probs.get("democrat", 0)
        leader = "R" if r > d else "D"
        pct = max(r, d)
        this_week_lines.append(f"**{body.title()}**: {leader} {pct:.0%}")

    # Build narrative cells
    this_week_cell = (
        f"{'; '.join(this_week_lines)}. Overall lean: **{lean}**. "
        f"Total volume: {_format_volume(summary['total_volume'])}."
    )
    if biggest_mover:
        delta_sign = "+" if biggest_mover["delta"] > 0 else ""
        delta_str = f"{delta_sign}{biggest_mover['delta']:.1%}"
        signals_cell = (
            f"Biggest mover: **{biggest_mover['question'][:35]}** "
            f"({biggest_mover['outcome']} {delta_str}). "
            f"{summary['total_markets']} markets across {len(summary.get('by_race', {}))} race categories."
        )
    else:
        signals_cell = (
            f"No previous snapshot for delta analysis. "
            f"{summary['total_markets']} markets across {len(summary.get('by_race', {}))} race categories."
        )
    phase_note = f"Markets showing {phase}. " if phase != "unknown" else ""
    guidance_cell = (
        f"{phase_note}"
        f"Cross-platform coverage from Polymarket ({summary['polymarket_count']}) and Kalshi ({summary['kalshi_count']}). "
        f"Prediction markets remain the highest-signal source for election probability."
    )

    lines += [
        "| THIS WEEK | KEY SIGNALS | MARKET GUIDANCE |",
        "|-----------|-------------|-----------------|",
        f"| {this_week_cell} | {signals_cell} | {guidance_cell} |",
        "",
    ]

    # ── Signal Dimensions (party control by body) ─────────────────────
    lines += [
        "---",
        "",
        "## CONTROL PROBABILITIES",
        "",
        "| Body | Republican | Democrat | Leader | Margin |",
        "|------|-----------|----------|--------|--------|",
    ]

    for body in ["presidency", "senate", "house"]:
        probs = control.get(body, {})
        r = probs.get("republican", 0)
        d = probs.get("democrat", 0)
        leader = "R" if r > d else "D"
        margin = abs(r - d)
        status = "Lean" if margin < 0.10 else "Likely" if margin < 0.25 else "Safe"
        lines.append(f"| {body.title()} | {r:.1%} | {d:.1%} | **{leader}** | {margin:.1%} ({status}) |")

    # ── Key Metrics Row ───────────────────────────────────────────────
    lines += [
        "",
        "---",
        "",
        "## KEY METRICS",
        "",
        f"| Total Markets | Total Volume | Polymarket | Kalshi | Composite Score |",
        f"|--------------|-------------|------------|--------|-----------------|",
        f"| **{summary['total_markets']}** | **{_format_volume(summary['total_volume'])}** | {summary['polymarket_count']} | {summary['kalshi_count']} | **{score}** |",
        "",
    ]

    # ── Race Category Breakdown ───────────────────────────────────────
    lines += [
        "## RACE CATEGORY BREAKDOWN",
        "",
        "| Category | Markets | Share |",
        "|----------|---------|-------|",
    ]
    total = summary["total_markets"] or 1
    for cat, count in sorted(summary.get("by_race", {}).items(), key=lambda x: -x[1]):
        pct = count / total
        bar = "█" * int(pct * 20) + "░" * (20 - int(pct * 20))
        lines.append(f"| {cat.title()} | {count} | {bar} {pct:.0%} |")

    # ── What Changed / What to Watch ──────────────────────────────────
    lines += ["", "---", ""]

    if movers:
        lines += [
            f"## WHAT CHANGED THIS WEEK (vs {prev_date or 'N/A'})",
            "",
        ]
        for m in movers[:7]:
            arrow = "→ " if m["delta"] > 0 else "→ "
            sign = "+" if m["delta"] > 0 else ""
            lines.append(
                f"{arrow}**{m['question'][:55]}** — {m['outcome']} moved "
                f"{sign}{m['delta']:.1%} to {m['current']:.0%} ({m['platform']})"
            )
        lines.append("")
    else:
        lines += [
            "## WHAT CHANGED THIS WEEK",
            "",
            "> First snapshot — no week-over-week comparison available yet.",
            "> Run `/election-report` again next week to see deltas.",
            "",
        ]

    # What to watch
    lines += [
        "## WHAT TO WATCH",
        "",
    ]
    # Competitive races (closest margins)
    competitive = []
    for m in markets:
        if len(m["outcomes"]) >= 2 and m["race_category"] in ("senate", "governor", "presidential"):
            prices = sorted([o["price"] for o in m["outcomes"]], reverse=True)
            if len(prices) >= 2:
                margin = prices[0] - prices[1]
                if margin < 0.20 and prices[0] > 0.05:
                    competitive.append((margin, m))
    competitive.sort(key=lambda x: x[0])

    if competitive:
        lines.append("**Most competitive races** (margins < 20pp):")
        lines.append("")
        for margin, m in competitive[:7]:
            state = m.get("state", "")
            top_two = sorted(m["outcomes"], key=lambda o: -o["price"])[:2]
            lines.append(
                f"- **{state or m['race_category'].title()}** {m['question'][:45]} — "
                f"{top_two[0]['name']} {top_two[0]['price']:.0%} vs {top_two[1]['name']} {top_two[1]['price']:.0%} "
                f"(margin: {margin:.0%})"
            )
        lines.append("")

    # ── Presidential Detail ───────────────────────────────────────────
    pres_markets = [m for m in markets if m["race_category"] == "presidential"]
    if pres_markets:
        lines += ["---", "", "## PRESIDENTIAL RACE — TOP CANDIDATES", ""]
        for m in pres_markets:
            q = m["question"].lower()
            if "winner" in q and "2028" in q and m["platform"] == "polymarket":
                top = sorted(m["outcomes"], key=lambda o: -o["price"])[:10]
                lines.append(f"**{m['question'][:70]}**")
                lines.append("")
                lines.append("| Candidate | Probability |")
                lines.append("|-----------|-------------|")
                for o in top:
                    if o["price"] >= 0.02:
                        lines.append(f"| {o['name']} | {o['price']:.1%} |")
                lines.append("")
                break

    # ── Senate Races ──────────────────────────────────────────────────
    senate_markets = [m for m in markets if m["race_category"] == "senate" and m.get("state")]
    if senate_markets:
        lines += ["---", "", "## SENATE RACES BY STATE", ""]
        lines.append("| State | Leading | Prob | Platform |")
        lines.append("|-------|---------|------|----------|")
        senate_markets.sort(key=lambda m: m.get("state", "ZZ"))
        seen = set()
        for m in senate_markets:
            state = m.get("state", "")
            if state in seen:
                continue
            seen.add(state)
            top = max(m["outcomes"], key=lambda o: o["price"]) if m["outcomes"] else None
            if top and top["price"] >= 0.05:
                lines.append(f"| {state} | {top['name']} | {top['price']:.0%} | {m['platform']} |")

    # ── Governor Races ────────────────────────────────────────────────
    gov_markets = [m for m in markets if m["race_category"] == "governor" and m.get("state")]
    if gov_markets:
        lines += ["", "---", "", "## GOVERNOR RACES BY STATE", ""]
        lines.append("| State | Leading | Prob | Platform |")
        lines.append("|-------|---------|------|----------|")
        gov_markets.sort(key=lambda m: m.get("state", "ZZ"))
        seen = set()
        for m in gov_markets:
            state = m.get("state", "")
            if state in seen:
                continue
            seen.add(state)
            top = max(m["outcomes"], key=lambda o: o["price"]) if m["outcomes"] else None
            if top and top["price"] >= 0.05:
                lines.append(f"| {state} | {top['name']} | {top['price']:.0%} | {m['platform']} |")

    # ── Methodology ───────────────────────────────────────────────────
    lines += [
        "", "---", "",
        "## METHODOLOGY",
        "",
        f"**Data sources:** Polymarket Gamma API (tag: `us-presidential-election`, `elections`) + "
        f"Kalshi Events API (category: `Elections`, `Politics`). "
        f"Markets filtered for US-specific races via keyword matching and ticker prefix analysis.",
        "",
        f"**Composite score:** Weighted average of control margins across Presidency, Senate, and House. "
        f"Score of 50 = maximum uncertainty (all races 50/50). Higher = more certainty in outcome.",
        "",
        f"**Coverage:** {summary['total_markets']} markets across {len(summary.get('by_race', {}))} "
        f"race categories. Volume represents total dollars traded on tracked markets.",
        "",
        "*This is market intelligence derived from prediction market prices, not election advice.*",
        "",
        "---",
        "",
        "## Related Notes",
        "",
        "- [[ELECTION_PREDICTION_EDGE]] — Trading thesis and 3-layer edge stack",
        "- [[PAPER_PORTFOLIO]] — Paper trading system",
        "- [[DATABASE_SCHEMA]] — Trade data schema",
    ]

    return "\n".join(lines)


# ── CLI Entry Point ───────────────────────────────────────────────────────

if __name__ == "__main__":
    report = generate_report()
    print(f"\nElection Report: {report['summary']['total_markets']} markets")
    print(f"  Polymarket: {report['summary']['polymarket_count']}")
    print(f"  Kalshi:     {report['summary']['kalshi_count']}")
    print(f"  By race:    {report['summary']['by_race']}")
    print(f"  Party control: {report['summary'].get('party_control', {})}")
    if report["top_movers"]:
        print(f"\nTop movers:")
        for m in report["top_movers"][:5]:
            print(f"  {m['question'][:50]} | {m['outcome']} | {m['delta']:+.1%}")

    path = save_snapshot(report)
    print(f"\nSnapshot saved: {path}")
