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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import live_preflight as pf

REPO = Path(__file__).resolve().parents[2]
SM = {"smart_wallet"}
UNITS = ("polyclawd-scheduler", "polyclawd-api")


NOW = datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc)


def row_aged(minutes):
    """The newest live_portfolio_state row, written `minutes` before NOW (how the governor writes ts)."""
    return {"has_column": True, "value": (NOW - timedelta(minutes=minutes)).isoformat()}


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
        own_override=None,
        unit_vars={u: {} for u in UNITS},
        env_file={"path": "config/polymarket.env", "mtime": 1_000.0},
        unit_started={u: 2_000.0 for u in UNITS},
        stored_row=row_aged(5),
        now=NOW,
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


def test_smallest_passing_bankroll_wording_describes_the_settings_and_claims_nothing_unchecked():
    text = line(pf.evaluate(mk()), "SMALLEST_PASSING_BANKROLL")
    assert "lowest bankroll at which the sizing checks pass" in text
    assert "searched in cents up to $1,000" in text and "not advice" in text
    assert "every sizing check passes from here" not in text


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
        own_override=lambda: None,
        unit_vars=lambda unit: {},
        env_file=lambda: {"path": "config/polymarket.env", "mtime": 1_000.0},
        unit_started=lambda unit: 2_000.0,
        stored_row=lambda: row_aged(5),
        now=lambda: NOW,
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


# =========================================================================== fix-6, part A


def ok_mk(**over):
    """A passing base: bankroll 31 (stored reading and market value)."""
    return mk(cash=31.0, governor_bankroll=31.0, **over)


# --------------------------------------------------------------------------- A2 override


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "abc", "2"])
def test_override_that_is_not_positive_finite_or_below_the_exchange_minimum_fails(raw):
    rep = pf.evaluate(ok_mk(own_override=raw))
    c = one(rep, "min_order_override")
    assert c["status"] == "FAIL" and "this process" in c["text"], c
    # the sizing check is computed from the exchange's 5 shares x 0.60, never from the override
    mn = one(rep, "sizer_covers_exchange_minimum")
    assert "$3.00" in mn["text"] and "5 shares" in mn["text"]
    assert rep["exit"] != 0


def test_override_equal_to_the_exchange_minimum_warns_and_does_not_change_the_exit_code():
    plain = pf.evaluate(ok_mk())
    rep = pf.evaluate(ok_mk(own_override="5"))
    c = one(rep, "min_order_override")
    assert c["status"] == "WARN" and "instead of the exchange's figure" in c["text"] and "5" in c["text"]
    assert rep["exit"] == plain["exit"] == 0


def test_override_above_the_exchange_minimum_warns():
    assert one(pf.evaluate(ok_mk(own_override="6")), "min_order_override")["status"] == "WARN"


def test_override_not_set_anywhere_passes_and_says_so():
    c = one(pf.evaluate(ok_mk(own_override="  ")), "min_order_override")
    assert c["status"] == "PASS" and "not set" in c["text"]


def test_override_set_only_in_one_units_environment_names_that_unit():
    rep = pf.evaluate(ok_mk(unit_vars={UNITS[0]: {}, UNITS[1]: {"POLYCLAWD_MIN_ORDER_SHARES": "0"}}))
    c = one(rep, "min_order_override")
    assert c["status"] == "FAIL" and UNITS[1] in c["text"] and UNITS[0] not in c["text"]


def test_override_check_does_not_pass_when_a_units_environment_could_not_be_read():
    c = one(pf.evaluate(ok_mk(unit_vars={UNITS[0]: {}, UNITS[1]: None})), "min_order_override")
    assert c["status"] == "FAIL" and UNITS[1] in c["text"]


def test_default_min_order_source_ignores_the_override(monkeypatch):
    from execution import clob_client

    seen = {}

    def fake(token, strict=False):
        seen["strict"] = strict
        return 15.0

    monkeypatch.setattr(clob_client, "get_min_order_size", fake)
    monkeypatch.setenv("POLYCLAWD_MIN_ORDER_SHARES", "1")
    assert pf._default_min_order("T") == (15.0, "exchange")
    assert seen["strict"] is True


# --------------------------------------------------------------------------- A3 partial read


def test_partial_read_is_pass_plus_a_warn_naming_each_failed_token():
    rep = pf.evaluate(ok_mk(tokens=["T1", "T2", "T3"], min_failures={"T2": "ClobError: x", "T3": "ClobError: y"}))
    assert one(rep, "exchange_minimum_read")["status"] == "PASS"
    c = one(rep, "exchange_minimum_partial")
    assert c["status"] == "WARN" and "T2" in c["text"] and "ClobError: x" in c["text"]
    assert "T3" in c["text"] and "ClobError: y" in c["text"] and "T1" not in c["text"]
    assert rep["exit"] == 0


def test_no_partial_warning_when_every_read_worked():
    assert one(pf.evaluate(ok_mk()), "exchange_minimum_partial")["status"] == "PASS"


# --------------------------------------------------------------------------- A4 bankrolls


def test_each_consumer_prints_which_reading_it_uses():
    rep = pf.evaluate(ok_mk())
    for name in ("BANKROLL_USED_BY_GOVERNOR", "BANKROLL_USED_BY_ROUTER", "BANKROLL_USED_BY_SCHEDULER_START_CHECK"):
        assert line(rep, name).startswith("$31.00"), name
        assert "live_portfolio_state" in line(rep, name)


def test_cap_and_sizer_use_the_stored_reading_the_consumers_read_not_the_market_value():
    # stored reading 40, market value 20: cap = min(5, 4.40) = 4.40, sizer = 4.00 -> PASS
    rep = pf.evaluate(mk(governor_bankroll=40.0, cash=20.0))
    c = one(rep, "cap_covers_sizer")
    assert c["status"] == "PASS" and "$4.40" in c["text"] and "$4.00" in c["text"] and "$40.00" in c["text"]
    assert line(rep, "SIZER[smart_wallet]") == "$4.00"
    assert line(rep, "EFFECTIVE_CAP_GOVERNOR").startswith("$4.40")
    assert one(rep, "sizer_covers_exchange_minimum")["status"] == "PASS"  # 4.00 >= 3.00


def test_result_follows_the_stored_reading_in_both_directions():
    low_stored = pf.evaluate(mk(governor_bankroll=18.0, cash=40.0))  # market value would pass
    assert one(low_stored, "cap_covers_sizer")["status"] == "FAIL"  # cap 1.98 < sizer 2.00
    low_market = pf.evaluate(mk(governor_bankroll=40.0, cash=18.0))  # market value would fail
    assert one(low_market, "cap_covers_sizer")["status"] == "PASS"


def test_kill_floor_check_stays_on_the_market_value():
    rep = pf.evaluate(mk(governor_bankroll=40.0, cash=9.0))
    c = one(rep, "bankroll_above_kill_floor")
    assert c["status"] == "FAIL" and "market-value" in c["text"] and "$9.00" in c["text"]


def test_unreadable_stored_reading_fails_the_sizing_checks_and_warns_with_both_figures():
    rep = pf.evaluate(mk(governor_bankroll=None, cash=31.0, failures={"governor_bankroll": "LookupError: none"}))
    assert one(rep, "cap_covers_sizer")["status"] == "FAIL"
    assert one(rep, "sizer_covers_exchange_minimum")["status"] == "FAIL"
    c = one(rep, "bankroll_readings_agree")
    assert c["status"] == "WARN" and "unknown" in c["text"] and "$31.00" in c["text"]


@pytest.mark.parametrize("stored,market,status", [
    (100.0, 95.0, "PASS"), (100.0, 94.9, "WARN"),
    (95.0, 100.0, "PASS"), (94.9, 100.0, "WARN"),
    (100.0, 100.0, "PASS"),
])
def test_readings_agree_boundary_is_five_percent_of_the_larger(stored, market, status):
    c = one(pf.evaluate(mk(governor_bankroll=stored, cash=market)), "bankroll_readings_agree")
    assert c["status"] == status
    if status == "WARN":
        assert f"${stored:,.2f}" in c["text"] and f"${market:,.2f}" in c["text"]


def test_consumer_sources_still_read_the_reading_this_script_says_they_read():
    gov = _squash((REPO / "execution" / "risk_governor.py").read_text())
    assert 'saved = live_db.get_state(conn)' in gov
    assert 'self._bankroll: float = float(saved.get("bankroll") or 0.0)' in gov
    assert '"SELECT * FROM live_portfolio_state ORDER BY id DESC LIMIT 1"' in _squash(
        (REPO / "execution" / "live_db.py").read_text())
    fast = _squash((REPO / "scripts" / "smart_wallet_fast_poll.py").read_text())
    assert '_ldb.execute("SELECT bankroll FROM live_portfolio_state ORDER BY id DESC LIMIT 1")' in fast
    assert "size_usd = _sw_live_size_usd(bankroll)" in fast
    sched = _squash((REPO / "services" / "scheduler.py").read_text())
    assert '"SELECT bankroll FROM live_portfolio_state ORDER BY id DESC LIMIT 1"' in sched
    assert [n for n, _ in pf.CONSUMER_READINGS] == ["GOVERNOR", "ROUTER", "SCHEDULER_START_CHECK"]


# --------------------------------------------------------------------------- A5 settings


@pytest.mark.parametrize("var,theirs", [
    ("POLYCLAWD_WEATHER_PER_TRADE_CAP", "50"), ("POLYCLAWD_TIER_SIZE_CAP", "25"),
    ("POLYCLAWD_PER_TRADE_FRAC", "0.5"), ("POLYCLAWD_KILL_FLOOR", "1"),
    ("POLYCLAWD_DAILY_LOSS_HALT", "999"), ("POLYCLAWD_MODE", "LIVE"),
])
def test_a_setting_that_differs_in_a_units_start_environment_fails_naming_both_values(var, theirs):
    rep = pf.evaluate(ok_mk(unit_vars={UNITS[0]: {}, UNITS[1]: {var: theirs}}))
    c = one(rep, "settings_match_services")
    assert c["status"] == "FAIL" and var in c["text"] and theirs in c["text"] and UNITS[1] in c["text"]
    assert f"in {UNITS[0]} is" not in c["text"]
    assert rep["exit"] != 0


def test_same_value_written_differently_is_not_a_difference_and_absent_is_not_a_failure():
    same = {"POLYCLAWD_WEATHER_PER_TRADE_CAP": "5.0", "POLYCLAWD_PER_TRADE_FRAC": "0.110", "POLYCLAWD_MODE": "paper"}
    c = one(pf.evaluate(ok_mk(unit_vars={UNITS[0]: same, UNITS[1]: {}})), "settings_match_services")
    assert c["status"] == "PASS"
    assert "not in the unit's start environment (loaded from the env file at start)" in c["text"]


def test_settings_check_fails_when_a_units_environment_could_not_be_read():
    c = one(pf.evaluate(ok_mk(unit_vars={UNITS[0]: None, UNITS[1]: {}})), "settings_match_services")
    assert c["status"] == "FAIL" and UNITS[0] in c["text"]


def test_env_file_newer_than_a_units_start_warns_naming_the_unit():
    rep = pf.evaluate(ok_mk(env_file={"path": "e.env", "mtime": 5_000.0},
                            unit_started={UNITS[0]: 9_000.0, UNITS[1]: 4_000.0}))
    c = one(rep, "env_file_older_than_services")
    assert c["status"] == "WARN" and UNITS[1] in c["text"] and UNITS[0] not in c["text"]
    assert "older settings" in c["text"]
    assert rep["exit"] == 0


def test_env_file_older_than_both_units_passes():
    assert one(pf.evaluate(ok_mk()), "env_file_older_than_services")["status"] == "PASS"


def test_unreadable_env_file_or_start_time_is_a_warn_never_a_pass():
    for over in (dict(env_file=None), dict(unit_started={UNITS[0]: None, UNITS[1]: 2_000.0})):
        c = one(pf.evaluate(ok_mk(**over)), "env_file_older_than_services")
        assert c["status"] == "WARN" and "unreadable" in c["text"], over


def test_number_lines_from_this_processs_environment_say_so():
    rep = pf.evaluate(ok_mk())
    for name in ("KILL_FLOOR", "DAILY_LOSS_HALT", "FIXED_PER_TRADE_CAP", "TIER_CAP", "FRACTION"):
        assert line(rep, name).endswith("(as this script sees it)"), name


def test_unit_vars_reader_returns_only_the_listed_names():
    env = (b"BOT_EOA_PRIVATE_KEY=0xSECRETSECRET\0POLYCLAWD_MODE=LIVE\0CLOB_API_SECRET=hush\0"
           b"POLYCLAWD_MIN_ORDER_SHARES=3\0POLYCLAWD_MODE_X=zzz\0NOEQUALS\0")
    got = pf.read_unit_vars("u", main_pid=lambda unit: 7, read_environ=lambda pid: env)
    assert got == {"POLYCLAWD_MODE": "LIVE", "POLYCLAWD_MIN_ORDER_SHARES": "3"}


@pytest.mark.parametrize("pid,env,exc", [(0, b"", RuntimeError), (77, None, PermissionError)])
def test_unit_vars_reader_failures_raise(pid, env, exc):
    def read_environ(p):
        raise PermissionError("denied")

    with pytest.raises(exc):
        pf.read_unit_vars("u", main_pid=lambda unit: pid, read_environ=read_environ)


def test_unit_vars_reader_absent_variables_are_an_empty_dict_not_a_failure():
    assert pf.read_unit_vars("u", main_pid=lambda unit: 7, read_environ=lambda pid: b"A=1\0") == {}


def test_no_secret_from_a_units_environment_reaches_the_output():
    secret = "0xSECRETVALUE1234567890"
    env = (f"BOT_EOA_PRIVATE_KEY={secret}\0CLOB_API_SECRET={secret}\0POLYCLAWD_WEATHER_PER_TRADE_CAP=50\0"
           f"POLYCLAWD_MIN_ORDER_SHARES=0\0").encode()
    reader = lambda unit: pf.read_unit_vars(unit, main_pid=lambda u: 7, read_environ=lambda pid: env)
    rc, out = run_main(["--token", "T1"], unit_vars=reader)
    assert rc == 1
    assert "settings_match_services" in out and "min_order_override" in out
    assert secret not in out
    assert "BOT_EOA_PRIVATE_KEY" not in out and "CLOB_API_SECRET" not in out


def test_unit_active_since_parses_systemctl_output_and_refuses_what_it_cannot_read():
    class R:
        def __init__(self, out, rc=0):
            self.stdout, self.returncode, self.stderr = out, rc, ""

    got = pf.unit_active_since("u", run=lambda *a, **k: R("Thu 2026-11-12 02:16:03 UTC\n"))
    assert got == pytest.approx(1_794_449_763.0)
    for bad in (R(""), R("Thu 2026-11-12 02:16:03 CET\n"), R("n/a\n"), R("x", rc=1)):
        with pytest.raises(Exception):
            pf.unit_active_since("u", run=lambda *a, **k: bad)


def test_setting_names_match_live_config_and_the_env_file_path_variable():
    src = (REPO / "execution" / "live_config.py").read_text()
    for name in pf.SETTING_VARS:
        assert f'"{name}"' in src, name
    assert "POLYCLAWD_ENV_FILE" in src and "_ENV_FILE" in src
    assert 'os.environ.get("POLYCLAWD_MIN_ORDER_SHARES"' in (REPO / "execution" / "clob_client.py").read_text()


# --------------------------------------------------------------------------- A6 URI


def test_database_path_with_a_space_hash_question_mark_and_percent_opens_the_right_file(tmp_path):
    d = tmp_path / "my dir #1 ?x %41"
    d.mkdir()
    db = d / "live ?#%.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (a)")
    con.execute("INSERT INTO t VALUES (7)")
    con.commit()
    con.close()
    ro = pf.open_readonly(db)
    assert ro.execute("SELECT a FROM t").fetchone()[0] == 7
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO t VALUES (8)")
    ro.close()


# --------------------------------------------------------------------------- fix-7 P1: the governor's kill floor
# on the figure the governor reads


def test_stored_bankroll_under_the_kill_floor_fails_even_when_the_market_value_is_fine():
    rep = pf.evaluate(mk(governor_bankroll=9.99, cash=40.0))
    c = one(rep, "stored_bankroll_above_kill_floor")
    assert c["status"] == "FAIL" and "$9.99" in c["text"] and "$10.00" in c["text"]
    assert one(rep, "bankroll_above_kill_floor")["status"] == "PASS"       # the market value is untouched
    assert rep["exit"] == 1 and "stored_bankroll_above_kill_floor" in rep["failing"]


def test_stored_bankroll_exactly_at_the_kill_floor_follows_the_governors_comparison():
    # RiskGovernor.check kills when `self._bankroll < kill_floor()`: equal is not a kill.
    c = one(pf.evaluate(mk(governor_bankroll=10.0, cash=40.0)), "stored_bankroll_above_kill_floor")
    assert c["status"] == "PASS" and "equal" in c["text"]
    assert one(pf.evaluate(mk(governor_bankroll=10.01, cash=40.0)),
               "stored_bankroll_above_kill_floor")["status"] == "PASS"
    assert one(pf.evaluate(mk(governor_bankroll=9.999, cash=40.0)),
               "stored_bankroll_above_kill_floor")["status"] == "FAIL"


def test_unreadable_stored_bankroll_fails_the_stored_kill_floor_check():
    rep = pf.evaluate(mk(governor_bankroll=None, cash=40.0, failures={"governor_bankroll": "LookupError: none"}))
    c = one(rep, "stored_bankroll_above_kill_floor")
    assert c["status"] == "FAIL" and "unknown" in c["text"]


def test_unknown_kill_floor_fails_the_stored_kill_floor_check():
    rep = pf.evaluate(mk(kill_floor=None))
    assert one(rep, "stored_bankroll_above_kill_floor")["status"] == "FAIL"


def test_the_two_kill_floor_checks_read_their_own_figure():
    stored_ok = pf.evaluate(mk(governor_bankroll=40.0, cash=9.0))
    assert one(stored_ok, "stored_bankroll_above_kill_floor")["status"] == "PASS"
    assert one(stored_ok, "bankroll_above_kill_floor")["status"] == "FAIL"
    market_ok = pf.evaluate(mk(governor_bankroll=9.0, cash=40.0))
    assert one(market_ok, "stored_bankroll_above_kill_floor")["status"] == "FAIL"
    assert one(market_ok, "bankroll_above_kill_floor")["status"] == "PASS"


# --------------------------------------------------------------------------- fix-7 P2: how old the stored figure is


def test_a_row_59_minutes_old_is_fresh_and_61_warns_with_the_age_and_the_time():
    fresh = pf.evaluate(ok_mk(stored_row=row_aged(59)))
    assert one(fresh, "stored_bankroll_fresh")["status"] == "PASS"
    assert line(fresh, "STORED_BANKROLL_AGE") == "0h 59m (row written 2026-11-12 11:01:00 UTC)"
    stale = pf.evaluate(ok_mk(stored_row=row_aged(61)))
    c = one(stale, "stored_bankroll_fresh")
    assert c["status"] == "WARN"
    assert "1h 1m" in c["text"] and "2026-11-12 10:59:00 UTC" in c["text"]
    assert "may not reflect the account now" in c["text"] and "PAPER" in c["text"]
    assert line(stale, "STORED_BANKROLL_AGE") == "1h 1m (row written 2026-11-12 10:59:00 UTC)"
    assert stale["exit"] == 0 and stale["failing"] == fresh["failing"]       # a WARN never changes the exit


def test_a_row_exactly_at_the_limit_is_fresh():
    assert one(pf.evaluate(mk(stored_row=row_aged(60))), "stored_bankroll_fresh")["status"] == "PASS"


@pytest.mark.parametrize("value", ["not a time", "", None, 12345, "2026-13-45 99:00:00"])
def test_a_timestamp_that_cannot_be_parsed_warns_and_prints_unknown(value):
    rep = pf.evaluate(ok_mk(stored_row={"has_column": True, "value": value}))
    c = one(rep, "stored_bankroll_fresh")
    assert c["status"] == "WARN" and "may not reflect the account now" in c["text"]
    assert line(rep, "STORED_BANKROLL_AGE").startswith("unknown")
    assert rep["exit"] == 0


def test_a_table_with_no_timestamp_column_prints_that_and_warns_and_invents_nothing():
    rep = pf.evaluate(ok_mk(stored_row={"has_column": False, "value": None}))
    assert line(rep, "STORED_BANKROLL_AGE") == "unknown (no timestamp column)"
    assert one(rep, "stored_bankroll_fresh")["status"] == "WARN" and rep["exit"] == 0


def test_a_row_that_could_not_be_read_warns_it_is_not_a_pass():
    rep = pf.evaluate(mk(stored_row=None, failures={"stored_row": "OperationalError: locked"}))
    assert one(rep, "stored_bankroll_fresh")["status"] == "WARN"
    assert line(rep, "STORED_BANKROLL_AGE").startswith("unknown")


def test_a_row_written_in_the_future_is_not_a_fresh_row():
    rep = pf.evaluate(mk(stored_row=row_aged(-30)))
    c = one(rep, "stored_bankroll_fresh")
    assert c["status"] == "WARN" and "future" in c["text"]


def test_the_limit_comes_from_the_input_and_defaults_to_sixty():
    assert one(pf.evaluate(mk(stored_row=row_aged(61), max_bankroll_age_minutes=120)),
               "stored_bankroll_fresh")["status"] == "PASS"
    assert one(pf.evaluate(mk(stored_row=row_aged(31), max_bankroll_age_minutes=30)),
               "stored_bankroll_fresh")["status"] == "WARN"
    assert pf.DEFAULT_MAX_BANKROLL_AGE_MINUTES == 60


@pytest.fixture
def tz_los_angeles(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize("written", ["2026-11-12T10:59:00+00:00", "2026-11-12T10:59:00Z", "2026-11-12 10:59:00",
                                     "2026-11-12T10:59:00"])
def test_the_time_is_read_as_utc_whatever_the_process_time_zone_is(tz_los_angeles, written):
    assert datetime.now().astimezone().utcoffset() != timedelta(0)         # the zone really is not UTC
    rep = pf.evaluate(mk(stored_row={"has_column": True, "value": written}))
    assert line(rep, "STORED_BANKROLL_AGE") == "1h 1m (row written 2026-11-12 10:59:00 UTC)"
    assert one(rep, "stored_bankroll_fresh")["status"] == "WARN"


def test_a_time_with_an_offset_is_converted_to_utc():
    rep = pf.evaluate(mk(stored_row={"has_column": True, "value": "2026-11-12T05:59:00-05:00"}))
    assert line(rep, "STORED_BANKROLL_AGE") == "1h 1m (row written 2026-11-12 10:59:00 UTC)"


def test_main_flag_changes_the_limit_and_the_age_line_is_printed():
    rc, out = run_main(["--token", "T1"], stored_row=lambda: row_aged(90))
    assert rc == 0 and re.search(r"^STORED_BANKROLL_AGE: 1h 30m \(row written 2026-11-12 10:30:00 UTC\)$", out, re.M)
    assert re.search(r"^WARN stored_bankroll_fresh: ", out, re.M)
    rc, out = run_main(["--token", "T1", "--max-bankroll-age-minutes", "120"], stored_row=lambda: row_aged(90))
    assert re.search(r"^PASS stored_bankroll_fresh: ", out, re.M)
    rc, out = run_main(["--token", "T1", "--max-bankroll-age-minutes", "30"], stored_row=lambda: row_aged(40))
    assert re.search(r"^WARN stored_bankroll_fresh: ", out, re.M)
    assert re.search(r"^(PASS|FAIL) stored_bankroll_above_kill_floor: ", out, re.M)


@pytest.mark.parametrize("bad", ["0", "-5", "nan", "inf", "abc"])
def test_main_refuses_a_limit_that_is_not_a_positive_finite_number(bad):
    with pytest.raises(SystemExit) as exc:
        pf.main(["--max-bankroll-age-minutes", bad], sources=make_sources(), out=io.StringIO())
    assert exc.value.code == 2


def test_a_source_that_fails_for_the_row_is_recorded_and_warns_in_main():
    def boom():
        raise sqlite3.OperationalError("database is locked")

    rc, out = run_main(["--token", "T1"], stored_row=boom)
    assert rc == 0
    assert re.search(r"^FAILED_SOURCE\[stored_row\]: OperationalError", out, re.M)
    assert re.search(r"^WARN stored_bankroll_fresh: ", out, re.M)


def test_the_row_reader_returns_the_timestamp_of_the_newest_row_read_only(tmp_path, monkeypatch):
    db = tmp_path / "live.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE live_portfolio_state (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, bankroll REAL);
        INSERT INTO live_portfolio_state (ts, bankroll) VALUES ('2026-11-12T08:00:00+00:00', 20.0),
                                                                ('2026-11-12T09:30:00+00:00', 16.97);
    """)
    con.commit()
    con.close()
    monkeypatch.setenv("POLYCLAWD_LIVE_DB", str(db))
    assert pf.read_stored_row() == {"has_column": True, "value": "2026-11-12T09:30:00+00:00"}


def test_the_row_reader_says_when_there_is_no_timestamp_column_and_fails_on_an_empty_table(tmp_path, monkeypatch):
    db = tmp_path / "live.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE live_portfolio_state (id INTEGER PRIMARY KEY AUTOINCREMENT, bankroll REAL)")
    con.execute("INSERT INTO live_portfolio_state (bankroll) VALUES (5.0)")
    con.commit()
    con.close()
    monkeypatch.setenv("POLYCLAWD_LIVE_DB", str(db))
    assert pf.read_stored_row() == {"has_column": False, "value": None}
    empty = tmp_path / "empty.db"
    con = sqlite3.connect(empty)
    con.execute("CREATE TABLE live_portfolio_state (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, bankroll REAL)")
    con.commit()
    con.close()
    monkeypatch.setenv("POLYCLAWD_LIVE_DB", str(empty))
    with pytest.raises(LookupError):
        pf.read_stored_row()


def test_the_row_reader_does_not_create_a_missing_database(tmp_path, monkeypatch):
    missing = tmp_path / "sub" / "nope.db"
    monkeypatch.setenv("POLYCLAWD_LIVE_DB", str(missing))
    with pytest.raises(Exception):
        pf.read_stored_row()
    assert not missing.exists() and not missing.parent.exists()


def test_the_governor_still_kills_below_the_floor_and_the_table_still_has_a_ts_column():
    gov = _squash((REPO / "execution" / "risk_governor.py").read_text())
    assert "if self._governor_state == \"KILL\" or self._bankroll < live_config.kill_floor():" in gov
    ddl = _squash((REPO / "execution" / "live_db.py").read_text())
    assert ("CREATE TABLE IF NOT EXISTS live_portfolio_state ( id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts TEXT, bankroll REAL,") in ddl
    assert "ts=datetime.now(timezone.utc).isoformat()," in gov            # written as an aware ISO time


def test_the_position_sync_that_writes_the_row_still_runs_every_five_minutes():
    sched = (REPO / "services" / "scheduler.py").read_text()
    block = sched.split('"5min": [', 1)[1].split("],", 1)[0]
    assert '"position_sync"' in block
    assert pf.DEFAULT_MAX_BANKROLL_AGE_MINUTES >= 2 * 5
