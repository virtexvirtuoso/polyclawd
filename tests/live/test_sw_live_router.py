"""The live smart-wallet router, in two stages (task 1.3; audit #54, #39, #31, #41).

Stage 1 runs in every mode and touches no money: per record it decides
would_route or filtered (first failing check wins), reads the order book once,
and logs the record's own facts. When the system is not armed the cycle ends with
one `sw_live DRYRUN` summary line: a week of those lines is the evidence for the
decision to trade real money again. Stage 2 (bankroll, sizing, governor rails,
`execute_intent`) runs only when `live_armed()`.

No network (autouse guard): the order book, the exit-cooldown file, the wallet
database and `execute_intent` are replaced; every record is built by hand.
"""
import logging
import sqlite3

import pytest

from execution import live_config
from odds.polymarket_clob import OrderBook, OrderBookLevel
from scripts import smart_wallet_fast_poll as sw

WALLET = "0xwallet0000000000000000000000000000000001"
CID = "0xcondition0000000001"
TOKEN_A = "1111111111111111111111111111111111111111111100000AAAA"
TOKEN_B = "2222222222222222222222222222222222222222222200000BBBB"
TITLE = "Lakers vs Celtics"
LOGGER = "scripts.smart_wallet_fast_poll"


def _rec(**kw):
    """An entry record as check_and_fire returns it: eligible, with the facts."""
    d = dict(wallet=WALLET, name="w", market=CID, title=TITLE, direction="BUY",
             outcome="Yes", outcome_index=0, asset=TOKEN_A, price=0.40,
             price_at_alert=0.40, alert_type="entry", live_eligible=True,
             live_filter_reason=None, is_bot=False, clv_muted=False,
             category="crypto", cumulative_usd=1200.0, num_fills=3)
    d.update(kw)
    return d


def _gamma(category="sports", **kw):
    g = {"category": category, "slug": "lakers-celtics", "question": "Will the Lakers beat the Celtics?"}
    g.update(kw)
    return {CID: g}


def _book(token, bids, asks):
    return OrderBook(market_id="", token_id=token, outcome="",
                     bids=[OrderBookLevel(price=p, size=10.0) for p in bids],
                     asks=[OrderBookLevel(price=p, size=10.0) for p in asks],
                     spread=0.0, mid_price=0.0, timestamp="")


class Env:
    """Everything the router reaches, replaced, with a record of what it did."""

    def __init__(self):
        self.books = {}            # token -> OrderBook | None | Exception
        self.book_calls = []
        self.intents = []          # execute_intent kwargs
        self.bankroll_reads = 0
        self.sizing_calls = 0
        self.rail_calls = 0
        self.cooldown = set()
        self.archetype_rows = []   # (wallet, archetype, trades, pnl)
        self.default_book = None


@pytest.fixture
def env(monkeypatch, tmp_path, caplog, fake_clob):
    e = Env()
    caplog.set_level(logging.INFO, logger=LOGGER)

    def get_orderbook(token):
        e.book_calls.append(token)
        b = e.books.get(token, e.default_book)
        if isinstance(b, Exception):
            raise b
        return b

    e.default_book = _book(TOKEN_A, [0.39], [0.40])
    monkeypatch.setattr("odds.polymarket_clob.get_orderbook", get_orderbook)
    monkeypatch.setattr(sw, "_is_in_exit_cooldown", lambda token: token in e.cooldown)

    def get_meta_db(*a, **k):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("CREATE TABLE wallet_archetype_pnl (wallet TEXT, archetype TEXT, trades INTEGER, "
                  "wins INTEGER, pnl REAL, concentration REAL, updated REAL)")
        for w, arch, trades, pnl in e.archetype_rows:
            c.execute("INSERT INTO wallet_archetype_pnl VALUES (?,?,?,?,?,0,0)", (w, arch, trades, 0, pnl))
        return c

    monkeypatch.setattr("signals.whale_wallets.get_meta_db", get_meta_db)

    def execute_intent(conn, governor, **kw):
        e.intents.append(kw)
        return {"action": "maker_resting", "reason": ""}

    monkeypatch.setattr("execution.live_executor.execute_intent", execute_intent)
    monkeypatch.setattr("execution.clob_client.get_tick_size", lambda token: 0.01)

    class Allow:
        allowed = True
        reason = ""

    class StubGovernor:
        def __init__(self, conn, mode="LIVE"):
            pass

        def check(self, intent):
            return Allow()

    monkeypatch.setattr("execution.risk_governor.RiskGovernor", StubGovernor)
    e.caplog = caplog
    e.fake_clob = fake_clob
    return e


@pytest.fixture
def bankroll_trap(env, monkeypatch):
    """Under PAPER the live database, the sizing and the rails must stay untouched:
    patch every one of them to explode and count."""

    def read_boom(*a, **k):
        env.bankroll_reads += 1
        raise AssertionError("bankroll read reached")

    def size(bankroll):
        env.sizing_calls += 1
        raise AssertionError("sizing reached")

    def rails(bankroll, size_usd):
        env.rail_calls += 1
        raise AssertionError("governor rails reached")

    monkeypatch.setattr("execution.live_db.connect", read_boom)
    monkeypatch.setattr(sw, "_sw_live_size_usd", size)
    monkeypatch.setattr(sw, "_check_governor_rails", rails)
    return env


@pytest.fixture
def armed_env(env, armed, live_db, monkeypatch):
    """Armed, with a temp live DB holding a bankroll; sizing and rails recorded."""
    live_db.conn.execute("INSERT INTO live_portfolio_state (bankroll) VALUES (?)", (50.0,))
    live_db.conn.commit()
    real_size = sw._sw_live_size_usd

    def size(bankroll):
        env.sizing_calls += 1
        return real_size(bankroll)

    monkeypatch.setattr(sw, "_sw_live_size_usd", size)
    monkeypatch.setattr(sw, "_check_governor_rails",
                        lambda bankroll, size_usd: setattr(env, "rail_calls", env.rail_calls + 1))
    return env


def _lines(env, prefix):
    return [r.getMessage() for r in env.caplog.records if r.getMessage().startswith(prefix)]


def _fields(line):
    return dict(p.split("=", 1) for p in line.split()[2:])


def _route(env, recs, gamma=None):
    env.caplog.clear()
    sw._route_live_smart_wallet(recs, _gamma() if gamma is None else gamma)
    return [_fields(x) for x in _lines(env, "sw_live RECORD")]


def _one(env, rec, gamma=None):
    (fields,) = _route(env, [rec], gamma)
    return fields


@pytest.fixture
def paper(mode_env):
    mode_env("PAPER", None, False)


@pytest.fixture
def live_unarmed(mode_env):
    mode_env("LIVE", None, True)       # flag unset: not armed


# --------------------------------------------------------------------------- #
# the plan's tests
# --------------------------------------------------------------------------- #
def test_entry_at_072_is_filtered_price_band_and_never_executed(armed_env):
    f = _one(armed_env, _rec(price=0.72, price_at_alert=0.72, live_eligible=False,
                             live_filter_reason="price_band"))
    assert (f["verdict"], f["reason"]) == ("filtered", "price_band")
    assert armed_env.intents == []
    assert armed_env.book_calls == []


def test_bot_wallet_is_filtered(env, paper):
    f = _one(env, _rec(is_bot=True, live_eligible=False, live_filter_reason="bot_wallet"))
    assert (f["verdict"], f["reason"], f["is_bot"]) == ("filtered", "bot_wallet", "true")


def test_outcome_index_none_with_asset_routes_that_token(env, paper):
    env.books[TOKEN_B] = _book(TOKEN_B, [0.39], [0.40])
    f = _one(env, _rec(outcome_index=None, asset=TOKEN_B))
    assert f["verdict"] == "would_route"
    assert f["token"] == TOKEN_B[-8:]
    assert env.book_calls == [TOKEN_B]


def test_armed_intent_carries_the_trades_own_token(armed_env):
    armed_env.books[TOKEN_B] = _book(TOKEN_B, [0.39], [0.40])
    _route(armed_env, [_rec(outcome_index=None, asset=TOKEN_B)])
    (intent,) = armed_env.intents
    assert intent["token_id"] == TOKEN_B


def test_token_comes_from_asset_not_from_outcome_index(armed_env):
    """outcome_index 1 used to pick the second token of the market's token list."""
    armed_env.books[TOKEN_A] = _book(TOKEN_A, [0.39], [0.40])
    _route(armed_env, [_rec(outcome_index=1, asset=TOKEN_A)])
    (intent,) = armed_env.intents
    assert intent["token_id"] == TOKEN_A
    assert armed_env.book_calls == [TOKEN_A]


def test_market_category_none_is_no_category_even_with_an_accepted_wallet_category(env, paper):
    f = _one(env, _rec(category="sports"), gamma=_gamma(category=None))
    assert (f["verdict"], f["reason"]) == ("filtered", "no_category")
    assert env.book_calls == []


def test_empty_market_category_is_no_category(env, paper):
    f = _one(env, _rec(category="sports"), gamma=_gamma(category="  "))
    assert f["reason"] == "no_category"


def test_wallet_category_is_never_read(env, paper):
    """The market is politics (accepted); the wallet's own category says pop-culture."""
    f = _one(env, _rec(category="entertainment"), gamma=_gamma(category="politics"))
    assert f["verdict"] == "would_route"
    assert f["category"] == "politics"


def test_category_outside_the_accept_list_is_filtered(env, paper):
    f = _one(env, _rec(), gamma=_gamma(category="pop-culture"))
    assert (f["verdict"], f["reason"]) == ("filtered", "category")


def test_the_accept_list_is_unchanged():
    assert sw._ALLOWED_CATEGORIES == {"sports", "crypto", "politics", "weather", "finance", "economics"}


# --------------------------------------------------------------------------- #
# archetype gate reads the title (#41)
# --------------------------------------------------------------------------- #
def test_archetype_gate_reads_the_rec_title_not_the_gamma_question(env, paper):
    """The wallet loses money in sports. The title says sports; Gamma's question
    (the field the old code read) says crypto, so reading it would have passed."""
    env.archetype_rows = [(WALLET, "sports", 40, -500.0)]
    gamma = _gamma(question="Will Bitcoin be above 100k?")
    f = _one(env, _rec(title="Lakers vs Celtics"), gamma=gamma)
    assert (f["verdict"], f["reason"]) == ("filtered", "archetype")


def test_archetype_gate_works_with_no_gamma_question_at_all(env, paper):
    env.archetype_rows = [(WALLET, "sports", 40, -500.0)]
    gamma = {CID: {"category": "sports"}}
    f = _one(env, _rec(title="Lakers vs Celtics"), gamma=gamma)
    assert f["reason"] == "archetype"


def test_archetype_gate_passes_a_wallet_with_no_losing_record(env, paper):
    env.archetype_rows = [(WALLET, "sports", 40, +500.0)]
    assert _one(env, _rec())["verdict"] == "would_route"


@pytest.mark.parametrize("title", [None, "", "   ", CID])
def test_missing_title_is_no_title(env, paper, title):
    f = _one(env, _rec(title=title))
    assert (f["verdict"], f["reason"]) == ("filtered", "no_title")
    assert env.book_calls == []


# --------------------------------------------------------------------------- #
# spread gate
# --------------------------------------------------------------------------- #
def _spread_case(env, bid, ask, price_at_alert=None):
    env.books[TOKEN_A] = _book(TOKEN_A, [bid], [ask])
    px = price_at_alert if price_at_alert is not None else ask
    return _one(env, _rec(price=px, price_at_alert=px))


def test_ten_percent_spread_is_not_over_the_limit(env, paper):
    f = _spread_case(env, 0.09, 0.10)
    assert f["verdict"] == "would_route"
    assert (f["bid"], f["ask"], f["spread_pct"]) == ("0.0900", "0.1000", "0.1000")


def test_just_over_ten_percent_is_wide_spread(env, paper):
    f = _spread_case(env, 0.08, 0.09)
    assert (f["verdict"], f["reason"]) == ("filtered", "wide_spread")
    assert (f["bid"], f["ask"], f["spread_pct"]) == ("0.0800", "0.0900", "0.1111")


def test_the_limit_is_the_shared_constant():
    assert live_config.SPREAD_ALLOW_PCT == 0.10


@pytest.mark.parametrize("book,kind", [
    (None, "none"),
    (_book(TOKEN_A, [], []), "empty"),
    (_book(TOKEN_A, [0.39, float("nan")], [0.40]), "bad_price"),
    (_book(TOKEN_A, [0.39], [0.40, 0.0]), "bad_price"),
    (_book(TOKEN_A, [0.39], [0.40, 1.5]), "bad_price"),
    (_book(TOKEN_A, [-0.1, 0.39], [0.40]), "bad_price"),
    (_book(TOKEN_A, [0.39], [0.40, None]), "bad_price"),
    (_book(TOKEN_A, [0.39], [0.40, "0.41"]), "bad_price"),
    (_book(TOKEN_A, [0.39, True], [0.40]), "bad_price"),
    (_book(TOKEN_A, [0.39], [float("inf")]), "bad_price"),
])
def test_missing_empty_or_corrupt_book_is_unpriced(env, paper, book, kind):
    env.books[TOKEN_A] = book
    f = _one(env, _rec())
    assert (f["verdict"], f["reason"]) == ("filtered", "unpriced_book")
    assert (f["bid"], f["ask"], f["spread_pct"]) == ("-", "-", "-")
    assert f["book"] == kind


@pytest.mark.parametrize("book,bid,ask", [
    (_book(TOKEN_A, [0.39, 0.30], []), "0.3900", "-"),
    (_book(TOKEN_A, [], [0.40, 0.55]), "-", "0.4000"),
])
def test_a_one_sided_book_is_unpriced_and_logs_the_side_that_exists(env, paper, book, bid, ask):
    env.books[TOKEN_A] = book
    sw._route_live_smart_wallet([_rec()], _gamma())
    (line,) = _lines(env, "sw_live RECORD")
    f = _fields(line)
    assert (f["verdict"], f["reason"], f["book"]) == ("filtered", "unpriced_book", "one_sided")
    assert (f["bid"], f["ask"], f["spread_pct"]) == (bid, ask, "-")
    assert f"bid={bid} ask={ask} spread_pct=-" in line


def test_a_level_priced_exactly_one_is_real(env, paper):
    env.books[TOKEN_A] = _book(TOKEN_A, [0.97], [1.0])
    f = _one(env, _rec(price=0.97, price_at_alert=0.97))
    assert f["verdict"] == "would_route"


def test_unsorted_levels_give_the_same_verdict_as_sorted(env, paper):
    env.books[TOKEN_A] = _book(TOKEN_A, [0.10, 0.39, 0.30], [0.55, 0.40, 0.90])
    unsorted_ = _one(env, _rec())
    env.books[TOKEN_A] = _book(TOKEN_A, [0.39, 0.30, 0.10], [0.40, 0.55, 0.90])
    sorted_ = _one(env, _rec())
    assert unsorted_ == sorted_
    assert (sorted_["bid"], sorted_["ask"]) == ("0.3900", "0.4000")


def test_book_is_fetched_once_per_record_that_reaches_the_gate(env, paper):
    env.books[TOKEN_B] = _book(TOKEN_B, [0.39], [0.40])
    recs = [
        _rec(asset=TOKEN_A),                                           # reaches the gate
        _rec(asset=TOKEN_B),                                           # reaches the gate
        _rec(live_eligible=False, live_filter_reason="bot_wallet"),    # filtered earlier
        _rec(asset=""),                                                # filtered earlier
        _rec(title=None),                                              # filtered earlier
    ]
    _route(env, recs)
    assert env.book_calls == [TOKEN_A, TOKEN_B]


def test_a_failing_book_read_filters_only_that_record(env, paper):
    env.books[TOKEN_A] = RuntimeError("connection reset by https://secret.example/x?key=abc")
    env.books[TOKEN_B] = _book(TOKEN_B, [0.39], [0.40])
    fields = _route(env, [_rec(asset=TOKEN_A), _rec(asset=TOKEN_B)])
    assert [(f["verdict"], f["reason"]) for f in fields] == [
        ("filtered", "record_error"), ("would_route", "-")]
    assert "secret.example" not in env.caplog.text       # no message text in the logs


# --------------------------------------------------------------------------- #
# every reason, and the first failing check wins
# --------------------------------------------------------------------------- #
def test_each_reason_appears_for_a_record_built_to_trigger_it(env, paper):
    env.cooldown = {TOKEN_B}
    env.books[TOKEN_B] = _book(TOKEN_B, [0.39], [0.40])
    env.books["tok-wide"] = _book("tok-wide", [0.30], [0.40])
    env.books["tok-none"] = None
    env.books["tok-crossed"] = _book("tok-crossed", [0.50], [0.40])
    env.books["tok-drift"] = _book("tok-drift", [0.69], [0.70])
    cases = {
        "price_band": _rec(live_eligible=False, live_filter_reason="price_band"),
        "bot_wallet": _rec(live_eligible=False, live_filter_reason="bot_wallet"),
        "clv_muted": _rec(live_eligible=False, live_filter_reason="clv_muted"),
        "not_entry": _rec(live_eligible=False, live_filter_reason="not_entry"),
        "eligibility_error": _rec(live_eligible=False, live_filter_reason="eligibility_error"),
        "not_eligible": _rec(live_eligible=False, live_filter_reason=None),
        "no_token": _rec(asset=None),
        "no_title": _rec(title=""),
        "wide_spread": _rec(asset="tok-wide"),
        "crossed_book": _rec(asset="tok-crossed"),
        "unpriced_book": _rec(asset="tok-none"),
        "exit_cooldown": _rec(asset=TOKEN_B),
        "price_drift": _rec(asset="tok-drift"),
        "unusable_rec": _rec(price_at_alert=0.0),
        "record_error": _rec(price_at_alert="not-a-number"),
        "would_route": _rec(),
    }
    names = list(cases)
    fields = _route(env, [cases[n] for n in names])
    got = {n: (f["reason"] if f["verdict"] == "filtered" else "would_route")
           for n, f in zip(names, fields)}
    assert got == {n: n for n in names}


def test_category_and_archetype_reasons_appear(env, paper):
    env.archetype_rows = [(WALLET, "sports", 40, -500.0)]
    assert _one(env, _rec(), gamma=_gamma(category="pop-culture"))["reason"] == "category"
    assert _one(env, _rec(), gamma=_gamma(category="sports"))["reason"] == "archetype"
    assert _one(env, _rec(), gamma=_gamma(category=None))["reason"] == "no_category"


def test_the_first_failing_check_wins(env, paper):
    env.cooldown = {TOKEN_A}
    env.books[TOKEN_A] = None
    env.archetype_rows = [(WALLET, "sports", 40, -500.0)]
    base = dict(live_eligible=False, live_filter_reason="price_band", asset="", title=None)
    # everything wrong at once: the eligibility reason is reported
    assert _one(env, _rec(**base), gamma=_gamma(category=None))["reason"] == "price_band"
    # eligible, still no token/title/category/archetype/book: no_token first
    base2 = dict(asset="", title=None)
    assert _one(env, _rec(**base2), gamma=_gamma(category=None))["reason"] == "no_token"
    # token present: no_category before no_title
    assert _one(env, _rec(title=None), gamma=_gamma(category=None))["reason"] == "no_category"
    # category fine: no_title before the archetype gate and the book
    assert _one(env, _rec(title=None))["reason"] == "no_title"
    # title fine, wallet loses in sports: archetype before the book
    assert _one(env, _rec())["reason"] == "archetype"
    env.archetype_rows = []
    # book unpriced before the cooldown
    assert _one(env, _rec())["reason"] == "unpriced_book"
    env.books[TOKEN_A] = _book(TOKEN_A, [0.39], [0.40])
    assert _one(env, _rec())["reason"] == "exit_cooldown"


def test_no_live_eligible_key_is_filtered_not_eligible(env, paper):
    rec = _rec()
    del rec["live_eligible"]
    del rec["live_filter_reason"]
    f = _one(env, rec)
    assert (f["verdict"], f["reason"]) == ("filtered", "not_eligible")
    assert env.book_calls == []


@pytest.mark.parametrize("value", [1, "true", "True", [1], 1.0, object()])
def test_truthy_but_not_true_is_filtered(env, paper, value):
    f = _one(env, _rec(live_eligible=value))
    assert f["verdict"] == "filtered"
    assert env.book_calls == []


def test_a_refire_that_claims_to_be_eligible_is_still_not_routed(env, paper):
    f = _one(env, _rec(alert_type="refire"))
    assert (f["verdict"], f["reason"]) == ("filtered", "not_entry")


# --------------------------------------------------------------------------- #
# the log lines
# --------------------------------------------------------------------------- #
def test_record_line_for_a_would_route_record_field_by_field(env, paper):
    env.books[TOKEN_A] = _book(TOKEN_A, [0.38, 0.39], [0.41, 0.40])
    sw._route_live_smart_wallet([_rec(price=0.4, price_at_alert=0.4)], _gamma())
    (line,) = _lines(env, "sw_live RECORD")
    assert line == (
        "sw_live RECORD verdict=would_route reason=- alert_type=entry price=0.4000 "
        f"wallet={WALLET} is_bot=false clv_muted=false category=sports bid=0.3900 ask=0.4000 "
        f"spread_pct=0.0250 market={CID[:16]} token={TOKEN_A[-8:]} gamma=present book=ok")
    assert all(" " not in v for v in _fields(line).values())


def test_record_line_for_a_filtered_record_field_by_field(env, paper):
    rec = _rec(price=0.72, price_at_alert=0.72, live_eligible=False,
               live_filter_reason="price_band", clv_muted=True)
    sw._route_live_smart_wallet([rec], _gamma(category="Politics"))
    (line,) = _lines(env, "sw_live RECORD")
    assert line == (
        "sw_live RECORD verdict=filtered reason=price_band alert_type=entry price=0.7200 "
        f"wallet={WALLET} is_bot=false clv_muted=true category=politics bid=- ask=- "
        f"spread_pct=- market={CID[:16]} token={TOKEN_A[-8:]} gamma=present book=-")
    assert all(" " not in v for v in _fields(line).values())


def test_missing_facts_print_a_dash_never_a_made_up_value(env, paper):
    rec = _rec(clv_muted=None, is_bot=None, wallet=None, price=None, price_at_alert=None,
               live_eligible=False, live_filter_reason="eligibility_error", asset=None,
               market=None, alert_type=None)
    f = _one(env, rec, gamma={})
    assert f["verdict"] == "filtered"
    assert {k: f[k] for k in ("clv_muted", "is_bot", "wallet", "price", "token", "market",
                              "alert_type", "category")} == dict.fromkeys(
        ("clv_muted", "is_bot", "wallet", "price", "token", "market", "alert_type", "category"), "-")


def test_dryrun_summary_counts_per_reason_sorted_by_name(env, paper):
    recs = [
        _rec(live_eligible=False, live_filter_reason="price_band"),
        _rec(live_eligible=False, live_filter_reason="bot_wallet"),
        _rec(live_eligible=False, live_filter_reason="price_band"),
        _rec(title=""),
        _rec(),
    ]
    sw._route_live_smart_wallet(recs, _gamma())
    (line,) = _lines(env, "sw_live DRYRUN")
    assert line == ("sw_live DRYRUN mode=PAPER armed=false records=5 would_route=1 "
                    "filtered=bot_wallet:1,no_title:1,price_band:2 gamma_missing=0")


def test_dryrun_summary_with_nothing_filtered(env, paper):
    sw._route_live_smart_wallet([_rec()], _gamma())
    (line,) = _lines(env, "sw_live DRYRUN")
    assert line == "sw_live DRYRUN mode=PAPER armed=false records=1 would_route=1 filtered=- gamma_missing=0"


def test_zero_records_log_nothing(env, paper):
    sw._route_live_smart_wallet([], _gamma())
    assert [r for r in env.caplog.records if r.getMessage().startswith("sw_live")] == []


def test_zero_records_log_nothing_when_armed_either(armed_env):
    sw._route_live_smart_wallet([], _gamma())
    assert [r for r in armed_env.caplog.records if r.getMessage().startswith("sw_live")] == []
    assert armed_env.sizing_calls == 0


# --------------------------------------------------------------------------- #
# not armed: stage 1 only
# --------------------------------------------------------------------------- #
def test_paper_never_executes_reads_a_bankroll_or_sizes(bankroll_trap, paper):
    env = bankroll_trap
    fields = _route(env, [_rec(), _rec(price_at_alert=0.0)])
    assert [f["verdict"] for f in fields] == ["would_route", "filtered"]
    assert env.intents == []
    assert (env.bankroll_reads, env.sizing_calls, env.rail_calls) == (0, 0, 0)
    assert "SIZING DEADLOCK" not in env.caplog.text
    env.fake_clob.assert_no_calls()


def test_live_but_unarmed_is_the_same_as_paper_and_says_so(bankroll_trap, live_unarmed):
    env = bankroll_trap
    _route(env, [_rec()])
    assert env.intents == []
    assert (env.bankroll_reads, env.sizing_calls, env.rail_calls) == (0, 0, 0)
    assert "SIZING DEADLOCK" not in env.caplog.text
    (line,) = _lines(env, "sw_live DRYRUN")
    assert line.startswith("sw_live DRYRUN mode=LIVE armed=false records=1 would_route=1")
    env.fake_clob.assert_no_calls()


@pytest.mark.parametrize("state", [
    ("PAPER", "1", True), ("LIVE", None, True), ("LIVE", "1", False), ("LIVE", "true", True)])
def test_every_unarmed_combination_stops_after_stage_1(bankroll_trap, mode_env, state):
    mode_env(*state)
    _route(bankroll_trap, [_rec()])
    assert bankroll_trap.intents == []
    assert bankroll_trap.bankroll_reads == 0
    assert len(_lines(bankroll_trap, "sw_live DRYRUN")) == 1


def test_a_deadlocking_bankroll_never_logs_the_deadlock_under_paper(env, paper, monkeypatch):
    """The real rail check, with a bankroll that would deadlock the sizer: under
    PAPER nothing reads the bankroll, so nothing can warn about it."""
    monkeypatch.setattr("execution.live_db.connect",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("db reached")))
    _route(env, [_rec()])
    assert "SIZING DEADLOCK" not in env.caplog.text
    assert not any(r.levelno >= logging.WARNING and "sw_live: bankroll" in r.getMessage()
                   for r in env.caplog.records)


# --------------------------------------------------------------------------- #
# armed: stage 2 for the would-route records, with the stage-1 token and book
# --------------------------------------------------------------------------- #
def test_armed_routes_would_route_records_with_the_stage_1_token_and_bid(armed_env):
    armed_env.books[TOKEN_B] = _book(TOKEN_B, [0.30, 0.37], [0.42, 0.41])
    _route(armed_env, [
        _rec(asset=TOKEN_B, outcome_index=None, price_at_alert=0.40, price=0.40),
        _rec(price=0.72, price_at_alert=0.72, live_eligible=False, live_filter_reason="price_band"),
    ])
    (intent,) = armed_env.intents
    assert intent["token_id"] == TOKEN_B
    assert intent["fair_price"] == 0.37                     # the stage-1 best bid
    assert intent["net_edge_taker"] == round(0.40 - 0.41 - 0.02, 4)   # the stage-1 best ask
    assert intent["side"] == "BUY" and intent["category"] == "smart_wallet"
    assert armed_env.book_calls == [TOKEN_B]                # filtered record: no book at all
    assert armed_env.sizing_calls == 1 and armed_env.rail_calls == 1


def test_armed_does_not_fetch_the_book_a_second_time(armed_env):
    _route(armed_env, [_rec(), _rec(asset=TOKEN_B)])
    assert armed_env.book_calls == [TOKEN_A, TOKEN_B]
    assert len(armed_env.intents) == 2


def test_armed_filtered_records_never_reach_the_executor(armed_env):
    _route(armed_env, [_rec(live_eligible=False, live_filter_reason="bot_wallet"),
                       _rec(asset=""), _rec(title=None)])
    assert armed_env.intents == []
    assert armed_env.sizing_calls == 0          # nothing to route: no bankroll read either


def test_armed_logs_the_summary_before_routing(armed_env):
    _route(armed_env, [_rec(), _rec(live_eligible=False, live_filter_reason="bot_wallet")])
    msgs = [r.getMessage() for r in armed_env.caplog.records if r.getMessage().startswith("sw_live")]
    armed_i = next(i for i, m in enumerate(msgs) if m.startswith("sw_live ARMED"))
    routing_i = next(i for i, m in enumerate(msgs) if m.startswith("sw_live: routing"))
    assert msgs[armed_i] == "sw_live ARMED records=2 would_route=1 filtered=bot_wallet:1 gamma_missing=0"
    assert armed_i < routing_i
    assert msgs[routing_i] == "sw_live: routing 1 fired alerts to live executor"
    assert not any(m.startswith("sw_live DRYRUN") for m in msgs)
    assert any(m.startswith("sw_live: bankroll=$50.00") for m in msgs)


def test_armed_order_ref_dedup_still_skips_a_ref_already_in_the_database(armed_env, live_db):
    from datetime import datetime, timezone
    ref = f"sw-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-{CID[:16]}-0"
    cols = [r[1] for r in live_db.conn.execute("PRAGMA table_info(live_open_orders)")]
    values = {"client_order_ref": ref, "status": "live"}
    for c in cols:
        if c in ("id", "client_order_ref"):
            continue
        r = next(x for x in live_db.conn.execute("PRAGMA table_info(live_open_orders)") if x[1] == c)
        if r[3] and r[4] is None and c not in values:          # NOT NULL without default
            values[c] = "x" if (r[2] or "").upper().startswith(("TEXT", "VARCHAR")) else 0
    live_db.conn.execute(
        f"INSERT INTO live_open_orders ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
        list(values.values()))
    live_db.conn.commit()
    _route(armed_env, [_rec()])
    assert armed_env.intents == []
    assert any("dedup" in m for m in _lines(armed_env, "sw_live: dedup"))


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
def test_a_stage_1_error_logs_the_type_returns_and_routes_nothing(env, paper, monkeypatch):
    def boom():
        raise RuntimeError("boom with https://secret.example/x?key=abc")

    monkeypatch.setattr(live_config, "live_armed", boom)
    sw._route_live_smart_wallet([_rec()], _gamma())          # must not raise
    errors = [r for r in env.caplog.records if r.levelno == logging.ERROR]
    assert [r.getMessage() for r in errors] == ["sw_live DRYRUN error=RuntimeError stage=filter"]
    assert env.intents == []
    assert "secret.example" not in env.caplog.text
    assert _lines(env, "sw_live DRYRUN mode") == []


def test_a_stage_1_error_when_armed_routes_nothing(armed_env, monkeypatch):
    monkeypatch.setattr(sw, "_live_stage1",
                        lambda fired, gamma: (_ for _ in ()).throw(KeyError("x")))
    sw._route_live_smart_wallet([_rec()], _gamma())
    assert [r.getMessage() for r in armed_env.caplog.records if r.levelno == logging.ERROR] == [
        "sw_live DRYRUN error=KeyError stage=filter"]
    assert armed_env.intents == [] and armed_env.sizing_calls == 0


def test_a_record_error_filters_that_record_and_the_rest_continue(env, paper):
    fields = _route(env, [_rec(price_at_alert="not-a-number"), _rec(asset=TOKEN_B), "not a dict", _rec()])
    assert [(f["verdict"], f["reason"]) for f in fields] == [
        ("filtered", "record_error"), ("would_route", "-"),
        ("filtered", "not_eligible"), ("would_route", "-")]
    (line,) = _lines(env, "sw_live DRYRUN")
    assert line.endswith("records=4 would_route=2 filtered=not_eligible:1,record_error:1 gamma_missing=1")   # the non-dict has no market


def test_a_gate_that_raises_outside_the_record_handling_is_the_stage_error(env, paper, monkeypatch):
    monkeypatch.setattr(sw, "_live_record_line",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("fmt")))
    sw._route_live_smart_wallet([_rec()], _gamma())
    assert [r.getMessage() for r in env.caplog.records if r.levelno == logging.ERROR] == [
        "sw_live DRYRUN error=ValueError stage=filter"]
    assert env.intents == []


# --------------------------------------------------------------------------- #
# the records carry what the router worked out
# --------------------------------------------------------------------------- #
def test_bid_ask_and_spread_are_kept_on_the_record(env, paper):
    rec = _rec()
    env.books[TOKEN_A] = _book(TOKEN_A, [0.39], [0.40])
    _route(env, [rec])
    assert (rec["bid"], rec["ask"], rec["spread_pct"]) == (0.39, 0.40, 0.025)


# --------------------------------------------------------------------------- #
# the whale-scanner call site (signals/whale_scanner.py, scan_polymarket_flow)
# --------------------------------------------------------------------------- #
def test_meta_write_lock_is_free_when_scanner_hook_hands_records_to_the_router(tmp_path, monkeypatch):
    """The sweep writes to its meta connection (queue_wallet_seen) and commits only
    at the end of the scan, then calls the router, which now makes an order-book
    HTTP request. The router must not run with that connection holding the write
    lock: check_and_fire's own commits free it before it returns records, and no
    records means no router work. Reproduces the sweep's order of operations on a
    real WAL file and probes the lock from a second connection."""
    from scripts import smart_wallet_alert as swa
    from signals.whale_wallets import get_meta_db, queue_wallet_seen

    meta_p = tmp_path / "whale_meta.db"
    monkeypatch.setattr(swa, "SHADOW_DB", tmp_path / "shadow_trades.db")
    monkeypatch.setattr(swa, "page_tier_for", lambda wallet, alert_type: 3)
    monkeypatch.setattr(swa, "dispatch", lambda *a, **k: None)
    monkeypatch.setattr(swa, "_executable_snapshot", lambda *a, **k: {})
    meta = get_meta_db(meta_p)
    meta.execute("CREATE TABLE probe_scratch (k INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
    meta.commit()
    queue_wallet_seen(meta, WALLET, "w", 50_000.0)          # the sweep's uncommitted write
    assert meta.in_transaction

    trade = {"transactionHash": "0xtx", "asset": TOKEN_A, "proxyWallet": WALLET,
             "conditionId": CID, "side": "BUY", "size": 3000.0, "price": 0.4,
             "outcome": "Yes", "outcomeIndex": 0, "title": TITLE, "timestamp": 1_700_000_000}
    gamma = {CID: {"volumeNum": 1_000_000, "last_price": 0.4, "question": TITLE,
                   "endDate": "2026-12-31", "slug": "s", "category": "sports"}}
    smart = {WALLET: {"name": "w", "win_rate": 0.6, "net_pnl": 1, "closed_positions": 50,
                      "source_category": "sports", "is_bot": 0}}
    fired = swa.scanner_hook(meta, [trade], gamma, smart)
    assert len(fired) == 1 and fired[0]["asset"] == TOKEN_A

    assert not meta.in_transaction                            # nothing pending at the router call
    probe = sqlite3.connect(str(meta_p), timeout=0.2, isolation_level=None)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("INSERT INTO probe_scratch (v) VALUES ('at-router-call')")
        probe.execute("COMMIT")
    finally:
        probe.close()
        meta.close()


# --------------------------------------------------------------------------- #
# fix-5 F1: a crossed book is filtered
# --------------------------------------------------------------------------- #
def test_a_crossed_book_is_filtered_and_keeps_its_numbers(env, paper):
    env.books[TOKEN_A] = _book(TOKEN_A, [0.50], [0.40])
    rec = _rec()
    sw._route_live_smart_wallet([rec], _gamma())
    (line,) = _lines(env, "sw_live RECORD")
    f = _fields(line)
    assert (f["verdict"], f["reason"], f["book"]) == ("filtered", "crossed_book", "crossed")
    assert (f["bid"], f["ask"], f["spread_pct"]) == ("0.5000", "0.4000", "-0.2500")
    assert (rec["bid"], rec["ask"], rec["spread_pct"]) == (0.50, 0.40, -0.25)


def test_a_book_with_bid_equal_to_ask_is_a_zero_spread_and_routes(env, paper):
    env.books[TOKEN_A] = _book(TOKEN_A, [0.40], [0.40])
    f = _one(env, _rec())
    assert (f["verdict"], f["reason"], f["book"]) == ("would_route", "-", "ok")
    assert (f["bid"], f["ask"], f["spread_pct"]) == ("0.4000", "0.4000", "0.0000")


def test_a_crossed_book_never_reaches_the_executor_when_armed(armed_env):
    armed_env.books[TOKEN_A] = _book(TOKEN_A, [0.50], [0.40])
    (f,) = _route(armed_env, [_rec()])
    assert f["reason"] == "crossed_book"
    assert armed_env.intents == []
    assert armed_env.sizing_calls == 0


# --------------------------------------------------------------------------- #
# fix-5 F2: the archetype gate fails closed
# --------------------------------------------------------------------------- #
def test_an_archetype_lookup_that_raises_is_archetype_error_and_never_would_route(env, paper, monkeypatch):
    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked: /secret/path")

    monkeypatch.setattr("signals.whale_wallets.get_meta_db", locked)
    sw._route_live_smart_wallet([_rec()], _gamma())
    (line,) = _lines(env, "sw_live RECORD")
    f = _fields(line)
    assert (f["verdict"], f["reason"], f["book"]) == ("filtered", "archetype_error", "-")
    assert env.book_calls == []
    warnings = [r.getMessage() for r in env.caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "OperationalError" in warnings[0]
    assert "secret" not in env.caplog.text and "locked" not in env.caplog.text   # type only


def test_a_failed_archetype_lookup_blocks_the_order_when_armed(armed_env, monkeypatch):
    monkeypatch.setattr("signals.whale_wallets.get_meta_db",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    (f,) = _route(armed_env, [_rec()])
    assert (f["verdict"], f["reason"]) == ("filtered", "archetype_error")
    assert armed_env.intents == []


def test_a_lookup_failure_that_the_gate_swallows_itself_is_also_archetype_error(env, paper, monkeypatch):
    """archetype_gate_ok catches a failing SELECT and returns (True, 'abstain: lookup
    failed ...'). That is a record that was not checked, so the router must not
    treat it like a clean pass. Uses the real gate against a database with no table."""
    def no_table(*a, **k):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr("signals.whale_wallets.get_meta_db", no_table)
    f = _one(env, _rec())
    assert (f["verdict"], f["reason"]) == ("filtered", "archetype_error")


def test_a_wallet_with_no_archetype_record_still_routes(env, paper):
    """The abstain-because-no-evidence answer is a real pass, unlike a failed lookup."""
    env.archetype_rows = []
    assert _one(env, _rec())["verdict"] == "would_route"


# --------------------------------------------------------------------------- #
# fix-5 F4: one list of reasons
# --------------------------------------------------------------------------- #
def _emitted_reasons(*funcs):
    import ast
    import inspect
    import textwrap

    out = set()
    for fn in funcs:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for n in ast.walk(tree):
            if isinstance(n, ast.Return) and n.value is not None:
                v = n.value.values[-1] if isinstance(n.value, ast.BoolOp) else n.value
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    out.add(v.value)
            elif isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) \
                    and isinstance(n.value.value, str):
                for t in n.targets:
                    if (isinstance(t, ast.Name) and t.id == "reason") or (
                            isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                            and t.slice.value == "live_filter_reason"):
                        out.add(n.value.value)
    return out


def test_the_reason_list_is_exactly_what_the_code_can_emit():
    from scripts import smart_wallet_alert as swa

    emitted = _emitted_reasons(sw._live_check_record, sw._live_book_check, sw._live_stage1,
                               swa._live_eligibility, swa.check_and_fire)
    assert {"no_token", "price_drift", "price_band", "crossed_book"} <= emitted   # the scan works
    assert emitted == set(sw.LIVE_FILTER_REASONS)


def test_the_reason_list_is_in_the_order_of_the_checks_and_has_no_duplicates():
    assert sw.LIVE_FILTER_REASONS == (
        "not_entry", "bot_wallet", "price_band", "clv_muted", "eligibility_error", "not_eligible",
        "no_token", "no_category", "category", "no_title", "archetype", "archetype_error",
        "unpriced_book", "crossed_book", "wide_spread", "unusable_rec", "exit_cooldown",
        "price_drift", "record_error", "unknown_reason")
    assert len(set(sw.LIVE_FILTER_REASONS)) == len(sw.LIVE_FILTER_REASONS)


def test_an_unexpected_reason_from_a_record_becomes_unknown_reason_with_one_warning(env, paper):
    long_bad = "weird, reason:" + "x" * 80 + "\nsecond line"
    recs = [_rec(live_eligible=False, live_filter_reason=long_bad),
            _rec(live_eligible=False, live_filter_reason="other thing"),
            _rec(live_eligible=False, live_filter_reason=12345),
            _rec(live_eligible=False, live_filter_reason="price_band")]
    fields = _route(env, recs)
    assert [(f["verdict"], f["reason"]) for f in fields] == [
        ("filtered", "unknown_reason"), ("filtered", "unknown_reason"),
        ("filtered", "unknown_reason"), ("filtered", "price_band")]
    warnings = [r.getMessage() for r in env.caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1                                   # once per call
    assert "weird" in warnings[0] and "\n" not in warnings[0]
    assert "x" * 41 not in warnings[0]                          # at most 40 characters of the value


def test_a_hostile_reason_cannot_break_the_summary_filtered_part(env, paper):
    import re

    recs = [_rec(live_eligible=False, live_filter_reason="a,b:9 c=d"),
            _rec(live_eligible=False, live_filter_reason="price_band")]
    sw._route_live_smart_wallet(recs, _gamma())
    (line,) = _lines(env, "sw_live DRYRUN")
    part = _fields(line)["filtered"]
    assert part == "price_band:1,unknown_reason:1"
    assert all(re.fullmatch(r"[a-z_]+:\d+", item) for item in part.split(","))


# --------------------------------------------------------------------------- #
# fix-5 F5: gamma= and book= on every RECORD line, gamma_missing= on the summary
# --------------------------------------------------------------------------- #
def test_gamma_missing_means_the_lookup_did_not_return_the_market(env, paper):
    f = _one(env, _rec(), gamma={})
    assert (f["gamma"], f["reason"], f["category"]) == ("missing", "no_category", "-")


def test_gamma_present_with_no_category_is_not_missing(env, paper):
    f = _one(env, _rec(), gamma=_gamma(category=None))
    assert (f["gamma"], f["reason"]) == ("present", "no_category")


def test_the_summary_counts_the_records_whose_market_was_not_in_the_gamma_data(env, paper):
    recs = [_rec(), _rec(market="0xother0000000000", asset=TOKEN_B), _rec(market="0xthird00000000")]
    sw._route_live_smart_wallet(recs, _gamma())
    (line,) = _lines(env, "sw_live DRYRUN")
    assert line.endswith("records=3 would_route=1 filtered=no_category:2 gamma_missing=2")


def test_the_armed_summary_has_gamma_missing_too(armed_env):
    _route(armed_env, [_rec(), _rec(market="0xother0000000000", asset=TOKEN_B)])
    (line,) = _lines(armed_env, "sw_live ARMED")
    assert line == "sw_live ARMED records=2 would_route=1 filtered=no_category:1 gamma_missing=1"


def test_book_is_a_dash_when_the_record_was_filtered_before_the_read(env, paper):
    f = _one(env, _rec(live_eligible=False, live_filter_reason="bot_wallet"))
    assert f["book"] == "-" and env.book_calls == []


def test_book_ok_for_a_priced_book_even_when_the_spread_is_too_wide(env, paper):
    f = _spread_case(env, 0.08, 0.09)
    assert (f["reason"], f["book"]) == ("wide_spread", "ok")


def test_book_dash_when_the_read_itself_raised(env, paper):
    env.books[TOKEN_A] = RuntimeError("boom")
    f = _one(env, _rec())
    assert (f["reason"], f["book"]) == ("record_error", "-")


# --------------------------------------------------------------------------- #
# fix-5 F3: a raw trade all the way to the router's log line
# --------------------------------------------------------------------------- #
def test_raw_trade_to_router_record_line_end_to_end(env, paper, tmp_path, monkeypatch):
    """Raw trades in the data API adapter's shape -> the real scanner_hook (fills_from_trades,
    run_from_scan, check_and_fire) -> the records it returns, untouched -> the real
    _route_live_smart_wallet. Catches a field-name mismatch between the two halves."""
    from scripts import smart_wallet_alert as swa

    cid2 = "0xcondition0000000002"
    monkeypatch.setattr(swa, "SHADOW_DB", tmp_path / "shadow_trades.db")
    monkeypatch.setattr(swa, "page_tier_for", lambda wallet, alert_type: 3)
    monkeypatch.setattr(swa, "dispatch", lambda *a, **k: None)
    monkeypatch.setattr(swa, "_executable_snapshot", lambda *a, **k: {})
    meta = sqlite3.connect(str(tmp_path / "whale_meta.db"), timeout=30)
    meta.row_factory = sqlite3.Row
    try:
        trades = [
            {"transactionHash": "0xtx1", "asset": TOKEN_A, "proxyWallet": WALLET, "conditionId": CID,
             "side": "BUY", "size": 3000.0, "price": 0.40, "outcome": "Yes", "outcomeIndex": 0,
             "title": TITLE, "slug": "s1", "timestamp": 1_700_000_000},
            {"transactionHash": "0xtx2", "asset": TOKEN_B, "proxyWallet": WALLET, "conditionId": cid2,
             "side": "BUY", "size": 2000.0, "price": 0.72, "outcome": "No", "outcomeIndex": 1,
             "title": "Knicks vs Nets", "slug": "s2", "timestamp": 1_700_000_001},
        ]
        gamma = {
            CID: {"volumeNum": 1_000_000, "last_price": 0.40, "question": TITLE,
                  "endDate": "2026-12-31", "slug": "s1", "category": "sports"},
            cid2: {"volumeNum": 1_000_000, "last_price": 0.72, "question": "Knicks vs Nets",
                   "endDate": "2026-12-31", "slug": "s2", "category": "sports"},
        }
        smart = {WALLET: {"name": "w", "win_rate": 0.6, "net_pnl": 1, "closed_positions": 50,
                          "source_category": "sports", "is_bot": 0}}
        fired = swa.scanner_hook(meta, trades, gamma, smart)
    finally:
        meta.close()
    assert len(fired) == 2

    env.books[TOKEN_A] = _book(TOKEN_A, [0.39], [0.40])
    env.caplog.clear()
    sw._route_live_smart_wallet(fired, gamma)                 # the records exactly as returned
    lines = _lines(env, "sw_live RECORD")
    assert lines == [
        "sw_live RECORD verdict=would_route reason=- alert_type=entry price=0.4000 "
        f"wallet={WALLET} is_bot=false clv_muted=false category=sports bid=0.3900 ask=0.4000 "
        f"spread_pct=0.0250 market={CID[:16]} token={TOKEN_A[-8:]} gamma=present book=ok",
        "sw_live RECORD verdict=filtered reason=price_band alert_type=entry price=0.7200 "
        f"wallet={WALLET} is_bot=false clv_muted=false category=sports bid=- ask=- "
        f"spread_pct=- market={cid2[:16]} token={TOKEN_B[-8:]} gamma=present book=-",
    ]
    assert env.book_calls == [TOKEN_A]
    (summary,) = _lines(env, "sw_live DRYRUN")
    assert summary == ("sw_live DRYRUN mode=PAPER armed=false records=2 would_route=1 "
                       "filtered=price_band:1 gamma_missing=0")
