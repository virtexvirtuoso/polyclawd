"""Task 2.0 — dead-warning fix + Telegram routing for stop-closes.

Proves:
(a) a position at -35% loss near resolution fires the ⚠️ pre-resolution
    warning (warning threshold is now BELOW the 40% universal stop);
(b) a position at -46% loss sends a 🛑 stop-close alert through the
    hardened Telegram sender (alert_openclaw), not only Discord.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import services.stop_evaluator as se

SCHEMA = """
CREATE TABLE paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL,
    market_id TEXT NOT NULL,
    market_title TEXT,
    platform TEXT DEFAULT 'kalshi',
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    bet_size REAL NOT NULL,
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


def insert_pos(db, **overrides):
    row = {
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "market_id": "KXTEST-STOPS-1",
        "market_title": "Fake test market on July 20?",
        "platform": "kalshi",
        "side": "YES",
        "entry_price": 0.50,
        "bet_size": 10.0,
        "edge_pct": 0.05,
        "status": "open",
        "strategy": "",
        "archetype": "",
    }
    row.update(overrides)
    conn = sqlite3.connect(db)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO paper_positions ({cols}) VALUES ({marks})",
                 tuple(row.values()))
    conn.commit()
    conn.close()


@pytest.fixture
def stop_db(tmp_path, monkeypatch):
    db = tmp_path / "shadow_trades.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO paper_portfolio_state (timestamp, bankroll, peak_bankroll)"
        " VALUES (?, 1000.0, 1000.0)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(se, "DB_PATH", db)
    monkeypatch.setattr(se, "_load_engine_state", lambda: {})
    monkeypatch.setattr(se, "_get_live_position", lambda market_id: None)
    monkeypatch.setattr(se, "_send_discord_alert", lambda info: None)
    import signals.resolution_logger as rl
    monkeypatch.setattr(rl, "log_position_close", lambda *a, **k: None)
    return db


def test_warn_threshold_default_is_below_universal_stop():
    # Structural fix: 0.40/0.40 made the warning branch unreachable.
    assert se.PRE_RESOLVE_WARN_LOSS_PCT == pytest.approx(0.30)
    assert se.PRE_RESOLVE_WARN_LOSS_PCT < se.UNIVERSAL_MAX_LOSS_PCT


def test_warning_fires_at_minus_35pct_near_resolution(stop_db, monkeypatch):
    insert_pos(stop_db)
    # -35% loss: entry 0.50 -> current 0.325
    monkeypatch.setattr(se, "_fetch_price", lambda pos: (pos["id"], 0.325))
    monkeypatch.setattr(
        se, "_parse_market_date",
        lambda title: datetime.now(timezone.utc) + timedelta(hours=3),
    )
    sent = []
    import scripts.alert_formatter as af
    monkeypatch.setattr(af, "send_telegram",
                        lambda msg, *a, **k: sent.append(msg) or True)

    stopped = se.evaluate_stops()

    assert stopped == []  # -35% must NOT close (universal stop is 40%)
    assert len(sent) == 1
    assert "PRE-RESOLUTION WARNING" in sent[0]
    # position stays open
    conn = sqlite3.connect(stop_db)
    status = conn.execute("SELECT status FROM paper_positions").fetchone()[0]
    conn.close()
    assert status == "open"


def test_universal_stop_sends_telegram_close(stop_db, monkeypatch):
    insert_pos(stop_db)
    # -46% loss: entry 0.50 -> current 0.27
    monkeypatch.setattr(se, "_fetch_price", lambda pos: (pos["id"], 0.27))
    sent = []
    import scripts.openclaw_alerts as oa
    monkeypatch.setattr(oa, "alert_openclaw",
                        lambda msg, **kw: sent.append((msg, kw)) or True)

    stopped = se.evaluate_stops()

    assert len(stopped) == 1
    assert "UNIVERSAL STOP" in stopped[0]["reason"]
    assert len(sent) == 1
    msg, kw = sent[0]
    assert "🛑" in msg
    assert "Fake test market" in msg
    assert kw.get("parse_mode") is None  # plain text
    conn = sqlite3.connect(stop_db)
    status = conn.execute("SELECT status FROM paper_positions").fetchone()[0]
    conn.close()
    assert status == "stopped"


def test_urgent_universal_stop_sends_telegram_close(stop_db, monkeypatch):
    insert_pos(stop_db)
    monkeypatch.setattr(se, "_fetch_price", lambda pos: (pos["id"], 0.27))
    monkeypatch.setattr(
        se, "_parse_market_date",
        lambda title: datetime.now(timezone.utc) + timedelta(hours=3),
    )
    sent = []
    import scripts.openclaw_alerts as oa
    monkeypatch.setattr(oa, "alert_openclaw",
                        lambda msg, **kw: sent.append((msg, kw)) or True)

    stopped = se.evaluate_stops_urgent()

    assert len(stopped) == 1
    assert "UNIVERSAL STOP (urgent)" in stopped[0]["reason"]
    assert len(sent) == 1
    assert "🛑" in sent[0][0]
