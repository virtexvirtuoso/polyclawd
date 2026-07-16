"""Task 3.3 — wire odds.gamma_title.resolve_title into stop_evaluator
fallback sites so alerts show market names instead of hex condition ids.

Sites: the live-close market_title fallback (~line 368), the stop-close
result dict (~line 394), and the ⚠️ pre-resolution warning (~line 609).
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import odds.gamma_title as gt
import services.stop_evaluator as se
from tests.test_stop_thresholds import SCHEMA, insert_pos

HEX_ID = "0x" + "ab" * 20
RESOLVED = "Will it rain in NYC on July 20?"


# ── _display_title unit behavior ──────────────────────────────────────────

def test_display_title_keeps_human_title_without_resolving(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("resolver must not be called for human titles")
    monkeypatch.setattr(gt, "resolve_title", boom)
    assert se._display_title("Nice human title?", HEX_ID) == "Nice human title?"


def test_display_title_resolves_empty_title(monkeypatch):
    monkeypatch.setattr(gt, "resolve_title", lambda mid, db_path=None: RESOLVED)
    assert se._display_title("", HEX_ID) == RESOLVED
    assert se._display_title(None, HEX_ID) == RESOLVED


def test_display_title_resolves_hex_like_title(monkeypatch):
    monkeypatch.setattr(gt, "resolve_title", lambda mid, db_path=None: RESOLVED)
    assert se._display_title(HEX_ID, HEX_ID) == RESOLVED


def test_display_title_falls_back_to_truncated_id(monkeypatch):
    monkeypatch.setattr(gt, "resolve_title", lambda mid, db_path=None: None)
    assert se._display_title("", HEX_ID) == HEX_ID[:24]
    # hex-like raw title survives as last resort over the id
    assert se._display_title(HEX_ID, HEX_ID) == HEX_ID


# ── warning site (~609) ───────────────────────────────────────────────────

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
    return db


def test_warning_resolves_hex_title(stop_db, monkeypatch):
    insert_pos(stop_db, market_id=HEX_ID, market_title=HEX_ID,
               platform="polymarket")
    monkeypatch.setattr(se, "_fetch_price", lambda pos: (pos["id"], 0.325))
    monkeypatch.setattr(
        se, "_parse_market_date",
        lambda title: datetime.now(timezone.utc) + timedelta(hours=3),
    )
    monkeypatch.setattr(gt, "resolve_title", lambda mid, db_path=None: RESOLVED)
    sent = []
    import scripts.alert_formatter as af
    monkeypatch.setattr(af, "send_telegram",
                        lambda msg, *a, **k: sent.append(msg) or True)

    se.evaluate_stops()

    assert len(sent) == 1
    assert RESOLVED in sent[0]
    assert HEX_ID not in sent[0]


# ── live-close sites (~368 fallback + ~394 result dict) ───────────────────

def test_live_close_resolves_title_for_alert_and_dict(monkeypatch):
    import execution.live_db as ldb
    import execution.risk_governor as rg
    import execution.live_executor as lx
    import odds.polymarket_clob as pclob
    from execution import clob_client
    import scripts.alert_formatter as af
    import scripts.smart_wallet_fast_poll as swfp

    class FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(ldb, "connect", lambda: FakeConn())
    monkeypatch.setattr(rg, "RiskGovernor", lambda conn, mode=None: object())
    monkeypatch.setattr(lx, "execute_exit", lambda *a, **k: {
        "pnl": -4.6, "action": "taker_closed", "shares_sold": 20.0,
        "exit_price": 0.27, "fee_paid": 0.0,
    })
    monkeypatch.setattr(pclob, "get_orderbook", lambda token_id: None)
    monkeypatch.setattr(clob_client, "get_tick_size", lambda t: 0.01)
    monkeypatch.setattr(swfp, "register_exit_cooldown", lambda t: None)
    monkeypatch.setattr(gt, "resolve_title", lambda mid, db_path=None: RESOLVED)
    sent = []
    monkeypatch.setattr(af, "send_telegram",
                        lambda msg, *a, **k: sent.append(msg) or True)

    row = {
        "id": 1, "market_id": HEX_ID, "token_id": "123",
        "market_title": "",  # empty -> must resolve, not show hex/blank
        "entry_price": 0.50, "shares": 20.0, "cost_usd": 10.0,
        "side": "BUY", "archetype": "weather",
    }
    info = se._close_live_position_early(row, 0.27, "UNIVERSAL STOP test",
                                         hard_cap_frac=0.40)

    assert info is not None
    assert info["market_title"] == RESOLVED
    assert len(sent) == 1
    assert RESOLVED in sent[0]
