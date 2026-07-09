#!/usr/bin/env python3
"""
wc_edge_scanner.py — World Cup + MLB cross-platform edge scanner

Runs every 30 min via cron. Scans:
  1. FIFA World Cup h2h (Pinnacle devig vs Polymarket) — starts Jun 11
  2. MLB game lines + O/U (Pinnacle devig vs Polymarket)
  3. WC futures (Betfair outright vs Polymarket winner market)
  4. THREE-WAY comparison: Vegas (Pinnacle) vs Polymarket vs Kalshi — all MLB games

Flags edges >= MIN_EDGE_PCT, logs to shadow_trades.db.
Three-way scan catches mispricings where one platform is out of step with the others.

Usage:
  python3 odds/wc_edge_scanner.py          # full scan, print results
  python3 odds/wc_edge_scanner.py --dry    # scan only, no shadow logging
  python3 odds/wc_edge_scanner.py --sport wc   # WC only
  python3 odds/wc_edge_scanner.py --sport mlb  # MLB only
  python3 odds/wc_edge_scanner.py --sport threeway  # Three-way only
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

MIN_EDGE_PCT = 0.03   # 3pp minimum (r/algobetting community consensus + our calibration)
MAX_EDGE_PCT = 0.15   # 15% cap (P1 recalibration — large claimed edges are usually wrong)
ALERT_EDGE   = 0.08   # 8pp+ → worth acting on (raised from 5pp — 5-8pp range was noise)

# Alert noise gates
ALERT_MIN_MINS = 0     # don't alert after game starts
ALERT_MAX_MINS = 720   # 12h max — edges further out are noise (markets haven't tightened)
ALERT_PER_SCAN = 5     # cap: only the strongest edges per scan
ALERT_THREE_WAY_MIN = 8.0  # three-way: only alert when gap ≥8pp


def _fmt_edge(e) -> str:
    dir_sym = "↑" if e.direction == "BUY" else "↓"
    alert = " ⚡" if abs(e.edge_pct) >= ALERT_EDGE else ""
    tradeable = " ✓" if getattr(e, "tradeable", False) else ""
    # Support both Edge (soccer) and MLBEdge schemas
    participant = getattr(e, "participant", None) or getattr(e, "bet_team", "?")
    book_prob   = getattr(e, "book_prob", None) or getattr(e, "true_prob", 0)
    poly_price  = getattr(e, "poly_price", None) or getattr(e, "polymarket_price", 0)
    return (
        f"  {dir_sym} {participant:<25} "
        f"book={book_prob:.1%}  PM={poly_price:.1%}  "
        f"edge={e.edge_pct:+.1%}{alert}  {e.direction}{tradeable}"
    )


def _fmt_title(e) -> str:
    return getattr(e, "event_title", None) or getattr(e, "game_title", "")


async def scan_wc_matches(dry: bool = False) -> list:
    from odds.soccer_match_edge import find_soccer_match_edges
    edges = await find_soccer_match_edges(min_edge=MIN_EDGE_PCT)
    # Apply P1 edge cap
    edges = [e for e in edges if abs(e.edge_pct) <= MAX_EDGE_PCT]
    if dry:
        for e in edges:
            e.tradeable = False
    return edges


async def scan_wc_futures(dry: bool = False) -> list:
    from odds.soccer_futures_edge import find_soccer_futures_edges
    edges = await find_soccer_futures_edges(min_edge=MIN_EDGE_PCT)
    edges = [e for e in edges if abs(e.edge_pct) <= MAX_EDGE_PCT]
    if dry:
        for e in edges:
            e.tradeable = False
    return edges


async def scan_mlb(dry: bool = False) -> list:
    try:
        from odds.baseball_edge import find_baseball_edges
        edges = await find_baseball_edges(min_edge=MIN_EDGE_PCT)
        edges = [e for e in edges if abs(e.edge_pct) <= MAX_EDGE_PCT]
        if dry:
            for e in edges:
                e.tradeable = False
        return edges
    except Exception as ex:
        logger.warning(f"MLB scan failed: {ex}")
        return []


def scan_three_way() -> list[dict]:
    """
    Three-way cross-platform comparison: Vegas (Pinnacle) vs Polymarket vs Kalshi.

    For each MLB game where all three sources have prices, computes:
      - vegas_fair  : Pinnacle devigged probability
      - pm_price    : Polymarket YES price
      - kalshi_mid  : Kalshi mid price (yes_bid + yes_ask) / 2

    Flags games where ANY two platforms differ by >= MIN_EDGE_PCT.
    Returns list of comparison dicts sorted by max pairwise gap.
    """
    import math

    # ── Fetch Kalshi game markets ────────────────────────────────────────
    try:
        from odds.kalshi_sports import fetch_mlb_game_markets
        kalshi_games = fetch_mlb_game_markets()
    except Exception as ex:
        logger.warning(f"Kalshi fetch failed: {ex}")
        kalshi_games = []

    # Build lookup: (home_code, away_code) → {home_mid, away_mid}
    # Also index by team code for fuzzy matching
    kalshi_by_code: dict[str, float] = {}  # team_code -> yes_mid
    for g in kalshi_games:
        kalshi_by_code[g["home_code"]] = g["home_yes"]
        kalshi_by_code[g["away_code"]] = g["away_yes"]

    # ── Fetch Vegas + PM baseball edges ─────────────────────────────────
    import asyncio as _asyncio
    import concurrent.futures as _cf
    try:
        from odds.baseball_edge import find_baseball_edges
        with _cf.ThreadPoolExecutor(max_workers=1) as pool:
            mlb_edges = pool.submit(_asyncio.run, find_baseball_edges(min_edge=0.0)).result(timeout=60)
    except Exception as ex:
        logger.warning(f"MLB baseball_edge failed: {ex}")
        mlb_edges = []

    # Group edges by game: {game_title: {team: {vegas_fair, pm_price}}}
    game_data: dict[str, dict] = {}
    for e in mlb_edges:
        if e.market_type not in ("h2h", "moneyline", "winner", "spreads"):
            continue
        title = e.game_title
        if title not in game_data:
            game_data[title] = {
                "home_team": e.home_team,
                "away_team": e.away_team,
                "commence_time": e.commence_time,
                "teams": {},
            }
        game_data[title]["teams"][e.bet_team] = {
            "vegas_fair": e.odds_api_prob,
            "pm_price":   e.polymarket_price,
        }

    results = []
    for title, gd in game_data.items():
        home = gd.get("home_team", "")
        away = gd.get("away_team", "")

        for team_name, prices in gd["teams"].items():
            vegas = prices.get("vegas_fair")
            pm    = prices.get("pm_price")
            if not vegas or not pm:
                continue

            # Match this team to Kalshi code
            kalshi_mid = None
            for code, mid in kalshi_by_code.items():
                from odds.kalshi_sports import TEAM_CODE_MAP
                canonical = TEAM_CODE_MAP.get(code, "").lower()
                if any(w in team_name.lower() for w in canonical.split() if len(w) > 3):
                    kalshi_mid = mid
                    break

            # Compute pairwise gaps
            gaps = {}
            if vegas and pm:
                gaps["vegas_vs_pm"] = round((vegas - pm) * 100, 2)
            if vegas and kalshi_mid:
                gaps["vegas_vs_kalshi"] = round((vegas - kalshi_mid) * 100, 2)
            if pm and kalshi_mid:
                gaps["pm_vs_kalshi"] = round((pm - kalshi_mid) * 100, 2)

            if not gaps:
                continue

            max_gap = max(abs(v) for v in gaps.values())
            if max_gap < MIN_EDGE_PCT * 100:
                continue
            if max_gap > MAX_EDGE_PCT * 100:
                continue  # P1 cap

            results.append({
                "team":         team_name,
                "game":         title,
                "commence":     gd.get("commence_time", ""),
                "vegas_fair":   round(vegas * 100, 1) if vegas else None,
                "pm_price":     round(pm * 100, 1) if pm else None,
                "kalshi_mid":   round(kalshi_mid * 100, 1) if kalshi_mid else None,
                "gaps":         gaps,
                "max_gap_pp":   round(max_gap, 1),
                "best_buy":     _best_buy(vegas, pm, kalshi_mid),
                "worst_sell":   _worst_sell(vegas, pm, kalshi_mid),
            })

    return sorted(results, key=lambda x: x["max_gap_pp"], reverse=True)


def _best_buy(vegas: float | None, pm: float | None, kalshi: float | None) -> str:
    """Platform with the lowest price (best place to buy YES)."""
    prices = {"Vegas": vegas, "PM": pm, "Kalshi": kalshi}
    prices = {k: v for k, v in prices.items() if v is not None}
    if not prices:
        return "?"
    return min(prices, key=lambda k: prices[k])


def _worst_sell(vegas: float | None, pm: float | None, kalshi: float | None) -> str:
    """Platform with the highest price (best place to sell/buy NO)."""
    prices = {"Vegas": vegas, "PM": pm, "Kalshi": kalshi}
    prices = {k: v for k, v in prices.items() if v is not None}
    if not prices:
        return "?"
    return max(prices, key=lambda k: prices[k])


DEDUP_FILE    = os.path.expanduser("~/.openclaw/wc_alert_dedup.json")
DEDUP_TTL_H   = 6      # expire state after 6h (well past game end)
REFIRE_PP     = 2.0    # re-alert (WIDENING) if edge grows ≥2pp
LAST_CALL_MIN = 45     # LAST CALL fires when ≤45min to game start


def _load_dedup() -> dict:
    if os.path.exists(DEDUP_FILE):
        try:
            import json
            with open(DEDUP_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_dedup(state: dict) -> None:
    import json
    os.makedirs(os.path.dirname(DEDUP_FILE), exist_ok=True)
    with open(DEDUP_FILE, "w") as f:
        json.dump(state, f)


def _mins_to_game(commence_time_str: str) -> float | None:
    """Parse ISO commence_time string → minutes until game. Negative = already started."""
    if not commence_time_str:
        return None
    try:
        ct_str = str(commence_time_str)[:19].replace(" ", "T")
        if not ct_str.endswith("Z"):
            ct_str += "Z"
        ct = datetime.strptime(ct_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        delta = (ct - datetime.now(timezone.utc)).total_seconds() / 60
        return delta
    except Exception:
        return None


def _classify_alert(key: str, edge_pp: float, mins_left: float | None, state: dict, now: float) -> str | None:
    """
    Returns alert type string or None (suppress).

    Types: "NEW", "WIDENING", "LAST_CALL"
    EDGE_GONE removed — was the #1 noise source (fires every 30min for half the games).
    If we didn't act on an edge, who cares it's gone?
    """
    entry = state.get(key)
    active = entry and (now - entry["alerted_at"] <= DEDUP_TTL_H * 3600)

    # Game-time gate: only alert for games within the window
    if mins_left is not None:
        if mins_left < ALERT_MIN_MINS or mins_left > ALERT_MAX_MINS:
            return None

    # Only fire positive alerts for edges ≥ ALERT_EDGE
    if edge_pp < ALERT_EDGE * 100:
        return None

    # LAST CALL — game starting soon, haven't sent last call yet
    if mins_left is not None and 0 < mins_left <= LAST_CALL_MIN:
        if not (active and entry.get("last_call_sent")):
            return "LAST_CALL"
        return None  # already sent last call

    # WIDENING — edge grew ≥ REFIRE_PP since last alert
    if active and (edge_pp - entry["edge_pct"]) >= REFIRE_PP:
        return "WIDENING"

    # NEW — first time seeing this edge
    if not active:
        return "NEW"

    return None  # suppress — same edge, no meaningful change


def _format_alert(alert_type: str, label: str, title: str, edge_pp: float,
                  direction: str, book_prob: float, poly_price: float,
                  mins_left: float | None, prev_pp: float | None,
                  extra: str = "") -> list[str]:
    """Format a single typed alert block."""
    icons = {"NEW": "🟢", "WIDENING": "📈", "LAST_CALL": "⏰"}
    headers = {
        "NEW":       "NEW EDGE",
        "WIDENING":  "WIDENING",
        "LAST_CALL": "LAST CALL",
    }
    icon   = icons[alert_type]
    header = headers[alert_type]
    arrow  = "↑" if direction == "BUY" else "↓"

    lines = [f"{icon} <b>{header}</b>"]
    if alert_type == "WIDENING" and prev_pp is not None:
        lines[0] += f"  (+{edge_pp - prev_pp:.1f}pp,  was {prev_pp:.1f}pp)"

    lines.append(
        f"{arrow} <b>{label}</b>  Vegas {book_prob:.1%}  Poly {poly_price:.1%}  "
        f"edge <b>{edge_pp/100:+.1%}</b>  [{direction}]"
    )
    if title:
        lines.append(f"   {title[:55]}")
    if mins_left is not None and mins_left > 0:
        h, m = divmod(int(mins_left), 60)
        time_str = f"{h}h {m}m" if h else f"{m}m"
        if alert_type == "LAST_CALL":
            lines.append(f"   ⚠️ Game starts in {time_str} — act now or pass")
        else:
            lines.append(f"   Game in {time_str}")
    if extra:
        lines.append(f"   {extra}")
    return lines


def _log_alert_row(sport: str, alert_type: str, participant: str, title: str,
                   direction: str, edge_pp: float, book_prob: float | None,
                   poly_price: float | None, kalshi_mid: float | None,
                   mins_left: float | None, dedup_key: str) -> None:
    """Append-only log of every FIRED edge alert (audit 2026-07-07: alerts were
    Telegram-only — delivered then discarded, so direction/outcome/CLV could
    never be scored). Probabilities stored 0-1. Never raises."""
    try:
        import sqlite3
        from pathlib import Path
        db = Path(__file__).resolve().parent.parent / "storage" / "shadow_trades.db"
        conn = sqlite3.connect(str(db), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""CREATE TABLE IF NOT EXISTS edge_alert_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fired_at     TEXT NOT NULL,
            sport        TEXT,
            alert_type   TEXT,      -- NEW / WIDENING / LAST_CALL
            participant  TEXT,
            event_title  TEXT,
            direction    TEXT,      -- BUY / SELL
            edge_pp      REAL,
            book_prob    REAL,      -- 0-1 (Vegas devig)
            poly_price   REAL,      -- 0-1
            kalshi_mid   REAL,      -- 0-1, three-way only
            mins_to_game REAL,
            dedup_key    TEXT
        )""")
        conn.execute(
            "INSERT INTO edge_alert_log (fired_at, sport, alert_type, participant,"
            " event_title, direction, edge_pp, book_prob, poly_price, kalshi_mid,"
            " mins_to_game, dedup_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), sport, alert_type,
             (participant or "")[:80], (title or "")[:180], direction,
             round(edge_pp, 2),
             round(book_prob, 4) if book_prob is not None else None,
             round(poly_price, 4) if poly_price is not None else None,
             round(kalshi_mid, 4) if kalshi_mid is not None else None,
             round(mins_left, 1) if mins_left is not None else None,
             dedup_key),
        )
        conn.commit()
        conn.close()
    except Exception as ex:
        logger.debug(f"edge_alert_log write failed: {ex}")


def _send_edge_alerts(results: dict, three_way: list) -> None:
    """Classify and send typed edge alerts. Deduped per alert type."""
    try:
        from scripts.alert_formatter import send_telegram
    except Exception:
        return

    now   = datetime.now(timezone.utc).timestamp()
    state = _load_dedup()

    all_blocks: list[list[str]] = []
    state_updates: dict = {}

    # ── Two-platform edges (WC + MLB) ────────────────────────────────────
    for sport_name, edges in results.items():
        seen_keys: set[str] = set()
        scan_count = 0

        for e in sorted(edges, key=lambda x: abs(x.edge_pct), reverse=True):
            if scan_count >= ALERT_PER_SCAN:
                break
            participant = getattr(e, "participant", None) or getattr(e, "bet_team", "?")
            book_prob   = getattr(e, "book_prob", None) or getattr(e, "true_prob", 0)
            poly_price  = getattr(e, "poly_price", None) or getattr(e, "polymarket_price", 0)
            title       = getattr(e, "event_title", None) or getattr(e, "game_title", "")
            ct_raw      = getattr(e, "commence_time", None) or getattr(e, "game_date", "")
            dir_str     = "BUY" if e.direction == "BUY" else "SELL"
            edge_pp     = abs(e.edge_pct) * 100
            mins_left   = _mins_to_game(str(ct_raw)) if ct_raw else None
            key         = f"{sport_name}:{participant}:{dir_str}"
            seen_keys.add(key)

            alert_type = _classify_alert(key, edge_pp, mins_left, state, now)
            if not alert_type:
                continue

            prev_pp = state[key]["edge_pct"] if key in state else None
            block = _format_alert(alert_type, participant, title, edge_pp,
                                   dir_str, book_prob, poly_price, mins_left, prev_pp)
            all_blocks.append(block)
            _log_alert_row(sport_name, alert_type, participant, title, dir_str,
                           edge_pp, book_prob or None, poly_price or None, None,
                           mins_left, key)
            scan_count += 1

            entry = state.get(key, {"edge_pct": edge_pp, "alerted_at": now,
                                     "last_call_sent": False})
            entry = dict(entry)
            entry["edge_pct"]  = edge_pp
            entry["alerted_at"] = now
            if alert_type == "LAST_CALL":
                entry["last_call_sent"] = True
            state_updates[key] = entry

    # ── Three-way (Vegas vs PM vs Kalshi) ────────────────────────────────
    # One game, one alert: if the per-sport engine alerted a team this scan or
    # within the dedup window, its threeway row is the same edge seen through a
    # second engine — skip it (was double-firing every MLB game, 2026-07-06).
    sport_alerted_teams = {
        k.split(":")[1]
        for k in (*state_updates, *state)
        if not k.startswith("threeway:") and k.count(":") == 2
        and (k in state_updates
             or now - state[k]["alerted_at"] <= DEDUP_TTL_H * 3600)
    }

    seen_tw_keys: set[str] = set()
    tw_count = 0

    for r in three_way:
        if tw_count >= ALERT_PER_SCAN:
            break
        edge_pp   = r["max_gap_pp"]
        if edge_pp < ALERT_THREE_WAY_MIN:
            continue
        if r["team"] in sport_alerted_teams:
            continue
        key       = f"threeway:{r['team']}:{r['best_buy']}"
        seen_tw_keys.add(key)
        mins_left = _mins_to_game(r.get("commence", ""))

        alert_type = _classify_alert(key, edge_pp, mins_left, state, now)
        if not alert_type:
            continue

        prev_pp = state[key]["edge_pct"] if key in state else None
        v  = f"Vegas {r['vegas_fair']:.1f}%" if r.get("vegas_fair") else ""
        pm = f"PM {r['pm_price']:.1f}%"      if r.get("pm_price")   else ""
        kl = f"Kalshi {r['kalshi_mid']:.1f}%" if r.get("kalshi_mid") else ""
        extra = f"{v}  {pm}  {kl}  → Buy {r['best_buy']} / Sell {r['worst_sell']}"

        block = _format_alert(alert_type, r["team"], r.get("game", ""), edge_pp,
                               r["best_buy"], r.get("vegas_fair", 0) / 100,
                               r.get("pm_price", 0) / 100, mins_left, prev_pp, extra)
        all_blocks.append(block)
        _log_alert_row("threeway", alert_type, r["team"], r.get("game", ""),
                       r["best_buy"], edge_pp,
                       (r["vegas_fair"] / 100) if r.get("vegas_fair") else None,
                       (r["pm_price"] / 100) if r.get("pm_price") else None,
                       (r["kalshi_mid"] / 100) if r.get("kalshi_mid") else None,
                       mins_left, key)
        tw_count += 1

        entry = state.get(key, {"edge_pct": edge_pp, "alerted_at": now,
                                  "last_call_sent": False})
        entry = dict(entry)
        entry["edge_pct"]   = edge_pp
        entry["alerted_at"] = now
        if alert_type == "LAST_CALL":
            entry["last_call_sent"] = True
        state_updates[key] = entry

    if not all_blocks:
        return

    # Assemble message — one block per alert, separated by blank line
    msg_lines: list[str] = []
    for block in all_blocks:
        msg_lines.extend(block)
        msg_lines.append("")

    # Persist state
    state = {k: v for k, v in state.items() if now - v["alerted_at"] <= DEDUP_TTL_H * 3600}
    state.update(state_updates)
    _save_dedup(state)

    send_telegram("\n".join(msg_lines).strip())


async def main(sport: str = "all", dry: bool = False, alert: bool = False):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}")
    print(f"  WC + MLB EDGE SCANNER  —  {ts}")
    print(f"  Min edge: {MIN_EDGE_PCT:.0%}  Cap: {MAX_EDGE_PCT:.0%}  Mode: {'DRY RUN' if dry else 'LIVE'}")
    print(f"{'='*60}")

    tasks = {}
    if sport in ("all", "wc"):
        tasks["WC Matches"] = scan_wc_matches(dry)
        tasks["WC Futures"] = scan_wc_futures(dry)
    if sport in ("all", "mlb"):
        tasks["MLB"] = scan_mlb(dry)

    results = {}
    for name, coro in tasks.items():
        try:
            results[name] = await coro
        except Exception as ex:
            logger.error(f"{name} scan error: {ex}")
            results[name] = []

    total = 0
    actionable = 0
    tw: list = []
    for name, edges in results.items():
        print(f"\n── {name} ({len(edges)} edges) ──")
        if not edges:
            print("  No edges found")
        for e in sorted(edges, key=lambda x: abs(x.edge_pct), reverse=True):
            print(_fmt_edge(e))
            print(f"     Market: {_fmt_title(e)}")
            ct = getattr(e, "commence_time", None) or getattr(e, "game_date", None)
            if ct:
                print(f"     Kickoff: {ct}")
            total += 1
            if abs(e.edge_pct) >= ALERT_EDGE:
                actionable += 1

    # ── Three-way: Vegas vs PM vs Kalshi ────────────────────────────────
    if sport in ("all", "mlb", "threeway"):
        print(f"\n── Three-Way: Vegas vs Polymarket vs Kalshi ──")
        try:
            tw = scan_three_way()  # noqa: F841  (also captured in outer tw)
        except Exception as ex:
            logger.error(f"Three-way scan error: {ex}")
            tw = []

        if not tw:
            print("  No cross-platform gaps found (book prices may not be live yet)")
        else:
            print(f"  {'Team':<28} {'Vegas':>6} {'PM':>6} {'Kalshi':>7} {'MaxGap':>7}  Best Buy → Best Sell")
            print(f"  {'-'*80}")
            for r in tw:
                v  = f"{r['vegas_fair']:.1f}%" if r['vegas_fair'] else "  —  "
                pm = f"{r['pm_price']:.1f}%"   if r['pm_price']  else "  —  "
                kl = f"{r['kalshi_mid']:.1f}%"  if r['kalshi_mid'] else "  —  "
                gap_flag = " ⚡" if r['max_gap_pp'] >= ALERT_EDGE * 100 else "  "
                print(
                    f"  {r['team']:<28} {v:>6} {pm:>6} {kl:>7} {r['max_gap_pp']:>6.1f}pp{gap_flag}"
                    f"  {r['best_buy']} → {r['worst_sell']}"
                )
                print(f"    └ {r['game'][:55]}  {r['commence'][:16]}")
            actionable += sum(1 for r in tw if r['max_gap_pp'] >= ALERT_EDGE * 100)
            total += len(tw)

    print(f"\n{'='*60}")
    print(f"  TOTAL: {total} signals | ACTIONABLE (≥{ALERT_EDGE:.0%}): {actionable}")
    if dry:
        print("  DRY RUN — no shadow trades logged")
    else:
        print("  Shadow trades logged for tradeable edges")
    print(f"{'='*60}\n")

    # ── Telegram delivery ─────────────────────────────────────────────────
    if alert and actionable > 0:
        try:
            _send_edge_alerts(results, tw if sport in ("all", "mlb", "threeway") else [])
        except Exception as ex:
            logger.warning(f"Telegram alert failed: {ex}")

    return total, actionable


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WC + MLB edge scanner")
    parser.add_argument("--dry", action="store_true", help="Scan only, no shadow logging")
    parser.add_argument("--alert", action="store_true", help="Send Telegram for actionable edges")
    parser.add_argument("--sport", choices=["all", "wc", "mlb", "threeway"], default="all")
    args = parser.parse_args()
    asyncio.run(main(sport=args.sport, dry=args.dry, alert=args.alert))
