#!/usr/bin/env python3
"""
cross_sport_drift.py — Cross-sport Pinnacle line drift scanner.

Fires every 5 minutes via tick_5min. Monitors MLB, WC Soccer, MLS, and UFC
for significant Vegas line shifts. On move > threshold since last snapshot,
fires a unified drift alert with Polymarket CLOB price comparison.

No ESPN score trigger — pure Vegas line movement detection.
Credits: bookmakers=pinnacle → 1 credit per sport per tick
         4 sports × 12 ticks/hr × 16hr/day ≈ 768 credits/day
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))
from odds.monitor_gate import gated_fetch_json

from scripts.alert_formatter import format_grid, send_telegram
from signals.alert_dispatch import TIER_DIGEST, dispatch

# ── Config ───────────────────────────────────────────────────────────────────
ODDS_API_KEY  = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"

DB_PATH = BASE_DIR / "storage" / "shadow_trades.db"

# Module-level executor — shared across sports and ticks
_EXECUTOR = ThreadPoolExecutor(max_workers=8)

# Per-game alert cooldown — prevents re-alerting on the same game within this window
# even if the line keeps moving tick-to-tick. sport_drift_dedup table was created
# for this but never wired up until now.
DRIFT_ALERT_COOLDOWN_SECS = 3600  # 1h per game

# Vegas-tier EV filter (2026-08-23, from edge_calibration): the BUY-side edge is
# regime-dependent. Buying cheap underdogs (<40% Vegas) = +15pp EV; buying
# favorites (>60%) = +9.3pp EV; the middle (40-60%) is NEGATIVE EV (-6 to -10pp).
# Only fire signals in the +EV tiers. VEGAS_UNDERDOG_MAX / VEGAS_FAVORITE_MIN are
# the Vegas devig probability bounds that define the +EV regimes.
VEGAS_UNDERDOG_MAX = 0.40   # Vegas prob below this = +EV underdog buy
VEGAS_FAVORITE_MIN = 0.60   # Vegas prob above this = +EV favorite buy


def _in_positive_ev_tier(vegas_prob: float) -> bool:
    """True if a Vegas devig prob is in a +EV regime (underdog <40% or favorite >60%).
    The 40-60% middle is negative EV per edge_calibration and is suppressed."""
    return vegas_prob < VEGAS_UNDERDOG_MAX or vegas_prob > VEGAS_FAVORITE_MIN

# ── Sport configs ─────────────────────────────────────────────────────────────
# Each config drives a full scan cycle for that sport.
# has_draw: whether the market has a 3-way (home/draw/away) outcome
# drift_pp: minimum pp shift to fire an alert
# active_months: skip entirely outside these months (saves credits)
SPORT_CONFIGS: List[Dict] = [
    {
        "name": "MLB",
        "odds_key": "baseball_mlb",
        "pm_tag": "baseball",
        "has_draw": False,
        "drift_pp": 8.0,
        "active_months": [4, 5, 6, 7, 8, 9, 10],
        "edge_floor_pp": 6.0,
    },
    {
        "name": "WC Soccer",
        "odds_key": "soccer_fifa_world_cup",
        "pm_tag": "fifa-world-cup",
        "has_draw": True,
        "drift_pp": 5.0,
        "active_months": [6, 7],
        "edge_floor_pp": 6.0,
    },
    {
        "name": "MLS",
        "odds_key": "soccer_usa_mls",
        "pm_tag": "mls",
        "has_draw": True,
        "drift_pp": 8.0,
        "active_months": [3, 4, 5, 6, 7, 8, 9, 10, 11],
        "edge_floor_pp": 6.0,
    },
    {
        "name": "UFC",
        "odds_key": "mma_mixed_martial_arts",
        "pm_tag": "ufc",
        "has_draw": False,
        "drift_pp": 10.0,
        "active_months": list(range(1, 13)),
        "edge_floor_pp": 6.0,
    },
    {
        "name": "NFL",
        "odds_key": "americanfootball_nfl",
        "pm_tag": "nfl",
        "has_draw": False,
        "drift_pp": 6.0,
        "active_months": [8, 9, 10, 11, 12, 1, 2],
        "edge_floor_pp": 6.0,
    },
    {
        "name": "NBA",
        "odds_key": "basketball_nba",
        "pm_tag": "nba",
        "has_draw": False,
        "drift_pp": 6.0,
        "active_months": [10, 11, 12, 1, 2, 3, 4, 5, 6],
        "edge_floor_pp": 6.0,
    },
]

# Team name aliases for cross-platform matching
ALIASES: Dict[str, List[str]] = {
    "usa": ["united states"],
    "united states": ["usa"],
    "korea": ["south korea", "korea republic"],
    "south korea": ["korea republic"],
    "korea republic": ["south korea"],
    "ivory coast": ["cote d'ivoire"],
    "cote d'ivoire": ["ivory coast"],
    "turkiye": ["turkey"],
    "turkey": ["turkiye"],
}

# Sport emoji map
SPORT_EMOJI: Dict[str, str] = {
    "MLB": "⚾",
    "WC Soccer": "⚽",
    "MLS": "⚽",
    "UFC": "🥊",
    "NFL": "🏈",
    "NBA": "🏀",
}


# ── DB ────────────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sport_line_snap (
            sport    TEXT,
            game_id  TEXT,
            outcome  TEXT,
            devig    REAL,
            ts       TEXT,
            PRIMARY KEY (sport, game_id, outcome)
        );
        CREATE TABLE IF NOT EXISTS sport_drift_dedup (
            sport         TEXT,
            game_id       TEXT,
            outcome       TEXT,
            last_alert_ts TEXT,
            PRIMARY KEY (sport, game_id, outcome)
        );
    """)
    conn.commit()


# ── HTTP ──────────────────────────────────────────────────────────────────────
def _get(url: str, params: Optional[dict] = None, timeout: int = 12) -> Optional[object]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[cross_sport_drift] GET {url[:70]} → {e}", flush=True)
        return None


# ── Name matching ─────────────────────────────────────────────────────────────
def _nmatch(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    if a == b or a in b or b in a:
        return True
    for alias in ALIASES.get(a, []):
        if alias in b or b in alias:
            return True
    return False


def _game_id(home: str, away: str, commence_time: str = "") -> str:
    """Include date so same matchup on consecutive days gets distinct IDs."""
    date_suffix = ""
    if commence_time:
        try:
            date_suffix = "_" + commence_time[:10]  # "2026-06-19"
        except Exception:
            pass
    h = home.lower().replace(" ", "_")
    a = away.lower().replace(" ", "_")
    return f"{h}_{a}{date_suffix}"


# ── Pinnacle odds ─────────────────────────────────────────────────────────────
def _imp(price: int) -> float:
    p = int(price)
    return (100 / (100 + p)) if p > 0 else (-p / (-p + 100))


# Sharp US books used for the consensus fallback when Pinnacle has no line.
# Pinnacle is sharpest; DK/FD/MGM/Caesars/Fanatics are the sharp US books.
# Soft/offshore books (Bovada, MyBookie) are excluded — they lag and add noise.
SHARP_BOOKS = ["pinnacle", "draftkings", "fanduel", "betmgm", "williamhill_us", "fanatics"]


def fetch_sharp_consensus_sport(odds_key: str) -> List[Dict]:
    """Fetch all h2h games for a sport, devigging a consensus across the sharp
    US books (Pinnacle + DK/FD/MGM/Caesars/Fanatics). Soft offshore books are
    excluded. Used as a fallback when Pinnacle has no line (common in preseason).

    Returns the same shape as fetch_pinnacle_sport:
      {"home", "away", "game_id", "outcomes": {name: devigged_prob}, "commence_time"}
    """
    if not ODDS_API_KEY:
        return []
    data = gated_fetch_json(f"{ODDS_API_BASE}/{odds_key}/odds", {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "american",
    })
    if not isinstance(data, list):
        return []

    games = []
    for event in data:
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        if not home or not away:
            continue
        ct = event.get("commence_time", "")
        if ct:
            try:
                start = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - start).total_seconds() / 3600 > 4:
                    continue  # Game almost certainly finished
            except (ValueError, TypeError):
                pass
        # Collect each sharp book's devigged 2-way probs.
        book_probs = []
        for bm in event.get("bookmakers", []):
            if bm.get("key") not in SHARP_BOOKS:
                continue
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                outcomes = mkt.get("outcomes", [])
                valid = [o for o in outcomes if o.get("price", 0) and o["price"] != 0]
                if len(valid) < 2:
                    continue
                raw = {o["name"]: _imp(o["price"]) for o in valid}
                total = sum(raw.values())
                if not (0.95 <= total <= 1.50):
                    continue
                book_probs.append({k: v / total for k, v in raw.items()})
        if not book_probs:
            continue
        # Average across the sharp books that carry the game.
        all_outcomes = set()
        for bp in book_probs:
            all_outcomes.update(bp.keys())
        consensus = {}
        for oc in all_outcomes:
            vals = [bp[oc] for bp in book_probs if oc in bp]
            consensus[oc] = sum(vals) / len(vals) if vals else 0
        total = sum(consensus.values())
        if total < 0.1:
            continue
        games.append({
            "home": home,
            "away": away,
            "game_id": _game_id(home, away, ct),
            "outcomes": {k: v / total for k, v in consensus.items()},
            "commence_time": ct,
        })
    return games


def fetch_pinnacle_sport(odds_key: str) -> List[Dict]:
    """Fetch all Pinnacle h2h games for a sport. Returns list of game dicts.

    Each game dict:
      {"home": str, "away": str, "game_id": str,
       "outcomes": {name: devigged_prob}, "commence_time": str}
    """
    if not ODDS_API_KEY:
        return []
    data = gated_fetch_json(f"{ODDS_API_BASE}/{odds_key}/odds", {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "american",
        "bookmakers": "pinnacle",
    })
    if not isinstance(data, list):
        return []

    games = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for event in data:
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        if not home or not away:
            continue
        # Skip completed games — if commence_time is >4h ago, game is likely over
        ct = event.get("commence_time", "")
        if ct:
            try:
                start = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                hours_since = (datetime.now(timezone.utc) - start).total_seconds() / 3600
                if hours_since > 4:
                    continue  # Game almost certainly finished
            except (ValueError, TypeError):
                pass
        for bm in event.get("bookmakers", []):
            if bm.get("key") != "pinnacle":
                continue
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                outcomes = mkt.get("outcomes", [])
                valid = [o for o in outcomes if o.get("price", 0) != 0]
                if not valid:
                    continue
                raw = {o["name"]: _imp(o["price"]) for o in valid}
                total = sum(raw.values())
                if total < 0.1:
                    continue
                devigged = {k: v / total for k, v in raw.items()}
                ct = event.get("commence_time", "")
                games.append({
                    "home": home,
                    "away": away,
                    "game_id": _game_id(home, away, ct),
                    "outcomes": devigged,
                    "commence_time": ct,
                })
    return games


# ── Polymarket (api.polymarket.us SDK) ───────────────────────────────────────
# Replaces the old Gamma/CLOB/memcache path entirely. The SDK backend carries
# live in-game prices with real bid/ask spreads for MLB, UFC, and soccer.
# MLS has no per-game SDK markets → returns {} cleanly.
#
# SDK response shapes:
#   search: {"events": [{"title", "ticker", "startDate", "markets": [{"slug", "sportsMarketType"}]}]}
#   bbo:    {"marketData": {"bestBid": {"value": "0.49"}, "bestAsk": {"value": "0.51"}, ...}}

_SMT_MAP: Dict[str, str] = {
    "baseball":       "baseball_team_full_game_winner",
    "ufc":            "ufc_fight_winner",
    "fifa-world-cup": "drawable_outcome",
    "soccer":         "drawable_outcome",
    "nfl":            "football_team_full_game_winner",
    "nba":            "basketball_team_full_game_winner",
}


def _bbo_price(bbo_resp: dict, key: str, default: float) -> float:
    """Extract a price from SDK BBO response. Handles both raw float and {'value': str} shapes."""
    md = bbo_resp.get("marketData", bbo_resp)
    val = md.get(key, default)
    if isinstance(val, dict):
        return float(val.get("value", default))
    return float(val)


def fetch_poly_mid(
    home: str,
    away: str,
    pm_tag: str,
    has_draw: bool,
    commence_time: str = "",
) -> Dict[str, Optional[float]]:
    """Fetch PM mid price via api.polymarket.us SDK. Returns {outcome_label: mid_price}.

    Uses live in-game pricing from the Polymarket US sports backend.
    Ghost-price guard applied: bestBid≤0.02 or bestAsk≥0.98 → skip outcome.
    commence_time (ISO8601) used to select the correct event date when multiple
    matchups exist (doubleheaders, past/future games in search results).
    """
    if pm_tag == "mls":
        return {}  # No per-game SDK markets for MLS

    target_smt = _SMT_MAP.get(pm_tag)
    if not target_smt:
        return {}
    draw_mode = has_draw and target_smt == "drawable_outcome"

    # Extract target date for event-matching (YYYY-MM-DD from commence_time)
    target_date = ""
    if commence_time:
        try:
            target_date = commence_time[:10]  # "2026-06-24"
        except Exception:
            pass

    try:
        from polymarket_us import PolymarketUS
        client = PolymarketUS()
    except Exception as e:
        print(f"[cross_sport_drift] PM SDK unavailable: {e}", flush=True)
        return {}

    query = f"{home} {away}"
    try:
        raw = client.search.query({"query": query}) or {}
    except Exception as e:
        print(f"[cross_sport_drift] PM SDK search '{query}' failed: {e}", flush=True)
        return {}

    events = raw.get("events", []) if isinstance(raw, dict) else raw

    # Find event matching home/away teams and (if known) the game date
    # Prefer exact date match; fall back to any active non-closed matching event
    target_event = None
    fallback_event = None
    for ev in events:
        title = ev.get("title", "")
        if not (_nmatch(home, title) and _nmatch(away, title)):
            continue
        if ev.get("closed") or ev.get("archived"):
            continue

        start = ev.get("startDate", "") or ev.get("ticker", "")
        if target_date and target_date in start:
            target_event = ev
            break  # exact date match — done
        if fallback_event is None:
            fallback_event = ev

    ev = target_event or fallback_event
    if ev is None:
        return {}

    # Filter event's markets by sportsMarketType
    all_mkts = ev.get("markets", [])
    matching = [m for m in all_mkts if m.get("sportsMarketType") == target_smt]
    if not matching:
        return {}

    result: Dict[str, Optional[float]] = {}

    if not draw_mode:
        # Binary market — one slug per game (MLB, UFC)
        mkt = matching[0]
        slug = mkt.get("slug", "")
        if not slug:
            return {}

        try:
            bbo = client.markets.bbo(slug)
        except Exception as e:
            print(f"[cross_sport_drift] PM BBO '{slug}' failed: {e}", flush=True)
            return {}

        best_bid = _bbo_price(bbo, "bestBid", 0.0)
        best_ask = _bbo_price(bbo, "bestAsk", 1.0)
        if best_bid <= 0.02 or best_ask >= 0.98:
            return {}  # ghost price / dead market

        mid = (best_bid + best_ask) / 2

        # Parse "TeamA vs. TeamB" — mid = P(TeamA wins)
        title = ev.get("title", "")
        sep = " vs. " if " vs. " in title else " vs "
        first_team = title.split(sep)[0].strip() if sep in title else home

        if _nmatch(home, first_team):
            result[home] = mid
            result[away] = 1.0 - mid
        else:
            result[away] = mid
            result[home] = 1.0 - mid

    else:
        # 3-way drawable market — 3 slugs per game: -home, -draw, -away
        for mkt in matching:
            slug = mkt.get("slug", "")
            if not slug:
                continue

            try:
                bbo = client.markets.bbo(slug)
            except Exception:
                continue

            best_bid = _bbo_price(bbo, "bestBid", 0.0)
            best_ask = _bbo_price(bbo, "bestAsk", 1.0)
            if best_bid <= 0.02 or best_ask >= 0.98:
                continue  # ghost / pre-open

            mid = (best_bid + best_ask) / 2

            if slug.endswith("-home"):
                result[home] = mid
            elif slug.endswith("-draw"):
                result["Draw"] = mid
            elif slug.endswith("-away"):
                result[away] = mid

    return result


# ── Drift detection ───────────────────────────────────────────────────────────
def check_sport_drift(conn: sqlite3.Connection, cfg: Dict, games: List[Dict]) -> None:
    """Compare current Pinnacle lines vs last snapshot. Fire alert on drift > threshold."""
    sport       = cfg["name"]
    pm_tag      = cfg["pm_tag"]
    has_draw    = cfg["has_draw"]
    drift_pp    = cfg["drift_pp"]
    edge_floor  = cfg["edge_floor_pp"]
    emoji       = SPORT_EMOJI.get(sport, "🎯")
    now_ts      = datetime.now(timezone.utc).isoformat()
    now_s       = datetime.now(timezone.utc).timestamp()

    for game in games:
        gid   = game["game_id"]
        home  = game["home"]
        away  = game["away"]
        probs = game["outcomes"]  # {outcome_name: devigged_prob}

        # Compare vs last snapshot
        any_drift = False
        outcome_data = []

        for outcome, prob in probs.items():
            row = conn.execute(
                "SELECT devig FROM sport_line_snap WHERE sport=? AND game_id=? AND outcome=?",
                (sport, gid, outcome),
            ).fetchone()

            prev = float(row["devig"]) if row else prob
            move = (prob - prev) * 100

            if abs(move) >= drift_pp:
                any_drift = True

            outcome_data.append({
                "name": outcome,
                "prev": prev,
                "now": prob,
                "move": move,
            })

            # Update snapshot
            conn.execute("""
                INSERT OR REPLACE INTO sport_line_snap (sport, game_id, outcome, devig, ts)
                VALUES (?, ?, ?, ?, ?)
            """, (sport, gid, outcome, prob, now_ts))

        conn.commit()

        if not any_drift:
            continue

        # Skip if PM is settling (any outcome ≥98%)
        pm_settled = False
        for _, prob in probs.items():
            if prob >= 0.98:
                pm_settled = True
                break
        if pm_settled:
            continue

        # Fetch PM mids in parallel with output already computed
        f_pm = _EXECUTOR.submit(fetch_poly_mid, home, away, pm_tag, has_draw, game.get("commence_time", ""))
        try:
            poly_mids = f_pm.result(timeout=15)
        except Exception:
            poly_mids = {}

        # Build alert
        fired_ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
        trade_signals: List[str] = []
        grid_rows: List[list] = []
        comparison_lines: List[str] = []

        for d in outcome_data:
            sym = "↑" if d["move"] > 0 else "↓"
            moved = abs(d["move"]) >= drift_pp
            move_str = f"{sym}{abs(d['move']):.0f}pts" if moved else f"{d['move']:+.0f}pts"

            # Plain label
            if d["name"] == "Draw":
                label = "It ends in a draw"
            elif _nmatch(d["name"], home):
                label = f"{home} wins"
            elif _nmatch(d["name"], away):
                label = f"{away} wins"
            else:
                label = f"{d['name']} wins"

            grid_rows.append([label, f"{d['prev']:.0%}", f"{d['now']:.0%}", move_str])

            # PM comparison
            pm_p = poly_mids.get(d["name"])
            if pm_p is None:
                # Try fuzzy match
                for k, v in poly_mids.items():
                    if _nmatch(k, d["name"]) or _nmatch(d["name"], k):
                        pm_p = v
                        break

            if pm_p is not None:
                gap = (d["now"] - pm_p) * 100
                if abs(gap) >= edge_floor:
                    # Vegas-tier EV filter (2026-08-23): only fire in +EV regimes.
                    if not _in_positive_ev_tier(d["now"]):
                        comparison_lines.append(f"<b>{label}</b>: Polymarket {pm_p:.0%}  (gap {abs(gap):.0f}pts but mid-tier {d['now']:.0%} — negative EV, suppressed)")
                    else:
                        direction = "cheaper" if gap > 0 else "pricier"
                        action = "BUY" if gap > 0 else "SELL"
                        comparison_lines.append(f"<b>{label}</b>: Polymarket {pm_p:.0%}  ← <b>{abs(gap):.0f}pts {direction} than Vegas</b>")
                        trade_signals.append(
                            f"→ <b>{action} {label} YES</b> at {pm_p:.0%}  (Vegas: {d['now']:.0%})"
                        )
                else:
                    comparison_lines.append(f"<b>{label}</b>: Polymarket {pm_p:.0%}  (in line)")

        lines = [
            f"{emoji} <b>ODDS MOVED</b> — {sport} | {home} vs {away}  <i>{fired_ts}</i>",
            "",
            "Vegas shifted big on this game:",
            "",
            format_grid(["Outcome", "Prev", "Now", "Move"], grid_rows),
            "",
        ]
        lines.extend(comparison_lines)

        if trade_signals:
            lines.append("━━━━━━━━━━━━━━━━")
            lines.append("💰 <b>PM hasn't caught up yet:</b>")
            lines.extend(trade_signals)

        # Only alert when there's an actionable PM edge. The rest is not
        # dropped — it goes to the tier-3 digest so a Vegas move is still
        # reviewable twice a day without being an interrupt.
        if not trade_signals:
            try:
                dispatch("ufc_drift",
                         f"{sport}: {home} vs {away} — Vegas moved, no PM edge",
                         TIER_DIGEST)
            except Exception as ex:  # noqa: BLE001 — digest must never block
                print(f"[cross_sport_drift] digest dispatch failed: {ex}", flush=True)
            print(f"[cross_sport_drift] Drift {sport} {home} vs {away} — no PM edge, digested", flush=True)
            continue

        # Cooldown: skip if we already alerted this game within DRIFT_ALERT_COOLDOWN_SECS
        dedup_row = conn.execute(
            "SELECT last_alert_ts FROM sport_drift_dedup WHERE sport=? AND game_id=? AND outcome='_game'",
            (sport, gid),
        ).fetchone()
        if dedup_row:
            try:
                last_alert = datetime.fromisoformat(dedup_row["last_alert_ts"])
                if (now_s - last_alert.timestamp()) < DRIFT_ALERT_COOLDOWN_SECS:
                    print(f"[cross_sport_drift] Drift {sport} {home} vs {away} — cooldown active, suppressed", flush=True)
                    continue
            except Exception:
                pass
        conn.execute("""
            INSERT OR REPLACE INTO sport_drift_dedup (sport, game_id, outcome, last_alert_ts)
            VALUES (?, ?, '_game', ?)
        """, (sport, gid, now_ts))
        conn.commit()

        send_telegram("\n".join(lines))
        print(f"[cross_sport_drift] Alert: {sport} {home} vs {away}", flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────
def run() -> None:
    if not ODDS_API_KEY:
        print("[cross_sport_drift] ODDS_API_KEY not set — skip", flush=True)
        return

    conn = get_db()
    migrate(conn)

    now_month = datetime.now(timezone.utc).month

    # Filter to sports active this month
    active_cfgs = [c for c in SPORT_CONFIGS if now_month in c["active_months"]]
    if not active_cfgs:
        conn.close()
        return

    print(f"[cross_sport_drift] Scanning {len(active_cfgs)} sport(s)", flush=True)

    # Fetch all Pinnacle lines in parallel
    futures = {
        cfg["name"]: _EXECUTOR.submit(fetch_pinnacle_sport, cfg["odds_key"])
        for cfg in active_cfgs
    }

    for cfg in active_cfgs:
        sport = cfg["name"]
        try:
            games = futures[sport].result(timeout=15)
        except Exception as e:
            print(f"[cross_sport_drift] {sport} fetch failed: {e}", flush=True)
            games = []

        if not games:
            print(f"[cross_sport_drift] {sport}: no games found", flush=True)
            continue

        print(f"[cross_sport_drift] {sport}: {len(games)} game(s)", flush=True)
        check_sport_drift(conn, cfg, games)

    conn.close()


if __name__ == "__main__":
    run()
