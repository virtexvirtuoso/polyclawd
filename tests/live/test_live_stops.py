"""Task 1.2a: evaluate_live_stops, the live stop-loss decision as a pure function.

The mark is the best bid of the held token; the same number goes to the exit.
Every open position gets exactly one entry. Nothing here sells: the exit
function is a recorder.
"""

import sqlite3
from decimal import Decimal
from fractions import Fraction

import pytest

from execution import live_config, live_db as live_db_module
from odds.polymarket_clob import OrderBook, OrderBookLevel
from services import stop_evaluator

TOKEN = "tok-{}"


def make_book(token_id, bids, asks):
    """A real OrderBook, built the way odds.polymarket_clob.get_orderbook builds
    it: bids best (highest) first, asks best (lowest) first."""
    bl = [OrderBookLevel(price=p, size=100.0) for p in bids]
    al = [OrderBookLevel(price=p, size=100.0) for p in asks]
    best_bid = bl[0].price if bl else 0
    best_ask = al[0].price if al else 1
    return OrderBook(
        market_id="m", token_id=token_id, outcome="Yes", bids=bl, asks=al,
        spread=round(best_ask - best_bid, 4),
        mid_price=round((best_bid + best_ask) / 2, 4) if bl and al else 0.5,
        timestamp="2026-11-11T00:00:00",
    )


class World:
    """Books by token id, a recorder over the exit function, and a call log
    for get_orderbook."""

    def __init__(self):
        self.books = {}
        self.book_calls = []
        self.exit_calls = []
        self.exit_result = "default"
        self.exit_exc = None

    def fake_get_orderbook(self, token_id):
        self.book_calls.append(token_id)
        res = self.books.get(token_id)
        if isinstance(res, Exception):
            raise res
        return res

    def fake_exit(self, row, mark, reason, hard_cap_frac=0.50):
        self.exit_calls.append((dict(row), mark, reason, hard_cap_frac))
        if self.exit_exc is not None:
            raise self.exit_exc
        if self.exit_result == "default":
            return {"action": "closed", "pnl": -1.0}
        return self.exit_result


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setattr("odds.polymarket_clob.get_orderbook", w.fake_get_orderbook)
    monkeypatch.setattr(stop_evaluator, "_close_live_position_early", w.fake_exit)
    monkeypatch.delenv("POLYCLAWD_STOP_EXEMPT_IDS", raising=False)
    return w


def add_position(db, entry, *, token=None, shares=10.0, status="open"):
    cur = db.conn.execute(
        "INSERT INTO live_positions (market_id, token_id, side, entry_price, shares,"
        " cost_usd, status) VALUES (?, ?, 'YES', ?, ?, ?, ?)",
        ("mkt", token or "pending", entry, shares, 1.0, status),
    )
    pid = cur.lastrowid
    if token is None:
        db.conn.execute("UPDATE live_positions SET token_id=? WHERE id=?",
                        (TOKEN.format(pid), pid))
    db.conn.commit()
    return pid


def evaluate(db, **kw):
    return stop_evaluator.evaluate_live_stops(db_path=db.path, **kw)


# ---------------------------------------------------------------------------
# Price and threshold cases (SPLIT-1.2.md Part 1.2a list)
# ---------------------------------------------------------------------------

PRICE_CASES = [
    # id,            entry, bid,   ask,   action,         loss,   spread, threshold
    ("prod26",       0.24,  0.08,  0.09,  "would_exit",   0.6667, 0.0417, 0.4417),
    ("held",         0.20,  0.11,  0.13,  "spread_held",  0.45,   0.1,    0.5),
    ("inclusive",    0.20,  0.10,  0.13,  "would_exit",   0.5,    0.15,   0.5),
    ("tight",        0.50,  0.28,  0.29,  "would_exit",   0.44,   0.02,   0.42),
    ("crossed",      0.20,  0.11,  0.10,  "would_exit",   0.45,   0.0,    0.4),
]


@pytest.mark.parametrize("name,entry,bid,ask,action,loss,spread,thr", PRICE_CASES,
                         ids=[c[0] for c in PRICE_CASES])
def test_price_cases_dry_run(live_db, world, name, entry, bid, ask, action, loss, spread, thr):
    pid = add_position(live_db, entry)
    world.books[TOKEN.format(pid)] = make_book(TOKEN.format(pid), [bid], [ask])
    out = evaluate(live_db, dry_run=True)
    assert len(out) == 1
    e = out[0]
    assert e["id"] == pid
    assert e["action"] == action
    assert e["mark"] == bid
    assert e["loss_pct"] == loss
    assert e["spread_pct"] == spread
    assert e["threshold"] == thr
    assert world.exit_calls == []


def test_exit_called_once_with_row_and_exact_mark_when_armed(live_db, world, armed):
    pid = add_position(live_db, 0.24)
    tok = TOKEN.format(pid)
    world.books[tok] = make_book(tok, [0.08, 0.07], [0.09, 0.10])
    out = evaluate(live_db)
    assert world.book_calls == [tok]                      # one fetch for this position
    assert len(world.exit_calls) == 1
    row, mark, reason, cap = world.exit_calls[0]
    assert row["id"] == pid and row["token_id"] == tok and row["entry_price"] == 0.24
    assert mark == 0.08 and type(mark) is float           # the decision's number, not a mid
    assert reason == "live stop 67%"
    assert cap == stop_evaluator.UNIVERSAL_MAX_LOSS_PCT
    # the returned entry carries the exit's result and the decision's figures
    assert out == [{"id": pid, "mark": 0.08, "loss_pct": 0.6667, "spread_pct": 0.0417,
                    "threshold": 0.4417, "action": "closed", "pnl": -1.0}]


def test_exit_result_is_not_overwritten_by_merge(live_db, world, armed):
    pid = add_position(live_db, 0.24)
    tok = TOKEN.format(pid)
    world.books[tok] = make_book(tok, [0.08], [0.09])
    world.exit_result = {"id": pid, "action": "held_remainder", "mark": 0.5, "extra": 1}
    out = evaluate(live_db)
    assert out[0]["action"] == "held_remainder"
    assert out[0]["mark"] == 0.5            # what the exit returned wins
    assert out[0]["extra"] == 1
    assert out[0]["loss_pct"] == 0.6667     # missing figures are merged in
    assert out[0]["threshold"] == 0.4417


def test_spread_held_then_one_sided_book_is_unpriced(live_db, world, armed):
    pid = add_position(live_db, 0.20)
    tok = TOKEN.format(pid)
    world.books[tok] = make_book(tok, [0.11], [0.13])
    assert evaluate(live_db)[0]["action"] == "spread_held"
    world.books[tok] = make_book(tok, [0.11], [])
    assert evaluate(live_db)[0]["action"] == "unpriced"
    assert world.exit_calls == []


# ---------------------------------------------------------------------------
# Boundaries and float traps
# ---------------------------------------------------------------------------

def one(db, world, entry, bids, asks, **kw):
    pid = add_position(db, entry)
    tok = TOKEN.format(pid)
    world.books[tok] = make_book(tok, bids, asks)
    return evaluate(db, dry_run=True, **kw)[-1]      # the position just added


def test_loss_exactly_40_with_zero_spread_exits(live_db, world):
    e = one(live_db, world, 0.50, [0.30], [0.30])
    assert e["loss_pct"] == 0.4 and e["action"] == "would_exit"


def test_loss_just_under_40_is_ok(live_db, world):
    e = one(live_db, world, 1.0, [0.6001], [0.7])   # loss 0.3999
    assert e["loss_pct"] == 0.3999 and e["action"] == "ok"
    assert "threshold" not in e


def test_spread_exactly_at_cap_allows_to_50(live_db, world):
    # spread = 0.02 / 0.20 = 0.10 exactly at the cap; loss 0.45 < 0.50
    e = one(live_db, world, 0.20, [0.11], [0.13])
    assert e["spread_pct"] == 0.1 and e["threshold"] == 0.5 and e["action"] == "spread_held"


def test_spread_far_above_cap_still_exits_at_50_loss(live_db, world):
    e = one(live_db, world, 0.40, [0.20], [0.90])   # spread 175 points of entry
    assert e["loss_pct"] == 0.5 and e["spread_pct"] == 1.75
    assert e["threshold"] == 0.5 and e["action"] == "would_exit"


def test_synthetic_positive_entry_099_bid_049(live_db, world):
    e = one(live_db, world, 0.99, [0.49], [0.60])
    assert e["action"] == "would_exit" and e["loss_pct"] > 0.5


def test_synthetic_negative_entry_equals_best_bid(live_db, world):
    e = one(live_db, world, 0.49, [0.49], [0.60])
    assert e == {"id": e["id"], "action": "ok", "mark": 0.49, "loss_pct": 0.0}


def test_float_trap_loss_exactly_40_computes_just_under_in_floating_point(live_db, world):
    # 1 - 0.171/0.285 is 0.3999999999999999 in floating point: a naive
    # `< 0.40` calls it ok. The EPS tolerance makes it a 40% loss: the stop fires.
    assert 1 - 0.171 / 0.285 < stop_evaluator.UNIVERSAL_MAX_LOSS_PCT
    e = one(live_db, world, 0.285, [0.171], [0.171])
    assert e["loss_pct"] == 0.4 and e["action"] == "would_exit"


def test_float_trap_loss_equals_threshold_in_floating_point(live_db, world):
    # entry .30, bid .17, ask .18: loss 0.43333333333333324 and threshold
    # 0.4333333333333333 differ in the 16th digit, so a naive `loss < threshold`
    # holds the position. The difference is float noise (inside EPS): the
    # boundary is inclusive, exit.
    entry, bid, ask = 0.30, 0.17, 0.18
    raw_loss = 1 - bid / entry
    raw_thr = stop_evaluator.UNIVERSAL_MAX_LOSS_PCT + min(
        max(0.0, ask - bid) / entry, live_config.SPREAD_ALLOW_PCT)
    assert raw_loss < raw_thr
    e = one(live_db, world, entry, [bid], [ask])
    assert e["loss_pct"] == 0.4333 and e["threshold"] == 0.4333
    assert e["action"] == "would_exit"


def test_spread_allow_pct_constant():
    assert live_config.SPREAD_ALLOW_PCT == 0.10


# ---------------------------------------------------------------------------
# unpriced and bad_row
# ---------------------------------------------------------------------------

class _Raw:
    """A book-like object whose levels carry whatever prices the test wants."""

    def __init__(self, bids, asks):
        self.bids = [type("L", (), {"price": p, "size": 1.0})() for p in bids]
        self.asks = [type("L", (), {"price": p, "size": 1.0})() for p in asks]


UNPRICED_BOOKS = {
    "none": lambda t: None,
    "no_bids": lambda t: make_book(t, [], [0.5]),
    "no_asks": lambda t: make_book(t, [0.1], []),
    "both_empty": lambda t: make_book(t, [], []),
    "non_numeric_bid": lambda t: _Raw(["abc"], [0.5]),
    "none_bid": lambda t: _Raw([None], [0.5]),
    "non_numeric_ask": lambda t: _Raw([0.1], ["abc"]),
    "zero_bid": lambda t: make_book(t, [0.0], [0.5]),
    "negative_bid": lambda t: make_book(t, [-0.1], [0.5]),
    "bid_above_one": lambda t: make_book(t, [1.2], [1.3]),
    "ask_above_one": lambda t: make_book(t, [0.1], [1.5]),
    "zero_ask": lambda t: make_book(t, [0.1], [0.0]),
    "nan_bid": lambda t: _Raw([float("nan")], [0.5]),
    "inf_ask": lambda t: _Raw([0.1], [float("inf")]),
}


@pytest.mark.parametrize("case", sorted(UNPRICED_BOOKS))
def test_unpriced_never_marks_or_sells(live_db, world, armed, case):
    pid = add_position(live_db, 0.60)
    tok = TOKEN.format(pid)
    world.books[tok] = UNPRICED_BOOKS[case](tok)
    out = evaluate(live_db)
    assert out == [{"id": pid, "action": "unpriced"}]
    assert world.exit_calls == []


def test_price_of_exactly_one_is_a_valid_price(live_db, world):
    e = one(live_db, world, 0.99, [0.45], [1.0])
    assert e["action"] == "would_exit"


@pytest.mark.parametrize("bad", [None, 0, 0.0, -0.3, "abc"], ids=["null", "zero", "zerof", "neg", "text"])
def test_bad_row_and_next_position_still_evaluated(live_db, world, bad):
    p1 = add_position(live_db, bad)
    p2 = add_position(live_db, 0.50)
    world.books[TOKEN.format(p2)] = make_book(TOKEN.format(p2), [0.45], [0.47])
    out = evaluate(live_db, dry_run=True)
    assert out[0] == {"id": p1, "action": "bad_row"}
    assert out[1]["id"] == p2 and out[1]["action"] == "ok"
    assert TOKEN.format(p1) not in world.book_calls     # no book fetch for a bad row


# ---------------------------------------------------------------------------
# Errors are per position
# ---------------------------------------------------------------------------

def test_get_orderbook_raising_is_error_and_next_still_evaluated(live_db, world):
    p1 = add_position(live_db, 0.50)
    p2 = add_position(live_db, 0.50)
    world.books[TOKEN.format(p1)] = RuntimeError("boom")
    world.books[TOKEN.format(p2)] = make_book(TOKEN.format(p2), [0.45], [0.47])
    out = evaluate(live_db, dry_run=True)
    assert out[0]["id"] == p1 and out[0]["action"] == "error"
    assert out[0]["error"] == "RuntimeError: boom"
    assert out[1]["id"] == p2 and out[1]["action"] == "ok"


def test_exit_raising_is_exit_unknown_and_next_still_evaluated(live_db, world, armed):
    # CHANGED in 1B-fix-1: this test expected "error" when the exit raised. An
    # exception from the exit call means a sale may have happened, so it is
    # exit_unknown ("error" now means: could not evaluate, no sale attempted).
    p1 = add_position(live_db, 0.50)
    p2 = add_position(live_db, 0.50)
    for p in (p1, p2):
        world.books[TOKEN.format(p)] = make_book(TOKEN.format(p), [0.20], [0.21])
    world.exit_exc = ValueError("exit blew up")
    out = evaluate(live_db)
    assert [e["action"] for e in out] == ["exit_unknown", "exit_unknown"]
    assert out[0]["error"] == "ValueError: exit blew up"
    assert out[0]["mark"] == 0.20 and out[0]["loss_pct"] == 0.6   # the figures are kept
    assert len(world.exit_calls) == 2       # the second was still attempted


def test_book_failure_is_error_and_no_exit_is_attempted(live_db, world, armed):
    pid = add_position(live_db, 0.50)
    world.books[TOKEN.format(pid)] = RuntimeError("book down")
    out = evaluate(live_db)
    assert [e["action"] for e in out] == ["error"]
    assert world.exit_calls == []


@pytest.mark.parametrize("result", [True, 1, "closed", ["closed"]], ids=["true", "one", "str", "list"])
def test_exit_returning_truthy_non_dict_is_exit_unknown(live_db, world, armed, result):
    pid = add_position(live_db, 0.50)
    world.books[TOKEN.format(pid)] = make_book(TOKEN.format(pid), [0.20], [0.21])
    world.exit_result = result
    out = evaluate(live_db)
    assert len(out) == 1 and out[0]["action"] == "exit_unknown"
    assert out[0]["error"] == f"exit returned {result!r}"
    assert out[0]["mark"] == 0.20 and out[0]["loss_pct"] == 0.6


def test_exit_unknown_error_text_is_capped_at_120_chars_of_repr(live_db, world, armed):
    pid = add_position(live_db, 0.50)
    world.books[TOKEN.format(pid)] = make_book(TOKEN.format(pid), [0.20], [0.21])
    world.exit_result = "x" * 500
    err = evaluate(live_db)[0]["error"]
    assert err == "exit returned " + repr("x" * 500)[:120]


def test_exit_dict_without_action_gets_exited(live_db, world, armed):
    pid = add_position(live_db, 0.50)
    world.books[TOKEN.format(pid)] = make_book(TOKEN.format(pid), [0.20], [0.21])
    world.exit_result = {"pnl": -2.0}
    out = evaluate(live_db)
    assert out[0]["action"] == "exited" and out[0]["pnl"] == -2.0 and out[0]["loss_pct"] == 0.6


@pytest.mark.parametrize("falsy", [None, 0, "", [], {}], ids=["none", "zero", "emptystr", "emptylist", "emptydict"])
def test_exit_returning_falsy_is_exit_failed_with_figures(live_db, world, armed, falsy):
    pid = add_position(live_db, 0.50)
    world.books[TOKEN.format(pid)] = make_book(TOKEN.format(pid), [0.20], [0.21])
    world.exit_result = falsy
    out = evaluate(live_db)
    assert out == [{"id": pid, "mark": 0.20, "loss_pct": 0.6, "spread_pct": 0.02,
                    "threshold": 0.42, "action": "exit_failed"}]


def test_malformed_book_object_is_error_not_crash(live_db, world, monkeypatch):
    p1 = add_position(live_db, 0.50)
    p2 = add_position(live_db, 0.50)
    world.books[TOKEN.format(p1)] = {"bids": "not a book"}   # attribute access raises
    world.books[TOKEN.format(p2)] = make_book(TOKEN.format(p2), [0.45], [0.47])
    out = evaluate(live_db, dry_run=True)
    assert out[0]["action"] == "error" and out[0]["id"] == p1
    assert out[1]["action"] == "ok"


# ---------------------------------------------------------------------------
# Exemptions
# ---------------------------------------------------------------------------

def test_exempt_id_over_threshold_is_exempt_without_book_or_exit(live_db, world, armed, monkeypatch):
    for _ in range(25):
        add_position(live_db, 0.5, status="closed")     # ids 1..25 closed
    pid = add_position(live_db, 0.24)
    assert pid == 26
    world.books[TOKEN.format(26)] = make_book(TOKEN.format(26), [0.08], [0.09])
    monkeypatch.setenv("POLYCLAWD_STOP_EXEMPT_IDS", "26")
    out = evaluate(live_db)
    assert out == [{"id": 26, "action": "exempt"}]
    assert world.exit_calls == [] and world.book_calls == []


@pytest.mark.parametrize("raw", ["2x6", "26,,27", "-1", "26.0", "26,", ",26", "+5", "٢٦", "0x1a"])
def test_stop_exempt_ids_bad_value_raises_naming_variable(monkeypatch, raw):
    monkeypatch.setenv("POLYCLAWD_STOP_EXEMPT_IDS", raw)
    with pytest.raises(ValueError) as ei:
        live_config.stop_exempt_ids()
    assert "POLYCLAWD_STOP_EXEMPT_IDS" in str(ei.value)


def test_stop_exempt_ids_error_names_the_bad_element(monkeypatch):
    monkeypatch.setenv("POLYCLAWD_STOP_EXEMPT_IDS", "26,2x6,27")
    with pytest.raises(ValueError, match="2x6"):
        live_config.stop_exempt_ids()


def test_stop_exempt_ids_whitespace_tolerated(monkeypatch):
    monkeypatch.setenv("POLYCLAWD_STOP_EXEMPT_IDS", " 26 , 27 ")
    assert live_config.stop_exempt_ids() == {26, 27}


@pytest.mark.parametrize("raw", ["", "   "])
def test_stop_exempt_ids_empty_is_empty_set(monkeypatch, raw):
    monkeypatch.setenv("POLYCLAWD_STOP_EXEMPT_IDS", raw)
    assert live_config.stop_exempt_ids() == set()


def test_stop_exempt_ids_unset_is_empty_set(monkeypatch):
    monkeypatch.delenv("POLYCLAWD_STOP_EXEMPT_IDS", raising=False)
    assert live_config.stop_exempt_ids() == set()


def test_stop_exempt_ids_zero_is_allowed(monkeypatch):
    monkeypatch.setenv("POLYCLAWD_STOP_EXEMPT_IDS", "0")
    assert live_config.stop_exempt_ids() == {0}


def test_bad_exempt_value_is_not_swallowed_by_evaluator(live_db, world, monkeypatch):
    add_position(live_db, 0.5)
    monkeypatch.setenv("POLYCLAWD_STOP_EXEMPT_IDS", "2x6")
    with pytest.raises(ValueError):
        evaluate(live_db, dry_run=True)


# ---------------------------------------------------------------------------
# Mode matrix
# ---------------------------------------------------------------------------

def _boom(*a, **k):
    raise AssertionError("must not be reached")


def test_paper_not_dry_run_returns_empty_with_no_db_or_network(live_db, world, mode_env, monkeypatch):
    mode_env("PAPER", None, False)
    add_position(live_db, 0.5)
    monkeypatch.setattr(live_db_module, "connect", _boom)
    monkeypatch.setattr("odds.polymarket_clob.get_orderbook", _boom)
    monkeypatch.setenv("POLYCLAWD_STOP_EXEMPT_IDS", "2x6")      # not even read
    assert evaluate(live_db) == []
    assert world.exit_calls == []


def test_paper_dry_run_gives_entries_and_never_exits(live_db, world, mode_env):
    mode_env("PAPER", None, False)
    pid = add_position(live_db, 0.24)
    world.books[TOKEN.format(pid)] = make_book(TOKEN.format(pid), [0.08], [0.09])
    out = evaluate(live_db, dry_run=True)
    assert [e["action"] for e in out] == ["would_exit"]
    assert world.exit_calls == []


@pytest.mark.parametrize("enforce,ready", [(None, True), ("1", False), ("0", True), ("true", True)])
def test_live_unarmed_not_dry_run_returns_empty(live_db, world, mode_env, monkeypatch, enforce, ready):
    mode_env("LIVE", enforce, ready)
    add_position(live_db, 0.24)
    monkeypatch.setattr(live_db_module, "connect", _boom)
    assert evaluate(live_db) == []
    assert world.exit_calls == []


def test_live_unarmed_dry_run_evaluates_without_exit(live_db, world, mode_env):
    mode_env("LIVE", "1", False)
    pid = add_position(live_db, 0.24)
    world.books[TOKEN.format(pid)] = make_book(TOKEN.format(pid), [0.08], [0.09])
    assert evaluate(live_db, dry_run=True)[0]["action"] == "would_exit"
    assert world.exit_calls == []


def test_armed_not_dry_run_calls_exit(live_db, world, armed):
    pid = add_position(live_db, 0.24)
    world.books[TOKEN.format(pid)] = make_book(TOKEN.format(pid), [0.08], [0.09])
    evaluate(live_db)
    assert len(world.exit_calls) == 1


def test_armed_dry_run_is_would_exit_and_no_exit(live_db, world, armed):
    pid = add_position(live_db, 0.24)
    world.books[TOKEN.format(pid)] = make_book(TOKEN.format(pid), [0.08], [0.09])
    assert evaluate(live_db, dry_run=True)[0]["action"] == "would_exit"
    assert world.exit_calls == []


# ---------------------------------------------------------------------------
# One entry per open position; database handling
# ---------------------------------------------------------------------------

def test_one_entry_per_open_position_ordered_by_id(live_db, world):
    a = add_position(live_db, 0.50)
    closed = add_position(live_db, 0.50, status="closed")
    b = add_position(live_db, 0.50)
    zero = add_position(live_db, 0.50, shares=0)
    c = add_position(live_db, 0.50)
    for p in (a, b, c, closed, zero):
        world.books[TOKEN.format(p)] = make_book(TOKEN.format(p), [0.45], [0.47])
    out = evaluate(live_db, dry_run=True)
    assert [e["id"] for e in out] == [a, b, c]
    assert all(e["action"] == "ok" for e in out)
    assert closed not in [e["id"] for e in out] and zero not in [e["id"] for e in out]


def test_no_open_positions_is_empty(live_db, world):
    assert evaluate(live_db, dry_run=True) == []


def test_db_path_is_used_and_default_path_not_opened(live_db, world, tmp_path, monkeypatch):
    other = live_db_module.connect(tmp_path / "other" / "x.db")
    other.execute("INSERT INTO live_positions (market_id, token_id, side, entry_price,"
                  " shares, cost_usd, status) VALUES ('m','tok-other','YES',0.5,5,1,'open')")
    other.commit()
    other.close()
    add_position(live_db, 0.5)                        # lives in the default db only
    world.books["tok-other"] = make_book("tok-other", [0.45], [0.47])
    calls = []
    real = live_db_module.connect

    def spy(path=None):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(live_db_module, "connect", spy)
    out = stop_evaluator.evaluate_live_stops(dry_run=True, db_path=tmp_path / "other" / "x.db")
    assert calls == [tmp_path / "other" / "x.db"]
    assert [e["action"] for e in out] == ["ok"] and world.book_calls == ["tok-other"]
    # without db_path, connect() is called with no path
    calls.clear()
    world.book_calls.clear()
    world.books[TOKEN.format(1)] = make_book(TOKEN.format(1), [0.45], [0.47])
    stop_evaluator.evaluate_live_stops(dry_run=True)
    assert calls == [None]
    assert world.book_calls == [TOKEN.format(1)]


def test_connection_is_closed_before_any_orderbook_fetch(live_db, world, monkeypatch):
    p1 = add_position(live_db, 0.5)
    p2 = add_position(live_db, 0.5)
    handed_out = []
    real = live_db_module.connect

    def spy(path=None):
        c = real(path)
        handed_out.append(c)
        return c

    monkeypatch.setattr(live_db_module, "connect", spy)
    seen_open = []

    def fetch(token_id):
        for c in handed_out:
            try:
                c.execute("SELECT 1")
                seen_open.append(token_id)
            except sqlite3.ProgrammingError:
                pass
        return make_book(token_id, [0.45], [0.47])

    monkeypatch.setattr("odds.polymarket_clob.get_orderbook", fetch)
    out = evaluate(live_db, dry_run=True)
    assert len(out) == 2 and handed_out and seen_open == []


def test_each_position_fetches_its_book_exactly_once(live_db, world):
    p1 = add_position(live_db, 0.24)
    p2 = add_position(live_db, 0.5)
    world.books[TOKEN.format(p1)] = make_book(TOKEN.format(p1), [0.08], [0.09])
    world.books[TOKEN.format(p2)] = make_book(TOKEN.format(p2), [0.45], [0.47])
    evaluate(live_db, dry_run=True)
    assert world.book_calls == [TOKEN.format(p1), TOKEN.format(p2)]


# ---------------------------------------------------------------------------
# 1B-fix-1 A2/A3: decide on unrounded numbers with EPS; the pure helper
# ---------------------------------------------------------------------------

def test_eps_is_a_named_constant():
    assert stop_evaluator.EPS == 1e-9


def test_off_grid_entry_just_under_40_is_ok_not_exit(live_db, world):
    # entry 0.33333 (an average fill price), bid 0.20: true loss 0.399994.
    # Rounded to 4 places that is 0.4 and used to sell. It must be ok.
    e = one(live_db, world, 0.33333, [0.20], [0.21])
    assert e["action"] == "ok"
    assert e["loss_pct"] == 0.4          # display only
    assert 1 - 0.20 / 0.33333 < 0.4


def test_off_grid_helper_directly():
    assert stop_evaluator._stop_decision(0.33333, 0.20, 0.21)[0] == "ok"
    assert stop_evaluator._stop_decision(0.33333, 0.19999, 0.21)[0] != "ok"


def test_float_trap_cases_through_helper():
    assert 1 - 0.171 / 0.285 < 0.4       # the noise is real
    assert stop_evaluator._stop_decision(0.285, 0.171, 0.171)[0] == "exit"
    assert stop_evaluator._stop_decision(0.30, 0.17, 0.18)[0] == "exit"


def test_loss_exactly_the_threshold_by_construction_exits():
    # 0.20 / 0.10 / 0.13: loss 0.5, capped spread 0.10, threshold 0.5
    decision, fig = stop_evaluator._stop_decision(0.20, 0.10, 0.13)
    assert decision == "exit" and fig == {"loss_pct": 0.5, "spread_pct": 0.15, "threshold": 0.5}


def test_helper_returns_figures_rounded_for_display():
    decision, fig = stop_evaluator._stop_decision(0.24, 0.08, 0.09)
    assert decision == "exit"
    assert fig == {"loss_pct": 0.6667, "spread_pct": 0.0417, "threshold": 0.4417}
    decision, fig = stop_evaluator._stop_decision(0.50, 0.49, 0.60)
    assert decision == "ok" and fig == {"loss_pct": 0.02}


def _cents(c):
    """Exact value of c hundredths, built from a decimal string."""
    return Fraction(f"{c // 100}.{c % 100:02d}")


def test_property_decision_matches_exact_arithmetic_over_grid(monkeypatch):
    # An exact oracle over every entry 0.01..0.99, bid 0.01..entry, ask bid..bid+0.10
    # (all step 0.01, ask capped at 1.00), with the true loss computed by Fraction
    # from decimal strings:
    #   loss < 40%                                -> "ok", never an exit
    #   40% <= loss < 40% + min(spread, 10%)      -> "spread_held"
    #   loss >= 40% + min(spread, 10%)            -> "exit"
    monkeypatch.setattr(stop_evaluator, "UNIVERSAL_MAX_LOSS_PCT", 0.40)
    base, cap = Fraction("0.40"), Fraction("0.10")
    n = {"ok": 0, "spread_held": 0, "exit": 0}
    for e in range(1, 100):
        entry_x = _cents(e)
        for b in range(1, e + 1):
            bid_x = _cents(b)
            loss = 1 - bid_x / entry_x
            for a in range(b, min(b + 10, 100) + 1):
                spread_share = min((_cents(a) - bid_x) / entry_x, cap)
                if loss < base:
                    want = "ok"
                elif loss < base + spread_share:
                    want = "spread_held"
                else:
                    want = "exit"
                got, _ = stop_evaluator._stop_decision(e / 100, b / 100, a / 100)
                assert got == want, (e, b, a, got, want, float(loss))
                n[want] += 1
    assert all(n.values()), n                    # every branch was exercised
    assert sum(n.values()) == 54285              # the grid is the size it claims


# ---------------------------------------------------------------------------
# A4: best prices without trusting the sort order; strict book check
# ---------------------------------------------------------------------------

def test_unsorted_bids_give_the_same_decision_as_sorted(live_db, world):
    sorted_e = one(live_db, world, 0.24, [0.12, 0.10, 0.08], [0.13, 0.20])
    unsorted_e = one(live_db, world, 0.24, [0.08, 0.12, 0.10], [0.13, 0.20])
    assert sorted_e["mark"] == unsorted_e["mark"] == 0.12
    assert {k: v for k, v in sorted_e.items() if k != "id"} == {k: v for k, v in unsorted_e.items() if k != "id"}
    assert unsorted_e["action"] == "would_exit"


def test_unsorted_asks_give_the_same_decision_as_sorted(live_db, world):
    # entry 0.50, bid 0.28, best ask 0.29: spread 0.02, threshold 0.42, loss 0.44 -> exit.
    # Taking the first (worst) ask 0.40 would give spread 0.24 -> cap -> threshold 0.5 -> held.
    sorted_e = one(live_db, world, 0.50, [0.28], [0.29, 0.40])
    unsorted_e = one(live_db, world, 0.50, [0.28], [0.40, 0.29])
    assert unsorted_e["spread_pct"] == sorted_e["spread_pct"] == 0.02
    assert unsorted_e["action"] == sorted_e["action"] == "would_exit"


@pytest.mark.parametrize("bids,asks", [
    ([0.08, "abc"], [0.09]),
    ([0.08, float("nan")], [0.09]),
    ([0.08, 1.5], [0.09]),
    ([0.08, 0.0], [0.09]),
    ([0.08], [0.09, None]),
    ([0.08], [0.09, float("inf")]),
    ([0.08], [0.09, -0.2]),
], ids=["bid-str", "bid-nan", "bid-over-1", "bid-zero", "ask-none", "ask-inf", "ask-neg"])
def test_book_with_a_corrupt_second_level_is_unpriced(live_db, world, armed, bids, asks):
    pid = add_position(live_db, 0.24)
    world.books[TOKEN.format(pid)] = _Raw(bids, asks)
    assert evaluate(live_db) == [{"id": pid, "action": "unpriced"}]
    assert world.exit_calls == []


@pytest.mark.parametrize("bad", [Decimal("0.08"), True, False, 1 + 0j], ids=["decimal", "true", "false", "complex"])
def test_decimal_and_bool_prices_are_unpriced(live_db, world, armed, bad):
    for side in ("bid", "ask"):
        pid = add_position(live_db, 0.24)
        world.books[TOKEN.format(pid)] = _Raw([bad] if side == "bid" else [0.08],
                                              [0.09] if side == "bid" else [bad])
        out = evaluate(live_db)
        assert out[-1] == {"id": pid, "action": "unpriced"}, side
    assert world.exit_calls == []


def test_usable_price_is_strict_about_types():
    class F(float):
        pass

    assert stop_evaluator._usable_price(F(0.5)) is True          # a float subclass
    assert stop_evaluator._usable_price(1) is True               # int 1 is the top of the range
    assert stop_evaluator._usable_price(Decimal("0.5")) is False
    assert stop_evaluator._usable_price(True) is False
    assert stop_evaluator._usable_price("0.5") is False
    np = pytest.importorskip("numpy")
    assert stop_evaluator._usable_price(np.float64(0.5)) is True  # np.float64 subclasses float
    assert stop_evaluator._usable_price(np.float32(0.5)) is False  # not an int or float subclass


# ---------------------------------------------------------------------------
# A5: rows
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token", [None, "", "   "], ids=["null", "empty", "blank"])
def test_missing_token_id_is_bad_row_and_no_book_fetch(live_db, world, armed, token):
    cur = live_db.conn.execute(
        "INSERT INTO live_positions (market_id, token_id, side, entry_price, shares, cost_usd, status)"
        " VALUES ('m', ?, 'YES', 0.5, 10, 1, 'open')", (token,))
    pid = cur.lastrowid
    live_db.conn.commit()
    out = evaluate(live_db)
    assert out == [{"id": pid, "action": "bad_row"}]
    assert world.book_calls == [] and world.exit_calls == []


class _FakeConn:
    """A connection that hands back exactly the given rows, so values SQLite
    cannot store (NaN becomes NULL on bind; REAL affinity turns "0.24" into a
    float) can still reach the evaluator."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **k):
        return list(self._rows)

    def close(self):
        pass


def _rows_with_entry(monkeypatch, entry, token="tok-x"):
    row = {"id": 1, "token_id": token, "entry_price": entry, "shares": 10.0, "status": "open"}
    monkeypatch.setattr(live_db_module, "connect", lambda path=None: _FakeConn([row]))


@pytest.mark.parametrize("entry", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"])
def test_non_finite_entry_price_is_bad_row(world, monkeypatch, entry):
    _rows_with_entry(monkeypatch, entry)
    world.books["tok-x"] = make_book("tok-x", [0.2], [0.3])
    out = stop_evaluator.evaluate_live_stops(dry_run=True)
    assert out == [{"id": 1, "action": "bad_row"}]
    assert world.book_calls == []


def test_numeric_string_entry_price_is_accepted(world, monkeypatch):
    _rows_with_entry(monkeypatch, "0.24")
    world.books["tok-x"] = make_book("tok-x", [0.08], [0.09])
    out = stop_evaluator.evaluate_live_stops(dry_run=True)
    assert out[0]["action"] == "would_exit" and out[0]["loss_pct"] == 0.6667


def test_numeric_string_entry_price_in_a_real_table_is_accepted(live_db, world):
    # a REAL column turns "0.24" into 0.24 on the way in; same result
    e = one(live_db, world, "0.24", [0.08], [0.09])
    assert e["action"] == "would_exit" and e["loss_pct"] == 0.6667


def test_nan_entry_price_in_a_real_table_arrives_as_null_and_is_bad_row(live_db, world):
    # SQLite stores NaN as NULL, so NaN can only reach the evaluator as NULL.
    pid = add_position(live_db, float("nan"))
    assert live_db.conn.execute("SELECT entry_price FROM live_positions WHERE id=?", (pid,)).fetchone()[0] is None
    assert evaluate(live_db, dry_run=True) == [{"id": pid, "action": "bad_row"}]


# ---------------------------------------------------------------------------
# A6: armed mode never sells on a non-exit decision
# ---------------------------------------------------------------------------

def _arrange(case, live_db, world, monkeypatch):
    """Returns the expected action; sets up one position for that action."""
    if case == "exempt":
        pid = add_position(live_db, 0.24)
        monkeypatch.setenv("POLYCLAWD_STOP_EXEMPT_IDS", str(pid))
        world.books[TOKEN.format(pid)] = make_book(TOKEN.format(pid), [0.08], [0.09])
    elif case == "bad_row":
        add_position(live_db, 0)
    elif case == "unpriced":
        pid = add_position(live_db, 0.24)
        world.books[TOKEN.format(pid)] = make_book(TOKEN.format(pid), [0.08], [])
    elif case == "ok":
        pid = add_position(live_db, 0.50)
        world.books[TOKEN.format(pid)] = make_book(TOKEN.format(pid), [0.45], [0.47])
    elif case == "spread_held":
        pid = add_position(live_db, 0.20)
        world.books[TOKEN.format(pid)] = make_book(TOKEN.format(pid), [0.11], [0.13])
    return case


@pytest.mark.parametrize("case", ["ok", "bad_row", "unpriced", "exempt", "spread_held"])
def test_armed_not_dry_run_never_calls_exit_on_a_non_exit_decision(live_db, world, armed, monkeypatch, case):
    _arrange(case, live_db, world, monkeypatch)
    out = evaluate(live_db, dry_run=False)
    assert [e["action"] for e in out] == [case]
    assert world.exit_calls == []


# ---------------------------------------------------------------------------
# A7: the bound is base plus cap, and the base can be overridden
# ---------------------------------------------------------------------------

def test_overridden_base_moves_the_bound(monkeypatch):
    monkeypatch.setattr(stop_evaluator, "UNIVERSAL_MAX_LOSS_PCT", 0.45)
    # wide spread (cap applies): the bound is 0.45 + 0.10 = 0.55
    assert stop_evaluator._stop_decision(1.0, 0.45, 0.95)[0] == "exit"          # 55% loss
    assert stop_evaluator._stop_decision(1.0, 0.46, 0.95)[0] == "spread_held"   # 54% loss
    assert stop_evaluator._stop_decision(1.0, 0.56, 0.95)[0] == "ok"            # 44% loss, under the base
    # with the default base a 54% loss would already have sold
    monkeypatch.setattr(stop_evaluator, "UNIVERSAL_MAX_LOSS_PCT", 0.40)
    assert stop_evaluator._stop_decision(1.0, 0.46, 0.95)[0] == "exit"


def test_docstring_states_the_bound():
    doc = stop_evaluator.evaluate_live_stops.__doc__
    assert "UNIVERSAL_MAX_LOSS_PCT +" in doc and "SPREAD_ALLOW_PCT" in doc and "0.50" in doc


# ---------------------------------------------------------------------------
# A8: the exemption value as an operator writes it in the env file
# ---------------------------------------------------------------------------

def _load_env_text(monkeypatch, tmp_path, text):
    """Run live_config's own loader on a temp file. The loader reads the module
    constant _ENV_FILE and fills os.environ; _ENV_FILE is pointed at a temp file
    (monkeypatch restores it) and the variable is removed first, because the
    loader never overrides a key that is already set. The setenv/delenv pair
    makes monkeypatch remove whatever the loader sets, at teardown. The real
    environment file is never read."""
    env_file = tmp_path / "polymarket.env"
    env_file.write_text(text)
    monkeypatch.setenv("POLYCLAWD_STOP_EXEMPT_IDS", "placeholder")
    monkeypatch.delenv("POLYCLAWD_STOP_EXEMPT_IDS")
    monkeypatch.setattr(live_config, "_ENV_FILE", env_file)
    live_config._load_env_file()


def test_env_file_line_with_double_quotes_and_a_space_gives_both_ids(monkeypatch, tmp_path):
    _load_env_text(monkeypatch, tmp_path, 'POLYCLAWD_STOP_EXEMPT_IDS="26, 27"\n')
    assert live_config.stop_exempt_ids() == {26, 27}


def test_env_file_line_with_per_element_quotes_raises(monkeypatch, tmp_path):
    _load_env_text(monkeypatch, tmp_path, "POLYCLAWD_STOP_EXEMPT_IDS='26','27'\n")
    with pytest.raises(ValueError, match="POLYCLAWD_STOP_EXEMPT_IDS"):
        live_config.stop_exempt_ids()
