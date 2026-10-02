#!/usr/bin/env python3
"""In-game cross-venue arb watcher: Polymarket-US <-> Kalshi. ALERT-ONLY.

Sweeps live NFL games on a fixed cadence, pairs identical markets across the
two venues, and Telegram-alerts any pair whose two-leg cost nets a riskless
profit after Kalshi fees. Never places orders — execution stays manual until
the rail/risk policy says otherwise.

Design facts probed 2026-10-01 (fixtures: tests/fixtures/in_game_arb/):
- PM-US search payload carries per-market best bid/ask (outcomePrices =
  [bid, ask] of outcomes[0]) -> the PM side of a sweep is ONE search call
  per game, not dozens of BBO calls. Candidates get a fresh BBO confirm.
- Kalshi /events?series_ticker=<S>&status=open&with_nested_markets=true
  nests markets (title/yes_bid/yes_ask/volume) -> 6 calls cover the slate.
  Nested markets carry NO ticker; alerting needs none (execution later must
  resolve tickers via the per-event endpoint).
- Kalshi spread ladder ("wins by over X") has different push semantics than
  PM spread covers -> spreads are NOT paired (false-arb risk). v1 pairs:
  moneyline, pass/rec/rush yards, receptions/completions, touchdowns,
  full-game totals (over/under, .5 lines only -> no push ambiguity).
- Kalshi INT series does not exist -> PM INT props stay unpaired.
- Kalshi taker fee ~ 7% * p * (1-p) per $1 contract (cents = 7*p*(1-p)).
  PM-US taker fee currently treated as 0 (POLY_ARB_PM_FEE_CENTS to change).
- The 2026-10-01 20:40 ET catch that motivated this service: Watson 125+
  pass yds, PM YES 71c + KAL NO 25c = 96c, window lasted ~5-7 minutes.

Env:
  POLY_ARB_INTERVAL        seconds between cycles (default 30)
  POLY_ARB_NET_EDGE_CENTS  min net edge in cents to alert (default 2.0)
  POLY_ARB_KAL_MIN_VOLUME  min Kalshi market volume $ to pair (default 10000)
  POLY_ARB_COOLDOWN_MIN    minutes before the same pair+direction re-alerts (10)
  POLY_ARB_MAX_CONFIRMS    max BBO confirms per cycle (5)
  POLY_ARB_PM_FEE_CENTS    PM-US taker fee assumption (0)
  POLY_ARB_TELEGRAM        "1" send, "0" print-only (default 1)
  POLY_ARB_DB              sqlite path (default storage/in_game_arb.db)
  POLY_ARB_SERIES          comma list of Kalshi series to sweep
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from collections import namedtuple

import requests

log = logging.getLogger("in_game_arb")

KALSHI_API = "https://api.elections.kalshi.com/v1"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ESPN_HEADERS = {"User-Agent": "Polyclawd/1.0"}

DEFAULT_SERIES = "KXNFLGAME,KXNFLPASSYDS,KXNFLTD,KXNFLRECYDS,KXNFLRUSHYDS,KXNFLRECEPTIONS,KXNFLCOMPLETIONS,KXNFLTOTAL"

# stat string (from question/title text) -> canonical key
STAT_MAP = {
    "passing yards": "pass_yds",
    "receiving yards": "rec_yds",
    "rushing yards": "rush_yds",
    "touchdowns": "td",
    "receptions": "rec",
    "completions": "comp",
}
PAIRABLE = set(STAT_MAP.values()) | {"total", "ml"}

# Kalshi ML market titles are "<City> wins"; event titles are "<ABBR> <Nickname> vs <ABBR> <Nickname>"
CITY_TO_ABBR = {
    "arizona": "ARI", "atlanta": "ATL", "baltimore": "BAL", "buffalo": "BUF",
    "carolina": "CAR", "chicago": "CHI", "cincinnati": "CIN", "cleveland": "CLE",
    "dallas": "DAL", "denver": "DEN", "detroit": "DET", "green bay": "GB",
    "houston": "HOU", "indianapolis": "IND", "jacksonville": "JAX",
    "kansas city": "KC", "las vegas": "LV", "los angeles": "LA", "miami": "MIA",
    "minnesota": "MIN", "new england": "NE", "new orleans": "NO",
    "philadelphia": "PHI", "pittsburgh": "PIT", "san francisco": "SF",
    "seattle": "SEA", "tampa bay": "TB", "tennessee": "TEN", "washington": "WSH",
}
TEAM_ABBRS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET",
    "GB", "HOU", "IND", "JAX", "KC", "LV", "LA", "LAC", "LAR", "MIA", "MIN",
    "NE", "NO", "NOLA", "NYG", "NYJ", "PHI", "PIT", "SF", "SEA", "TB", "TEN",
    "WSH", "WAS",
}
# Venue abbreviation variants (Kalshi uses NOLA/LA; ESPN uses NO/LAR/LAC/WSH).
# Each ESPN game registers under every alias variant so Kalshi suffixes match.
ABBR_ALIASES = {
    "NOLA": {"NO"}, "NO": {"NOLA"},
    "WAS": {"WSH"}, "WSH": {"WAS"},
    "LA": {"LAR", "LAC"}, "LAR": {"LA"}, "LAC": {"LA"},
}
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

STAT_UNITS = {"pass_yds": "pass yds", "rec_yds": "rec yds", "rush_yds": "rush yds",
              "td": "TD", "rec": "receptions", "comp": "completions"}

Pair = namedtuple("Pair", "key game stat name line pm_bid pm_ask kal_bid kal_ask pm_slug kal_title")

RE_PM_PROP = re.compile(r"^Will (.+?) (?:record|throw) (\d+)\+ (.+?)\?$")
RE_KAL_PROP = re.compile(r"^(.+?):\s*(\d+)\+\s*(.+?)$")
RE_KAL_TOTAL = re.compile(r"^Full Game:\s*over\s*([0-9.]+)\s+points", re.I)


# ---------------------------------------------------------------- fees / math

def kalshi_fee_cents(p: float) -> float:
    """Kalshi taker fee for a $1 contract bought at price p (0..1), in cents."""
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return 7.0 * p * (1.0 - p)


def eval_pair(pair: Pair, pm_fee_cents: float = 0.0) -> dict:
    """Best two-leg combination for one paired market.

    direction 'A': buy PM YES at pm_ask + Kalshi NO at (100 - kal_bid)
    direction 'B': buy Kalshi YES at kal_ask + PM NO at (100 - pm_bid)
    Kalshi fee applies to the Kalshi leg only; PM leg uses pm_fee_cents.
    Returns {'direction', 'cost', 'net', 'legs'} — best of the two.
    """
    no_kal = 100.0 - pair.kal_bid
    no_pm = 100.0 - pair.pm_bid
    a_cost = pair.pm_ask + no_kal
    a_net = 100.0 - a_cost - kalshi_fee_cents(no_kal / 100.0) - pm_fee_cents
    b_cost = pair.kal_ask + no_pm
    b_net = 100.0 - b_cost - kalshi_fee_cents(pair.kal_ask / 100.0) - pm_fee_cents
    if a_net >= b_net:
        return {"direction": "A", "cost": a_cost, "net": a_net,
                "legs": "PM YES %.0f¢ + KAL NO %.0f¢" % (pair.pm_ask, no_kal)}
    return {"direction": "B", "cost": b_cost, "net": b_net,
            "legs": "KAL YES %.0f¢ + PM NO %.0f¢" % (pair.kal_ask, no_pm)}


# ---------------------------------------------------------------- parsers

def norm_stat(s: str) -> str:
    return STAT_MAP.get((s or "").strip().lower(), (s or "").strip().lower())


def parse_pmus_prop(question: str):
    """'Will Deshaun Watson record 125+ passing yards?' -> (name, line, stat)."""
    m = RE_PM_PROP.match((question or "").strip())
    if not m:
        return None
    name, line, stat = m.group(1).strip().lower(), float(m.group(2)), norm_stat(m.group(3))
    return (name, line, stat) if stat in PAIRABLE else None


def parse_kalshi_prop(title: str):
    """'Deshaun Watson: 125+ passing yards' -> (name, line, stat)."""
    m = RE_KAL_PROP.match((title or "").strip())
    if not m:
        return None
    name, line, stat = m.group(1).strip().lower(), float(m.group(2)), norm_stat(m.group(3))
    return (name, line, stat) if stat in PAIRABLE else None


def parse_kalshi_total(title: str):
    """'Full Game: over 17.5 points scored?' -> 17.5 (else None)."""
    m = RE_KAL_TOTAL.match((title or "").strip())
    return float(m.group(1)) if m else None


def split_team_abbrs(s: str):
    """'PITCLE' -> ('PIT','CLE'); 'NOLAPHI' -> ('NOLA','PHI'); else ()."""
    for i in range(2, 5):
        a, b = s[:i], s[i:]
        if a in TEAM_ABBRS and b in TEAM_ABBRS:
            return (a, b)
    return ()


def parse_game_suffix(suffix: str):
    """'26OCT01PITCLE' -> ('2026-10-01', ('PIT','CLE'))."""
    m = re.match(r"^(\d{2})([A-Z]{3})(\d{2})(.+)$", suffix or "")
    if not m or m.group(2) not in MONTHS:
        return None, ()
    yy, mon, dd, rest = m.groups()
    abbrs = split_team_abbrs(rest)
    if len(abbrs) != 2:
        return None, ()
    return "20%s-%02d-%s" % (yy, MONTHS[mon], dd), abbrs


def kalshi_event_suffix(ticker: str) -> str:
    return ticker.split("-", 1)[1] if ticker and "-" in ticker else ""


def espn_live_games(scoreboard: dict) -> dict:
    """{('CLE','PIT'): {'state','detail','period','score':{abbr:pts}}}"""
    out = {}
    for ev in scoreboard.get("events") or []:
        for comp in ev.get("competitions") or []:
            abbrs, scores = [], {}
            for c in comp.get("competitors") or []:
                ab = ((c.get("team") or {}).get("abbreviation") or "").upper()
                if ab:
                    abbrs.append(ab)
                    try:
                        scores[ab] = int(c.get("score") or 0)
                    except (TypeError, ValueError):
                        scores[ab] = 0
            if len(abbrs) != 2:
                continue
            st = ev.get("status") or {}
            typ = st.get("type") or {}
            info = {
                "state": typ.get("state"),
                "detail": typ.get("shortDetail"),
                "period": st.get("period"),
                "score": scores,
            }
            a, b = abbrs
            va = {a} | ABBR_ALIASES.get(a, set())
            vb = {b} | ABBR_ALIASES.get(b, set())
            for x in va:
                for y in vb:
                    out[tuple(sorted((x, y)))] = info
    return out


# ---------------------------------------------------------------- venue fetch

def kalshi_series_events(series: str, timeout: int = 20) -> dict:
    """{event_ticker: event} for one series, paginated, nested markets included."""
    out, cursor = {}, None
    for _ in range(6):
        params = {"series_ticker": series, "status": "open",
                  "with_nested_markets": "true", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(KALSHI_API + "/events", params=params, timeout=timeout)
        except requests.RequestException as e:
            log.warning("kalshi %s request: %s", series, e)
            break
        if r.status_code != 200:
            log.warning("kalshi %s http %s", series, r.status_code)
            break
        d = r.json()
        evs = d.get("events") or []
        for e in evs:
            t = e.get("ticker")
            if t:
                out[t] = e
        cursor = d.get("cursor")
        if not cursor or not evs:
            break
        time.sleep(0.3)
    return out


def espn_scoreboard(timeout: int = 15) -> dict:
    r = requests.get(ESPN_SCOREBOARD, headers=ESPN_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


_PMUS = {"client": None}


def pmus_client():
    """Lazy singleton — keeps this module importable without the SDK (tests)."""
    if _PMUS["client"] is None:
        from polymarket_us import PolymarketUS  # lazy import
        _PMUS["client"] = PolymarketUS()
    return _PMUS["client"]


def _abbr_variants(ab: str) -> list:
    return [ab] + sorted(ABBR_ALIASES.get(ab, set()))


def pmus_search_event(query: str, date: str, abbrs, timeout: int = 25):
    """The PM-US game event matching date + both team abbrevs, most markets wins.
    Abbreviation aliases (NOLA/NO, WAS/WSH, LA/LAR/LAC) are tried per side."""
    try:
        resp = pmus_client().search.query({"query": query})
    except Exception as e:
        log.warning("pmus search %r: %s", query, e)
        return None
    variants_a = _abbr_variants(abbrs[0]) if abbrs else []
    variants_b = _abbr_variants(abbrs[1]) if len(abbrs) > 1 else []

    def slug_matches(slug: str) -> bool:
        if date and date not in slug:
            return False
        if not abbrs:
            return True
        for a in variants_a:
            for b in variants_b:
                if a.lower() in slug and b.lower() in slug:
                    return True
        return False

    best = None
    for e in resp.get("events") or []:
        slug = (e.get("slug") or "").lower()
        if not slug_matches(slug):
            continue
        n = len(e.get("markets") or [])
        if best is None or n > best[1]:
            best = (e, n)
    if best is None:
        log.info("pmus: no event for %r date=%s abbrs=%s", query, date, abbrs)
    return best[0] if best else None


def pmus_bbo(slug: str):
    """(bid_cents, ask_cents) or None."""
    try:
        md = pmus_client().markets.bbo(slug).get("marketData") or {}
        bb, ba = (md.get("bestBid") or {}).get("value"), (md.get("bestAsk") or {}).get("value")
        if bb is None or ba is None:
            return None
        return float(bb) * 100.0, float(ba) * 100.0
    except Exception as e:
        log.warning("pmus bbo %s: %s", slug, e)
        return None


# ---------------------------------------------------------------- pairing

def _as_list(v):
    """PM-US SDK returns outcomes/outcomePrices as JSON-encoded strings."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return []
    return v or []


def _pm_prices(m) -> tuple:
    """(bid_cents, ask_cents) of outcomes[0] from a PM-US market, or None."""
    prices = _as_list(m.get("outcomePrices"))
    if len(prices) < 2:
        return None
    try:
        return float(prices[0]) * 100.0, float(prices[1]) * 100.0
    except (TypeError, ValueError):
        return None


def _kal_quote(m) -> tuple:
    kb, ka = m.get("yes_bid"), m.get("yes_ask")
    if kb is None or ka is None:
        return None
    return float(kb), float(ka)


def _nicknames_from_event_title(title: str):
    """'PIT Steelers vs CLE Browns' -> ['steelers','browns'] (or shorter)."""
    parts = [p.split(":")[0].strip() for p in (title or "").split(" vs ")]
    nicks = []
    for p in parts:
        toks = p.split()
        nicks.append(" ".join(toks[1:]).lower() if len(toks) > 1 else p.lower())
    return parts, nicks


def pair_props(game: str, pm_markets: list, kal_markets: list) -> list:
    kal_idx = {}
    qbs = set()  # players with a passing-yards market = QBs
    for m in kal_markets:
        p = parse_kalshi_prop(m.get("title") or "")
        if p:
            kal_idx.setdefault(p, m)
            if p[2] == "pass_yds":
                qbs.add(p[0])
    out = []
    for m in pm_markets:
        p = parse_pmus_prop(m.get("question") or "")
        if not p:
            continue
        if p[2] == "pass_yds":
            qbs.add(p[0])
        km = kal_idx.get(p)
        if not km:
            continue
        # Scope-mismatch guard: Kalshi '1+ touchdowns' excludes passing TDs;
        # PM anytime-TD scope differs per market. A QB's TD pair can be a false
        # arb, so QBs are excluded from TD pairing entirely (yards/comp pairs
        # for QBs are fine — both venues settle on the same stat).
        if p[2] == "td" and p[0] in qbs:
            continue
        pmq, kalq = _pm_prices(m), _kal_quote(km)
        if not pmq or not kalq:
            continue
        out.append(Pair(key="%s|%s|%s|%g" % (game, p[2], p[0], p[1]), game=game,
                        stat=p[2], name=p[0], line=p[1],
                        pm_bid=pmq[0], pm_ask=pmq[1], kal_bid=kalq[0], kal_ask=kalq[1],
                        pm_slug=m.get("slug") or "", kal_title=km.get("title") or ""))
    return out


def pair_totals(game: str, pm_markets: list, kal_markets: list) -> list:
    kal_idx = {}
    for m in kal_markets:
        line = parse_kalshi_total(m.get("title") or "")
        if line is not None:
            kal_idx.setdefault(line, m)
    out = []
    for m in pm_markets:
        if (m.get("sportsMarketType") or "") not in (
                "football_team_full_game_total", "football_team_points_full_game_total"):
            continue
        line = m.get("line")
        try:
            line = float(line)
        except (TypeError, ValueError):
            continue
        km = kal_idx.get(line)
        if not km:
            continue
        pmq, kalq = _pm_prices(m), _kal_quote(km)
        if not pmq or not kalq:
            continue
        out.append(Pair(key="%s|total|game|%g" % (game, line), game=game,
                        stat="total", name="game", line=line,
                        pm_bid=pmq[0], pm_ask=pmq[1], kal_bid=kalq[0], kal_ask=kalq[1],
                        pm_slug=m.get("slug") or "", kal_title=km.get("title") or ""))
    return out


def pair_ml(game: str, pm_event: dict, kal_game_event: dict) -> list:
    pm_ml = next((m for m in pm_event.get("markets") or []
                  if (m.get("slug") or "").startswith("aec-")), None)
    if not pm_ml:
        return []
    outcomes = _as_list(pm_ml.get("outcomes"))
    prices = _pm_prices(pm_ml)
    if len(outcomes) != 2 or not prices:
        return []
    parts, nicks = _nicknames_from_event_title(kal_game_event.get("title") or "")
    if len(parts) != 2:
        return []
    # PM outcomes[0] -> which Kalshi side (by nickname containment)
    i0 = None
    for i, nick in enumerate(nicks):
        o = (outcomes[0] or "").lower()
        if nick and (nick in o or o in nick):
            i0 = i
            break
    if i0 is None:
        return []
    # Kalshi ML market per side: "<City> wins" -> city map -> abbr -> part index
    kal_side = {}
    for m in kal_game_event.get("markets") or []:
        t = m.get("title") or ""
        if not t.lower().endswith(" wins"):
            continue
        city = t[: -len(" wins")].strip().lower()
        ab = CITY_TO_ABBR.get(city)
        if ab is None:
            continue
        for i, p in enumerate(parts):
            if p.split()[0].upper() == ab:
                kal_side[i] = m
    if len(kal_side) < 2:
        return []
    out = []
    for i in (0, 1):
        km = kal_side.get(i)
        if not km:
            continue
        kalq = _kal_quote(km)
        if not kalq:
            continue
        if i == i0:
            pm_bid, pm_ask = prices
        else:
            pm_bid, pm_ask = 100.0 - prices[1], 100.0 - prices[0]
        nick = nicks[i] or ("side%d" % i)
        out.append(Pair(key="%s|ml|%s" % (game, nick), game=game,
                        stat="ml", name=nick, line=0.0,
                        pm_bid=pm_bid, pm_ask=pm_ask, kal_bid=kalq[0], kal_ask=kalq[1],
                        pm_slug=pm_ml.get("slug") or "", kal_title=km.get("title") or ""))
    return out


def build_pairs(game: str, pm_event: dict, kal_events: dict) -> list:
    """All v1-pairable markets for one game. kal_events: {series: event}.
    Deduped by pair key (PM-US has two total market types that can collide)."""
    pm_markets = pm_event.get("markets") or []
    pairs = []
    kal_game = kal_events.get("KXNFLGAME")
    if kal_game:
        pairs += pair_ml(game, pm_event, kal_game)
    kal_all = []
    for series, ev in kal_events.items():
        kal_all += ev.get("markets") or []
    pairs += pair_props(game, pm_markets, kal_all)
    pairs += pair_totals(game, pm_markets, kal_events.get("KXNFLTOTAL", {}).get("markets") or [])
    seen, deduped = set(), []
    for p in pairs:
        if p.key in seen:
            continue
        seen.add(p.key)
        deduped.append(p)
    return deduped


# ---------------------------------------------------------------- persistence

def db_init(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
CREATE TABLE IF NOT EXISTS cycles (ts REAL, games INT, pairs INT, cands INT,
                                   alerts INT, err TEXT);
CREATE TABLE IF NOT EXISTS pairs_log (ts REAL, game TEXT, key TEXT, stat TEXT,
    name TEXT, line REAL, pm_bid REAL, pm_ask REAL, kal_bid REAL, kal_ask REAL,
    net_edge REAL, direction TEXT, alerted INT);
CREATE TABLE IF NOT EXISTS fired (pair_key TEXT, direction TEXT, last_ts REAL,
                                  PRIMARY KEY (pair_key, direction));
""")
    conn.commit()
    return conn


def cooldown_ok(conn: sqlite3.Connection, pair_key: str, direction: str,
                cooldown_min: float, now: float) -> bool:
    row = conn.execute("SELECT last_ts FROM fired WHERE pair_key=? AND direction=?",
                       (pair_key, direction)).fetchone()
    return row is None or (now - row[0]) >= cooldown_min * 60.0


def mark_fired(conn: sqlite3.Connection, pair_key: str, direction: str, now: float):
    conn.execute("INSERT INTO fired (pair_key, direction, last_ts) VALUES (?,?,?) "
                 "ON CONFLICT(pair_key, direction) DO UPDATE SET last_ts=excluded.last_ts",
                 (pair_key, direction, now))
    conn.commit()


# ---------------------------------------------------------------- alerting

def pair_label(pair: Pair) -> str:
    if pair.stat == "ml":
        return "%s ML" % pair.name.title()
    if pair.stat == "total":
        return "game total o%g" % pair.line
    return "%s %g+ %s" % (pair.name.title(), pair.line, STAT_UNITS.get(pair.stat, pair.stat))


def format_alert(pair: Pair, ev: dict, espn: dict, confirmed: bool) -> str:
    game_label = pair.game  # suffix e.g. 26OCT01PITCLE
    m = re.match(r"^(\d{2})([A-Z]{3})(\d{2})(.+)$", game_label)
    label = game_label
    if m:
        abbrs = split_team_abbrs(m.group(4))
        if len(abbrs) == 2:
            label = "%s@%s" % (abbrs[0], abbrs[1])
    state = ""
    if espn:
        state = " · %s" % espn.get("detail") if espn.get("detail") else ""
        sc = espn.get("score") or {}
        if sc:
            state += " " + " ".join("%s %d" % (k, v) for k, v in sorted(sc.items()))
    tag = "" if confirmed else " (unconfirmed — BBO failed)"
    return (
        "💰 ARB %s · %s\n"
        "Legs: %s = %.0f¢ → net %+.1f¢ after Kal fees%s\n"
        "Books: PM %.0f/%.0f · KAL %.0f/%.0f%s\n"
        "polymarket.com/event/%s · verify depth in both apps before sizing"
    ) % (label, pair_label(pair), ev["legs"], ev["cost"], ev["net"], tag,
         pair.pm_bid, pair.pm_ask, pair.kal_bid, pair.kal_ask, state,
         pair.pm_slug)


def send_telegram(text: str, enabled: bool) -> bool:
    if not enabled:
        print(text)
        return True
    try:
        from scripts.openclaw_alerts import _telegram_http_send
        ok, err = _telegram_http_send(text)
        if not ok:
            log.warning("telegram send failed: %s", err)
        return ok
    except Exception as e:
        log.warning("telegram import/send: %s", e)
        return False


# ---------------------------------------------------------------- cycle

def run_cycle(cfg: dict, conn: sqlite3.Connection) -> dict:
    """One sweep. cfg keys: series, interval, net_edge, kal_min_volume,
    cooldown_min, max_confirms, pm_fee, telegram."""
    now = time.time()
    summary = {"games": 0, "pairs": 0, "cands": 0, "alerts": 0}
    try:
        live = {}
        try:
            live = espn_live_games(espn_scoreboard())
        except Exception as e:
            log.warning("espn scoreboard: %s", e)

        events_by_series = {}
        for i, series in enumerate(cfg["series"]):
            if i:
                time.sleep(0.3)
            events_by_series[series] = kalshi_series_events(series)
        games = {}
        for series, evs in events_by_series.items():
            for ticker, e in evs.items():
                suffix = kalshi_event_suffix(ticker)
                if suffix:
                    games.setdefault(suffix, {})[series] = e

        for suffix, kal_events in sorted(games.items()):
            if "KXNFLGAME" not in kal_events:
                continue
            date, abbrs = parse_game_suffix(suffix)
            if not date:
                continue
            espn = live.get(tuple(sorted(abbrs)))
            if not espn or espn.get("state") != "in":
                continue  # in-game focus
            summary["games"] += 1
            parts, nicks = _nicknames_from_event_title(kal_events["KXNFLGAME"].get("title") or "")
            query = " ".join(n.title() for n in nicks if n)
            pm_event = pmus_search_event(query, date, abbrs)
            time.sleep(1.2)
            if not pm_event:
                continue
            pairs = build_pairs(suffix, pm_event, kal_events)
            summary["pairs"] += len(pairs)

            vol_by_title = {}
            for series, ev in kal_events.items():
                for m in ev.get("markets") or []:
                    if m.get("volume") is not None:
                        vol_by_title[m.get("title")] = m.get("volume")
            candidates = []
            for p in pairs:
                if (p.kal_bid + p.kal_ask) <= 1.0:  # degenerate Kalshi quote
                    continue
                vol = vol_by_title.get(p.kal_title)
                if vol is not None and cfg["kal_min_volume"] and float(vol) < cfg["kal_min_volume"]:
                    continue
                ev_eval = eval_pair(p, cfg["pm_fee"])
                # ML direction B (KAL YES + PM NO) loses on the rare NFL tie:
                # PM NO settles 50¢, KAL YES settles 0. Direction A is tie-safe.
                if p.stat == "ml" and ev_eval["direction"] == "B":
                    continue
                if ev_eval["net"] >= cfg["net_edge"]:
                    candidates.append((p, ev_eval))
            summary["cands"] += len(candidates)

            confirmed, unconfirmed = [], []
            confirmed_keys = set()
            for p, ev_eval in candidates[: cfg["max_confirms"]]:
                fresh = pmus_bbo(p.pm_slug)
                time.sleep(2.0)
                if fresh:
                    p2 = p._replace(pm_bid=fresh[0], pm_ask=fresh[1])
                    ev2 = eval_pair(p2, cfg["pm_fee"])
                    if ev2["net"] >= cfg["net_edge"]:
                        confirmed.append((p2, ev2))
                        confirmed_keys.add(p.key)
                    else:
                        log.info("evaporated on confirm: %s sweep net %.1f bbo net %.1f",
                                 p.key, ev_eval["net"], ev2["net"])
                else:
                    unconfirmed.append((p, ev_eval))

            alerted_keys = set()
            for p, ev_eval in confirmed + unconfirmed:
                if not cooldown_ok(conn, p.key, ev_eval["direction"], cfg["cooldown_min"], now):
                    continue
                text = format_alert(p, ev_eval, espn, confirmed=bool(p.key in confirmed_keys))
                ok = send_telegram(text, cfg["telegram"])
                if ok:
                    mark_fired(conn, p.key, ev_eval["direction"], now)
                    summary["alerts"] += 1
                    alerted_keys.add(p.key)
                conn.execute(
                    "INSERT INTO pairs_log (ts, game, key, stat, name, line, pm_bid, pm_ask,"
                    " kal_bid, kal_ask, net_edge, direction, alerted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now, p.game, p.key, p.stat, p.name, p.line, p.pm_bid, p.pm_ask,
                     p.kal_bid, p.kal_ask, ev_eval["net"], ev_eval["direction"], 1 if ok else 0))
            # log near-arb pairs only (calibration: how often gaps appear);
            # full raw sweeps stay in memcached-land, not sqlite
            for p in pairs:
                if p.key in alerted_keys:
                    continue
                ev_eval = eval_pair(p, cfg["pm_fee"])
                if ev_eval["net"] < 0.5:
                    continue
                conn.execute(
                    "INSERT INTO pairs_log (ts, game, key, stat, name, line, pm_bid, pm_ask,"
                    " kal_bid, kal_ask, net_edge, direction, alerted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now, p.game, p.key, p.stat, p.name, p.line, p.pm_bid, p.pm_ask,
                     p.kal_bid, p.kal_ask, ev_eval["net"], ev_eval["direction"], 0))
            conn.execute("DELETE FROM pairs_log WHERE ts < ?", (now - 7 * 86400,))
            conn.commit()
    except Exception as e:
        log.exception("cycle error: %s", e)
        summary["err"] = repr(e)[:200]
    conn.execute("INSERT INTO cycles (ts, games, pairs, cands, alerts, err) VALUES (?,?,?,?,?,?)",
                 (now, summary["games"], summary["pairs"], summary["cands"],
                  summary["alerts"], summary.get("err")))
    conn.commit()
    log.info("cycle: %(games)s games, %(pairs)s pairs, %(cands)s cands, %(alerts)s alerts", summary)
    return summary


# ---------------------------------------------------------------- main

STOP = threading.Event()


def _sigterm(_sig, _frm):
    STOP.set()


def load_cfg() -> dict:
    return {
        "interval": float(os.environ.get("POLY_ARB_INTERVAL", "45")),
        "net_edge": float(os.environ.get("POLY_ARB_NET_EDGE_CENTS", "2.0")),
        "kal_min_volume": float(os.environ.get("POLY_ARB_KAL_MIN_VOLUME", "10000")),
        "cooldown_min": float(os.environ.get("POLY_ARB_COOLDOWN_MIN", "10")),
        "max_confirms": int(os.environ.get("POLY_ARB_MAX_CONFIRMS", "5")),
        "pm_fee": float(os.environ.get("POLY_ARB_PM_FEE_CENTS", "0")),
        "telegram": os.environ.get("POLY_ARB_TELEGRAM", "1") != "0",
        "db": os.environ.get("POLY_ARB_DB", "storage/in_game_arb.db"),
        "series": [s.strip() for s in os.environ.get("POLY_ARB_SERIES", DEFAULT_SERIES).split(",") if s.strip()],
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)
    cfg = load_cfg()
    conn = db_init(cfg["db"])
    log.info("in_game_arb up: interval=%.0fs net_edge=%.1f¢ series=%s",
             cfg["interval"], cfg["net_edge"], ",".join(cfg["series"]))
    while not STOP.is_set():
        t0 = time.time()
        run_cycle(cfg, conn)
        remaining = max(2.0, cfg["interval"] - (time.time() - t0))
        STOP.wait(remaining)
    log.info("in_game_arb stopping")
    conn.close()
    return 0


if __name__ == "__main__":
    if "--once" in sys.argv:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")
        cfg = load_cfg()
        conn = db_init(cfg["db"])
        s = run_cycle(cfg, conn)
        print(json.dumps(s))
        conn.close()
        sys.exit(0)
    sys.exit(main())