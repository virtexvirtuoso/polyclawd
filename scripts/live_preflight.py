#!/usr/bin/env python3
"""Live pre-flight (plan task 1.12): the numbers a return to real-money trading depends on.

Read-only. Prints `NAME: value` lines, then a CHECKS section, then a RESULT line, and exits
non-zero if any check is a FAIL. It writes nothing (the live database is opened read-only and
never created), sends no alert, and makes no network call except the public order-book and
/book reads and one authenticated balance read.

Run it as `linuxuser` (the user both services run as): the allowlist is read from
/proc/<MainPID>/environ of polyclawd-scheduler and polyclawd-api, which only the owner of a
process can read.

    cd /var/www/virtuosocrypto.com/polyclawd && venv/bin/python3 -m scripts.live_preflight --token <token_id>

Three layers: gather() does all I/O through injectable sources, evaluate() is pure, main()
parses arguments and prints. Two formulas are copied from code that cannot be called without
side effects; tests/live/test_live_preflight.py fails if either source expression changes.
"""

from __future__ import annotations

import argparse
import ast
import math
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

UNITS = ("polyclawd-scheduler", "polyclawd-api")
ALLOWLIST_VAR = "POLYCLAWD_LIVE_STRATEGY_ALLOWLIST"
EXPECTED_ALLOWLIST = {"smart_wallet"}
TIER_CAP_VAR = "POLYCLAWD_TIER_SIZE_CAP"
TIER_CAP_DEFAULT = "25.0"
EXPECTED_UNIVERSAL_STOP = 0.40
SEARCH_MAX_CENTS = 100_000  # $1,000.00
EPS = 1e-9


# --------------------------------------------------------------------------- copied formulas


def governor_cap(bankroll, fixed_cap, tier_cap, frac):
    """The governor's per-trade cap (execution/risk_governor.py, Rule 3):
        flat_cap = max(tier_cap, live_config.per_trade_cap())   # fixed cap alone if the tier cap cannot be read
        cap = min(flat_cap, self._bankroll * live_config.per_trade_frac())
    The governor refuses an order only when it is strictly over this cap."""
    flat_cap = fixed_cap if tier_cap is None else max(tier_cap, fixed_cap)
    return min(flat_cap, bankroll * frac)


def scheduler_cap(bankroll, fixed_cap, frac):
    """The scheduler start-up check's cap (services/scheduler.py, _assert_live_cap_fits_min_action):
        eff = min(_lc.per_trade_cap(), bankroll * _lc.per_trade_frac())   # no tier cap"""
    return min(fixed_cap, bankroll * frac)


def _ge(a, b):
    return a >= b - EPS


# --------------------------------------------------------------------------- readers


def _short_error(exc):
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def open_readonly(path):
    """Open the SQLite file read-only. A missing file raises; nothing is created."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"live database not found: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _db_path():
    from execution import live_db

    return live_db.resolve_db_path()


def read_open_positions():
    con = open_readonly(_db_path())
    try:
        rows = con.execute(
            "SELECT token_id, shares FROM live_positions WHERE status = 'open' ORDER BY id"
        ).fetchall()
    finally:
        con.close()
    return [{"token_id": r["token_id"] or "", "shares": float(r["shares"] or 0.0)} for r in rows]


def read_governor_bankroll():
    con = open_readonly(_db_path())
    try:
        row = con.execute("SELECT bankroll FROM live_portfolio_state ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        con.close()
    if row is None or row["bankroll"] is None:
        raise LookupError("live_portfolio_state has no bankroll row")
    return float(row["bankroll"])


def systemctl_main_pid(unit, run=subprocess.run):
    res = run(["systemctl", "show", "-p", "MainPID", "--value", unit],
              capture_output=True, text=True, timeout=10)
    if res.returncode != 0:
        raise RuntimeError(f"systemctl show {unit} failed rc={res.returncode}: {(res.stderr or '')[:100]}")
    try:
        pid = int(res.stdout.strip())
    except ValueError:
        raise RuntimeError(f"systemctl show {unit}: MainPID is not a number: {res.stdout.strip()[:50]!r}")
    return pid


def _read_proc_environ(pid):
    return Path(f"/proc/{pid}/environ").read_bytes()


def read_unit_allowlist(unit, main_pid=systemctl_main_pid, read_environ=_read_proc_environ):
    """The allowlist the RUNNING unit has: `POLYCLAWD_LIVE_STRATEGY_ALLOWLIST` from its process
    environment. Any failure raises (unit not running, pid 0, unreadable, variable absent):
    unreadable is not the same as absent, and neither is an empty list."""
    pid = main_pid(unit)
    if not pid:
        raise RuntimeError(f"{unit} has MainPID 0 (not running)")
    env = read_environ(pid)
    prefix = (ALLOWLIST_VAR + "=").encode()
    for item in env.split(b"\0"):
        if item.startswith(prefix):
            raw = item[len(prefix):].decode("utf-8", "replace")
            return {s.strip() for s in raw.split(",") if s.strip()}
    raise LookupError(f"{ALLOWLIST_VAR} is not in the environment of {unit} (pid {pid})")


def read_price_ceiling():
    """PRICE_GATE_MAX from scripts/smart_wallet_alert.py, read from the source with `ast`.

    The module is NOT imported: its import pulls in config.data_api, signals.alert_dispatch and
    more. The router's price band reads this same module constant (smart_wallet_alert._price_band_gate_suppress),
    and a literal assignment is all this reads, so a changed expression fails loudly here."""
    path = REPO / "scripts" / "smart_wallet_alert.py"
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PRICE_GATE_MAX"
                                                 for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float)):
                return float(node.value.value)
            raise ValueError("PRICE_GATE_MAX is no longer a numeric literal")
    raise LookupError("PRICE_GATE_MAX not found in scripts/smart_wallet_alert.py")


def default_sizers():
    from scripts.smart_wallet_fast_poll import _sw_live_size_usd

    return {"smart_wallet": _sw_live_size_usd}


def _best_bid(book):
    """Highest usable bid price in a book (max over levels, whatever the order), else None."""
    if book is None:
        return None
    best = None
    for lvl in getattr(book, "bids", None) or []:
        price = lvl.get("price") if isinstance(lvl, dict) else getattr(lvl, "price", None)
        try:
            p = float(price)
        except (TypeError, ValueError):
            continue
        if math.isfinite(p) and 0.0 < p <= 1.0 and (best is None or p > best):
            best = p
    return best


def _default_min_order(token):
    from execution import clob_client

    value = clob_client.get_min_order_size(token, strict=True)
    override = os.environ.get("POLYCLAWD_MIN_ORDER_SHARES", "").strip()
    try:
        float(override)
        source = "POLYCLAWD_MIN_ORDER_SHARES"
    except ValueError:
        source = "exchange"
    return value, source


def default_sources():
    from execution import live_config

    def caps():
        try:
            tier = live_config._parse_float(TIER_CAP_VAR, TIER_CAP_DEFAULT)
        except Exception:
            tier = None  # the governor falls back to the fixed cap alone
        return {"fixed": live_config.per_trade_cap(), "tier": tier, "frac": live_config.per_trade_frac()}

    def cash():
        from execution import clob_client

        return clob_client.get_balance()

    def orderbook(token):
        from odds.polymarket_clob import get_orderbook

        return get_orderbook(token)

    def stop_bounds():
        from services.stop_evaluator import UNIVERSAL_MAX_LOSS_PCT

        return UNIVERSAL_MAX_LOSS_PCT, live_config.SPREAD_ALLOW_PCT

    return dict(
        arming=live_config.arming_state,  # display only
        caps=caps,
        kill_floor=live_config.kill_floor,
        daily_halt=live_config.daily_loss_halt,
        governor_bankroll=read_governor_bankroll,
        cash=cash,
        positions=read_open_positions,
        orderbook=orderbook,
        min_order=_default_min_order,
        price_ceiling=read_price_ceiling,
        allowlist=read_unit_allowlist,
        stop_bounds=stop_bounds,
        sizers=None,  # filled lazily by gather
    )


# --------------------------------------------------------------------------- layer 1: gather


def gather(cli_tokens, sources=None):
    """All I/O. Every source is a function in `sources` (production defaults fill the gaps).
    A source that fails is recorded under its name with a short error; its value is None."""
    src = default_sources() if sources is None else dict(sources)
    if sources is not None:
        # fill any gaps with production defaults only when the caller left them out
        for k, v in default_sources().items():
            src.setdefault(k, v)
    failures = {}

    def call(name, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - every failure is recorded, none guessed
            failures[name] = _short_error(exc)
            return None

    inp = {"failures": failures}
    inp["arming"] = call("arming", src["arming"])
    inp["caps"] = call("caps", src["caps"])
    inp["kill_floor"] = call("kill_floor", src["kill_floor"])
    inp["daily_halt"] = call("daily_halt", src["daily_halt"])
    inp["governor_bankroll"] = call("governor_bankroll", src["governor_bankroll"])
    inp["cash"] = call("cash", src["cash"])
    inp["price_ceiling"] = call("price_ceiling", src["price_ceiling"])
    sb = call("stop_bounds", src["stop_bounds"])
    inp["stop"] = {"universal": sb[0], "spread_allow": sb[1]} if sb else None

    sizers = src.get("sizers")
    if sizers is None:
        sizers = call("sizers", default_sizers) or {}
    inp["sizers"] = sizers

    raw_positions = call("positions", src["positions"])
    if raw_positions is None:
        inp["positions"] = None
    else:
        positions = []
        for p in raw_positions:
            tok = p.get("token_id") or ""
            bid = None
            if tok:
                book = call(f"orderbook[{tok}]", src["orderbook"], tok)
                bid = _best_bid(book)
            positions.append({"token_id": tok, "shares": float(p.get("shares") or 0.0), "bid": bid})
        inp["positions"] = positions

    tokens = []
    for t in [p["token_id"] for p in (inp["positions"] or [])] + list(cli_tokens or []):
        if t and t not in tokens:
            tokens.append(t)
    inp["tokens"] = tokens
    inp["min_reads"], inp["min_failures"] = {}, {}
    for t in tokens:
        try:
            value, source = src["min_order"](t)
            inp["min_reads"][t] = {"value": float(value), "source": source}
        except Exception as exc:  # noqa: BLE001
            inp["min_failures"][t] = _short_error(exc)
            failures[f"min_order[{t}]"] = inp["min_failures"][t]

    inp["allowlists"] = {u: call(f"allowlist[{u}]", src["allowlist"], u) for u in UNITS}
    return inp


# --------------------------------------------------------------------------- layer 2: evaluate


def _money(x):
    return "unknown" if x is None else f"${x:,.2f}"


def _strategies(inp):
    readable = [a for a in inp["allowlists"].values() if a is not None]
    if not readable:
        return ["smart_wallet"], True
    names = sorted(set().union(*readable))
    return names, False


def _market_value(inp):
    if inp["cash"] is None or inp["positions"] is None:
        return None
    return inp["cash"] + sum((p["bid"] or 0.0) * p["shares"] for p in inp["positions"])


def _exchange_minimum(inp):
    reads = inp["min_reads"]
    return max(r["value"] for r in reads.values()) if reads else None


def _sizing_passes(bankroll, strategies, caps, sizers, minimum_shares, ceiling, kill_floor):
    """The one pass test: used for the checks and for the search for the smallest bankroll."""
    if caps is None or minimum_shares is None or ceiling is None or kill_floor is None:
        return False
    if not bankroll > kill_floor:
        return False
    minimum_usd = minimum_shares * ceiling
    cap = governor_cap(bankroll, caps["fixed"], caps["tier"], caps["frac"])
    for s in strategies:
        fn = sizers.get(s)
        if fn is None:
            return False
        size = fn(bankroll)
        if not (_ge(cap, size) and _ge(size, minimum_usd)):
            return False
    return True


def evaluate(inp):
    caps, floor, halt = inp["caps"], inp["kill_floor"], inp["daily_halt"]
    ceiling, sizers = inp["price_ceiling"], inp["sizers"]
    strategies, assumed = _strategies(inp)
    bankroll = _market_value(inp)
    minimum = _exchange_minimum(inp)
    min_usd = None if minimum is None or ceiling is None else minimum * ceiling

    lines, checks = [], []

    def add(status, name, text):
        checks.append({"status": status, "name": name, "text": text})

    arming = inp["arming"]
    if arming:
        lines += [("MODE", arming["mode"]), ("ENFORCE", str(arming["enforce"])),
                  ("LIVE_ORDER_PATH_READY", str(arming["path_ready"])), ("ARMED", str(arming["armed"]))]
    else:
        lines += [(n, "unknown") for n in ("MODE", "ENFORCE", "LIVE_ORDER_PATH_READY", "ARMED")]
    lines += [("KILL_FLOOR", _money(floor)), ("DAILY_LOSS_HALT", _money(halt))]
    if caps:
        lines += [("FIXED_PER_TRADE_CAP", _money(caps["fixed"])),
                  ("TIER_CAP", _money(caps["tier"]) if caps["tier"] is not None else "unreadable (governor uses the fixed cap)"),
                  ("FRACTION", f"{caps['frac']:.4f}")]
    else:
        lines += [("FIXED_PER_TRADE_CAP", "unknown"), ("TIER_CAP", "unknown"), ("FRACTION", "unknown")]

    gov_eff = sch_eff = frac_cap = None
    if caps and bankroll is not None:
        frac_cap = bankroll * caps["frac"]
        gov_eff = governor_cap(bankroll, caps["fixed"], caps["tier"], caps["frac"])
        sch_eff = scheduler_cap(bankroll, caps["fixed"], caps["frac"])
    lines += [
        ("FRACTIONAL_CAP", _money(frac_cap)),
        ("EFFECTIVE_CAP_GOVERNOR", f"{_money(gov_eff)}  = min(max(tier cap, fixed cap), bankroll x fraction)"),
        ("EFFECTIVE_CAP_SCHEDULER_CHECK", f"{_money(sch_eff)}  = min(fixed cap, bankroll x fraction)"),
    ]
    stop = inp["stop"]
    spread_allow = stop["spread_allow"] if stop else None
    lines.append(("SPREAD_ALLOW_PCT", "unknown" if spread_allow is None else f"{spread_allow:.2f}"))

    lines.append(("BANKROLL_GOVERNOR_READING", _money(inp["governor_bankroll"])))
    lines.append(("EXCHANGE_CASH", _money(inp["cash"])))
    if inp["positions"] is None:
        lines += [("OPEN_POSITIONS", "unknown"), ("UNPRICED_POSITIONS", "unknown")]
    else:
        pos = inp["positions"]
        lines.append(("OPEN_POSITIONS", str(len(pos))))
        unpriced = [p["token_id"] or "(no token)" for p in pos if p["bid"] is None]
        lines.append(("UNPRICED_POSITIONS", ", ".join(unpriced) + " (valued at 0)" if unpriced else "none"))
    lines.append(("BANKROLL_MARKET_VALUE",
                  _money(bankroll) + ("  = cash + sum(best bid x shares)" if bankroll is not None else "")))

    # sizers
    for s in strategies:
        fn = sizers.get(s)
        if fn is None:
            lines.append((f"SIZER[{s}]", "no live sizer known"))
        elif bankroll is None:
            lines.append((f"SIZER[{s}]", "unknown (bankroll unknown)"))
        else:
            lines.append((f"SIZER[{s}]", _money(fn(bankroll))))
    if assumed:
        lines.append(("SIZER_NOTE", "no allowlist could be read: sizing shown for smart_wallet only"))

    # exchange minimum
    if minimum is None:
        lines.append(("EXCHANGE_MINIMUM_SHARES", "unknown (none read)"))
    else:
        lines.append(("EXCHANGE_MINIMUM_SHARES", f"{minimum:g}  (largest of {len(inp['min_reads'])} read)"))
    for t in inp["tokens"]:
        if t in inp["min_reads"]:
            r = inp["min_reads"][t]
            where = r["source"] if r["source"] != "exchange" else "the exchange"
            lines.append((f"EXCHANGE_MINIMUM_SOURCE[{t}]", f"{r['value']:g} shares from {where}"))
        else:
            lines.append((f"EXCHANGE_MINIMUM_SOURCE[{t}]", f"NOT READ: {inp['min_failures'].get(t, 'no error recorded')}"))
    lines.append(("PRICE_CEILING", "unknown" if ceiling is None else f"{ceiling:.2f}"))
    lines.append(("MIN_ORDER_USD", _money(min_usd) + ("  = minimum shares x price ceiling" if min_usd is not None else "")))

    for u in UNITS:
        a = inp["allowlists"][u]
        lines.append((f"ALLOWLIST[{u}]", "UNREADABLE" if a is None else ",".join(sorted(a)) or "(empty)"))

    # smallest passing bankroll
    smallest = None
    if caps is not None and minimum is not None and ceiling is not None and floor is not None:
        for cents in range(0, SEARCH_MAX_CENTS + 1):
            if _sizing_passes(cents / 100.0, strategies, caps, sizers, minimum, ceiling, floor):
                smallest = cents / 100.0
                break
        lines.append(("SMALLEST_PASSING_BANKROLL",
                      f"${smallest:,.2f}  (every sizing check passes from here)" if smallest is not None
                      else "none up to $1,000"))
    else:
        lines.append(("SMALLEST_PASSING_BANKROLL", "unknown (an input is missing)"))

    for name, err in sorted(inp["failures"].items()):
        lines.append((f"FAILED_SOURCE[{name}]", err))

    # ---- checks
    for s in strategies:
        fn = sizers.get(s)
        if fn is None or bankroll is None or caps is None:
            why = "no live sizer known" if fn is None else "bankroll or caps unknown"
            add("FAIL", f"cap_covers_sizer[{s}]", why)
            add("FAIL", f"sizer_covers_exchange_minimum[{s}]", why)
            continue
        size = fn(bankroll)
        ok = _ge(gov_eff, size)
        add("PASS" if ok else "FAIL", f"cap_covers_sizer[{s}]",
            f"effective cap {_money(gov_eff)} {'>=' if ok else '<'} sizer {_money(size)} at bankroll {_money(bankroll)}")
        if min_usd is None:
            add("FAIL", f"sizer_covers_exchange_minimum[{s}]",
                "exchange minimum or price ceiling unknown, so the sizer cannot be shown to cover it")
        else:
            ok = _ge(size, min_usd)
            add("PASS" if ok else "FAIL", f"sizer_covers_exchange_minimum[{s}]",
                f"sizer {_money(size)} {'>=' if ok else '<'} minimum order {_money(min_usd)} "
                f"({minimum:g} shares x {ceiling:.2f})")

    if bankroll is None or floor is None:
        add("FAIL", "bankroll_above_kill_floor", f"unknown: bankroll {_money(bankroll)}, kill floor {_money(floor)}")
    else:
        ok = bankroll > floor
        add("PASS" if ok else "FAIL", "bankroll_above_kill_floor",
            f"market-value bankroll {_money(bankroll)} {'>' if ok else 'is not >'} kill floor {_money(floor)}")

    if inp["min_reads"]:
        extra = ""
        if inp["min_failures"]:
            extra = "; not read: " + ", ".join(f"{t} ({e})" for t, e in inp["min_failures"].items())
        add("PASS", "exchange_minimum_read",
            f"read {len(inp['min_reads'])} of {len(inp['tokens'])} sampled tokens; largest {minimum:g} shares{extra}")
    elif not inp["tokens"]:
        add("FAIL", "exchange_minimum_read",
            "no sample: no open live position and no --token given, so no minimum was read from the exchange")
    else:
        add("FAIL", "exchange_minimum_read",
            "every read failed: " + ", ".join(f"{t} ({e})" for t, e in inp["min_failures"].items()))

    al = inp["allowlists"]
    unreadable = [u for u in UNITS if al[u] is None]
    if unreadable:
        add("FAIL", "allowlist_readable", "environment not read for " + ", ".join(unreadable)
            + " (unreadable is not absent; run as linuxuser)")
        add("FAIL", "allowlists_agree", "cannot compare: " + ", ".join(unreadable) + " unreadable")
        add("FAIL", "allowlist_is_smart_wallet_only", "unknown: " + ", ".join(unreadable) + " unreadable")
    else:
        add("PASS", "allowlist_readable", "environment of both units read")
        same = al[UNITS[0]] == al[UNITS[1]]
        add("PASS" if same else "FAIL", "allowlists_agree",
            "both " + (",".join(sorted(al[UNITS[0]])) or "(empty)") if same else
            "; ".join(f"{u}={','.join(sorted(al[u])) or '(empty)'}" for u in UNITS))
        bad = [u for u in UNITS if al[u] != EXPECTED_ALLOWLIST]
        add("PASS" if not bad else "FAIL", "allowlist_is_smart_wallet_only",
            "each is exactly smart_wallet" if not bad else "not exactly smart_wallet: " + ", ".join(
                f"{u}={','.join(sorted(al[u])) or '(empty)'}" for u in bad))

    if gov_eff is None:
        add("WARN", "cap_formulas_agree", "unknown (caps or bankroll missing)")
    elif abs(gov_eff - sch_eff) <= EPS:
        add("PASS", "cap_formulas_agree", f"both {_money(gov_eff)}")
    else:
        add("WARN", "cap_formulas_agree",
            f"governor {_money(gov_eff)} = min(max(tier, fixed), bankroll x frac) differs from the scheduler "
            f"start check {_money(sch_eff)} = min(fixed, bankroll x frac); the start check is the stricter or looser of the two")

    if bankroll is None or halt is None or floor is None:
        add("WARN", "daily_halt_reachable", "unknown (bankroll, halt or floor missing)")
    else:
        room = bankroll - floor
        if halt >= room:
            add("WARN", "daily_halt_reachable",
                f"daily-loss halt {_money(halt)} >= bankroll {_money(bankroll)} - kill floor {_money(floor)} "
                f"= {_money(room)}: the kill floor is hit first")
        else:
            add("PASS", "daily_halt_reachable",
                f"daily-loss halt {_money(halt)} < room above the kill floor {_money(room)}")

    if stop is None:
        add("WARN", "stop_bound", "unknown (stop constants not readable)")
    else:
        total = stop["universal"] + stop["spread_allow"]
        text = (f"UNIVERSAL_MAX_LOSS_PCT {stop['universal']:.2f} + SPREAD_ALLOW_PCT {stop['spread_allow']:.2f} "
                f"= {total:.2f}: the stop always fires by this loss at the best bid")
        if abs(stop["universal"] - EXPECTED_UNIVERSAL_STOP) > EPS:
            add("WARN", "stop_bound", text + f"; the plan's numbers assume {EXPECTED_UNIVERSAL_STOP:.2f}")
        else:
            add("INFO", "stop_bound", text)

    if arming:
        add("INFO", "arming",
            f"mode={arming['mode']} enforce={arming['enforce']} LIVE_ORDER_PATH_READY={arming['path_ready']} "
            f"armed={arming['armed']} as this process sees them; the running services are checked "
            f"separately through GET /api/live/governor")
    else:
        add("WARN", "arming", "arming state not readable")

    failing = [c["name"] for c in checks if c["status"] == "FAIL"]
    return {"lines": lines, "checks": checks, "bankroll": bankroll, "smallest_passing": smallest,
            "failing": failing, "exit": 1 if failing else 0}


# --------------------------------------------------------------------------- layer 3: main


def render(rep):
    out = [f"{n}: {v}" for n, v in rep["lines"]]
    out.append("")
    out.append("CHECKS")
    out += [f"{c['status']} {c['name']}: {c['text']}" for c in rep["checks"]]
    out.append("")
    if rep["failing"]:
        out.append(f"RESULT: FAIL ({len(rep['failing'])} failing: {', '.join(rep['failing'])})")
    else:
        out.append("RESULT: PASS")
    return "\n".join(out) + "\n"


def main(argv=None, *, sources=None, out=None):
    ap = argparse.ArgumentParser(description="Read-only live trading pre-flight (plan task 1.12).")
    ap.add_argument("--token", action="append", default=[], metavar="TOKEN_ID",
                    help="extra token to read the exchange minimum for (repeatable)")
    args = ap.parse_args(argv)
    out = out or sys.stdout
    rep = evaluate(gather(args.token, sources))
    out.write(render(rep))
    return rep["exit"]


if __name__ == "__main__":
    sys.exit(main())
