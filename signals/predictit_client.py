#!/usr/bin/env python3
"""PredictIt API client — election prediction market data for cross-platform arbitrage."""

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from loguru import logger

from signals.election_tracker import classify_race, _extract_state

PREDICTIT_API = "https://www.predictit.org/api/marketdata/all/"
CACHE_DIR = Path(__file__).parent.parent / "storage" / "predictit_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 300  # 5 minutes (server refreshes every 60s)
PUSHED_CACHE_TTL = 3600  # 1 hour — pushed data from Mac is stale-but-usable longer

# Keywords to identify election markets
ELECTION_KEYWORDS = ["senate", "governor", "president", "congress", "house"]


def _predictit_get(timeout: int = 15) -> dict:
    """GET all PredictIt market data with multi-strategy fallback.

    Strategy chain:
    1. Fresh local cache (< 5 min) → return immediately
    2. Direct API fetch → works from residential IPs, blocked from some datacenters
    3. Pushed cache from Mac Mini (< 1 hour) → populated by push-predictit-cache cron
    4. Stale local cache (any age) → last resort, better than nothing
    5. Empty → no data available
    """
    cache_path = CACHE_DIR / "all_markets.json"
    pushed_cache_path = CACHE_DIR / "pushed_markets.json"

    # 1. Fresh local cache
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            with open(cache_path) as f:
                return json.load(f)

    # 2. Direct API fetch (may 403 from datacenter IPs)
    req = urllib.request.Request(
        PREDICTIT_API,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        with open(cache_path, "w") as f:
            json.dump(data, f)
        logger.info("PredictIt: direct fetch OK ({} markets)", len(data.get("markets", [])))
        return data
    except urllib.error.HTTPError as e:
        if e.code == 403:
            logger.info("PredictIt: direct fetch blocked (403), trying pushed cache")
        else:
            logger.warning("PredictIt API error: {}", e)
    except Exception as e:
        logger.warning("PredictIt API error: {}", e)

    # 3. Pushed cache from Mac Mini (fresher than stale local cache)
    if pushed_cache_path.exists():
        age = time.time() - pushed_cache_path.stat().st_mtime
        if age < PUSHED_CACHE_TTL:
            try:
                with open(pushed_cache_path) as f:
                    data = json.load(f)
                logger.info("PredictIt: using pushed cache ({:.0f}m old, {} markets)",
                            age / 60, len(data.get("markets", [])))
                return data
            except Exception:
                pass

    # 4. Stale local cache (any age)
    if cache_path.exists():
        try:
            with open(cache_path) as f:
                data = json.load(f)
            age = time.time() - cache_path.stat().st_mtime
            logger.warning("PredictIt: using stale cache ({:.0f}m old)", age / 60)
            return data
        except Exception:
            pass

    # 5. Stale pushed cache (any age — last resort)
    if pushed_cache_path.exists():
        try:
            with open(pushed_cache_path) as f:
                data = json.load(f)
            age = time.time() - pushed_cache_path.stat().st_mtime
            logger.warning("PredictIt: using stale pushed cache ({:.0f}m old)", age / 60)
            return data
        except Exception:
            pass

    logger.error("PredictIt: no data available (all strategies exhausted)")
    return {"markets": []}


def fee_adjusted_prob(price: float) -> float:
    """Convert PredictIt price to true probability accounting for fees.

    PredictIt charges 10% profit fee + 5% withdrawal fee.
    If you buy a share at price p and it resolves YES (payout $1):
        profit = 1 - p
        after 10% fee on profit: net = p + (1-p)*0.90
        after 5% withdrawal fee: net = (p + (1-p)*0.90) * 0.95
    Break-even when net == 1.0:
        (p + (1-p)*0.90) * 0.95 = 1
        p + 0.90 - 0.90p = 1/0.95
        0.10p + 0.90 = 1.05263
        0.10p = 0.15263
        p = 1.5263 (never break even if price > ~$0.85)

    For comparability, we compute the fee-adjusted break-even probability:
        true_prob = price / (price + (1 - price) * 0.90 * 0.95)
    This approximates what the market-implied probability would be without fees.
    """
    if price <= 0:
        return 0.0
    if price >= 1:
        return 1.0
    # Net payout if YES: (1 - price) * 0.90 profit after fee, times 0.95 withdrawal
    net_if_yes = (1 - price) * 0.90 * 0.95
    # Expected value comparison: true_prob * net_if_yes = (1 - true_prob) * price * 0.95
    # Simplify: the fee-adjusted probability
    adjusted = price / (price + net_if_yes)
    return round(adjusted, 4)


def _is_election_market(name: str) -> bool:
    """Check if a PredictIt market is election-related."""
    lower = name.lower()
    return any(kw in lower for kw in ELECTION_KEYWORDS)


def _bid_ask_spread(contract: dict) -> float | None:
    """Compute bid-ask spread from top-of-book prices. None if no book."""
    best_buy = contract.get("bestBuyYesCost")
    best_sell = contract.get("bestSellYesCost")
    if best_buy is not None and best_sell is not None and best_buy > 0:
        return round(best_buy - best_sell, 4)
    return None


def _intraday_delta(contract: dict) -> float | None:
    """Compute intraday move: lastTradePrice - lastClosePrice."""
    last = contract.get("lastTradePrice")
    close = contract.get("lastClosePrice")
    if last is not None and close is not None and close > 0:
        return round(last - close, 4)
    return None


def fetch_predictit_elections() -> list[dict]:
    """Fetch all PredictIt markets, filter to election-related, return normalized list."""
    data = _predictit_get()
    markets_raw = data.get("markets", [])
    results = []

    for market in markets_raw:
        market_name = market.get("name", "")
        if not _is_election_market(market_name):
            continue

        market_id = market.get("id", 0)
        contracts = market.get("contracts", [])

        for contract in contracts:
            contract_id = contract.get("id", 0)
            contract_name = contract.get("name", "")
            last_price = contract.get("lastTradePrice") or 0

            if last_price <= 0:
                continue  # Skip contracts with no trading

            full_name = f"{market_name} - {contract_name}"
            race_cat = classify_race(full_name)
            state = _extract_state(full_name)

            spread = _bid_ask_spread(contract)
            delta = _intraday_delta(contract)
            close_price = contract.get("lastClosePrice")

            results.append({
                "id": f"predictit_{market_id}_{contract_id}",
                "platform": "predictit",
                "question": contract_name,
                "market_name": market_name,
                "race_category": race_cat,
                "state": state,
                "outcomes": [
                    {"name": "Yes", "price": round(last_price, 4)},
                    {"name": "No", "price": round(1 - last_price, 4)},
                ],
                "volume": 0,  # PredictIt doesn't expose volume
                "end_date": contract.get("dateEnd", ""),
                "bid_ask_spread": spread,
                "last_close_price": close_price,
                "intraday_delta": delta,
                # Liquidity flag: spread > 5c or no book = thin
                "thin_market": spread is None or spread > 0.05,
            })

    logger.info("PredictIt: fetched {} election contracts ({} thin)",
                len(results), sum(1 for r in results if r["thin_market"]))
    return results


_DEM_KEYWORDS = {"democrat", "democratic", "democrats", "dem"}
_REP_KEYWORDS = {"republican", "republicans", "gop", "rep"}


def _detect_party(text: str) -> str | None:
    """Detect party from contract/question text. Returns 'D', 'R', or None."""
    lower = text.lower()
    is_dem = any(kw in lower for kw in _DEM_KEYWORDS)
    is_rep = any(kw in lower for kw in _REP_KEYWORDS)
    if is_dem and not is_rep:
        return "D"
    if is_rep and not is_dem:
        return "R"
    return None


def compute_predictit_spreads(
    predictit_markets: list[dict],
    polymarket_markets: list[dict],
    kalshi_markets: list[dict],
) -> list[dict]:
    """Find same-race divergences across PredictIt, Polymarket, and Kalshi.

    Normalizes all platforms to Democrat win probability before comparing.
    Each platform has per-party contracts — we detect party from the contract
    name/question and use the Dem contract's Yes price (or 1 - Rep Yes price).
    Filters out thin PredictIt markets (wide bid-ask spread).
    Returns list sorted by max spread magnitude.
    """

    # Skip primaries — intra-party races can't be normalized to D vs R
    skip_races = {"primary", "other"}

    def _build_dem_lookup(markets: list[dict], platform: str) -> dict:
        """Build (state, race) -> dem_probability lookup.

        For each race, prefer the Democrat contract's Yes price.
        If only a Republican contract exists, use 1 - price.
        """
        # Collect all party-tagged prices per (state, race)
        race_data: dict[tuple, dict] = {}  # key -> {"D": price, "R": price}
        for m in markets:
            state = m.get("state", "")
            race = m.get("race_category", "")
            if not state or race in skip_races:
                continue
            key = (state, race)

            # Detect party from question, outcomes, or contract name
            question = m.get("question", "") or m.get("market_name", "")
            party = _detect_party(question)

            # Also check outcome names for party keywords
            outcomes = m.get("outcomes", [])
            if party is None and outcomes:
                for o in outcomes:
                    oname = o.get("name", "")
                    party = _detect_party(oname)
                    if party:
                        break

            if party is None:
                continue  # Can't determine party — skip

            # Get the "Yes" price (probability this party wins)
            yes_price = None
            for o in outcomes:
                oname = o.get("name", "").lower()
                if oname == "yes" or party == "D" and "democrat" in oname or party == "R" and "republican" in oname:
                    yes_price = o.get("price", 0)
                    break
            if yes_price is None and outcomes:
                yes_price = outcomes[0].get("price", 0)

            if yes_price is not None and yes_price > 0:
                if key not in race_data:
                    race_data[key] = {}
                race_data[key][party] = yes_price

        # Convert to dem probability
        lookup = {}
        for key, parties in race_data.items():
            if "D" in parties:
                lookup[key] = parties["D"]
            elif "R" in parties:
                lookup[key] = 1.0 - parties["R"]
        return lookup

    # PredictIt lookup — skip thin markets, fee-adjust, normalize to dem prob
    pi_race_data: dict[tuple, dict] = {}
    pi_meta = {}
    for m in predictit_markets:
        if m.get("thin_market"):
            continue
        state = m.get("state", "")
        race = m.get("race_category", "")
        if not state or race in skip_races:
            continue
        key = (state, race)

        question = m.get("question", "")
        party = _detect_party(question)
        if party is None:
            party = _detect_party(m.get("market_name", ""))
        if party is None:
            continue

        outcomes = m.get("outcomes", [])
        if not outcomes:
            continue
        raw_price = outcomes[0].get("price", 0)
        if raw_price <= 0:
            continue

        adjusted = fee_adjusted_prob(raw_price)
        if key not in pi_race_data:
            pi_race_data[key] = {}
        pi_race_data[key][party] = adjusted
        pi_meta[key] = {
            "bid_ask_spread": m.get("bid_ask_spread"),
            "intraday_delta": m.get("intraday_delta"),
            "raw_price": raw_price,
        }

    pi_lookup = {}
    for key, parties in pi_race_data.items():
        if "D" in parties:
            pi_lookup[key] = parties["D"]
        elif "R" in parties:
            pi_lookup[key] = 1.0 - parties["R"]

    poly_lookup = _build_dem_lookup(polymarket_markets, "polymarket")
    kalshi_lookup = _build_dem_lookup(kalshi_markets, "kalshi")

    # Find all keys that appear in PredictIt + at least one other platform
    all_keys = set(pi_lookup.keys()) & (set(poly_lookup.keys()) | set(kalshi_lookup.keys()))

    spreads = []
    for key in all_keys:
        state, race = key
        pi_d = pi_lookup.get(key)
        poly_d = poly_lookup.get(key)
        kalshi_d = kalshi_lookup.get(key)

        prices = []
        if pi_d is not None:
            prices.append(pi_d)
        if poly_d is not None:
            prices.append(poly_d)
        if kalshi_d is not None:
            prices.append(kalshi_d)

        if len(prices) < 2:
            continue

        max_spread = max(prices) - min(prices)
        if max_spread < 0.01:
            continue  # Less than 1pp spread isn't notable

        meta = pi_meta.get(key, {})
        spreads.append({
            "state": state,
            "race": race,
            "predictit_d": round(pi_d, 4) if pi_d is not None else None,
            "polymarket_d": round(poly_d, 4) if poly_d is not None else None,
            "kalshi_d": round(kalshi_d, 4) if kalshi_d is not None else None,
            "max_spread_pp": round(max_spread * 100, 1),
            # PredictIt liquidity context
            "pi_bid_ask_spread": meta.get("bid_ask_spread"),
            "pi_intraday_delta": meta.get("intraday_delta"),
        })

    spreads.sort(key=lambda x: -x["max_spread_pp"])
    logger.info("PredictIt spreads: found {} cross-platform divergences (party-normalized)",
                len(spreads))
    return spreads
