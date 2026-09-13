#!/usr/bin/env python3
"""
Whale alert Telegram notifier — sends actionable CRITICAL/HIGH signals.
Runs inline after every whale scanner cycle (~5 min).

Dedup logic:
  - Same market suppressed for 4h UNLESS:
    a) Score jumped >0.15 since last send (escalation)
    b) HTR < 2h (closing soon — urgency bypass)
  - CLOB×scanner fusion: if whale_clob fired on same market within 15 min,
    header becomes "DOUBLE CONFIRMATION"
"""
import requests, json, os, time, re, html
# Runnable as a script (scheduler/cron launch it as a subprocess, which does
# NOT inherit the parent sys.path). Make the project root importable so the
# module-level project imports below resolve either way.
import sys as _sys
from pathlib import Path as _Path
_ROOT = str(_Path(__file__).resolve().parent.parent)
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

from scripts.alert_formatter import send_telegram as send_tg

# HTML-escape dynamic content — raw & < > in market titles 400 at Telegram
_esc = lambda s: html.escape(str(s), quote=False)

API = "http://127.0.0.1:8420/api"
STATE_FILE = "/tmp/whale_alert_tg_state.json"
CLOB_LAST_FILE = "/tmp/whale_clob_last.json"

MIN_SCORE = 7           # Must reach at least MONITOR verdict (score ≥ 7) to alert
MIN_HTR = 0.5         # hours — skip only if resolves in < 30 min
MAX_HTR = 72
MIN_FLOW = 5000
MIN_WALLET_WR = 0.45
MIN_WALLET_N = 5
# ── Top-accounts filter (2026-06-20) ────────────────────────────────
# Only alert when a verified smart wallet ($10K+ net, 55%+ WR) is driving
# the flow. Kalshi has no wallet data, so use high flow+score threshold.
REQUIRE_SMART_WALLET = True
KALSHI_FALLBACK_MIN_FLOW = 25_000   # Kalshi alerts without wallet ID need big flow
KALSHI_FALLBACK_MIN_SCORE = 9       # ... and high score
DEDUP_WINDOW = 4 * 3600   # 4h standard
SCORE_ESCALATION_DELTA = 0.15  # re-alert if score jumps this much
HTR_URGENCY_THRESHOLD = 2.0    # bypass dedup if <2h to resolve
CLOB_FUSION_WINDOW = 15 * 60   # 15 min — CLOB×scanner fusion window
PM_ANON_FLOW_FLOOR = 75_000   # PM anon flow above this bypasses smart-wallet gate
PM_FUTURES_HTR_MAX = 720      # 30 days — WC futures have HTR 336-672h


def load_state():
    """State: {market: {ts, score}} — tracks last send time + score."""
    try:
        with open(STATE_FILE) as f:
            raw = json.load(f)
        # Migrate old format {sent: {market: ts}} → new format
        if "sent" in raw:
            migrated = {}
            for mkt, val in raw["sent"].items():
                migrated[mkt] = {"ts": val, "score": 0.0}
            return migrated
        return raw
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def load_clob_fired() -> set:
    """Return set of markets that whale_clob fired on within fusion window."""
    try:
        with open(CLOB_LAST_FILE) as f:
            data = json.load(f)
        if time.time() - data.get("ts", 0) > CLOB_FUSION_WINDOW:
            return set()
        return {e["market"] for e in data.get("fired", [])}
    except Exception:
        return set()


def get_top_alerts():
    try:
        r = requests.get(f"{API}/whale/top", params={"limit": 20, "severity": "CRITICAL"}, timeout=10)
        alerts = r.json().get("alerts", [])
    except Exception:
        return []
    try:
        r2 = requests.get(f"{API}/whale/top", params={"limit": 10, "severity": "HIGH"}, timeout=10)
        highs = [a for a in r2.json().get("alerts", []) if a.get("score", 0) >= 0.65]
        alerts += highs
    except Exception:
        pass
    return alerts


def is_actionable(alert):
    score = alert.get("score", 0)
    htr = alert.get("hours_to_resolve")
    flow = alert.get("flow_dollars", 0)
    wr = alert.get("wallet_win_rate")
    if score < MIN_SCORE:
        return False
    if htr is not None:
        if htr < MIN_HTR:
            return False
        # PM futures (WC Advance/Winner) resolve in 14-28 days — use 30-day ceiling
        htr_ceiling = (PM_FUTURES_HTR_MAX
                       if (alert.get("platform") == "polymarket" and flow >= PM_ANON_FLOW_FLOOR)
                       else MAX_HTR)
        if htr > htr_ceiling:
            return False
    if flow < MIN_FLOW:
        return False
    wallet_n = alert.get("wallet_n")
    if wr is not None and wallet_n is not None and wallet_n >= MIN_WALLET_N:
        if wr < MIN_WALLET_WR:
            return False
    # ── Close-time guard: skip resolved markets ──────────────────────
    close_iso = alert.get("close_time", "")
    if close_iso:
        try:
            from datetime import datetime, timezone
            close_dt = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > close_dt:
                print(f"SKIP resolved: {alert.get('title','')[:40]} closed {close_iso}")
                return False
        except Exception:
            pass
    # ── Price sanity: skip if YES > 90¢ or NO > 90¢ (already decided) ─
    bid = alert.get("best_bid")
    if bid is not None and (bid > 0.90 or bid < 0.10):
        print(f"SKIP decided: {alert.get('title','')[:40]} bid={bid:.2f}")
        return False
    # ── PM near-settled fallback: bid is often None for PM (no CLOB snapshot)
    # Use current_price (last executed trade) as defense-in-depth
    current_price = alert.get("current_price")
    if current_price is not None and (current_price >= 0.97 or current_price <= 0.03):
        print(f"SKIP near-settled PM: {alert.get('title','')[:40]} @ {current_price:.2f}")
        return False
    # ── No price = no edge visibility, skip for CRITICAL/HIGH unless large PM flow ─
    platform = alert.get("platform", "")
    if bid is None and current_price is None and alert.get("severity") in ("CRITICAL", "HIGH"):
        print(f"SKIP no-price: {alert.get('title','')[:40]}")
        return False
    # ── WNBA: log to shadow only, no Telegram alerts ────────────────
    mkt = alert.get("market", "")
    if mkt.upper().startswith("KXWNBA"):
        print(f"SKIP WNBA (shadow only): {alert.get('title','')[:40]}")
        return False
    # ── Top-accounts gate: only alert on verified smart wallets ──────
    if REQUIRE_SMART_WALLET:
        reasons = alert.get("reasons", "")
        has_smart = "smart_wallet" in reasons
        if platform == "polymarket" and not has_smart:
            # Large anonymous PM flow: aggregate dollar size IS the signal
            if flow < PM_ANON_FLOW_FLOOR:
                print(f"SKIP PM anon low-flow (${flow:,.0f}): {alert.get('title','')[:40]}")
                return False
        if platform != "polymarket" and not has_smart:
            # Kalshi/other: no wallet ID available, use high-bar fallback
            if flow < KALSHI_FALLBACK_MIN_FLOW or score < KALSHI_FALLBACK_MIN_SCORE:
                print(f"SKIP no-smart-wallet (Kalshi, flow=${flow:,.0f} score={score}): {alert.get('title','')[:40]}")
                return False
    return True


def should_send(alert, state, now) -> tuple[bool, str]:
    """Returns (send, reason). reason is 'new'|'escalation'|'urgency'|'suppressed'."""
    mkt = alert.get("market", "")
    score = alert.get("score", 0)
    htr = alert.get("hours_to_resolve")
    prev = state.get(mkt)

    if prev is None:
        return True, "new"

    # HTR urgency bypass — closing soon, always re-alert
    if htr is not None and htr < HTR_URGENCY_THRESHOLD:
        if now - prev["ts"] > 1800:  # but at most every 30 min even for urgent
            return True, "urgency"

    # Score escalation bypass
    prev_score = prev.get("score", 0)
    if score - prev_score >= SCORE_ESCALATION_DELTA:
        return True, "escalation"

    # Standard dedup window
    if now - prev["ts"] > DEDUP_WINDOW:
        return True, "new"

    return False, "suppressed"


def _clean_market_name(mkt: str) -> str:
    cleaned = re.sub(r'-[a-f0-9]{8,}$', '', mkt)
    cleaned = cleaned.replace('-', ' ').title()
    if len(cleaned) > 60:
        cleaned = cleaned[:57] + '...'
    return cleaned


# ── Kalshi World Cup team code → matchup name ───────────────────────
WC_TEAM_CODES = {
    "ALG": "Algeria", "ARG": "Argentina", "AUS": "Australia", "AUT": "Austria",
    "BEL": "Belgium", "BIH": "Bosnia", "BRA": "Brazil", "CAN": "Canada",
    "CPV": "Cape Verde", "CIV": "Côte d'Ivoire", "COL": "Colombia", "COD": "Congo DR",
    "CRO": "Croatia", "CUW": "Curaçao", "CZE": "Czechia", "ECU": "Ecuador",
    "EGY": "Egypt", "ENG": "England", "FRA": "France", "GER": "Germany",
    "GHA": "Ghana", "HAI": "Haiti", "IRI": "IR Iran", "IRQ": "Iraq",
    "JPN": "Japan", "JOR": "Jordan", "KOR": "Korea Republic", "KSA": "Saudi Arabia",
    "MEX": "Mexico", "MAR": "Morocco", "NED": "Netherlands", "NZL": "New Zealand",
    "NOR": "Norway", "PAN": "Panama", "PAR": "Paraguay", "POR": "Portugal",
    "QAT": "Qatar", "RSA": "South Africa", "SCO": "Scotland", "SEN": "Senegal",
    "ESP": "Spain", "SUI": "Switzerland", "SWE": "Sweden", "TUN": "Tunisia",
    "TUR": "Türkiye", "UZB": "Uzbekistan", "URU": "Uruguay", "USA": "USA",
    "DZA": "Algeria",  # alternate code
}


# ── Kalshi MLB team codes (3-char, from KXMLBGAME tickers) ──────────
MLB_TEAM_CODES = {
    "SEA": "Mariners", "BOS": "Red Sox", "STL": "Cardinals", "LAD": "Dodgers",
    "NYY": "Yankees", "LAA": "Angels", "MIL": "Brewers", "CHC": "Cubs",
    "TOR": "Blue Jays", "CLE": "Guardians", "NYM": "Mets", "TB": "Rays",
    "SD": "Padres", "CIN": "Reds", "PHI": "Phillies", "AZ": "D-backs",
    "BAL": "Orioles", "COL": "Rockies", "CWS": "White Sox", "HOU": "Astros",
    "ATH": "Athletics", "TEX": "Rangers", "MIA": "Marlins", "KC": "Royals",
    "DET": "Tigers", "MIN": "Twins", "SF": "Giants", "PIT": "Pirates",
    "ATL": "Braves", "WSH": "Nationals",
}


def _extract_matchup_from_ticker(mkt: str):
    """Extract matchup (+ first-pitch time) from Kalshi ticker codes.
    World Cup: KXWCTOTAL-26JUN17GHAPAN-3 → ("Ghana vs Panama", None)
    MLB: KXMLBGAME-26SEP011845SEABOS-BOS → ("Mariners @ Red Sox", "Sep 1, 6:45 PM ET")
    Returns (matchup, game_time) — either may be None.
    """
    # MLB game ticker: KXMLBGAME-<YY><MON><DD><HHMM ET><AWAY><HOME>-<SIDE>
    # Combined segment is 5-6 chars (2-char codes exist: SD, TB, AZ, KC, SF).
    m = re.search(r'KXMLBGAME-\d{2}([A-Z]{3})(\d{2})(\d{4})([A-Z]{5,6})-', mkt)
    if m:
        mon, day, hhmm, seg = m.groups()
        for split_at in (3, 2):
            away, home = seg[:split_at], seg[split_at:]
            if away in MLB_TEAM_CODES and home in MLB_TEAM_CODES:
                h = int(hhmm[:2])
                h12 = h % 12 or 12
                ampm = "AM" if h < 12 else "PM"
                game_time = f"{mon.title()} {int(day)}, {h12}:{hhmm[2:]} {ampm} ET"
                return f"{MLB_TEAM_CODES[away]} @ {MLB_TEAM_CODES[home]}", game_time
    # World Cup tickers (existing behaviour)
    m = re.search(r'KXWC\w+-\d{2}\w+\d{2}([A-Z]{3,8})-', mkt)
    if not m:
        return None, None
    code_str = m.group(1)
    # Try 4-char split first (e.g. GHAPAN = GHA + PAN)
    if len(code_str) == 6:
        c1, c2 = code_str[:3], code_str[3:]
        if c1 in WC_TEAM_CODES and c2 in WC_TEAM_CODES:
            return f"{WC_TEAM_CODES[c1]} vs {WC_TEAM_CODES[c2]}", None
    # Try 3-char (e.g. POR = Portugal)
    if len(code_str) == 3 and code_str in WC_TEAM_CODES:
        return WC_TEAM_CODES[code_str], None
    return None, None


# ── Fair-value join: sharp-book devig vs whale entry (2026-09-01) ────
# sport_line_snap only has LIVE-game rows (drift scanner self-gates on live
# games), but whale alerts fire PRE-game — so fetch the devig on demand,
# once per process run, through the same credit gate the monitors use.
# Pattern copied from cross_sport_drift.fetch_pinnacle_sport: per-book 2-way
# devig across sharp US books, averaged. Fail-closed: any miss → no line.
MLB_TEAM_FULL = {
    "SEA": "Seattle Mariners", "BOS": "Boston Red Sox", "STL": "St. Louis Cardinals",
    "LAD": "Los Angeles Dodgers", "NYY": "New York Yankees", "LAA": "Los Angeles Angels",
    "MIL": "Milwaukee Brewers", "CHC": "Chicago Cubs", "TOR": "Toronto Blue Jays",
    "CLE": "Cleveland Guardians", "NYM": "New York Mets", "TB": "Tampa Bay Rays",
    "SD": "San Diego Padres", "CIN": "Cincinnati Reds", "PHI": "Philadelphia Phillies",
    "AZ": "Arizona Diamondbacks", "BAL": "Baltimore Orioles", "COL": "Colorado Rockies",
    "CWS": "Chicago White Sox", "HOU": "Houston Astros", "ATH": "Athletics",
    "TEX": "Texas Rangers", "MIA": "Miami Marlins", "KC": "Kansas City Royals",
    "DET": "Detroit Tigers", "MIN": "Minnesota Twins", "SF": "San Francisco Giants",
    "PIT": "Pittsburgh Pirates", "ATL": "Atlanta Braves", "WSH": "Washington Nationals",
}
_FV_SHARP_BOOKS = ("pinnacle", "draftkings", "fanduel", "betmgm", "williamhill_us", "fanatics")
_FV_CACHE: dict = {}   # side_code -> fair_prob (per process run)


def _fv_imp(price) -> float:
    p = int(price)
    return (100 / (100 + p)) if p > 0 else (-p / (-p + 100))


def _mlb_fair_value(mkt: str):
    """Devigged sharp-book consensus win prob for the alert's side.
    Returns None on any miss (no key, gated, no line, date mismatch)."""
    m = re.search(r'KXMLBGAME-(\d{2})([A-Z]{3})(\d{2})\d{4}[A-Z]{5,6}-([A-Z]{2,3})$', mkt)
    if not m:
        return None
    yy, mon, day, side = m.groups()
    months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7,
              "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
    if side not in MLB_TEAM_FULL or mon not in months:
        return None
    # Ticker date is the ET game date; commence_time is UTC — evening ET
    # games roll to the next UTC day, so compare on the ET date.
    et_date = f"{2000 + int(yy)}-{months[mon]:02d}-{int(day):02d}"
    if side in _FV_CACHE:
        return _FV_CACHE[side]
    key = os.environ.get("ODDS_API_KEY", "")
    if not key:
        return None
    try:
        from odds.monitor_gate import gated_fetch_json
        data = gated_fetch_json("https://api.the-odds-api.com/v4/sports/baseball_mlb/odds", {
            "apiKey": key, "regions": "us", "markets": "h2h", "oddsFormat": "american",
        })
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    side_full = MLB_TEAM_FULL[side]
    book_probs = []
    for event in data:
        if side_full not in (event.get("home_team", ""), event.get("away_team", "")):
            continue
        ct = event.get("commence_time", "")
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            start = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York"))
            if start.strftime("%Y-%m-%d") != et_date:
                continue
        except Exception:
            continue
        for bm in event.get("bookmakers", []):
            if bm.get("key") not in _FV_SHARP_BOOKS:
                continue
            for mk in bm.get("markets", []):
                if mk.get("key") != "h2h":
                    continue
                valid = [o for o in mk.get("outcomes", []) if o.get("price")]
                if len(valid) < 2:
                    continue
                raw = {o["name"]: _fv_imp(o["price"]) for o in valid}
                tot = sum(raw.values())
                if not (0.95 <= tot <= 1.50) or side_full not in raw:
                    continue
                book_probs.append(raw[side_full] / tot)
    if not book_probs:
        return None
    fair = sum(book_probs) / len(book_probs)
    _FV_CACHE[side] = fair
    return fair


DASHBOARD_URL = "https://virtuosocrypto.com/polyclawd/whale-flow.html"



def _build_market_links(platform: str, mkt: str) -> list:
    """Build useful links for the alert: direct market + dashboard."""
    links = []
    if platform == "polymarket":
        links.append(f"<a href='https://polymarket.com/market/{mkt}'>Polymarket</a>")
    else:
        series = mkt.split('-')[0]
        links.append(f"<a href='https://kalshi.com/markets/{series}'>Kalshi</a>")
    links.append(f"<a href='{DASHBOARD_URL}'>Dashboard</a>")
    return links


def _infer_category(title: str, ticker: str = "") -> str:
    """Return emoji for market category based on title + ticker."""
    tl = title.lower()
    tk = ticker.upper()
    # ── Ticker-based (most reliable) ─────────────────────────────────
    if tk.startswith("KXMLB"):
        return "⚾"
    if tk.startswith("KXNCAAF"):
        return "🏈"  # college football (KXNCAAF* was mis-mapped to 🏀)
    if any(tk.startswith(p) for p in ["KXNCAAB", "KXNCAAW"]):
        return "🏀"
    if any(tk.startswith(p) for p in ["KXNBA", "KXWNBA"]):
        return "🏀"
    if tk.startswith("KXNFL"):
        return "🏈"
    if tk.startswith("KXNHL"):
        return "🏒"
    if any(tk.startswith(p) for p in ["KXUFC", "KXMMA"]):
        return "🥊"
    # ── Title-based (order matters: specific → generic) ──────────────
    # Politics
    if any(w in tl for w in ["election", "president", "senate", "house ", "governor", "democrat", "republican", "congress", "electoral"]):
        return "🏛️"
    # Baseball (before generic sports — "runs" is the key differentiator)
    if any(w in tl for w in ["runs", "pitcher", "batter", "innings", "strikeout", "home run", "baseball", " mlb", "rbi"]):
        return "⚾"
    # Basketball (title-based, no ticker)
    if any(w in tl for w in ["points scored", "rebounds", "assists", "three-pointer", "nba", "wnba"]):
        return "🏀"
    # American football
    if any(w in tl for w in ["touchdown", "passing yards", "rushing", "nfl ", "super bowl"]):
        return "🏈"
    # Tennis (specific keywords, NOT generic "win the")
    if any(w in tl for w in ["round of", "wta", "atp", "grand slam", "qualification", "set ", "roland garros", "wimbledon", "us open tennis"]):
        return "🎾"
    # "match?" with player-style names → likely tennis
    if "match?" in tl and " the " in tl and " vs " not in tl:
        return "🎾"
    # UFC / MMA
    if any(w in tl for w in ["ufc", "fight night", "knockout", "submission", "mma", "bellator"]):
        return "🥊"
    # Soccer (specific team names + "goal" — NOT generic "vs")
    if any(w in tl for w in ["goal", "fc ", "juventus", "liverpool", "bayern", "psg", "barcelona", "real madrid", "chelsea", "arsenal"]):
        return "⚽"
    # "spread" with country/team name in parens → likely soccer (FIFA)
    if "spread" in tl and "(" in tl:
        return "⚽"
    # Crypto
    if any(w in tl for w in ["bitcoin", "ethereum", "crypto", "btc", "eth ", "sol ", "xrp", "doge", "price of"]):
        return "₿"
    # Weather
    if any(w in tl for w in ["temperature", "climate", "weather", "co2", "emission"]):
        return "🌡️"
    return "📊"


def _short_title(title: str) -> str:
    """Shorten verbose prediction-market titles to the core matchup."""
    import re
    t = title.strip().rstrip('?')

    # ── Totals: "Will over X goals be scored?" → "O/U X goals" ──────
    m = re.match(r'Will (?:over|under) (\d+\.?\d*)\s*(goals?|runs?|points?)\s+be\s+scored', t, re.I)
    if m:
        return f"O/U {m.group(1)} {m.group(2)}"

    # ── 1H totals: "Over X 1H goals scored" → "1H O/U X" ────────────
    m = re.match(r'(Over|Under) (\d+\.?\d*)\s*(1H|1st Half)\s*(goals?|runs?|points?)\s+scored', t, re.I)
    if m:
        return f"1H O/U {m.group(2)} {m.group(4)}"

    # ── Spread/runline: "Will X win(s) by over N runs/points?" ───────
    m = re.match(r'Will (?:the |a |an )?(.+?) wins? by (?:over |at least )?(\d+\.?\d*)\s*(runs?|points?|goals?)', t)
    if m:
        return f"{m.group(1).strip()} +{m.group(2)} {m.group(3)}"

    # ── Goals/totals: "Will X score over N goals?" ───────────────────
    m = re.match(r'Will (?:the |a |an )?(.+?) score (?:over |at least |under )?(\d+\.?\d*)\+?\s*(goals?|runs?|points?)', t)
    if m:
        return f"{m.group(1).strip()} {m.group(2)}+ {m.group(3)}"

    # ── Exact score: "Exact Score: X N - N Y" ────────────────────────
    m = re.match(r'Exact Score:\s*(.+)', t)
    if m:
        return m.group(1).strip()

    # ── Spread label: "Spread: X (-N.N)" — already clean ────────────
    if t.startswith("Spread:"):
        return t

    # ── Player props: "Name: N+ stat?" ───────────────────────────────
    m = re.match(r'([A-Z][a-z]+(?: [A-Z][a-z]+)*)\s*:\s*(\d+\+?\s*.+)', t)
    if m:
        return f"{m.group(1)}: {m.group(2).strip()}"

    # ── "X vs Y" — prefer capitalized matchup names ──────────────────
    m = re.search(r'([A-Z][a-z]+(?: [A-Z][a-z]+)*)\s+vs\.?\s+([A-Z][a-z]+(?: [A-Z][a-z]+)*)', t)
    if m:
        return f"{m.group(1).strip()} vs {m.group(2).strip()}"

    # ── Broader vs: "X vs Y" with any casing ─────────────────────────
    m = re.search(r'([A-Za-z][A-Za-z .\'-]+?)\s+vs\.?\s+([A-Za-z][A-Za-z .\'-]+)', t)
    if m:
        left, right = m.group(1).strip(), m.group(2).strip()
        # Trim at colon (e.g. "de Minaur vs Diallo: Round Of 32 match")
        right = right.split(':')[0].strip()
        return f"{left} vs {right}"

    # ── "Will X win the Y vs Z... match?" → "Y vs Z" ────────────────
    m = re.match(r'Will .+?win the (.+?vs\.?\s+.+?)(?::\s*| match| -)', t, re.I)
    if m:
        matchup = m.group(1).strip().split(':')[0].strip()
        if len(matchup) <= 50:
            return matchup

    # ── "Will X win..." → X (but NOT for "final score" markets) ──────
    m = re.match(r'Will (?:the |a |an )?(.+?) wins?(?:\s|$)', t)
    if m and 'final score' not in m.group(1).lower():
        name = m.group(1).strip()
        if len(name) > 40:
            name = name[:37] + "..."
        return name

    # ── "Will the final score be X" → "Score: X" ────────────────────
    m = re.match(r'Will the final score be (.+)', t)
    if m:
        return f"Score: {m.group(1).strip()}"

    # ── Temperature/threshold: "Will the X be N or higher?" ──────────
    m = re.match(r'Will (?:the )?(.+?) be (\d+.+?)(?:\s+or\s+(?:higher|lower|more|less))', t)
    if m:
        return f"{m.group(1).strip()} {m.group(2).strip()}+"

    # ── Fallback: strip "Will" prefix, truncate ──────────────────────
    clean = re.sub(r'^Will (?:the |a |an )?', '', t)
    if len(clean) > 50:
        clean = clean[:47] + "..."
    return clean


def _human_reasons(reasons: str) -> str:
    """Convert raw reason codes to a short human-readable line.
    Uses plain language — no jargon."""
    parts = []
    for r in reasons.split(","):
        r = r.strip()
        if r.startswith("vol_spike_"):
            try: parts.append(f"Vol spike {int(float(r.split('_')[-1]))/1000:.0f}K")
            except: pass
        elif r.startswith("vol_move_"):
            try: parts.append(f"Vol +{int(float(r.split('_')[-1]))/1000:.0f}K")
            except: pass
        elif r.startswith("oi_spike_"):
            try: parts.append(f"Open interest +{int(float(r.split('_')[-1]))/1000:.0f}K")
            except: pass
        elif r.startswith("taker_YES_"):
            try: parts.append(f"{r.split('_')[-1].replace('%','')}% aggressive buys")
            except: pass
        elif r.startswith("taker_NO_"):
            try: parts.append(f"{r.split('_')[-1].replace('%','')}% aggressive sells")
            except: pass
        elif r.startswith("level_jump_bid_"):
            try: parts.append(f"Bid wall ${int(float(r.split('_')[-1]))/1000:.0f}K")
            except: pass
        elif r.startswith("level_jump_ask_"):
            try: parts.append(f"Ask wall ${int(float(r.split('_')[-1]))/1000:.0f}K")
            except: pass
        elif r.startswith("flow_mag_"):
            try: parts.append(f"${int(float(r.split('_')[-1]))/1000:.0f}K flow")
            except: pass
        elif r.startswith("whale_flow_pierce_"):
            try: parts.append(f"Whale ${int(float(r.split('_')[-1]))/1000:.0f}K")
            except: pass
        elif r.startswith("intensity_"):
            try: parts.append(f"{r.split('_')[-1].replace('%','')}% intensity")
            except: pass
        elif r == "aggressive_taker_100%":
            parts.append("100% aggressive")
        elif r == "bilateral_moderate":
            parts.append("Both sides")
        elif r == "imbalance_flip":
            parts.append("Book flipped")
        elif r == "spread_collapse":
            parts.append("Spread tight")
        elif r == "smart_wallet":
            parts.append("Smart wallet")
        elif r.startswith("class_outlier_"):
            pass
    return " · ".join(parts[:3])


def _close_time_str(close_iso: str) -> str:
    """Format close time to local time (America/New_York).
    Time-only when it closes today; includes the date otherwise
    (2026-09-01 fix: date-less close times misled on multi-day markets —
    a Sept-4 deadline rendered as bare '6:45 PM ET' on a Sept-1 game)."""
    if not close_iso:
        return ""
    try:
        from datetime import datetime, date
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
        local = dt.astimezone(ZoneInfo("America/New_York"))
        t = local.strftime("%I:%M %p ET").lstrip("0")
        if local.date() == date.today():
            return t
        return f"{local.strftime('%b')} {local.day}, {t}"
    except:
        return ""



def format_alert(alert, rank, send_reason: str, clob_match: bool) -> str:
    """Two-tier alert format:
    - Tier 1 (newcomer): clean, spaced, explains terms
    - Tier 2 (power user): compact data line with market metrics
    """
    mkt = alert.get("market", "")
    platform = alert.get("platform", "kalshi")
    title = alert.get("title") or _clean_market_name(mkt)
    short = _short_title(title)

    # ── If title is generic (no team names), extract from ticker ────
    matchup, game_time = _extract_matchup_from_ticker(mkt)
    if matchup and (short.lower().startswith("over ") or short.lower().startswith("under ") or short.lower().startswith("total ") or short.lower().startswith("o/u ")):
        short = f"{matchup}: {short}"

    score = alert.get("score", 0)
    flow = alert.get("flow_dollars", 0)
    htr = alert.get("hours_to_resolve")
    sev = alert.get("severity", "")
    direction = alert.get("direction")
    bid = alert.get("best_bid")
    ask = alert.get("best_ask")
    oi = alert.get("open_interest", 0)
    vol = alert.get("volume", 0)
    close_iso = alert.get("close_time", "")
    reasons = alert.get("reasons", "")
    fy = alert.get("flow_yes", 0)
    fn = alert.get("flow_no", 0)

    cat_emoji = _infer_category(title, mkt)

    # ── Direction ────────────────────────────────────────────────────
    if direction == 1:
        dir_str = "YES"
    elif direction == -1:
        dir_str = "NO"
    else:
        if fy > fn * 2:
            dir_str = "YES"
        elif fn > fy * 2:
            dir_str = "NO"
        else:
            dir_str = ""

    bid_c = int(bid * 100) if bid is not None else None
    ask_c = int(ask * 100) if ask is not None else None

    # ── Flow direction % ─────────────────────────────────────────────
    total_flow = fy + fn
    if total_flow > 0:
        dominant_pct = max(fy, fn) / total_flow * 100
        flow_dir = "YES" if fy >= fn else "NO"
        if dominant_pct >= 95:
            flow_dir_str = f"99%+ {flow_dir}"
        elif dominant_pct >= 65:
            flow_dir_str = f"{dominant_pct:.0f}% {flow_dir}"
        else:
            flow_dir_str = "mixed"
    else:
        flow_dir_str = ""

    # ── Tags ─────────────────────────────────────────────────────────
    tags = []
    if htr is not None and htr < HTR_URGENCY_THRESHOLD:
        tags.append("🔴 CLOSING SOON")
    if send_reason == "escalation":
        tags.append("⬆️ ESCALATING")
    if clob_match:
        tags.append("🦈 DOUBLE CONF")
    tag_str = " · ".join(tags) + "\n" if tags else ""

    # ── Verdict label ────────────────────────────────────────────────
    is_whale = "whale_flow_pierce" in reasons
    is_aggressive = "aggressive_taker" in reasons
    # Smart-wallet ENTRY is outcome-validated (65.8% hit); anonymous whale
    # flow is not (54.3%) — size alone is not an entry signal (2026-08-31 audit).
    smart_name = None
    for _r in reasons.split(","):
        _r = _r.strip()
        if _r.startswith("smart_wallet_"):
            smart_name = _r.replace("smart_wallet_", "").replace("_", " ")
            break
    if score >= 9 and (is_whale or smart_name):
        verdict = "🟩 ENTRY" if smart_name else "🟩 WHALE FLOW"
    elif score >= 9:
        verdict = "🟡 WATCH"
    elif score >= 7 and is_whale:
        verdict = "🟡 WATCH"
    elif score >= 7:
        verdict = "👀 MONITOR"
    else:
        verdict = "⚪ NOISE"

    lines = []

    # ── Header ───────────────────────────────────────────────────────
    raw = alert.get("raw_score")
    top_pct = alert.get("top_pct")
    # Display the raw intensity score (unbounded). The severity tag carries the
    # bounded meaning — "/10" implied a precision the capped score never had.
    # top_pct (API-computed) anchors raw against the live 7d alert population.
    if raw is not None:
        hdr_score = f"{raw:.1f}" + (f" · top {top_pct}" if top_pct else "")
    else:
        hdr_score = f"{score:.0f}"
    lines.append(f"{tag_str}{cat_emoji} <b>#{rank}</b> · {_esc(sev)} · {hdr_score} {verdict}")
    lines.append("")

    # ── Market name ──────────────────────────────────────────────────
    lines.append(_esc(short))
    # ── Matchup + first pitch (sports context from ticker) ──────────
    if matchup and matchup.lower() not in short.lower():
        sport_emoji = "⚾" if mkt.upper().startswith("KXMLB") else "⚽"
        ctx_line = f"{sport_emoji} {_esc(matchup)}"
        if game_time:
            ctx_line += f" · first pitch {_esc(game_time)}"
        lines.append(ctx_line)
    sub = alert.get("sub_title") or ""
    sub_clean = ""
    if sub and sub != short and sub != title:
        sub_clean = re.sub(r'^Will (?:the |a |an )?', '', sub).strip()
        is_redundant = sub_clean.lower() in short.lower() or sub_clean.lower() in title.lower()
        sub_numbers = re.findall(r'\d+\.?\d*', sub_clean)
        short_numbers = re.findall(r'\d+\.?\d*', short)
        if sub_numbers and short_numbers and sub_numbers == short_numbers:
            is_redundant = True
        is_team_name = len(sub_clean.split()) <= 3 and not any(w in sub_clean.lower() for w in ['over', 'under', 'total', 'goals', 'runs', 'points', 'score', 'scored'])
        if is_redundant and not is_team_name:
            sub_clean = ""
    lines.append("")

    # ── Price (with team name if applicable) ────────────────────────
    px_line = []
    if sub_clean:
        px_line.append(f"<i>{_esc(sub_clean)}</i>")
    if bid_c is not None:
        px_line.append(f"{_esc(dir_str)} @ {bid_c}¢" if dir_str else f"{bid_c}¢")
    if ask_c is not None and ask_c != bid_c:
        px_line.append(f"(ask {ask_c}¢)")
    if px_line:
        lines.append(" ".join(px_line))

    # ── Whale action (explained, with avg entry price) ─────────────
    dom_shares = max(fy, fn)
    avg_c = int(round(flow / dom_shares * 100)) if (flow and dom_shares > 0) else None
    avg_str = f" · avg ~{avg_c}¢" if avg_c and 1 <= avg_c <= 99 else ""
    whale_line = []
    if flow:
        if smart_name:
            base = f"🐋 <b>{_esc(smart_name)}</b> · ${flow:,.0f}{avg_str}"
            whale_line.append(f"{base} · {flow_dir_str}" if flow_dir_str else base)
        elif flow_dir_str:
            whale_line.append(f"🐋 Whale bought ${flow:,.0f}{avg_str} · {_esc(flow_dir_str)} of flow")
        else:
            whale_line.append(f"🐋 ${flow:,.0f}{avg_str} flow")
    if whale_line:
        lines.append(" ".join(whale_line))

    # ── Fair value vs whale entry (sharp-book devig) ─────────────────
    if platform == "kalshi" and mkt.startswith("KXMLBGAME") and avg_c:
        fair = _mlb_fair_value(mkt)
        if fair is not None:
            fair_c = fair * 100
            edge_c = avg_c - fair_c
            sign = "+" if edge_c >= 0 else "−"
            side_code = mkt.rsplit("-", 1)[-1]
            lines.append(
                f"📚 Books: {side_code} {fair_c:.0f}¢ fair · whale paid {avg_c}¢ "
                f"({sign}{abs(edge_c):.0f}¢ vs fair)"
            )

    # ── Concentration warning ────────────────────────────────────────
    m_int = re.search(r'intensity_(\d+)%', reasons)
    if m_int and int(m_int.group(1)) >= 80:
        lines.append(f"⚠️ Whale is {m_int.group(1)}% of all volume — price is whale-set, no independent confirmation")

    # ── Close time ───────────────────────────────────────────────────
    ct = _close_time_str(close_iso)
    if ct:
        lines.append(f"Closes {_esc(ct)}")
    elif htr is not None:
        lines.append(_esc(f"{htr:.0f}h left"))
    lines.append("")

    # ── Power user line: market size + triggers ─────────────────────
    data = []
    mid_px = alert.get("mid")
    # Kalshi OI/volume are CONTRACT counts, not dollars — flow_yes 147K
    # shares = $80.6K at 54.8¢ avg proves the units (2026-09-01 fix).
    # Show contracts + mid-price dollar estimate. PM values are already USD.
    kalshi_ct = platform == "kalshi" and mid_px
    if oi:
        data.append(f"OI {oi/1000:.0f}K ct ≈ ${oi*mid_px/1000:.0f}K" if kalshi_ct else f"Open interest ${oi/1000:.0f}K")
    if vol:
        data.append(f"Vol {vol/1000:.0f}K ct ≈ ${vol*mid_px/1000:.0f}K" if kalshi_ct else f"Volume ${vol/1000:.0f}K")
    hr = _human_reasons(reasons)
    if hr:
        data.append(_esc(hr))
    if data:
        lines.append(f"📊 {' · '.join(data)}")

    # ── Links ────────────────────────────────────────────────────────
    links = _build_market_links(alert.get("platform", "kalshi"), mkt)
    lines.append(" · ".join(links))

    return "\n".join(lines)


def _refresh_price(alert: dict) -> dict:
    """Fetch live bid/ask via /whale/book and update the alert.
    Returns the alert with fresh price if available, original otherwise."""
    mkt = alert.get("market", "")
    platform = alert.get("platform", "")
    try:
        r = requests.get(f"{API}/whale/book",
                         params={"platform": platform, "market": mkt},
                         timeout=5)
        if r.ok:
            d = r.json()
            bids = d.get("bids", [])
            asks = d.get("asks", [])
            if bids:
                alert["best_bid"] = bids[0][0]
                alert["_price_refreshed"] = True
            if asks:
                alert["best_ask"] = asks[0][0]
    except Exception:
        pass  # keep original price if refresh fails
    return alert


def _enrich_top_pct(alert: dict) -> dict:
    """Attach live 'top X%' percentile for the raw score (single-alert path;
    the batch path gets top_pct straight from /whale/top)."""
    if alert.get("raw_score") is None or alert.get("top_pct"):
        return alert
    try:
        r = requests.get(f"{API}/whale/percentile",
                         params={"raw": alert["raw_score"]}, timeout=3)
        if r.ok:
            alert["top_pct"] = r.json().get("top_pct")
    except Exception:
        pass  # percentile is decoration — never block the alert on it
    return alert


def send_single(alert: dict) -> bool:
    """Send a single alert immediately. Returns True if sent."""
    state = load_state()
    clob_fired = load_clob_fired()
    now = time.time()

    if not is_actionable(alert):
        return False

    mkt = alert.get("market", "")
    ok, reason = should_send(alert, state, now)
    if not ok:
        return False

    # Refresh price before sending — catch stale-price alerts
    alert = _refresh_price(alert)
    alert = _enrich_top_pct(alert)
    # Re-check after price refresh (market may have resolved)
    bid = alert.get("best_bid")
    if bid is not None and (bid > 0.90 or bid < 0.10):
        print(f"SKIP post-refresh decided: {alert.get('title','')[:40]} bid={bid:.2f}")
        return False

    prev = state.get(mkt, {})
    alert["_prev_score"] = prev.get("score", 0)
    alert["_send_reason"] = reason
    clob_match = mkt in clob_fired

    msg = format_alert(alert, 1, reason, clob_match)
    if clob_match:
        header = "🦈 <b>DOUBLE CONFIRMATION</b>\n\n"
    else:
        header = "🎯 <b>WHALE ALERT</b>\n\n"
    full = header + msg

    ok = send_tg(full)
    if ok:
        state[mkt] = {"ts": now, "score": alert.get("score", 0)}
        save_state(state)
    return ok


def main():
    import sys as _sys
    if "--single" in _sys.argv:
        # Called with a single alert JSON via stdin
        alert = json.loads(_sys.stdin.read())
        ok = send_single(alert)
        print(f"Sent: {ok}")
        return

    state = load_state()
    clob_fired = load_clob_fired()
    alerts = get_top_alerts()

    if not alerts:
        print("No alerts returned")
        return

    actionable = [a for a in alerts if is_actionable(a)]
    print(f"Total alerts: {len(alerts)}, actionable: {len(actionable)}")

    now = time.time()
    to_send = []
    for a in actionable:
        mkt = a.get("market", "")
        ok, reason = should_send(a, state, now)
        if ok:
            prev = state.get(mkt, {})
            a["_prev_score"] = prev.get("score", 0)
            a["_send_reason"] = reason
            to_send.append(a)

    if not to_send:
        print("No new actionable alerts (all deduplicated)")
        return

    # ── Build message ─────────────────────────────────────────────────
    n = len(to_send)
    double_conf = [a for a in to_send if a.get("market", "") in clob_fired]

    if double_conf:
        header = f"🦈 <b>DOUBLE CONFIRMATION — {len(double_conf)} market(s) confirmed by CLOB + scanner</b>"
    else:
        header = f"🎯 <b>WHALE ALERT — {n} signal(s)</b>"

    # Refresh prices and filter out decided markets
    refreshed = []
    for a in to_send[:8]:
        a = _refresh_price(a)
        bid = a.get("best_bid")
        if bid is not None and (bid > 0.90 or bid < 0.10):
            print(f"SKIP post-refresh decided: {a.get('title','')[:40]} bid={bid:.2f}")
            continue
        refreshed.append(a)
    to_send = refreshed
    if not to_send:
        print("All alerts filtered out after price refresh")
        return

    lines = [header, ""]
    for i, a in enumerate(to_send[:8], 1):
        clob_match = a.get("market", "") in clob_fired
        lines.append(format_alert(a, i, a["_send_reason"], clob_match))
        lines.append("")
    msg = "\n".join(lines)

    ok = send_tg(msg)
    if ok:
        for a in to_send:
            state[a.get("market", "")] = {"ts": now, "score": a.get("score", 0)}
        save_state(state)
        print(f"Sent {len(to_send)} alert(s)")
    else:
        print("TG send failed")


if __name__ == "__main__":
    main()
