"""Task 1.12: scripts/live_preflight.py, the read-only pre-flight for a return to live trading.

Three layers are tested separately: `evaluate` (pure), `gather` / `main` with every source
injected, and the default readers that touch the world (database, /proc, systemctl) with
their own I/O injected. Nothing here reads /proc, runs systemctl, opens a production
database or calls the exchange.

Fixtures follow the plan: fixed cap $5 and tier cap $5 (so nothing depends on production
values), fraction 11%, sizer 10% with a $2 floor and $15 cap, exchange minimum 5 shares,
price ceiling 0.60 (minimum order $3.00), kill floor $10.
"""

import io
import re
import sqlite3
from pathlib import Path

import pytest

from scripts import live_preflight as pf

REPO = Path(__file__).resolve().parents[2]
SM = {"smart_wallet"}
UNITS = ("polyclawd-scheduler", "polyclawd-api")


def sizer(bankroll):
    return max(2.0, min(15.0, bankroll * 0.10))


def mk(**over):
    base = dict(
        failures={},
        arming={"mode": "PAPER", "enforce": False, "path_ready": False, "armed": False},
        caps={"fixed": 5.0, "tier": 5.0, "frac": 0.11},
        kill_floor=10.0,
        daily_halt=30.0,
        governor_bankroll=16.97,
        cash=16.97,
        positions=[],
        tokens=["T1"],
        min_reads={"T1": {"value": 5.0, "source": "exchange"}},
        min_failures={},
        price_ceiling=0.60,
        allowlists={u: set(SM) for u in UNITS},
        stop={"universal": 0.40, "spread_allow": 0.10},
        sizers={"smart_wallet": sizer},
    )
    base.update(over)
    return base


def checks(rep, prefix):
    return [c for c in rep["checks"] if c["name"].startswith(prefix)]


def one(rep, prefix):
    found = checks(rep, prefix)
    assert len(found) == 1, (prefix, [c["name"] for c in rep["checks"]])
    return found[0]


def line(rep, name):
    vals = [v for n, v in rep["lines"] if n == name]
    assert vals, (name, [n for n, _ in rep["lines"]])
    return vals[0]


# --------------------------------------------------------------------------- plan fixtures


def test_today_fixture_cap_under_sizer_fails_naming_both_numbers():
    rep = pf.evaluate(mk(cash=16.97))
    c = one(rep, "cap_covers_sizer")
    assert c["status"] == "FAIL"
    assert "1.87" in c["text"] and "2.00" in c["text"]
    assert rep["exit"] != 0


def test_pass_fixture_all_sizing_checks_pass_and_exit_zero():
    rep = pf.evaluate(mk(cash=31.0, governor_bankroll=31.0))
    cap = one(rep, "cap_covers_sizer")
    mn = one(rep, "sizer_covers_exchange_minimum")
    assert cap["status"] == "PASS" and "3.41" in cap["text"] and "3.10" in cap["text"]
    assert mn["status"] == "PASS" and "3.00" in mn["text"]
    assert one(rep, "bankroll_above_kill_floor")["status"] == "PASS"
    assert not [c for c in rep["checks"] if c["status"] == "FAIL"]
    assert rep["exit"] == 0


def test_boundary_fixture_sizer_under_minimum_fails_exchange_minimum_line():
    rep = pf.evaluate(mk(cash=29.0, governor_bankroll=29.0))
    c = one(rep, "sizer_covers_exchange_minimum")
    assert c["status"] == "FAIL" and "2.90" in c["text"] and "3.00" in c["text"]
    assert rep["exit"] != 0


def test_smallest_passing_bankroll_is_30_00_and_29_99_fails():
    rep = pf.evaluate(mk())
    assert rep["smallest_passing"] == pytest.approx(30.00)
    assert line(rep, "SMALLEST_PASSING_BANKROLL").startswith("$30.00")
    below = pf.evaluate(mk(cash=29.99, governor_bankroll=29.99))
    assert one(below, "sizer_covers_exchange_minimum")["status"] == "FAIL"
    at = pf.evaluate(mk(cash=30.00, governor_bankroll=30.00))
    assert not [c for c in at["checks"] if c["status"] == "FAIL"]


def test_smallest_passing_bankroll_none_up_to_1000():
    rep = pf.evaluate(mk(price_ceiling=0.60, min_reads={"T1": {"value": 5000.0, "source": "exchange"}}))
    assert line(rep, "SMALLEST_PASSING_BANKROLL") == "none up to $1,000"
    assert rep["smallest_passing"] is None


def test_cap_equal_to_sizer_passes_greater_or_equal():
    """cap == sizer exactly: bankroll 20, fraction 0.10 -> cap 2.00, sizer max(2, 2.0) = 2.00."""
    rep = pf.evaluate(
        mk(cash=20.0, governor_bankroll=20.0, caps={"fixed": 5.0, "tier": 5.0, "frac": 0.10},
           price_ceiling=0.40)
    )
    assert one(rep, "cap_covers_sizer")["status"] == "PASS"
    assert one(rep, "sizer_covers_exchange_minimum")["status"] == "PASS"  # 2.00 >= 5 x 0.40


# --------------------------------------------------------------------------- cap formulas


def test_governor_formula_uses_the_tier_cap_scheduler_formula_does_not():
    caps = {"fixed": 15.0, "tier": 25.0, "frac": 0.11}
    assert pf.governor_cap(200.0, 15.0, 25.0, 0.11) == pytest.approx(22.0)  # min(25, 22)
    assert pf.scheduler_cap(200.0, 15.0, 0.11) == pytest.approx(15.0)  # min(15, 22)
    rep = pf.evaluate(mk(cash=200.0, governor_bankroll=200.0, caps=caps))
    assert line(rep, "EFFECTIVE_CAP_GOVERNOR").startswith("$22.00")
    assert line(rep, "EFFECTIVE_CAP_SCHEDULER_CHECK").startswith("$15.00")
    assert one(rep, "cap_formulas_agree")["status"] == "WARN"


def test_formulas_agree_below_136_36_and_differ_above():
    caps = {"fixed": 15.0, "tier": 25.0, "frac": 0.11}
    below = pf.evaluate(mk(cash=136.0, governor_bankroll=136.0, caps=caps))
    assert one(below, "cap_formulas_agree")["status"] == "PASS"
    above = pf.evaluate(mk(cash=137.0, governor_bankroll=137.0, caps=caps))
    assert one(above, "cap_formulas_agree")["status"] == "WARN"
    assert 25.0 / 0.11 == pytest.approx(227.27, abs=0.01)  # tier side
    assert 15.0 / 0.11 == pytest.approx(136.36, abs=0.01)  # where fixed 15 starts to bind


def test_tier_cap_that_cannot_be_read_falls_back_to_the_fixed_cap_like_the_governor():
    assert pf.governor_cap(200.0, 15.0, None, 0.11) == pytest.approx(15.0)


# --------------------------------------------------------------------------- allowlists


def test_differing_allowlists_fail_agree():
    rep = pf.evaluate(mk(allowlists={UNITS[0]: {"smart_wallet"}, UNITS[1]: {"smart_wallet", "baseball_total"}}))
    assert one(rep, "allowlists_agree")["status"] == "FAIL"
    assert rep["exit"] != 0


def test_unreadable_allowlist_fails_readable_and_is_not_treated_as_empty():
    rep = pf.evaluate(
        mk(allowlists={UNITS[0]: {"smart_wallet"}, UNITS[1]: None},
           failures={"allowlist[polyclawd-api]": "PermissionError: denied"})
    )
    c = one(rep, "allowlist_readable")
    assert c["status"] == "FAIL" and "polyclawd-api" in c["text"]
    assert one(rep, "allowlists_agree")["status"] == "FAIL"
    assert one(rep, "allowlist_is_smart_wallet_only")["status"] == "FAIL"
    assert rep["exit"] != 0


def test_both_unreadable_fails_readable():
    rep = pf.evaluate(mk(allowlists={UNITS[0]: None, UNITS[1]: None}))
    assert one(rep, "allowlist_readable")["status"] == "FAIL"


def test_not_smart_wallet_only_fails_and_soccer_has_no_sizer():
    both = {"smart_wallet", "soccer_match_3way"}
    rep = pf.evaluate(mk(allowlists={u: set(both) for u in UNITS}))
    assert one(rep, "allowlist_is_smart_wallet_only")["status"] == "FAIL"
    assert line(rep, "SIZER[soccer_match_3way]") == "no live sizer known"
    assert rep["smallest_passing"] is None  # a strategy with no sizer can never pass
    assert rep["exit"] != 0


def test_good_allowlists_pass_the_three_allowlist_checks():
    rep = pf.evaluate(mk(cash=31.0))
    for name in ("allowlist_readable", "allowlists_agree", "allowlist_is_smart_wallet_only"):
        assert one(rep, name)["status"] == "PASS"


# --------------------------------------------------------------------------- exchange minimum


def test_no_sample_fails_exchange_minimum_read():
    rep = pf.evaluate(mk(tokens=[], min_reads={}, min_failures={}))
    c = one(rep, "exchange_minimum_read")
    assert c["status"] == "FAIL" and "no sample" in c["text"]
    assert rep["exit"] != 0


def test_every_read_failing_fails_naming_the_tokens():
    rep = pf.evaluate(
        mk(tokens=["TOKA", "TOKB"], min_reads={},
           min_failures={"TOKA": "ClobError: down", "TOKB": "ClobError: down"})
    )
    c = one(rep, "exchange_minimum_read")
    assert c["status"] == "FAIL" and "TOKA" in c["text"] and "TOKB" in c["text"]
    assert one(rep, "sizer_covers_exchange_minimum")["status"] == "FAIL"  # unknown is not covered


def test_largest_minimum_is_used():
    rep = pf.evaluate(
        mk(cash=31.0, tokens=["T1", "T2"],
           min_reads={"T1": {"value": 5.0, "source": "exchange"}, "T2": {"value": 15.0, "source": "exchange"}})
    )
    assert line(rep, "EXCHANGE_MINIMUM_SHARES").startswith("15")
    assert line(rep, "MIN_ORDER_USD").startswith("$9.00")  # 15 x 0.60
    assert one(rep, "sizer_covers_exchange_minimum")["status"] == "FAIL"  # 3.10 < 9.00


def test_environment_override_is_labelled_as_such():
    rep = pf.evaluate(mk(min_reads={"T1": {"value": 7.0, "source": "POLYCLAWD_MIN_ORDER_SHARES"}}))
    text = line(rep, "EXCHANGE_MINIMUM_SOURCE[T1]")
    assert "POLYCLAWD_MIN_ORDER_SHARES" in text and "exchange" not in text.replace("POLYCLAWD_MIN_ORDER_SHARES", "")


def test_partial_failure_still_reads_the_ones_that_worked_and_lists_the_failure():
    rep = pf.evaluate(mk(tokens=["T1", "T2"], min_failures={"T2": "ClobError: x"}))
    assert one(rep, "exchange_minimum_read")["status"] == "PASS"
    assert "T2" in one(rep, "exchange_minimum_read")["text"]


# --------------------------------------------------------------------------- market value


def test_market_value_bankroll_is_cash_plus_best_bid_times_shares():
    rep = pf.evaluate(mk(cash=12.22, governor_bankroll=16.97,
                         positions=[{"token_id": "T1", "shares": 19.82, "bid": 0.08}]))
    assert rep["bankroll"] == pytest.approx(13.8056, abs=1e-4)
    assert line(rep, "BANKROLL_MARKET_VALUE").startswith("$13.81")
    assert line(rep, "BANKROLL_GOVERNOR_READING").startswith("$16.97")


def test_position_without_a_bid_is_valued_zero_and_listed_unpriced():
    rep = pf.evaluate(mk(cash=12.0, positions=[
        {"token_id": "T1", "shares": 10.0, "bid": 0.5},
        {"token_id": "TNOBID", "shares": 99.0, "bid": None}]))
    assert rep["bankroll"] == pytest.approx(17.0)
    assert "TNOBID" in line(rep, "UNPRICED_POSITIONS")


def test_failed_cash_read_is_unknown_and_fails_the_kill_floor_check_never_a_guess():
    rep = pf.evaluate(mk(cash=None, failures={"cash": "ClobError: no credentials"}))
    assert rep["bankroll"] is None
    c = one(rep, "bankroll_above_kill_floor")
    assert c["status"] == "FAIL" and "unknown" in c["text"]
    assert "unknown" in line(rep, "BANKROLL_MARKET_VALUE").lower()
    assert "cash" in " ".join(f"{n}{v}" for n, v in rep["lines"] if n.startswith("FAILED"))
    assert rep["exit"] != 0


def test_bankroll_equal_to_kill_floor_is_not_above():
    rep = pf.evaluate(mk(cash=10.0))
    assert one(rep, "bankroll_above_kill_floor")["status"] == "FAIL"


# --------------------------------------------------------------------------- warnings and info


def test_daily_halt_reachable_warns_with_production_numbers():
    rep = pf.evaluate(mk(cash=13.81, daily_halt=30.0, kill_floor=10.0))
    c = one(rep, "daily_halt_reachable")
    assert c["status"] == "WARN" and "30.00" in c["text"] and "3.81" in c["text"]
    ok = pf.evaluate(mk(cash=100.0, daily_halt=30.0, kill_floor=10.0))
    assert one(ok, "daily_halt_reachable")["status"] == "PASS"


def test_stop_bound_prints_sum_and_warns_when_not_forty_percent():
    rep = pf.evaluate(mk())
    c = one(rep, "stop_bound")
    assert c["status"] == "INFO" and "0.50" in c["text"]
    odd = pf.evaluate(mk(stop={"universal": 0.35, "spread_allow": 0.10}))
    assert one(odd, "stop_bound")["status"] == "WARN"


def test_arming_is_informational_and_points_at_the_governor_route():
    rep = pf.evaluate(mk())
    c = one(rep, "arming")
    assert c["status"] == "INFO"
    assert "mode=PAPER" in c["text"] and "/api/live/governor" in c["text"]


# --------------------------------------------------------------------------- gather and main


def make_sources(**over):
    ob = {}

    def orderbook(token):
        return ob.get(token)

    src = dict(
        arming=lambda: {"mode": "PAPER", "enforce": False, "path_ready": False, "armed": False},
        caps=lambda: {"fixed": 5.0, "tier": 5.0, "frac": 0.11},
        kill_floor=lambda: 10.0,
        daily_halt=lambda: 30.0,
        governor_bankroll=lambda: 31.0,
        cash=lambda: 31.0,
        positions=lambda: [],
        orderbook=orderbook,
        min_order=lambda token: (5.0, "exchange"),
        price_ceiling=lambda: 0.60,
        allowlist=lambda unit: set(SM),
        stop_bounds=lambda: (0.40, 0.10),
        sizers={"smart_wallet": sizer},
    )
    src.update(over)
    src["_books"] = ob
    return src


class Level:
    def __init__(self, price):
        self.price = price
        self.size = 1.0


class Book:
    def __init__(self, prices):
        self.bids = [Level(p) for p in prices]


def run_main(argv, **over):
    src = make_sources(**over)
    books = src.pop("_books")
    for k, v in over.pop("books", {}).items() if "books" in over else []:
        books[k] = v
    buf = io.StringIO()
    rc = pf.main(argv, sources=src, out=buf)
    return rc, buf.getvalue()


def test_main_passing_run_prints_every_named_line_and_exits_zero():
    rc, out = run_main(["--token", "T1"])
    assert rc == 0, out
    for name in ("MODE", "ENFORCE", "LIVE_ORDER_PATH_READY", "ARMED", "KILL_FLOOR", "DAILY_LOSS_HALT",
                 "FIXED_PER_TRADE_CAP", "TIER_CAP", "FRACTION", "EFFECTIVE_CAP_GOVERNOR",
                 "EFFECTIVE_CAP_SCHEDULER_CHECK", "SPREAD_ALLOW_PCT", "BANKROLL_GOVERNOR_READING",
                 "EXCHANGE_CASH", "BANKROLL_MARKET_VALUE", "SIZER[smart_wallet]",
                 "EXCHANGE_MINIMUM_SHARES", "PRICE_CEILING", "MIN_ORDER_USD",
                 "ALLOWLIST[polyclawd-scheduler]", "ALLOWLIST[polyclawd-api]", "SMALLEST_PASSING_BANKROLL"):
        assert re.search(rf"^{re.escape(name)}: ", out, re.M), (name, out)
    assert re.search(r"^CHECKS$", out, re.M)
    assert out.rstrip().splitlines()[-1].startswith("RESULT: PASS")


def test_main_checks_section_lists_each_check_once():
    rc, out = run_main(["--token", "T1"])
    section = out.split("\nCHECKS\n", 1)[1]
    names = re.findall(r"^(?:PASS|FAIL|WARN|INFO) (\S+):", section, re.M)
    assert len(names) == len(set(names))
    for expected in ("cap_covers_sizer[smart_wallet]", "sizer_covers_exchange_minimum[smart_wallet]",
                     "bankroll_above_kill_floor", "exchange_minimum_read", "allowlist_readable",
                     "allowlists_agree", "allowlist_is_smart_wallet_only", "cap_formulas_agree",
                     "daily_halt_reachable", "stop_bound", "arming"):
        assert expected in names, (expected, names)


def test_main_exit_code_is_nonzero_only_for_a_fail():
    rc, out = run_main(["--token", "T1"], cash=lambda: 29.0, governor_bankroll=lambda: 29.0)
    assert rc == 1 and re.search(r"^FAIL sizer_covers_exchange_minimum", out, re.M)
    assert out.rstrip().splitlines()[-1].startswith("RESULT: FAIL")
    rc, out = run_main(["--token", "T1"], daily_halt=lambda: 30.0, cash=lambda: 31.0)
    assert rc == 0 and re.search(r"^WARN daily_halt_reachable", out, re.M)


def test_main_unsorted_bid_levels_use_the_maximum():
    src = make_sources(cash=lambda: 10.0, positions=lambda: [{"token_id": "P1", "shares": 100.0}])
    src["_books"]["P1"] = Book([0.05, 0.31, 0.12])
    books = src.pop("_books")
    src["orderbook"] = books.get
    buf = io.StringIO()
    pf.main(["--token", "T1"], sources=src, out=buf)
    assert re.search(r"^BANKROLL_MARKET_VALUE: \$41\.00", buf.getvalue(), re.M), buf.getvalue()


def test_gather_book_without_usable_bid_is_unpriced_and_fetch_error_is_recorded():
    def orderbook(token):
        if token == "BOOM":
            raise ConnectionError("book down")
        return {"EMPTY": Book([]), "ZERO": Book([0.0, -1.0, float("nan")])}.get(token)

    src = make_sources(
        positions=lambda: [{"token_id": t, "shares": 10.0} for t in ("EMPTY", "ZERO", "NONE", "BOOM")],
        orderbook=orderbook)
    src.pop("_books")
    inp = pf.gather(["T1"], src)
    bids = {p["token_id"]: p["bid"] for p in inp["positions"]}
    assert bids == {"EMPTY": None, "ZERO": None, "NONE": None, "BOOM": None}
    assert "orderbook[BOOM]" in inp["failures"] and "ConnectionError" in inp["failures"]["orderbook[BOOM]"]


def test_gather_samples_open_position_tokens_plus_cli_tokens_once():
    seen = []

    def min_order(token):
        seen.append(token)
        return 5.0, "exchange"

    src = make_sources(positions=lambda: [{"token_id": "P1", "shares": 1.0}, {"token_id": "P2", "shares": 1.0},
                                          {"token_id": "", "shares": 1.0}],
                       min_order=min_order)
    src.pop("_books")
    inp = pf.gather(["P2", "CLI"], src)
    assert seen == ["P1", "P2", "CLI"]
    assert inp["tokens"] == ["P1", "P2", "CLI"]


def test_gather_records_every_failing_source_with_name_and_short_error_and_guesses_nothing():
    def boom(*a):
        raise RuntimeError("x" * 500)

    src = make_sources(cash=boom, governor_bankroll=boom, kill_floor=boom, daily_halt=boom, caps=boom,
                       price_ceiling=boom, stop_bounds=boom, arming=boom, positions=boom,
                       allowlist=boom, min_order=boom)
    src.pop("_books")
    inp = pf.gather(["T1"], src)
    for key in ("cash", "governor_bankroll", "kill_floor", "daily_halt", "caps", "price_ceiling",
                "stop_bounds", "arming", "positions", "allowlist[polyclawd-scheduler]",
                "allowlist[polyclawd-api]", "min_order[T1]"):
        assert key in inp["failures"], (key, sorted(inp["failures"]))
        assert inp["failures"][key].startswith("RuntimeError: ")
        assert len(inp["failures"][key]) <= len("RuntimeError: ") + 200
    assert inp["cash"] is None and inp["kill_floor"] is None and inp["positions"] is None
    assert inp["allowlists"] == {"polyclawd-scheduler": None, "polyclawd-api": None}
    rep = pf.evaluate(inp)
    assert rep["exit"] != 0
    assert rep["bankroll"] is None


def test_gather_min_order_failure_goes_to_min_failures_and_not_to_a_default():
    def min_order(token):
        raise RuntimeError("down")

    src = make_sources(min_order=min_order)
    src.pop("_books")
    inp = pf.gather(["T1"], src)
    assert inp["min_reads"] == {} and "T1" in inp["min_failures"]


def test_main_usage_error_exit_2():
    with pytest.raises(SystemExit) as ei:
        pf.main(["--bogus"], sources=make_sources(), out=io.StringIO())
    assert ei.value.code == 2


# --------------------------------------------------------------------------- default readers


def test_allowlist_reader_parses_environ_of_main_pid():
    env = b"A=1\0POLYCLAWD_LIVE_STRATEGY_ALLOWLIST=smart_wallet, soccer\0B=2\0"
    got = pf.read_unit_allowlist("polyclawd-scheduler", main_pid=lambda unit: 4242,
                                 read_environ=lambda pid: env if pid == 4242 else b"")
    assert got == {"smart_wallet", "soccer"}


@pytest.mark.parametrize("pid,env,exc", [
    (0, b"", RuntimeError),  # unit not running
    (77, None, PermissionError),  # unreadable
    (77, b"A=1\0", LookupError),  # variable absent
])
def test_allowlist_reader_failures_raise_never_return_empty(pid, env, exc):
    def read_environ(p):
        if env is None:
            raise PermissionError("denied")
        return env

    with pytest.raises(exc):
        pf.read_unit_allowlist("u", main_pid=lambda unit: pid, read_environ=read_environ)


def test_allowlist_reader_present_but_empty_is_an_empty_set_not_a_failure():
    got = pf.read_unit_allowlist("u", main_pid=lambda unit: 5,
                                 read_environ=lambda pid: b"POLYCLAWD_LIVE_STRATEGY_ALLOWLIST=\0")
    assert got == set()


def test_main_pid_parses_systemctl_output():
    class R:
        def __init__(self, out, rc=0):
            self.stdout, self.returncode, self.stderr = out, rc, ""

    assert pf.systemctl_main_pid("u", run=lambda *a, **k: R("1234\n")) == 1234
    with pytest.raises(RuntimeError):
        pf.systemctl_main_pid("u", run=lambda *a, **k: R("", rc=1))
    with pytest.raises(RuntimeError):
        pf.systemctl_main_pid("u", run=lambda *a, **k: R("garbage\n"))


def test_database_is_opened_read_only_and_a_missing_file_is_not_created(tmp_path, monkeypatch):
    missing = tmp_path / "sub" / "nope.db"
    monkeypatch.setenv("POLYCLAWD_LIVE_DB", str(missing))
    with pytest.raises(Exception):
        pf.read_open_positions()
    with pytest.raises(Exception):
        pf.read_governor_bankroll()
    assert not missing.exists() and not missing.parent.exists()


def test_database_connection_refuses_writes_and_reads_the_two_tables(tmp_path, monkeypatch):
    db = tmp_path / "live.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE live_positions (id INTEGER PRIMARY KEY, token_id TEXT, shares REAL, status TEXT);
        CREATE TABLE live_portfolio_state (id INTEGER PRIMARY KEY AUTOINCREMENT, bankroll REAL);
        INSERT INTO live_positions (token_id, shares, status) VALUES ('A', 3.5, 'open'), ('B', 9, 'closed'),
                                                                     ('C', 2, 'open');
        INSERT INTO live_portfolio_state (bankroll) VALUES (20.0), (16.97);
    """)
    con.commit()
    con.close()
    monkeypatch.setenv("POLYCLAWD_LIVE_DB", str(db))
    ro = pf.open_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO live_portfolio_state (bankroll) VALUES (1)")
    ro.close()
    assert pf.read_open_positions() == [{"token_id": "A", "shares": 3.5}, {"token_id": "C", "shares": 2.0}]
    assert pf.read_governor_bankroll() == pytest.approx(16.97)


def test_empty_portfolio_state_table_is_a_failure_not_zero(tmp_path, monkeypatch):
    db = tmp_path / "live.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE live_portfolio_state (id INTEGER PRIMARY KEY AUTOINCREMENT, bankroll REAL)")
    con.commit()
    con.close()
    monkeypatch.setenv("POLYCLAWD_LIVE_DB", str(db))
    with pytest.raises(LookupError):
        pf.read_governor_bankroll()


# --------------------------------------------------------------------------- price ceiling and sizer


def test_price_ceiling_is_read_from_source_without_importing_the_alert_module():
    import sys
    before = "scripts.smart_wallet_alert" in sys.modules
    assert pf.read_price_ceiling() == pytest.approx(0.60)
    assert ("scripts.smart_wallet_alert" in sys.modules) == before


def test_default_sizer_is_the_fast_polls_and_matches_the_plan_constants():
    sizers = pf.default_sizers()
    assert set(sizers) == {"smart_wallet"}
    assert sizers["smart_wallet"](31.0) == pytest.approx(3.10)
    assert sizers["smart_wallet"](10.0) == pytest.approx(2.00)
    assert sizers["smart_wallet"](500.0) == pytest.approx(15.0)


# --------------------------------------------------------------------------- drift tests


def _squash(text):
    return re.sub(r"\s+", " ", text)


def test_governor_formula_copy_still_matches_the_governor_source():
    src = _squash((REPO / "execution" / "risk_governor.py").read_text())
    assert _squash('tier_cap = live_config._parse_float("POLYCLAWD_TIER_SIZE_CAP", "25.0")') in src
    assert "flat_cap = max(tier_cap, live_config.per_trade_cap())" in src
    assert "except Exception: flat_cap = live_config.per_trade_cap()" in src
    assert "cap = min(flat_cap, self._bankroll * live_config.per_trade_frac())" in src
    assert "if size_usd > cap:" in src
    assert pf.TIER_CAP_VAR == "POLYCLAWD_TIER_SIZE_CAP" and pf.TIER_CAP_DEFAULT == "25.0"


def test_scheduler_formula_copy_still_matches_the_scheduler_source():
    src = _squash((REPO / "services" / "scheduler.py").read_text())
    assert "eff = min(_lc.per_trade_cap(), bankroll * _lc.per_trade_frac())" in src
    assert "inert = [s for s in allow if sizes.get(s) and eff < sizes[s]]" in src


def test_unit_names_and_variable_match_the_services_and_live_config():
    assert pf.UNITS == UNITS
    assert pf.ALLOWLIST_VAR == "POLYCLAWD_LIVE_STRATEGY_ALLOWLIST"
    assert pf.ALLOWLIST_VAR in (REPO / "execution" / "live_config.py").read_text()
