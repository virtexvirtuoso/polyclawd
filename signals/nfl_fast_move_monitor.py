#!/usr/bin/env python3
"""nfl_fast_move_monitor.py — REST-based fast-move monitor for NFL game markets.

Closes the event-driven gap the WS layer was meant to fill, WITHOUT needing
Polymarket API keys (the polymarket_us MarketsWebSocket requires key_id/secret).

Source: api.polymarket.us/v1/search?query=NFL&status=upcoming — the US-sports
backend enumerates NFL game events with live moneyline quotes (bestBidQuote /
bestAskQuote + updatedAt). Verified 2026-08-22: 39/40 upcoming games carry
live ML quotes.

Design:
  - Poll the search endpoint every FAST_MOVE_POLL_SECS (default 30s).
  - Track each game's moneyline mid (bid+ask)/2 in a rolling window.
  - When a mid moves >= MOVE_PP (default 0.05 = 5pp) within MOVE_WINDOW, fire
    an alert via run_sport_edge_alerts (dedup'd) OR a direct fast-move alert.
  - Cooldown per game to avoid spam; warmup after (re)start to avoid snapshot
    artifacts.

This is a poll, not a push — but at 30s it's ~240x faster than the 2h scan and
~2x faster than the 1-min drift scanner, and it catches PM-side moves that the
Pinnacle-drift scanner (book-side) does not.

FIELD FRESHNESS CONVENTION (2026-08-23):
  Every alert field is either LIVE (changes during the game) or STATIC (fixed
  pre-game). A STATIC field must NEVER be presented as live in-game state.
  - LIVE fields (PM mid, Vegas line) must come from a real-time source.
  - STATIC fields (starters, probables, pre-game odds) must be labeled as
    such or overridden by a live source once the game is in progress.
  - If the Odds API has NO Vegas anchor for a game (preseason data-availability
    gap), SUPPRESS the alert rather than emit a partial "no line" signal — a
    fast-move alert with no Vegas-vs-PM read is pure noise.
  Origin: 2026-08-23 — Bengals-Eagles preseason game not in Odds API produced
  "no Pinnacle or sharp-book line" alerts. Suppressed; see vault
  Odds-API-NFL-Keys-and-Preseason-Gap.md.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque
from typing import Dict, List, Optional

from loguru import logger

# ── Tuning ───────────────────────────────────────────────────────
FAST_MOVE_POLL_SECS = 30      # poll cadence
FAST_MOVE_PP = 0.05           # mid move threshold (5pp) — NFL lines move slower than crypto
FAST_MOVE_WINDOW = 120        # s — lookback for the move
FAST_MOVE_COOLDOWN = 180      # s — min gap between alerts per market
FAST_MOVE_WARMUP = 90         # s — suppress moves after (re)start (snapshot artifacts)
MAX_MARKETS = 60              # cap on tracked markets
EDGE_FLOOR_PP = 6.0           # min Vegas-vs-PM gap to emit a trade signal

# Dedup table for fast-move alerts (reuse sport_edge_alert_dedup via the
# generic alert module; this module just detects + formats the move).


def _fetch_nfl_games() -> List[Dict]:
    """Fetch upcoming NFL game events with live ML quotes from the US-sports
    backend. Returns list of {slug, home, away, bid, ask, mid, updated_at}."""
    try:
        from polymarket_us import PolymarketUS
        client = PolymarketUS()
        raw = client.search.query({"query": "NFL", "status": "upcoming", "limit": 50})
        events = raw.get("events", []) if isinstance(raw, dict) else raw
    except Exception as e:
        logger.debug(f"NFL fast-move fetch failed: {e}")
        return []

    games = []
    if not isinstance(events, list):
        logger.debug("NFL fast-move: events not a list, skipping")
        return []
    for ev in events:
        if not ev.get("gameId"):
            continue
        for m in (ev.get("markets") or []):
            if m.get("sportsMarketType") != "football_team_full_game_winner":
                continue
            bid = (m.get("bestBidQuote") or {}).get("value")
            ask = (m.get("bestAskQuote") or {}).get("value")
            if bid is None or ask is None:
                continue
            try:
                bid = float(bid)
                ask = float(ask)
            except (TypeError, ValueError):
                continue
            parts = ev.get("title", "").split(" vs. ")
            games.append({
                "slug": m.get("slug"),
                "home": parts[0] if parts else "",
                "away": parts[-1] if len(parts) > 1 else "",
                "bid": bid,
                "ask": ask,
                "mid": (bid + ask) / 2.0,
                "updated_at": m.get("updatedAt", ""),
            })
            break  # only the ML market per game
    return games


class FastMoveMonitor:
    """Tracks NFL game ML mids and fires alerts on fast moves."""

    def __init__(self, poll_secs: int = FAST_MOVE_POLL_SECS,
                 move_pp: float = FAST_MOVE_PP,
                 window: int = FAST_MOVE_WINDOW,
                 cooldown: int = FAST_MOVE_COOLDOWN):
        self.poll_secs = poll_secs
        self.move_pp = move_pp
        self.window = window
        self.cooldown = cooldown
        self.mid_hist: Dict[str, deque] = defaultdict(deque)
        self.last_alert: Dict[str, float] = {}
        self.start_ts = time.time()

    def _warmup_done(self) -> bool:
        return (time.time() - self.start_ts) > FAST_MOVE_WARMUP

    def _check(self, game: Dict) -> Optional[Dict]:
        """Detect a fast move for one game. Returns move event or None."""
        slug = game["slug"]
        mid = game["mid"]
        now = time.time()
        hist = self.mid_hist[slug]
        hist.append((now, mid))
        while hist and now - hist[0][0] > self.window:
            hist.popleft()
        if len(hist) < 2:
            return None
        old = hist[0][1]
        if abs(mid - old) < self.move_pp:
            return None
        if now - self.last_alert.get(slug, 0.0) < self.cooldown:
            return None
        self.last_alert[slug] = now
        return {
            "slug": slug,
            "home": game["home"],
            "away": game["away"],
            "from": round(old, 3),
            "to": round(mid, 3),
            "delta": round(mid - old, 3),
            "ts": round(now, 1),
        }

    def scan(self, games: List[Dict]) -> List[Dict]:
        """Run detection over all games. Returns list of move events."""
        if not self._warmup_done():
            return []
        moves = []
        for g in games:
            ev = self._check(g)
            if ev:
                moves.append(ev)
        return moves


def _executable_edge_from_book(book: Dict, side: str, target_usd: float = 100.0,
                              max_slip_bps: float = 50.0) -> Dict:
    """Compute executable edge from a US-sports order book.

    side = "YES" (buy the long/team) or "NO" (buy the short). Walks the
    offers (asks) for a BUY, computing VWAP fill price for target_usd, then
    returns executable_price, fillable_usd, slippage_bps, and whether the
    fill is within slip/spread caps. Mirrors the CLOB executable_edge logic
    but on the US-sports book (which has real bids/offers + qty).
    """
    md = book.get("marketData", book) if isinstance(book, dict) else {}
    # BUY hits offers (asks); SELL hits bids. For a YES buy we hit offers.
    levels = md.get("offers", []) if side == "YES" else md.get("bids", [])
    if not levels:
        return {"available": False, "reason": "no book"}
    # Walk levels to fill target_usd
    remaining = target_usd
    fill_qty = 0.0
    fill_cost = 0.0
    best = float(levels[0]["px"]["value"])
    for lv in levels:
        px = float(lv["px"]["value"])
        qty = float(lv["qty"])
        cost_at_level = px * qty
        if cost_at_level >= remaining:
            take_qty = remaining / px
            fill_qty += take_qty
            fill_cost += take_qty * px
            remaining = 0
            break
        else:
            fill_qty += qty
            fill_cost += cost_at_level
            remaining -= cost_at_level
    if remaining > 0:
        # Not enough depth for target — fill what's there
        if fill_qty <= 0:
            return {"available": False, "reason": "no depth"}
    vwap = fill_cost / fill_qty if fill_qty > 0 else 0.0
    slip_bps = (vwap - best) / best * 10000 if best > 0 else 0.0
    fillable_usd = fill_cost
    return {
        "available": True,
        "executable_price": round(vwap, 4),
        "best_price": round(best, 4),
        "fillable_usd": round(fillable_usd, 2),
        "slippage_bps": round(slip_bps, 1),
        "tradeable": slip_bps <= max_slip_bps and fillable_usd >= 15.0,
    }


def _fetch_us_book(slug: str) -> Optional[Dict]:
    """Fetch the US-sports order book for a market slug."""
    try:
        from polymarket_us import PolymarketUS
        client = PolymarketUS()
        return client.markets.book(slug)
    except Exception as e:
        logger.debug(f"NFL fast-move book fetch failed ({slug}): {e}")
        return None


def _rec_size(fillable_usd: float) -> float:
    """Conservative recommended size: 25% of fillable depth, capped at $500."""
    return min(max(fillable_usd * 0.25, 0.0), 500.0)


def _fetch_us_book_for_game(home: str, away: str) -> Optional[Dict]:
    """Fetch the US-sports market + order book for a matchup.

    Returns {"market": <market dict>, "book": <book dict>} or None. The
    market carries marketSides (team → long/short + quote); the book carries
    bids/offers with qty for executable sizing.
    """
    try:
        from polymarket_us import PolymarketUS
        client = PolymarketUS()
        raw = client.search.query({"query": f"{home} {away}", "status": "upcoming", "limit": 10})
        events = raw.get("events", []) if isinstance(raw, dict) else raw
        for ev in events:
            if not ev.get("gameId"):
                continue
            for m in (ev.get("markets") or []):
                if m.get("sportsMarketType") != "football_team_full_game_winner":
                    continue
                slug = m.get("slug")
                book = client.markets.book(slug)
                return {"market": m, "book": book}
    except Exception as e:
        logger.debug(f"NFL fast-move US book fetch failed ({home} vs {away}): {e}")
    return None


def _executable_for_team(us: Dict, team: str, home: str, away: str) -> Optional[Dict]:
    """Compute the executable buy price for a team's YES from the US book.

    The US book is keyed to the LONG side (marketSides[long=True]). To buy
    the long team's YES, walk the offers (asks). To buy the short team's YES
    (= sell the long), walk the bids and take 1 - bid-VWAP. Returns the
    _executable_edge_from_book result with the correct side's price.
    """
    if not us:
        return None
    market = us.get("market", {})
    book = us.get("book", {})
    # Find the team's side
    side_long = None
    for s in (market.get("marketSides") or []):
        desc = (s.get("description") or "").lower()
        team_l = team.lower()
        if team_l in desc or desc in team_l:
            side_long = s.get("long")
            break
    if side_long is None:
        return None
    if side_long:
        # Long team: buy YES by hitting offers (asks)
        return _executable_edge_from_book(book, "YES")
    else:
        # Short team: buy YES = sell the long side = hit bids, then 1 - price
        ex = _executable_edge_from_book(book, "NO")
        if ex and ex.get("available"):
            ex["executable_price"] = round(1.0 - ex["executable_price"], 4)
            ex["best_price"] = round(1.0 - ex["best_price"], 4)
        return ex


def _fire_alert(move: Dict) -> None:
    """Send a Telegram fast-move alert with an executable Vegas-vs-PM signal.

    On a fast PM move, fetch Pinnacle + the US-sports order book for that
    game. Compute the EXECUTABLE edge (walk the ask book, net of taker fee)
    vs Vegas — not the mid. Emit a BUY/SELL with rec size only when the
    executable edge clears the floor AND depth is real. This makes the alert
    genuinely tradeable ("buy at this price, this size") instead of a mid
    comparison you can't actually fill at.
    """
    try:
        from scripts.openclaw_alerts import alert_openclaw
        arrow = "↑" if move["delta"] > 0 else "↓"
        # Shorten team names: "Seattle Seahawks" → "Seahawks"
        home_short = move['home'].split()[-1] if move['home'] else ""
        away_short = move['away'].split()[-1] if move['away'] else ""
        lines = [
            f"🏈 NFL Fast Move — {home_short} vs {away_short}",
            f"ML mid {move['from']:.3f} → {move['to']:.3f} ({arrow}{abs(move['delta'])*100:.1f}pp)",
        ]

        try:
            from scripts.cross_sport_drift import fetch_pinnacle_sport, fetch_sharp_consensus_sport
            from execution.fee_model import taker_fee_fraction
            # Try BOTH NFL sport keys (regular + preseason). Preseason games
            # live under a separate Odds API key; querying only the regular
            # key returns no line for them (the "no Pinnacle line" bug).
            # If Pinnacle has no line at all (common in preseason), fall back
            # to the sharp US-book consensus (DK/FD/MGM/Caesars/Fanatics).
            game = None
            source = "Pinnacle"
            for _key in ("americanfootball_nfl", "americanfootball_nfl_preseason"):
                try:
                    _games = fetch_pinnacle_sport(_key)
                except Exception:
                    _games = []
                game = next((g for g in _games
                             if (g["home"] == move["home"] and g["away"] == move["away"]) or
                                (g["home"] == move["away"] and g["away"] == move["home"])), None)
                if game:
                    break
            if not game:
                # Fall back to sharp-book consensus (Pinnacle absent).
                for _key in ("americanfootball_nfl", "americanfootball_nfl_preseason"):
                    try:
                        _games = fetch_sharp_consensus_sport(_key)
                    except Exception:
                        _games = []
                    game = next((g for g in _games
                                 if (g["home"] == move["home"] and g["away"] == move["away"]) or
                                    (g["home"] == move["away"] and g["away"] == move["home"])), None)
                    if game:
                        source = "Sharp consensus"
                        break
            if not game:
                # Suppress the alert entirely when the Odds API has no Vegas
                # anchor for this game (preseason data-availability gap). A
                # fast-move alert with no Vegas-vs-PM read is pure noise.
                # Log silently so we can verify the monitor is firing and not
                # silently broken. Approved 2026-08-23.
                logger.info(
                    f"NFL fast-move: no Vegas line for {move['home']} vs "
                    f"{move['away']}, suppressed (Odds API data-availability gap)"
                )
                return
            else:
                vegas = game["outcomes"]
                # Fetch the US-sports book for the moved market
                book = _fetch_us_book_for_game(move["home"], move["away"])
                signal_lines = []
                for team, v_prob in vegas.items():
                    # Determine the side to buy: if PM cheaper than Vegas, BUY
                    # the team's YES; else consider SELL (buy the other side).
                    # We need the team's executable ask price.
                    ex = _executable_for_team(book, team, move["home"], move["away"])
                    if not ex or not ex.get("available"):
                        continue
                    exec_price = ex["executable_price"]
                    # Net edge: Vegas prob - executable price, minus taker fee
                    fee = taker_fee_fraction(exec_price, "polymarket", "sports")
                    net_edge = (v_prob - exec_price) - fee
                    net_pp = net_edge * 100
                    team_short = team.split()[-1] if team else ""
                    if net_pp >= EDGE_FLOOR_PP:
                        rec = _rec_size(ex.get("fillable_usd", 0))
                        signal_lines.append(
                            f"💰 BUY {team_short} YES @ {exec_price:.3f} — "
                            f"{source} {v_prob:.0%}, net +{net_pp:.1f}pp, depth ${ex.get('fillable_usd',0):.0f} → rec ${rec:.0f}"
                        )
                    elif net_pp <= -EDGE_FLOOR_PP:
                        rec = _rec_size(ex.get("fillable_usd", 0))
                        signal_lines.append(
                            f"🔻 SELL {team_short} YES @ {exec_price:.3f} — "
                            f"{source} {v_prob:.0%}, net {net_pp:+.1f}pp, depth ${ex.get('fillable_usd',0):.0f} → rec ${rec:.0f}"
                        )
                    # "in line" teams are skipped — no edge = no signal
                if signal_lines:
                    lines.append("")  # one blank line before signals
                    lines.extend(signal_lines)
        except Exception as e:
            logger.debug(f"NFL fast-move executable compare failed: {e}")
            lines.append("(executable comparison unavailable)")

        alert_openclaw("\n".join(lines))
    except Exception as e:
        logger.debug(f"NFL fast-move alert send failed: {e}")


async def run_fast_move_monitor(once: bool = False) -> Dict:
    """Main loop: poll NFL games, detect moves, fire alerts.

    once=True → single pass (for testing). Returns {"scanned": n, "moves": n}.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("SMART_WALLET_ALERT_SEND") == "0":
        return {"scanned": 0, "moves": 0}

    monitor = FastMoveMonitor()
    total_moves = 0
    polls = 0
    while True:
        try:
            games = await asyncio.to_thread(_fetch_nfl_games)
            moves = monitor.scan(games)
            for m in moves:
                _fire_alert(m)
            total_moves += len(moves)
            polls += 1
            # Heartbeat every ~10 min (20 polls @ 30s) so a dead loop is
            # detectable in logs — silent death was a QA finding (2026-08-22).
            if polls % 20 == 0:
                logger.info(f"NFL fast-move heartbeat: {polls} polls, "
                            f"{len(games)} games, {total_moves} moves total")
            if once:
                return {"scanned": len(games), "moves": total_moves}
        except Exception as e:
            # A transient failure (malformed API response, network blip) must
            # NEVER kill the loop — log and continue on the next poll.
            logger.warning(f"NFL fast-move iteration failed: {e}")
            if once:
                return {"scanned": 0, "moves": 0}
        await asyncio.sleep(monitor.poll_secs)


if __name__ == "__main__":
    import asyncio
    res = asyncio.run(run_fast_move_monitor(once=True))
    print("RESULT:", res)
