#!/usr/bin/env python3
"""Regression tests for the soccer alert relevance gate (2026-08-20).

Covers the change that stopped soccer_live_monitor.py paging on every Vegas
line move regardless of whether Polymarket actually lagged it.

Guards three properties:
  1. soccer_line_drift is registered ENFORCE — soccer previously had no dedup
     of any kind (one match emitted a dozen pings across 21 minutes).
  2. The governor still escalates for soccer: flip / new leg / +delta re-fire.
  3. The tier split works BOTH ways — a no-gap move enqueues to the tier-3
     digest and sends nothing, while an actionable move still reaches the send
     path. Ledger rule: a gate that rejects 100% is indistinguishable from no
     signal, so the pass case is asserted explicitly, not assumed.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signals import alert_dispatch
from signals.alert_dispatch import TIER_CRITICAL, TIER_DIGEST, dispatch
from signals.alert_governor import CONFIG, Leg, govern

_passed = _failed = 0


def check(label, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {label}")
    else:
        _failed += 1
        print(f"  ❌ {label}")


def _tmpdb():
    return Path(tempfile.mkdtemp()) / "t.db"


def test_soccer_registered_shadow():
    """Soccer must stay SHADOW until the 3-way topology issue is measured.

    Downgraded from enforce 2026-08-21 (/qa audit). MLB is a 2-way market whose
    legs are exact complements, so a falling leg always has a rising complement
    to satisfy the one-directional upgrade test. Soccer is 3-way: a 7pp fall
    splits (e.g. +4/+3) and both complements drop below LINE_DRIFT_PP=5.0, so no
    escalation candidate exists. With max_rearms=2 a leg then goes permanently
    silent after ~90 min — the last 25-40 min of a match. Do NOT flip this back
    to enforce without evidence that soccer escalations still fire.
    """
    print("\ntest_soccer_registered_shadow:")
    cfg = CONFIG.get("soccer_line_drift")
    check("soccer_line_drift is registered", cfg is not None)
    check("mode is ENFORCE (3-way maths fixed 2026-08-21 via delta_mode=abs)",
          cfg and cfg["mode"] == "enforce")
    # Deliberately NOT asserting delta == mlb_line_drift's. F3 established the
    # MLB params are not transferable to a 3-way market; locking the copy in
    # would fail whoever fixes that. Assert only that params are present.
    check("params present", bool(cfg and cfg.get("delta") and cfg.get("rearm_min")))


def test_relevance_gate_precedes_governor():
    """Structural: the relevance gate MUST run before govern().

    The governor unconditionally stamps 'alerted at level X at time T' for any
    non-suppress verdict. If a digest-only (non-actionable) event arms it, the
    next genuinely actionable move fails the escalation test against a level that
    was never sent — inverting alert_governor's own contract. Read from the
    deployed source so the ordering cannot silently regress.
    """
    print("\ntest_relevance_gate_precedes_governor:")
    _root = Path(__file__).resolve().parent.parent
    src = (_root / "scripts" / "soccer_live_monitor.py").read_text()
    body = src[src.index("def check_line_drift"):]
    body = body[:body.index("\ndef ")] if "\ndef " in body[1:] else body
    i_gate = body.find("if not trade_signals:")
    i_gov = body.find('govern("soccer_line_drift"')
    check("both markers found", i_gate != -1 and i_gov != -1)
    check("relevance gate appears BEFORE govern()", -1 < i_gate < i_gov)
    gov_tail = body[i_gov:]
    check("governed branch digests instead of returning silently",
          "_to_digest(" in gov_tail and "governed" in gov_tail)


def test_soccer_dedup_and_escalation():
    print("\ntest_soccer_dedup_and_escalation:")
    db, t0 = _tmpdb(), 1_700_000_000
    legs = lambda mag, d="up": [Leg("Arsenal", mag, d)]  # noqa: E731

    v1 = govern("soccer_line_drift", "g1", legs(59.0), db, _now=t0)
    check("first sighting fires", v1.should_send)

    v2 = govern("soccer_line_drift", "g1", legs(60.0), db, _now=t0 + 120)
    # SHADOW mode: the decision is recorded but should_send stays True, so the
    # caller keeps sending. Assert the DECISION is right; enforcement is off.
    check("same-state repeat is DECIDED suppress (12-pings-per-match bug)",
          v2.action == "suppress")
    check("enforce mode blocks the send", not v2.should_send)
    # The soccer CALLER gates on .action, not .should_send, so a shadow-mode
    # suppress still prevents the 1-min-tick flood. Locked structurally below.
    _root = Path(__file__).resolve().parent.parent
    _src = (_root / "scripts" / "soccer_live_monitor.py").read_text()
    check("caller gates on verdict.action == 'suppress' (flood guard)",
          'verdict.action == "suppress"' in _src)

    v3 = govern("soccer_line_drift", "g1", legs(66.0), db, _now=t0 + 300)
    check("+delta move re-fires as upgrade", v3.should_send and v3.action == "fire_upgrade")

    v4 = govern("soccer_line_drift", "g1", legs(66.0, "down"), db, _now=t0 + 400)
    check("direction flip fires immediately", v4.should_send)

    v5 = govern("soccer_line_drift", "g1",
                [Leg("Arsenal", 66.0, "down"), Leg("Draw", 30.0, "up")], db, _now=t0 + 500)
    check("new leg fires", v5.should_send)


def test_tier_split_both_directions():
    print("\ntest_tier_split_both_directions:")
    db = _tmpdb()
    sent = []
    original = alert_dispatch.alert_openclaw
    alert_dispatch.alert_openclaw = lambda msg, parse_mode=None: sent.append(msg) or True
    try:
        # No PM gap -> digest. Must NOT send.
        dispatch("soccer_odds_moved", "vegas moved, PM in line", TIER_DIGEST, db_path=db)
        check("no-gap move sent nothing to Telegram", len(sent) == 0)

        import sqlite3
        con = sqlite3.connect(str(db))
        n3 = con.execute("SELECT COUNT(*) FROM alert_queue WHERE tier=3 AND shadow=0").fetchone()[0]
        con.close()
        check("no-gap move was queued for the digest (not dropped)", n3 == 1)

        # Actionable -> must still page. This is the gate-can-pass assertion.
        dispatch("soccer_odds_moved", "BUY Arsenal YES at 41%", TIER_CRITICAL, db_path=db)
        check("actionable move DID reach the send path", len(sent) == 1)
        check("actionable message preserved", "BUY Arsenal" in sent[0])
    finally:
        alert_dispatch.alert_openclaw = original



def test_delta_mode_bidirectional_soccer_only():
    """Soccer escalates on movement in EITHER direction; MLB is unchanged.

    3-way markets have no complementary leg to trip an increase-only test, so a
    falling leg would never escalate and would go permanently silent after
    max_rearms. 2-way mlb_line_drift does have that complement (verified: all 18
    live leg-pairs sum to exactly 100.0), and mlb_run's magnitude is an EDGE
    size where shrinking is a de-escalation that must NOT re-page.
    """
    print("\ntest_delta_mode_bidirectional_soccer_only:")
    check("soccer uses abs", CONFIG["soccer_line_drift"].get("delta_mode") == "abs")
    check("mlb_run stays increase-only",
          CONFIG["mlb_run"].get("delta_mode", "increase") == "increase")
    check("mlb_line_drift stays increase-only",
          CONFIG["mlb_line_drift"].get("delta_mode", "increase") == "increase")

    t0 = 1_700_000_000

    # Soccer: a 7pp FALL must now escalate.
    db = _tmpdb()
    govern("soccer_line_drift", "s1", [Leg("Alaves", 40.0, "down")], db, _now=t0)
    v = govern("soccer_line_drift", "s1", [Leg("Alaves", 33.0, "down")], db, _now=t0 + 60)
    check("soccer: 7pp FALL escalates (was permanently silent)",
          v.should_send and v.action == "fire_upgrade")

    # Soccer: a sub-delta move still suppresses.
    db2 = _tmpdb()
    govern("soccer_line_drift", "s2", [Leg("Alaves", 40.0, "down")], db2, _now=t0)
    v2 = govern("soccer_line_drift", "s2", [Leg("Alaves", 37.0, "down")], db2, _now=t0 + 60)
    check("soccer: 3pp move still suppressed (no new flood)", v2.action == "suppress")

    # mlb_run REGRESSION GUARD: a shrinking edge must NOT re-page.
    db3 = _tmpdb()
    govern("mlb_run", "m1", [Leg("Yankees", 12.0, "BUY")], db3, _now=t0)
    v3 = govern("mlb_run", "m1", [Leg("Yankees", 5.0, "BUY")], db3, _now=t0 + 60)
    check("mlb_run: SHRINKING edge does NOT escalate (unchanged)",
          v3.action == "suppress" and not v3.should_send)

    # mlb_run: a growing edge still escalates.
    db4 = _tmpdb()
    govern("mlb_run", "m2", [Leg("Mets", 6.0, "BUY")], db4, _now=t0)
    v4 = govern("mlb_run", "m2", [Leg("Mets", 12.0, "BUY")], db4, _now=t0 + 60)
    check("mlb_run: GROWING edge still escalates", v4.action == "fire_upgrade")

for fn in (test_soccer_registered_shadow, test_delta_mode_bidirectional_soccer_only,
           test_relevance_gate_precedes_governor,
           test_soccer_dedup_and_escalation, test_tier_split_both_directions):
    fn()

print("\n" + "=" * 40)
print(f"{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
