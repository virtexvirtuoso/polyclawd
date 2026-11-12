"""Characterization of the PAPER stop loops: evaluate_stops() and evaluate_stops_urgent().

Phase 1B task 1.2c-2 separates the paper loops from the live exit path and deletes
stop tiers that can never fire. The requirement is that paper-trading stop behaviour
does not change at all. This test is the proof: it drives the REAL functions over a
grid of paper positions and compares every decision with expectations that were
GENERATED FROM THE CODE AS IT WAS BEFORE THAT TASK and stored in
tests/fixtures/paper_stop_characterization.json.

What is real: both loops, _close_position_early, _write_heartbeat, _parse_market_date,
_post_lock_threshold, _compute_unrealized_pnl, the paper bookkeeping
(signals.paper_portfolio._get_bankroll/_save_state), the SQLite reads and writes.
What is replaced: the database path (tmp_path), the price source (_fetch_price returns
the grid's price), the clock (se.datetime is frozen so that "hours to close" is exact),
the engine-state toggle read, the resolution logger, and the three notifier calls
(recorded, never sent). The live-position lookup is NOT replaced: the process is PAPER,
so it finds nothing, which is the production path.

Inputs the paper stop decision branches on (all of them are on the grid):
  strategy      selects STOP_CONFIG / URGENT_STOP_CONFIG ("weather", "tweet_count_mc",
                anything else = "default"; NULL = "")
  archetype     only "kalshi_weather_fade" matters (skipped: hold to resolution)
  side          YES / NO (decides how the price turns into a P&L)
  entry_price   0.05 ... 0.95; NULL and 0 are error cases
  bet_size      the loss fraction is relative to it; NULL and 0 are error cases
  current price from the price source; missing (None) skips the position
  title         a date ("on November 20") gives hours-to-close; no date, an impossible
                date and a date in the past (rolls to next year) are all covered
  hours to close  the windows: warning <= 6h, weather defer > 6h, post-lock window,
                urgent <= 6h, urgent weather [2h, 4h] (on the 1-decimal rounded value)
  edge_pct      the edge-decay stop (normalised when > 1)
  post-lock toggle and its two parameters (engine state)
  status        only 'open' rows are looked at
Not decision inputs (proved by cases that vary them): opened_at, platform, market_id.

To regenerate the expectations (only when behaviour is MEANT to change):
  POLYCLAWD_REGEN_STOP_CHAR=1 pytest tests/test_paper_stop_characterization.py
"""

import hashlib
import json
import os
import re
import sqlite3
from copy import deepcopy
from datetime import datetime as _RealDatetime
from datetime import timedelta, timezone
from pathlib import Path

import pytest

import services.stop_evaluator as se

FIXTURE = Path(__file__).parent / "fixtures" / "paper_stop_characterization.json"
TARGET = _RealDatetime(2026, 11, 20, 23, 59, tzinfo=timezone.utc)   # "on November 20"
KNOBS = ("UNIVERSAL_MAX_LOSS_PCT", "WEATHER_HARD_CAP_MAX_LOSS_PCT", "URGENT_WEATHER_MAX_LOSS_PCT",
         "URGENT_MAX_HOURS_TO_CLOSE_WEATHER", "PRE_RESOLVE_WARN_HOURS", "PRE_RESOLVE_WARN_LOSS_PCT")

SCHEMA = """
CREATE TABLE paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL,
    market_id TEXT NOT NULL,
    market_title TEXT,
    platform TEXT DEFAULT 'kalshi',
    side TEXT NOT NULL,
    entry_price REAL,
    bet_size REAL,
    potential_payout REAL,
    confidence REAL,
    edge_pct REAL,
    status TEXT DEFAULT 'open',
    closed_at TEXT,
    exit_price REAL,
    pnl REAL,
    close_reason TEXT,
    strategy TEXT DEFAULT '',
    archetype TEXT DEFAULT '',
    entry_forecast_json TEXT
);
CREATE TABLE paper_portfolio_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    bankroll REAL NOT NULL,
    total_pnl REAL DEFAULT 0,
    total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    max_drawdown REAL DEFAULT 0,
    peak_bankroll REAL NOT NULL,
    current_drawdown_pct REAL DEFAULT 0,
    sharpe_estimate REAL DEFAULT 0
);
"""

MODES = {   # the engine-state toggle read once per tick
    "off": {},
    "on": {"post_lock_stops_enabled": True},
    "on_custom": {"post_lock_stops_enabled": True, "post_lock_weather_hours": 5.0,
                  "post_lock_weather_max_loss": 0.20},
}

TIERS = (   # (name, regex on the reason text the code reports)
    ("universal_urgent", r"^UNIVERSAL STOP \(urgent\)"),
    ("universal", r"^UNIVERSAL STOP loss"),
    ("hard_cap", r"^HARD-CAP"),
    ("post_lock_urgent", r"^POST-LOCK URGENT"),
    ("post_lock", r"^POST-LOCK loss"),
    ("urgent_edge_decay", r"^URGENT edge decay"),
    ("edge_decay", r"^edge decay"),
)


def classify(reason):
    for name, rx in TIERS:
        if re.match(rx, reason):
            return name
    m = re.match(r"^URGENT loss \d+% >= (\d+)%", reason)
    if m:
        return f"urgent_loss_ge{m.group(1)}"
    m = re.match(r"^loss \d+% >= (\d+)% threshold", reason)
    if m:
        return f"check1_ge{m.group(1)}"
    return "UNRECOGNISED:" + reason[:40]


# ----------------------------------------------------------------------------------
# The grid
# ----------------------------------------------------------------------------------

def px(side, entry, loss):
    """The YES price that gives `loss` (a fraction of the bet) for this side."""
    p = entry * (1 - loss) if side == "YES" else 1 - (1 - entry) * (1 - loss)
    return round(min(max(p, 0.0), 1.0), 6)


def P(strategy="", side="YES", entry=0.5, loss=None, price="auto", bet=10.0, edge=0.05,
      archetype="", title="date", opened="2026-11-19T12:00:00+00:00", status="open", extra=""):
    if price == "auto":
        price = px(side, entry, loss)
    label = (f"{strategy!r}|{archetype!r}|{side}|e={entry}|b={bet}|edge={edge}|"
             f"{'L=' + str(loss) if loss is not None else 'p=' + str(price)}|{title}|{status}{extra}")
    return {"label": label, "strategy": strategy, "side": side, "entry": entry, "price": price,
            "bet": bet, "edge": edge, "archetype": archetype, "title": title,
            "opened": opened, "status": status}


STOPS, URGENT = "evaluate_stops", "evaluate_stops_urgent"
STRATS = ["", "weather", "tweet_count_mc", "other_strategy"]
SIDES = ["YES", "NO"]
ENTRIES = [0.05, 0.20, 0.50, 0.80, 0.95]
# gain, flat, and T-0.01 / T / T+0.01 for the tiers 0.12 0.30 0.40 0.50 0.70, plus a deep loss
LOSS_A = [-0.10, 0.0, 0.11, 0.12, 0.13, 0.29, 0.30, 0.31, 0.39, 0.40, 0.41,
          0.49, 0.50, 0.51, 0.69, 0.70, 0.71, 0.90]
# exact hits: price chosen so that abs(unrealized)/bet == T in floating point.
# (side, entry, bet, price). 0.30 has no exact hit on any decimal grid; its neighbours are in LOSS_A.
EXACT = {
    0.12: [("YES", 0.5, 10.0, 0.44), ("NO", 0.1, 10.0, 0.208)],
    0.40: [("YES", 0.5, 10.0, 0.30), ("YES", 0.2, 10.0, 0.12), ("NO", 0.2, 10.0, 0.52)],
    0.50: [("YES", 0.5, 10.0, 0.25), ("NO", 0.5, 10.0, 0.75), ("YES", 0.8, 10.0, 0.40)],
    0.70: [("YES", 0.5, 10.0, 0.15), ("NO", 0.5, 10.0, 0.85)],
}
# (side, entry, edge_pct, price): current_edge == the floor exactly. Stops floor -0.02, urgent floor 0.0.
EDGE_EXACT_STOPS = [("YES", 0.2, -0.2, 0.02)]
EDGE_EXACT_URGENT = [("YES", 0.2, -0.01, 0.19), ("NO", 0.2, -0.01, 0.21), ("YES", 0.5, -0.01, 0.49)]


def _t(h):
    return "nodate" if h is None else "date"


def build_runs():
    runs = []

    def run(grid, fn, hours, mode, positions):
        key = f"{grid}|{fn}|H={hours}|{mode}"
        labels = [p["label"] for p in positions]
        assert len(labels) == len(set(labels)), f"duplicate case label in {key}"
        runs.append({"key": key, "fn": fn, "hours": hours, "mode": mode, "positions": positions})

    # A: every tier on both sides of every threshold, every strategy, side and entry price
    for fn, hours in ((STOPS, None), (STOPS, 12.0), (STOPS, 3.5), (URGENT, 5.0), (URGENT, 3.0)):
        run("A", fn, hours, "off",
            [P(s, side, e, L, title=_t(hours)) for s in STRATS for side in SIDES
             for e in ENTRIES for L in LOSS_A])

    # B: time windows. Each hour value sits on a boundary of some window.
    hb_stops = [-1.0, 0.0, 0.001, 2.999, 3.0, 3.001, 4.999, 5.0, 5.001, 5.999, 6.0, 6.001, 24.0, 200.0]
    hb_urgent = [-1.0, 0.0, 0.001, 1.94, 1.95, 1.96, 2.0, 2.999, 3.0, 3.001, 4.0, 4.04, 4.05, 4.1,
                 5.0, 5.001, 5.95, 5.999, 6.0, 6.001, 6.04]
    lb = [(0.03, -0.2), (0.12, 0.05), (0.13, 0.05), (0.21, 0.05), (0.35, 0.05), (0.45, 0.05), (0.75, 0.05)]
    for fn, hs in ((STOPS, hb_stops), (URGENT, hb_urgent)):
        for h in hs:
            for mode in MODES:
                run("B", fn, h, mode, [P(s, "YES", 0.5, L, edge=ed) for s in ("weather", "")
                                       for (L, ed) in lb])

    # C: edge decay
    edges = [None, 0.0, -0.01, -0.02, -0.05, -0.2, 0.05, 5.0, 1.0]
    for fn, hours in ((STOPS, None), (URGENT, 3.0)):
        run("C", fn, hours, "off",
            [P(s, side, e, L, edge=ed, title=_t(hours)) for s in ("", "weather", "other_strategy")
             for side in SIDES for e in (0.2, 0.5) for ed in edges for L in (-0.05, 0.0, 0.02, 0.10, 0.25)])

    # E: exact threshold hits and their neighbours on the 0.001 price grid
    def exact_positions(only_012=False):
        out = []
        for T, combos in EXACT.items():
            if only_012 and T != 0.12:
                continue
            for s in ("", "weather"):
                for side, e, bet, p in combos:
                    for dp in (-0.001, 0.0, 0.001):
                        out.append(P(s, side, e, price=round(p + dp, 6), bet=bet, extra=f"|T={T}"))
        return out
    for fn, hours, mode in ((STOPS, None, "off"), (STOPS, 3.5, "off"), (STOPS, 12.0, "off"),
                            (URGENT, 3.0, "off"), (URGENT, 5.0, "off")):
        run("E", fn, hours, mode, [dict(p, title=_t(hours)) for p in exact_positions()])
    run("E", STOPS, None, "on", [dict(p, title="nodate") for p in exact_positions(only_012=True)])
    run("E", URGENT, 3.0, "on", exact_positions(only_012=True))
    for fn, hours, hits in ((STOPS, None, EDGE_EXACT_STOPS), (URGENT, 3.0, EDGE_EXACT_URGENT)):
        run("E2", fn, hours, "off",
            [P("", side, e, price=round(p + dp, 6), edge=ed, title=_t(hours))
             for side, e, ed, p in hits for dp in (-0.001, 0.0, 0.001)])

    # F: titles, bet sizes, archetypes, opened_at, other statuses, missing and zero values
    for fn, hours in ((STOPS, 3.0), (URGENT, 3.0)):
        run("F-title", fn, hours, "off",
            [P(s, "YES", 0.5, L, title=t) for s in ("", "weather") for L in (0.35, 0.45)
             for t in ("date", "nodate", "badday", "past")])
        run("F-misc", fn, hours, "off",
            [P("", "YES", 0.5, 0.45, bet=b, extra="|bet") for b in (1.0, 7.5, 100.0)]
            + [P("", "YES", 0.5, 0.35, bet=b, extra="|bet") for b in (1.0, 7.5, 100.0)]
            + [P(s, "YES", 0.5, L, archetype=a)
               for s in ("", "weather") for a in ("kalshi_weather_fade", "weather", "tweet_count_mc", None)
               for L in (0.05, 0.35, 0.45)]
            + [P("", "YES", 0.5, 0.45, opened=o, extra=f"|opened={o}") for o in
               ("2020-01-01T00:00:00+00:00", "2026-11-20T23:00:00+00:00", "")]
            + [P("", "YES", 0.5, 0.45, status=st) for st in ("won", "lost", "stopped", "void")]
            + [P(None, "YES", 0.5, L, extra="|nullstrat") for L in (0.05, 0.35, 0.45)]
            + [P("", "YES", 0.5, price=None, extra="|noprice"), P("", "NO", 0.5, price=None, extra="|noprice")]
            + [P("", "YES", 0.5, price=0.0, extra="|p0"), P("", "YES", 0.5, price=1.0, extra="|p1"),
               P("", "NO", 0.5, price=0.0, extra="|p0"), P("", "NO", 0.5, price=1.0, extra="|p1")]
            + [P("", "NO", 1.0, 0.45, extra="|entry1"), P("", "NO", 0.0, 0.45, extra="|entry0no")]
            + [P("", "YES", 1.0, price=0.5, extra="|entry1yes")]
            + [P("", "YES", 0.5, 0.0, bet=0.0, extra="|bet0"), P("", "NO", 0.5, 0.0, bet=0.0, extra="|bet0")])
    # F-err: a position that makes the loop raise (one per run: the exception ends the run)
    for fn, hours in ((STOPS, 3.0), (URGENT, 3.0)):
        for name, p in (
            ("entry_null_yes", P("", "YES", None, price=0.3)),
            ("entry_null_no", P("", "NO", None, price=0.3)),
            ("entry_zero_yes", P("", "YES", 0.0, price=0.3)),
            ("bet_null", P("", "YES", 0.5, 0.45, bet=None)),
            ("price_text", P("", "YES", 0.5, price="x")),
            ("edge_text", P("", "YES", 0.5, 0.45, edge="x")),
        ):
            run(f"F-err-{name}", fn, hours, "off", [p])
    # F-empty: nothing open
    for fn in (STOPS, URGENT):
        run("F-empty", fn, 3.0, "off", [])
    # F-mix: a raising position behind an ordinary stop in the same run (order matters)
    run("F-mix", STOPS, 3.0, "off", [P("", "YES", 0.5, 0.45), P("", "YES", None, price=0.3), P("", "YES", 0.5, 0.46)])
    run("F-mix", URGENT, 3.0, "off", [P("", "YES", 0.5, 0.45), P("", "YES", None, price=0.3), P("", "YES", 0.5, 0.46)])
    return runs


# ----------------------------------------------------------------------------------
# Running one batch of positions through the real function
# ----------------------------------------------------------------------------------

class _Frozen(_RealDatetime):
    _now = TARGET

    @classmethod
    def now(cls, tz=None):
        return cls._now if tz is not None else cls._now.replace(tzinfo=None)


def _sha(obj):
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:10]


def _tag_of(text):
    m = re.search(r"#(\d+) ", text)
    return int(m.group(1)) if m else None


TITLES = {"date": "Weather market on November 20?", "nodate": "Will it happen?",
          "badday": "Weather market on February 30?", "past": "Weather market on May 1?"}


def execute(run, tmp_path, monkeypatch):
    import scripts.alert_formatter as af
    import scripts.openclaw_alerts as oa
    import signals.discord_alerts as da
    import signals.resolution_logger as rl

    db = tmp_path / "run.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO paper_portfolio_state (timestamp, bankroll, peak_bankroll)"
                 " VALUES ('2026-11-01T00:00:00+00:00', 1000.0, 1000.0)")
    by_id = {}
    for i, p in enumerate(run["positions"], start=1):
        by_id[i] = p
        conn.execute(
            "INSERT INTO paper_positions (id, opened_at, market_id, market_title, platform, side,"
            " entry_price, bet_size, edge_pct, status, strategy, archetype, pnl)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, p["opened"], f"KXTEST-{i}",
             f"#{i} " + TITLES["nodate" if run["hours"] is None and p["title"] == "date" else p["title"]],
             "kalshi", p["side"], p["entry"], p["bet"], p["edge"], p["status"], p["strategy"], p["archetype"],
             None if p["status"] == "open" else 0.0))   # closed rows carry a pnl, as real ones do
    conn.commit()
    conn.close()

    hours = 100.0 if run["hours"] is None else run["hours"]
    _Frozen._now = TARGET - timedelta(hours=hours)
    prices = {i: p["price"] for i, p in by_id.items()}
    alerts, rlog = [], []

    def rec(channel, text, payload):
        alerts.append((_tag_of(text), channel, _sha(payload)))

    monkeypatch.setattr(se, "DB_PATH", db)
    monkeypatch.setattr(se, "datetime", _Frozen)
    monkeypatch.setattr(se, "_load_engine_state", lambda: dict(MODES[run["mode"]]))
    monkeypatch.setattr(se, "_fetch_price", lambda pos: (pos["id"], prices.get(pos["id"])))
    monkeypatch.setattr(rl, "log_position_close",
                        lambda pos, won, pnl, close_reason, closing_line=None:
                        rlog.append((pos["id"], won, pnl, close_reason, closing_line)))
    monkeypatch.setattr(da, "_portfolio_context",
                        lambda: {"bankroll": 1000.0, "record": "0W/0L", "open": 0, "won": 0,
                                 "lost": 0, "wr": 0, "at_risk": 0})

    def fake_send(embeds, **kw):
        text = json.dumps(embeds, default=str)
        stripped = [{k: v for k, v in e.items() if k != "timestamp"} for e in embeds]
        rec("discord", text, {"embeds": stripped, "kw": kw})
        return True

    monkeypatch.setattr(da, "_send", fake_send)
    monkeypatch.setattr(oa, "alert_openclaw",
                        lambda msg, **kw: rec("telegram_stop", msg, {"msg": msg, "kw": kw}) or True)
    monkeypatch.setattr(af, "send_telegram",
                        lambda msg, *a, **kw: rec("telegram_warn", msg, {"msg": msg, "a": a, "kw": kw}) or True)

    saved_post_lock = deepcopy(se.POST_LOCK_CONFIG)
    error = None
    ret = None
    try:
        ret = getattr(se, run["fn"])()
    except Exception as exc:                               # noqa: BLE001 - recorded, not hidden
        error = type(exc).__name__
    finally:
        se.POST_LOCK_CONFIG.clear()
        se.POST_LOCK_CONFIG.update(saved_post_lock)

    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    rows = {r["id"]: r for r in c.execute("SELECT * FROM paper_positions")}
    state = c.execute("SELECT bankroll, total_pnl, total_trades, wins, losses, peak_bankroll"
                      " FROM paper_portfolio_state ORDER BY id DESC LIMIT 1").fetchone()
    nstate = c.execute("SELECT COUNT(*) FROM paper_portfolio_state").fetchone()[0]
    try:
        hb = c.execute("SELECT id, positions_checked, warnings_fired, ts IS NOT NULL"
                       " FROM stop_heartbeat").fetchall()
        heartbeat = [list(r) for r in hb]
    except sqlite3.OperationalError:
        heartbeat = None
    c.close()

    returned = {}
    for r in (ret or []):
        returned[r["position_id"]] = r
    cases = {}
    for i, p in by_id.items():
        row = rows[i]
        mine = [(ch, h) for (t, ch, h) in alerts if t == i]
        stopped = i in returned
        reason = returned[i]["reason"] if stopped else None
        cases[p["label"]] = {
            "stopped": stopped,
            "tier": classify(reason) if stopped else None,
            "ret": _sha(returned[i]) if stopped else None,
            "row": [row["status"], row["close_reason"], row["exit_price"], row["pnl"],
                    row["closed_at"] is not None],
            "warn": any(ch == "telegram_warn" for ch, _ in mine),
            "alerts": [list(a) for a in mine],
            "rlog": [list(x[1:]) for x in rlog if x[0] == i] or None,
        }
    return {
        "error": error,
        "returned_ids": None if ret is None else [r["position_id"] for r in ret],
        "heartbeat": heartbeat,
        "state": None if state is None else [round(x, 6) if isinstance(x, float) else x for x in state],
        "state_rows": nstate,
        "stray_alerts": [a for a in alerts if a[0] not in by_id],
        "cases": cases,
    }


def run_everything(tmp_path, monkeypatch):
    for k in KNOBS:
        assert os.environ.get(k) is None, f"{k} is set: the grid assumes the code defaults"
    assert se.UNIVERSAL_MAX_LOSS_PCT == 0.40
    from execution import live_config
    assert not live_config.live_armed(), "characterization is of the PAPER path"
    out = {}
    for r in build_runs():
        sub = tmp_path / re.sub(r"[^A-Za-z0-9]+", "_", r["key"])
        sub.mkdir()
        assert r["key"] not in out
        out[r["key"]] = execute(r, sub, monkeypatch)
    return out


def test_paper_stop_decisions_are_unchanged(tmp_path, monkeypatch):
    got = run_everything(tmp_path, monkeypatch)
    if os.environ.get("POLYCLAWD_REGEN_STOP_CHAR") == "1":
        FIXTURE.parent.mkdir(exist_ok=True)
        FIXTURE.write_text(json.dumps(got, sort_keys=True, separators=(",", ":")) + "\n")
        pytest.skip(f"regenerated {FIXTURE}")
    want = json.loads(FIXTURE.read_text())
    assert set(got) == set(want), "the set of runs changed"
    diffs = []
    for key in sorted(want):
        g = json.loads(json.dumps(got[key], sort_keys=True))
        w = want[key]
        for field in ("error", "returned_ids", "heartbeat", "state", "state_rows", "stray_alerts"):
            if g[field] != w[field]:
                diffs.append((key, field, w[field], g[field]))
        assert set(g["cases"]) == set(w["cases"]), f"{key}: the set of cases changed"
        for label, wc in w["cases"].items():
            if g["cases"][label] != wc:
                diffs.append((key, label, wc, g["cases"][label]))
    assert not diffs, f"{len(diffs)} differences, first 5: {diffs[:5]}"


def test_grid_size_and_tiers_that_fire():
    """Reads the stored expectations: how big the grid is, and which tiers ever fired."""
    want = json.loads(FIXTURE.read_text())
    n = sum(len(r["cases"]) for r in want.values())
    assert n >= 5000
    fired = {}
    for r in want.values():
        for c in r["cases"].values():
            if c["tier"]:
                fired[c["tier"]] = fired.get(c["tier"], 0) + 1
            if c["warn"]:
                fired["pre_resolve_warning"] = fired.get("pre_resolve_warning", 0) + 1
    for alive in ("universal", "universal_urgent", "post_lock", "post_lock_urgent",
                  "urgent_edge_decay", "edge_decay", "pre_resolve_warning", "urgent_loss_ge30"):
        assert fired.get(alive, 0) > 0, f"{alive} never fired on the grid"
    assert not [t for t in fired if t.startswith("UNRECOGNISED")]
    # the tiers the universal 40% stop shadows: never fire anywhere on the grid
    for dead in ("hard_cap", "check1_ge50", "check1_ge70", "urgent_loss_ge40"):
        assert fired.get(dead, 0) == 0, f"{dead} fired {fired[dead]} times"
