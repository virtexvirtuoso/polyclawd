#!/usr/bin/env python3
"""Cross-Venue Whale Mirror — Polymarket whale entries -> Kalshi shadow logger.

Spec: vault 02-Projects/Polyclawd/Strategy/Smart-Money-Whale/Cross-Venue-Whale-Mirror-Kalshi-2026-10-16.md
SHADOW ONLY: no orders, no writes to shadow_trades.db / whale_scanner.db, no POLYCLAWD_MODE change.

Subcommands:
  cache   refresh Kalshi markets cache (public API, no auth)
  match   match unmatched PM entry-alert markets to Kalshi events
  tick    cron worker (every minute): sample order books, apply guards, record fills
  report  compute spec metrics + pre-registered gate verdict
"""
import json, math, os, re, sqlite3, sys, time, unicodedata
from datetime import datetime, timezone
from urllib.request import urlopen, Request

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
STORAGE = os.path.join(ROOT, "storage")
MIRROR_DB = os.path.join(STORAGE, "kalshi_mirror.db")
SHADOW_DB_RO = "file:" + os.path.join(STORAGE, "shadow_trades.db") + "?mode=ro"
SNAPSHOTS = os.path.join(STORAGE, "wallet_allowlist_snapshots.json")
LOG = os.path.join(ROOT, "logs", "kalshi_mirror.log")

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
FIRST_SNAPSHOT_TS = 1790045102            # 2026-09-22 allowlist snapshot
SAMPLE_OFFSETS = (0, 60, 300, 900)        # +0s, +60s, +5m, +15m
FILL_OFFSET = 60                          # spec: fill at alert_ts + 60s
PRICE_GUARD = 0.03                        # skip if ask > PM entry + 3c
ORDER_USD = 25.0                          # spec: $25 order
LIQ_FLOOR = 0.90                          # skip if top-of-book < 90% of needed size
CACHE_WINDOW_DAYS = (2, 90)               # close_time in [now-2d, now+90d]

# Kalshi taker fee — hard-coded 2026-10-16 from kalshi.com/fee-schedule
# (July 2026 schedule, multiplier 1, most markets) + docs.kalshi.com fee rounding.
FEE_SCHEDULE_DATE = "2026-10-16"
def taker_fee(price):                     # price in dollars 0..1
    return math.ceil(0.07 * price * (1.0 - price) * 100.0) / 100.0

NICK_TO_CITY = {
    "ravens": "baltimore", "cowboys": "dallas", "vikings": "minnesota",
    "buccaneers": "tampa bay", "eagles": "philadelphia", "bears": "chicago",
    "astros": "houston", "athletics": "a s", "phillies": "philadelphia",
    "braves": "atlanta", "cubs": "chicago", "padres": "san diego",
    "valkyries": "golden state", "wings": "dallas", "liberty": "new york",
    "dream": "atlanta",
}
MATCH_SERIES = {"KXCS2GAME", "KXDOTA2GAME", "KXNFLGAME", "KXMLBGAME", "KXWNBA",
                "KXNCAAFGAME", "KXATPCHALLENGERMATCH", "KXITFMATCH", "KXITFWMATCH"}

def log(msg):
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def http_json(url):
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()

def day_shift(d, n):
    return datetime.fromisoformat(d + "T00:00:00+00:00").fromtimestamp(
        __import__("calendar").timegm(datetime.fromisoformat(d + "T00:00:00+00:00").timetuple()) + n * 86400
    ).strftime("%Y-%m-%d")

# ---------------------------------------------------------------- storage
def mirror_db():
    db = sqlite3.connect(MIRROR_DB, timeout=30)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS kalshi_markets_cache (
        ticker TEXT PRIMARY KEY, event_ticker TEXT, series TEXT, title TEXT,
        yes_sub_title TEXT, no_sub_title TEXT, rules TEXT,
        close_time TEXT, status TEXT, result TEXT, fetched_at INTEGER);
    CREATE TABLE IF NOT EXISTS pm_kalshi_matches (
        pm_condition_id TEXT PRIMARY KEY, pm_title TEXT, pm_end_date TEXT,
        kalshi_event TEXT, kalshi_series TEXT, kalshi_side_ticker TEXT,
        match_confidence TEXT, matched_at INTEGER, notes TEXT);
    CREATE TABLE IF NOT EXISTS mirror_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shadow_row_id INTEGER, pm_condition_id TEXT, wallet TEXT,
        is_allowlist_followed INTEGER, outcome TEXT, outcome_index INTEGER,
        pm_entry_price REAL, ts_alert INTEGER,
        kalshi_event TEXT, kalshi_ticker TEXT, kalshi_side TEXT,
        ask_0s REAL, ask_60s REAL, ask_5m REAL, ask_15m REAL,
        ask_0s_ts INTEGER, ask_60s_ts INTEGER, ask_5m_ts INTEGER, ask_15m_ts INTEGER,
        yes_ask REAL, no_ask REAL, yes_size REAL, no_size REAL,
        fill_price REAL, fee REAL, status TEXT, skip_reason TEXT,
        late_detection_s REAL, resolved INTEGER DEFAULT 0,
        pm_outcome_result TEXT, kalshi_result TEXT, clv REAL, notes TEXT);
    CREATE TABLE IF NOT EXISTS report_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_ts INTEGER, params TEXT,
        result TEXT);
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    return db

def shadow_db():
    return sqlite3.connect(SHADOW_DB_RO, uri=True, timeout=30)

def deploy_ts():
    """First-tick timestamp: enroll only alerts fired after deployment.
    The locked fill model forbids historical ask reconstruction, so the
    gate clock starts at the first tick, not at the first alert."""
    db = mirror_db()
    row = db.execute("SELECT value FROM meta WHERE key='deploy_ts'").fetchone()
    if row:
        return int(row[0])
    v = int(time.time())
    db.execute("INSERT OR IGNORE INTO meta VALUES ('deploy_ts', ?)", (str(v),))
    db.commit()
    return v

def load_snapshots():
    with open(SNAPSHOTS) as f:
        data = json.load(f)
    return [(s["ts"], list(s["wallets"].keys())) for s in data["snapshots"]]

def allowlist_wallet_at(snapshots, ts):
    best = None
    for ts_snap, wallets in snapshots:
        if ts_snap <= ts and (best is None or ts_snap > best[0]):
            best = (ts_snap, wallets)
    return set(best[1]) if best else None

def followed_entry_alerts():
    """Spec rule 1: entry alerts, no refires, BUY, 0.01-0.60, near_settled=0,
    wallet in latest snapshot BEFORE alert ts."""
    snaps = load_snapshots()
    db = shadow_db()
    rows = db.execute(
        "SELECT id, wallet, market, title, direction, outcome, outcome_index, "
        "price_at_alert, ts_alert, resolved, outcome_result, closing_price, clv, near_settled "
        "FROM smart_wallet_shadows WHERE ts_alert >= ? AND alert_type='entry' "
        "AND direction='BUY' AND price_at_alert BETWEEN 0.01 AND 0.60 "
        "AND COALESCE(near_settled,0)=0", (FIRST_SNAPSHOT_TS,)).fetchall()
    out = []
    for (rid, wallet, market, title, direction, outcome, oidx, price,
         ts, resolved, result, close_px, clv, near) in rows:
        ws = allowlist_wallet_at(snaps, ts)
        if ws and wallet in ws:
            out.append(dict(id=rid, wallet=wallet, market=market, title=title,
                            outcome=outcome, outcome_index=oidx, price=price,
                            ts=ts, resolved=resolved, result=result,
                            closing_price=close_px, clv=clv))
    return out

def blanket_entry_alerts():
    """Control: every whale entry alert in the window (no allowlist filter)."""
    db = shadow_db()
    return db.execute(
        "SELECT id, wallet, market, title, outcome, outcome_index, price_at_alert, "
        "ts_alert, resolved, outcome_result, closing_price, clv FROM smart_wallet_shadows "
        "WHERE ts_alert >= ? AND alert_type='entry' AND direction='BUY' "
        "AND price_at_alert BETWEEN 0.01 AND 0.60 AND COALESCE(near_settled,0)=0",
        (FIRST_SNAPSHOT_TS,)).fetchall()

# ---------------------------------------------------------------- cache
def cmd_cache():
    db = mirror_db()
    now = int(time.time())
    lo = now - CACHE_WINDOW_DAYS[0] * 86400
    hi = now + CACHE_WINDOW_DAYS[1] * 86400
    url = f"{KALSHI}/markets?min_close_ts={lo}&max_close_ts={hi}&limit=1000"
    n = 0
    while url:
        data = http_json(url)
        for m in data.get("markets", []):
            db.execute(
                "INSERT OR REPLACE INTO kalshi_markets_cache VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (m["ticker"], m.get("event_ticker"), m["ticker"].split("-")[0],
                 m.get("title"), m.get("yes_sub_title"), m.get("no_sub_title"),
                 (m.get("rules_primary") or "")[:2000], m.get("close_time"),
                 m.get("status"), m.get("result"), now))
            n += 1
        cur = data.get("cursor")
        url = f"{KALSHI}/markets?min_close_ts={lo}&max_close_ts={hi}&limit=1000&cursor={cur}" if cur else None
    db.commit()
    total = db.execute("SELECT COUNT(*) FROM kalshi_markets_cache").fetchone()[0]
    log(f"cache: fetched {n} rows, cache total {total}")
    print(f"cache: fetched {n}, total {total}")

# ---------------------------------------------------------------- match
NFL_NICKS = ("ravens|cowboys|vikings|buccaneers|eagles|bears|packers|chiefs|bills|dolphins|jets|"
             "patriots|steelers|bengals|browns|texans|colts|jaguars|titans|broncos|chargers|"
             "raiders|commanders|lions|falcons|panthers|saints|cardinals|rams|49ers|seahawks")
MLB_NICKS = ("astros|phillies|cubs|padres|braves|athletics|yankees|dodgers|mets|orioles|red sox|"
             "blue jays|rays|guardians|royals|tigers|twins|white sox|angels|mariners|rangers|"
             "diamondbacks|rockies|giants|brewers|pirates|cardinals|marlins|nationals|reds")
WNBA_NICKS = "valkyries|liberty|wings|dream|aces|sky|sun|fever|storm|lynx|mercury|sparks|mystics"
NCAAF_RE = (r"clemson|ucla|wake forest|tcu|uconn|ole miss|air force|maryland|louisville|"
            r"ucf|nevada|california|florida")
TENNIS_RE = r"china open|japan open|hangzhou|jingshan|adana|mouilleron"
SOCCER_SERIES = ["KXESOCCERGAME", "KXISRNLGAME", "KXLIGAPROMATCH", "KXBUNDESLIGA1H",
                 "KXSERIEA1H", "KXLIGUE11H", "KXEREDIVISIE1H", "KXALLSVENSKANGAME",
                 "KXGER3LGAME", "KXSUPERLIGGAME", "KXDENSUPERLIGAGAME", "KXAUTBUNDESGAME",
                 "KXLIIGAGAME", "KXELHGAME", "KXSESBLGAME", "KXABAGAME", "KXSKLGAME",
                 "KXBUNDESLIGA2GAME", "KXBUNDESLIGAWGAME", "KXAUTBSLGAME", "KXQSTARSGAME",
                 "KXEBASKETBALLGAME", "KXEUROLEAGUEGAME", "KXKHLGAME"]

def series_for(title):
    l = title.lower()
    if l.startswith("cs:") or l.startswith("counter-strike"): return ["KXCS2GAME", "KXCS2MAP"]
    if l.startswith("dota 2"):         return ["KXDOTA2GAME", "KXDOTA2MAP"]
    if l.startswith("lol:"):           return ["KXLOLMAP"]
    if l.startswith("valorant"):       return []
    if l.startswith("spread:"):        return ["KXWNBA"]
    cands = []
    if re.search(NFL_NICKS, l): cands += ["KXNFLGAME"]
    if re.search(MLB_NICKS, l): cands += ["KXMLBGAME"]
    if re.search(WNBA_NICKS, l): cands += ["KXWNBA"]
    if re.search(NCAAF_RE, l): cands += ["KXNCAAFGAME"]
    if re.search(TENNIS_RE, l) or re.match(r"^[^:]+:\s*[a-z.'\-]+ vs\.? [a-z.'\-]+\s*$", l):
        cands += ["KXATPCHALLENGERMATCH", "KXITFMATCH", "KXITFWMATCH"]
    if re.match(r"^will .* win on ", l):
        cands += SOCCER_SERIES
    return cands

def parse_teams(title):
    t = re.sub(r"^spread:\s*", "", title, flags=re.I)
    t = re.sub(r"^[^:]+:\s*", "", t)
    t = re.sub(r"\s*\(BO\d\).*", "", t, flags=re.I)
    t = re.sub(r"\s*-\s*(Game|Map) \d+ Winner.*", "", t, flags=re.I)
    t = re.sub(r"\s*-\s*.*$", "", t)
    t = re.sub(r"^will (.*?) win on .*?\?$", r"\1", t, flags=re.I)
    return [x.strip() for x in re.split(r"\s+vs\.?\s+", t, flags=re.I) if x.strip()]

def scope_of(title):
    g = re.search(r"Game (\d+) Winner", title, re.I)
    if g: return ("game", int(g.group(1)))
    m = re.search(r"Map (\d+) Winner", title, re.I)
    if m: return ("map", int(m.group(1)))
    if title.lower().startswith("spread:"): return ("spread", 0)
    return ("match", 0)

def team_variants(team):
    n = norm(team)
    out = [n]
    for nick, city in NICK_TO_CITY.items():
        if nick in n:
            out.append(norm(city))
    return list(dict.fromkeys(out))

def clob_market(cid):
    try:
        return http_json(f"https://clob.polymarket.com/markets/{cid}")
    except Exception:
        return None

def cmd_match():
    mdb = mirror_db()
    alerts = followed_entry_alerts()
    cids = sorted({a["market"] for a in alerts})
    # events from cache
    events = {}
    for (ticker, event_ticker, series, title, ysub, nsub, rules, close, status, result, _f) in \
            mdb.execute("SELECT * FROM kalshi_markets_cache"):
        ev = event_ticker or ticker.rsplit("-", 1)[0]
        e = events.setdefault(ev, {"series": series, "close": close[:10],
                                   "titles": [], "status": status})
        e["titles"].append(title or "")
    matched = skipped = 0
    for cid in cids:
        row = mdb.execute("SELECT 1 FROM pm_kalshi_matches WHERE pm_condition_id=?", (cid,)).fetchone()
        if row:
            continue
        title = next((a["title"] for a in alerts if a["market"] == cid), "")
        meta = clob_market(cid)
        if meta is None:
            log(f"match: CLOB fetch failed {cid}; retry next pass")
            continue
        end_date = (meta.get("end_date_iso") or "")[:10]
        game_date = meta.get("game_start_time") or ""
        game_date = game_date[:10] if game_date else None
        ref = game_date or end_date or None
        if not ref:
            mdb.execute("INSERT INTO pm_kalshi_matches VALUES (?,?,?,?,?,?,?,?,?)",
                        (cid, title, None, None, None, None, "reject", int(time.time()),
                         "no PM end date"))
            mdb.commit(); skipped += 1; continue
        teams = parse_teams(title)
        vinfo = [team_keys(t) for t in teams]
        variants = [v[0] for v in vinfo]
        kind, num = scope_of(title)
        cands = series_for(title)
        dmin, dmax = day_shift(ref, -1), day_shift(ref, 1)
        hit = None
        for ev, e in sorted(events.items()):
            if (e.get("status") or "") in ("settled", "finalized"): continue
            if e["series"] not in cands: continue
            if not (dmin <= e["close"] <= dmax): continue
            text = norm(" ".join(e["titles"]))
            if not all(any(v in text for v in vs) for vs in variants): continue
            noncity = [k for vi in vinfo for k in vi[1]]
            if not any(k in text for k in noncity): continue
            if teams and len(teams) < 2 and kind != "spread": continue
            if kind == "match":
                ok = e["series"] in MATCH_SERIES
            elif kind in ("game", "map"):
                ok = any(re.search(rf"map {num}\b", t.split(" | ")[0], re.I) for t in e["titles"])
            else:
                ok = "spread" in " ".join(e["titles"]).lower()
            if ok:
                hit = (ev, e); break
        if hit:
            ev, e = hit
            side = pick_side_ticker(mdb, ev, teams)
            mdb.execute("INSERT OR REPLACE INTO pm_kalshi_matches VALUES (?,?,?,?,?,?,?,?,?)",
                        (cid, title, end_date, ev, e["series"], side, "exact",
                         int(time.time()), f"ref_date={ref}"))
            matched += 1
        else:
            mdb.execute("INSERT INTO pm_kalshi_matches VALUES (?,?,?,?,?,?,?,?,?)",
                        (cid, title, end_date, None, None, None, "reject",
                         int(time.time()), f"ref_date={ref}"))
            skipped += 1
        mdb.commit()
    log(f"match: {matched} matched, {skipped} rejected this pass")
    print(f"match: matched {matched}, rejected {skipped}")

def pick_side_ticker(mdb, event_ticker, teams):
    """Return the Kalshi market ticker for the PM outcome side (YES-side default)."""
    row = mdb.execute(
        "SELECT ticker FROM kalshi_markets_cache WHERE event_ticker=? LIMIT 1",
        (event_ticker,)).fetchone()
    return row[0] if row else None

GENERIC_TOKENS = {"blue", "city", "united", "inter", "real", "sporting", "athletic", "club"}
CITY_WORDS = {"baltimore", "dallas", "minnesota", "tampa", "philadelphia", "chicago",
              "houston", "atlanta", "diego", "francisco", "angeles", "toronto", "boston",
              "miami", "seattle", "denver", "kansas", "vegas", "arizona", "cleveland",
              "cincinnati", "pittsburgh", "detroit", "milwaukee", "oakland", "nashville",
              "jacksonville", "carolina", "buffalo", "green", "bay", "new", "york", "los",
              "san", "st", "louis", "minneapolis", "arlington", "anaheim", "washington",
              "california", "florida", "maryland", "nevada", "louisville", "ucla", "uconn"}

def team_keys(team):
    """(all_keys, noncity_keys) for a team/outcome string. Keys cover full name,
    nickname->city variants, significant tokens and token bigrams; noncity keys
    exclude bare city words so cross-league city collisions cannot match."""
    n = norm(team or "")
    if not n:
        return ([], [])
    toks = [t for t in n.split() if len(t) >= 4 and t not in GENERIC_TOKENS]
    words = n.split()
    bigrams = [" ".join(words[i:i + 2]) for i in range(len(words) - 1)]
    allk = list(dict.fromkeys(team_variants(team) + toks + bigrams))
    noncity = [k for k in allk
               if not (k in CITY_WORDS or all(w in CITY_WORDS for w in k.split()))]
    return (allk, noncity)

def resolve_side(mdb, kalshi_event, pm_title, outcome, oidx):
    """Map one PM alert to (kalshi_side_market_ticker, 'yes'|'no'); None if unclear.
    Kalshi game events are per-team binaries; the PM outcome is a team name, Yes/No
    on a 'Will X win' title, or Over/Under (no Kalshi twin -> None)."""
    o = (outcome or "").strip()
    oi = 0 if oidx is None else oidx
    if o.lower() in ("over", "under", ""):
        return None
    rows = mdb.execute(
        "SELECT ticker, title, yes_sub_title FROM kalshi_markets_cache WHERE event_ticker=?",
        (kalshi_event,)).fetchall()
    if not rows:
        return None
    g = re.search(r"(?:game|map) (\d+) winner", pm_title or "", re.I)
    if g:
        rows = [r for r in rows if re.search(rf"map {g.group(1)}\b", r[1] or "", re.I)]
    else:
        rows = [r for r in rows if not re.search(r"map \d+\b", r[1] or "", re.I)]
    if not rows:
        return None
    if o.lower() in ("yes", "no"):
        m = re.match(r"^will (.*?) win", pm_title or "", re.I)
        if m:
            subj = m.group(1)
        else:
            teams = parse_teams(pm_title or "")
            if len(teams) < 2:
                return None
            subj = teams[0] if oi == 0 else teams[1]
        want_yes = o.lower() == "yes"
    else:
        subj = o
        want_yes = True
    keys, _nc = team_keys(subj)
    if not keys:
        return None
    hits = []
    for (ticker, title, ysub) in rows:
        text = norm((title or "") + " " + (ysub or ""))
        h = max((len(k) for k in keys if k in text), default=0)
        if h:
            hits.append((h, ticker))
    if not hits:
        return None
    top = max(h for h, _ in hits)
    winners = [t for h, t in hits if h == top]
    if len(winners) != 1:
        return None
    return (winners[0], "yes" if want_yes else "no")

# ---------------------------------------------------------------- tick
def orderbook(ticker):
    try:
        return http_json(f"{KALSHI}/markets/{ticker}/orderbook")
    except Exception as e:
        log(f"orderbook {ticker}: {e}")
        return None

def best_bid(ob, side):
    """Best BID (price, size) on one side from orderbook_fp *_dollars string pairs.
    Kalshi books rest bids on each side; a side's executable ask is derived from
    the opposite side's best bid (verified live 2026-10-16: best yes_dollars ==
    yes_bid_dollars; 1 - best no_dollars == yes_ask_dollars)."""
    book = ob.get("orderbook_fp") or {}
    levels = book.get(f"{side}_dollars") or []
    best = None
    for lvl in levels:
        try:
            price = float(lvl[0])
            size = float(lvl[1])
        except Exception:
            continue
        if size > 0 and 0 < price < 1:
            if best is None or price > best[0]:
                best = (price, size)
    return best or (None, None)

def ask_for(ob, side):
    """Executable ask (price, backing_size) for side 'yes'/'no'."""
    opp = "no" if side == "yes" else "yes"
    bid, bsz = best_bid(ob, opp)
    if bid is not None:
        return (round(1.0 - bid, 4), bsz)
    return (None, None)

def open_trades(mdb):
    return mdb.execute(
        "SELECT id, shadow_row_id, pm_condition_id, wallet, is_allowlist_followed, "
        "outcome, outcome_index, pm_entry_price, ts_alert, kalshi_event, kalshi_ticker, "
        "kalshi_side, ask_0s, ask_60s, ask_5m, ask_15m, status FROM mirror_trades WHERE status='open'"
    ).fetchall()

def cmd_tick():
    mdb = mirror_db()
    now = int(time.time())
    # 1) enroll new alerts (allowlist + blanket) that have a match
    matches = {r[0]: r for r in mdb.execute(
        "SELECT pm_condition_id, kalshi_event, kalshi_series, kalshi_side_ticker, "
        "match_confidence FROM pm_kalshi_matches WHERE match_confidence IN ('exact','equivalent')")}
    seen = {r[0] for r in mdb.execute("SELECT shadow_row_id FROM mirror_trades")}
    enrolled = 0
    dep = deploy_ts()
    for a in followed_entry_alerts():
        if a["id"] in seen or a["market"] not in matches: continue
        if a["ts"] < dep: continue          # locked fill model: no historical back-fill
        m = matches[a["market"]]
        side = resolve_side(mdb, m[1], a["title"], a["outcome"], a["outcome_index"])
        if side is None:
            mdb.execute(
                "INSERT INTO mirror_trades (shadow_row_id, pm_condition_id, wallet, "
                "is_allowlist_followed, outcome, outcome_index, pm_entry_price, ts_alert, "
                "kalshi_event, status, skip_reason) VALUES (?,?,?,?,?,?,?,?,?,'skipped','side_unresolved')",
                (a["id"], a["market"], a["wallet"], 1, a["outcome"], a["outcome_index"],
                 a["price"], a["ts"], m[1]))
            continue
        mdb.execute(
            "INSERT INTO mirror_trades (shadow_row_id, pm_condition_id, wallet, "
            "is_allowlist_followed, outcome, outcome_index, pm_entry_price, ts_alert, "
            "kalshi_event, kalshi_ticker, kalshi_side, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,'open')",
            (a["id"], a["market"], a["wallet"], 1, a["outcome"], a["outcome_index"],
             a["price"], a["ts"], m[1], side[0], side[1]))
        enrolled += 1
    for (rid, wallet, market, title, outcome, oidx, price, ts, resolved, result, close_px, clv) in blanket_entry_alerts():
        if rid in seen or market not in matches: continue
        if ts < dep: continue
        m = matches[market]
        dup = mdb.execute("SELECT 1 FROM mirror_trades WHERE shadow_row_id=?", (rid,)).fetchone()
        if dup: continue
        side = resolve_side(mdb, m[1], title, outcome, oidx)
        if side is None:
            mdb.execute(
                "INSERT INTO mirror_trades (shadow_row_id, pm_condition_id, wallet, "
                "is_allowlist_followed, outcome, outcome_index, pm_entry_price, ts_alert, "
                "kalshi_event, status, skip_reason) VALUES (?,?,?,?,?,?,?,?,?,'skipped','side_unresolved')",
                (rid, market, wallet, 0, outcome, oidx, price, ts, m[1]))
            continue
        mdb.execute(
            "INSERT INTO mirror_trades (shadow_row_id, pm_condition_id, wallet, "
            "is_allowlist_followed, outcome, outcome_index, pm_entry_price, ts_alert, "
            "kalshi_event, kalshi_ticker, kalshi_side, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,'open')",
            (rid, market, wallet, 0, outcome, oidx, price, ts, m[1], side[0], side[1]))
        enrolled += 1
    mdb.commit()
    # 2) sample books for due offsets. Kalshi books rest BIDS per side; a side's
    #    executable ask = 1 - best bid on the opposite side (verified 2026-10-16).
    sampled = filled = skipped = resolved_n = 0
    for t in open_trades(mdb):
        (tid, srid, cid, wallet, is_allow, outcome, oidx, entry, ts_alert,
         kev, kticker, kside, a0, a60, a5, a15, status) = t
        for off, col, tscol in ((0, "ask_0s", "ask_0s_ts"), (60, "ask_60s", "ask_60s_ts"),
                                (300, "ask_5m", "ask_5m_ts"), (900, "ask_15m", "ask_15m_ts")):
            if now < ts_alert + off: continue
            cur = mdb.execute(f"SELECT {col} FROM mirror_trades WHERE id=?", (tid,)).fetchone()
            if cur[0] is not None: continue
            ob = orderbook(kticker)
            if not ob: continue
            ya, ysz = ask_for(ob, "yes")
            na, nsz = ask_for(ob, "no")
            price, size = (ya, ysz) if (kside or "yes") == "yes" else (na, nsz)
            late = now - (ts_alert + off)
            mdb.execute(f"UPDATE mirror_trades SET {col}=?, {tscol}=?, late_detection_s=?, "
                        f"yes_ask=?, no_ask=?, yes_size=?, no_size=? WHERE id=?",
                        (price, now, late, ya, na, ysz, nsz, tid))
            sampled += 1
        # 3) fill decision at +60s
        row = mdb.execute("SELECT ask_60s, yes_size, no_size, kalshi_side FROM mirror_trades WHERE id=?", (tid,)).fetchone()
        ask60, ysz, nsz, kside2 = row
        if ask60 is None:
            if now > ts_alert + 1200:
                mdb.execute("UPDATE mirror_trades SET status='skipped', skip_reason='no_book' WHERE id=?", (tid,))
                skipped += 1
            continue
        side = kside2 or "yes"
        size = ysz if side == "yes" else nsz
        need = math.ceil(ORDER_USD / ask60) if ask60 > 0 else 0
        if ask60 > entry + PRICE_GUARD:
            mdb.execute("UPDATE mirror_trades SET status='skipped', skip_reason='price_guard' WHERE id=?", (tid,))
            skipped += 1
        elif size is not None and size < LIQ_FLOOR * need:
            mdb.execute("UPDATE mirror_trades SET status='skipped', skip_reason='liquidity' WHERE id=?", (tid,))
            skipped += 1
        else:
            fee = taker_fee(ask60)
            mdb.execute("UPDATE mirror_trades SET status='filled', fill_price=?, fee=?, kalshi_side=? WHERE id=?",
                        (ask60, fee, side, tid))
            filled += 1
    # 4) resolve filled trades whose Kalshi market has settled (cache carries result)
    for (tid, kticker, kside, srid) in mdb.execute(
            "SELECT id, kalshi_ticker, kalshi_side, shadow_row_id FROM mirror_trades "
            "WHERE status='filled' AND resolved=0").fetchall():
        c = mdb.execute("SELECT status, result FROM kalshi_markets_cache WHERE ticker=?", (kticker,)).fetchone()
        if not c or not c[1] or (c[0] or "") in ("active", "initialized"):
            continue
        pm = shadow_db().execute("SELECT outcome_result FROM smart_wallet_shadows WHERE id=?", (srid,)).fetchone()
        mdb.execute("UPDATE mirror_trades SET resolved=1, kalshi_result=?, pm_outcome_result=? WHERE id=?",
                    ((c[1] or "").strip().lower(), (pm[0] if pm else None), tid))
        resolved_n += 1
    mdb.commit()
    if enrolled or sampled or filled or skipped or resolved_n:
        log(f"tick: enrolled {enrolled}, sampled {sampled}, filled {filled}, skipped {skipped}, resolved {resolved_n}")

# ---------------------------------------------------------------- report
def bootstrap_ci(values, n_resamples=10000, lo=0.05):
    import random
    if not values: return (None, None)
    random.seed(20261016)
    means = []
    n = len(values)
    for _ in range(n_resamples):
        means.append(sum(values[random.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return (means[int(lo * n_resamples)], means[int((1 - lo) * n_resamples)])

def cmd_report():
    mdb = mirror_db()
    rows = mdb.execute(
        "SELECT pm_entry_price, fill_price, fee, kalshi_side, kalshi_result, "
        "pm_outcome_result, outcome, is_allowlist_followed, shadow_row_id, ts_alert "
        "FROM mirror_trades WHERE status='filled' AND resolved=1").fetchall()

    def k_win(r):
        # primary: Kalshi settlement vs the side we mirrored
        if r[3] and r[4]:
            return 1 if r[4].strip().lower() == r[3].strip().lower() else 0
        return None

    YN = {"yes", "no"}
    def pm_win(r):
        # PM-side win, only when outcome text and its resolution are comparable
        pres, oc = (r[5] or "").strip().lower(), (r[6] or "").strip().lower()
        if not pres or not oc:
            return None
        if pres == oc:
            return 1
        if pres in YN and oc in YN:
            return 0
        return None

    def pnl(rs, winfn, with_fee):
        out = []
        for r in rs:
            w = winfn(r)
            if w is None or not r[1]:
                continue
            fee = r[2] if with_fee else 0.0
            out.append((w * (1 - r[1]) - (1 - w) * r[1] - fee) / r[1])
        return out

    allow_rows = [r for r in rows if r[7] == 1]
    blanket_rows = [r for r in rows if r[7] == 0]
    allow_pnl = pnl(allow_rows, k_win, True)
    blanket_pnl = pnl(blanket_rows, k_win, True)
    pm_pnl = pnl(allow_rows, pm_win, False)
    lo, hi = bootstrap_ci(allow_pnl) if allow_pnl else (None, None)
    n_days = 0.0
    if rows:
        ts = [r[9] for r in rows]
        n_days = (max(ts) - min(ts)) / 86400.0
    n = len(allow_pnl)
    mean = lambda v: (sum(v) / len(v) if v else None)
    ec = (sum(allow_pnl) / sum(pm_pnl)) if (allow_pnl and pm_pnl and sum(pm_pnl) != 0) else None
    diff_pp = ((mean(allow_pnl) - mean(blanket_pnl)) * 100.0) if (allow_pnl and blanket_pnl) else None

    def bucket(p):
        return "<10c" if p < 0.10 else ("10-25c" if p < 0.25 else "25-60c")
    strat = {}
    for r in allow_rows:
        w = k_win(r)
        if w is None or not r[1]:
            continue
        strat.setdefault(bucket(r[1]), []).append(
            (w * (1 - r[1]) - (1 - w) * r[1] - r[2]) / r[1])
    strat_out = {b: {"n": len(v), "pnl_per_dollar": round(sum(v) / len(v), 4)}
                 for b, v in sorted(strat.items())}
    try:
        att, mat = mdb.execute(
            "SELECT COUNT(*), COALESCE(SUM(match_confidence IN ('exact','equivalent')),0) "
            "FROM pm_kalshi_matches").fetchone()
        match_rate = {"attempted": att, "matched": mat}
    except Exception:
        match_rate = None
    skips = {k: v for k, v in mdb.execute(
        "SELECT skip_reason, COUNT(*) FROM mirror_trades WHERE status='skipped' GROUP BY skip_reason")}
    criteria = {
        "n>=100_and_days>=30": bool(n >= 100 and n_days >= 30),
        "pnl_pos_and_ci_lo_pos": bool(allow_pnl and sum(allow_pnl) > 0 and lo is not None and lo > 0),
        "edge_capture>=0.5": bool(ec is not None and ec >= 0.5),
        "allowlist_beats_blanket>=3pp": bool(diff_pp is not None and diff_pp >= 3.0),
    }
    if n < 100:
        verdict = "NOT PASSED (insufficient n)"
    elif all(criteria.values()):
        verdict = "PASSED (recommendation only — live needs Mr. V explicit go)"
    else:
        verdict = "FAILED"
    gate = {
        "n_resolved": n,
        "days": round(n_days, 1),
        "pnl_per_dollar": round(mean(allow_pnl), 4) if allow_pnl else None,
        "ci90": [round(lo, 4) if lo is not None else None, round(hi, 4) if hi is not None else None],
        "edge_capture": round(ec, 4) if ec is not None else None,
        "allowlist_minus_blanket_pp": round(diff_pp, 2) if diff_pp is not None else None,
        "fee_stratified": strat_out,
        "match_rate": match_rate,
        "skip_reasons": skips,
        "criteria": criteria,
        "verdict": verdict,
    }
    result = json.dumps(gate, default=str)
    mdb.execute("INSERT INTO report_runs (run_ts, params, result) VALUES (?,?,?)",
                (int(time.time()), json.dumps({"fee_date": FEE_SCHEDULE_DATE}), result))
    mdb.commit()
    print(json.dumps(gate, indent=1))
    log("report: " + result)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    {"cache": cmd_cache, "match": cmd_match, "tick": cmd_tick, "report": cmd_report}[cmd]()
