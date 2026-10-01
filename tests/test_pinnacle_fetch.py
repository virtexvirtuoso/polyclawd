"""Offline tests for the Pinnacle guest-API sharp-line adapter (odds/pinnacle_fetch.py).

Fixtures are REAL payloads captured 2026-09-30 from guest.api.arcadia.pinnacle.com:
- pinnacle_matchups_props.json  — full MLB league matchups incl. 130 prop specials (BOS@NYY slate)
- pinnacle_matchups.json        — fresh upcoming slate (2 games, futures/series specials only)
- pinnacle_straight.json        — bulk straight-market prices for the fresh slate

The straight rows for prop specials are synthesized in-test from REAL participant ids
in the matchups fixture, using the exact price shape captured live
(prices: [{participantId, points, price}], American odds ints).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from odds import mlb_props
from odds import pinnacle_fetch

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str):
    with open(FIXTURES / name) as f:
        return json.load(f)


def _special_by_desc(matchups, needle: str):
    for m in matchups:
        if m.get("type") == "special" and needle.lower() in ((m.get("special") or {}).get("description", "")).lower():
            return m
    raise AssertionError(f"special not found: {needle}")


def _straight_over_under(special, over_price: int, under_price: int, points: float = 0.5):
    """Synthesize a straight-market row in the exact live shape for one special."""
    parts = {p["name"]: p["id"] for p in special.get("participants", [])}
    assert set(parts) == {"Over", "Under"}, f"unexpected participants: {parts}"
    return {
        "cutoffAt": "2026-10-01T00:00:00+00:00",
        "key": "s;0;ou",
        "limits": [{"amount": 1000, "type": "maxRiskStake"}],
        "matchupId": special["id"],
        "period": 0,
        "prices": [
            {"participantId": parts["Over"], "points": points, "price": over_price},
            {"participantId": parts["Under"], "points": points, "price": under_price},
        ],
        "type": "total",
    }


# ── Task 2a: description parser ────────────────────────────────────────────────

def test_parse_player_props():
    parse = pinnacle_fetch.parse_special_description
    assert parse("Ben Rice Total Home Runs") == ("Ben Rice", "batter_home_runs")
    assert parse("Jazz Chisholm Jr. Total Bases") == ("Jazz Chisholm Jr.", "batter_total_bases")
    assert parse("Sonny Gray Total Strikeouts") == ("Sonny Gray", "pitcher_strikeouts")
    assert parse("Max Fried Total Earned Runs") == ("Max Fried", "pitcher_earned_runs")
    assert parse("Max Fried Total Hits Allowed") == ("Max Fried", "pitcher_hits_allowed")
    assert parse("Max Fried Total Pitching Outs") == ("Max Fried", "pitcher_outs")


def test_parse_rejects_team_and_future_specials():
    parse = pinnacle_fetch.parse_special_description
    # none of these may map to a player-prop market key
    for desc in (
        "2026 American League Pennant Winner",
        "MLB Best of 3 Series Winner",
        "Team To Score 1st Run",
        "New York Yankees Exact Total Runs",
        "Boston Red Sox Runs Odd/Even",
        "Moneyline and Total Runs",
        "Total Runs Range",
        "Winning Margin",
        "Correct Score",
        "New York Yankees Total Runs",
    ):
        assert parse(desc) == (None, None), desc


# ── Task 2b: implied-probability math (shared with mlb_props) ─────────────────

def test_american_to_ip():
    assert mlb_props._american_to_ip(-118) == 54.1
    assert mlb_props._american_to_ip(105) == 48.8
    assert mlb_props._american_to_ip(331) == 23.2
    assert mlb_props._american_to_ip(-440) == 81.5


# ── Task 2c: payload builder on real fixture ──────────────────────────────────

def test_build_props_payload_contract():
    matchups = _load("pinnacle_matchups_props.json")
    rice = _special_by_desc(matchups, "Ben Rice Total Home Runs")
    fried_k = _special_by_desc(matchups, "Max Fried Total Strikeouts")
    # real game id from the fixture (BOS@NYY, Oct 1 00:10Z)
    game_id = (rice.get("parent") or {}).get("id")
    assert game_id

    straight = [
        _straight_over_under(rice, over_price=331, under_price=-440),      # 23.2% / 81.5%
        _straight_over_under(fried_k, over_price=-120, under_price=100),
        # game-level rows exist but must be ignored by the props builder
        {"key": "s;0;m", "matchupId": game_id, "period": 0,
         "prices": [{"designation": "home", "price": -127}, {"designation": "away", "price": 117}]},
    ]

    payload = pinnacle_fetch.build_props_payload(
        matchups, straight, probable_pitchers={"york": "Max Fried"},
        now="2026-09-30T23:00:00+00:00",
    )

    assert payload["source"] == "pinnacle_guest_api"
    assert payload["credit_remaining"] is None
    assert isinstance(payload["games"], list)

    # only the NYY game has accepted player props (PHI@ATL specials are team-level)
    assert len(payload["games"]) == 1
    game = payload["games"][0]
    assert game["home_team"] == "New York Yankees"
    assert game["away_team"] == "Boston Red Sox"
    assert game["home_pitcher"] == "Max Fried"
    assert game["commence_time"].startswith("2026-10-01T00:10")

    hr_rows = game["props"]["batter_home_runs"]
    assert len(hr_rows) == 1
    row = hr_rows[0]
    assert row["player"] == "Ben Rice"
    assert row["book"] == "Pinnacle"
    assert row["line"] == 0.5
    assert row["over_odds"] == "+331"
    assert row["over_ip"] == 23.2
    assert row["under_odds"] == "-440"
    assert row["under_ip"] == 81.5

    k_rows = game["props"]["pitcher_strikeouts"]
    assert len(k_rows) == 1
    assert k_rows[0]["player"] == "Max Fried"


def test_build_props_payload_window_filter():
    """Games starting outside the 30h window are dropped."""
    matchups = _load("pinnacle_matchups_props.json")
    rice = _special_by_desc(matchups, "Ben Rice Total Home Runs")
    straight = [_straight_over_under(rice, 331, -440)]
    payload = pinnacle_fetch.build_props_payload(
        matchups, straight, {}, now="2026-10-05T00:00:00+00:00",
    )
    assert payload["games"] == []


# ── Task 2d: robustness ───────────────────────────────────────────────────────

def test_empty_and_garbage_inputs_never_raise():
    payload = pinnacle_fetch.build_props_payload([], [], {})
    assert payload["games"] == []
    assert payload["source"] == "pinnacle_guest_api"
    # garbage rows must be skipped, not raise
    payload = pinnacle_fetch.build_props_payload([1, "x", None], [{"bad": "row"}], {})
    assert payload["games"] == []


def test_get_pinnacle_props_never_raises(monkeypatch):
    monkeypatch.setattr(pinnacle_fetch, "_get", lambda url: None)  # total fetch failure
    payload = pinnacle_fetch.get_pinnacle_props(force=True)
    assert payload["games"] == []
    assert "note" in payload


# ── Task 2e: seam wiring + kill switch ────────────────────────────────────────

def _reset_mlb_props_cache():
    mlb_props._CACHE["data"] = None
    mlb_props._CACHE["ts"] = 0.0


def test_default_source_is_pinnacle(monkeypatch):
    import asyncio

    monkeypatch.delenv("POLYCLAWD_PROP_SOURCE", raising=False)
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    _reset_mlb_props_cache()
    calls = []

    def fake_adapter(force=False):
        calls.append(force)
        return {"source": "pinnacle_guest_api", "timestamp": "t", "credit_remaining": None,
                "games": [{"away_team": "A", "home_team": "B", "props": {"batter_home_runs": [{"player": "P"}]}}]}

    monkeypatch.setattr(pinnacle_fetch, "get_pinnacle_props", fake_adapter)
    payload = asyncio.run(mlb_props.get_mlb_props(force=True))
    assert calls == [True]
    assert payload["source"] == "pinnacle_guest_api"


def test_kill_switch_reverts_to_odds_api(monkeypatch):
    import asyncio

    monkeypatch.setenv("POLYCLAWD_PROP_SOURCE", "odds_api")
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    _reset_mlb_props_cache()

    def forbidden(force=False):
        raise AssertionError("adapter must not be called when POLYCLAWD_PROP_SOURCE=odds_api")

    monkeypatch.setattr(pinnacle_fetch, "get_pinnacle_props", forbidden)
    monkeypatch.setattr(mlb_props, "get_credit_status", lambda: {"remaining": None})
    try:
        monkeypatch.setattr("odds.the_odds_api.refresh_credit_balance", lambda: {"remaining": None})
    except AttributeError:
        pass  # stub fallback in mlb_props — inner import will fail into its own except
    payload = asyncio.run(mlb_props.get_mlb_props(force=True))
    assert payload["source"] == "the_odds_api_mlb_props"
    assert payload["games"] == []  # no key → empty, but no crash


def test_pinnacle_failure_falls_back(monkeypatch):
    import asyncio

    monkeypatch.delenv("POLYCLAWD_PROP_SOURCE", raising=False)
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    _reset_mlb_props_cache()

    def broken(force=False):
        raise RuntimeError("pinnacle down")

    monkeypatch.setattr(pinnacle_fetch, "get_pinnacle_props", broken)
    monkeypatch.setattr(mlb_props, "get_credit_status", lambda: {"remaining": None})
    try:
        monkeypatch.setattr("odds.the_odds_api.refresh_credit_balance", lambda: {"remaining": None})
    except AttributeError:
        pass
    payload = asyncio.run(mlb_props.get_mlb_props(force=True))
    assert payload["source"] == "the_odds_api_mlb_props"  # fell through, no raise