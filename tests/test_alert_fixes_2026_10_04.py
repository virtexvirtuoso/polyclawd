"""Tests for the 2026-10-04 alert-audit fixes.

Covers the two approved builds (48h alert audit, vault
Research/Edge-Methodology/Alert-Effectiveness-Audit-2026-10-04.md):

  #1 MLB prop playoff gate      — signals/mlb_prop_alerts.py
  #2 whale digest + attribution — signals/whale_outcomes.py,
                                  scripts/whale_alert_tg.py,
                                  services/scheduler.py recap wiring
"""

import asyncio
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import signals.mlb_prop_alerts as pa
import signals.whale_outcomes as wo
import scripts.whale_alert_tg as tg
import signals.discord_alerts as da


# ─────────────────────────────────────────────────────────────────────────────
# #1 prop playoff gate — helpers
# ─────────────────────────────────────────────────────────────────────────────

def test_is_postseason():
    assert pa.is_postseason("F")        # World Series
    assert pa.is_postseason("D")        # division series (verified live 2026-10-04)
    assert pa.is_postseason("W")
    assert pa.is_postseason("L")
    assert not pa.is_postseason("R")    # regular season
    assert not pa.is_postseason("A")    # all-star
    assert not pa.is_postseason("S")    # spring
    assert not pa.is_postseason(None)   # missing field -> default R, never block


def test_taker_fee_pp():
    assert pa.taker_fee_pp(0.5) == pytest.approx(1.75, abs=0.01)
    assert pa.taker_fee_pp(0.9) == pytest.approx(0.63, abs=0.05)
    assert pa.taker_fee_pp(0.1) == pytest.approx(0.63, abs=0.05)


def test_mid_moved_toward_pick():
    # book 41.3%, hit rate 50% -> lean YES; fair moved +4pp toward YES -> blocked
    assert pa.mid_moved_toward_pick(0.453, 41.3, 50.0)
    # fair moved AWAY from the lean -> not blocked
    assert not pa.mid_moved_toward_pick(0.38, 41.3, 50.0)
    # small move under tolerance -> not blocked
    assert not pa.mid_moved_toward_pick(0.42, 41.3, 50.0)
    # no lean -> never blocked
    assert not pa.mid_moved_toward_pick(0.50, 50.0, 50.0)


def test_windows_carry_game_type(monkeypatch):
    import odds.mlb_lineups as lineups

    games = [{
        "gamePk": 1,
        "gameDate": datetime.now(timezone.utc).isoformat(),
        "officialDate": "2026-10-05",
        "gameType": "F",
        "teams": {"away": {"team": {"name": "Away"}}, "home": {"team": {"name": "Home"}}},
    }]
    monkeypatch.setattr(lineups, "get_scheduled_games", lambda d=None: games)
    wins = pa.build_scan_windows("2026-10-05")
    assert wins and wins[0]["game_type"] == "F"


# ─────────────────────────────────────────────────────────────────────────────
# #1 prop playoff gate — end-to-end scan-tick integration
# ─────────────────────────────────────────────────────────────────────────────

def _mk_temp_db(monkeypatch, tmp_path):
    """File-backed shadow DB; _db() returns a FRESH connection per call
    (production call sites commit() and close() their connection)."""
    db = tmp_path / "shadow.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    pa._init_tables(conn)
    conn.close()

    def fake_db():
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(pa, "_db", fake_db)
    return db


def _scan_row():
    vals = [6.0 if i % 2 == 0 else 4.0 for i in range(20)]  # L20 hit rate 50% vs 5.5
    return {
        "player": "Gerrit Cole",
        "market": "pitcher_strikeouts",
        "stat_label": "K",
        "prop_line": 5.5,
        "hit_rate_pct": 50.0,
        "book_over_pct": 41.3,
        "edge_pct": 8.7,
        "games_sampled": 20,
        "away_team": "New York Yankees",
        "home_team": "Tampa Bay Rays",
        "last_n_vals": vals,
    }


def _run_scan(game_type, monkeypatch, tmp_path, kalshi_fair=None):
    """One run_prop_alert_scan tick with hermetic mocks. Returns (summary, db path)."""
    db = _mk_temp_db(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    window = {
        "game_pk": 123,
        "game_date": "2026-10-03",
        "game_time_utc": now + timedelta(minutes=90),  # inside T-4h..T-1h
        "away_team": "New York Yankees",
        "home_team": "Tampa Bay Rays",
        "window_start": now - timedelta(hours=1),
        "window_end": now + timedelta(minutes=30),
        "status": "Preview",
        "game_type": game_type,
    }
    monkeypatch.setattr(pa, "active_windows", lambda now=None, date_str=None: [window])
    monkeypatch.setattr(pa, "_push_alerts", lambda rows: None)

    import odds.mlb_lineups as lineups
    monkeypatch.setattr(lineups, "is_player_starting", lambda p, pk: True)

    import odds.mlb_prop_scout as scout
    monkeypatch.setattr(scout, "_lookup_player_id", lambda name: 12345)

    async def fake_scout(**kw):
        return {"results": [_scan_row()]}
    monkeypatch.setattr(scout, "get_prop_scout", fake_scout)

    import odds.mlb_enrichment as enrich
    import odds.statcast as sc
    import odds.sports_edge_common as sec
    monkeypatch.setattr(enrich, "enrich_row", lambda row, h, a: row)
    monkeypatch.setattr(sc, "enrich_with_statcast", lambda row: row)
    monkeypatch.setattr(sec, "log_enrichment", lambda **kw: None)

    import odds.kalshi_props as kp
    ks_rows = []
    if kalshi_fair is not None:
        # fair 0-1 -> kalshi_mid in cents; book line 5.5 -> kalshi_line 6
        ks_rows = [{"prop_type": "KS", "kalshi_line": 6, "player": "Cole",
                    "kalshi_mid": kalshi_fair * 100}]

    async def fake_kalshi_scan(**kw):
        return {"results": ks_rows}
    monkeypatch.setattr(kp, "get_kalshi_prop_scan", fake_kalshi_scan)

    summary = asyncio.run(pa.run_prop_alert_scan(now=now))
    return summary, db


def test_scan_blocks_postseason_alert_but_logs_control(monkeypatch, tmp_path):
    summary, db = _run_scan("F", monkeypatch, tmp_path)
    assert summary["alerted"] == 0
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    # control sample still logged with alerted=0 (playoff calibration data)
    logged = conn.execute("SELECT alerted FROM mlb_prop_scan_log").fetchall()
    assert logged and all(r["alerted"] == 0 for r in logged)
    # no shadow trade created for a blocked alert
    assert conn.execute("SELECT COUNT(*) c FROM mlb_prop_shadow").fetchone()["c"] == 0
    conn.close()


def test_scan_regular_season_still_alerts(monkeypatch, tmp_path):
    summary, db = _run_scan("R", monkeypatch, tmp_path, kalshi_fair=0.42)  # fair ~= book
    assert summary["alerted"] == 1
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) c FROM mlb_prop_shadow").fetchone()[0] == 1
    conn.close()


def test_scan_blocks_when_mid_already_moved(monkeypatch, tmp_path):
    # regular-season game, but Kalshi fair moved +4pp toward the YES lean
    summary, db = _run_scan("R", monkeypatch, tmp_path, kalshi_fair=0.453)
    assert summary["alerted"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# #2 whale attribution — priority lane for the 1h horizon
# ─────────────────────────────────────────────────────────────────────────────

def test_backfill_fresh_lane_samples_1h(monkeypatch, tmp_path):
    meta = wo.get_meta_db(tmp_path / "m.db")
    now = time.time()
    # 600 old rows saturate the age-ordered batch (BACKFILL_CAP=500)...
    for i in range(600):
        meta.execute(
            "INSERT INTO whale_outcomes (alert_id, ts, platform, market, done) "
            "VALUES (?, ?, 'kalshi', ?, 0)",
            (1000 + i, now - 10 * 86400 - i, f"KXOLD{i}"))
    # ...but the fresh row's 1h horizon is open RIGHT NOW and must not starve.
    fresh_ts = now - wo.H1 - 60
    meta.execute(
        "INSERT INTO whale_outcomes (alert_id, ts, platform, market, done, "
        "direction, price_at_alert) VALUES (1, ?, 'kalshi', 'KXFRESH', 0, 1, 0.50)",
        (fresh_ts,))
    meta.commit()

    monkeypatch.setattr(wo, "kalshi_lookup",
                        lambda ticks: {t: {"mid": 0.55, "result": ""} for t in ticks})
    monkeypatch.setattr(wo, "pm_lookup", lambda slugs: {})

    stats = wo.backfill(meta)
    fresh = meta.execute("SELECT * FROM whale_outcomes WHERE alert_id=1").fetchone()
    assert fresh["price_1h"] is not None, "fresh 1h horizon must be sampled this pass"
    assert fresh["correct_1h"] == 1  # direction +1, mid moved 0.50 -> 0.55
    assert stats["filled"] >= 1
    meta.close()


# ─────────────────────────────────────────────────────────────────────────────
# #2 whale digest — precision gate + digest send
# ─────────────────────────────────────────────────────────────────────────────

def test_precision_gate_fails_open():
    pmap = {("kalshi", "KXBAD"): (0.30, 25), ("kalshi", "KXTHIN"): (0.30, 3)}
    assert not tg._precision_ok("kalshi", "KXBAD", pmap)     # measured, bad -> suppress
    assert tg._precision_ok("kalshi", "KXTHIN", pmap)       # too few grades -> fail open
    assert tg._precision_ok("kalshi", "KXUNKNOWN", pmap)    # no data -> fail open


def test_precision_map_cache(monkeypatch, tmp_path):
    db = tmp_path / "whale_meta.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE whale_outcomes (platform TEXT, market TEXT, "
                 "correct_res REAL, ts REAL)")
    conn.execute("INSERT INTO whale_outcomes VALUES ('kalshi','KXA',1,?)", (time.time(),))
    conn.commit()
    conn.close()
    monkeypatch.setattr(tg, "PRECISION_DB_PATH", db)
    tg._PRECISION_CACHE["ts"] = 0.0
    m = tg._load_precision_map()
    assert m.get(("kalshi", "KXA"))[0] == 1.0
    ts_before = tg._PRECISION_CACHE["ts"]
    m2 = tg._load_precision_map()
    assert tg._PRECISION_CACHE["ts"] == ts_before, "TTL cache: no re-read inside window"
    assert m2 is m


def test_send_digest(monkeypatch):
    state = {}
    monkeypatch.setattr(tg, "load_state", lambda: dict(state))
    monkeypatch.setattr(tg, "save_state", lambda s: state.update(s))
    monkeypatch.setattr(tg, "load_clob_fired", lambda: set())
    monkeypatch.setattr(tg, "get_top_alerts", lambda: [
        # flow must clear the Kalshi no-wallet fallback (>= $25K) to be actionable
        {"market": "KXB", "platform": "kalshi", "title": "B wins", "score": 7,
         "flow_dollars": 30000},
        {"market": "KXA", "platform": "kalshi", "title": "A wins", "score": 9,
         "flow_dollars": 50000},
    ])
    sent = {}
    monkeypatch.setattr(tg, "send_tg", lambda msg: sent.update(msg=msg) or True)
    assert tg.send_digest() is True
    msg = sent["msg"]
    assert "Whale Digest" in msg
    assert "A wins" in msg and "B wins" in msg
    assert msg.index("A wins") < msg.index("B wins"), "ranked: score 9 above score 7"
    assert "KXA" in state and "KXB" in state, "digest marks dedup state"


def test_send_digest_empty(monkeypatch):
    monkeypatch.setattr(tg, "load_state", lambda: {})
    monkeypatch.setattr(tg, "get_top_alerts", lambda: [])
    assert tg.send_digest() is True  # quiet, not an error


# ─────────────────────────────────────────────────────────────────────────────
# #2 weekly recap scoreboard
# ─────────────────────────────────────────────────────────────────────────────

def test_recap_renders_shadow_and_paper_rows(monkeypatch):
    captured = {}
    monkeypatch.setattr(da, "_send", lambda embeds, **kw: captured.update(e=embeds) or True)
    ok = da.alert_weekly_recap(
        12000, 12100, 5, 3, -100,
        strategies={
            "mlb_props": {"pnl": None, "wr": 40.0, "n": 5},
            "whale_kalshi": {"pnl": -500.0, "wr": 40.0, "n": 10},
            "whale_polymarket": {"pnl": 2500.0, "wr": 38.0, "n": 200},
        })
    assert ok
    fields = captured["e"][0]["fields"]
    strat = [f for f in fields if f["name"] == "By Strategy"][0]["value"]
    assert "MLB Props (shadow)" in strat and "40% WR" in strat
    assert "$0" not in strat, "shadow rows must not render a fake $0 P&L"
    assert "Whale KX (paper)" in strat and "$-500" in strat  # renderer sign convention
    assert "Whale PM (paper)" in strat and "+$2500" in strat