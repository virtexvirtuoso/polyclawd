"""
Kalshi K + HR + HIT prop scanner.

Cross-references Kalshi orderbook prices against Odds API implied probs
and MLB Stats API L10 hit rates to find pricing discrepancies.

Line mapping (Kalshi integer → sportsbook half-point):
    Kalshi N+ Ks   ≡  Sportsbook O(N-0.5) Ks
    Kalshi 1+ HR   ≡  Sportsbook O0.5 HR
    Kalshi 2+ HR   ≡  Sportsbook O1.5 HR
    Kalshi 1+ Hits ≡  Sportsbook O0.5 H   (KXMLBHIT — batter hits 1+/2+/3+)
    Kalshi 2+ Hits ≡  Sportsbook O1.5 H

Venue notes (WS-E): poll /incentive_programs for maker-subsidy status per series
(do NOT assume the platform-wide LIP covers our series), and surface a fee-adjusted
edge (Kalshi taker fee ≈ 0.07·P·(1-P)/contract, rounded up; maker orders fee-free).

Endpoint: GET /api/baseball/kalshi/scan?min_edge=2.0&last_n=10

edge_vs_book (positive) = Kalshi YES is cheaper than sportsbooks → value on YES
edge_vs_book (negative) = Kalshi YES is more expensive → value on NO
"""

from __future__ import annotations

import asyncio
import datetime
import json
import re
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

from loguru import logger

try:
    from .mlb_props import get_mlb_props
    from .mlb_prop_scout import (
        MARKET_STAT_MAP,
        _fetch_game_log,
        _lookup_player_id,
        _pool as _scout_pool,
    )
except ImportError:
    from mlb_props import get_mlb_props
    from mlb_prop_scout import (
        MARKET_STAT_MAP,
        _fetch_game_log,
        _lookup_player_id,
        _pool as _scout_pool,
    )

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
CACHE_TTL_S = 300  # 5 min
MAX_SPREAD_C = 15  # drop markets with spread wider than this (cents)
MIN_DEPTH = 5_000  # minimum total orderbook depth ($)
OB_DELAY_S = 0.15  # delay between orderbook requests (rate limit)

_CACHE: Dict = {"ts": 0.0, "data": None, "key": ""}

# Disk cache shared across uvicorn workers (in-memory _CACHE is per-worker, so with
# multiple workers most requests would still eat the ~28s cold scan). Mirrors
# mlb_prop_scout's disk cache.
_DISK_CACHE_PATH = "/tmp/polyclawd_kalshi_scan_cache.json"


def _read_disk_cache(key: str):
    try:
        import os

        if not os.path.exists(_DISK_CACHE_PATH):
            return None
        with open(_DISK_CACHE_PATH) as f:
            p = json.load(f)
        if p.get("_cache_key") != key or (time.time() - p.get("_disk_ts", 0)) > CACHE_TTL_S:
            return None
        return p
    except Exception:
        return None


def _write_disk_cache(payload: Dict) -> None:
    try:
        import os

        p = dict(payload)
        p["_disk_ts"] = time.time()
        tmp = _DISK_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(p, f)
        os.replace(tmp, _DISK_CACHE_PATH)
    except Exception as e:
        logger.debug(f"kalshi_props disk cache write failed: {e}")


# Ticker pattern examples:
#   KXMLBKS-26JUN061610PITATL-ATLSSTRIDER99-7
#   KXMLBHR-26JUN061507BALTOR-TORGSPRINGER4-1
#   KXMLBHIT-26JUN061610PITATL-ATLSACUNA13-1
_TICKER_RE = re.compile(r"^KXMLB(KS|HR|HIT)-(\d{2}\w{3}\d{6}\w+)-([A-Z]{3})([A-Z]?)([A-Z]+)(\d+)-(\d+)$")

# prop_type -> (odds market key, stat group, short label)
_PROP_MAP = {
    "KS": ("pitcher_strikeouts", "pitching", "K"),
    "HR": ("batter_home_runs", "hitting", "HR"),
    "HIT": ("batter_hits", "hitting", "H"),
}


# ── Kalshi helpers ─────────────────────────────────────────────────────────────


def _kalshi_get(path: str) -> dict:
    url = KALSHI_BASE + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _taker_fee_c(prob: float, fee_type: str = "quadratic") -> float:
    """Kalshi taker fee per contract in CENTS (≈ pp of edge consumed on a $1
    contract). Documented quadratic: 0.07·P·(1-P) dollars/contract → 7·P·(1-P)¢,
    max ~1.75¢ at P=0.5. Maker fills are fee-free. Unknown override fee_types fall
    back to the quadratic estimate (the label is surfaced so it can be verified)."""
    p = max(0.0, min(1.0, prob))
    return round(7.0 * p * (1.0 - p), 2)


def get_incentive_programs() -> List[Dict]:
    """Poll the public /incentive_programs endpoint. Maker rewards (LIP) are NOT
    assumed to cover our series — we check. Returns [] on error."""
    try:
        d = _kalshi_get("/incentive_programs")
        return d.get("incentive_programs", d.get("programs", [])) or []
    except Exception as e:
        logger.debug(f"kalshi_props: incentive_programs poll failed: {e}")
        return []


def _lip_covers_mlb_props(programs: List[Dict]) -> bool:
    """True if any active incentive program references our MLB prop series."""
    blob = json.dumps(programs).upper()
    return any(s in blob for s in ("KXMLBHIT", "KXMLBKS", "KXMLBHR"))


def kalshi_fair_lookup(scan_results: List[Dict], player: str, market_key: str, book_line: float) -> Optional[float]:
    """Find the Kalshi mid (fair prob, 0-1) for a scout prop so WS-A can benchmark
    CLV against an INDEPENDENT close. Maps the book half-point line back to the
    Kalshi integer ladder (book O(N-0.5) ≡ Kalshi N+). Returns None if Kalshi
    doesn't carry this market (e.g. total_bases / RBIs are not on Kalshi)."""
    want_prop = {"pitcher_strikeouts": "KS", "batter_home_runs": "HR", "batter_hits": "HIT"}.get(market_key)
    if not want_prop:
        return None
    want_line = int(round(book_line + 0.5))
    ln = (player or "").split()[-1].upper()
    for r in scan_results:
        if r.get("prop_type") == want_prop and r.get("kalshi_line") == want_line:
            rp = (r.get("player") or "").split()[-1].upper()
            if rp == ln or rp.startswith(ln) or ln.startswith(rp):
                return r.get("kalshi_mid", 0) / 100.0
    return None


def _parse_ticker(ticker: str) -> Optional[Dict]:
    """Parse a Kalshi K/HR ticker into structured fields."""
    m = _TICKER_RE.match(ticker)
    if not m:
        return None
    prop_type, game_code, team, initial, lastname, jersey, line = m.groups()
    return {
        "prop": prop_type,  # "KS" | "HR"
        "game_code": game_code,  # e.g. "26JUN061610PITATL"
        "team": team,  # e.g. "ATL"
        "last_name": lastname.upper(),
        # Full letter blob (team+initial+lastname). The fixed-width regex
        # mis-splits 2-letter team codes (KC: "KCMWACHA52" -> last_name "ACHA"),
        # so matching uses suffix-of-blob, not the parsed last_name alone.
        "name_blob": f"{team}{initial}{lastname}".upper(),
        "jersey": int(jersey),
        "line": int(line),  # e.g. 7 for "7+ Ks"
        "ticker": ticker,
    }


def _get_market_list(series: str) -> List[Dict]:
    """Fetch all open markets for a Kalshi series."""
    try:
        d = _kalshi_get(f"/markets?limit=200&series_ticker={series}&status=open")
        return d.get("markets", [])
    except Exception as e:
        logger.warning(f"kalshi_props: market list error for {series}: {e}")
        return []


def _get_orderbook(ticker: str) -> Optional[Dict]:
    """
    Fetch orderbook for one ticker.
    Returns {bid, ask, mid, spread_c, depth} or None if illiquid.
    """
    # The shared 20-worker pool can burst past Kalshi's public rate limit —
    # a 429 here is a transient fetch error, NOT illiquidity. Retry with
    # backoff before giving up, so liquid markets aren't silently dropped.
    d = None
    for attempt in (1, 2, 3):
        try:
            time.sleep(OB_DELAY_S * attempt)
            d = _kalshi_get(f"/markets/{ticker}/orderbook")
            break
        except Exception as e:
            if attempt == 3:
                logger.debug(f"kalshi_props: orderbook fetch failed {ticker}: {e}")
                return None
    try:
        ob = d.get("orderbook_fp", {})
        yes = [(float(l[0]), float(l[1])) for l in ob.get("yes_dollars", [])]
        no = [(float(l[0]), float(l[1])) for l in ob.get("no_dollars", [])]
        total = sum(dd for _, dd in yes) + sum(dd for _, dd in no)
        if total < MIN_DEPTH:
            return None
        # Ignore 1-3c bot quotes at extremes; find real market
        mid_yes = sorted([(p, dd) for p, dd in yes if p >= 0.04], reverse=True)
        mid_no = sorted([(p, dd) for p, dd in no if p >= 0.04], reverse=True)
        bid = mid_yes[0][0] if mid_yes else 0.0
        ask = (1 - mid_no[0][0]) if mid_no else 1.0
        spread_c = (ask - bid) * 100
        if spread_c > MAX_SPREAD_C or ask <= 0:
            return None
        return {
            "bid": round(bid, 4),
            "ask": round(ask, 4),
            "mid": round((bid + ask) / 2, 4),
            "spread_c": round(spread_c, 1),
            "depth": round(total),
        }
    except Exception as e:
        logger.debug(f"kalshi_props: orderbook error {ticker}: {e}")
        return None


# ── Odds API helpers ───────────────────────────────────────────────────────────


def _extract_odds_rows(props_payload: Dict) -> List[Dict]:
    """
    Flatten Odds API props payload into a list of K and HR rows.
    Returns: [{player, last_name, market_key, line, over_ip, book}]
    """
    rows = []
    for game in props_payload.get("games", []):
        for mkey, mrows in game.get("props", {}).items():
            if mkey not in ("pitcher_strikeouts", "batter_home_runs", "batter_hits"):
                continue
            for row in mrows:
                player = row.get("player", "")
                last_name = player.split()[-1].upper() if player else ""
                line = float(row.get("line", 0) or 0)
                ip = float(row.get("over_ip", 0) or 0)
                if ip <= 0:
                    continue
                rows.append(
                    {
                        "player": player,
                        "last_name": last_name,
                        "market_key": mkey,
                        "line": line,
                        "over_ip": ip,  # percentage 0–100
                        "book": row.get("book", ""),
                    }
                )
    return rows


def _match_odds(parsed: Dict, odds_rows: List[Dict]) -> Optional[Dict]:
    """
    Find Odds API matches for a parsed Kalshi market.
    Returns consensus odds info or None if no match.

    Kalshi N+ → sportsbook O(N−0.5):
      7+ Ks  → book O6.5 Ks
      1+ HR  → book O0.5 HR
    """
    mkey = _PROP_MAP.get(parsed["prop"], ("batter_hits", "hitting", "H"))[0]
    target_line = parsed["line"] - 0.5
    ln = parsed["last_name"]

    # Exact last-name match first
    cands = [
        r for r in odds_rows if r["market_key"] == mkey and abs(r["line"] - target_line) < 0.6 and r["last_name"] == ln
    ]
    # Fallback 1: odds last name is a suffix of the ticker's full name blob —
    # handles 2-letter team codes (KC) where the regex mis-splits the last name
    # ("KCMWACHA52" -> parsed "ACHA", blob "KCMWACHA" endswith "WACHA").
    # Longest suffix wins (guards "SMITH" vs "HIGHSMITH").
    if not cands:
        blob = parsed.get("name_blob", "")
        suffix_cands = [
            r
            for r in odds_rows
            if r["market_key"] == mkey
            and abs(r["line"] - target_line) < 0.6
            and len(r["last_name"]) >= 4
            and blob.endswith(r["last_name"])
        ]
        if suffix_cands:
            best_len = max(len(r["last_name"]) for r in suffix_cands)
            cands = [r for r in suffix_cands if len(r["last_name"]) == best_len]
    # Fallback 2: prefix match (handles suffixes like "Jr.")
    if not cands:
        cands = [
            r
            for r in odds_rows
            if r["market_key"] == mkey
            and abs(r["line"] - target_line) < 0.6
            and (ln.startswith(r["last_name"]) or r["last_name"].startswith(ln))
        ]
    if not cands:
        return None

    ips = [c["over_ip"] for c in cands]
    return {
        "player": cands[0]["player"],
        "odds_line": cands[0]["line"],
        "avg_ip": round(sum(ips) / len(ips), 1),
        "best_ip": round(max(ips), 1),
        "worst_ip": round(min(ips), 1),
        "books": sorted(set(c["book"] for c in cands)),
        "n_books": len(cands),
    }


# ── Main async entry point ─────────────────────────────────────────────────────


async def get_kalshi_prop_scan(
    min_edge_pct: float = 2.0,
    last_n: int = 10,
) -> Dict:
    """
    Full cross-platform scan: Kalshi K/HR orderbooks vs Odds API vs MLB Stats L10.

    Only returns rows where |edge_vs_book| >= min_edge_pct.
    Sorted: STRONG signals (book + L10 agree) first, then by edge magnitude.

    Args:
        min_edge_pct: minimum |edge vs book| to include (default 2.0%)
        last_n:       recent games for L10 hit rate (default 10)
    """
    now = time.time()
    cache_key = f"{min_edge_pct}_{last_n}"
    if _CACHE["data"] and _CACHE["key"] == cache_key and now - _CACHE["ts"] < CACHE_TTL_S:
        return _CACHE["data"]
    disk = _read_disk_cache(cache_key)
    if disk is not None:
        _CACHE["data"], _CACHE["ts"], _CACHE["key"] = disk, now, cache_key
        return disk

    loop = asyncio.get_event_loop()

    # ── Step 1: Odds API props (uses its own cache) ───────────────────────────
    props_payload = await get_mlb_props()
    odds_rows = _extract_odds_rows(props_payload)
    logger.info(f"kalshi_scan: {len(odds_rows)} Odds API K/HR prop rows")

    if not odds_rows:
        payload = {
            "source": "kalshi_prop_scan",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "error": "No Odds API K/HR props available",
            "results": [],
        }
        return payload

    # ── Step 2: Kalshi market lists (parallel) ────────────────────────────────
    k_raw, hr_raw, hit_raw = await asyncio.gather(
        loop.run_in_executor(None, _get_market_list, "KXMLBKS"),
        loop.run_in_executor(None, _get_market_list, "KXMLBHR"),
        loop.run_in_executor(None, _get_market_list, "KXMLBHIT"),
    )
    logger.info(f"kalshi_scan: {len(k_raw)} K, {len(hr_raw)} HR, {len(hit_raw)} HIT markets fetched")

    # Parse and pre-filter: only keep markets with an Odds API match. Carry the
    # raw market dict so fee metadata (fee_type, per-event overrides) is available
    # at the edge step rather than hardcoding the quadratic.
    pre_matched = []
    for mkt in k_raw + hr_raw + hit_raw:
        p = _parse_ticker(mkt.get("ticker", ""))
        if p and _match_odds(p, odds_rows):
            p["_raw"] = mkt
            pre_matched.append(p)

    logger.info(f"kalshi_scan: {len(pre_matched)} tickers pre-matched to Odds API props")

    # ── Step 3: Orderbook fetches (only pre-matched, rate-limited via delay) ──
    ob_futures = [loop.run_in_executor(_scout_pool, _get_orderbook, p["ticker"]) for p in pre_matched]
    ob_results = await asyncio.gather(*ob_futures)

    liquid = [{**p, **ob} for p, ob in zip(pre_matched, ob_results) if ob is not None]
    logger.info(f"kalshi_scan: {len(liquid)} liquid matched markets")

    # ── Step 4: Pre-warm player IDs + game logs ───────────────────────────────
    player_names = list({_match_odds(m, odds_rows)["player"] for m in liquid})

    def _warm_ids(names: List[str]) -> None:
        for n in names:
            _lookup_player_id(n)

    await loop.run_in_executor(_scout_pool, _warm_ids, player_names)

    # Game log warm-up
    pid_groups = set()
    for mkt in liquid:
        odds = _match_odds(mkt, odds_rows)
        if not odds:
            continue
        pid = _lookup_player_id(odds["player"])
        if pid:
            pid_groups.add((pid, _PROP_MAP[mkt["prop"]][1]))

    def _warm_log(pg: Tuple[int, str]) -> None:
        pid, grp = pg
        _fetch_game_log(pid, grp, last_n)

    log_futs = [loop.run_in_executor(_scout_pool, _warm_log, pg) for pg in pid_groups]
    await asyncio.gather(*log_futs)

    # ── Step 5: Compute edges ─────────────────────────────────────────────────
    results = []
    for mkt in liquid:
        odds = _match_odds(mkt, odds_rows)
        if not odds:
            continue

        kalshi_mid_pct = mkt["mid"] * 100
        edge_vs_book = odds["avg_ip"] - kalshi_mid_pct  # positive = Kalshi cheap

        # L10 hit rate
        market_key = _PROP_MAP[mkt["prop"]][0]
        stat_group, stat_field, stat_label = MARKET_STAT_MAP[market_key]
        pid = _lookup_player_id(odds["player"])
        l10_pct: Optional[float] = None
        games_n = 0

        if pid:
            splits = _fetch_game_log(pid, stat_group, last_n)
            if splits:
                vals = [s.get("stat", {}).get(stat_field, 0) or 0 for s in splits]
                games_n = len(vals)
                # For "N+" props: count games where stat ≥ N
                hits = sum(1 for v in vals if v >= mkt["line"])
                l10_pct = round((hits / games_n) * 100, 1) if games_n else None

        l10_edge = round(l10_pct - kalshi_mid_pct, 1) if l10_pct is not None else None

        # Signal classification
        if l10_edge is not None:
            if edge_vs_book >= min_edge_pct and l10_edge > 0:
                signal = "STRONG_YES"  # book + L10 both support buying YES
            elif edge_vs_book <= -min_edge_pct and l10_edge < 0:
                signal = "STRONG_NO"  # book + L10 both support buying NO
            elif edge_vs_book >= min_edge_pct:
                signal = "BUY_YES"  # book only
            elif edge_vs_book <= -min_edge_pct:
                signal = "BUY_NO"  # book only
            else:
                signal = "NEUTRAL"
        else:
            if edge_vs_book >= min_edge_pct:
                signal = "BUY_YES"
            elif edge_vs_book <= -min_edge_pct:
                signal = "BUY_NO"
            else:
                signal = "NEUTRAL"

        # Fee-aware: Kalshi taker fee per contract from the market's own fee_type
        # when present (poll, don't hardcode); else the documented quadratic.
        fee_type = (mkt.get("_raw") or {}).get("fee_type") or "quadratic"
        taker_fee_c = _taker_fee_c(mkt["mid"], fee_type)
        # Fee-adjusted executable edge for a TAKER buy of the cheap side. Maker
        # fills are fee-free, so the maker edge == edge_vs_book_pct.
        fee_adj_edge = round(abs(edge_vs_book) - taker_fee_c, 1)

        label = _PROP_MAP.get(mkt["prop"], ("", "", "?"))[2]
        row = {
            "player": odds["player"],
            "prop": f"{mkt['line']}+ {label}",
            "prop_type": mkt["prop"],
            "kalshi_line": mkt["line"],
            "odds_line": odds["odds_line"],
            "kalshi_mid": round(kalshi_mid_pct, 1),
            "kalshi_bid": round(mkt["bid"] * 100, 1),
            "kalshi_ask": round(mkt["ask"] * 100, 1),
            "kalshi_spread_c": mkt["spread_c"],
            "kalshi_depth": mkt["depth"],
            "avg_book_ip": odds["avg_ip"],
            "best_book_ip": odds["best_ip"],
            "n_books": odds["n_books"],
            "edge_vs_book_pct": round(edge_vs_book, 1),
            "fee_type": fee_type,
            "taker_fee_c": taker_fee_c,
            "fee_adj_edge_pct": fee_adj_edge,  # taker; maker edge == edge_vs_book_pct
            "l10_hit_rate": l10_pct,
            "l10_edge_pct": l10_edge,
            "l10_games": games_n,
            "signal": signal,
            "ticker": mkt["ticker"],
        }

        if signal != "NEUTRAL":
            results.append(row)

    # Sort: STRONG first, then by |edge| descending
    results.sort(key=lambda r: (0 if "STRONG" in r["signal"] else 1, -abs(r["edge_vs_book_pct"])))

    # Maker-subsidy status — polled, not assumed (LIP may not cover our series).
    programs = await loop.run_in_executor(None, get_incentive_programs)
    maker_subsidized = _lip_covers_mlb_props(programs)

    payload = {
        "_cache_key": cache_key,
        "source": "kalshi_prop_scan",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "last_n": last_n,
        "min_edge_pct": min_edge_pct,
        "liquid_matched_markets": len(liquid),
        "total_results": len(results),
        "maker_subsidized": maker_subsidized,  # LIP covers KXMLB props today?
        "incentive_programs_total": len(programs),
        "results": results,
    }
    _CACHE["data"] = payload
    _CACHE["ts"] = now
    _CACHE["key"] = cache_key
    _write_disk_cache(payload)
    return payload
