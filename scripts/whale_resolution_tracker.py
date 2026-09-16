#!/usr/bin/env python3
"""whale_resolution_tracker.py — Track whether the dominant whale won or lost.

Records every alert's dominant direction in a separate DB (no lock contention),
then checks Kalshi + Polymarket APIs at settlement to compare against actual.

Fixes applied 2026-06-12:
  - Also checks resolved=1 (was dead-end bug)
  - Polymarket Gamma API checking via slug (outcome + closed fields)
  - EVEN direction uses taker % tiebreaker
  - Limit 500, checks past-close regardless
"""

import sqlite3, json, urllib.request, time, sys
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone
from config.polymarket_urls import GAMMA_API, CLOB_API  # polyproxy: central URL config

SRC_DB = Path("/var/www/virtuosocrypto.com/polyclawd/storage/whale_scanner.db")
PRED_DB = Path("/var/www/virtuosocrypto.com/polyclawd/storage/whale_predictions.db")
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
PRED_TABLE = """
CREATE TABLE IF NOT EXISTS whale_predictions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market      TEXT NOT NULL,
    base_ticker TEXT NOT NULL,
    platform    TEXT NOT NULL,
    title       TEXT,
    close_time  TEXT,
    severity    TEXT,
    max_score   INTEGER,
    dominant_direction TEXT,
    dominant_amount    REAL,
    total_flow         REAL,
    taker_pct          REAL,
    resolved         INTEGER DEFAULT 0,
    actual_outcome   TEXT,
    whale_won        INTEGER,
    close_price      REAL,
    last_checked     TEXT,
    created          TEXT DEFAULT (datetime('now'))
)"""


def _fetch_json(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {"_error": "fetch failed"}


def kalshi_fetch(path):
    return _fetch_json(f"{KALSHI_API}{path}")


def gamma_fetch(path):
    return _fetch_json(f"{GAMMA_API}{path}")


def open_db(path):
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_or_create_table(conn):
    conn.execute(PRED_TABLE)
    conn.commit()


def normalize_close_time(ct_str):
    if not ct_str:
        return None
    try:
        if ct_str.endswith("Z"):
            return datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
        return datetime.fromisoformat(ct_str)
    except:
        return None


def extract_alerts():
    src = open_db(SRC_DB)
    rows = src.execute("""
        SELECT id, ts, market, severity, score, platform, payload, reasons
        FROM whale_alerts WHERE score >= 3
        ORDER BY ts ASC
    """).fetchall()
    src.close()
    groups = defaultdict(
        lambda: {
            "platforms": set(),
            "severities": [],
            "scores": [],
            "flow_yes": 0,
            "flow_no": 0,
            "flow_dollars": 0,
            "taker_count": 0,
            "total_trades": 0,
            "titles": set(),
            "close_time": "",
            "base_ticker": "",
        }
    )
    for r in rows:
        p = json.loads(r["payload"])
        ticker = r["market"]
        parts = ticker.split("-")
        base = "-".join(parts[:-1]) if len(parts) > 2 else ticker
        g = groups[ticker]
        g["platforms"].add(r["platform"])
        g["severities"].append(r["severity"])
        g["scores"].append(r["score"])
        g["flow_yes"] += p.get("flow_yes", 0) or 0
        g["flow_no"] += p.get("flow_no", 0) or 0
        g["flow_dollars"] += p.get("flow_dollars", 0) or 0
        rs = p.get("raw_score")
        if rs is not None:
            g["raw_score"] = max(g.get("raw_score", 0), rs)
        g["titles"].add((p.get("title", "") or "")[:200])
        # First alert's book snapshot = the price a follower could act on.
        # Without it whale_won is unpriced (60% WR at 65c is a LOSS) — QA 2026-07-08.
        if "alert_price" not in g:
            _mid = p.get("mid") or (
                (p.get("best_bid") and p.get("best_ask"))
                and (float(p["best_bid"]) + float(p["best_ask"])) / 2
            ) or p.get("current_price")
            if _mid and 0.0 < float(_mid) < 1.0:
                g["alert_price"] = float(_mid)
        g["close_time"] = p.get("close_time", "") or ""
        g["base_ticker"] = base
        reasons = r["reasons"] or ""
        if "taker_YES" in reasons:
            g["taker_count"] += 1
        g["total_trades"] += 1
    results = []
    for ticker, g in sorted(groups.items(), key=lambda x: -x[1]["flow_dollars"]):
        if g["flow_dollars"] < 100:
            continue
        taker_pct = (
            g["taker_count"] / g["total_trades"] * 100 if g["total_trades"] > 0 else 50
        )
        if g["flow_yes"] > g["flow_no"]:
            dom_dir, dom_amt = "YES", g["flow_yes"]
        elif g["flow_no"] > g["flow_yes"]:
            dom_dir, dom_amt = "NO", g["flow_no"]
        else:
            dom_dir = "YES" if taker_pct >= 50 else "NO"
            dom_amt = max(g["flow_yes"], g["flow_no"])
        max_sev = (
            "CRITICAL"
            if "CRITICAL" in g["severities"]
            else "HIGH"
            if "HIGH" in g["severities"]
            else "LOW"
        )
        results.append(
            {
                "market": ticker,
                "base_ticker": g["base_ticker"],
                "platform": next(iter(g["platforms"])),
                "title": list(g["titles"])[0] if g["titles"] else "",
                "close_time": g["close_time"],
                "severity": max_sev,
                "max_score": max(g["scores"]),
                "dominant_direction": dom_dir,
                "dominant_amount": dom_amt,
                "total_flow": g["flow_dollars"],
                "taker_pct": taker_pct,
                "alert_price": g.get("alert_price"),
            }
        )
    return results


def backfill_predictions(pred):
    try:
        pred.execute("ALTER TABLE whale_predictions ADD COLUMN alert_price REAL")
    except Exception:
        pass
    existing = {r[0] for r in pred.execute("SELECT market FROM whale_predictions")}
    alerts = extract_alerts()
    print(f"  Extracted {len(alerts)} alert groups from source")
    inserted = 0
    for a in alerts:
        if a["market"] in existing:
            continue
        pred.execute(
            """
            INSERT INTO whale_predictions
            (market, base_ticker, platform, title, close_time, severity, max_score,
             dominant_direction, dominant_amount, total_flow, taker_pct, alert_price,
             resolved, last_checked)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,datetime('now'))
        """,
            (
                a["market"],
                a["base_ticker"],
                a["platform"],
                a["title"],
                a["close_time"],
                a["severity"],
                a["max_score"],
                a["dominant_direction"],
                a["dominant_amount"],
                a["total_flow"],
                a["taker_pct"],
                a.get("alert_price"),
            ),
        )
        inserted += 1
    pred.commit()
    print(f"  Inserted {inserted} new predictions")


def check_polymarket_resolution(ticker):
    """Check Polymarket market outcome via Gamma API by slug."""
    data = gamma_fetch(f"/markets?slug={ticker}&limit=1")
    if isinstance(data, list) and data:
        m = data[0]
        closed = m.get("closed", False)
        outcome = m.get("outcome", "")
        if closed and outcome:
            return {"status": "closed", "outcome": outcome}
        return {"status": "open", "outcome": None}
    return {"status": "unknown", "outcome": None}


def check_resolutions(pred, limit=500):
    now = datetime.now(timezone.utc)
    unresolved = pred.execute(
        """
        SELECT id, market, base_ticker, platform, close_time,
               dominant_direction, dominant_amount, total_flow, title
        FROM whale_predictions
        WHERE platform IN ('kalshi', 'polymarket')
          AND (resolved = 0 OR (resolved = 1 AND last_checked IS NOT NULL))
          AND close_time != '' AND close_time IS NOT NULL
        ORDER BY total_flow DESC LIMIT ?
    """,
        (limit,),
    ).fetchall()
    total = len(unresolved)
    if total == 0:
        print("  No unresolved predictions to check")
        return 0, 0, 0, 0
    print(f"  Checking {total} predictions...")
    checked, resolved, still_open, errors = 0, 0, 0, 0
    for r in unresolved:
        ticker = r["market"]
        platform = r["platform"]
        ct = normalize_close_time(r["close_time"])
        if ct and ct > now:
            still_open += 1
            continue
        if platform == "kalshi":
            data = kalshi_fetch(f"/markets?tickers={ticker}")
            if data and not data.get("_error") and data.get("markets"):
                m = data["markets"][0]
                status = m.get("status", "")
                if status == "finalized":
                    result_str = m.get("result", "")
                    if result_str in ("yes", "no"):
                        actual = "YES" if result_str == "yes" else "NO"
                        whale_dir = r["dominant_direction"]
                        won = (
                            None
                            if whale_dir == "EVEN"
                            else (1 if actual == whale_dir else 0)
                        )
                        pred.execute(
                            "UPDATE whale_predictions SET resolved=2, actual_outcome=?, whale_won=?, last_checked=datetime('now') WHERE id=?",
                            (actual, won, r["id"]),
                        )
                        resolved += 1
                        icon = "🟢" if won == 1 else "🔴" if won == 0 else "❓"
                        print(
                            f"  {icon} ${r['total_flow']:>8,.0f} | BET {whale_dir}->{actual} | {'WON' if won == 1 else 'LOST' if won == 0 else '?'} | kalshi | {(r['title'] or '')[:55]}"
                        )
                    else:
                        pred.execute(
                            "UPDATE whale_predictions SET resolved=1, last_checked=datetime('now') WHERE id=?",
                            (r["id"],),
                        )
                        still_open += 1
                elif status in ("active", "open"):
                    pred.execute(
                        "UPDATE whale_predictions SET resolved=1, last_checked=datetime('now') WHERE id=?",
                        (r["id"],),
                    )
                    still_open += 1
                else:
                    still_open += 1
            else:
                errors += 1
                time.sleep(0.2)
                continue
        elif platform == "polymarket":
            result = check_polymarket_resolution(ticker)
            if result["status"] == "closed" and result.get("outcome"):
                outcome = result["outcome"]
                actual = "YES" if outcome.lower() in ("yes", "true") else "NO"
                whale_dir = r["dominant_direction"]
                won = None if whale_dir == "EVEN" else (1 if actual == whale_dir else 0)
                pred.execute(
                    "UPDATE whale_predictions SET resolved=2, actual_outcome=?, whale_won=?, last_checked=datetime('now') WHERE id=?",
                    (actual, won, r["id"]),
                )
                resolved += 1
                icon = "🟢" if won == 1 else "🔴" if won == 0 else "❓"
                print(
                    f"  {icon} ${r['total_flow']:>8,.0f} | BET {whale_dir}->{actual} | {'WON' if won == 1 else 'LOST' if won == 0 else '?'} | polymarket | {(r['title'] or '')[:55]}"
                )
            else:
                still_open += 1
            time.sleep(0.2)
        checked += 1
        time.sleep(0.2)
        if checked % 100 == 0:
            pred.commit()
            print(f"    Progress: {checked}/{total}")
    pred.commit()
    return checked, resolved, still_open, errors


def _priced_edge_line(pred) -> str:
    """Mean (whale_won − entry) per contract — the WITH/AGAINST number."""
    rows = pred.execute("""
        SELECT whale_won, dominant_direction dd, alert_price, taker_pct
        FROM whale_predictions
        WHERE resolved=2 AND whale_won IN (0,1) AND alert_price IS NOT NULL
          AND dominant_direction IN ('YES','NO')
    """).fetchall()
    if not rows:
        return "Priced edge: n/a (no alert_price yet)"
    entry = lambda r: r["alert_price"] if r["dd"] == "YES" else 1.0 - r["alert_price"]
    ret = sum(r["whale_won"] - entry(r) for r in rows) / len(rows)
    agg = [r for r in rows if (r["taker_pct"] or 50) >= 70]
    agg_ret = (sum(r["whale_won"] - entry(r) for r in agg) / len(agg)) if agg else 0.0
    return (f"Priced edge: {ret*100:+.1f}c/contract (n={len(rows)}) | "
            f"aggressive-taker: {agg_ret*100:+.1f}c (n={len(agg)})")


def summary(pred, verbose=False):
    total = pred.execute("SELECT COUNT(*) FROM whale_predictions").fetchone()[0]
    resolved_count = pred.execute(
        "SELECT COUNT(*) FROM whale_predictions WHERE resolved = 2"
    ).fetchone()[0]
    open_count = pred.execute(
        "SELECT COUNT(*) FROM whale_predictions WHERE resolved = 1"
    ).fetchone()[0]
    unchecked = total - resolved_count - open_count
    wins = pred.execute(
        "SELECT COUNT(*) FROM whale_predictions WHERE whale_won = 1"
    ).fetchone()[0]
    losses = pred.execute(
        "SELECT COUNT(*) FROM whale_predictions WHERE whale_won = 0"
    ).fetchone()[0]
    even = pred.execute(
        "SELECT COUNT(*) FROM whale_predictions WHERE resolved = 2 AND whale_won IS NULL"
    ).fetchone()[0]
    k_wins = pred.execute(
        "SELECT COUNT(*) FROM whale_predictions WHERE platform='kalshi' AND whale_won=1"
    ).fetchone()[0]
    k_losses = pred.execute(
        "SELECT COUNT(*) FROM whale_predictions WHERE platform='kalshi' AND whale_won=0"
    ).fetchone()[0]
    p_wins = pred.execute(
        "SELECT COUNT(*) FROM whale_predictions WHERE platform='polymarket' AND whale_won=1"
    ).fetchone()[0]
    p_losses = pred.execute(
        "SELECT COUNT(*) FROM whale_predictions WHERE platform='polymarket' AND whale_won=0"
    ).fetchone()[0]
    print(f"\n{'=' * 60}")
    print(f"  WHALE PREDICTION TRACKER")
    print(f"{'=' * 60}")
    print(
        f"  Total: {total}  |  Pending: {unchecked}  |  Open: {open_count}  |  Resolved: {resolved_count}"
    )
    print(f"  WON: {wins}  |  LOST: {losses}  |  Even: {even}")
    if wins + losses > 0:
        print(f"  Win rate: {wins / (wins + losses) * 100:.1f}%")
        print(
            f"  Kalshi: {k_wins}W/{k_losses}L ({k_wins / (k_wins + k_losses) * 100:.0f}%)"
        )
        if p_wins + p_losses > 0:
            print(
                f"  Polymarket: {p_wins}W/{p_losses}L ({p_wins / (p_wins + p_losses) * 100:.0f}%)"
            )
    if resolved_count > 0:
        print(f"\n  === BIGGEST RESOLVED ===")
        recent = pred.execute("""
            SELECT title, dominant_direction, total_flow, actual_outcome, whale_won, platform
            FROM whale_predictions WHERE resolved = 2
            ORDER BY total_flow DESC LIMIT 30
        """).fetchall()
        for r in recent:
            icon = (
                "🟢" if r["whale_won"] == 1 else ("🔴" if r["whale_won"] == 0 else "❓")
            )
            won_str = (
                "WON"
                if r["whale_won"] == 1
                else ("LOST" if r["whale_won"] == 0 else "?")
            )
            t = (r["title"] or "")[:55]
            print(
                f"  {icon} ${r['total_flow']:>8,.0f} | BET {r['dominant_direction']}->{r['actual_outcome']} | {won_str} | [{r['platform']}] {t}"
            )


def run_backfill(pred):
    backfill_predictions(pred)


def run_check(pred):
    print(f"\n  Checking all platforms for resolutions...")
    c, r, o, e = check_resolutions(pred)
    print(f"  Done: checked={c} | resolved={r} | still_open={o} | errors={e}")
    return r


def telegram_summary(pred, new_resolved=0):
    """Compact plain-text daily report (no Markdown — alert_openclaw sends with
    parse_mode=Markdown and stray * / _ in titles would 400)."""
    total = pred.execute("SELECT COUNT(*) FROM whale_predictions").fetchone()[0]
    resolved = pred.execute(
        "SELECT COUNT(*) FROM whale_predictions WHERE resolved=2"
    ).fetchone()[0]
    open_c = pred.execute(
        "SELECT COUNT(*) FROM whale_predictions WHERE resolved=1"
    ).fetchone()[0]
    pending = total - resolved - open_c
    wins = pred.execute(
        "SELECT COUNT(*) FROM whale_predictions WHERE whale_won=1"
    ).fetchone()[0]
    losses = pred.execute(
        "SELECT COUNT(*) FROM whale_predictions WHERE whale_won=0"
    ).fetchone()[0]
    kw = pred.execute(
        "SELECT COUNT(*) FROM whale_predictions WHERE platform='kalshi' AND whale_won=1"
    ).fetchone()[0]
    kl = pred.execute(
        "SELECT COUNT(*) FROM whale_predictions WHERE platform='kalshi' AND whale_won=0"
    ).fetchone()[0]
    pw = pred.execute(
        "SELECT COUNT(*) FROM whale_predictions WHERE platform='polymarket' AND whale_won=1"
    ).fetchone()[0]
    pl = pred.execute(
        "SELECT COUNT(*) FROM whale_predictions WHERE platform='polymarket' AND whale_won=0"
    ).fetchone()[0]

    head = (
        f"🐋 {new_resolved} NEW RESOLUTION(S)"
        if new_resolved
        else "🐋 No new resolutions"
    )
    lines = [head]
    if new_resolved:
        recent = pred.execute(
            """
            SELECT title, dominant_direction, total_flow, actual_outcome, whale_won, platform
            FROM whale_predictions WHERE resolved=2
            ORDER BY last_checked DESC LIMIT ?""",
            (min(new_resolved, 6),),
        ).fetchall()
        for r in recent:
            icon = (
                "🟢" if r["whale_won"] == 1 else ("🔴" if r["whale_won"] == 0 else "❓")
            )
            wl = (
                "WON"
                if r["whale_won"] == 1
                else ("LOST" if r["whale_won"] == 0 else "?")
            )
            lines.append(
                f"{icon} ${r['total_flow']:,.0f} {r['dominant_direction']}->{r['actual_outcome']} "
                f"{wl} | {r['platform']} | {(r['title'] or '')[:44]}"
            )
    if wins + losses:
        lines.append(
            f"Win rate: {wins / (wins + losses) * 100:.0f}% ({wins}W/{losses}L)"
        )
    if kw + kl:
        lines.append(f"Kalshi: {kw}W/{kl}L ({kw / (kw + kl) * 100:.0f}%)")
    lines.append(_priced_edge_line(pred))
    if pw + pl:
        lines.append(f"PM: {pw}W/{pl}L ({pw / (pw + pl) * 100:.0f}%)")
    lines.append(f"Tracked: {total} | resolved {resolved} | pending {pending}")
    return "\n".join(lines)


def _send_summary(text: str) -> None:
    """Push the resolution summary to Telegram (direct send stays
    authoritative) and mirror it into the dispatch queue in shadow mode
    (Task 5.3 Step 1). dedup_key = digest of the summary body (entity+state:
    same resolved-set state never enqueues twice within a batch window)."""
    print(text)
    try:
        sys.path.insert(0, str(PRED_DB.parent.parent))
        from scripts.openclaw_alerts import alert_openclaw

        # LIVE since 2026-08-21: routed through the tier-2 batch queue instead
        # of a direct push. The former direct alert_openclaw() send was removed
        # at the same time — keeping both would double-deliver every resolution.
        import hashlib

        from signals.alert_dispatch import dispatch

        ok = dispatch(
            "whale_resolutions", text, tier=2,
            dedup_key="resolutions:" + hashlib.sha1(text.encode()).hexdigest()[:16])
        print(f"[send] whale_resolutions queued ok={ok}")
    except Exception as e:
        print(f"[send] failed: {e}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument(
        "--send",
        action="store_true",
        help="push compact summary to Telegram (Bot API, no LLM)",
    )
    args = parser.parse_args()
    pred = open_db(PRED_DB)
    get_or_create_table(pred)
    new_resolved = 0
    if args.all or args.backfill:
        run_backfill(pred)
    if args.all or args.check or args.send:
        new_resolved = run_check(pred) or 0
    if (
        args.all
        or args.summary
        or (not args.backfill and not args.check and not args.send)
    ):
        summary(pred, verbose=args.verbose)
    if args.send:
        _send_summary(telegram_summary(pred, new_resolved))
    pred.close()
