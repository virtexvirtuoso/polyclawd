#!/usr/bin/env python3
"""Smart Money analysis — Polymarket YES holders + FEC cross-reference + activity tracking."""

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

HOLDERS_API = "https://data-api.polymarket.com/holders"
GAMMA_API = "https://gamma-api.polymarket.com/markets"
CACHE_DIR = Path(__file__).parent.parent / "storage" / "smart_money_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR = Path(__file__).parent.parent / "storage" / "smart_money_history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 1800  # 30 min

# Meme/novelty candidates to exclude
_MEME_NAMES = {
    "lebron", "kardashian", "kanye", "mrbeast", "clooney", "oprah",
    "rihanna", "swift", "beyonce", "elon musk", "dwayne", "the rock",
    "mark cuban", "chelsea clinton", "kim k", "taylor swift",
}


def _fetch_holders(condition_id: str, limit: int = 20) -> list[dict]:
    """Fetch top holders for a Polymarket market by condition_id."""
    cache_key = f"holders_{condition_id[:20]}".replace("/", "_")
    cache_path = CACHE_DIR / f"{cache_key}.json"
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            with open(cache_path) as f:
                return json.load(f)

    url = f"{HOLDERS_API}?market={condition_id}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        with open(cache_path, "w") as f:
            json.dump(data, f)
        return data
    except Exception as e:
        logger.warning("Holders API error for {}: {}", condition_id[:20], e)
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f)
        return []


def _fetch_market_tokens(market_id: str) -> dict | None:
    """Fetch conditionId and clobTokenIds for a Polymarket market."""
    cache_path = CACHE_DIR / f"tokens_{market_id}.json"
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < 86400:  # 24h cache
            with open(cache_path) as f:
                return json.load(f)

    url = f"{GAMMA_API}/{market_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        result = {
            "conditionId": data.get("conditionId", ""),
            "clobTokenIds": json.loads(data.get("clobTokenIds") or "[]"),
            "outcomes": data.get("outcomes", ""),
            "question": data.get("question", ""),
        }
        with open(cache_path, "w") as f:
            json.dump(result, f)
        return result
    except Exception as e:
        logger.warning("Gamma market fetch error for {}: {}", market_id, e)
        return None


def _extract_yes_holders(holders_data: list[dict]) -> list[dict]:
    """Extract YES holders (outcomeIndex=0) from holders response."""
    holders = []
    for token_group in holders_data:
        for h in token_group.get("holders", []):
            if h.get("outcomeIndex", 0) != 0:
                continue
            name = h.get("pseudonym") or h.get("name") or ""
            if not name or (not h.get("displayUsernamePublic", True) and not h.get("pseudonym")):
                name = h.get("proxyWallet", "")[:10] + "..."
            holders.append({
                "name": name,
                "shares": round(h.get("amount", 0), 0),
                "wallet": h.get("proxyWallet", ""),
            })
    return holders


def _select_target_markets(election_markets: list[dict], top_n: int) -> list[dict]:
    """Select the most important Polymarket markets to track holders for."""
    candidates = []
    for m in election_markets:
        if m.get("platform") != "polymarket":
            continue
        if m.get("volume", 0) < 50000:
            continue
        cat = m.get("race_category", "")
        if cat not in ("senate", "presidential", "governor", "primary"):
            continue
        q_lower = m.get("question", "").lower()
        if any(meme in q_lower for meme in _MEME_NAMES):
            continue
        candidates.append(m)

    # Prioritize: state races > control markets > primaries, then by volume
    def _sort_key(m):
        has_state = 1 if m.get("state") else 0
        cat = m.get("race_category", "")
        cat_rank = {"senate": 0, "governor": 1, "presidential": 2, "primary": 3}.get(cat, 4)
        return (-has_state, cat_rank, -m.get("volume", 0))
    candidates.sort(key=_sort_key)
    return candidates[:top_n]


def fetch_smart_money(election_markets: list[dict], top_n: int = 10) -> list[dict]:
    """Fetch YES holder data for top Polymarket election markets.

    Returns list of markets with their top YES holders, sorted by total YES shares.
    """
    targets = _select_target_markets(election_markets, top_n)

    results = []
    for mkt in targets:
        tokens = _fetch_market_tokens(mkt["id"])
        if not tokens or not tokens.get("conditionId"):
            continue

        holders_data = _fetch_holders(tokens["conditionId"])
        if not holders_data:
            continue

        yes_holders = _extract_yes_holders(holders_data)
        if not yes_holders:
            continue

        total_yes = sum(h["shares"] for h in yes_holders)
        top5_total = sum(h["shares"] for h in yes_holders[:5])
        concentration = top5_total / total_yes if total_yes > 0 else 0
        whale_pct = (yes_holders[0]["shares"] / total_yes) if total_yes > 0 else 0

        # Get current YES price from outcomes
        outcomes = mkt.get("outcomes", [])
        yes_price = outcomes[0].get("price", 0) if outcomes else 0

        results.append({
            "market_id": mkt["id"],
            "question": mkt["question"],
            "slug": mkt.get("slug", ""),
            "state": mkt.get("state", ""),
            "race_category": mkt.get("race_category", ""),
            "volume": mkt.get("volume", 0),
            "yes_price": yes_price,
            "yes_holders": yes_holders[:10],
            "yes_total_shares": round(total_yes),
            "top5_concentration": round(concentration, 3),
            "whale_pct": round(whale_pct, 3),
            "holder_count": len(yes_holders),
        })

        time.sleep(0.3)  # Rate limit

    # Sort by total YES shares (most conviction first)
    results.sort(key=lambda r: r["yes_total_shares"], reverse=True)
    return results


def cross_reference_fec(smart_money: list[dict], fec_fundraising: dict,
                         ie_spending: dict) -> list[dict]:
    """Cross-reference YES holder positions with FEC campaign finance.

    Signals:
    - ALIGNED: Smart money YES + FEC cash both favor same party
    - DIVERGE: Smart money YES bets opposite to FEC cash flow
    - WHALE RISK: Single holder >30% of YES shares
    """
    signals = []
    for sm in smart_money:
        state = sm.get("state", "")
        if not state:
            continue

        fec = fec_fundraising.get(state, {})
        ie = ie_spending.get(f"{state}_S", {})
        if not fec and not ie:
            continue

        # Determine party from question
        q = sm.get("question", "").lower()
        if "democrat" in q:
            yes_party = "D"
        elif "republican" in q:
            yes_party = "R"
        else:
            continue

        fec_advantage = fec.get("cash_advantage", "?")
        dem_cash = fec.get("dem_receipts", 0) or 0
        rep_cash = fec.get("rep_receipts", 0) or 0
        ie_net = ie.get("net_advantage", "?") if ie else "?"

        # Alignment
        fec_aligned = yes_party == fec_advantage
        ie_aligned = yes_party == ie_net

        if fec_advantage == "?" and ie_net == "?":
            continue

        if fec_aligned and (ie_aligned or ie_net == "?"):
            strength = "aligned"
            detail = f"YES bettors + FEC cash both favor {yes_party} in {state}"
        elif not fec_aligned and fec_advantage != "?":
            strength = "divergence"
            detail = f"YES bettors back {yes_party} but FEC cash favors {fec_advantage} in {state}"
        else:
            strength = "mixed"
            detail = f"{state}: YES→{yes_party}, FEC→{fec_advantage}, IE→{ie_net}"

        signals.append({
            "state": state,
            "question": sm["question"][:55],
            "yes_party": yes_party,
            "yes_shares": sm["yes_total_shares"],
            "yes_price": sm.get("yes_price", 0),
            "fec_advantage": fec_advantage,
            "ie_advantage": ie_net,
            "dem_cash": dem_cash,
            "rep_cash": rep_cash,
            "strength": strength,
            "detail": detail,
            "whale_pct": sm["whale_pct"],
            "concentration": sm["top5_concentration"],
        })

    # Divergences first (most actionable)
    order = {"divergence": 0, "aligned": 1, "mixed": 2}
    signals.sort(key=lambda s: (order.get(s["strength"], 9), -s["yes_shares"]))
    return signals


def build_smart_money_overlay(markets: list[dict], fec_fundraising: dict,
                               ie_spending: dict) -> dict:
    """Build complete smart money overlay for election report."""
    try:
        smart_money = fetch_smart_money(markets, top_n=10)
        fec_signals = cross_reference_fec(smart_money, fec_fundraising, ie_spending)
        whale_activity = analyze_whale_activity(smart_money)
        logger.info("Smart money: {} markets analyzed, {} FEC cross-signals, {} whale changes",
                     len(smart_money), len(fec_signals), len(whale_activity))
        return {
            "smart_money": smart_money,
            "fec_cross_signals": fec_signals,
            "whale_activity": whale_activity,
        }
    except Exception as e:
        logger.warning("Smart money overlay failed: {}", e)
        return {"smart_money": [], "fec_cross_signals": [], "whale_activity": []}


# ── Whale Activity Over Time ──────────────────────────────────────────

def save_whale_snapshot(smart_money: list[dict]) -> None:
    """Save timestamped snapshot of whale holder data for historical tracking."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snapshot_path = HISTORY_DIR / f"{ts}.json"

    # Compact: only store market_id, top holders, shares, whale_pct
    records = []
    for sm in smart_money:
        records.append({
            "market_id": sm["market_id"],
            "question": sm.get("question", "")[:60],
            "state": sm.get("state", ""),
            "yes_total_shares": sm.get("yes_total_shares", 0),
            "whale_pct": sm.get("whale_pct", 0),
            "top5_concentration": sm.get("top5_concentration", 0),
            "yes_price": sm.get("yes_price", 0),
            "top_holders": [
                {"name": h["name"], "shares": h["shares"]}
                for h in sm.get("yes_holders", [])[:5]
            ],
        })

    snapshot = {"timestamp": datetime.now(timezone.utc).isoformat(), "markets": records}

    # Append to daily file (multiple snapshots per day)
    existing = []
    if snapshot_path.exists():
        try:
            with open(snapshot_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, ValueError):
            existing = []
    if not isinstance(existing, list):
        existing = [existing]
    existing.append(snapshot)

    with open(snapshot_path, "w") as f:
        json.dump(existing, f, indent=1)

    logger.debug("Whale snapshot saved: {} markets to {}", len(records), snapshot_path.name)


def _load_recent_snapshots(days: int = 7) -> list[dict]:
    """Load whale snapshots from the last N days."""
    from datetime import timedelta
    snapshots = []
    now = datetime.now(timezone.utc)
    for i in range(days):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        path = HISTORY_DIR / f"{d}.json"
        if not path.exists():
            continue
        try:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, list):
                snapshots.extend(data)
            else:
                snapshots.append(data)
        except (json.JSONDecodeError, ValueError):
            continue
    return snapshots


def analyze_whale_activity(current_smart_money: list[dict]) -> list[dict]:
    """Detect accumulation/dumping by comparing current vs historical holder data.

    Returns list of activity signals with direction and magnitude.
    """
    # Save current snapshot
    if current_smart_money:
        save_whale_snapshot(current_smart_money)

    history = _load_recent_snapshots(days=7)
    if len(history) < 2:
        return []

    # Get earliest snapshot for comparison
    earliest = history[-1]
    earliest_markets = {m["market_id"]: m for m in earliest.get("markets", [])}

    activities = []
    for sm in current_smart_money:
        mid = sm["market_id"]
        prev = earliest_markets.get(mid)
        if not prev:
            continue

        # Compare total YES shares
        curr_shares = sm.get("yes_total_shares", 0)
        prev_shares = prev.get("yes_total_shares", 0)
        if prev_shares == 0:
            continue

        change_pct = ((curr_shares - prev_shares) / prev_shares) * 100

        # Compare whale concentration
        curr_whale = sm.get("whale_pct", 0)
        prev_whale = prev.get("whale_pct", 0)
        whale_delta = curr_whale - prev_whale

        # Compare price
        curr_price = sm.get("yes_price", 0)
        prev_price = prev.get("yes_price", 0)
        price_delta = curr_price - prev_price

        # Only flag significant changes (>10% share change or >5pp whale shift)
        if abs(change_pct) < 10 and abs(whale_delta) < 0.05:
            continue

        if change_pct > 10:
            direction = "accumulating"
            detail = f"YES shares up {change_pct:.0f}% over 7d"
        elif change_pct < -10:
            direction = "dumping"
            detail = f"YES shares down {abs(change_pct):.0f}% over 7d"
        elif whale_delta > 0.05:
            direction = "whale_growing"
            detail = f"Top holder share grew {whale_delta*100:.1f}pp"
        else:
            direction = "whale_shrinking"
            detail = f"Top holder share dropped {abs(whale_delta)*100:.1f}pp"

        activities.append({
            "market_id": mid,
            "question": sm.get("question", "")[:55],
            "state": sm.get("state", ""),
            "direction": direction,
            "detail": detail,
            "share_change_pct": round(change_pct, 1),
            "whale_delta": round(whale_delta, 3),
            "price_delta": round(price_delta, 3),
            "current_shares": curr_shares,
            "previous_shares": prev_shares,
            "current_whale_pct": curr_whale,
        })

    # Sort by magnitude of change
    activities.sort(key=lambda a: abs(a["share_change_pct"]), reverse=True)
    return activities
