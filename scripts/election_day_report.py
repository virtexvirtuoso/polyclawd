#!/usr/bin/env python3
"""Election-day report for the 2026 US midterms: Kalshi vs Polymarket -> Telegram.

Targeted pull (~150 requests): chamber control, balance of power, Kalshi seat
buckets and every Senate/governor party-win market on both venues. The full
election_tracker scan pages ~35k Kalshi markets and is too slow for a 30-min
cadence. Prices are bid/ask mids; last trade only when the book is empty or
wider than 10c.

Guarded to 2026-11-03 12:00 UTC .. 2026-11-04 12:30 UTC so its cron lines are
inert outside election day; --force overrides.

  venv/bin/python3 scripts/election_day_report.py --force --no-state   # print only
  venv/bin/python3 scripts/election_day_report.py --send               # cron
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
STATE_FILE = ROOT / "storage" / "election_day_state.json"
REPORT_DIR = ROOT / "storage" / "election_reports"
ET = ZoneInfo("America/New_York")
WINDOW = (datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc),
          datetime(2026, 11, 4, 12, 30, tzinfo=timezone.utc))
ELECTION_DAY = datetime(2026, 11, 3, tzinfo=ET)
GAP_FLAG = 0.05   # cross-venue gap worth a look
WIDE_BOOK = 0.10  # spread above which the mid is not trusted
SAFE = 0.90       # leader price at which a race is listed as safe
DEBUG = False

STATE_NAMES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IA": "iowa", "KS": "kansas", "KY": "kentucky",
    "LA": "louisiana", "ME": "maine", "MD": "maryland", "MA": "massachusetts",
    "MI": "michigan", "MN": "minnesota", "MS": "mississippi", "MT": "montana",
    "NE": "nebraska", "NV": "nevada", "NH": "new-hampshire", "NJ": "new-jersey",
    "NM": "new-mexico", "NY": "new-york", "NC": "north-carolina", "OH": "ohio",
    "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania", "RI": "rhode-island",
    "SC": "south-carolina", "SD": "south-dakota", "TN": "tennessee", "TX": "texas",
    "VT": "vermont", "VA": "virginia", "WV": "west-virginia", "WI": "wisconsin",
    "WY": "wyoming",
}
# 33 class-2 seats + FL and OH specials
SENATE = ["AL", "AK", "AR", "CO", "DE", "FL", "GA", "ID", "IL", "IA", "KS", "KY",
          "LA", "ME", "MA", "MI", "MN", "MS", "MT", "NE", "NH", "NJ", "NM", "NC",
          "OH", "OK", "OR", "RI", "SC", "SD", "TN", "TX", "VA", "WV", "WY"]
GOVERNOR = ["AL", "AK", "AZ", "AR", "CA", "CO", "CT", "FL", "GA", "HI", "ID", "IL",
            "IA", "KS", "ME", "MD", "MA", "MI", "MN", "NE", "NV", "NH", "NM", "NY",
            "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "VT", "WI", "WY"]
# Main poll close, ET (24:00 = midnight). Approximate: FL panhandle, TX El Paso,
# MI UP, KS/NE/SD west, ID north and western KY close later than listed.
POLL_CLOSE = {
    "KY": "18:00", "FL": "19:00", "GA": "19:00", "SC": "19:00", "VT": "19:00",
    "VA": "19:00", "NC": "19:30", "OH": "19:30", "WV": "19:30", "AL": "20:00",
    "CT": "20:00", "DE": "20:00", "IL": "20:00", "KS": "20:00", "ME": "20:00",
    "MD": "20:00", "MA": "20:00", "MI": "20:00", "MS": "20:00", "NH": "20:00",
    "NJ": "20:00", "OK": "20:00", "PA": "20:00", "RI": "20:00", "SD": "20:00",
    "TN": "20:00", "TX": "20:00", "AR": "20:30", "AZ": "21:00", "CO": "21:00",
    "IA": "21:00", "LA": "21:00", "MN": "21:00", "NE": "21:00", "NM": "21:00",
    "NY": "21:00", "WI": "21:00", "WY": "21:00", "ID": "22:00", "MT": "22:00",
    "NV": "22:00", "CA": "23:00", "OR": "23:00", "AK": "24:00", "HI": "24:00",
    "IN": "18:00", "MO": "20:00", "ND": "21:00", "UT": "22:00", "WA": "23:00",
}
K_FIXED = ("CONTROLH-2026", "CONTROLS-2026", "KXBALANCEPOWERCOMBO-27FEB",
           "KXDSENATESEATS-27", "KXDHOUSESEATS-27")
PM_FIXED = ("which-party-will-win-the-house-in-2026",
            "which-party-will-win-the-senate-in-2026",
            "balance-of-power-2026-midterms")
# 2026 races whose Kalshi event breaks the SENATE{ST}-26 / GOVPARTY{ST}-26 pattern;
# each one's rules say "pursuant to the ... November 3, 2026" / "2026 election"
K_ALT = {
    ("senate", "LA"): "KXSENATELA-26NOV",
    ("senate", "KY"): "SENATELA-26",  # Kalshi's Kentucky event, despite the LA ticker
    ("senate", "FL"): "SENATEFLS-26",  # class-3 special
    ("senate", "OH"): "SENATEOHS-26",  # class-3 special
    ("governor", "KS"): "GOVPARTYKS-27",
    ("governor", "NH"): "GOVPARTYNH-28",
    ("governor", "AK"): "KXGOVPARTYAK-26",
}
PM_ALT = {("governor", "CA"): ["california-governor-election-2026"]}
# Candidate-only Polymarket markets with no "(D)"/"(R)" in groupItemTitle
PM_CANDIDATE_PARTY = {
    "xavier becerra": "D", "steve hilton": "R",
    "mary peltola": "D", "dan sullivan": "R",
    "jonathan kreiss-tomkins": "D",
}
# Kalshi House districts priced 8-92% R with >=$150k volume at 01:30 ET.
# Kalshi only: Polymarket district markets aren't mapped.
HOUSE_EVENTS = {
    "HOUSETX15-26": "TX-15", "KXHOUSERACE-VA05-26": "VA-05", "KXHOUSERACE-TX23-26": "TX-23",
    "HOUSENY17-26": "NY-17", "KXHOUSERACE-FL25-26": "FL-25", "KXHOUSERACE-NC03-26": "NC-03",
    "HOUSEMT1-26": "MT-01", "HOUSENC1-26": "NC-01", "KXHOUSERACE-NC13-26": "NC-13",
    "KXHOUSERACE-FL14-26": "FL-14", "KXHOUSERACE-NY01-26": "NY-01", "HOUSEPA1-26": "PA-01",
    "KXHOUSERACE-NC09-26": "NC-09", "KXHOUSERACE-AL02-26": "AL-02", "HOUSEVA1-26": "VA-01",
    "HOUSEPA8-26": "PA-08", "KXHOUSETX35-26": "TX-35", "HOUSEPA7-26": "PA-07",
    "KXHOUSERACE-MO02-26": "MO-02", "KXHOUSERACE-MN01-26": "MN-01", "HOUSEME2-26": "ME-02",
    "KXHOUSERACE-OH07-26": "OH-07", "HOUSEVA2-26": "VA-02", "HOUSEWA3-26": "WA-03",
    "KXHOUSERACE-NJ02-26": "NJ-02", "HOUSENY3-26": "NY-03", "HOUSETX28-26": "TX-28",
    "HOUSEAZ1-26": "AZ-01", "KXHOUSERACE-OH10-26": "OH-10", "HOUSEOH9-26": "OH-09",
    "KXHOUSERACE-SC01-26": "SC-01", "KXHOUSERACE-NC14-26": "NC-14", "HOUSEWI3-26": "WI-03",
    "KXHOUSERACE-KY06-26": "KY-06", "KXHOUSERACE-TN09-26": "TN-09", "KXHOUSERACE-NY02-26": "NY-02",
    "KXHOUSERACE-NV02-26": "NV-02", "KXHOUSERACE-NY21-26": "NY-21", "KXHOUSERACE-CO05-26": "CO-05",
    "KXHOUSERACE-CO04-26": "CO-04", "HOUSEMI7-26": "MI-07", "KXHOUSERACE-IN05-26": "IN-05",
    "KXHOUSERACE-FL22-26": "FL-22", "KXHOUSERACE-FL09-26": "FL-09", "KXHOUSERACE-FL26-26": "FL-26",
    "KXHOUSERACE-FL27-26": "FL-27", "KXHOUSERACE-WA05-26": "WA-05", "HOUSEAZ2-26": "AZ-02",
    "KXHOUSERACE-NE01-26": "NE-01", "HOUSEWI1-26": "WI-01", "HOUSEIA3-26": "IA-03",
    "KXHOUSERACE-OK05-26": "OK-05", "HOUSEMI4-26": "MI-04", "KXHOUSERACE-IA02-26": "IA-02",
    "HOUSECO3-26": "CO-03", "KXHOUSERACE-AR02-26": "AR-02", "HOUSETX34-26": "TX-34",
    "KXHOUSERACE-OH15-26": "OH-15", "HOUSEFL13-26": "FL-13",
}
# Kalshi combo suffix is House then Senate (DR = D-House, R-Senate)
BOP_LABEL = {"DD": "D House + D Senate", "DR": "D House + R Senate",
             "RR": "R House + R Senate", "RD": "R House + D Senate"}
SENATE_SEAT_THRESHOLDS = ((51, "P(D≥51)"),)
HOUSE_SEAT_THRESHOLDS = ((218, "P(D≥218)"),)


# ── Fetch ──

def _get(url):
    """GET JSON. Returns (data, error); a 404 is (None, None), i.e. no such event."""
    err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (polyclawd election-day)"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r), None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, None
            err = f"HTTP {e.code}"
        except Exception as e:  # timeouts, resets
            err = type(e).__name__
        time.sleep(1.5 * (attempt + 1))
    return None, err


def kalshi_event(ticker):
    d, err = _get(f"{KALSHI}/events/{ticker}?with_nested_markets=true")
    if not d:
        return [], err
    return d.get("markets") or (d.get("event") or {}).get("markets") or [], None


def pm_event(slugs):
    """First slug that exists wins; Polymarket slug patterns differ by state."""
    err = None
    for slug in slugs:
        d, e = _get(f"{GAMMA}/events?slug={slug}")
        if d:
            return d[0].get("markets") or [], None
        err = e or err
    return [], err


def collect():
    """Fetch everything in parallel. Returns ({(venue, key): markets}, errors, missing)."""
    plan = {("K", t): (kalshi_event, t) for t in K_FIXED}
    plan.update({("PM", s): (pm_event, [s]) for s in PM_FIXED})
    for kind, states, k_fmt, pm_fmts in (
        ("senate", SENATE, "SENATE{}-26",
         ("{}-senate-election-winner", "{}-senate-election-winner-2026")),
        ("governor", GOVERNOR, "GOVPARTY{}-26",
         ("{}-governor-winner-2026", "{}-governor-election-winner")),
    ):
        for st in states:
            plan[("K", f"{kind}|{st}")] = (kalshi_event, K_ALT.get((kind, st), k_fmt.format(st)))
            plan[("PM", f"{kind}|{st}")] = (pm_event, PM_ALT.get((kind, st))
                                             or [f.format(STATE_NAMES[st]) for f in pm_fmts])
    for ticker, dist in HOUSE_EVENTS.items():
        plan[("K", f"house|{dist}")] = (kalshi_event, ticker)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {key: ex.submit(fn, arg) for key, (fn, arg) in plan.items()}
        res = {key: f.result() for key, f in futs.items()}
    data, errors, missing = {}, [], []
    for key, (markets, err) in res.items():
        # Kalshi tickers don't reliably encode the state (SENATELA-26 is Kentucky),
        # so the rules text must name the state the event is filed under
        kind, _, st = key[1].partition("|")
        if key[0] == "K" and markets and kind in ("senate", "governor"):
            rules = (markets[0].get("rules_primary") or "").lower()
            if STATE_NAMES[st].replace("-", " ") not in rules:
                errors.append(f"K {key[1]} (event is for another state)")
                data[key] = []
                continue
        data[key] = markets
        if err:
            errors.append(f"{key[0]} {key[1]} ({err})")
        elif not markets:
            missing.append(f"{key[0]} {key[1]}")
    return data, errors, missing


# ── Quotes ──

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def k_quote(m):
    """YES quote for a Kalshi market."""
    vol = _f(m.get("volume_fp")) or _f(m.get("volume")) or 0.0
    res = (m.get("result") or "").lower()
    if res in ("yes", "no"):
        return {"p": 1.0 if res == "yes" else 0.0, "src": "settled", "vol": vol, "final": True}
    bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
    if bid is not None and ask and ask - bid <= WIDE_BOOK:
        return {"p": (bid + ask) / 2, "src": "mid", "vol": vol, "final": False}
    return {"p": _f(m.get("last_price_dollars")), "src": "last", "vol": vol, "final": False}


def pm_quote(m):
    """YES quote for a Polymarket market."""
    vol = _f(m.get("volume")) or 0.0
    try:
        names = [n.lower() for n in json.loads(m.get("outcomes") or "[]")]
        yes = float(json.loads(m.get("outcomePrices") or "[]")[names.index("yes")])
    except (ValueError, TypeError, IndexError):
        yes = None
    if m.get("closed"):
        return {"p": yes, "src": "settled", "vol": vol, "final": yes in (0.0, 1.0)}
    bid, ask = _f(m.get("bestBid")), _f(m.get("bestAsk"))
    if bid is not None and ask and ask - bid <= WIDE_BOOK:
        return {"p": (bid + ask) / 2, "src": "mid", "vol": vol, "final": False}
    return {"p": yes, "src": "last", "vol": vol, "final": False}


def k_sides(markets):
    """party -> quote for a Kalshi party-win event. Ticker suffix -R/-D is the
    party; any other suffix is a named independent, best one kept as "I"."""
    sides = {}
    for m in markets:
        q = k_quote(m)
        if q["p"] is None:
            continue
        q["name"] = m.get("yes_sub_title") or ""
        suf = (m.get("ticker") or "").rsplit("-", 1)[-1]
        party = suf if suf in ("R", "D") else "I"
        if party not in sides or q["p"] > sides[party]["p"]:
            sides[party] = q
    return sides


def pm_party(m):
    q = (m.get("question") or "").lower()
    if re.search(r"\bthe democrats?\b|democratic party", q):
        return "D"
    if re.search(r"\bthe republicans?\b|republican party", q):
        return "R"
    if "an independent" in q:
        return "I"
    gi = m.get("groupItemTitle") or ""
    g = re.search(r"\(([DRI])\)", gi)
    if g:
        return g.group(1)
    return PM_CANDIDATE_PARTY.get(re.sub(r"^(sen|gov|rep)\.\s+", "", gi.strip().lower()))


def pm_sides(markets):
    """party -> quote for a Polymarket event; candidate markets map through
    groupItemTitle "(D)"/"(R)". Max per party, so a party market and its
    candidate market are never added together."""
    sides = {}
    for m in markets:
        party = pm_party(m)
        if not party:
            if DEBUG:
                print(f"[pm] unmapped: {m.get('question')} | {m.get('groupItemTitle')}")
            continue
        q = pm_quote(m)
        if q["p"] is None:
            continue
        q["name"] = re.sub(r"\s*\([DRI]\)", "", m.get("groupItemTitle") or "")
        if party not in sides or q["p"] > sides[party]["p"]:
            sides[party] = q
    return sides


def _r_of(sides):
    if "R" in sides:
        return sides["R"]["p"]
    if "D" in sides:
        return 1 - sides["D"]["p"]
    return None


def _d_of(sides):
    if "D" in sides:
        return sides["D"]["p"]
    if "R" in sides:
        return 1 - sides["R"]["p"]
    return None


# ── Races ──

def _short(name):
    if not name or "party" in name.lower() or name.lower() in ("yes", "no"):
        return ""
    return name.split()[-1]


def build_race(kind, st, k, p):
    venues = [s for s in (k, p) if s]
    if not venues:
        return None
    avg = {pt: _mean([s[pt]["p"] for s in venues if pt in s])
           for pt in {pt for s in venues for pt in s}}
    leader = max(avg, key=avg.get)
    called = [(pt, vname) for vname, s in (("Kalshi", k), ("Polymarket", p))
              for pt, q in s.items() if q["final"] and q["p"] == 1.0]
    gap = None
    for pt in set(k) & set(p):
        g = abs(k[pt]["p"] - p[pt]["p"])
        if gap is None or g > gap[0]:
            gap = (g, pt)
    name = next((s[leader]["name"] for s in (k, p) if leader in s and _short(s[leader]["name"])), "")
    return {"key": f"{kind}|{st}", "kind": kind, "st": st, "k": k, "p": p,
            "leader": leader, "lead_p": avg[leader], "name": _short(name),
            "cons_r": _mean([_r_of(s) for s in venues]), "called": called, "gap": gap}


def _bucket(title):
    """Seat range (lo, hi) from a Kalshi seat-count market title."""
    t = title.lower().replace(",", "")
    t = re.sub(r"\b1\d\dth congress\b|\b20\d\d\b", "", t)
    m = re.search(r"(\d+)\s*-\s*(\d+)", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(?:above|more than|over)\s+(\d+)", t)
    if m:
        return int(m.group(1)) + 1, 999
    m = re.search(r"(?:below|less than|fewer than|under)\s+(\d+)", t)
    if m:
        return 0, int(m.group(1)) - 1
    m = re.search(r"(\d+)\s+(?:house |senate )?seats", t)
    if m:
        return int(m.group(1)), int(m.group(1))
    return None


def seat_line(label, markets, thresholds):
    dist = []
    for m in markets:
        b, q = _bucket(m.get("title") or ""), k_quote(m)
        if DEBUG:
            print(f"[seat] {m.get('ticker')} | {m.get('title')} -> {b} p={q['p']}")
        if b and q["p"] is not None:
            dist.append((b[0], b[1], q["p"]))
    if DEBUG and markets:
        print(f"[seat] rules: {(markets[0].get('rules_primary') or '')[:400]}")
    # Bucket mids carry the overround (House summed to 108.6 at 01:21 ET), so
    # every probability is normalized to the bucket total
    total = sum(d[2] for d in dist)
    if not dist or total <= 0:
        return f"{label}: no data"
    parts = []
    for n, name in thresholds:
        if any(lo < n <= hi for lo, hi, _ in dist):
            parts.append(f"{name} n/a (a bucket straddles {n})")
        else:
            parts.append(f"{name} {fmt(sum(p for lo, _, p in dist if lo >= n) / total)}")
    lo, hi, p = max(dist, key=lambda d: d[2])
    mode = str(lo) if lo == hi else f"{lo}+" if hi >= 999 else f"≤{hi}" if lo == 0 else f"{lo}-{hi}"
    parts.append(f"modal {mode} ({fmt(p / total)})")
    parts.append(f"raw sum {fmt(total)}")
    return f"{label}: " + " · ".join(parts)


# ── Render ──

def fmt(p):
    return "—" if p is None else f"{100 * p:.1f}"


def _deltas(cur, key, prev, first, sign):
    parts = []
    for tag, base in (("Δ", prev), ("day ", first)):
        if cur is not None and key in base and abs(cur - base[key]) >= 0.0005:
            parts.append(f"{tag}{100 * sign * (cur - base[key]):+.1f}")
    return ("  " + " · ".join(parts)) if parts else ""


def _close_dt(st):
    h, m = map(int, POLL_CLOSE[st[:2]].split(":"))
    return ELECTION_DAY + timedelta(hours=h, minutes=m)


def _close_label(st):
    h, m = map(int, POLL_CLOSE[st[:2]].split(":"))
    return f"{h - 12}:{m:02d}p" if h < 24 else f"12:{m:02d}a"


def _et_label(ts):
    return datetime.fromisoformat(ts).astimezone(ET).strftime("%-I:%M %p")


def _key_label(key):
    kind, what = key.split("|")
    if kind == "control":
        return f"{what.title()} control (Dem)"
    return f"{what} {({'senate': 'Senate', 'governor': 'Gov', 'house': 'House'})[kind]} (R)"


def race_line(r, prev, first, now):
    st, ldr = r["st"], r["leader"]
    lock = "🔒" if now >= _close_dt(st) else ""
    kv = fmt(r["k"][ldr]["p"]) if ldr in r["k"] else "—"
    pv = fmt(r["p"][ldr]["p"]) if ldr in r["p"] else "—"
    who = f"{ldr} {r['name']}".strip()
    sign = 1 if ldr == "R" else -1
    prices = kv if r["kind"] == "house" else f"{kv} | {pv}"
    return f"{st} {_close_label(st)}{lock}  {who} {prices}{_deltas(r['cons_r'], r['key'], prev, first, sign)}"


def render(data, state, now, errors, missing):
    prev, first = state.get("prev", {}), state.get("first", {})
    snap, flags = {}, []
    L = [f"🗳 2026 MIDTERMS — Kalshi | Polymarket",
         f"{now.astimezone(ET):%a %b %-d, %-I:%M %p} ET"
         + (f" · run #{state.get('runs', 0) + 1} · Δ vs {_et_label(state['prev_ts'])},"
            f" day vs {_et_label(state['first_ts'])}" if state.get("prev_ts") else " · first run, no deltas yet"),
         "Win % from bid/ask mids.", ""]

    L.append("CONTROL (Dem win %, K | PM)")
    for label, kt, ps in (("House", "CONTROLH-2026", "which-party-will-win-the-house-in-2026"),
                          ("Senate", "CONTROLS-2026", "which-party-will-win-the-senate-in-2026")):
        kd, pd = _d_of(k_sides(data[("K", kt)])), _d_of(pm_sides(data[("PM", ps)]))
        key, cons = f"control|{label.lower()}", _mean([kd, pd])
        if cons is not None:
            snap[key] = cons
        L.append(f"{label}: {fmt(kd)} | {fmt(pd)}{_deltas(cons, key, prev, first, 1)}")
        if kd is not None and pd is not None and abs(kd - pd) >= GAP_FLAG:
            flags.append(f"{label} control: Dem {fmt(kd)} K vs {fmt(pd)} PM ({100 * abs(kd - pd):.1f} pts)")

    bop_k, bop_p = {}, {}
    for m in data[("K", "KXBALANCEPOWERCOMBO-27FEB")]:
        suf = (m.get("ticker") or "").rsplit("-", 1)[-1]
        if DEBUG:
            print(f"[bop] {m.get('ticker')} | {m.get('yes_sub_title')}")
        if suf in BOP_LABEL:
            bop_k[suf] = k_quote(m)["p"]
    for m in data[("PM", "balance-of-power-2026-midterms")]:
        g = re.search(r"\b([DR]) Senate,\s*([DR]) House", m.get("question") or "")
        if g:
            bop_p[g.group(2) + g.group(1)] = pm_quote(m)["p"]
    L.append("Balance of power:")
    for suf, label in BOP_LABEL.items():
        if suf in bop_k or suf in bop_p:
            L.append(f"{label}: {fmt(bop_k.get(suf))} | {fmt(bop_p.get(suf))}")
    L.append("Seats (Kalshi buckets, %):")
    L.append(seat_line("Senate", data[("K", "KXDSENATESEATS-27")], SENATE_SEAT_THRESHOLDS))
    L.append(seat_line("House", data[("K", "KXDHOUSESEATS-27")], HOUSE_SEAT_THRESHOLDS))

    races = {}
    for kind, states in (("senate", SENATE), ("governor", GOVERNOR),
                         ("house", list(HOUSE_EVENTS.values()))):
        for st in states:
            r = build_race(kind, st, k_sides(data.get(("K", f"{kind}|{st}"), [])),
                           pm_sides(data.get(("PM", f"{kind}|{st}"), [])))
            if not r:
                continue
            races[r["key"]] = r
            if r["cons_r"] is not None:
                snap[r["key"]] = r["cons_r"]
            if r["gap"] and r["gap"][0] >= GAP_FLAG and not r["called"]:
                g, pt = r["gap"]
                kq, pq = r["k"][pt], r["p"][pt]
                thin = " — thin book, last trade" if "last" in (kq["src"], pq["src"]) else ""
                flags.append(f"{st} {kind}: {pt} {fmt(kq['p'])} K vs {fmt(pq['p'])} PM "
                             f"({100 * g:.1f} pts; PM vol ${pq['vol'] / 1e3:,.0f}k){thin}")

    called = [r for r in races.values() if r["called"]]
    if flags:
        L += ["", "⚠️ CROSS-VENUE GAPS ≥{:.0f} pts".format(100 * GAP_FLAG)] + [f"• {f}" for f in flags]
    if called:
        L += ["", "✅ RESOLVED (market settled)"]
        for r in sorted(called, key=lambda r: (r["kind"], r["st"])):
            who = ", ".join(f"{pt} on {v}" for pt, v in r["called"])
            L.append(f"• {r['st']} {r['kind']}: {who}")

    moves = sorted(((abs(v - prev[k]), k) for k, v in snap.items() if k in prev), reverse=True)
    moves = [k for d, k in moves if d >= 0.01][:8]
    if moves:
        L += ["", "📈 MOVERS since last run"]
        L += [f"• {_key_label(k)} {fmt(prev[k])} → {fmt(snap[k])} ({100 * (snap[k] - prev[k]):+.1f})" for k in moves]

    for kind, title in (("senate", "SENATE"), ("governor", "GOVERNOR"), ("house", "HOUSE")):
        rs = [r for r in races.values() if r["kind"] == kind]
        comp = sorted((r for r in rs if not r["called"] and r["lead_p"] < SAFE),
                      key=lambda r: (POLL_CLOSE[r["st"][:2]], r["st"]))
        if kind == "house":
            lead = {pt: sum(r["leader"] == pt for r in rs) for pt in ("D", "R")}
            L += ["", f"HOUSE — {len(rs)} key districts, Kalshi only (leader win %; 🔒 = closed)",
                  f"Leading: D {lead['D']} · R {lead['R']}"]
        else:
            L += ["", f"{title} — competitive (leader win %, K | PM; time = main poll close ET; 🔒 = closed)"]
        L += [race_line(r, prev, first, now) for r in comp] or ["(none)"]
        for party in ("R", "D"):
            safe = sorted(r["st"] for r in rs if not r["called"] and r["lead_p"] >= SAFE and r["leader"] == party)
            if safe:
                L.append(f"Safe {party} (≥{SAFE:.0%}): {' '.join(safe)}")

    L += ["", f"Data: {len(data) - len(errors) - len(missing)}/{len(data)} events fetched"]
    if errors:
        L.append("Failed: " + ", ".join(errors[:12]) + (f" +{len(errors) - 12} more" if len(errors) > 12 else ""))
    if missing:
        L.append("No market: " + ", ".join(missing[:15]) + (f" +{len(missing) - 15} more" if len(missing) > 15 else ""))
    return "\n".join(L), snap


# ── State ──

def _load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state, snap, now):
    first = state.get("first", {})
    for k, v in snap.items():
        first.setdefault(k, v)
    state.update(first=first, first_ts=state.get("first_ts") or now.isoformat(),
                 prev=snap, prev_ts=now.isoformat(), runs=state.get("runs", 0) + 1)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)


def main():
    global DEBUG
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--send", action="store_true", help="post to Telegram")
    ap.add_argument("--force", action="store_true", help="ignore the election-day window")
    ap.add_argument("--no-state", action="store_true", help="leave the delta baseline untouched")
    ap.add_argument("--debug", action="store_true", help="print parse details")
    args = ap.parse_args()
    DEBUG = args.debug

    now = datetime.now(timezone.utc)
    if not args.force and not WINDOW[0] <= now <= WINDOW[1]:
        print(f"[skip] {now:%Y-%m-%d %H:%M} UTC is outside the election-day window")
        return

    data, errors, missing = collect()
    state = _load_state()
    text, snap = render(data, state, now, errors, missing)
    print(text)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"election_day_{now:%Y%m%d_%H%M}.txt").write_text(text)
    if not args.no_state:
        _save_state(state, snap, now)
    if args.send:
        from scripts.openclaw_alerts import alert_openclaw
        # parse_mode=None: plain text, the body has unbalanced '_' / '*'
        print(f"[send] telegram ok={alert_openclaw(text, parse_mode=None)}")


if __name__ == "__main__":
    main()
