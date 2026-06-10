"""
MLB prop scout — enriches today's Odds API props with MLB Stats API game logs.

For each player in today's batter_home_runs / batter_hits / batter_total_bases /
pitcher_strikeouts props, we fetch their last N games from the free MLB Stats API
and compute:
  - hit_rate_L{N}  : fraction of last N games where player cleared the prop line
  - avg_stat_L{N}  : rolling average of the stat
  - edge           : hit_rate - book_implied_prob  (positive = player is cheap)

Results are sorted by edge descending so the best values surface first.

Endpoint: GET /api/baseball/props/scout?last_n=10&min_edge=-0.99
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from loguru import logger

try:
    from .mlb_props import get_mlb_props
except ImportError:
    from mlb_props import get_mlb_props

MLB_STATS_API = "https://statsapi.mlb.com/api/v1"
SCOUT_CACHE_TTL_S = 600  # 10 min — same as props cache

# Market key → (stat_group, stat_field, description)
# stat_field is the key in the MLB Stats API game log split.
MARKET_STAT_MAP: Dict[str, Tuple[str, str, str]] = {
    "batter_home_runs":    ("hitting", "homeRuns",      "HR"),
    "batter_hits":         ("hitting", "hits",          "H"),
    "batter_total_bases":  ("hitting", "totalBases",    "TB"),
    "pitcher_strikeouts":  ("pitching", "strikeOuts",   "K"),
    "batter_rbis":         ("hitting", "rbi",           "RBI"),
}

_PLAYER_ID_CACHE: Dict[str, Optional[int]] = {}   # name → mlb id (or None)
_GAME_LOG_CACHE: Dict[str, Dict] = {}              # "{pid}_{group}" → {ts, splits}
_SCOUT_CACHE: Dict[str, object] = {"ts": 0.0, "data": None}

_pool = ThreadPoolExecutor(max_workers=20, thread_name_prefix="prop_scout")

# Disk cache path — shared across workers so only one worker pays the Odds API cost
_DISK_CACHE_PATH = "/tmp/polyclawd_scout_cache.json"


def _read_disk_cache(cache_key: str) -> Optional[Dict]:
    """Return disk-cached payload if fresh and matching cache_key, else None."""
    try:
        import os
        if not os.path.exists(_DISK_CACHE_PATH):
            return None
        with open(_DISK_CACHE_PATH, "r") as f:
            payload = json.load(f)
        if payload.get("_cache_key") != cache_key:
            return None
        age = time.time() - payload.get("_disk_ts", 0)
        if age > SCOUT_CACHE_TTL_S:
            return None
        return payload
    except Exception:
        return None


def _write_disk_cache(payload: Dict) -> None:
    """Write payload to disk cache (atomic via temp file)."""
    try:
        import os
        payload["_disk_ts"] = time.time()
        tmp = _DISK_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, _DISK_CACHE_PATH)
    except Exception as e:
        logger.debug(f"prop_scout: disk cache write failed: {e}")


# ── MLB Stats API helpers ──────────────────────────────────────────────────────

def _mlb_get(path: str) -> dict:
    url = f"{MLB_STATS_API}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _lookup_player_id(name: str) -> Optional[int]:
    if name in _PLAYER_ID_CACHE:
        return _PLAYER_ID_CACHE[name]
    try:
        data = _mlb_get(f"/people/search?names={urllib.parse.quote(name)}&sportId=1")
        people = data.get("people", [])
        pid = people[0].get("id") if people else None
    except Exception as e:
        logger.debug(f"prop_scout: player lookup failed for '{name}': {e}")
        pid = None
    _PLAYER_ID_CACHE[name] = pid
    return pid


def _fetch_game_log(pid: int, stat_group: str, last_n: int) -> List[Dict]:
    cache_key = f"{pid}_{stat_group}"
    cached = _GAME_LOG_CACHE.get(cache_key)
    if cached and time.time() - cached["ts"] < SCOUT_CACHE_TTL_S:
        return cached["splits"][-last_n:]
    try:
        data = _mlb_get(
            f"/people/{pid}/stats"
            f"?stats=gameLog&group={stat_group}&season=2026&gameType=R"
        )
        splits = data.get("stats", [{}])[0].get("splits", [])
        _GAME_LOG_CACHE[cache_key] = {"ts": time.time(), "splits": splits}
        return splits[-last_n:]
    except Exception as e:
        logger.debug(f"prop_scout: game log fetch failed for pid={pid}: {e}")
        return []


# ── Edge calculation ───────────────────────────────────────────────────────────

def _implied_prob(american: str) -> float:
    """'+330' or '-110' → 0.0–1.0."""
    try:
        o = int(str(american).replace("+", ""))
    except (TypeError, ValueError):
        return 0.0
    return (100.0 / (o + 100.0)) if o > 0 else (abs(o) / (abs(o) + 100.0))


def _scout_player(
    player: str,
    market_key: str,
    book_over_ip: float,  # already 0–1
    prop_line: float,
    last_n: int,
) -> Optional[Dict]:
    """Return scout row for one player/market combo, or None on failure."""
    stat_group, stat_field, stat_label = MARKET_STAT_MAP.get(
        market_key, ("hitting", "hits", "H")
    )
    pid = _lookup_player_id(player)
    if not pid:
        return None

    splits = _fetch_game_log(pid, stat_group, last_n)
    if not splits:
        return None

    vals = [s.get("stat", {}).get(stat_field, 0) or 0 for s in splits]
    games = len(vals)
    if games == 0:
        return None

    # "hit" = player exceeded the prop line (e.g. ≥1 HR for a 0.5-line HR prop)
    hit_count = sum(1 for v in vals if v > prop_line)
    hit_rate = hit_count / games
    avg_stat = sum(vals) / games
    edge = hit_rate - book_over_ip

    return {
        "player": player,
        "market": market_key,
        "stat_label": stat_label,
        "prop_line": prop_line,
        "book_over_pct": round(book_over_ip * 100, 1),
        "hit_rate_pct": round(hit_rate * 100, 1),
        "avg_stat": round(avg_stat, 2),
        "edge_pct": round(edge * 100, 1),
        "games_sampled": games,
        "last_n_vals": vals,
    }


# ── Dedup: best book price per (player, market) ───────────────────────────────

def _best_book_rows(props: Dict[str, List[Dict]]) -> Dict[Tuple[str, str], Dict]:
    """Return {(player, market_key): row_with_highest_over_ip} across all games."""
    best: Dict[Tuple[str, str], Dict] = {}
    for market_key, rows in props.items():
        if market_key not in MARKET_STAT_MAP:
            continue
        for row in rows:
            player = row.get("player", "")
            ip_val = row.get("over_ip", 0.0) or 0.0
            key = (player, market_key)
            if key not in best or ip_val > best[key].get("over_ip", 0.0):
                best[key] = {**row, "_market_key": market_key}
    return best


# ── Main async entry point ─────────────────────────────────────────────────────

async def get_prop_scout(last_n: int = 10, min_edge: float = -0.99, min_games: int = 5) -> Dict:
    """
    Return prop scout analysis for today's slate.

    Args:
        last_n:    number of recent games to use for hit rate (default 10)
        min_edge:  filter rows below this edge threshold (default show all)
        min_games: minimum games sampled required to include a player (default 5)
                   guards against noisy edges from rookies/call-ups with tiny samples
    """
    now = time.time()
    cache_key = f"{last_n}_{min_edge}_{min_games}"

    # Check in-memory cache first (fastest)
    cached = _SCOUT_CACHE.get("data")
    if (
        cached is not None
        and isinstance(cached, dict)
        and cached.get("_cache_key") == cache_key
        and (now - float(_SCOUT_CACHE.get("ts", 0))) < SCOUT_CACHE_TTL_S
    ):
        return cached

    # Check disk cache (shared across workers — avoids double Odds API cost on restart)
    disk_payload = _read_disk_cache(cache_key)
    if disk_payload is not None:
        logger.info("prop_scout: serving from disk cache (worker warm-up avoided)")
        _SCOUT_CACHE["data"] = disk_payload
        _SCOUT_CACHE["ts"] = disk_payload.get("_disk_ts", now)
        return disk_payload

    # Step 1: get today's props (uses its own cache — no extra Odds API credits)
    props_payload = await get_mlb_props()
    games = props_payload.get("games", [])

    # Step 2: collect all unique (player, market, best_book_ip, line) combos
    all_combos: List[Tuple[str, str, float, float, str, str]] = []
    # (player, market_key, over_ip, prop_line, away_team, home_team)
    for game in games:
        away = game.get("away_team", "")
        home = game.get("home_team", "")
        best = _best_book_rows(game.get("props", {}))
        for (player, market_key), row in best.items():
            try:
                line = float(row.get("line", 0.5))
            except (TypeError, ValueError):
                line = 0.5
            over_ip = (row.get("over_ip") or 0.0) / 100.0  # convert % → 0–1
            if over_ip <= 0:
                continue
            all_combos.append((player, market_key, over_ip, line, away, home))

    # Step 3: Phase A — parallel player ID lookups for all unique names
    loop = asyncio.get_event_loop()
    unique_names = list({c[0] for c in all_combos})

    def _bulk_lookup(names):
        for name in names:
            _lookup_player_id(name)

    # Split into batches of 10 and submit each batch as a separate task so the
    # pool actually runs them concurrently rather than in one blocking lambda.
    batch_size = 10
    lookup_futs = [
        loop.run_in_executor(_pool, _bulk_lookup, unique_names[i:i + batch_size])
        for i in range(0, len(unique_names), batch_size)
    ]
    await asyncio.gather(*lookup_futs)

    # Phase B — parallel game log fetches for unique (pid, stat_group) pairs
    unique_pid_groups = {
        (_PLAYER_ID_CACHE.get(c[0]), MARKET_STAT_MAP.get(c[1], ("hitting",))[0])
        for c in all_combos
        if _PLAYER_ID_CACHE.get(c[0])
    }

    def _fetch_one_log(pid_group):
        pid, grp = pid_group
        _fetch_game_log(pid, grp, last_n)

    log_futs = [
        loop.run_in_executor(_pool, _fetch_one_log, pg)
        for pg in unique_pid_groups
    ]
    await asyncio.gather(*log_futs)

    # Phase C — pure-CPU stat computation (no more I/O, fast)
    def _scout_one(args):
        player, market_key, over_ip, line, away, home = args
        row = _scout_player(player, market_key, over_ip, line, last_n)
        if row:
            row["away_team"] = away
            row["home_team"] = home
        return row

    raw_results = [_scout_one(c) for c in all_combos]

    # Step 4: filter, sort by edge desc
    results = [
        r for r in raw_results
        if r is not None
        and r["edge_pct"] >= min_edge * 100
        and r["games_sampled"] >= min_games
    ]
    results.sort(key=lambda r: r["edge_pct"], reverse=True)

    payload = {
        "_cache_key": cache_key,
        "source": "mlb_prop_scout",
        "timestamp": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "last_n": last_n,
        "total_players_analyzed": len([r for r in raw_results if r is not None]),
        "results": results,
    }
    _SCOUT_CACHE["data"] = payload
    _SCOUT_CACHE["ts"] = now
    _write_disk_cache(payload)
    return payload
