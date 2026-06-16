#!/usr/bin/env python3
"""
ufc_edge_scanner.py — Cross-platform UFC edge scanner.

Sources:
  - Polymarket Global (Gamma API) — fight moneylines + props, $2M+ volume
  - Kalshi (trade API) — individual fight markets + method/round props
  - Odds API (Pinnacle + all books) — sharp + soft sportsbook reference

Features:
  - Line movement tracking (15min/1h price deltas)
  - PM-Kalshi direct arbitrage comparison
  - Multi-book sportsbook comparison (DK, FD, MGM, Caesars)
  - Whale flow integration (re-scan on large trades)
  - Fighter code table with auto-discovery fallback

Usage:
  python3 odds/ufc_edge_scanner.py              # full scan
  python3 odds/ufc_edge_scanner.py --dry         # no logging
  python3 odds/ufc_edge_scanner.py --min-edge 3 # lower threshold
"""

import os, sys, json, requests, sqlite3
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

# ── Config ──────────────────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_API = "https://api.the-odds-api.com/v4"
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "storage", "shadow_trades.db")

def _load_env():
    env_path = os.path.join(PROJECT_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()  # Use = not setdefault

_load_env()
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
MIN_EDGE_PP = float(os.environ.get("UFC_MIN_EDGE_PP", "4.0"))
DRY_RUN = "--dry" in sys.argv
if "--min-edge" in sys.argv:
    idx = sys.argv.index("--min-edge")
    MIN_EDGE_PP = float(sys.argv[idx + 1])

# Tonight's main card fights (Odds API event IDs)
TONIGHT_FIGHTS = {
    "54eb45b661945af7aa7da285a12e835c": "Ruffy vs Chandler",
    "2f1aabeeefbe5b02db96846ac879a6ae": "Lewis vs Hokit",
    "20d0d02349fe04d63631b1e3f1879fa3": "O'Malley vs Zahabi",
    "1e972b73d422aa4cadb9185ca87c3c93": "Pereira vs Gane",
    "e34a17d5cf2a82a5161a54631e06770c": "Topuria vs Gaethje",
}

FIGHTER_ALIASES = {
    "Mauricio Ruffy": ["ruffy", "mauricio ruffy"],
    "Michael Chandler": ["chandler", "michael chandler"],
    "Derrick Lewis": ["lewis", "derrick lewis"],
    "Josh Hokit": ["hokit", "josh hokit"],
    "Sean O'Malley": ["omalley", "sean o'malley", "o'malley"],
    "Aiemann Zahabi": ["zahabi", "aiemann zahabi"],
    "Alex Pereira": ["pereira", "alex pereira"],
    "Ciryl Gane": ["gane", "ciryl gane"],
    "Ilia Topuria": ["topuria", "ilia topuria"],
    "Justin Gaethje": ["gaethje", "justin gaethje"],
    "Bo Nickal": ["nickal", "bo nickal"],
    "Kyle Daukaus": ["daukaus", "kyle daukaus"],
    "Diego Lopes": ["lopes", "diego lopes"],
    "Steve Garcia": ["garcia", "steve garcia"],
}

# Kalshi fighter code lookup (3-letter codes for ticker generation)
FIGHTER_CODES = {
    "Alex Pereira": "PER", "Ciryl Gane": "GAN",
    "Mauricio Ruffy": "RUF", "Michael Chandler": "CHA",
    "Ilia Topuria": "TOP", "Justin Gaethje": "GAE",
    "Sean O'Malley": "OMA", "Aiemann Zahabi": "ZAH",
    "Derrick Lewis": "LEW", "Josh Hokit": "HOK",
    "Bo Nickal": "NIC", "Kyle Daukaus": "DAU",
    "Steve Garcia": "GAR", "Diego Lopes": "LOP",
}

# Soft books to compare against Pinnacle
SOFT_BOOKS = ["draftkings", "fanduel", "betmgm", "caesars", "betrivers"]


# ── Data Models ─────────────────────────────────────────────────────

@dataclass
class PlatformPrice:
    platform: str
    fighter: str
    price: float
    volume: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    raw_odds: Optional[float] = None

@dataclass
class LineMovement:
    delta_15m: Optional[float] = None
    delta_1h: Optional[float] = None
    direction: str = ""

@dataclass
class UFCEdge:
    fight: str
    fighter: str
    edge_type: str = "pm_vs_pinnacle"  # pm_vs_pinnacle | pm_vs_kalshi | soft_vs_pinnacle
    polymarket: Optional[PlatformPrice] = None
    kalshi: Optional[PlatformPrice] = None
    pinnacle: Optional[PlatformPrice] = None
    soft_book: Optional[PlatformPrice] = None
    edge_pp: Optional[float] = None
    direction: str = ""
    movement: Optional[LineMovement] = None
    tradeable: bool = False


# ── Database ────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    _init_tables(conn)
    return conn

def _init_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ufc_price_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_time TEXT NOT NULL,
            event_id TEXT NOT NULL,
            fighter TEXT NOT NULL,
            platform TEXT NOT NULL,
            price REAL NOT NULL,
            volume REAL,
            bid REAL,
            ask REAL,
            UNIQUE(scan_time, event_id, fighter, platform)
        );
        CREATE INDEX IF NOT EXISTS idx_ufc_snapshots_lookup
            ON ufc_price_snapshots(event_id, fighter, platform, scan_time);
    """)

def save_price_snapshot(conn: sqlite3.Connection, event_id: str, fighter: str,
                        platform: str, price: float, volume=None, bid=None, ask=None):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO ufc_price_snapshots (scan_time, event_id, fighter, platform, price, volume, bid, ask) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (now, event_id, fighter, platform, price, volume, bid, ask)
    )
    conn.commit()

def get_line_movement(conn: sqlite3.Connection, event_id: str, fighter: str, platform: str) -> LineMovement:
    """Get 15min and 1h price deltas for a fighter on a platform."""
    now = datetime.now(timezone.utc)
    cutoff_15m = (now - timedelta(minutes=15)).isoformat()
    cutoff_1h = (now - timedelta(hours=1)).isoformat()

    rows = conn.execute(
        "SELECT price, scan_time FROM ufc_price_snapshots WHERE event_id=? AND fighter=? AND platform=? AND scan_time >= ? ORDER BY scan_time ASC",
        (event_id, fighter, platform, cutoff_1h)
    ).fetchall()

    if len(rows) < 2:
        return LineMovement()

    latest = rows[-1][0]
    delta_15m = None
    delta_1h = None

    # Find closest to 15min ago
    for price, st in rows:
        st_dt = datetime.fromisoformat(st)
        if (now - st_dt).total_seconds() >= 900:  # 15 min
            delta_15m = latest - price
            break

    # Find closest to 1h ago
    for price, st in rows:
        st_dt = datetime.fromisoformat(st)
        if (now - st_dt).total_seconds() >= 3600:  # 1h
            delta_1h = latest - price
            break

    direction = ""
    if delta_15m is not None:
        direction = "↑" if delta_15m > 0 else "↓"

    return LineMovement(
        delta_15m=round(delta_15m * 100, 1) if delta_15m is not None else None,
        delta_1h=round(delta_1h * 100, 1) if delta_1h is not None else None,
        direction=direction,
    )


# ── Polymarket Global (Gamma API) ──────────────────────────────────

def get_polymarket_fights() -> dict:
    """Fetch active UFC fight markets from Polymarket Global (Gamma API)."""
    r = requests.get(f"{GAMMA_API}/events", params={
        "tag_slug": "ufc", "closed": "false", "limit": 50
    }, timeout=15)
    if r.status_code != 200:
        print(f"  ⚠ Polymarket Gamma error: {r.status_code}")
        return {}

    events = r.json()
    fights = {}

    for e in events:
        title = e.get("title", "")
        skip_keywords = ["champion", "next", "pound-for-pound", "attend", "trump",
                         "weather", "knockout o/u", "submission o/u", "who will",
                         "tie color", "dance", "hug", "shake", "announcer"]
        if any(kw in title.lower() for kw in skip_keywords):
            continue
        if " vs " not in title and "freedom 250" not in title.lower():
            continue

        for m in e.get("markets", []):
            q = m.get("question", "")
            prices = m.get("outcomePrices", "[]")
            vol = m.get("volumeNum", 0)
            closed = m.get("closed", False)
            if closed:
                continue
            try:
                p = [float(x) for x in json.loads(prices)]
            except:
                continue
            if len(p) < 2:
                continue

            if q == title:
                fighters_in_title = [f for f in FIGHTER_ALIASES if f.lower() in title.lower()]
                if len(fighters_in_title) >= 2:
                    fighters_in_title.sort(key=lambda f: title.lower().index(f.lower()))
                    f1, f2 = fighters_in_title[0], fighters_in_title[1]
                    fights[f1] = PlatformPrice("polymarket", f1, p[0], vol)
                    fights[f2] = PlatformPrice("polymarket", f2, p[1], vol)

    return fights


# ── Kalshi (Trade API) ─────────────────────────────────────────────

def _generate_kalshi_ticker(fighter1: str, fighter2: str, commence_time: str) -> Optional[str]:
    """Generate Kalshi event ticker from fighter names + date."""
    f1_code = FIGHTER_CODES.get(fighter1)
    f2_code = FIGHTER_CODES.get(fighter2)
    if not f1_code or not f2_code:
        return None

    try:
        dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        # Kalshi uses US Eastern local date for tickers
        from datetime import timezone, timedelta
        eastern = timezone(timedelta(hours=-4))
        dt_local = dt.astimezone(eastern)
        date_str = dt_local.strftime("%y%^b%d").upper()
        # Also try UTC date as fallback (some events use UTC)
        date_str_utc = dt.strftime("%y%^b%d").upper()
    except:
        return None

    # Try Eastern date first, then UTC date
    for ds in [date_str, date_str_utc]:
        ticker = f"KXUFCFIGHT-{ds}{f1_code}{f2_code}"
        r = requests.get(f"{KALSHI_API}/events/{ticker}", timeout=5)
        if r.status_code == 200:
            return ticker

    return None

def _search_kalshi_by_name(fighter: str) -> Optional[str]:
    """Fallback: search Kalshi for a fighter by name when code table doesn't have them."""
    r = requests.get(f"{KALSHI_API}/events?limit=50", timeout=10)
    if r.status_code != 200:
        return None

    for e in r.json().get("events", []):
        ticker = e.get("ticker", "")
        if not ticker.startswith("KXUFCFIGHT-"):
            continue
        for m in e.get("markets", []):
            title = m.get("title", "").lower()
            if fighter.lower() in title:
                return ticker
    return None

def get_kalshi_fights() -> dict:
    """Fetch individual UFC fight markets from Kalshi."""
    fights = {}

    # Try generated tickers first
    # Get commence times from Odds API events list
    r_events = requests.get(f"{ODDS_API}/sports/mma_mixed_martial_arts/events?apiKey={ODDS_API_KEY}", timeout=10)
    event_times = {}
    if r_events.status_code == 200:
        for ev in r_events.json():
            eid = ev.get("id", "")
            ct = ev.get("commence_time", "")
            if eid and ct:
                event_times[eid] = ct

    for event_id, label in TONIGHT_FIGHTS.items():
        fighters = label.split(" vs ")
        if len(fighters) != 2:
            continue

        # Skip if event has already started (in-play data is poison)
        ct = event_times.get(event_id, "")
        if ct:
            try:
                dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                if dt < datetime.now(timezone.utc) - timedelta(minutes=5):
                    continue  # Skip live/past events
            except:
                pass

        # Map short names to full names for FIGHTER_CODES lookup
        short_to_full = {}
        for full_name in FIGHTER_CODES:
            for alias in FIGHTER_ALIASES.get(full_name, []):
                short_to_full[alias.lower()] = full_name

        f1_full = short_to_full.get(fighters[0].strip().lower(), fighters[0])
        f2_full = short_to_full.get(fighters[1].strip().lower(), fighters[1])

        ticker = _generate_kalshi_ticker(f1_full, f2_full, ct)
        if not ticker:
            # Fallback: search by name
            ticker = _search_kalshi_by_name(fighters[0])
            if not ticker:
                print(f"  ⚠ No Kalshi ticker for {label}")
                continue

        r = requests.get(f"{KALSHI_API}/events/{ticker}", timeout=10)
        if r.status_code != 200:
            continue

        for m in r.json().get("markets", []):
            mt = m.get("ticker", "")
            title = m.get("title", "")

            ob_r = requests.get(f"{KALSHI_API}/markets/{mt}/orderbook", timeout=10)
            if ob_r.status_code != 200:
                continue

            ob = ob_r.json().get("orderbook_fp", {})
            yes_bids = ob.get("yes_dollars", [])
            no_bids = ob.get("no_dollars", [])
            if not yes_bids and not no_bids:
                continue

            best_yes = max([float(x[0]) for x in yes_bids]) if yes_bids else 0
            best_no = max([float(x[0]) for x in no_bids]) if no_bids else 0
            implied_ask = 1 - best_no if best_no > 0 else 1
            mid = (best_yes + implied_ask) / 2 if best_yes > 0 else 0

            # Match fighter from "Will [FIGHTER] win..." format
            will_idx = title.lower().find("will ")
            if will_idx >= 0:
                after_will = title[will_idx + 5:]
                for fname, aliases in FIGHTER_ALIASES.items():
                    for a in aliases:
                        if after_will.lower().startswith(a):
                            fights[fname] = PlatformPrice("kalshi", fname, mid, bid=best_yes, ask=implied_ask)
                            break
                    else:
                        continue
                    break

    return fights


# ── Kalshi Prop Markets (Method of Finish / Rounds) ────────────────

@dataclass
class KalshiProp:
    fight: str
    prop_type: str  # "method_of_finish" | "round"
    label: str      # e.g. "KO/TKO", "Submission", "Round 2"
    price: float
    bid: float
    ask: float
    depth: int


def _generate_kalshi_prop_ticker(event_ticker: str, prop_type: str, value: str) -> str:
    """Generate Kalshi prop market ticker.
    prop_type: "MOF" for method of finish, "ROUNDS" for round
    value: "KOTKODQ" | "SUB" | "DEC" for MOF, "1"-"5" for rounds
    """
    prefix = "KXUFCMOF" if prop_type == "MOF" else "KXUFCROUNDS"
    # Extract date + fighter codes from event ticker
    # KXUFCFIGHT-26JUN14PERGAN → 26JUN14PERGAN
    suffix = event_ticker.replace("KXUFCFIGHT-", "")
    return f"{prefix}-{suffix}-{value}"


def get_kalshi_props(event_ticker: str, fight_label: str) -> list[KalshiProp]:
    """Fetch Kalshi prop markets (method of finish, rounds) for a fight."""
    props = []

    # Method of finish markets
    for mof_code, mof_label in [("KOTKODQ", "KO/TKO"), ("SUB", "Submission"), ("DEC", "Decision")]:
        ticker = _generate_kalshi_prop_ticker(event_ticker, "MOF", mof_code)
        r = requests.get(f"{KALSHI_API}/markets/{ticker}/orderbook", timeout=10)
        if r.status_code != 200:
            continue
        ob = r.json().get("orderbook_fp", {})
        yes_bids = ob.get("yes_dollars", [])
        no_bids = ob.get("no_dollars", [])
        if not yes_bids and not no_bids:
            continue
        best_yes = max([float(x[0]) for x in yes_bids]) if yes_bids else 0
        best_no = max([float(x[0]) for x in no_bids]) if no_bids else 0
        implied_ask = 1 - best_no if best_no > 0 else 1
        mid = (best_yes + implied_ask) / 2 if best_yes > 0 else 0
        props.append(KalshiProp(fight_label, "method_of_finish", mof_label, mid, best_yes, implied_ask, len(yes_bids) + len(no_bids)))

    # Round markets (up to 5 rounds)
    for rnd in range(1, 6):
        ticker = _generate_kalshi_prop_ticker(event_ticker, "ROUNDS", str(rnd))
        r = requests.get(f"{KALSHI_API}/markets/{ticker}/orderbook", timeout=10)
        if r.status_code != 200:
            continue
        ob = r.json().get("orderbook_fp", {})
        yes_bids = ob.get("yes_dollars", [])
        no_bids = ob.get("no_dollars", [])
        if not yes_bids and not no_bids:
            continue
        best_yes = max([float(x[0]) for x in yes_bids]) if yes_bids else 0
        best_no = max([float(x[0]) for x in no_bids]) if no_bids else 0
        implied_ask = 1 - best_no if best_no > 0 else 1
        mid = (best_yes + implied_ask) / 2 if best_yes > 0 else 0
        props.append(KalshiProp(fight_label, "round", f"Round {rnd}", mid, best_yes, implied_ask, len(yes_bids) + len(no_bids)))

    return props


def display_kalshi_props(all_props: dict[str, list[KalshiProp]]):
    """Display Kalshi prop markets."""
    if not all_props:
        return
    print(f"\n{'─' * 70}")
    print(f"  KALSHI PROP MARKETS")
    print(f"{'─' * 70}")
    for fight_label, props in sorted(all_props.items()):
        if not props:
            continue
        print(f"\n  {fight_label}:")
        for p in props:
            print(f"     {p.label:15s} | {p.price:.1%} | bid: {p.bid:.1%} ask: {p.ask:.1%} | depth: {p.depth}")


# ── Odds API (Pinnacle + All Books) ─────────────────────────────────

def get_pinnacle_odds(event_id: str) -> dict:
    """Fetch Pinnacle odds for a UFC event, return devigged prices."""
    url = f"{ODDS_API}/sports/mma_mixed_martial_arts/events/{event_id}/odds"
    r = requests.get(url, params={
        "apiKey": ODDS_API_KEY,
        "regions": "us,eu",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }, timeout=10)

    if r.status_code != 200:
        return {}

    data = r.json()
    for bm in data.get("bookmakers", []):
        if bm.get("key") == "pinnacle":
            for m in bm.get("markets", []):
                outcomes = m.get("outcomes", [])
                if len(outcomes) >= 2:
                    o1, o2 = outcomes[0], outcomes[1]
                    imp1, imp2 = 1 / o1["price"], 1 / o2["price"]
                    total = imp1 + imp2
                    return {
                        o1["name"]: PlatformPrice("pinnacle", o1["name"],
                            imp1 / total, raw_odds=o1["price"]),
                        o2["name"]: PlatformPrice("pinnacle", o2["name"],
                            imp2 / total, raw_odds=o2["price"]),
                    }
    return {}

def get_all_book_odds(event_id: str) -> dict:
    """Fetch odds from ALL books, return {fighter: {book: price}}."""
    url = f"{ODDS_API}/sports/mma_mixed_martial_arts/events/{event_id}/odds"
    r = requests.get(url, params={
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }, timeout=10)

    if r.status_code != 200:
        return {}

    data = r.json()
    result = {}  # {fighter_name: {book_key: price}}

    for bm in data.get("bookmakers", []):
        bk = bm.get("key", "")
        if bk == "pinnacle":
            continue
        for m in bm.get("markets", []):
            for o in m.get("outcomes", []):
                name = o["name"]
                price = o["price"]
                if name not in result:
                    result[name] = {}
                result[name][bk] = 1 / price  # Convert to implied probability

    return result


# ── Edge Computation ────────────────────────────────────────────────

def compute_edges(poly_fights: dict, kalshi_fights: dict, conn: sqlite3.Connection) -> list[UFCEdge]:
    """Compare all platforms vs Pinnacle, plus PM-Kalshi direct arb, plus multi-book."""
    edges = []

    for event_id, label in TONIGHT_FIGHTS.items():
        pinnacle = get_pinnacle_odds(event_id)
        all_books = get_all_book_odds(event_id)

        if not pinnacle:
            print(f"  ⚠ No Pinnacle data for {label}")
            continue

        for fname in pinnacle:
            pinfo = pinnacle[fname]
            poly = poly_fights.get(fname)
            kalshi = kalshi_fights.get(fname)

            # ── PM vs Pinnacle ──
            if poly and poly.price > 0:
                edge_pp = (pinfo.price - poly.price) * 100
                direction = "BUY" if edge_pp > 0 else "SELL"
                movement = get_line_movement(conn, event_id, fname, "polymarket")
                save_price_snapshot(conn, event_id, fname, "polymarket", poly.price, poly.volume)

                edges.append(UFCEdge(
                    fight=label, fighter=fname, edge_type="pm_vs_pinnacle",
                    polymarket=poly, pinnacle=pinfo,
                    edge_pp=round(edge_pp, 1), direction=direction,
                    movement=movement,
                    tradeable=abs(edge_pp) >= MIN_EDGE_PP,
                ))

            # ── Kalshi vs Pinnacle ──
            if kalshi and kalshi.price > 0:
                edge_pp = (pinfo.price - kalshi.price) * 100
                direction = "BUY" if edge_pp > 0 else "SELL"
                movement = get_line_movement(conn, event_id, fname, "kalshi")
                save_price_snapshot(conn, event_id, fname, "kalshi", kalshi.price, bid=kalshi.bid, ask=kalshi.ask)

                edges.append(UFCEdge(
                    fight=label, fighter=fname, edge_type="pm_vs_pinnacle",
                    kalshi=kalshi, pinnacle=pinfo,
                    edge_pp=round(edge_pp, 1), direction=direction,
                    movement=movement,
                    tradeable=abs(edge_pp) >= MIN_EDGE_PP,
                ))

            # ── PM vs Kalshi Direct Arb ──
            if poly and kalshi and poly.price > 0 and kalshi.price > 0:
                arb_pp = (poly.price - kalshi.price) * 100
                arb_direction = "BUY KALSHI" if arb_pp > 0 else "BUY POLYMARKET"
                edges.append(UFCEdge(
                    fight=label, fighter=fname, edge_type="pm_vs_kalshi",
                    polymarket=poly, kalshi=kalshi,
                    edge_pp=round(abs(arb_pp), 1), direction=arb_direction,
                    tradeable=abs(arb_pp) >= MIN_EDGE_PP,
                ))

            # ── Multi-Book: Soft Books vs Pinnacle ──
            if fname in all_books:
                for book, soft_price in all_books[fname].items():
                    if book not in SOFT_BOOKS:
                        continue
                    edge_pp = (pinfo.price - soft_price) * 100
                    if abs(edge_pp) >= MIN_EDGE_PP:
                        soft_info = PlatformPrice(book, fname, soft_price)
                        edges.append(UFCEdge(
                            fight=label, fighter=fname, edge_type="soft_vs_pinnacle",
                            pinnacle=pinfo, soft_book=soft_info,
                            edge_pp=round(edge_pp, 1),
                            direction="BUY" if edge_pp > 0 else "SELL",
                            tradeable=True,
                        ))

    return sorted(edges, key=lambda e: abs(e.edge_pp) if e.edge_pp else 0, reverse=True)


# ── Display ─────────────────────────────────────────────────────────

def display(edges: list[UFCEdge]):
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    print(f"\n{'═' * 70}")
    print(f"  UFC EDGE SCAN — {now}")
    print(f"  PM Global + Kalshi + Multi-Book vs Pinnacle")
    print(f"{'═' * 70}")

    if not edges:
        print("\n  No edges found. Markets are in agreement.")
        return

    for e in edges:
        icon = "⚡" if e.tradeable else "  "

        if e.edge_type == "pm_vs_pinnacle":
            parts = []
            if e.polymarket:
                parts.append(f"PM: {e.polymarket.price:.1%}")
            if e.kalshi:
                parts.append(f"KA: {e.kalshi.price:.1%}")
            if e.pinnacle:
                parts.append(f"PIN: {e.pinnacle.price:.1%}")

            print(f"\n  {icon} {e.fight:<30s} {e.fighter:<25s}")
            print(f"     {' | '.join(parts)}")
            if e.edge_pp is not None:
                arrow = "↑" if e.edge_pp > 0 else "↓"
                print(f"     Edge: {e.edge_pp:+.1f}pp {arrow}  →  {e.direction}")
            if e.movement and (e.movement.delta_15m is not None or e.movement.delta_1h is not None):
                d15 = f"{e.movement.delta_15m:+.1f}pp" if e.movement.delta_15m is not None else "?"
                d1h = f"{e.movement.delta_1h:+.1f}pp" if e.movement.delta_1h is not None else "?"
                print(f"     Movement: {d15} (15m) | {d1h} (1h) {e.movement.direction}")
            if e.pinnacle and e.pinnacle.raw_odds:
                print(f"     Pinnacle raw: {e.pinnacle.raw_odds:.2f}")
            if e.kalshi and e.kalshi.bid and e.kalshi.ask:
                print(f"     Kalshi book: {e.kalshi.bid:.1%} bid / {e.kalshi.ask:.1%} ask")
            if e.polymarket and e.polymarket.volume:
                print(f"     PM volume: ${e.polymarket.volume:,.0f}")

        elif e.edge_type == "pm_vs_kalshi":
            print(f"\n  {icon} [PM-KALSHI ARB] {e.fight:<25s} {e.fighter:<25s}")
            if e.polymarket and e.kalshi:
                print(f"     PM: {e.polymarket.price:.1%}  |  KA: {e.kalshi.price:.1%}")
            if e.edge_pp is not None:
                print(f"     Gap: {e.edge_pp:.1f}pp  →  {e.direction}")

        elif e.edge_type == "soft_vs_pinnacle":
            print(f"\n  {icon} [STALE LINE] {e.fight:<25s} {e.fighter:<25s}")
            if e.soft_book and e.pinnacle:
                print(f"     {e.soft_book.platform:15s}: {e.soft_book.price:.1%}  |  PIN: {e.pinnacle.price:.1%}")
            if e.edge_pp is not None:
                print(f"     Overpriced by {e.edge_pp:.1f}pp  →  {e.direction}")

    actionable = [e for e in edges if e.tradeable]
    print(f"\n{'─' * 70}")
    print(f"  Total: {len(edges)} edges  |  Actionable (≥{MIN_EDGE_PP}pp): {len(actionable)}")
    print(f"{'═' * 70}\n")


# ── Main ────────────────────────────────────────────────────────────

def main():
    conn = _get_db()

    print("\n  Fetching Polymarket Global fights...")
    poly_fights = get_polymarket_fights()
    print(f"  Found {len(poly_fights)} fighters on Polymarket Global")

    print("\n  Fetching Kalshi fights...")
    kalshi_fights = get_kalshi_fights()
    print(f"  Found {len(kalshi_fights)} fighters on Kalshi")

    print("\n  Fetching Kalshi prop markets...")
    all_props = {}
    for event_id, label in TONIGHT_FIGHTS.items():
        fighters = label.split(" vs ")
        if len(fighters) != 2:
            continue
        short_to_full = {}
        for full_name in FIGHTER_CODES:
            for alias in FIGHTER_ALIASES.get(full_name, []):
                short_to_full[alias.lower()] = full_name
        f1_full = short_to_full.get(fighters[0].strip().lower(), fighters[0])
        f2_full = short_to_full.get(fighters[1].strip().lower(), fighters[1])
        f1_code = FIGHTER_CODES.get(f1_full)
        f2_code = FIGHTER_CODES.get(f2_full)
        if f1_code and f2_code:
            # Try both Eastern and UTC date
            for ds in ["26JUN14", "26JUN15"]:
                ticker = f"KXUFCFIGHT-{ds}{f1_code}{f2_code}"
                r = requests.get(f"{KALSHI_API}/events/{ticker}", timeout=5)
                if r.status_code == 200:
                    props = get_kalshi_props(ticker, label)
                    if props:
                        all_props[label] = props
                    break
    print(f"  Found props for {len(all_props)} fights")

    print("\n  Fetching Pinnacle + multi-book odds...")

    print("\n  Fetching Pinnacle + multi-book odds...")
    edges = compute_edges(poly_fights, kalshi_fights, conn)

    display(edges)
    display_kalshi_props(all_props)

    conn.close()


if __name__ == "__main__":
    main()
