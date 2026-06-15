#!/usr/bin/env python3
"""
scorer_resolution_fetch.py — LIVE fetch glue for goalscorer resolution.

Bridges the network world to the pure logic in scorer_resolution.py:
  - fetch_live_event_odds(sport)  → goalscorer event-odds for the PAPER pipeline scan
  - make_resolver(...)            → a resolver_fn(position_dict)->ResolveState that
                                     fetches FINAL event sets (ESPN free + API-Football
                                     optional) and calls resolve_scorer().

Two-source rule (design note): with an API-Football key present we grade
API-Football AND ESPN and require agreement; WITHOUT a key we cannot satisfy
two-source, so we return PENDING rather than settle on one source. Network only
fires here — never inside the pure resolver or in tests (which inject).
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import timedelta

from signals import scorer_resolution as sr

_UA = {"User-Agent": "Polyclawd/1.0"}
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
AF_BASE = "https://v3.football.api-sports.io"

SHARP = "betfair_ex_uk,betfair_ex_eu,pinnacle,williamhill"
SOFT = "draftkings,fanduel,betrivers,onexbet,skybet"


def _get_json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={**_UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ── live odds fetch for the PAPER pipeline scan ──────────────────────────────────
def fetch_live_event_odds(sport="soccer_fifa_world_cup", within_hours=6.0):
    """Goalscorer event-odds for upcoming, not-yet-started matches. Needs ODDS_API_KEY."""
    key = os.getenv("ODDS_API_KEY")
    if not key:
        raise RuntimeError("ODDS_API_KEY not set — cannot fetch live odds.")
    books = f"{SHARP},{SOFT}"
    events = _get_json(f"{ODDS_API_BASE}/sports/{sport}/events?apiKey={key}")
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    out = []
    for ev in events:
        ct = ev.get("commence_time")
        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except Exception:
            continue
        hrs = (dt - now).total_seconds() / 3600.0
        if hrs <= 0 or hrs > within_hours:
            continue
        url = (
            f"{ODDS_API_BASE}/sports/{sport}/events/{ev['id']}/odds?apiKey={key}"
            f"&regions=us,uk,eu&markets=player_goal_scorer_anytime&oddsFormat=american&bookmakers={books}"
        )
        try:
            od = _get_json(url)
            if od.get("bookmakers"):
                out.append(od)
        except Exception:
            continue
    return out


# ── ESPN final event set ─────────────────────────────────────────────────────────
def _split_title(title):
    parts = title.split(" vs ")
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (title, "")


def _espn_final(league_slug, event_title, commence_iso):
    """Return (key_events, completed). Find the ESPN event by date+teams, then summary."""
    home, away = _split_title(event_title)
    try:
        d = commence_iso[:10].replace("-", "")
    except Exception:
        return None, False
    try:
        sb = _get_json(f"{ESPN_BASE}/{league_slug}/scoreboard?dates={d}")
    except Exception:
        return None, False
    eid = None
    for e in sb.get("events", []):
        comps = (e.get("competitions") or [{}])[0].get("competitors", [])
        names = [c.get("team", {}).get("displayName", "") for c in comps]
        if any(sr.name_match(n, home) for n in names) and any(sr.name_match(n, away) for n in names):
            eid = e.get("id")
            break
    if not eid:
        return None, False
    summ = _get_json(f"{ESPN_BASE}/{league_slug}/summary?event={eid}")
    completed = (
        (summ.get("header", {}).get("competitions") or [{}])[0].get("status", {}).get("type", {}).get("completed")
    ) is True
    return summ.get("keyEvents", []), completed


# ── API-Football final event set (optional) ──────────────────────────────────────
def _af_final(af_key, event_title, commence_iso):
    """Return (af_events, final). Requires a key; otherwise (None, False)."""
    if not af_key:
        return None, False
    home, away = _split_title(event_title)
    d = commence_iso[:10]
    try:
        fixtures = _get_json(f"{AF_BASE}/fixtures?date={d}", headers={"x-apisports-key": af_key}).get("response", [])
    except Exception:
        return None, False
    fid = None
    for f in fixtures:
        t = f.get("teams", {})
        hn, an = t.get("home", {}).get("name", ""), t.get("away", {}).get("name", "")
        if sr.name_match(hn, home) and sr.name_match(an, away):
            if f.get("fixture", {}).get("status", {}).get("short") in sr.AF_FINAL_STATUSES:
                fid = f.get("fixture", {}).get("id")
            break
    if not fid:
        return None, False
    try:
        evs = _get_json(f"{AF_BASE}/fixtures/events?fixture={fid}", headers={"x-apisports-key": af_key}).get(
            "response", []
        )
    except Exception:
        return None, False
    return evs, True


def make_resolver(league_slug="fifa.world", af_key=None):
    """Build a resolver_fn(position_dict)->ResolveState. Two-source when af_key present;
    otherwise PENDING (cannot satisfy the two-source rule on ESPN alone)."""

    def _resolver(pos):
        title, player, ct = pos["event_title"], pos["player"], pos.get("commence_time", "")
        espn_events, espn_done = _espn_final(league_slug, title, ct)
        af_events, af_done = _af_final(af_key, title, ct)
        if af_key is None:
            return sr.ResolveState.PENDING  # no second source → cannot confirm
        return sr.resolve_scorer(player, af_events or [], espn_events or [], af_done, espn_done)

    return _resolver
