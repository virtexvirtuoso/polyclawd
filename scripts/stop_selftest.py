#!/usr/bin/env python3
"""stop_selftest.py — synthetic end-to-end stop-path test (Task 2.2, REDESIGNED).

Proves the stop path works for real: inserts a fake -46% open PAPER position,
runs services.stop_evaluator.evaluate_stops(), and asserts the UNIVERSAL STOP
closes it AND the 🛑 [SELFTEST] stop-close alert goes through the Telegram
sender (scripts.openclaw_alerts.alert_openclaw).

Safety design (critic-reviewed — do not "simplify"):
* market_id = 'selftest-<uuid4>' — can NEVER collide with a real market, so
  _get_live_position() finds nothing and _close_live_position_early (an
  ACTUAL live exit) is unreachable. Never reuse a real market_id here.
* _fetch_price is monkeypatched PREFIX-SCOPED: only rows whose market_id
  starts with 'selftest-' get the synthetic 0.27 price; every other row
  delegates to the real function (real positions get normal treatment).
* Touches the paper_positions table only. The finally block restores ALL
  paper-accounting side effects (_close_position_early mutates paper
  bankroll state and records a closed losing trade): the synthetic row is
  deleted, paper_portfolio_state is rolled back to its pre-run snapshot,
  and any REAL close that interleaved during the run is replayed so its
  P&L survives the rollback. The resolution logger is no-op'd for the run.
* --local additionally monkeypatches alert_openclaw (no real send) and
  suppresses Discord — for CI/dev machines. On the VPS, run WITHOUT
  --local so the 🛑 [SELFTEST] alert really lands in Telegram.

Usage:
  venv/bin/python3 scripts/stop_selftest.py            # VPS: real Telegram send
  venv/bin/python3 scripts/stop_selftest.py --local    # no network sends
  (optional --db PATH to target a non-default sqlite file, e.g. in tests)
"""

import argparse
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SELFTEST_TITLE = "[SELFTEST] fake position"
SELFTEST_ENTRY = 0.50
SELFTEST_PRICE = 0.27  # -46% on a YES from 0.50 — beyond the 40% universal stop
SELFTEST_BET = 10.0


def run_selftest(local: bool = False, db_path=None) -> dict:
    """Run the synthetic stop test. Returns {ok, alerts, notes}."""
    import services.stop_evaluator as se
    import scripts.openclaw_alerts as oa
    import signals.resolution_logger as resolution_logger
    import signals.paper_portfolio as pp

    report = {"ok": False, "alerts": [], "notes": []}

    market_id = f"selftest-{uuid.uuid4()}"
    # Hard invariant: a synthetic id can never route into a live exit.
    assert market_id.startswith("selftest-") and not market_id.startswith("0x")

    # ── originals to restore (module monkeypatches) ────────────────────────
    orig_db_path = se.DB_PATH
    orig_fetch = se._fetch_price
    orig_alert = oa.alert_openclaw
    orig_log_close = resolution_logger.log_position_close
    orig_discord = se._send_discord_alert

    if db_path is not None:
        se.DB_PATH = Path(db_path)

    def _conn():
        c = sqlite3.connect(str(se.DB_PATH))
        c.row_factory = sqlite3.Row
        return c

    # ── snapshot paper accounting BEFORE the run ───────────────────────────
    conn = _conn()
    state_snapshot_id = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM paper_portfolio_state"
    ).fetchone()[0]
    pre_open_ids = {r["id"] for r in conn.execute(
        "SELECT id FROM paper_positions WHERE status = 'open'")}
    run_start = datetime.now(timezone.utc).isoformat()

    conn.execute(
        "INSERT INTO paper_positions"
        " (opened_at, market_id, market_title, platform, side, entry_price,"
        "  bet_size, edge_pct, status, strategy, archetype)"
        " VALUES (?, ?, ?, 'polymarket', 'YES', ?, ?, 0.05, 'open', '', '')",
        (run_start, market_id, SELFTEST_TITLE, SELFTEST_ENTRY, SELFTEST_BET),
    )
    conn.commit()
    conn.close()

    sent = []  # (message, ok) pairs seen by the (possibly wrapped) sender
    try:
        # Prefix-scoped price patch: selftest rows get the synthetic price,
        # everything else delegates to the REAL fetch.
        def _selftest_fetch(pos):
            if str(pos.get("market_id") or "").startswith("selftest-"):
                return (pos["id"], SELFTEST_PRICE)
            return orig_fetch(pos)

        se._fetch_price = _selftest_fetch

        if local:
            def _sender(message, *a, **kw):
                sent.append((message, True))
                return True
            se._send_discord_alert = lambda info: None  # no network in --local
        else:
            def _sender(message, *a, **kw):
                ok = orig_alert(message, *a, **kw)
                sent.append((message, bool(ok)))
                return ok

        oa.alert_openclaw = _sender
        # Don't contaminate the calibration/resolution log with a fake trade.
        resolution_logger.log_position_close = lambda *a, **k: None

        stopped = se.evaluate_stops()

        mine = [s for s in stopped
                if (s.get("market_title") or "") == SELFTEST_TITLE]
        selftest_msgs = [(m, ok) for m, ok in sent if SELFTEST_TITLE in m]
        report["alerts"] = [m for m, _ in selftest_msgs]

        checks = {
            "synthetic position closed by UNIVERSAL STOP":
                bool(mine) and "UNIVERSAL STOP" in mine[0]["reason"],
            "stop-close 🛑 [SELFTEST] alert through telegram sender":
                bool(selftest_msgs)
                and any("🛑" in m for m, _ in selftest_msgs)
                and all(ok for _, ok in selftest_msgs),
        }
        for name, ok in checks.items():
            report["notes"].append(f"{'PASS' if ok else 'FAIL'}: {name}")
        report["ok"] = all(checks.values())

    finally:
        # ── restore module state ───────────────────────────────────────────
        se._fetch_price = orig_fetch
        oa.alert_openclaw = orig_alert
        resolution_logger.log_position_close = orig_log_close
        se._send_discord_alert = orig_discord

        # ── restore paper accounting ───────────────────────────────────────
        conn = _conn()
        try:
            # Real closes that interleaved during the run must SURVIVE the
            # state rollback — collect them before deleting anything.
            real_closes = [
                r for r in conn.execute(
                    "SELECT id, pnl FROM paper_positions"
                    " WHERE status != 'open' AND market_id != ?"
                    "   AND closed_at >= ? ORDER BY id",
                    (market_id, run_start),
                )
                if r["id"] in pre_open_ids
            ]

            conn.execute("DELETE FROM paper_positions WHERE market_id = ?",
                         (market_id,))
            conn.execute("DELETE FROM paper_portfolio_state WHERE id > ?",
                         (state_snapshot_id,))
            conn.commit()

            # Replay the legitimate closes on top of the snapshot.
            for r in real_closes:
                pnl = r["pnl"] or 0.0
                bankroll = pp._get_bankroll(conn) + pnl
                pp._save_state(conn, bankroll, pnl)
                report["notes"].append(
                    f"replayed real close position_id={r['id']} pnl={pnl:+.2f}"
                    " (resolution log was suppressed for this run —"
                    " reconcile manually if needed)")

            leftovers = conn.execute(
                "SELECT COUNT(*) FROM paper_positions"
                " WHERE market_id LIKE 'selftest-%'"
                "    OR market_title LIKE '[SELFTEST]%'"
            ).fetchone()[0]
            if leftovers:
                report["ok"] = False
                report["notes"].append(
                    f"FAIL: {leftovers} selftest row(s) left behind")
            else:
                report["notes"].append("PASS: selftest rows cleaned up")
        finally:
            conn.close()
            se.DB_PATH = orig_db_path

    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--local", action="store_true",
                    help="monkeypatch alert_openclaw — no real Telegram send")
    ap.add_argument("--db", default=None,
                    help="override sqlite path (tests only; default: "
                         "storage/shadow_trades.db)")
    args = ap.parse_args()

    report = run_selftest(local=args.local, db_path=args.db)
    for note in report["notes"]:
        print(note)
    for msg in report["alerts"]:
        print("--- alert sent ---")
        print(msg)
    print(f"RESULT: {'PASS' if report['ok'] else 'FAIL'}"
          f" ({'local' if args.local else 'REAL SEND'} mode)")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
