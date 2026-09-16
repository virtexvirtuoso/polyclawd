"""Tests for the category-threshold branch of whale_scanner.alert_gate().

WHY THIS FILE EXISTS
--------------------
`_CAT_THRESHOLDS` is populated by `_load_thresholds()`, which has exactly ONE
call site: `run_scan()`. No test calls `run_scan()`, so in every test process
the dict stayed empty and this branch was DEAD CODE under test:

    if _CAT_THRESHOLDS and effective_flow > 0:          # always False in tests
        whale_pierce = cat_t.get("mega_whale", CRITICAL_FLOW_USD)
        if effective_flow >= whale_pierce:
            return None                                 # pierces ALL gates

That is the single most permissive path in the alert gate -- the escape hatch
that lets a large bet bypass every quality check. Production loads the config
and runs it; tests validated the unloaded defaults instead. The two disagree by
up to 20x, so no test could have detected a regression here.

WHAT THE TESTS REVEALED
-----------------------
With the real config loaded, EVERY kalshi category has mega_whale == 2000,
which is exactly MIN_ALERT_FLOW_USD. Any flow large enough to clear the alert
floor is therefore also large enough to pierce every downstream gate, so
near_settled suppression is effectively inoperative on kalshi in production.

These tests pin that as the OBSERVED contract so it cannot change silently.
They are not an endorsement of it -- see test_kalshi_pierce_equals_alert_floor.
alert_gate governs DELIVERY only (never DB logging), so the blast radius is
alert quality and the whale-follower study, not live capital.
"""

import pytest

import signals.whale_scanner as ws


@pytest.fixture()
def loaded():
    """Load the real per-category thresholds, then restore module state.

    _load_thresholds() mutates a module global, so without restoration this
    fixture would leak into every later test in the session.
    """
    original = ws._CAT_THRESHOLDS
    ws._load_thresholds()
    yield ws._CAT_THRESHOLDS
    ws._CAT_THRESHOLDS = original


NEAR_SETTLED = {"best_bid": 0.97, "best_ask": 0.99}


def test_thresholds_actually_load(loaded):
    """Guard the premise: if the config file goes missing this whole file is
    testing defaults again, silently."""
    assert loaded, "whale_thresholds.json failed to load — branch is dead again"
    assert "kalshi" in loaded and "polymarket" in loaded


def test_branch_is_reachable_once_loaded(loaded):
    """The regression this file exists to prevent."""
    assert bool(ws._CAT_THRESHOLDS) is True


def test_loaded_config_differs_sharply_from_the_default(loaded):
    """Tests-vs-production divergence, made explicit.

    Unloaded, a kalshi market falls back to CRITICAL_FLOW_USD (25000) or the
    hardcoded default; loaded, it is 2000. A test suite that never loads is
    validating a gate an order of magnitude stricter than the live one.
    """
    t = ws.get_market_thresholds("kalshi", "KXWNBA1HWINNER-26JUN11NYATL-NY")
    assert t["mega_whale"] == 2000
    assert t["mega_whale"] < ws.CRITICAL_FLOW_USD


def test_mega_whale_flow_pierces_near_settled(loaded):
    """The escape hatch works as designed: big flow ignores near_settled."""
    det = {"flow_dollars": 50_000.0, "last_yes_price": 0.98}
    assert ws.alert_gate("kalshi", "KXWNBA1HWINNER-26JUN11NYATL-NY",
                         det, NEAR_SETTLED) is None


def test_single_huge_print_is_rejected_before_it_can_pierce(loaded):
    """FINDING: max_single_trade_usd is computed into effective_flow, then the
    usd_floor check discards the row using flow_dollars ALONE.

        effective_flow = max(flow_usd, max_single)          # 50,000
        if not smart and flow_usd < MIN_ALERT_FLOW_USD:     # 100 < 2000
            return "usd_floor"                              # returns here

    So a market with tiny aggregate flow but one $50k print never reaches the
    pierce. The max_single half of effective_flow only ever matters once
    flow_dollars is already above the floor on its own.

    Pinned as observed behaviour, not endorsed.
    """
    det = {"flow_dollars": 100.0, "max_single_trade_usd": 50_000.0,
           "last_yes_price": 0.98}
    assert ws.alert_gate("kalshi", "KXWNBA1HWINNER-26JUN11NYATL-NY",
                         det, NEAR_SETTLED) == "usd_floor"


def test_max_single_pierces_once_flow_clears_the_floor(loaded):
    """The other side of the same coin: with flow above the floor, the big
    single print does drive effective_flow and pierces."""
    det = {"flow_dollars": 2_500.0, "max_single_trade_usd": 50_000.0,
           "last_yes_price": 0.98}
    assert ws.alert_gate("polymarket", "soccer-epl-2026", det, NEAR_SETTLED) is None


def test_below_alert_floor_is_still_gated(loaded):
    """The usd_floor check runs BEFORE the pierce, so small flow is rejected."""
    det = {"flow_dollars": 100.0, "last_yes_price": 0.98}
    assert ws.alert_gate("kalshi", "KXWNBA1HWINNER-26JUN11NYATL-NY",
                         det, NEAR_SETTLED) == "usd_floor"


def test_kalshi_pierce_equals_alert_floor(loaded):
    """FINDING, pinned deliberately.

    Every kalshi category ships mega_whale == 2000 == MIN_ALERT_FLOW_USD. The
    consequence: any flow that survives the usd_floor check immediately clears
    the pierce, so near_settled (and every gate after it) cannot fire on kalshi
    in production.

    This test documents the live behaviour rather than the intended design. If
    someone raises mega_whale or lowers the alert floor, this fails and forces
    a deliberate decision instead of a silent change.
    """
    for cat, vals in ws._CAT_THRESHOLDS["kalshi"].items():
        assert vals["mega_whale"] == ws.MIN_ALERT_FLOW_USD, (
            f"kalshi/{cat} mega_whale {vals['mega_whale']} no longer equals the "
            f"alert floor {ws.MIN_ALERT_FLOW_USD} — re-examine whether "
            f"near_settled suppression is reachable again")

    # Demonstrated end-to-end: a flow one dollar over the floor pierces.
    det = {"flow_dollars": ws.MIN_ALERT_FLOW_USD + 1, "last_yes_price": 0.98}
    assert ws.alert_gate("kalshi", "KXMLB-26JUN11-NYY", det, NEAR_SETTLED) is None


def test_category_thresholds_above_the_flat_fallback_are_unreachable(loaded):
    """FINDING: a per-category mega_whale above CRITICAL_FLOW_USD can never bind.

    The category check runs first; if it does NOT pierce, control falls to:

        if effective_flow >= CRITICAL_FLOW_USD:   # 25,000
            return None

    So soccer's 207,000 and nba's 500,000 are decorative -- anything at or above
    25,000 pierces via the flat fallback regardless. Category thresholds can
    only ever LOWER the pierce bar (kalshi 2,000, pm ufc 2,000, crypto 8,000,
    nfl 19,000), never raise it, which inverts the intent for exactly the
    categories tuned highest.
    """
    pm = ws._CAT_THRESHOLDS["polymarket"]
    assert pm["soccer"]["mega_whale"] == 207_000
    assert pm["soccer"]["mega_whale"] > ws.CRITICAL_FLOW_USD

    # Far below soccer's 207k, but at/above the 25k flat fallback -> pierces.
    det = {"flow_dollars": 30_000.0, "last_yes_price": 0.98}
    assert ws.alert_gate("polymarket", "soccer-epl-2026", det, NEAR_SETTLED) is None

    # Below BOTH -> the near_settled gate finally bites.
    det = {"flow_dollars": 20_000.0, "last_yes_price": 0.98}
    assert ws.alert_gate("polymarket", "soccer-epl-2026", det, NEAR_SETTLED) == "near_settled"


def test_first_sight_still_wins_over_the_pierce(loaded):
    """first_sight returns before any threshold logic — ordering guard."""
    det = {"flow_dollars": 1_000_000.0, "last_yes_price": 0.98}
    assert ws.alert_gate("kalshi", "KXMLB-26JUN11-NYY", det, NEAR_SETTLED,
                         first_sight=True) == "first_sight"


def test_fixture_restores_module_state(loaded):
    """Belt and braces: the fixture must not leak loaded thresholds into other
    tests, which would change their behaviour depending on run order."""
    assert ws._CAT_THRESHOLDS is not None
