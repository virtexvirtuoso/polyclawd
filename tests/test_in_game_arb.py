"""Offline tests for services/in_game_arb.py — fixture-driven, zero network.

Fixtures captured live 2026-10-01 ~20:55 ET from PIT@CLE (see module docstring
of services/in_game_arb.py for the probe facts they pin).
"""
import json
import time
from pathlib import Path

import pytest

import services.in_game_arb as arb

FIX = Path(__file__).parent / "fixtures" / "in_game_arb"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


@pytest.fixture(scope="module")
def pmus_event() -> dict:
    return load("pmus_slim.json")["event"]


def kalshi_event(series: str) -> dict:
    return load("kalshi_%s_slim.json" % series)["event"]


# ---------------------------------------------------------------- parsers

def test_parse_pmus_prop():
    assert arb.parse_pmus_prop("Will Deshaun Watson record 125+ passing yards?") == \
        ("deshaun watson", 125.0, "pass_yds")


def test_parse_pmus_prop_int_unpairable():
    # Kalshi has no INT series — INTs must never pair
    assert arb.parse_pmus_prop("Will Aaron Rodgers throw 2+ interceptions?") is None


def test_parse_kalshi_prop():
    assert arb.parse_kalshi_prop("Deshaun Watson: 125+ passing yards") == \
        ("deshaun watson", 125.0, "pass_yds")


def test_parse_kalshi_total():
    assert arb.parse_kalshi_total("Full Game: over 17.5 points scored?") == 17.5
    assert arb.parse_kalshi_total("PIT Steelers wins by over 20.5 points?") is None


def test_split_team_abbrs():
    assert arb.split_team_abbrs("PITCLE") == ("PIT", "CLE")
    assert arb.split_team_abbrs("NOLAPHI") == ("NOLA", "PHI")
    assert arb.split_team_abbrs("XXYZ") == ()


def test_parse_game_suffix():
    assert arb.parse_game_suffix("26OCT01PITCLE") == ("2026-10-01", ("PIT", "CLE"))
    assert arb.parse_game_suffix("garbage") == (None, ())


# ---------------------------------------------------------------- fees / math

def test_kalshi_fee_cents():
    assert arb.kalshi_fee_cents(0.5) == pytest.approx(1.75)
    assert arb.kalshi_fee_cents(0.0) == 0.0
    assert arb.kalshi_fee_cents(1.0) == 0.0


def test_eval_pair_direction_a():
    p = arb.Pair(key="k", game="g", stat="pass_yds", name="w", line=125,
                 pm_bid=70, pm_ask=71, kal_bid=75, kal_ask=80, pm_slug="s", kal_title="t")
    ev = arb.eval_pair(p)
    # A: PM YES 71 + KAL NO 25 = 96; fee 7*.25*.75 = 1.3125
    assert ev["direction"] == "A"
    assert ev["cost"] == pytest.approx(96.0)
    assert ev["net"] == pytest.approx(100 - 96 - 1.3125)


def test_eval_pair_direction_b():
    p = arb.Pair(key="k", game="g", stat="pass_yds", name="w", line=125,
                 pm_bid=90, pm_ask=91, kal_bid=60, kal_ask=62, pm_slug="s", kal_title="t")
    ev = arb.eval_pair(p)
    # B: KAL YES 62 + PM NO 10 = 72; fee 7*.62*.38 = 1.6492
    assert ev["direction"] == "B"
    assert ev["net"] == pytest.approx(100 - 72 - 1.6492)


# ---------------------------------------------------------------- espn

def test_espn_live_games_aliases():
    sb = {"events": [{"competitions": [{"competitors": [
        {"team": {"abbreviation": "NO"}, "score": "7"},
        {"team": {"abbreviation": "LV"}, "score": "3"}]}],
        "status": {"type": {"state": "in"}, "period": 1,
                   "shortDetail": "5:00 - 1st"}}]}
    live = arb.espn_live_games(sb)
    # sorted-tuple keys; NO registers under both itself and its NOLA alias
    assert ("LV", "NOLA") in live
    assert ("LV", "NO") in live
    assert live[("LV", "NO")]["score"] == {"NO": 7, "LV": 3}


def test_espn_live_games_fixture():
    live = arb.espn_live_games(load("espn_pitcle.json"))
    info = live[("CLE", "PIT")]
    assert info["state"] == "in"
    assert info["score"] == {"CLE": 0, "PIT": 7}


# ---------------------------------------------------------------- pairing (fixtures)

def test_pair_props_fixture(pmus_event):
    kal = []
    for s in ("KXNFLPASSYDS", "KXNFLTD", "KXNFLRECYDS"):
        kal += kalshi_event(s)["markets"]
    pairs = arb.pair_props("26OCT01PITCLE", pmus_event["markets"], kal)
    # Watson 125+ pairs, with prices taken from the fixture rows themselves
    # (fixture is a live-game snapshot — absolute cents move, pairing must not)
    w = [p for p in pairs if p.name == "deshaun watson" and p.stat == "pass_yds" and p.line == 125]
    assert w, "Watson 125+ should pair"
    src = next(m for m in pmus_event["markets"] if (m.get("slug") or "").endswith("pyd-deswat-gte125"))
    assert (w[0].pm_bid, w[0].pm_ask) == pytest.approx(arb._pm_prices(src))
    ksrc = next(m for m in kal if m["title"] == "Deshaun Watson: 125+ passing yards")
    assert (w[0].kal_bid, w[0].kal_ask) == (float(ksrc["yes_bid"]), float(ksrc["yes_ask"]))
    # QB TD exclusion: Watson/Rodgers TD markets must never pair
    assert not any(p.stat == "td" and p.name in ("deshaun watson", "aaron rodgers") for p in pairs)
    # non-QB TD pairs do exist
    tds = [p for p in pairs if p.stat == "td"]
    assert tds, "expected non-QB TD pairs"
    assert all("watson" not in p.name and "rodgers" not in p.name for p in tds)


def test_pair_totals_fixture(pmus_event):
    kal_total = kalshi_event("KXNFLTOTAL")["markets"]
    # Inject a Kalshi total row guaranteed to match a PM line (ladders may not
    # coincide in a given snapshot; the pairing logic is what's under test)
    pm_lines = [float(m["line"]) for m in pmus_event["markets"]
                if (m.get("sportsMarketType") or "") == "football_team_full_game_total"]
    assert pm_lines, "fixture should contain PM totals"
    target = pm_lines[0]
    kal_total = kal_total + [{"title": "Full Game: over %g points scored?" % target,
                              "yes_bid": 40, "yes_ask": 42, "volume": 50000,
                              "status": "active", "ticker": "KXNFLTOTAL-26OCT01PITCLE-TST"}]
    pairs = arb.pair_totals("26OCT01PITCLE", pmus_event["markets"], kal_total)
    matched = [p for p in pairs if p.line == target]
    assert matched, "injected total line must pair"
    assert (matched[0].kal_bid, matched[0].kal_ask) == (40.0, 42.0)


def test_pair_ml_fixture(pmus_event):
    kal_game = kalshi_event("KXNFLGAME")
    pairs = arb.pair_ml("26OCT01PITCLE", pmus_event, kal_game)
    assert len(pairs) == 2
    by_name = {p.name: p for p in pairs}
    assert set(by_name) == {"steelers", "browns"}
    ml = next(m for m in pmus_event["markets"] if (m.get("slug") or "").startswith("aec-"))
    pm0 = arb._pm_prices(ml)  # outcomes[0] = Steelers
    st, br = by_name["steelers"], by_name["browns"]
    assert (st.pm_bid, st.pm_ask) == pytest.approx(pm0)
    assert (br.pm_bid, br.pm_ask) == pytest.approx((100.0 - pm0[1], 100.0 - pm0[0]))
    kal_by_title = {m["title"]: m for m in kal_game["markets"]}
    assert (st.kal_bid, st.kal_ask) == (float(kal_by_title["Pittsburgh wins"]["yes_bid"]),
                                        float(kal_by_title["Pittsburgh wins"]["yes_ask"]))
    assert (br.kal_bid, br.kal_ask) == (float(kal_by_title["Cleveland wins"]["yes_bid"]),
                                        float(kal_by_title["Cleveland wins"]["yes_ask"]))


def test_build_pairs_fixture(pmus_event):
    kal_events = {s: kalshi_event(s) for s in ("KXNFLGAME", "KXNFLPASSYDS", "KXNFLTD", "KXNFLTOTAL")}
    pairs = arb.build_pairs("26OCT01PITCLE", pmus_event, kal_events)
    stats = {p.stat for p in pairs}
    assert "ml" in stats and "pass_yds" in stats and "total" in stats


# ---------------------------------------------------------------- alert loop (mocked transports)

def _cfg(db_path: str, **over) -> dict:
    cfg = dict(interval=45, net_edge=2.0, kal_min_volume=10000, cooldown_min=10,
               max_confirms=5, sanity_gap=20.0, pm_fee=0.0, telegram=False, db=db_path,
               series=["KXNFLGAME", "KXNFLPASSYDS", "KXNFLTD"])
    cfg.update(over)
    return cfg


def _wire(monkeypatch, pmus_event, *, bbo=(60.0, 61.0), espn=None, kal_tweak=None):
    monkeypatch.setattr(arb.time, "sleep", lambda s: None)
    sb = espn if espn is not None else load("espn_pitcle.json")
    monkeypatch.setattr(arb, "espn_scoreboard", lambda: sb)

    def fake_series(series):
        ev = json.loads(json.dumps(kalshi_event(series)))  # deep copy
        if kal_tweak:
            kal_tweak(series, ev)
        return {ev["ticker"]: ev}

    monkeypatch.setattr(arb, "kalshi_series_events", fake_series)
    monkeypatch.setattr(arb, "pmus_search_event", lambda q, d, a: pmus_event)
    monkeypatch.setattr(arb, "pmus_bbo", lambda slug: bbo)


def _juicy(series, ev):
    """Sweep-time candidates that must NOT alert: Watson 125+ direction-B
    nets 1.85c (under threshold), PIT ML at 95/97 trips the sanity gate
    (64pp mid gap). Exercises the sweep-gate paths."""
    if series == "KXNFLPASSYDS":
        for m in ev["markets"]:
            if m["title"] == "Deshaun Watson: 125+ passing yards":
                m["yes_bid"], m["yes_ask"] = 90, 92
    if series == "KXNFLGAME":
        for m in ev["markets"]:
            if m["title"] == "Pittsburgh wins":
                m["yes_bid"], m["yes_ask"] = 95, 97


def test_run_cycle_alert_and_cooldown(tmp_path, pmus_event, monkeypatch):
    """The frozen fixture contains one live arb snapshot (Rodgers 200+ pass
    yds: PM 60/61 vs KAL 74/75 — a PM-US lag window caught at recapture).
    _juicy's Watson/PIT tweaks are sweep-time candidates the BBO-confirm
    sanity re-check must kill (the mock BBO diverges from the juiced Kal
    quote — exactly the stale-confirm case the re-check exists for)."""
    _wire(monkeypatch, pmus_event, kal_tweak=_juicy)
    conn = arb.db_init(str(tmp_path / "arb.db"))
    cfg = _cfg(str(tmp_path / "arb.db"))
    s1 = arb.run_cycle(cfg, conn)
    assert s1["games"] == 1
    assert s1["pairs"] > 0
    assert s1["alerts"] == 1
    rows = conn.execute("SELECT pair_key FROM fired").fetchall()
    assert rows == [("26OCT01PITCLE|pass_yds|aaron rodgers|200",)]
    # cooldown suppresses the identical second cycle
    s2 = arb.run_cycle(cfg, conn)
    assert s2["alerts"] == 0
    assert conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0] == 2


def test_run_cycle_unconfirmed_still_alerts(tmp_path, pmus_event, monkeypatch):
    _wire(monkeypatch, pmus_event, bbo=None, kal_tweak=_juicy)
    conn = arb.db_init(str(tmp_path / "arb.db"))
    s = arb.run_cycle(_cfg(str(tmp_path / "arb.db")), conn)
    assert s["alerts"] >= 1
    row = conn.execute("SELECT key FROM pairs_log WHERE alerted=1 LIMIT 1").fetchone()
    assert row is not None


def test_run_cycle_high_edge_no_alerts(tmp_path, pmus_event, monkeypatch):
    _wire(monkeypatch, pmus_event, kal_tweak=_juicy)
    conn = arb.db_init(str(tmp_path / "arb.db"))
    s = arb.run_cycle(_cfg(str(tmp_path / "arb.db"), net_edge=999), conn)
    assert s["alerts"] == 0
    assert s["pairs"] > 0


def test_run_cycle_skips_pregame(tmp_path, pmus_event, monkeypatch):
    sb = load("espn_pitcle.json")
    sb["events"][0]["status"]["type"]["state"] = "pre"
    _wire(monkeypatch, pmus_event, espn=sb, kal_tweak=_juicy)
    conn = arb.db_init(str(tmp_path / "arb.db"))
    s = arb.run_cycle(_cfg(str(tmp_path / "arb.db")), conn)
    assert s["games"] == 0 and s["alerts"] == 0


def test_team_totals_never_pair(pmus_event):
    """Regression for cycle-1 false alerts: PM-US team totals (tt- slugs,
    football_team_points_full_game_total) must never pair vs Kalshi game totals."""
    kal_total = kalshi_event("KXNFLTOTAL")["markets"]
    pairs = arb.pair_totals("26OCT01PITCLE", pmus_event["markets"], kal_total)
    assert pairs, "game totals should still pair"
    for p in pairs:
        assert "-tt-" not in p.pm_slug, "team total leaked into pairing: %s" % p.pm_slug


def test_degenerate_pm_rejected():
    """Regression for the Judkins false alert: settled PM book (bid 100/ask 0)."""
    p = arb.Pair(key="k", game="g", stat="td", name="q", line=1,
                 pm_bid=100.0, pm_ask=0.0, kal_bid=99, kal_ask=100,
                 pm_slug="s", kal_title="t")
    assert arb.degenerate_pm(p)
    good = arb.Pair(key="k2", game="g", stat="td", name="q", line=1,
                    pm_bid=29, pm_ask=31, kal_bid=28, kal_ask=29,
                    pm_slug="s", kal_title="t")
    assert not arb.degenerate_pm(good)


def test_sanity_gap_blocks_scope_mismatch():
    """Cycle-1 false alerts sat at 38-80pp mid gaps; the real Watson arb at ~7pp.
    Default gate is 20pp."""
    false_tot = arb.Pair(key="k", game="g", stat="total", name="game", line=38.5,
                         pm_bid=2.0, pm_ask=3.0, kal_bid=81, kal_ask=84,
                         pm_slug="s", kal_title="t")
    assert not arb.sanity_gap_ok(false_tot, 20.0)
    false_tot2 = arb.Pair(key="k3", game="g", stat="total", name="game", line=36.5,
                          pm_bid=88.5, pm_ask=89.0, kal_bid=50, kal_ask=51,
                          pm_slug="s", kal_title="t")
    assert not arb.sanity_gap_ok(false_tot2, 20.0)
    real_arb = arb.Pair(key="k2", game="g", stat="pass_yds", name="w", line=125,
                        pm_bid=70, pm_ask=71, kal_bid=83, kal_ask=87,
                        pm_slug="s", kal_title="t")
    assert arb.sanity_gap_ok(real_arb, 20.0)


def test_apply_bbo_inverts_ml_browns_side():
    """Regression for the 2026-10-02 01:28 ET corrupted alert: the ML market's
    BBO describes outcomes[0] (Steelers); the Browns pair must re-invert it."""
    browns = arb.Pair(key="26OCT01PITCLE|ml|browns", game="26OCT01PITCLE", stat="ml",
                      name="browns", line=0.0, pm_bid=68.0, pm_ask=68.5,
                      kal_bid=74, kal_ask=75,
                      pm_slug="aec-nfl-pit-cle-2026-10-01",
                      kal_title="Cleveland wins", pm_inverted=True)
    fixed = arb.apply_bbo(browns, (25.5, 26.0))  # raw Steelers book
    assert (fixed.pm_bid, fixed.pm_ask) == (74.0, 74.5)
    # honest eval: PM YES 74.5 + KAL NO 26 = 100.5 -> no arb
    assert arb.eval_pair(fixed)["net"] < 0


def test_apply_bbo_passthrough_direct_side():
    steel = arb.Pair(key="g|ml|steelers", game="g", stat="ml", name="steelers",
                     line=0.0, pm_bid=31.5, pm_ask=32.0, kal_bid=25, kal_ask=26,
                     pm_slug="aec-x", kal_title="Pittsburgh wins", pm_inverted=False)
    fixed = arb.apply_bbo(steel, (30.0, 31.0))
    assert (fixed.pm_bid, fixed.pm_ask) == (30.0, 31.0)


def test_apply_bbo_none_keeps_sweep_prices():
    p = arb.Pair(key="k", game="g", stat="td", name="q", line=1,
                 pm_bid=29, pm_ask=31, kal_bid=28, kal_ask=29,
                 pm_slug="s", kal_title="t")
    assert arb.apply_bbo(p, None) is p


def test_run_cycle_ml_confirm_corruption_regression(tmp_path, monkeypatch):
    """Full-loop regression for the 01:28 ET corrupted Browns ML alert.

    Live sequence: PM-US had collapsed to Browns-favored (Steelers book
    31.5/32 -> Browns side 68/68.5 via inversion) while Kalshi still quoted
    Browns 74/75 — a real ~+4c direction-A window at sweep. The BBO confirm
    returns the RAW outcomes[0] (Steelers) book; the old code copied it into
    the Browns pair un-inverted and alerted a nonsense +46.7c arb. Fixed
    code re-inverts (apply_bbo), the re-evaluated net goes negative, and no
    alert fires. Minimal synthetic event so fixture-native prop snapshots
    can't mask the path."""
    pm_event = {"slug": "nfl-pit-cle-2026-10-01", "title": "Pittsburgh vs. Cleveland",
                "markets": [{
                    "slug": "aec-nfl-pit-cle-2026-10-01",
                    "question": "Who will win in the upcoming football event?",
                    "sportsMarketType": "football_team_full_game_winner",
                    "outcomes": "[\"Steelers\",\"Browns\"]",
                    "outcomePrices": "[\"0.3150\",\"0.3200\"]",
                }]}
    kal_game = {"ticker": "KXNFLGAME-26OCT01PITCLE", "title": "PIT Steelers vs CLE Browns",
                "markets": [
                    {"title": "Pittsburgh wins", "yes_bid": 25, "yes_ask": 26,
                     "volume": 500000, "status": "active"},
                    {"title": "Cleveland wins", "yes_bid": 74, "yes_ask": 75,
                     "volume": 500000, "status": "active"},
                ]}
    monkeypatch.setattr(arb.time, "sleep", lambda s: None)
    monkeypatch.setattr(arb, "espn_scoreboard", lambda: load("espn_pitcle.json"))
    monkeypatch.setattr(arb, "kalshi_series_events",
                        lambda s: {"KXNFLGAME-26OCT01PITCLE": kal_game} if s == "KXNFLGAME" else {})
    monkeypatch.setattr(arb, "pmus_search_event", lambda q, d, a: pm_event)
    monkeypatch.setattr(arb, "pmus_bbo", lambda slug: (25.5, 26.0))  # raw Steelers book
    conn = arb.db_init(str(tmp_path / "arb.db"))
    s = arb.run_cycle(_cfg(str(tmp_path / "arb.db")), conn)
    assert s["alerts"] == 0
    # non-vacuous: the Browns side WAS a sweep-time candidate (~+4c), logged unalerted
    row = conn.execute("SELECT net_edge, direction FROM pairs_log WHERE key=? AND alerted=0",
                       ("26OCT01PITCLE|ml|browns",)).fetchone()
    assert row is not None and row[1] == "A" and row[0] >= 2.0


def test_format_recap_orders_and_sums(tmp_path):
    """Recap: best window first, alert count summed, empty game silent."""
    conn = arb.db_init(str(tmp_path / "arb.db"))
    ins = ("INSERT INTO pairs_log (ts, game, key, stat, name, line, pm_bid, pm_ask,"
           " kal_bid, kal_ask, net_edge, direction, alerted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")
    now = time.time()
    conn.execute(ins, (now, "26OCT01PITCLE", "26OCT01PITCLE|pass_yds|deshaun watson|250",
                       "pass_yds", "deshaun watson", 250, 18, 19, 13, 14, 3.2, "B", 1))
    conn.execute(ins, (now, "26OCT01PITCLE", "26OCT01PITCLE|ml|browns",
                       "ml", "browns", 0, 68, 68.5, 74, 75, 4.2, "A", 1))
    conn.execute(ins, (now, "26OCT01PITCLE", "26OCT01PITCLE|ml|browns",
                       "ml", "browns", 0, 68, 68.5, 74, 75, 4.1, "A", 1))
    conn.commit()
    text = arb.format_recap(conn, "26OCT01PITCLE", {"score": {"CLE": 27, "PIT": 24}})
    assert "🏁 <b>PIT@CLE</b>" in text and "CLE 27 PIT 24" in text
    assert "3 alerts on 2 windows · best +4.2¢" in text  # own line now
    assert "Browns ML — best +4.2¢ ×2" in text  # biggest first
    assert "Deshaun Watson 250+ pass yds — best +3.2¢ ×1" in text
    assert "transient" in text
    assert arb.format_recap(conn, "26OCT02XXXXXX", {}) == ""


def test_recap_pending_games_idempotent(tmp_path, monkeypatch):
    """One recap per finished game — second call sends nothing."""
    conn = arb.db_init(str(tmp_path / "arb.db"))
    ins = ("INSERT INTO pairs_log (ts, game, key, stat, name, line, pm_bid, pm_ask,"
           " kal_bid, kal_ask, net_edge, direction, alerted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")
    now = time.time()
    conn.execute(ins, (now, "26OCT01PITCLE", "26OCT01PITCLE|ml|browns",
                       "ml", "browns", 0, 68, 68.5, 74, 75, 4.2, "A", 1))
    conn.commit()
    sends = []
    monkeypatch.setattr(arb, "send_telegram", lambda text, enabled: sends.append(text) or True)
    live = {("CLE", "PIT"): {"state": "post", "detail": "Final",
                              "score": {"CLE": 27, "PIT": 24}}}
    cfg = {"telegram": False}
    assert arb.recap_pending_games(conn, live, cfg, now) == 1
    assert len(sends) == 1 and "PIT@CLE" in sends[0]
    assert arb.recap_pending_games(conn, live, cfg, now) == 0  # durable dedupe
    # in-game (not post) -> not yet
    conn2 = arb.db_init(str(tmp_path / "b.db"))
    conn2.execute(ins, (now, "26OCT01PITCLE", "26OCT01PITCLE|ml|browns",
                        "ml", "browns", 0, 68, 68.5, 74, 75, 4.2, "A", 1))
    conn2.commit()
    live2 = {("CLE", "PIT"): {"state": "in", "detail": "2:00 - 4th", "score": {}}}
    assert arb.recap_pending_games(conn2, live2, cfg, now) == 0


def test_format_alert_contents(pmus_event):
    p = arb.Pair(key="26OCT01PITCLE|pass_yds|deshaun watson|125", game="26OCT01PITCLE",
                 stat="pass_yds", name="deshaun watson", line=125,
                 pm_bid=60, pm_ask=61, kal_bid=90, kal_ask=92,
                 pm_slug="astatc-nfl-pit-cle-2026-10-01-pyd-deswat-gte125",
                 kal_title="Deshaun Watson: 125+ passing yards",
                 pm_event_slug="nfl-pit-cle-2026-10-01",
                 kal_event_ticker="KXNFLPASSYDS-26OCT01PITCLE")
    ev = arb.eval_pair(p)
    text = arb.format_alert(p, ev, {"detail": "5:00 - 2nd", "score": {"CLE": 0, "PIT": 7}},
                            confirmed=True)
    assert "🔥 <b>ARB PIT@CLE</b> · Deshaun Watson 125+ pass yds" in text
    # A: PM YES 61 + KAL NO 10 = 71; fee 7*.10*.90 = 0.63 -> net +28.4
    assert "<b>+28.4¢ net</b>" in text
    assert "PM YES 60/61 · KAL YES 90/92" in text
    assert ('href="https://polymarket.com/event/nfl-pit-cle-2026-10-01/'
            'astatc-nfl-pit-cle-2026-10-01-pyd-deswat-gte125"') in text
    assert 'href="https://kalshi.com/markets/KXNFLPASSYDS-26OCT01PITCLE"' in text
    assert "5:00 - 2nd" in text and "CLE 0 PIT 7" in text
    assert "verify depth" in text
    # no raw slug lines in the body (the old format's worst offender)
    assert "PM slug:" not in text and "KAL:" not in text
    # unconfirmed tag + repeat counter render
    assert "<i>unconfirmed</i>" in arb.format_alert(p, ev, None, confirmed=False)
    assert "re-alert 2/hr" in arb.format_alert(p, ev, None, confirmed=True, repeats=2)


def test_format_alert_batch_packs_rows():
    """Same-cycle candidates pack into ONE message with per-row links."""
    def mk(name, line):
        return arb.Pair(key="g|pass_yds|%s|%g" % (name, line), game="26OCT01PITCLE",
                        stat="pass_yds", name=name, line=line,
                        pm_bid=60, pm_ask=61, kal_bid=74, kal_ask=75,
                        pm_slug="astatc-x-%s" % name.replace(" ", "-"), kal_title="t",
                        pm_event_slug="nfl-pit-cle-2026-10-01",
                        kal_event_ticker="KXNFLPASSYDS-26OCT01PITCLE")
    rows = [(mk("aaron rodgers", 200), arb.eval_pair(mk("aaron rodgers", 200)), True, 0),
            (mk("deshaun watson", 200), arb.eval_pair(mk("deshaun watson", 200)), False, 1)]
    text = arb.format_alert_batch(rows, {"detail": "7:39 - 4th", "score": {"CLE": 21, "PIT": 16}})
    assert "2 windows" in text
    assert "Aaron Rodgers 200+ pass yds" in text
    assert "Deshaun Watson 200+ pass yds" in text
    assert text.count("polymarket.com/event/") == 2
    assert "re-alert 1/hr" in text and "<i>unconfirmed</i>" in text
    assert "7:39 - 4th" in text


def test_venue_links_forms():
    p_full = arb.Pair(key="k", game="g", stat="pass_yds", name="w", line=125,
                      pm_bid=1, pm_ask=2, kal_bid=3, kal_ask=4, pm_slug="ps", kal_title="t",
                      pm_event_slug="es", kal_event_ticker="KXNFLPASSYDS-26OCT01PITCLE")
    links = arb._venue_links(p_full)
    assert 'href="https://polymarket.com/event/es/ps"' in links
    assert 'href="https://kalshi.com/markets/KXNFLPASSYDS-26OCT01PITCLE"' in links
    p_event_only = p_full._replace(pm_slug="")
    assert 'href="https://polymarket.com/event/es"' in arb._venue_links(p_event_only)
    p_none = p_full._replace(pm_event_slug="", kal_event_ticker="")
    assert arb._venue_links(p_none) == ""


def test_prior_alert_count(tmp_path):
    conn = arb.db_init(str(tmp_path / "arb.db"))
    now = time.time()
    ins = ("INSERT INTO pairs_log (ts, game, key, stat, name, line, pm_bid, pm_ask,"
           " kal_bid, kal_ask, net_edge, direction, alerted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")
    for i in range(3):  # three alerted rows in the last hour
        conn.execute(ins, (now - 60 * i, "g", "k", "pass_yds", "w", 125, 1, 2, 3, 4, 5, "A", 1))
    conn.execute(ins, (now - 7200, "g", "k", "pass_yds", "w", 125, 1, 2, 3, 4, 5, "A", 1))  # 2h old
    conn.commit()
    assert arb.prior_alert_count(conn, "k", "A", now) == 3
    assert arb.prior_alert_count(conn, "k", "B", now) == 0


def test_run_cycle_batch_single_send(tmp_path, monkeypatch):
    """Two candidates in one cycle -> ONE packed message; both fired+logged."""
    pm_event = {"slug": "nfl-pit-cle-2026-10-01", "title": "Pittsburgh vs. Cleveland",
                "markets": [
                    {"slug": "astatc-nfl-pit-cle-2026-10-01-pyd-aarrod-gte200",
                     "question": "Will Aaron Rodgers record 200+ passing yards?",
                     "sportsMarketType": "football_player_passing_yards", "line": 200,
                     "outcomes": "[\"Yes\",\"No\"]",
                     "outcomePrices": "[\"0.6000\",\"0.6100\"]"},
                    {"slug": "astatc-nfl-pit-cle-2026-10-01-pyd-deswat-gte200",
                     "question": "Will Deshaun Watson record 200+ passing yards?",
                     "sportsMarketType": "football_player_passing_yards", "line": 200,
                     "outcomes": "[\"Yes\",\"No\"]",
                     "outcomePrices": "[\"0.6000\",\"0.6100\"]"},
                ]}
    kal_pass = {"ticker": "KXNFLPASSYDS-26OCT01PITCLE", "title": "Passing yards",
                "markets": [
                    {"title": "Aaron Rodgers: 200+ passing yards", "yes_bid": 74,
                     "yes_ask": 75, "volume": 500000, "status": "active"},
                    {"title": "Deshaun Watson: 200+ passing yards", "yes_bid": 74,
                     "yes_ask": 75, "volume": 500000, "status": "active"},
                ]}
    kal_game = {"ticker": "KXNFLGAME-26OCT01PITCLE", "title": "PIT Steelers vs CLE Browns",
                "markets": []}
    sends = []
    monkeypatch.setattr(arb.time, "sleep", lambda s: None)
    monkeypatch.setattr(arb, "espn_scoreboard", lambda: load("espn_pitcle.json"))
    monkeypatch.setattr(arb, "kalshi_series_events",
                        lambda s: {"KXNFLPASSYDS-26OCT01PITCLE": kal_pass}
                        if s == "KXNFLPASSYDS" else
                        ({"KXNFLGAME-26OCT01PITCLE": kal_game} if s == "KXNFLGAME" else {}))
    monkeypatch.setattr(arb, "pmus_search_event", lambda q, d, a: pm_event)
    monkeypatch.setattr(arb, "pmus_bbo", lambda slug: (60.0, 61.0))
    monkeypatch.setattr(arb, "send_telegram", lambda text, enabled: sends.append(text) or True)
    conn = arb.db_init(str(tmp_path / "arb.db"))
    s = arb.run_cycle(_cfg(str(tmp_path / "arb.db")), conn)
    assert s["alerts"] == 2
    assert len(sends) == 1, "two candidates must pack into one message"
    assert "2 windows" in sends[0]
    assert conn.execute("SELECT COUNT(*) FROM fired").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM pairs_log WHERE alerted=1").fetchone()[0] == 2