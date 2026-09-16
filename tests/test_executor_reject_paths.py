"""soccer_executor / weather_executor: rejection paths must not raise NameError.

Both executors logged `client_order_ref` in reject branches (close-window /
velocity gates) that ran BEFORE the variable was assigned later in the loop
body. Since these gates sit outside any try/except, a rejection raised
NameError and aborted the remaining edges in that scan — silently truncating
it. Fixed by building client_order_ref before the first gate that logs it.
"""
import execution.live_config as live_config
import execution.live_db as live_db
import execution.soccer_executor as soccer_executor
import execution.weather_executor as weather_executor
import odds.poly_executable_edge as poly_executable_edge


def _patch_live_db(monkeypatch, tmp_path, name):
    """Route live_db.connect() at a fresh tmp sqlite file instead of the prod DB."""
    db_path = tmp_path / f"{name}.db"
    real_connect = live_db.connect
    monkeypatch.setattr(live_db, "connect", lambda path=db_path: real_connect(path))


class _Edge:
    """Minimal stand-in for sports_edge_common.Edge (attribute access, not dict)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_soccer_executor_close_window_reject_no_namerror(tmp_path, monkeypatch):
    """Close-window gate rejects the only edge -> must log client_order_ref
    (previously undefined there) and return clean stats, not raise."""
    monkeypatch.setattr(live_config, "mode", lambda: "LIVE")
    monkeypatch.setattr(
        live_config, "in_close_window", lambda mins, category: (False, "outside window (test)")
    )
    monkeypatch.setattr(
        poly_executable_edge, "condition_id_to_token_ids", lambda cid: ["tok_yes", "tok_no"]
    )
    _patch_live_db(monkeypatch, tmp_path, "soccer")

    edge = _Edge(
        tradeable=True,
        poly_market_id="0xcondition",
        direction="BUY",
        participant="Arsenal",
        market_type="home",
        commence_time="2026-08-20T12:00:00Z",
        poly_event_id="evt1",
        event_title="Arsenal vs Chelsea",
        book_prob=0.55,
        executable_edge=0.1,
        edge_pct=0.1,
        fillable_usd=50.0,
        tick_size=0.01,
    )

    stats = soccer_executor.execute_tradeable_soccer_edges([edge])

    assert stats["dropped"] >= 1
    assert stats["errors"] == 0
    assert stats["filled"] == 0


def test_soccer_executor_velocity_reject_no_namerror(tmp_path, monkeypatch):
    """Velocity gate rejects (close-window passes first) -> second buggy
    log line in the original code, also previously undefined there."""
    monkeypatch.setattr(live_config, "mode", lambda: "LIVE")
    monkeypatch.setattr(live_config, "in_close_window", lambda mins, category: (True, ""))
    monkeypatch.setattr(
        live_config, "velocity_check", lambda **kw: (False, "collapsing edge (test)")
    )
    monkeypatch.setattr(
        poly_executable_edge, "condition_id_to_token_ids", lambda cid: ["tok_yes", "tok_no"]
    )
    _patch_live_db(monkeypatch, tmp_path, "soccer_vel")

    edge = _Edge(
        tradeable=True,
        poly_market_id="0xcondition",
        direction="BUY",
        participant="Arsenal",
        market_type="home",
        commence_time="2026-08-20T12:00:00Z",
        poly_event_id="evt1",
        event_title="Arsenal vs Chelsea",
        book_prob=0.55,
        executable_edge=0.1,
        edge_pct=0.1,
        fillable_usd=50.0,
        tick_size=0.01,
    )

    stats = soccer_executor.execute_tradeable_soccer_edges([edge])

    assert stats["dropped"] >= 1
    assert stats["errors"] == 0
    assert stats["filled"] == 0


def test_weather_executor_velocity_reject_no_namerror(tmp_path, monkeypatch):
    """The original bug: velocity gate rejects and logs client_order_ref,
    which was assigned AFTER this point (close-window gate must pass first
    so we actually reach the velocity check)."""
    monkeypatch.setattr(live_config, "mode", lambda: "LIVE")
    monkeypatch.setattr(live_config, "in_close_window", lambda mins, category: (True, ""))
    monkeypatch.setattr(
        live_config, "velocity_check", lambda **kw: (False, "collapsing edge (test)")
    )
    monkeypatch.setattr(
        poly_executable_edge, "condition_id_to_token_ids", lambda cid: ["tok_yes", "tok_no"]
    )
    _patch_live_db(monkeypatch, tmp_path, "weather")

    sig = {
        "condition_id": "0xcondition2",
        "direction": "buy_no",
        "edge_pp": 8.0,
        "market_price": 0.3,
        "city": "Miami",
        "market_title": "Miami High Temp",
        "twc_implied_prob": 0.6,
        "end_date": "2026-08-20T18:00:00Z",
    }

    stats = weather_executor.execute_tradeable_weather_edges([sig])

    assert stats["dropped"] >= 1
    assert stats["errors"] == 0
    assert stats["filled"] == 0
