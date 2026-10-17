#!/usr/bin/env python3
"""Tests for the ESPN/DK CLV odds backend (odds/espn_odds.py additions).

Odds API key 51ef… was permanently deactivated 2026-10-17; the game-level CLV
lane now reads DraftKings prices off ESPN's free scoreboard API, reshaped into
the same event/odds JSON shapes the loggers already parse.

Pure fixture tests — NO live HTTP. Fixtures: tests/fixtures/espn_odds/*.json
are trimmed real ESPN scoreboard payloads (recorded 2026-10-17).

Run: venv/bin/python -m pytest tests/test_espn_dk_clv_source.py -q --noconftest
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "espn_odds"


def _fixture(name):
    return json.load(open(FIXTURES / name))


@pytest.fixture
def espn():
    """Fresh espn_odds module with cleared caches per test."""
    import odds.espn_odds as m
    m._scoreboard_cache.clear()
    m._backoff_until.clear()
    yield m
    m._scoreboard_cache.clear()
    m._backoff_until.clear()


# ── shape adapter: _espn_event_to_oddsapi ────────────────────────────────────
class TestShapeAdapter:
    def test_nfl_event_shape(self, espn):
        data = _fixture("nfl_scoreboard.json")
        ev = next(e for e in data["events"]
                  if any(o for o in e["competitions"][0].get("odds", []) if o))
        shaped = espn._espn_event_to_oddsapi(ev, "Jacksonville Jaguars", "Houston Texans")
        assert shaped["id"] == ev["id"]
        assert shaped["home_team"] == "Jacksonville Jaguars"
        assert shaped["away_team"] == "Houston Texans"
        assert shaped["commence_time"] == ev["date"]
        bks = shaped["bookmakers"]
        assert len(bks) == 1 and bks[0]["key"] == "draftkings"
        keys = {m["key"] for m in bks[0]["markets"]}
        assert keys == {"h2h", "totals"}
        h2h = next(m for m in bks[0]["markets"] if m["key"] == "h2h")
        names = {o["name"]: o["price"] for o in h2h["outcomes"]}
        assert names["Jacksonville Jaguars"] == -148
        assert names["Houston Texans"] == 124
        tot = next(m for m in bks[0]["markets"] if m["key"] == "totals")
        outs = {o["name"]: o for o in tot["outcomes"]}
        assert outs["Over"]["price"] == -110 and outs["Under"]["price"] == -110
        assert outs["Over"]["point"] == 42.5

    def test_soccer_three_way_includes_draw(self, espn):
        data = _fixture("soccer_eng1_scoreboard.json")
        ev = next(e for e in data["events"]
                  if any(o for o in e["competitions"][0].get("odds", []) if o))
        shaped = espn._espn_event_to_oddsapi(ev, "Brentford", "Liverpool")
        h2h = next(m for m in shaped["bookmakers"][0]["markets"] if m["key"] == "h2h")
        names = {o["name"]: o["price"] for o in h2h["outcomes"]}
        assert names == {"Brentford": 175, "Liverpool": 150, "Draw": 260}

    def test_unpriced_event_returns_empty(self, espn):
        data = _fixture("nfl_scoreboard.json")
        ev = next(e for e in data["events"]
                  if not any(o for o in e["competitions"][0].get("odds", []) if o))
        assert espn._espn_event_to_oddsapi(ev, "Denver Broncos", "Seattle Seahawks") == {}

    def test_totals_without_prices_skipped(self, espn):
        ev = {"id": "1", "date": "2026-10-18T13:30Z",
              "competitions": [{"competitors": [], "odds": [{
                  "provider": {"name": "Draft Kings"}, "overUnder": 42.5,
                  "moneyline": {"home": {"close": {"odds": "-148"}},
                                 "away": {"close": {"odds": "+124"}}},
                  "total": {},
              }]}]}
        shaped = espn._espn_event_to_oddsapi(ev, "A", "B")
        keys = {m["key"] for m in shaped["bookmakers"][0]["markets"]}
        assert keys == {"h2h"}  # no prices → no totals market


# ── event discovery: fetch_espn_events ───────────────────────────────────────
class TestFetchEvents:
    def test_lists_upcoming_with_window(self, espn):
        with mock.patch.object(espn, "_espn_scoreboard",
                               return_value=_fixture("nfl_scoreboard.json")):
            now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
            evs = espn.fetch_espn_events("americanfootball_nfl", within_hours=48, now=now)
        assert evs, "fixture slate should produce events"
        ids = [e["id"] for e in evs]
        assert "401872995" not in ids  # already kicked off (2026-10-16) — excluded
        assert "401872996" in ids
        # shape matches The Odds API /events
        ev = next(e for e in evs if e["id"] == "401872996")
        assert set(ev) >= {"id", "commence_time", "home_team", "away_team"}
        assert ev["home_team"] == "Jacksonville Jaguars"
        assert ev["commence_time"] == "2026-10-18T13:30:00Z"
        # sorted soonest-first
        assert evs == sorted(evs, key=lambda e: e["commence_time"])

    def test_empty_window_returns_empty(self, espn):
        with mock.patch.object(espn, "_espn_scoreboard",
                               return_value=_fixture("nfl_scoreboard.json")):
            now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
            assert espn.fetch_espn_events("americanfootball_nfl", within_hours=0.5, now=now) == []


# ── per-event odds: fetch_event_odds ─────────────────────────────────────────
class TestFetchEventOdds:
    def test_known_event_returns_oddsapi_shape(self, espn):
        with mock.patch.object(espn, "_espn_scoreboard",
                               return_value=_fixture("mlb_scoreboard.json")):
            od = espn.fetch_event_odds("baseball_mlb", "401908027")
        assert od is not None
        assert od["home_team"] == "Cleveland Guardians"
        assert od["away_team"] == "Tampa Bay Rays"
        assert od["bookmakers"][0]["key"] == "draftkings"
        assert {m["key"] for m in od["bookmakers"][0]["markets"]} == {"h2h", "totals"}

    def test_unknown_event_returns_none(self, espn):
        with mock.patch.object(espn, "_espn_scoreboard",
                               return_value=_fixture("mlb_scoreboard.json")):
            assert espn.fetch_event_odds("baseball_mlb", "999999") is None

    def test_window_filter_excludes_kicked_off(self, espn):
        with mock.patch.object(espn, "_espn_scoreboard",
                               return_value=_fixture("mlb_scoreboard.json")):
            now = datetime(2026, 10, 18, 6, 0, tzinfo=timezone.utc)  # after 00:00Z start
            assert espn.fetch_event_odds("baseball_mlb", "401908027", within_hours=8, now=now) is None


# ── coverage: UFC skip + sport-key mapping ───────────────────────────────────
class TestCoverage:
    def test_ufc_is_uncovered(self, espn):
        assert "mma_mixed_martial_arts" in espn.ESPN_NO_ODDS
        # even with a real (odds-free) UFC fixture, availability is False
        with mock.patch.object(espn, "_espn_scoreboard",
                               return_value=_fixture("ufc_scoreboard.json")):
            assert espn.espn_dk_available("mma_mixed_martial_arts") is False
            assert espn.fetch_espn_events("mma_mixed_martial_arts") == []
            assert espn.fetch_event_odds("mma_mixed_martial_arts", "600060773") is None

    def test_odds_api_keys_map_to_espn_leagues(self, espn):
        for k, (path, league) in {
            "americanfootball_nfl": ("football", "nfl"),
            "baseball_mlb": ("baseball", "mlb"),
            "soccer_epl": ("soccer", "eng.1"),
            "soccer_spain_la_liga": ("soccer", "esp.1"),
            "soccer_uefa_champs_league": ("soccer", "uefa.champions"),
            "soccer_fifa_world_cup": ("soccer", "fifa.world"),
        }.items():
            assert espn.CLV_SPORT_MAP[k] == (path, league), k

    def test_available_true_when_slate_priced(self, espn):
        with mock.patch.object(espn, "_espn_scoreboard",
                               return_value=_fixture("soccer_uefa_scoreboard.json")):
            assert espn.espn_dk_available("soccer_uefa_champs_league") is True

    def test_unknown_sport_key_unavailable(self, espn):
        assert espn.espn_dk_available("soccer_some_random_league") is False


# ── caching + backoff (no live HTTP) ─────────────────────────────────────────
class TestCacheBackoff:
    def test_second_call_served_from_cache(self, espn):
        calls = []
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"events": []}
        with mock.patch.object(espn.requests, "get",
                               side_effect=lambda *a, **k: calls.append(1) or resp):
            espn._espn_scoreboard("baseball_mlb")
            espn._espn_scoreboard("baseball_mlb")
        assert len(calls) == 1  # second hit served from cache

    def test_http_503_sets_backoff_and_serves_stale(self, espn):
        good = mock.Mock(status_code=200)
        good.json.return_value = {"events": [{"id": "x"}]}
        bad = mock.Mock(status_code=503)
        with mock.patch.object(espn.requests, "get", return_value=good):
            first = espn._espn_scoreboard("baseball_mlb")
        with mock.patch.object(espn.requests, "get", return_value=bad) as mget:
            espn._backoff_until["baseball_mlb"] = float("inf")  # force backoff window
            second = espn._espn_scoreboard("baseball_mlb")
        assert first == second == {"events": [{"id": "x"}]}
        mget.assert_not_called()  # backed off, served stale, no call

    def test_failure_returns_none_not_raise(self, espn):
        with mock.patch.object(espn.requests, "get",
                               side_effect=ConnectionError("boom")):
            assert espn._espn_scoreboard("baseball_mlb") is None


# ── source switch: consumers pick the backend from CLV_ODDS_SOURCE ──────────
class TestSourceSwitch:
    def _reload_logger(self, source):
        os.environ["CLV_ODDS_SOURCE"] = source
        for mod in ("prop_clv_logger", "btts_totals_snapshot"):
            sys.modules.pop(mod, None)
        import prop_clv_logger  # noqa
        import btts_totals_snapshot  # noqa
        return prop_clv_logger, btts_totals_snapshot

    @pytest.fixture(autouse=True)
    def _restore_env(self):
        old = os.environ.get("CLV_ODDS_SOURCE")
        yield
        if old is None:
            os.environ.pop("CLV_ODDS_SOURCE", None)
        else:
            os.environ["CLV_ODDS_SOURCE"] = old
        for mod in ("prop_clv_logger", "btts_totals_snapshot"):
            sys.modules.pop(mod, None)

    def test_default_is_espn_dk(self):
        os.environ.pop("CLV_ODDS_SOURCE", None)
        sys.modules.pop("prop_clv_logger", None)
        import prop_clv_logger as lg
        assert lg.CLV_ODDS_SOURCE == "espn_dk"

    def test_prop_logger_espn_dk_skips_uncovered_sport(self, espn, tmp_path):
        lg, _ = self._reload_logger("espn_dk")
        cfg = lg.CONFIGS["ufc"]
        con = lg.db_connect(str(tmp_path / "t.db"))
        # espn_dk_available returns False for UFC without any HTTP (uncovered set)
        lg.live_snapshot(con, cfg, window_hours=8, max_events=20)
        n = con.execute("SELECT COUNT(*) FROM prop_snapshot").fetchone()[0]
        con.close()
        assert n == 0  # clean skip, no rows, no error

    def test_prop_logger_espn_dk_skips_props_only_config(self, espn, tmp_path):
        lg, _ = self._reload_logger("espn_dk")
        cfg = lg.CONFIGS["soccer_epl"]  # player_goal_scorer_anytime only
        con = lg.db_connect(str(tmp_path / "t.db"))
        with mock.patch.object(espn, "espn_dk_available", return_value=True):
            lg.live_snapshot(con, cfg, window_hours=8, max_events=20)
        n = con.execute("SELECT COUNT(*) FROM prop_snapshot").fetchone()[0]
        con.close()
        assert n == 0

    def test_prop_logger_espn_dk_snapshots_nfl_game_lines(self, espn, tmp_path):
        lg, _ = self._reload_logger("espn_dk")
        cfg = lg.CONFIGS["nfl"]
        con = lg.db_connect(str(tmp_path / "t.db"))
        with mock.patch.object(espn, "espn_dk_available", return_value=True), \
             mock.patch.object(espn, "fetch_espn_events", return_value=[
                 {"id": "401872996", "commence_time": "2026-10-18T13:30:00Z",
                  "home_team": "Jacksonville Jaguars", "away_team": "Houston Texans"},
             ]), \
             mock.patch.object(espn, "fetch_event_odds", return_value={
                 "id": "401872996", "sport_key": "americanfootball_nfl",
                 "commence_time": "2026-10-18T13:30:00Z",
                 "home_team": "Jacksonville Jaguars", "away_team": "Houston Texans",
                 "bookmakers": [{"key": "draftkings", "title": "DraftKings", "markets": [
                     {"key": "h2h", "outcomes": [
                         {"name": "Jacksonville Jaguars", "price": -148},
                         {"name": "Houston Texans", "price": 124}]},
                     {"key": "totals", "outcomes": [
                         {"name": "Over", "price": -110, "point": 42.5, "description": "Over"},
                         {"name": "Under", "price": -110, "point": 42.5, "description": "Under"}]},
                 ]}],
             }):
            lg.live_snapshot(con, cfg, window_hours=30, max_events=20)
        rows = con.execute(
            "SELECT market, participant, line, side, soft_book, n_sharp FROM prop_snapshot"
        ).fetchall()
        con.close()
        assert rows, "NFL h2h should collect under espn_dk"
        assert all(r[5] == 0 for r in rows)          # single-book: n_sharp=0
        assert all(r[4] == "draftkings" for r in rows)
        assert {r[0] for r in rows} == {"h2h", "totals"}

    def test_prop_logger_the_odds_api_path_still_bills_key(self, tmp_path):
        lg, _ = self._reload_logger("the_odds_api")
        cfg = lg.CONFIGS["nfl"]
        con = lg.db_connect(str(tmp_path / "t.db"))
        with mock.patch.object(lg, "credits_remaining", return_value=5000), \
             mock.patch.object(lg.P, "_get", side_effect=AssertionError("must not fetch")):
            # no ODDS_API_KEY set → clean exit before any fetch
            with pytest.raises(SystemExit):
                lg.live_snapshot(con, cfg, window_hours=8, max_events=20)
        con.close()

    def test_btts_logger_espn_dk_skips_cleanly(self, tmp_path, capsys):
        _, bt = self._reload_logger("espn_dk")
        con = bt.db_connect(str(tmp_path / "t.db"))
        out = bt.live_snapshot(con, "soccer_epl", window_hours=8)
        con.close()
        assert out == []
        assert "[skip]" in capsys.readouterr().out  # one-line skip, not an error

    def test_btts_logger_the_odds_api_requires_key(self, tmp_path):
        _, bt = self._reload_logger("the_odds_api")
        os.environ.pop("ODDS_API_KEY", None)
        con = bt.db_connect(str(tmp_path / "t.db"))
        # chdir into tmp so the repo's config/.env (dead key) is NOT found —
        # proves the key-required exit, with no live HTTP possible.
        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            with pytest.raises(SystemExit):
                bt.live_snapshot(con, "soccer_epl", window_hours=8)
        finally:
            os.chdir(old_cwd)
        con.close()


# ── scorer logger: props lane skips under espn_dk ────────────────────────────
class TestScorerLogger:
    @pytest.fixture(autouse=True)
    def _restore_env(self):
        old = os.environ.get("CLV_ODDS_SOURCE")
        old_key = os.environ.pop("ODDS_API_KEY", None)
        yield
        if old is None:
            os.environ.pop("CLV_ODDS_SOURCE", None)
        else:
            os.environ["CLV_ODDS_SOURCE"] = old
        if old_key is not None:
            os.environ["ODDS_API_KEY"] = old_key

    def test_scorer_espn_dk_skips_props(self, tmp_path, capsys):
        sys.modules.pop("scorer_paper_logger", None)
        import scorer_paper_logger as sp
        con = sp.db_connect(str(tmp_path / "t.db"))
        sp.live_snapshot(con, "soccer_fifa_world_cup", window_hours=8)
        con.close()
        assert "[skip]" in capsys.readouterr().out

    def test_scorer_the_odds_api_requires_key(self, tmp_path):
        os.environ["CLV_ODDS_SOURCE"] = "the_odds_api"
        sys.modules.pop("scorer_paper_logger", None)
        import scorer_paper_logger as sp
        con = sp.db_connect(str(tmp_path / "t.db"))
        with pytest.raises(SystemExit):
            sp.live_snapshot(con, "soccer_fifa_world_cup", window_hours=8)
        con.close()