#!/usr/bin/env python3
"""Polling data client — VoteHub API + RCP legacy for election trading signals.

Primary source: VoteHub API (free, no auth, active 2026 data)
Secondary: RealClearPolitics legacy JSON endpoints (approval tracking)

Poll-to-market latency arb: detect when new polls shift the average before
prediction markets reprice.
"""

import json
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode

from loguru import logger

# VoteHub API (free, no auth, comprehensive 2026 data)
VOTEHUB_API = "https://api.votehub.com"

# RealClearPolitics legacy JSON endpoints (backup)
RCP_JSON_BASE = "https://www.realclearpolitics.com/epolls/json"

CACHE_DIR = Path(__file__).parent.parent / "storage" / "rcp_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 3600  # 1 hour
SNAPSHOT_DIR = Path(__file__).parent.parent / "storage" / "poll_snapshots"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

# All 33 Class II Senate seats up in 2026
VOTEHUB_SENATE_SUBJECTS = [
    "2026 Alabama", "2026 Alaska", "2026 Arkansas", "2026 Colorado",
    "2026 Delaware", "2026 Georgia", "2026 Idaho", "2026 Illinois",
    "2026 Iowa", "2026 Kansas", "2026 Kentucky", "2026 Louisiana",
    "2026 Maine", "2026 Massachusetts", "2026 Michigan", "2026 Minnesota",
    "2026 Mississippi", "2026 Montana", "2026 Nebraska", "2026 New Hampshire",
    "2026 New Jersey", "2026 New Mexico", "2026 North Carolina",
    "2026 Oklahoma", "2026 Oregon", "2026 Rhode Island",
    "2026 South Carolina", "2026 South Dakota", "2026 Tennessee",
    "2026 Texas", "2026 Virginia", "2026 West Virginia", "2026 Wyoming",
    # Key primaries
    "2026 Texas Republican", "2026 Texas Democratic",
    "2026 Illinois Republican", "2026 Illinois Democratic",
    "2026 Georgia Republican", "2026 Georgia Democratic",
    "2026 North Carolina Republican", "2026 North Carolina Democratic",
]

# 2026 Governor races — all states holding gubernatorial elections
VOTEHUB_GOVERNOR_SUBJECTS = [
    "2026 Alabama", "2026 Alaska", "2026 Arizona", "2026 Arkansas",
    "2026 California", "2026 Colorado", "2026 Connecticut", "2026 Florida",
    "2026 Georgia", "2026 Hawaii", "2026 Idaho", "2026 Illinois",
    "2026 Iowa", "2026 Kansas", "2026 Maine", "2026 Maryland",
    "2026 Massachusetts", "2026 Michigan", "2026 Minnesota",
    "2026 Nebraska", "2026 Nevada", "2026 New Hampshire", "2026 New Mexico",
    "2026 New York", "2026 Ohio", "2026 Oklahoma", "2026 Oregon",
    "2026 Pennsylvania", "2026 Rhode Island", "2026 South Carolina",
    "2026 South Dakota", "2026 Tennessee", "2026 Texas", "2026 Vermont",
    "2026 Wisconsin", "2026 Wyoming",
]

# State name → abbreviation mapping for VoteHub subjects
_STATE_ABBREV = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}


def _subject_to_state(subject: str) -> str:
    """Extract state abbreviation from VoteHub subject like '2026 Georgia Republican'."""
    # Remove year prefix and party suffix
    rest = subject.replace("2026 ", "").strip()
    for suffix in (" Republican", " Democratic", " Independent"):
        rest = rest.replace(suffix, "")
    return _STATE_ABBREV.get(rest, "")


# Map VoteHub subjects to state codes for market matching (auto-generated)
SUBJECT_TO_STATE = {s: _subject_to_state(s) for s in VOTEHUB_SENATE_SUBJECTS}

# RCP legacy poll IDs (backup source)
RCP_POLL_IDS = {
    "trump_approval": (8117, "US", "presidential"),
}


def _http_get(url: str, timeout: int = 15) -> bytes | None:
    """Simple HTTP GET returning raw bytes."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "polyclawd/1.0 (election research)",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        logger.warning("HTTP error fetching {}: {}", url[:80], e)
        return None


def _cached_get_json(url: str, cache_key: str, ttl: int = CACHE_TTL) -> list | dict | None:
    """HTTP GET JSON with file-based caching and stale fallback."""
    cache_path = CACHE_DIR / f"{cache_key}.json"

    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < ttl:
            with open(cache_path) as f:
                return json.load(f)

    raw = _http_get(url)
    if raw is None:
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f)
        return None

    try:
        data = json.loads(raw)
        with open(cache_path, "w") as f:
            json.dump(data, f)
        return data
    except json.JSONDecodeError:
        logger.warning("Invalid JSON from {}", url[:80])
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f)
        return None


# ── VoteHub API ──────────────────────────────────────────────────────────

def fetch_votehub_polls(poll_type: str, subject: str = "",
                        from_date: str = "") -> list[dict]:
    """Fetch polls from VoteHub API.

    Args:
        poll_type: "generic-ballot", "us-senator", "approval", "governor"
        subject: e.g. "2026", "2026 Georgia", "Donald Trump"
        from_date: ISO date string to filter recent polls
    """
    params = {"poll_type": poll_type}
    if subject:
        params["subject"] = subject
    if from_date:
        params["from_date"] = from_date

    url = f"{VOTEHUB_API}/polls?{urlencode(params)}"
    cache_key = f"vh_{poll_type}_{subject}_{from_date}".replace(" ", "_")[:100]
    data = _cached_get_json(url, cache_key)
    return data if isinstance(data, list) else []


def fetch_votehub_generic_ballot(days_back: int = 90) -> list[dict]:
    """Fetch 2026 generic ballot polls."""
    from_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    return fetch_votehub_polls("generic-ballot", subject="2026", from_date=from_date)


def fetch_votehub_senate_polls(days_back: int = 180) -> dict[str, list[dict]]:
    """Fetch senate polls for all 33 Class II races + key primaries.

    Returns dict mapping state code to list of polls.
    """
    from_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    result = {}

    for subject in VOTEHUB_SENATE_SUBJECTS:
        state = _subject_to_state(subject)
        if not state:
            continue
        polls = fetch_votehub_polls("us-senator", subject=subject, from_date=from_date)
        if polls:
            if state not in result:
                result[state] = []
            result[state].extend(polls)
        time.sleep(0.3)  # Be polite

    return result


def fetch_votehub_governor_polls(days_back: int = 180) -> dict[str, list[dict]]:
    """Fetch governor polls for all 2026 gubernatorial races.

    Returns dict mapping state code to list of polls.
    """
    from_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    result = {}

    for subject in VOTEHUB_GOVERNOR_SUBJECTS:
        state = _subject_to_state(subject)
        if not state:
            continue
        polls = fetch_votehub_polls("governor", subject=subject, from_date=from_date)
        if polls:
            if state not in result:
                result[state] = []
            result[state].extend(polls)
        time.sleep(0.3)  # Be polite

    return result


def fetch_votehub_approval() -> list[dict]:
    """Fetch Trump approval polls."""
    from_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    return fetch_votehub_polls("approval", subject="Donald Trump", from_date=from_date)


def _compute_poll_average(polls: list[dict]) -> dict | None:
    """Compute weighted average from a list of polls.

    Weights: sample size × recency (exponential decay).
    Returns {dem_pct, rep_pct, margin, poll_count, latest_date, candidates}.

    For generic ballot: choices are "Dem"/"Rep".
    For senate races: choices are candidate names — we return top 2 candidates
    with their averaged pct (labeled as dem/rep by position for market matching).
    """
    if not polls:
        return None

    # Sort by end_date descending
    sorted_polls = sorted(polls, key=lambda p: p.get("end_date", ""), reverse=True)
    now = datetime.now(timezone.utc)

    # Detect if this is generic ballot (Dem/Rep) or candidate-name polls
    first_answers = sorted_polls[0].get("answers", [])
    first_choices = [a.get("choice", "").lower() for a in first_answers]
    is_generic = any(c in ("dem", "rep", "democrat", "republican") for c in first_choices)

    if is_generic:
        return _compute_generic_average(sorted_polls, now)
    else:
        return _compute_candidate_average(sorted_polls, now)


def _compute_generic_average(sorted_polls: list[dict], now: datetime) -> dict | None:
    """Compute average for generic ballot polls (Dem/Rep answers)."""
    dem_weighted = 0
    rep_weighted = 0
    total_weight = 0

    for p in sorted_polls[:20]:
        answers = p.get("answers", [])
        dem_pct = 0
        rep_pct = 0
        for a in answers:
            choice = a.get("choice", "").lower()
            pct = a.get("pct", 0) or 0
            if choice in ("dem", "democrat", "democratic", "d"):
                dem_pct = pct
            elif choice in ("rep", "republican", "gop", "r"):
                rep_pct = pct

        if dem_pct == 0 and rep_pct == 0:
            continue

        # Weight by sample size
        sample = p.get("sample_size") or 500
        # Weight by recency (half-life = 14 days)
        end_date_str = p.get("end_date", "")
        try:
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_old = (now - end_date).days
        except (ValueError, TypeError):
            days_old = 30
        recency = 2 ** (-days_old / 14)

        # Population weight: LV > RV > A
        pop = (p.get("population") or "").lower()
        pop_weight = 1.2 if pop == "lv" else 1.0 if pop == "rv" else 0.8

        weight = sample * recency * pop_weight
        dem_weighted += dem_pct * weight
        rep_weighted += rep_pct * weight
        total_weight += weight

    if total_weight == 0:
        return None

    dem_avg = dem_weighted / total_weight
    rep_avg = rep_weighted / total_weight

    return {
        "dem_pct": round(dem_avg, 1),
        "rep_pct": round(rep_avg, 1),
        "margin": round(dem_avg - rep_avg, 1),
        "poll_count": len(sorted_polls),
        "latest_date": sorted_polls[0].get("end_date", ""),
        "latest_pollster": sorted_polls[0].get("pollster", ""),
    }


def _compute_candidate_average(sorted_polls: list[dict], now: datetime) -> dict | None:
    """Compute average for senate/race polls with candidate names.

    Aggregates by candidate name across polls, returns top 2 as dem/rep
    (first = frontrunner labeled dem, second = challenger labeled rep
    for compatibility with market matching — actual party determined at
    the divergence computation stage).
    """
    candidate_weighted = {}  # name → (weighted_sum, total_weight)

    for p in sorted_polls[:20]:
        answers = p.get("answers", [])
        sample = p.get("sample_size") or 500
        end_date_str = p.get("end_date", "")
        try:
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_old = (now - end_date).days
        except (ValueError, TypeError):
            days_old = 30
        recency = 2 ** (-days_old / 14)
        pop = (p.get("population") or "").lower()
        pop_weight = 1.2 if pop == "lv" else 1.0 if pop == "rv" else 0.8
        weight = sample * recency * pop_weight

        for a in answers:
            name = a.get("choice", "").strip()
            pct = a.get("pct", 0) or 0
            if not name or pct <= 0:
                continue
            if name not in candidate_weighted:
                candidate_weighted[name] = [0.0, 0.0]
            candidate_weighted[name][0] += pct * weight
            candidate_weighted[name][1] += weight

    if not candidate_weighted:
        return None

    # Compute averages and sort by pct descending
    candidates = []
    for name, (w_sum, w_total) in candidate_weighted.items():
        if w_total > 0:
            candidates.append({"name": name, "pct": round(w_sum / w_total, 1)})
    candidates.sort(key=lambda c: c["pct"], reverse=True)

    if len(candidates) < 2:
        return None

    leader = candidates[0]
    runner = candidates[1]

    return {
        "dem_pct": leader["pct"],  # Frontrunner (party TBD)
        "rep_pct": runner["pct"],  # Runner-up (party TBD)
        "margin": round(leader["pct"] - runner["pct"], 1),
        "poll_count": len(sorted_polls),
        "latest_date": sorted_polls[0].get("end_date", ""),
        "latest_pollster": sorted_polls[0].get("pollster", ""),
        "candidates": candidates[:5],  # Top 5 for display
    }


def _detect_recent_shift(polls: list[dict], days: int = 7) -> dict | None:
    """Detect if recent polls shifted vs older polls.

    Compares average of polls from last `days` to average of older polls.
    Returns shift info if margin changed by >2pp.
    """
    if len(polls) < 3:
        return None

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    recent = [p for p in polls if (p.get("end_date", "") or "") >= cutoff]
    older = [p for p in polls if (p.get("end_date", "") or "") < cutoff]

    if not recent or not older:
        return None

    recent_avg = _compute_poll_average(recent)
    older_avg = _compute_poll_average(older)

    if not recent_avg or not older_avg:
        return None

    margin_shift = recent_avg["margin"] - older_avg["margin"]
    if abs(margin_shift) < 2.0:
        return None

    return {
        "recent_margin": recent_avg["margin"],
        "older_margin": older_avg["margin"],
        "shift": round(margin_shift, 1),
        "direction": "D improving" if margin_shift > 0 else "R improving",
        "recent_polls": len(recent),
        "older_polls": len(older),
        "latest_date": recent_avg["latest_date"],
    }


# ── RCP Legacy (backup) ──────────────────────────────────────────────────

def _parse_rcp_jsonp(raw: bytes) -> dict | None:
    """Parse JSONP response from RCP legacy endpoint."""
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    start = text.find("(")
    end = text.rfind(")")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start + 1:end])
    except json.JSONDecodeError:
        return None


def fetch_rcp_historical(poll_id: int, cache_key: str = "") -> dict | None:
    """Fetch historical polling averages from RCP legacy JSON endpoint."""
    url = f"{RCP_JSON_BASE}/{poll_id}_historical.js"
    cache_k = cache_key or f"rcp_{poll_id}"
    cache_path = CACHE_DIR / f"{cache_k}.json"

    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            with open(cache_path) as f:
                return json.load(f)

    raw = _http_get(url)
    data = _parse_rcp_jsonp(raw)
    if data:
        with open(cache_path, "w") as f:
            json.dump(data, f)
    elif cache_path.exists():
        with open(cache_path) as f:
            return json.load(f)

    return data


# ── Main Orchestration ────────────────────────────────────────────────────

def fetch_all_polls() -> dict:
    """Fetch all available poll data from VoteHub + RCP.

    Returns dict with generic ballot, senate averages, governor averages, approval, and shifts.
    """
    result = {
        "generic_ballot": {},
        "senate_averages": {},
        "governor_averages": {},
        "approval": {},
        "poll_shifts": [],
        "raw_polls": {},
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    # 1. VoteHub: Generic ballot
    try:
        gb_polls = fetch_votehub_generic_ballot()
        if gb_polls:
            avg = _compute_poll_average(gb_polls)
            shift = _detect_recent_shift(gb_polls)
            result["generic_ballot"] = {
                "average": avg,
                "poll_count": len(gb_polls),
                "shift": shift,
            }
            result["raw_polls"]["generic_ballot"] = gb_polls[-10:]
            if shift:
                shift["label"] = "generic_ballot"
                shift["state"] = "US"
                shift["race_category"] = "house"
                result["poll_shifts"].append(shift)
    except Exception as e:
        logger.warning("VoteHub generic ballot failed: {}", e)

    # 2. VoteHub: Senate races
    try:
        senate_polls = fetch_votehub_senate_polls()
        for state, polls in senate_polls.items():
            avg = _compute_poll_average(polls)
            shift = _detect_recent_shift(polls)
            result["senate_averages"][state] = {
                "average": avg,
                "poll_count": len(polls),
                "shift": shift,
            }
            result["raw_polls"][f"senate_{state}"] = polls[-5:]
            if shift:
                shift["label"] = f"senate_{state}"
                shift["state"] = state
                shift["race_category"] = "senate"
                result["poll_shifts"].append(shift)
    except Exception as e:
        logger.warning("VoteHub senate polls failed: {}", e)

    # 3. VoteHub: Governor races
    try:
        governor_polls = fetch_votehub_governor_polls()
        for state, polls in governor_polls.items():
            avg = _compute_poll_average(polls)
            shift = _detect_recent_shift(polls)
            result["governor_averages"][state] = {
                "average": avg,
                "poll_count": len(polls),
                "shift": shift,
            }
            result["raw_polls"][f"governor_{state}"] = polls[-5:]
            if shift:
                shift["label"] = f"governor_{state}"
                shift["state"] = state
                shift["race_category"] = "governor"
                result["poll_shifts"].append(shift)
    except Exception as e:
        logger.warning("VoteHub governor polls failed: {}", e)

    # 4. VoteHub: Trump approval
    try:
        approval_polls = fetch_votehub_approval()
        if approval_polls:
            avg = _compute_poll_average(approval_polls)
            result["approval"] = {
                "average": avg,
                "poll_count": len(approval_polls),
            }
    except Exception as e:
        logger.warning("VoteHub approval failed: {}", e)

    return result


def compute_poll_market_divergence(poll_data: dict, markets: list[dict]) -> list[dict]:
    """Compare polling averages to prediction market prices.

    The core arb signal: when polls shift but markets haven't repriced yet.
    """
    divergences = []

    # Generic ballot → senate control market
    gb = poll_data.get("generic_ballot", {}).get("average")
    if gb and gb.get("margin") is not None:
        margin = gb["margin"]
        # Generic ballot margin → implied senate control probability
        # Historical: D+3 ≈ 50/50 senate, each 1pp above → ~3pp probability shift
        implied_d_control = 0.50 + (margin - 3) * 0.03
        implied_d_control = max(0.05, min(0.95, implied_d_control))

        for m in markets:
            q = m.get("question", "").lower()
            if m.get("platform") != "polymarket":
                continue
            if "senate" in q and "control" in q and "democrat" in q:
                market_price = 0
                for o in m.get("outcomes", []):
                    if o.get("name", "").lower() in ("yes", "democrat", "democrats"):
                        market_price = o.get("price", 0)
                        break
                if market_price <= 0:
                    continue
                div = abs(implied_d_control - market_price)
                if div < 0.03:
                    continue
                direction = "underpriced" if implied_d_control > market_price else "overpriced"
                divergences.append({
                    "label": "generic_ballot → D senate control",
                    "state": "US",
                    "race_category": "senate",
                    "poll_dem_pct": gb["dem_pct"],
                    "poll_rep_pct": gb["rep_pct"],
                    "poll_margin": margin,
                    "implied_prob": round(implied_d_control, 3),
                    "market_price": market_price,
                    "divergence_pp": round(div * 100, 1),
                    "direction": direction,
                    "market_id": m.get("id", ""),
                    "market_question": m.get("question", "")[:100],
                    "detail": (
                        f"Generic ballot D+{margin:.1f} → implied {implied_d_control*100:.0f}% D senate control, "
                        f"market at {market_price*100:.0f}% ({direction} by {div*100:.1f}pp)"
                    ),
                })
                break

    # State senate polls → individual race markets
    for state, info in poll_data.get("senate_averages", {}).items():
        avg = info.get("average")
        if not avg or avg.get("margin") is None:
            continue

        margin = avg["margin"]
        # State poll margin → win probability
        # Each 1pp poll lead ≈ 2pp win probability shift from 50%
        implied_prob = 0.50 + (margin / 100) * 2
        implied_prob = max(0.05, min(0.95, implied_prob))

        for m in markets:
            if m.get("state") != state or m.get("race_category") != "senate":
                continue

            # Get D price from market
            dem_price = None
            for o in m.get("outcomes", []):
                name = o.get("name", "").lower()
                if "democrat" in name or "(d)" in name:
                    dem_price = o.get("price", 0)
                    break
            if dem_price is None:
                continue

            div = abs(implied_prob - dem_price)
            if div < 0.03:
                continue

            direction = "underpriced" if implied_prob > dem_price else "overpriced"
            divergences.append({
                "label": f"{state} senate polls",
                "state": state,
                "race_category": "senate",
                "poll_dem_pct": avg["dem_pct"],
                "poll_rep_pct": avg["rep_pct"],
                "poll_margin": margin,
                "implied_prob": round(implied_prob, 3),
                "market_price": dem_price,
                "divergence_pp": round(div * 100, 1),
                "direction": direction,
                "market_id": m.get("id", ""),
                "market_question": m.get("question", "")[:100],
                "detail": (
                    f"{state} polls: D {avg['dem_pct']:.1f}% vs R {avg['rep_pct']:.1f}% "
                    f"({margin:+.1f}pp) → implied {implied_prob*100:.0f}%, "
                    f"market {dem_price*100:.0f}% ({direction} by {div*100:.1f}pp)"
                ),
            })
            break  # One market per state

    # Governor polls → governor race markets
    for state, info in poll_data.get("governor_averages", {}).items():
        avg = info.get("average")
        if not avg or avg.get("margin") is None:
            continue

        margin = avg["margin"]
        implied_prob = 0.50 + (margin / 100) * 2
        implied_prob = max(0.05, min(0.95, implied_prob))

        for m in markets:
            if m.get("state") != state or m.get("race_category") != "governor":
                continue

            dem_price = None
            for o in m.get("outcomes", []):
                name = o.get("name", "").lower()
                if "democrat" in name or "(d)" in name:
                    dem_price = o.get("price", 0)
                    break
            if dem_price is None:
                continue

            div = abs(implied_prob - dem_price)
            if div < 0.03:
                continue

            direction = "underpriced" if implied_prob > dem_price else "overpriced"
            divergences.append({
                "label": f"{state} governor polls",
                "state": state,
                "race_category": "governor",
                "poll_dem_pct": avg["dem_pct"],
                "poll_rep_pct": avg["rep_pct"],
                "poll_margin": margin,
                "implied_prob": round(implied_prob, 3),
                "market_price": dem_price,
                "divergence_pp": round(div * 100, 1),
                "direction": direction,
                "market_id": m.get("id", ""),
                "market_question": m.get("question", "")[:100],
                "detail": (
                    f"{state} gov polls: D {avg['dem_pct']:.1f}% vs R {avg['rep_pct']:.1f}% "
                    f"({margin:+.1f}pp) → implied {implied_prob*100:.0f}%, "
                    f"market {dem_price*100:.0f}% ({direction} by {div*100:.1f}pp)"
                ),
            })
            break

    divergences.sort(key=lambda x: x["divergence_pp"], reverse=True)
    return divergences


def save_poll_snapshot(poll_data: dict) -> None:
    """Save daily poll snapshot for trend tracking."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = SNAPSHOT_DIR / f"polls_{date_str}.json"
    # Don't save raw_polls to snapshot (too large)
    snapshot = {k: v for k, v in poll_data.items() if k != "raw_polls"}
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2)
    logger.info("Saved poll snapshot to {}", path)


def build_rcp_overlay(markets: list[dict] | None = None) -> dict:
    """Build polling overlay for election report — called from generate_report().

    Returns dict with poll_data, poll_shifts, and poll_market_divergences.
    """
    try:
        poll_data = fetch_all_polls()
        divergences = []
        if markets:
            divergences = compute_poll_market_divergence(poll_data, markets)

        try:
            save_poll_snapshot(poll_data)
        except Exception:
            pass

        shifts = poll_data.get("poll_shifts", [])
        senate_count = len(poll_data.get("senate_averages", {}))
        governor_count = len(poll_data.get("governor_averages", {}))
        gb_count = poll_data.get("generic_ballot", {}).get("poll_count", 0)
        logger.info("Poll overlay: generic_ballot={} polls, {} senate states, {} governor states, {} shifts, {} divergences",
                     gb_count, senate_count, governor_count, len(shifts), len(divergences))

        return {
            "poll_data": poll_data,
            "poll_shifts": shifts,
            "poll_market_divergences": divergences,
        }
    except Exception as e:
        logger.warning("Poll overlay failed: {}", e)
        return {
            "poll_data": {},
            "poll_shifts": [],
            "poll_market_divergences": [],
        }
