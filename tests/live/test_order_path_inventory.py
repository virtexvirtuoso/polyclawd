"""Task 1.10 tripwires: the order path and the readers of the mode stay inventoried.

The arming rule covers Polymarket orders sent through execution/clob_client.py. Two
lists below are the 2026-11-11 inventory. Each test parses every tracked .py file
outside tests/ with `ast` (comments and strings do not count) and fails when the set
of files it finds differs from its list, so a new reader of the mode or a new order
sender fails the suite until someone looks at it.

When a test fails: review the new file (does it send orders or read the mode in a way
the arming rule must cover?), then add it to the list here and to task 1.10's "Scope
of the guard" bullet in the plan.
"""

import ast
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Files `ast` cannot parse today: none. A file listed here is skipped by BOTH tests
# (with its reason as a comment); a parse failure anywhere else fails the test.
KNOWN_UNPARSEABLE: dict = {}

# --- Test 1: who reads the mode or the arming state --------------------------------
MODE_READERS = {
    "api/routes/live.py": "display: reports mode and armed in /live/portfolio and /live/governor",
    "execution/clob_client.py": "order gate: assert_live_armed() in post_maker and cross_taker",
    "execution/soccer_executor.py": "order gate: returns unless live_armed()",
    "execution/weather_executor.py": "order gate: returns unless live_armed()",
    "scripts/position_sync.py": "passed to RiskGovernor (the governor ignores the mode); no order gate",
    "scripts/smart_wallet_fast_poll.py": "order gate: _route_live_smart_wallet returns unless live_armed()",
    "services/live_stop_shadow.py": "live_armed() and mode(): tick passes dry_run unless armed; mode in the log line; no order gate of its own",
    "services/scheduler.py": "page: _log_live_unarmed() and _start_live_unarmed_page() at start",
    "services/stop_evaluator.py": "live_armed(): live-position lookup gate and the stop evaluator's arming check (evaluate_live_stops); mode(): passed to RiskGovernor",
    "signals/paper_portfolio.py": "order gate: weather signals go live only when live_armed()",
}
MODE_FUNCS = {"mode", "live_armed", "arming_state", "arming_state_text"}

# --- Test 2: who sends an order or moves money -------------------------------------
ORDER_SENDERS = {
    "execution/clob_client.py": "Polymarket orders; post_maker and cross_taker call assert_live_armed() first",
    "clob_client.py": "dead copy of execution/clob_client.py: nothing imports it",
    "scripts/position_sync.py": "redeem_positions collects winnings and opens nothing: intentionally not gated",
    "scripts/kalshi_order_maker.py": "Kalshi orders, hand-run with --execute; nothing schedules it",
    "api/routes/trading.py": "Simmer trade route, behind POLYCLAWD_SIMMER_TRADE_ENABLED (off, not set)",
}
SENDER_METHODS = {"place_limit_order", "place_market_order", "redeem_positions"}
KALSHI_ORDERS_PATH = "/trade-api/v2/portfolio/events/orders"
KALSHI_POST_FUNC = "kalshi_post"
SIMMER_TRADE_PATH = "/trade"


# --- detectors (pure functions of source text) ---------------------------------------


def _called_name(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _is_live_config_module(name):
    """`live_config` itself (a bare import) or any dotted path ending in `.live_config`."""
    return bool(name) and (name == "live_config" or name.endswith(".live_config"))


def _live_config_names(tree):
    """Names bound to the live_config module, and names bound to its functions."""
    modules, funcs = {"live_config"}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "live_config":
                    modules.add(a.asname or a.name)
                elif _is_live_config_module(node.module) and a.name in MODE_FUNCS:
                    funcs[a.asname or a.name] = a.name
        elif isinstance(node, ast.Import):
            for a in node.names:
                if _is_live_config_module(a.name) and a.asname:
                    modules.add(a.asname)
    return modules, funcs


def mode_reads(source):
    """Names of the mode/arming functions that live_config (or an import from it) is
    asked for: any load of the attribute or the imported name, so `f = live_config.mode`
    counts as well as a call."""
    tree = ast.parse(source)
    modules, funcs = _live_config_names(tree)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr in MODE_FUNCS:
            obj = node.value
            if (isinstance(obj, ast.Name) and obj.id in modules) or (
                isinstance(obj, ast.Attribute) and obj.attr == "live_config"
            ):
                found.add(node.attr)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in funcs:
            found.add(funcs[node.id])
    return found


def order_sends(source):
    """What in the source sends an order or moves money, as a set of labels.

    A sender method counts when it is ACCESSED (`client.place_limit_order`, called or
    not: `f = client.place_limit_order; f(...)` is caught), when its bare name is
    loaded, or when it is named in `getattr(obj, "place_limit_order")`. A function or
    method DEFINITION with that name does not count."""
    tree = ast.parse(source)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr in SENDER_METHODS:
            found.add(node.attr)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in SENDER_METHODS:
            found.add(node.id)
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        if name == "getattr" and len(node.args) >= 2:
            attr = node.args[1]
            if isinstance(attr, ast.Constant) and attr.value in SENDER_METHODS:
                found.add(attr.value)
        if name == KALSHI_POST_FUNC:
            found.add("kalshi_post")
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                    and arg.value.startswith(KALSHI_ORDERS_PATH):
                found.add("kalshi_orders_path")
        # the Simmer trade call: _simmer_request("/trade", method="POST", ...)
        method_post = any(
            k.arg == "method" and isinstance(k.value, ast.Constant) and str(k.value.value).upper() == "POST"
            for k in node.keywords
        )
        first = node.args[0] if node.args else None
        if method_post and isinstance(first, ast.Constant) and first.value == SIMMER_TRADE_PATH:
            found.add("simmer_trade_post")
    return found


# --- scanning -------------------------------------------------------------------------


def tracked_python_files_outside_tests():
    try:
        out = subprocess.run(
            ["git", "ls-files", "*.py"], cwd=REPO, capture_output=True, text=True, check=True, timeout=60
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:  # git missing or not a repo: fail, never skip
        pytest.fail(f"cannot list tracked files with `git ls-files '*.py'` in {REPO}: {exc}")
    files = [line for line in out.splitlines() if line]
    assert files, "git ls-files returned no .py files"
    return sorted(f for f in files if not f.startswith("tests/"))


def scan(detector):
    """{file: labels} for tracked .py files outside tests/; fails naming any unparseable
    file that is not in KNOWN_UNPARSEABLE."""
    hits, unparseable = {}, []
    for rel in tracked_python_files_outside_tests():
        if rel in KNOWN_UNPARSEABLE:
            continue
        try:
            found = detector((REPO / rel).read_text(encoding="utf-8"))
        except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
            unparseable.append(f"{rel} ({type(exc).__name__}: {exc})")
            continue
        if found:
            hits[rel] = found
    assert not unparseable, (
        "these tracked files cannot be parsed, so the tripwire cannot see inside them: "
        + "; ".join(unparseable)
        + ". Fix them, or list them in KNOWN_UNPARSEABLE with a reason."
    )
    return hits


def _diff_message(what, found, listed):
    new, gone = sorted(found - listed), sorted(listed - found)
    parts = []
    if new:
        parts.append(
            f"files not in the list that {what}: {new}. Review each new file (does the arming "
            "rule have to cover it?), then add it to the list in this test and to task 1.10's "
            '"Scope of the guard" bullet in the plan.'
        )
    if gone:
        parts.append(
            f"listed files that no longer {what}: {gone}. Remove them from the list in this "
            'test and from the plan\'s "Scope of the guard" bullet.'
        )
    return " ".join(parts)


# --- the tripwires --------------------------------------------------------------------


def test_files_reading_the_mode_or_arming_state_are_inventoried():
    found = set(scan(mode_reads))
    listed = set(MODE_READERS)
    assert found == listed, _diff_message("call live_config.mode()/live_armed()/arming_state()", found, listed)


def test_files_sending_orders_or_moving_money_are_inventoried():
    found = set(scan(order_sends))
    listed = set(ORDER_SENDERS)
    assert found == listed, _diff_message("send an order or move money", found, listed)


# --- the detectors themselves (positive and negative controls) -------------------------


@pytest.mark.parametrize(
    "src,expected",
    [
        ("from execution import live_config\nlive_config.mode()", {"mode"}),
        ("from execution import live_config\nx = live_config.live_armed()", {"live_armed"}),
        ("from execution import live_config as lc\nlc.mode()", {"mode"}),
        ("import execution.live_config as lc\nlc.live_armed()", {"live_armed"}),
        ("from execution.live_config import mode, live_armed\nmode(); live_armed()", {"mode", "live_armed"}),
        ("from execution.live_config import mode as m\nm()", {"mode"}),
        ("import execution\nexecution.live_config.mode()", {"mode"}),
        ("from execution import live_config\nlive_config.arming_state()", {"arming_state"}),
        ("from execution import live_config\nlive_config.arming_state_text()", {"arming_state_text"}),
        # a bare module import counts as the live-config module
        ("import live_config as lc\nlc.mode()", {"mode"}),
        ("import live_config\nlive_config.live_armed()", {"live_armed"}),
        ("from live_config import mode\nmode()", {"mode"}),
        ("from live_config import arming_state as st\nst()", {"arming_state"}),
        # an attribute ACCESS is a read, called or not
        ("from execution import live_config\nf = live_config.mode\nf()", {"mode"}),
        ("from execution import live_config as lc\nfn = lc.live_armed", {"live_armed"}),
        ("from live_config import mode\nf = mode", {"mode"}),
        # negatives: comments, strings, docstrings, other objects, other functions, no call
        ("# live_config.mode()\nx = 1", set()),
        ('s = "live_config.mode()"', set()),
        ('def f():\n    """calls live_config.live_armed()"""', set()),
        ("other.mode()", set()),
        ("from execution import live_config\nlive_config.per_trade_cap()", set()),
        ("import live_config as lc", set()),
        ("from live_config import mode", set()),
        ("from live_config import per_trade_cap\nper_trade_cap()", set()),
        ("import live_config\nlive_config.per_trade_cap", set()),
        ("from execution import live_config\nlive_config.mode = 1", set()),
        ("def mode(): pass\nmode()", set()),
    ],
)
def test_mode_detector_controls(src, expected):
    assert mode_reads(src) == expected


@pytest.mark.parametrize(
    "src,expected",
    [
        ("c.place_limit_order(token_id=1)", {"place_limit_order"}),
        ("place_market_order(1)", {"place_market_order"}),
        ("handle = client.redeem_positions(condition_id=m)", {"redeem_positions"}),
        # an access without a call
        ("f = client.place_limit_order\nf(token_id=1)", {"place_limit_order"}),
        ("fns = [c.place_market_order, c.redeem_positions]", {"place_market_order", "redeem_positions"}),
        # getattr with a string constant naming a sender method
        ('getattr(client, "place_limit_order")(1)', {"place_limit_order"}),
        ('m = getattr(self._c, "redeem_positions", None)', {"redeem_positions"}),
        ('f = place_market_order', {"place_market_order"}),
        ('kalshi_post("/x", {})', {"kalshi_post"}),
        ('r = post("/trade-api/v2/portfolio/events/orders", o)', {"kalshi_orders_path"}),
        ('_simmer_request("/trade", method="POST", data=d)', {"simmer_trade_post"}),
        # negatives
        ("# c.place_limit_order()", set()),
        ('s = "place_limit_order"', set()),
        ("def place_limit_order(self): pass", set()),
        ("class C:\n    def place_market_order(self): pass\n    async def redeem_positions(self): pass", set()),
        ("client.place_limit_order = fake", set()),
        ('getattr(client, "place_limit_orders")', set()),
        ("getattr(client, name)", set()),
        ('getattr(client, "get_order")', set()),
        ('x = "/trade-api/v2/portfolio/events/orders"', set()),
        ('_simmer_request("/trades?limit=5")', set()),
        ('_simmer_request("/trade", method="GET")', set()),
        ('kalshi_get("/trade-api/v2/portfolio/orders?status=resting")', set()),
    ],
)
def test_order_detector_controls(src, expected):
    assert order_sends(src) == expected


def test_unparseable_file_fails_naming_it(monkeypatch, tmp_path):
    """A file `ast` cannot read must fail the scan, not be skipped."""
    import sys

    mod = sys.modules[__name__]
    (tmp_path / "broken.py").write_text("def f(:\n")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(mod, "tracked_python_files_outside_tests", lambda: ["broken.py"])
    with pytest.raises(AssertionError, match=r"broken\.py"):
        mod.scan(mod.mode_reads)


def test_inventory_lists_have_a_reason_for_every_entry():
    for table in (MODE_READERS, ORDER_SENDERS):
        assert all(isinstance(v, str) and len(v) > 10 for v in table.values())
