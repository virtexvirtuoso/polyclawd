"""Task 1.2a: evaluate_live_stops, the live stop-loss decision as a pure function.

The mark is the best bid of the held token; the same number goes to the exit.
Every open position gets exactly one entry. Nothing here sells: the exit
function is a recorder.
"""

import sqlite3

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
    return evaluate(db, dry_run=True, **kw)[0]


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
    # `< 0.40` calls it ok. Rounded to 4 places it is 0.4 and the stop fires.
    assert 1 - 0.171 / 0.285 < stop_evaluator.UNIVERSAL_MAX_LOSS_PCT
    e = one(live_db, world, 0.285, [0.171], [0.171])
    assert e["loss_pct"] == 0.4 and e["action"] == "would_exit"


def test_float_trap_loss_equals_threshold_in_floating_point(live_db, world):
    # entry .30, bid .17, ask .18: loss 0.43333333333333324 and threshold
    # 0.4333333333333333 differ in the 16th digit, so a naive `loss < threshold`
    # holds the position. Both round to 0.4333: the boundary is inclusive, exit.
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


def test_exit_raising_is_error_and_next_still_evaluated(live_db, world, armed):
    p1 = add_position(live_db, 0.50)
    p2 = add_position(live_db, 0.50)
    for p in (p1, p2):
        world.books[TOKEN.format(p)] = make_book(TOKEN.format(p), [0.20], [0.21])
    world.exit_exc = ValueError("exit blew up")
    out = evaluate(live_db)
    assert [e["action"] for e in out] == ["error", "error"]
    assert out[0]["error"] == "ValueError: exit blew up"
    assert len(world.exit_calls) == 2       # the second was still attempted


def test_exit_returning_none_is_exit_failed(live_db, world, armed):
    pid = add_position(live_db, 0.50)
    world.books[TOKEN.format(pid)] = make_book(TOKEN.format(pid), [0.20], [0.21])
    world.exit_result = None
    assert evaluate(live_db) == [{"id": pid, "action": "exit_failed"}]


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
