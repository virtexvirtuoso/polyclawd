"""Election signal generator — converts election analysis into tradeable signals.

Strategies:
1. Cross-platform arb: Polymarket vs Kalshi vs PredictIt spread >5pp
2. Money vs odds: FEC cash diverges from market odds >10pp
3. Primary overround: Multi-candidate markets with probability sum != 100%
4. Momentum: Daily movers >3pp with volume confirmation
5. IE spending surge: Super PAC spending spikes signal smart money direction
6. GDELT narrative shift: News sentiment changes precede market moves
7. Poll-to-market divergence: Polling averages vs market prices
8. FEC eFiling real-time: Super PAC spending alerts within minutes
9. Wiki attention spike: Wikipedia pageview z-score >2 + GDELT tone
10. Google Trends spike: Search interest z-score >2 + GDELT tone + Wiki cross-confirm
11. Smart money divergence: Polymarket YES holders disagree with FEC campaign cash
12. Whale concentration: Single holder >30% of YES shares = thin market risk
13. eFiling cash momentum: Fresh cash-on-hand from eFiling diverges from quarterly data
14. Economic macro: FRED indicators (CPI, unemployment, GDP, gas) favor/hurt incumbent

Confluence scoring stacks confirming signals on the same market+side:
+8 confidence per extra source (max +24), source_agreement drives Kelly +10%/source.
"""

import logging
import time

logger = logging.getLogger(__name__)

# Cache to avoid re-generating on every aggregation call
_cache = {"signals": [], "ts": 0}
_CACHE_TTL = 300  # 5 minutes


def generate_election_signals() -> list[dict]:
    """Generate tradeable signals from election analysis data.

    Returns list of signal dicts compatible with aggregate_all_signals().
    """
    now = time.time()
    if _cache["signals"] and (now - _cache["ts"]) < _CACHE_TTL:
        return _cache["signals"]

    signals = []

    try:
        from signals.election_tracker import generate_report
        # Use cached report from the election endpoint if available
        import asyncio
        from api.routes.signals import _election_cache
        report = _election_cache.get("data")
        if not report:
            return []
    except Exception as e:
        logger.warning("Election signal: could not load report: %s", e)
        return []

    try:
        signals.extend(_arb_signals(report))
    except Exception as e:
        logger.warning("Election arb signals failed: %s", e)

    try:
        signals.extend(_money_vs_odds_signals(report))
    except Exception as e:
        logger.warning("Election money signals failed: %s", e)

    try:
        signals.extend(_momentum_signals(report))
    except Exception as e:
        logger.warning("Election momentum signals failed: %s", e)

    try:
        signals.extend(_ie_spending_signals(report))
    except Exception as e:
        logger.warning("Election IE signals failed: %s", e)

    try:
        signals.extend(_primary_overround_signals(report))
    except Exception as e:
        logger.warning("Election primary signals failed: %s", e)

    try:
        signals.extend(_narrative_shift_signals(report))
    except Exception as e:
        logger.warning("Election narrative signals failed: %s", e)

    try:
        signals.extend(_poll_divergence_signals(report))
    except Exception as e:
        logger.warning("Election poll divergence signals failed: %s", e)

    try:
        signals.extend(_efiling_signals(report))
    except Exception as e:
        logger.warning("Election eFiling signals failed: %s", e)

    try:
        signals.extend(_wiki_attention_signals(report))
    except Exception as e:
        logger.warning("Election wiki attention signals failed: %s", e)

    try:
        signals.extend(_gtrends_signals(report))
    except Exception as e:
        logger.warning("Election Google Trends signals failed: %s", e)

    try:
        signals.extend(_smart_money_divergence_signals(report))
    except Exception as e:
        logger.warning("Election smart money signals failed: %s", e)

    try:
        signals.extend(_whale_concentration_signals(report))
    except Exception as e:
        logger.warning("Election whale concentration signals failed: %s", e)

    try:
        signals.extend(_cash_momentum_signals(report))
    except Exception as e:
        logger.warning("Election cash momentum signals failed: %s", e)

    try:
        signals.extend(_economic_macro_signals(report))
    except Exception as e:
        logger.warning("Election economic macro signals failed: %s", e)

    # Apply confluence scoring — stack confirming signals on same market
    signals = _apply_confluence(signals)

    _cache["signals"] = signals
    _cache["ts"] = now
    logger.info("Election signals: generated %d signals", len(signals))
    return signals


def _apply_confluence(signals: list[dict]) -> list[dict]:
    """Stack confirming signals on the same market+side into one boosted signal.

    When multiple strategies point the same direction on the same market,
    keep the highest-confidence signal and boost it:
    - +8 confidence per confirming source (capped at +24 for 3 extra)
    - Set source_agreement count (used by Kelly sizing for +10%/source)
    - Append confirming source names to reasoning
    """
    from collections import defaultdict

    # Group by (market_id, side)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for sig in signals:
        mid = sig.get("market_id", "")
        side = sig.get("side", "")
        if mid and side:
            groups[(mid, side)].append(sig)
        # Signals without market_id pass through ungrouped

    result = []
    seen_keys = set()

    for key, group in groups.items():
        seen_keys.add(key)
        if len(group) == 1:
            result.append(group[0])
            continue

        # Sort by confidence descending — lead signal gets the boost
        group.sort(key=lambda s: s.get("confidence", 0), reverse=True)
        lead = dict(group[0])  # Copy so we don't mutate cached signals
        confirming = group[1:]

        # Boost confidence: +8 per confirming source, max +24
        boost = min(24, len(confirming) * 8)
        lead["confidence"] = min(85, lead["confidence"] + boost)

        # Record agreement for Kelly sizing
        lead["source_agreement"] = len(group)
        confirming_names = [s.get("strategy", s.get("source", "?")) for s in confirming]
        lead["confirming_sources"] = confirming_names

        # Append to reasoning
        names_str = ", ".join(confirming_names)
        lead["reasoning"] = (
            f"{lead['reasoning']} "
            f"[CONFLUENCE: {len(group)} sources agree — {names_str}]"
        )

        result.append(lead)

    # Add any signals that had no market_id (shouldn't happen but be safe)
    for sig in signals:
        mid = sig.get("market_id", "")
        side = sig.get("side", "")
        if not mid or not side:
            result.append(sig)

    # Sort final list by confidence
    result.sort(key=lambda s: s.get("confidence", 0), reverse=True)
    return result


def _find_market_by_state_race(markets: list, state: str, race: str) -> dict | None:
    """Find best market for a state/race combo (prefer Polymarket for execution)."""
    candidates = [
        m for m in markets
        if m.get("state") == state and m.get("race_category") == race
    ]
    if not candidates:
        return None
    # Prefer Polymarket (we execute via Simmer on Polymarket)
    poly = [m for m in candidates if m.get("platform") == "polymarket"]
    return poly[0] if poly else candidates[0]


# ── Strategy 1: Cross-Platform Arbitrage ──────────────────────────────────

def _arb_signals(report: dict) -> list[dict]:
    """Generate signals from cross-platform price disagreements (Poly vs Kalshi vs PredictIt)."""
    spreads = report.get("insights", {}).get("cross_platform_spreads", [])
    markets = report.get("markets", [])
    signals = []

    for s in spreads[:5]:  # Top 5 biggest spreads
        spread_pp = s.get("spread_pp", 0)
        if spread_pp < 5:
            continue  # Only trade >5pp spreads

        state = s["state"]
        race = s["race_category"]
        poly_d = s.get("poly_d", 0)
        kalshi_d = s.get("kalshi_d")
        pi_d = s.get("pi_d")
        num_platforms = s.get("platforms", 2)

        # Find the Polymarket market for execution
        mkt = _find_market_by_state_race(markets, state, race)
        if not mkt or mkt.get("platform") != "polymarket":
            continue

        # Collect all other platform D prices to compare against Poly
        other_prices = {}
        if kalshi_d is not None:
            other_prices["Kalshi"] = kalshi_d
        if pi_d is not None:
            other_prices["PI"] = pi_d

        if not other_prices:
            continue

        # Consensus direction: average of non-Poly platforms
        consensus_d = sum(other_prices.values()) / len(other_prices)

        # Buy the cheaper side on Polymarket
        if poly_d < consensus_d:
            side = "YES"  # Poly underprices D → buy D
        else:
            side = "NO"   # Poly overprices D → buy R

        # Build reasoning with all available platforms
        parts = [f"Poly {poly_d*100:.0f}%"]
        for name, price in other_prices.items():
            parts.append(f"{name} {price*100:.0f}%")
        price_str = " / ".join(parts)
        reasoning = f"Cross-platform arb: {state} {race} Dem odds: {price_str} ({spread_pp:.1f}pp spread)"

        # Confidence: base scales with spread, bonus for 3-platform confluence
        confidence = min(75, 30 + spread_pp * 3)
        if num_platforms >= 3:
            confidence = min(80, confidence + 5)  # 3-way agreement is stronger

        signals.append({
            "source": "election_cross_platform",
            "platform": "polymarket",
            "market": mkt["question"][:200],
            "market_id": mkt["id"],
            "side": side,
            "price": poly_d if side == "YES" else (1 - poly_d),
            "confidence": round(confidence),
            "reasoning": reasoning,
            "volume": mkt.get("volume", 0) or 0,
            "state": state,
            "race_category": race,
            "strategy": "ElectionCrossPlatformArb",
        })

    return signals


# ── Strategy 2: Money vs Odds Divergence ─────────────────────────────────

def _money_vs_odds_signals(report: dict) -> list[dict]:
    """Generate signals when FEC fundraising diverges from market odds."""
    divergences = report.get("insights", {}).get("money_vs_odds", [])
    markets = report.get("markets", [])
    signals = []

    for div in divergences[:5]:  # Top 5 divergences
        div_pp = div.get("divergence_pp", 0)
        if div_pp < 12:
            continue  # Only trade >12pp divergences (stricter threshold)

        state = div["state"]
        mkt = _find_market_by_state_race(markets, state, "senate")
        if not mkt or mkt.get("platform") != "polymarket":
            continue

        # If D outfunding market odds → market underprices D → buy D
        signal_text = div.get("signal", "")
        if "D outfunding" in signal_text:
            side = "YES"
        else:
            side = "NO"

        # Confidence: 12pp=35, 20pp=50, 30pp=65 (conservative — FEC data lags)
        confidence = min(65, 20 + div_pp * 1.5)

        signals.append({
            "source": "election_fec_money",
            "platform": "polymarket",
            "market": mkt["question"][:200],
            "market_id": mkt["id"],
            "side": side,
            "price": div.get("dem_market_odds", 0.5),
            "confidence": round(confidence),
            "reasoning": div.get("detail", signal_text),
            "volume": mkt.get("volume", 0) or 0,
            "state": state,
            "race_category": "senate",
            "strategy": "ElectionFECDivergence",
        })

    return signals


# ── Strategy 3: Momentum / Daily Movers ──────────────────────────────────

def _momentum_signals(report: dict) -> list[dict]:
    """Generate signals from daily price momentum with volume confirmation."""
    movers = report.get("top_movers", [])
    delta_period = report.get("delta_period")
    signals = []

    for m in movers[:8]:  # Top 8 movers
        delta = m.get("delta", 0)
        abs_delta = abs(delta)
        if abs_delta < 0.03:
            continue  # Only trade >3pp moves

        # Need the market to have enough volume for conviction
        # (volume isn't in the mover dict but we can use price stability)
        current = m.get("current", 0.5)
        if current < 0.05 or current > 0.95:
            continue  # Skip extreme prices (near resolution)

        # Trend-follow: if price went up, buy YES; if down, buy NO
        side = "YES" if delta > 0 else "NO"
        price = current if side == "YES" else (1 - current)

        # Confidence: 3pp=30, 5pp=40, 10pp=55 (momentum is noisier)
        confidence = min(60, 20 + abs_delta * 100 * 2)

        period_label = "24h" if delta_period == "daily" else "7d"
        reasoning = (
            f"Election momentum: {m.get('outcome', '?')} in {m.get('question', '?')[:50]} "
            f"moved {'+' if delta > 0 else ''}{delta*100:.1f}pp ({period_label})"
        )

        signals.append({
            "source": "election_momentum",
            "platform": m.get("platform", "polymarket"),
            "market": m.get("question", "")[:200],
            "market_id": m.get("id", ""),
            "side": side,
            "price": price,
            "confidence": round(confidence),
            "reasoning": reasoning,
            "state": "",
            "race_category": m.get("race_category", ""),
            "strategy": "ElectionMomentum",
        })

    return signals


# ── Strategy 4: IE Spending Surge ─────────────────────────────────────────

def _ie_spending_signals(report: dict) -> list[dict]:
    """Generate signals from Super PAC independent expenditure surges."""
    surges = report.get("insights", {}).get("spending_surges", [])
    markets = report.get("markets", [])
    signals = []

    for surge in surges[:5]:
        amount = surge.get("total_amount", 0)
        if amount < 200_000:
            continue  # Only significant spending

        candidate = surge.get("candidate_name", "")
        support_oppose = surge.get("support_oppose", "")
        state = surge.get("state", "")

        if not state or not support_oppose:
            continue

        # Find the market
        mkt = _find_market_by_state_race(markets, state, "senate")
        if not mkt or mkt.get("platform") != "polymarket":
            # Try governor
            mkt = _find_market_by_state_race(markets, state, "governor")
            if not mkt or mkt.get("platform") != "polymarket":
                continue

        # Oppose spending = smart money betting against candidate
        # Support spending = smart money backing candidate
        # We fade oppose targets and follow support targets
        if support_oppose == "O":
            # Opposing a candidate — fade them
            side = "NO"
            reasoning = (
                f"IE spending surge: ${amount:,.0f} spent OPPOSING {candidate} in {state}. "
                f"Super PAC smart money signal."
            )
        elif support_oppose == "S":
            side = "YES"
            reasoning = (
                f"IE spending surge: ${amount:,.0f} spent SUPPORTING {candidate} in {state}. "
                f"Super PAC smart money signal."
            )
        else:
            continue

        # Confidence: $200K=30, $500K=40, $1M=55 (confirmation signal)
        confidence = min(55, 20 + (amount / 100_000) * 5)

        signals.append({
            "source": "election_ie_spending",
            "platform": "polymarket",
            "market": mkt["question"][:200],
            "market_id": mkt["id"],
            "side": side,
            "price": 0.5,  # We don't know which side the candidate is on
            "confidence": round(confidence),
            "reasoning": reasoning,
            "volume": mkt.get("volume", 0) or 0,
            "state": state,
            "race_category": mkt.get("race_category", ""),
            "strategy": "ElectionIESpending",
        })

    return signals


# ── Strategy 5: Primary Overround ─────────────────────────────────────────

def _primary_overround_signals(report: dict) -> list[dict]:
    """Generate signals from multi-candidate primaries with overround.

    When candidate probabilities sum to >105%, the market is overpriced
    on at least one candidate. Fade the least-liquid longshots.
    When sum < 95%, there's value — buy the frontrunner.
    """
    primaries = report.get("insights", {}).get("primary_index", [])
    markets = report.get("markets", [])
    signals = []

    for p in primaries[:10]:
        candidates = p.get("top_3", [])
        if len(candidates) < 2:
            continue

        total_cands = p.get("candidates", 0)
        leader = candidates[0]
        runner_up = candidates[1]

        # Sum of all candidate prices — if we only have top 3 from a
        # multi-candidate field, estimate total from what we have
        top_sum = sum(c["price"] for c in candidates)

        # For multi-candidate fields, overround is common
        # Only generate signal if leader is a clear value play
        spread = p.get("spread", 0)
        if spread > 0.15:
            continue  # Only trade competitive primaries

        vol = p.get("volume", 0) or 0
        if vol < 10_000:
            continue  # Need liquidity

        # In competitive primaries, the frontrunner at <55% with narrow spread
        # often has value because markets overprice longshots
        leader_price = leader["price"]
        if 0.30 <= leader_price <= 0.55 and spread < 0.10:
            # Find the actual Polymarket market for the leader
            leader_name = leader["name"].lower()
            target_mkt = None
            for m in markets:
                if m.get("race_category") != "primary":
                    continue
                if m.get("platform") != "polymarket":
                    continue
                q = m["question"].lower()
                if leader_name in q:
                    target_mkt = m
                    break

            if not target_mkt:
                continue

            confidence = min(50, 25 + (10 - spread * 100) * 2.5)
            reasoning = (
                f"Primary value: {leader['name']} leads at {leader_price*100:.0f}% "
                f"with only {spread*100:.1f}pp spread over {runner_up['name']} "
                f"({runner_up['price']*100:.0f}%) in {total_cands}-candidate field"
            )

            signals.append({
                "source": "election_primary",
                "platform": "polymarket",
                "market": target_mkt["question"][:200],
                "market_id": target_mkt["id"],
                "side": "YES",
                "price": leader_price,
                "confidence": round(confidence),
                "reasoning": reasoning,
                "volume": vol,
                "state": p.get("state", ""),
                "race_category": "primary",
                "strategy": "ElectionPrimaryValue",
            })

    return signals


# ── Strategy 6: GDELT Narrative Shift ────────────────────────────────────

def _narrative_shift_signals(report: dict) -> list[dict]:
    """Generate signals from GDELT news sentiment shifts.

    When sentiment for a candidate/party deteriorates or improves significantly,
    prediction markets often lag by hours. Trade the direction of the shift.
    """
    shifts = report.get("insights", {}).get("narrative_shifts", [])
    state_sentiment = report.get("insights", {}).get("state_sentiment", [])
    markets = report.get("markets", [])
    signals = []

    for shift in shifts[:5]:
        magnitude = shift.get("magnitude", 0)
        if magnitude < 1.0:
            continue

        shift_type = shift.get("type", "")
        direction = shift.get("direction", "")

        if shift_type == "state_narrative":
            state = shift.get("state", "")
            if not state:
                continue
            # Find the senate market for this state
            mkt = _find_market_by_state_race(markets, state, "senate")
            if not mkt or mkt.get("platform") != "polymarket":
                continue

            # Positive sentiment → buy incumbent/Dem, Negative → sell
            # (simplified: positive tone shift → YES, negative → NO)
            if direction in ("positive", "improving"):
                side = "YES"
            else:
                side = "NO"

            # Confidence: magnitude 1.0=30, 2.0=40, 3.0=50 (capped at 55)
            confidence = min(55, 20 + magnitude * 10)

            signals.append({
                "source": "election_narrative",
                "platform": "polymarket",
                "market": mkt["question"][:200],
                "market_id": mkt["id"],
                "side": side,
                "price": 0.5,
                "confidence": round(confidence),
                "reasoning": shift.get("detail", f"Narrative shift in {state}: {direction}"),
                "volume": mkt.get("volume", 0) or 0,
                "state": state,
                "race_category": "senate",
                "strategy": "ElectionNarrativeShift",
            })

        elif shift_type == "candidate_narrative":
            label = shift.get("label", "")
            # Map candidate labels to market lookups
            if "dem" in label.lower() or "senate_dem" in label:
                # Broad Dem sentiment shift — trade generic ballot / control markets
                for m in markets:
                    if m.get("platform") != "polymarket":
                        continue
                    q = m.get("question", "").lower()
                    if "senate" in q and "control" in q:
                        side = "YES" if direction in ("positive", "improving") else "NO"
                        confidence = min(50, 20 + magnitude * 8)
                        signals.append({
                            "source": "election_narrative",
                            "platform": "polymarket",
                            "market": m["question"][:200],
                            "market_id": m["id"],
                            "side": side,
                            "price": 0.5,
                            "confidence": round(confidence),
                            "reasoning": shift.get("detail", f"Dem narrative {direction}"),
                            "state": "",
                            "race_category": "senate",
                            "strategy": "ElectionNarrativeShift",
                        })
                        break

    return signals


# ── Strategy 7: Poll-to-Market Divergence ────────────────────────────────

def _poll_divergence_signals(report: dict) -> list[dict]:
    """Generate signals when polling averages diverge from market prices.

    The core latency arb: polls shift but markets haven't repriced yet.
    """
    divergences = report.get("insights", {}).get("poll_market_divergences", [])
    markets = report.get("markets", [])
    signals = []

    for div in divergences[:5]:
        div_pp = div.get("divergence_pp", 0)
        if div_pp < 5:
            continue  # Only trade >5pp divergences

        market_id = div.get("market_id", "")
        if not market_id:
            continue

        # Find the market
        mkt = None
        for m in markets:
            if m.get("id") == market_id:
                mkt = m
                break
        if not mkt or mkt.get("platform") != "polymarket":
            continue

        direction = div.get("direction", "")
        # If D is underpriced by polls → buy YES (buy D)
        # If D is overpriced by polls → buy NO (sell D)
        side = "YES" if direction == "underpriced" else "NO"

        # Confidence: 5pp=30, 10pp=45, 15pp=55, 20pp=65
        confidence = min(65, 15 + div_pp * 3)

        signals.append({
            "source": "election_poll_divergence",
            "platform": "polymarket",
            "market": mkt["question"][:200],
            "market_id": market_id,
            "side": side,
            "price": div.get("market_price", 0.5),
            "confidence": round(confidence),
            "reasoning": div.get("detail", f"Poll-market divergence: {div_pp:.1f}pp"),
            "volume": mkt.get("volume", 0) or 0,
            "state": div.get("state", ""),
            "race_category": div.get("race_category", ""),
            "strategy": "ElectionPollDivergence",
        })

    return signals


# ── Strategy 8: FEC eFiling Real-Time Alerts ──────────────────────────────

def _efiling_signals(report: dict) -> list[dict]:
    """Generate signals from real-time FEC eFiling spending alerts.

    When a Super PAC files a large expenditure (>$100K), this signal fires
    within minutes. The fastest public campaign finance signal available.
    """
    alerts = report.get("insights", {}).get("efiling_alerts", [])
    markets = report.get("markets", [])
    signals = []

    for alert in alerts[:5]:
        amount = alert.get("amount", 0)
        if amount < 100_000:
            continue

        state = alert.get("candidate_state", "")
        office = alert.get("candidate_office", "")
        sup_opp = alert.get("support_oppose", "")
        party = alert.get("candidate_party", "?")

        if not state or sup_opp not in ("S", "O"):
            continue

        # Map office to race category
        race = {"S": "senate", "H": "house", "P": "presidential"}.get(office, "")
        if not race:
            continue

        mkt = _find_market_by_state_race(markets, state, race)
        if not mkt or mkt.get("platform") != "polymarket":
            continue

        candidate = alert.get("candidate_name", "?")
        committee = alert.get("committee_name", "?")

        # Determine direction: support D or oppose R = pro-D, and vice versa
        if (party == "D" and sup_opp == "S") or (party == "R" and sup_opp == "O"):
            side = "YES"  # Pro-D spending
            direction = "supporting" if sup_opp == "S" else "opposing"
            reasoning = (
                f"LIVE eFiling: {committee} spent ${amount:,.0f} {direction} "
                f"{candidate} ({party}) in {state} — pro-D smart money"
            )
        elif (party == "R" and sup_opp == "S") or (party == "D" and sup_opp == "O"):
            side = "NO"  # Pro-R spending
            direction = "supporting" if sup_opp == "S" else "opposing"
            reasoning = (
                f"LIVE eFiling: {committee} spent ${amount:,.0f} {direction} "
                f"{candidate} ({party}) in {state} — pro-R smart money"
            )
        else:
            continue

        # Confidence: $100K=35, $500K=50, $1M=60, $5M=70
        confidence = min(70, 25 + (amount / 100_000) * 5)

        signals.append({
            "source": "election_efiling",
            "platform": "polymarket",
            "market": mkt["question"][:200],
            "market_id": mkt["id"],
            "side": side,
            "price": 0.5,
            "confidence": round(confidence),
            "reasoning": reasoning,
            "volume": mkt.get("volume", 0) or 0,
            "state": state,
            "race_category": race,
            "strategy": "ElectionEFilingAlert",
        })

    return signals


# ── Strategy 9: Wikipedia Attention Spike ─────────────────────────────────

def _wiki_attention_signals(report: dict) -> list[dict]:
    """Generate signals from Wikipedia pageview spikes.

    When a candidate's Wikipedia page traffic spikes >2 std devs above
    their 30-day average, something happened. Combine with GDELT tone
    to determine if the attention is positive or negative.
    """
    spikes = report.get("insights", {}).get("wiki_spikes", [])
    sentiment = report.get("insights", {}).get("candidate_sentiment", [])
    markets = report.get("markets", [])
    signals = []

    # Build sentiment lookup by candidate name fragment
    sentiment_map = {}
    for s in sentiment:
        label = s.get("label", "").lower()
        tone = s.get("avg_tone", 0)
        sentiment_map[label] = tone

    for spike in spikes[:5]:
        state = spike.get("state", "")
        race = spike.get("race", "")
        party = spike.get("party", "")
        z_score = spike.get("z_score", 0)
        pct_above = spike.get("pct_above_avg", 0)
        candidate = spike.get("candidate", "?")

        if not race:
            continue

        # Try to find the market
        if state:
            mkt = _find_market_by_state_race(markets, state, race)
        else:
            # Presidential — find any presidential market on Polymarket
            mkt = None
            for m in markets:
                if m.get("race_category") == "presidential" and m.get("platform") == "polymarket":
                    mkt = m
                    break

        if not mkt or mkt.get("platform") != "polymarket":
            continue

        # Determine if attention is positive or negative using GDELT sentiment
        name_lower = candidate.lower()
        tone = None
        for label, t in sentiment_map.items():
            if name_lower.split()[0].lower() in label or name_lower.split()[-1].lower() in label:
                tone = t
                break

        if tone is not None and tone < -1.0:
            # Negative attention — bad for this candidate
            if party == "D":
                side = "NO"  # Bad for D
            else:
                side = "YES"  # Bad for R = good for D
            tone_label = f"negative tone ({tone:.1f})"
        elif tone is not None and tone > 1.0:
            # Positive attention — good for this candidate
            if party == "D":
                side = "YES"
            else:
                side = "NO"
            tone_label = f"positive tone ({tone:.1f})"
        else:
            # Neutral or no sentiment data — attention alone is not directional
            continue

        # Confidence: z=2 baseline=25, scales with z-score. Low because noisy.
        confidence = min(50, 15 + z_score * 5)

        reasoning = (
            f"Wiki attention spike: {candidate} pageviews +{pct_above:.0f}% above avg "
            f"(z={z_score:.1f}), {tone_label}"
        )

        signals.append({
            "source": "election_wiki_attention",
            "platform": "polymarket",
            "market": mkt["question"][:200],
            "market_id": mkt["id"],
            "side": side,
            "price": 0.5,
            "confidence": round(confidence),
            "reasoning": reasoning,
            "state": state,
            "race_category": race,
            "strategy": "ElectionWikiAttention",
        })

    return signals


# ── Strategy 10: Google Trends Trending Topic ────────────────────────────

def _gtrends_signals(report: dict) -> list[dict]:
    """Generate signals when tracked candidates appear in Google Trends top trending.

    When a candidate hits the Google Trends RSS feed (top ~20 trending US topics),
    it means massive public attention. Combine with GDELT tone to determine
    if the attention is positive or negative, and with Wiki for cross-confirmation.
    """
    spikes = report.get("insights", {}).get("gtrends_spikes", [])
    wiki_data = report.get("insights", {}).get("wiki_pageviews", [])
    sentiment = report.get("insights", {}).get("candidate_sentiment", [])
    markets = report.get("markets", [])
    signals = []

    # Build lookups
    sentiment_map = {}
    for s in sentiment:
        label = s.get("label", "").lower()
        sentiment_map[label] = s.get("avg_tone", 0)

    wiki_map = {}
    for w in wiki_data:
        wiki_map[w.get("candidate", "").lower()] = w

    for spike in spikes[:5]:
        candidate = spike.get("candidate")
        if not candidate:
            continue  # Skip non-candidate election topics

        state = spike.get("state", "")
        race = spike.get("race", "")
        party = spike.get("party", "")
        traffic = spike.get("traffic", 0)
        topic = spike.get("trending_topic", "?")

        if not race:
            continue

        # Find market
        if state:
            mkt = _find_market_by_state_race(markets, state, race)
        else:
            mkt = None
            for m in markets:
                if m.get("race_category") == "presidential" and m.get("platform") == "polymarket":
                    mkt = m
                    break

        if not mkt or mkt.get("platform") != "polymarket":
            continue

        # Determine direction using GDELT sentiment
        name_lower = candidate.lower()
        tone = None
        for label, t in sentiment_map.items():
            if name_lower.split()[0].lower() in label or name_lower.split()[-1].lower() in label:
                tone = t
                break

        # Check if Wiki also shows a spike (cross-confirmation)
        wiki_confirms = False
        wiki_entry = wiki_map.get(name_lower)
        if wiki_entry and wiki_entry.get("is_spike"):
            wiki_confirms = True

        if tone is not None and tone < -1.0:
            if party == "D":
                side = "NO"
            else:
                side = "YES"
            tone_label = f"negative tone ({tone:.1f})"
        elif tone is not None and tone > 1.0:
            if party == "D":
                side = "YES"
            else:
                side = "NO"
            tone_label = f"positive tone ({tone:.1f})"
        else:
            # No sentiment data — trending alone is massive but non-directional
            continue

        # Confidence: trending = high attention, base 35, scale with traffic
        # 200K+ traffic = 45, 500K+ = 55, wiki confirms = +10
        confidence = min(60, 30 + (traffic / 100_000) * 5)
        if wiki_confirms:
            confidence = min(65, confidence + 10)

        traffic_str = spike.get("traffic_str", f"{traffic:,}")
        reasoning = (
            f"Google Trending: '{topic}' ({traffic_str} searches) "
            f"matches {candidate}, {tone_label}"
        )
        if wiki_confirms:
            reasoning += " [Wiki confirms]"

        signals.append({
            "source": "election_gtrends",
            "platform": "polymarket",
            "market": mkt["question"][:200],
            "market_id": mkt["id"],
            "side": side,
            "price": 0.5,
            "confidence": round(confidence),
            "reasoning": reasoning,
            "state": state,
            "race_category": race,
            "strategy": "ElectionGoogleTrends",
        })

    return signals


# ── Strategy 11: Smart Money Divergence ──────────────────────────────────

def _smart_money_divergence_signals(report: dict) -> list[dict]:
    """Generate signals when Polymarket YES holders disagree with FEC cash flow.

    DIVERGE = YES bettors back one party but FEC cash favors the other.
    This is the highest-signal cross-reference: real money on both sides
    of the ledger pointing in opposite directions.
    """
    cross_signals = report.get("insights", {}).get("fec_cross_signals", [])
    markets = report.get("markets", [])
    signals = []

    for s in cross_signals:
        if s.get("strength") != "divergence":
            continue

        state = s.get("state", "")
        if not state:
            continue

        mkt = _find_market_by_state_race(markets, state, "senate")
        if not mkt or mkt.get("platform") != "polymarket":
            mkt = _find_market_by_state_race(markets, state, "governor")
            if not mkt or mkt.get("platform") != "polymarket":
                continue

        yes_party = s.get("yes_party", "")
        fec_adv = s.get("fec_advantage", "?")

        # FEC cash historically predicts better than prediction markets in
        # low-liquidity races. Fade the YES bettors, follow the money.
        if fec_adv == "D":
            side = "YES"  # FEC says D wins
        elif fec_adv == "R":
            side = "NO"   # FEC says R wins
        else:
            continue

        # Confidence: base 35, boost for large YES share pools and IE confirmation
        yes_shares = s.get("yes_shares", 0)
        ie_adv = s.get("ie_advantage", "?")
        confidence = 35

        # More YES shares diverging = stronger signal
        if yes_shares > 20000:
            confidence += 5
        if yes_shares > 50000:
            confidence += 5

        # IE spending confirms FEC direction = stronger
        if ie_adv == fec_adv:
            confidence += 10

        # Whale concentration makes the divergence less reliable
        whale_pct = s.get("whale_pct", 0)
        if whale_pct > 0.3:
            confidence -= 5  # Single whale could be wrong

        confidence = min(65, max(20, confidence))

        reasoning = (
            f"Smart money divergence: YES bettors back {yes_party} "
            f"({yes_shares:,.0f} shares) but FEC cash favors {fec_adv} in {state}"
        )
        if ie_adv != "?" and ie_adv == fec_adv:
            reasoning += f" [IE spending confirms {fec_adv}]"
        if whale_pct > 0.3:
            reasoning += f" [whale risk: {whale_pct:.0%}]"

        signals.append({
            "source": "election_smart_money",
            "platform": "polymarket",
            "market": mkt["question"][:200],
            "market_id": mkt["id"],
            "side": side,
            "price": s.get("yes_price", 0.5),
            "confidence": round(confidence),
            "reasoning": reasoning,
            "volume": mkt.get("volume", 0) or 0,
            "state": state,
            "race_category": mkt.get("race_category", "senate"),
            "strategy": "ElectionSmartMoneyDivergence",
        })

    return signals


# ── Strategy 12: Whale Concentration Warning ─────────────────────────────

def _whale_concentration_signals(report: dict) -> list[dict]:
    """Generate warning signals when a single holder dominates a market.

    When whale_pct > 30%, the market is thin and the price is set by
    one actor. If that whale exits, the price will crash. This is a
    contrarian fade signal — bet against concentrated markets.
    """
    smart_money = report.get("insights", {}).get("smart_money", [])
    markets = report.get("markets", [])
    signals = []

    for sm in smart_money:
        whale_pct = sm.get("whale_pct", 0)
        if whale_pct < 0.35:
            continue  # Only flag extreme concentration

        concentration = sm.get("top5_concentration", 0)
        if concentration < 0.6:
            continue  # Top 5 should also be concentrated

        yes_price = sm.get("yes_price", 0)
        if yes_price < 0.15 or yes_price > 0.85:
            continue  # Skip near-resolved markets

        state = sm.get("state", "")
        race = sm.get("race_category", "")
        if not state or not race:
            continue

        mkt = _find_market_by_state_race(markets, state, race)
        if not mkt or mkt.get("platform") != "polymarket":
            continue

        # Fade the whale — if YES is dominated by one holder, bet NO
        side = "NO"
        confidence = min(45, 25 + int(whale_pct * 30))

        top_holder = ""
        holders = sm.get("yes_holders", [])
        if holders:
            top_holder = holders[0].get("name", "anon")

        reasoning = (
            f"Whale concentration: {top_holder} holds {whale_pct:.0%} of YES "
            f"in {state} {race} (top5={concentration:.0%}). "
            f"Thin market — price fragile if whale exits."
        )

        signals.append({
            "source": "election_whale_concentration",
            "platform": "polymarket",
            "market": mkt["question"][:200],
            "market_id": mkt["id"],
            "side": side,
            "price": 1 - yes_price,
            "confidence": round(confidence),
            "reasoning": reasoning,
            "volume": mkt.get("volume", 0) or 0,
            "state": state,
            "race_category": race,
            "strategy": "ElectionWhaleConcentration",
        })

    return signals


# ── Strategy 13: eFiling Cash Momentum ───────────────────────────────────

def _cash_momentum_signals(report: dict) -> list[dict]:
    """Generate signals from fresh eFiling cash-on-hand data.

    When recent_advantage (from eFiling) disagrees with cash_advantage
    (from quarterly), one side is gaining momentum. Trade the fresher signal.
    """
    fundraising = report.get("insights", {}).get("fundraising", {})
    markets = report.get("markets", [])
    signals = []

    for state, fund in fundraising.items():
        recent_adv = fund.get("recent_advantage", "?")
        quarterly_adv = fund.get("cash_advantage", "?")
        recent_total = fund.get("recent_total", 0)

        if recent_adv == "?" or quarterly_adv == "?":
            continue
        if recent_total < 50_000:
            continue  # Not enough recent data

        # Only signal when fresh data disagrees with stale data
        if recent_adv == quarterly_adv:
            continue

        mkt = _find_market_by_state_race(markets, state, "senate")
        if not mkt or mkt.get("platform") != "polymarket":
            continue

        # Follow the fresh money
        if recent_adv == "D":
            side = "YES"
            reasoning = (
                f"Cash momentum shift: {state} recent eFiling favors D "
                f"(${recent_total:,.0f} recent) but quarterly cash favored R. "
                f"Fundraising momentum shifting."
            )
        else:
            side = "NO"
            reasoning = (
                f"Cash momentum shift: {state} recent eFiling favors R "
                f"(${recent_total:,.0f} recent) but quarterly cash favored D. "
                f"Fundraising momentum shifting."
            )

        # Conservative confidence — eFiling receipts are partial (>$200 only)
        confidence = min(50, 25 + (recent_total / 200_000) * 5)

        signals.append({
            "source": "election_cash_momentum",
            "platform": "polymarket",
            "market": mkt["question"][:200],
            "market_id": mkt["id"],
            "side": side,
            "price": 0.5,
            "confidence": round(confidence),
            "reasoning": reasoning,
            "volume": mkt.get("volume", 0) or 0,
            "state": state,
            "race_category": "senate",
            "strategy": "ElectionCashMomentum",
        })

    return signals


# ── Strategy 14: Economic Macro Signal ───────────────────────────────────

def _economic_macro_signals(report: dict) -> list[dict]:
    """Generate signals from FRED economic indicators.

    Strong economy favors the incumbent party across competitive races.
    Only fires on toss-up/lean races where the macro signal is decisive.
    Requires |incumbent_score| >= 30 to avoid noise.
    """
    econ = report.get("insights", {}).get("economic", {})
    if not econ.get("available"):
        return []

    score_data = econ.get("incumbent_score", {})
    score = score_data.get("score", 0)
    outlook = score_data.get("outlook", "neutral")
    factors = score_data.get("factors", [])

    # Only signal when economic direction is clear (not neutral)
    if abs(score) < 30:
        return []

    markets = report.get("markets", [])
    structural = report.get("insights", {}).get("structural", {})
    tossup_states = {r.get("state") for r in structural.get("tossup_races", [])}

    # Also target lean races via race ratings
    ratings = structural.get("race_ratings", {}).get("senate", {})
    competitive_states = set(tossup_states)
    for st, info in ratings.items():
        rating = (info.get("rating") or "").lower()
        if "lean" in rating or "toss" in rating:
            competitive_states.add(st)

    if not competitive_states:
        # Fallback: target presidential/senate control markets
        competitive_states = {"GA", "MI", "NC", "PA", "AZ", "NV", "WI"}

    signals = []
    for state in competitive_states:
        mkt = _find_market_by_state_race(markets, state, "senate")
        if not mkt or mkt.get("platform") != "polymarket":
            continue

        # Determine incumbent party for this state
        incumbent_party = ratings.get(state, {}).get("incumbent_party", "")
        if not incumbent_party:
            continue

        # Favorable economy → buy incumbent party
        if score > 0:
            # Economy favors incumbent
            side = "YES" if incumbent_party == "D" else "NO"
            direction = "favorable"
        else:
            # Economy hurts incumbent
            side = "NO" if incumbent_party == "D" else "YES"
            direction = "unfavorable"

        # Confidence: scales with |score|, capped conservatively
        # |score| 30=25, 50=30, 80=40, 100=45
        confidence = min(45, 20 + abs(score) * 0.25)

        factors_str = "; ".join(factors[:3]) if factors else "multiple indicators"
        reasoning = (
            f"Economic macro: {direction} for {incumbent_party} incumbent in {state} "
            f"(score {score:+d}). {factors_str}"
        )

        signals.append({
            "source": "election_economic_macro",
            "platform": "polymarket",
            "market": mkt["question"][:200],
            "market_id": mkt["id"],
            "side": side,
            "price": 0.5,
            "confidence": round(confidence),
            "reasoning": reasoning,
            "volume": mkt.get("volume", 0) or 0,
            "state": state,
            "race_category": "senate",
            "strategy": "ElectionEconomicMacro",
        })

    return signals
