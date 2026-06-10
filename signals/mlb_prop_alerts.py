"""
MLB prop alert pipeline (WS-A of the MLB-Prop-Edge-Optimization plan).

Turns the on-demand prop scout (odds/mlb_prop_scout.get_prop_scout) into a
schedule-driven, alert-driven, auto-resolved shadow-trade engine:

  1. build_scan_windows()       — statsapi schedule -> per-game T-4h..T-1h windows
                                    (doubleheaders + off-days handled automatically).
  2. run_prop_alert_scan()      — fires only inside a window; scans the slate once,
                                    logs a CONTROL SAMPLE of every scanned prop with
                                    raw L-7/10/15/20 hit rates (calibration integrity),
                                    then alerts the edge>=+15% / min_games>=7 /
                                    lineup-confirmed subset with 4h dedup + 10pp re-alert.
  3. log_prop_shadow()          — every alert auto-logs a shadow trade with a CLV
                                    column benchmarked vs an INDEPENDENT close.
  4. resolve_open_prop_shadows()— box-score auto-resolution at game final, with
                                    explicit edge-case rules (rain-shortened / partial
                                    PAs / suspended / scratched).
  5. retract_scratched_alerts() — scratch guard: exit an open alert if the player
                                    drops from the confirmed lineup.

Component boundary (Polyclawd CLAUDE.md): this is a SCANNER/LOGGER. It gathers
intel and records shadow (paper) trades. It NEVER executes real trades.

All paid Odds API cost is borne by get_prop_scout's existing cache (the scan
reuses it; statsapi is free). No new Odds API credits are spent here.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger

# Reuse the shadow DB location + the scout/lineup primitives.
try:
    from signals.shadow_tracker import DB_PATH
except Exception:  # pragma: no cover
    DB_PATH = Path(__file__).parent.parent / "storage" / "shadow_trades.db"

MLB_STATS_API = "https://statsapi.mlb.com/api/v1"
STATS_API_11 = "https://statsapi.mlb.com/api/v1.1"

# ── Tunables (mirror the plan) ───────────────────────────────────────────────
EDGE_THRESHOLD_PCT = 15.0       # alert when scout edge >= +15pp
MIN_GAMES = 7                   # alert floor (plan: min_games >= 7)
ALERT_LAST_N = 20               # fetch up to 20 games so we can compute every
#                                 candidate lookback (7/10/15/20) from one pull.
LOOKBACK_WINDOWS = (7, 10, 15, 20)
COOLDOWN_HOURS = 4              # dedup cooldown per (player, market)
EDGE_CHANGE_PP = 10.0           # re-alert if edge moves >= 10pp inside cooldown
WINDOW_OPEN_H = 4               # scan window opens T-4h
WINDOW_CLOSE_H = 1             # scan window closes T-1h (props lock ~first pitch)

# Stat field per market (matches mlb_prop_scout.MARKET_STAT_MAP).
MARKET_STAT: Dict[str, Tuple[str, str]] = {
    "batter_home_runs":   ("batting", "homeRuns"),
    "batter_hits":        ("batting", "hits"),
    "batter_total_bases": ("batting", "totalBases"),
    "batter_rbis":        ("batting", "rbi"),
    "pitcher_strikeouts": ("pitching", "strikeOuts"),
}


# ============================================================================
# Database — dedicated prop tables in the shared shadow DB
# ============================================================================

def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init_tables(conn)
    return conn


def _init_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        -- One row per fired alert == one shadow trade (OVER side of the prop).
        CREATE TABLE IF NOT EXISTS mlb_prop_shadow (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alerted_at TEXT NOT NULL,
            game_pk INTEGER,
            game_date TEXT,
            player TEXT NOT NULL,
            player_id INTEGER,
            market TEXT NOT NULL,
            stat_label TEXT,
            prop_line REAL,
            side TEXT DEFAULT 'OVER',
            book_over_pct REAL,          -- book implied prob % (our entry reference)
            hit_rate_pct REAL,           -- L-N hit rate that triggered the alert
            edge_pct REAL,               -- hit_rate - book_over (pp)
            games_sampled INTEGER,
            last_n INTEGER,
            away_team TEXT,
            home_team TEXT,
            window_kind TEXT,            -- posting / lineup_confirm / pre_lock
            alert_price REAL,            -- book_over as 0-1 (entry cost proxy)
            -- CLV vs an INDEPENDENT close (Kalshi last fair / Pinnacle close), NOT
            -- our own pre-lock snapshot. Captured near lock; clv_pp filled then.
            clv_close REAL,
            clv_close_source TEXT,
            clv_pp REAL,
            status TEXT DEFAULT 'open',  -- open/won/lost/void/retracted
            result_stat REAL,
            resolved_at TEXT,
            resolution_note TEXT,
            UNIQUE(game_pk, player, market)
        );

        -- Control sample: EVERY scanned prop, every scan, with raw lookback hit
        -- rates for all candidate windows. This is what makes the Gate-2
        -- calibration curve + lookback sweep unbiased (not censored to the alert
        -- top-of-sort). Also doubles as the per-scan line snapshot for the timing
        -- study (book_over_pct over time).
        CREATE TABLE IF NOT EXISTS mlb_prop_scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at TEXT NOT NULL,
            game_pk INTEGER,
            player TEXT NOT NULL,
            player_id INTEGER,
            market TEXT NOT NULL,
            prop_line REAL,
            book_over_pct REAL,
            avg_stat REAL,
            edge_pct REAL,               -- at ALERT_LAST_N baseline
            hr_l7 REAL, hr_l10 REAL, hr_l15 REAL, hr_l20 REAL,
            games_sampled INTEGER,
            lineup_confirmed INTEGER,
            alerted INTEGER DEFAULT 0,   -- 1 if this scan also fired an alert
            window_kind TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_scan_log_player ON mlb_prop_scan_log(player, market);
        CREATE INDEX IF NOT EXISTS idx_prop_shadow_status ON mlb_prop_shadow(status);
        """
    )
    conn.commit()


# ============================================================================
# Schedule -> scan windows  (Task 3)
# ============================================================================

def _parse_iso(dt_str: str) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        return None


def build_scan_windows(date_str: Optional[str] = None) -> List[Dict]:
    """Per-game scan windows for the slate: scan opens T-WINDOW_OPEN_H, closes
    T-WINDOW_CLOSE_H. One entry PER gamePk so doubleheaders are handled; an
    off-day yields []. statsapi is free."""
    try:
        from odds.mlb_lineups import get_scheduled_games
    except Exception as e:  # pragma: no cover
        logger.warning(f"mlb_prop_alerts: cannot import lineups — {e}")
        return []

    windows: List[Dict] = []
    for g in get_scheduled_games(date_str):
        gt = _parse_iso(g.get("gameDate", ""))
        if gt is None:
            continue
        teams = g.get("teams", {})
        windows.append(
            {
                "game_pk": g.get("gamePk"),
                "game_date": g.get("officialDate") or (g.get("gameDate", "")[:10]),
                "game_time_utc": gt,
                "away_team": teams.get("away", {}).get("team", {}).get("name", ""),
                "home_team": teams.get("home", {}).get("team", {}).get("name", ""),
                "window_start": gt - timedelta(hours=WINDOW_OPEN_H),
                "window_end": gt - timedelta(hours=WINDOW_CLOSE_H),
                "status": g.get("status", {}).get("abstractGameState", ""),
            }
        )
    return windows


def active_windows(now: Optional[datetime] = None, date_str: Optional[str] = None) -> List[Dict]:
    """Windows currently open for scanning (now in [start, end])."""
    now = now or datetime.now(timezone.utc)
    return [w for w in build_scan_windows(date_str) if w["window_start"] <= now <= w["window_end"]]


def _window_kind(now: datetime, w: Dict) -> str:
    """Which part of the window we're in (for the WS-D timing study)."""
    total = (w["window_end"] - w["window_start"]).total_seconds() or 1.0
    frac = (now - w["window_start"]).total_seconds() / total
    if frac < 0.34:
        return "posting"        # earliest third (T-4h..~T-3h) — props freshest/softest
    if frac < 0.67:
        return "lineup_confirm"  # middle — lineups usually post here
    return "pre_lock"            # final third (~T-2h..T-1h)


# ============================================================================
# Lookback hit rates from the per-game value list  (control sample / sweep)
# ============================================================================

def _lookback_hit_rates(vals: List[float], line: float) -> Dict[int, Optional[float]]:
    """Hit rate (fraction of games with stat > line) for each candidate window.
    None when fewer games than the window are available — never fabricate."""
    out: Dict[int, Optional[float]] = {}
    n = len(vals)
    for w in LOOKBACK_WINDOWS:
        if n >= w:
            window = vals[-w:]
            out[w] = round(sum(1 for v in window if (v or 0) > line) / w * 100, 1)
        else:
            out[w] = None
    return out


# ============================================================================
# Shadow logging + control sample  (Task 5)
# ============================================================================

def log_scan_row(row: Dict, *, lineup_confirmed: bool, alerted: bool,
                 window_kind: str, game_pk: Optional[int]) -> None:
    """Append a control-sample row for one scanned prop (every scanned prop is
    logged regardless of edge — this is the calibration-integrity requirement)."""
    vals = row.get("last_n_vals", []) or []
    hr = _lookback_hit_rates(vals, float(row.get("prop_line", 0.5)))
    try:
        conn = _db()
        conn.execute(
            """INSERT INTO mlb_prop_scan_log
               (scanned_at, game_pk, player, player_id, market, prop_line, book_over_pct,
                avg_stat, edge_pct, hr_l7, hr_l10, hr_l15, hr_l20, games_sampled,
                lineup_confirmed, alerted, window_kind)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(), game_pk, row.get("player"),
                row.get("_player_id"), row.get("market"), row.get("prop_line"),
                row.get("book_over_pct"), row.get("avg_stat"), row.get("edge_pct"),
                hr.get(7), hr.get(10), hr.get(15), hr.get(20), row.get("games_sampled"),
                1 if lineup_confirmed else 0, 1 if alerted else 0, window_kind,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:  # pragma: no cover
        logger.debug(f"log_scan_row failed: {e}")


def log_prop_shadow(row: Dict, *, game_pk: Optional[int], window_kind: str) -> bool:
    """Auto-log a shadow trade for a fired alert. Idempotent per
    (game_pk, player, market) via the UNIQUE constraint — re-alerts update the
    edge/price but don't duplicate the position. Returns True if a NEW shadow
    row was created."""
    alert_price = round((row.get("book_over_pct") or 0) / 100.0, 4)
    try:
        conn = _db()
        cur = conn.execute(
            """INSERT INTO mlb_prop_shadow
               (alerted_at, game_pk, game_date, player, player_id, market, stat_label,
                prop_line, side, book_over_pct, hit_rate_pct, edge_pct, games_sampled,
                last_n, away_team, home_team, window_kind, alert_price, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open')
               ON CONFLICT(game_pk, player, market) DO UPDATE SET
                   edge_pct=excluded.edge_pct,
                   hit_rate_pct=excluded.hit_rate_pct,
                   book_over_pct=excluded.book_over_pct
               """,
            (
                datetime.now(timezone.utc).isoformat(), game_pk, row.get("_game_date"),
                row.get("player"), row.get("_player_id"), row.get("market"),
                row.get("stat_label"), row.get("prop_line"), "OVER",
                row.get("book_over_pct"), row.get("hit_rate_pct"), row.get("edge_pct"),
                row.get("games_sampled"), ALERT_LAST_N, row.get("away_team"),
                row.get("home_team"), window_kind, alert_price,
            ),
        )
        created = cur.rowcount == 1
        conn.commit()
        conn.close()
        return created
    except Exception as e:  # pragma: no cover
        logger.warning(f"log_prop_shadow failed: {e}")
        return False


def capture_clv_close(game_pk: int, player: str, market: str,
                      close_prob: float, source: str) -> None:
    """Record the INDEPENDENT closing fair prob for an open shadow position and
    compute CLV (pp) vs our alert price. Called near lock from the Kalshi fair
    price (preferred) — NOT from our own book snapshot. clv_pp > 0 means we
    alerted at a better price than the close (the fast edge verdict)."""
    try:
        conn = _db()
        r = conn.execute(
            "SELECT alert_price FROM mlb_prop_shadow WHERE game_pk=? AND player=? AND market=? AND status='open'",
            (game_pk, player, market),
        ).fetchone()
        if r and r["alert_price"] is not None:
            clv_pp = round((close_prob - r["alert_price"]) * 100, 1)
            conn.execute(
                "UPDATE mlb_prop_shadow SET clv_close=?, clv_close_source=?, clv_pp=? "
                "WHERE game_pk=? AND player=? AND market=? AND status='open'",
                (round(close_prob, 4), source, clv_pp, game_pk, player, market),
            )
            conn.commit()
        conn.close()
    except Exception as e:  # pragma: no cover
        logger.debug(f"capture_clv_close failed: {e}")


# ============================================================================
# Scan + alert  (Task 4)  — fires only inside a window
# ============================================================================

def _resolve_game_pk(away: str, home: str, windows: List[Dict]) -> Optional[int]:
    for w in windows:
        if (home or "").lower() in (w["home_team"] or "").lower() and \
           (away or "").lower() in (w["away_team"] or "").lower():
            return w["game_pk"]
    # Fallback to lineups helper (handles alias mismatches).
    try:
        from odds.mlb_lineups import get_game_pk_for_teams
        return get_game_pk_for_teams(home, away)
    except Exception:
        return None


async def run_prop_alert_scan(now: Optional[datetime] = None) -> Dict:
    """One scan tick. Returns a summary dict. Safe to call every tick — it no-ops
    outside game windows and dedups alerts."""
    now = now or datetime.now(timezone.utc)
    wins = active_windows(now)
    if not wins:
        return {"status": "no_active_window", "scanned": 0, "alerted": 0}

    active_pks = {w["game_pk"] for w in wins}
    win_by_pk = {w["game_pk"]: w for w in wins}

    # Single scan of the slate (reuses the scout/Odds-API cache — no new credits).
    try:
        from odds.mlb_prop_scout import get_prop_scout, _lookup_player_id
        from odds.mlb_lineups import is_player_starting
    except Exception as e:  # pragma: no cover
        logger.warning(f"mlb_prop_alerts: scout import failed — {e}")
        return {"status": "scout_unavailable", "scanned": 0, "alerted": 0}

    payload = await get_prop_scout(last_n=ALERT_LAST_N, min_edge=-0.99, min_games=MIN_GAMES)
    results = payload.get("results", [])

    # Import dedup state lazily from the scheduler if present; else local dict.
    try:
        from services.scheduler import _state
        prev_state = _state.setdefault("mlb_props_alert_state", {})
    except Exception:
        prev_state = {}

    scanned = 0
    to_alert: List[Dict] = []
    ts = time.time()

    for row in results:
        away, home = row.get("away_team", ""), row.get("home_team", "")
        game_pk = _resolve_game_pk(away, home, wins)
        # Only consider props for games whose window is currently open.
        if game_pk not in active_pks:
            continue
        w = win_by_pk.get(game_pk, {})
        wk = _window_kind(now, w) if w else "unknown"

        player = row.get("player", "")
        pid = _lookup_player_id(player)  # warm from the scout run
        row["_player_id"] = pid
        row["_game_date"] = w.get("game_date")

        confirmed = False
        try:
            confirmed = is_player_starting(player, game_pk) if game_pk else False
        except Exception:
            confirmed = False

        edge = row.get("edge_pct", 0) or 0
        qualifies = edge >= EDGE_THRESHOLD_PCT and confirmed and (row.get("games_sampled", 0) >= MIN_GAMES)

        # Dedup / cooldown (mirror task_edge_alerts).
        will_alert = False
        if qualifies:
            key = f"{player}|{row.get('market')}"
            prev = prev_state.get(key, {})
            hours_since = (ts - prev.get("ts", 0)) / 3600
            edge_moved = abs(edge - prev.get("edge", 0)) >= EDGE_CHANGE_PP
            if hours_since >= COOLDOWN_HOURS or edge_moved or not prev:
                will_alert = True
                prev_state[key] = {"ts": ts, "edge": edge}

        # CONTROL SAMPLE: log EVERY scanned prop (calibration integrity).
        log_scan_row(row, lineup_confirmed=confirmed, alerted=will_alert,
                     window_kind=wk, game_pk=game_pk)
        scanned += 1

        if will_alert:
            log_prop_shadow(row, game_pk=game_pk, window_kind=wk)
            to_alert.append(row)

    if to_alert:
        to_alert.sort(key=lambda r: r.get("edge_pct", 0), reverse=True)
        _push_alerts(to_alert)

    # WS-E fold: benchmark open shadows' CLV against an INDEPENDENT close (Kalshi
    # fair price). Free (Kalshi API) + reuses the Odds cache. The value at the last
    # pre-lock scan becomes the effective close.
    clv_n = 0
    try:
        clv_n = await _capture_kalshi_clv(active_pks)
    except Exception as e:  # pragma: no cover
        logger.debug(f"kalshi CLV capture skipped: {e}")

    return {"status": "ok", "scanned": scanned, "alerted": len(to_alert),
            "active_games": len(active_pks), "clv_captured": clv_n}


async def _capture_kalshi_clv(active_pks: set) -> int:
    """For open shadows in active games whose market Kalshi carries (hits / HR /
    strikeouts), capture the Kalshi fair price as the independent CLV close.
    Returns count updated. total_bases / RBIs aren't on Kalshi -> left null."""
    kalshi_markets = ("batter_hits", "batter_home_runs", "pitcher_strikeouts")
    try:
        conn = _db()
        q = ("SELECT game_pk, player, market, prop_line FROM mlb_prop_shadow "
             "WHERE status='open' AND game_pk IS NOT NULL")
        opens = [dict(r) for r in conn.execute(q).fetchall()]
        conn.close()
    except Exception:
        return 0
    targets = [o for o in opens if o["game_pk"] in active_pks and o["market"] in kalshi_markets]
    if not targets:
        return 0

    from odds.kalshi_props import get_kalshi_prop_scan, kalshi_fair_lookup
    res = await get_kalshi_prop_scan(min_edge_pct=0.0)  # 0.0 => return all matched
    rows = res.get("results", [])
    n = 0
    for o in targets:
        fair = kalshi_fair_lookup(rows, o["player"], o["market"], o["prop_line"] or 0.5)
        if fair is not None:
            capture_clv_close(o["game_pk"], o["player"], o["market"], fair, "kalshi_mid")
            n += 1
    return n


# ============================================================================
# Alert push  (Task 7)  — Discord + Telegram
# ============================================================================

def _push_alerts(rows: List[Dict]) -> None:
    """Discord embed + Telegram (OpenClaw gateway). Never raises."""
    # Discord — reuse the batched-edge embed shape.
    try:
        from signals.discord_alerts import alert_mlb_prop_batch
        alert_mlb_prop_batch(rows[:8])
    except Exception as e:  # pragma: no cover
        logger.debug(f"discord prop alert skipped: {e}")

    # Telegram via OpenClaw gateway.
    try:
        from scripts.openclaw_alerts import alert_openclaw
        lines = ["⚾ *MLB Prop Edges* (lineup-confirmed)"]
        for r in rows[:8]:
            lines.append(
                f"• {r.get('player')} {r.get('stat_label')} o{r.get('prop_line')} — "
                f"hit {r.get('hit_rate_pct')}% vs book {r.get('book_over_pct')}% "
                f"(+{r.get('edge_pct')}pp)"
            )
        alert_openclaw("\n".join(lines))
    except Exception as e:  # pragma: no cover
        logger.debug(f"telegram prop alert skipped: {e}")


# ============================================================================
# Scratch guard  (Task 7)  — retract an open alert if player drops from lineup
# ============================================================================

def retract_scratched_alerts(date_str: Optional[str] = None) -> int:
    """For each open shadow whose game window has progressed enough that lineups
    are posted, verify the player is still a confirmed starter. If not, retract
    (Kalshi DNP settles at last fair price, not void — so a scratch is a real exit
    signal, and our lineup feed often sees it first). Returns count retracted."""
    try:
        from odds.mlb_lineups import is_player_starting, get_starting_lineup
    except Exception:
        return 0
    retracted = 0
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT id, game_pk, player FROM mlb_prop_shadow WHERE status='open' AND game_pk IS NOT NULL"
        ).fetchall()
        for r in rows:
            # Only act once lineups are actually posted for that game (else we'd
            # retract everything pre-lineup). get_starting_lineup == {} -> not posted.
            if not get_starting_lineup(r["game_pk"], date_str):
                continue
            if not is_player_starting(r["player"], r["game_pk"], date_str):
                conn.execute(
                    "UPDATE mlb_prop_shadow SET status='retracted', resolved_at=?, "
                    "resolution_note='scratched — dropped from confirmed lineup' WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), r["id"]),
                )
                retracted += 1
        conn.commit()
        conn.close()
    except Exception as e:  # pragma: no cover
        logger.debug(f"retract_scratched_alerts failed: {e}")
    if retracted:
        logger.info(f"mlb_prop_alerts: retracted {retracted} scratched alert(s)")
    return retracted


# ============================================================================
# Box-score auto-resolution  (Task 6)  — statsapi, free
# ============================================================================

def _mlb_get(url: str, timeout: int = 12) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:  # pragma: no cover
        logger.debug(f"statsapi get failed: {url} — {e}")
        return None


def _resolve_mlb_prop_from_statsapi(
    game_pk: int, player_id: Optional[int], market: str, prop_line: float
) -> Tuple[str, Optional[float], str]:
    """Grade ONE prop from the live feed/box score. Returns (status, result_stat, note).

    Edge-case rules (defined BEFORE logging started, per the plan):
      • Game not Final/official    -> ('open', None, 'in progress')  — try again later.
      • Suspended                  -> ('open', None, 'suspended')    — wait for completion.
      • Postponed / cancelled      -> ('void', None, 'postponed')    — player didn't play.
      • Rain-shortened but OFFICIAL -> resolve on the actual box-score stat (it counts).
      • Player not in box / 0 stat row (scratch / DNP) -> ('void', None, 'did not play').
      • Partial PAs (entered late / pulled early) -> resolves on actual stat; a prop is
        OVER iff stat > line regardless of how many PAs produced it.
    """
    if not player_id:
        return ("void", None, "no player id")

    feed = _mlb_get(f"{STATS_API_11}/game/{game_pk}/feed/live")
    if not feed:
        return ("open", None, "feed unavailable")

    gd = feed.get("gameData", {})
    detailed = gd.get("status", {}).get("detailedState", "")
    abstract = gd.get("status", {}).get("abstractGameState", "")

    if detailed in ("Postponed", "Cancelled", "Canceled"):
        return ("void", None, f"game {detailed.lower()}")
    if "Suspended" in detailed:
        return ("open", None, "suspended — awaiting completion")
    if abstract != "Final":
        return ("open", None, f"not final ({detailed or abstract or 'unknown'})")

    # Game is Final (rain-shortened games that are official also report Final — the
    # stats in the box score are what counts, so no special handling is needed).
    box = feed.get("liveData", {}).get("boxscore", {}).get("teams", {})
    stat_group, stat_field = MARKET_STAT.get(market, ("batting", "hits"))
    pkey = f"ID{player_id}"
    for side in ("home", "away"):
        players = box.get(side, {}).get("players", {})
        if pkey in players:
            stats = players[pkey].get("stats", {}).get(stat_group, {})
            if not stats:  # in roster but no stats for this group -> did not play that way
                # A position player with no batting stats, or pitcher who didn't pitch.
                return ("void", None, "did not play (no stat line)")
            val = stats.get(stat_field, 0) or 0
            won = val > prop_line
            return ("won" if won else "lost", float(val), "final")
    # Player not in either box score -> scratched / DNP.
    return ("void", None, "did not play (not in box score)")


def resolve_open_prop_shadows() -> Dict:
    """Resolve every open (non-retracted) shadow whose game has finalized.
    statsapi only — $0. Returns counts."""
    counts = {"won": 0, "lost": 0, "void": 0, "still_open": 0}
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT id, game_pk, player_id, market, prop_line FROM mlb_prop_shadow "
            "WHERE status='open' AND game_pk IS NOT NULL"
        ).fetchall()
        for r in rows:
            status, result_stat, note = _resolve_mlb_prop_from_statsapi(
                r["game_pk"], r["player_id"], r["market"], r["prop_line"] or 0.5
            )
            if status == "open":
                counts["still_open"] += 1
                continue
            conn.execute(
                "UPDATE mlb_prop_shadow SET status=?, result_stat=?, resolved_at=?, "
                "resolution_note=? WHERE id=?",
                (status, result_stat, datetime.now(timezone.utc).isoformat(), note, r["id"]),
            )
            counts[status] = counts.get(status, 0) + 1
        conn.commit()
        conn.close()
    except Exception as e:  # pragma: no cover
        logger.warning(f"resolve_open_prop_shadows failed: {e}")
    if counts["won"] or counts["lost"] or counts["void"]:
        logger.info(f"mlb_prop_alerts: resolved {counts}")
    return counts


def get_prop_alert_feed(limit: int = 100) -> Dict:
    """Alert history + shadow-trade calibration for the dashboard (WS-F / WS-D feed).
    Degrades to zeros/empty before any data accumulates."""
    out = {"alerts": [], "summary": {}, "calibration": [], "scan_count_24h": 0}
    try:
        conn = _db()
        out["alerts"] = [dict(r) for r in conn.execute(
            "SELECT alerted_at, player, stat_label, prop_line, market, edge_pct, hit_rate_pct, "
            "book_over_pct, clv_pp, clv_close_source, window_kind, status, result_stat, "
            "away_team, home_team, game_date FROM mlb_prop_shadow ORDER BY id DESC LIMIT ?",
            (limit,))]
        counts = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM mlb_prop_shadow GROUP BY status").fetchall()}
        won, lost = counts.get("won", 0), counts.get("lost", 0)
        resolved = won + lost
        clv_vals = [r["clv_pp"] for r in conn.execute(
            "SELECT clv_pp FROM mlb_prop_shadow WHERE clv_pp IS NOT NULL").fetchall()]
        out["summary"] = {
            "open": counts.get("open", 0), "won": won, "lost": lost,
            "void": counts.get("void", 0), "retracted": counts.get("retracted", 0),
            "resolved": resolved,
            "hit_rate_pct": round(won / resolved * 100, 1) if resolved else None,
            "clv_n": len(clv_vals),
            "clv_positive_pct": round(sum(1 for v in clv_vals if v > 0) / len(clv_vals) * 100, 1) if clv_vals else None,
            "avg_clv_pp": round(sum(clv_vals) / len(clv_vals), 2) if clv_vals else None,
        }
        # Calibration curve: edge bucket (10pp) -> realized hit rate (resolved only).
        out["calibration"] = [
            {"edge_bucket": r["bucket"], "n": r["n"],
             "realized_hit_pct": round(r["won"] / r["n"] * 100, 1) if r["n"] else None}
            for r in conn.execute(
                "SELECT CAST(edge_pct/10 AS INT)*10 AS bucket, "
                "SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) won, "
                "COUNT(*) n FROM mlb_prop_shadow WHERE status IN ('won','lost') "
                "GROUP BY bucket ORDER BY bucket").fetchall()]
        out["scan_count_24h"] = conn.execute(
            "SELECT COUNT(*) FROM mlb_prop_scan_log WHERE scanned_at >= ?",
            ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),)).fetchone()[0]
        conn.close()
    except Exception as e:  # pragma: no cover
        out["error"] = str(e)
    return out


if __name__ == "__main__":
    import asyncio
    print("Active windows:", len(active_windows()))
    print(asyncio.run(run_prop_alert_scan()))
    print("Resolve:", resolve_open_prop_shadows())
