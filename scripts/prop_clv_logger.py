#!/usr/bin/env python3
"""
prop_clv_logger.py — generalized, config-driven prop CLV paper-logger.

Spec: 02-Projects/Polyclawd/Development/Prop-CLV-Generalized-Spec.md

Same mechanism as scorer_paper_logger but multi-sport: snapshot live props for a
configured sport, persist to SQLite, and report the CONTROL-CORRECTED gate
(within-event/within-card: do selected bets move more than same-unit control bets?).

  python3 scripts/prop_clv_logger.py --config mlb --db storage/prop_clv.db --snapshot
  python3 scripts/prop_clv_logger.py --config mlb --db storage/prop_clv.db --report
  python3 scripts/prop_clv_logger.py --list
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase0_prop_falsification as P  # noqa: E402  (fetch + iso parse helpers)
from prop_clv_config import CONFIGS  # noqa: E402
from prop_clv_shapes import extract_bets  # noqa: E402


def send_telegram(text: str):
    """Direct Bot API send (no LLM). Reads TELEGRAM_BOT_TOKEN/CHAT_ID from env
    (sourced from ~/.config/polyclawd/alerts.env in cron). Plain text — no
    parse_mode, to dodge the Markdown-400 trap."""
    import urllib.parse
    import urllib.request

    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("[send] TELEGRAM_BOT_TOKEN/CHAT_ID not set — skipping")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=20)
        print("[send] alert sent")
    except Exception as e:
        print(f"[send] failed: {e}")


def db_connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    # WAL + busy-timeout: multiple sport snapshot crons share one DB; this lets a
    # reader never block and a second writer wait out a brief insert lock instead of
    # throwing "database is locked" (/qa follow-up 2026-06-18).
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("""CREATE TABLE IF NOT EXISTS prop_snapshot (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_at TEXT NOT NULL, source TEXT NOT NULL, sport TEXT NOT NULL,
        event_id TEXT NOT NULL, event_title TEXT, commence_time TEXT,
        participant TEXT NOT NULL, market TEXT NOT NULL, line REAL, side TEXT NOT NULL,
        consensus_fair REAL, soft_book TEXT, soft_implied REAL,
        edge_pct REAL, n_sharp INTEGER, mins_to_kickoff REAL,
        UNIQUE(sport, event_id, participant, market, line, side, snapshot_at))""")
    con.execute("""CREATE TABLE IF NOT EXISTS gate_state (
        sport TEXT PRIMARY KEY, verdict TEXT, n_pair INTEGER,
        beat_rate REAL, alerted_at TEXT)""")
    con.commit()
    return con


def _insert(con, **r):
    cols = ",".join(r)
    ph = ",".join("?" * len(r))
    try:
        con.execute(f"INSERT INTO prop_snapshot ({cols}) VALUES ({ph})", tuple(r.values()))
        return 1
    except sqlite3.IntegrityError:
        return 0


def credits_remaining(key):
    """Free /sports call; returns x-requests-remaining (int) or None."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"{P.ODDS_API_BASE}/sports?apiKey={key}", timeout=20) as r:
            v = r.headers.get("x-requests-remaining")
            return int(float(v)) if v is not None else None
    except Exception:
        return None


def live_snapshot(con, config, window_hours, max_events, min_credits=1000):
    key = os.getenv("ODDS_API_KEY")
    if not key:
        sys.exit("ODDS_API_KEY not set — cannot snapshot live.")
    rem = credits_remaining(key)
    if rem is not None and rem < min_credits:
        print(f"[abort] credits remaining {rem} < floor {min_credits} — skipping snapshot")
        return
    if rem is not None:
        print(f"[credits] {rem} remaining (floor {min_credits})")
    books = ",".join([b for b, _ in config.sharp_books] + sorted(config.soft_books))
    markets = ",".join(config.market_keys)
    now = datetime.now(timezone.utc)
    events = P._get(f"{P.ODDS_API_BASE}/sports/{config.sport_key}/events?apiKey={key}")
    snap_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    # in-window, soonest first; cap to bound credit spend
    windowed = []
    for ev in events:
        ct = P._parse_iso(ev.get("commence_time"))
        if ct is None:
            continue
        mins = (ct - now).total_seconds() / 60.0
        if 0 < mins <= window_hours * 60:
            windowed.append((mins, ev))
    windowed.sort(key=lambda x: x[0])
    windowed = windowed[:max_events]

    n_rows = n_ev = 0
    for mins, ev in windowed:
        eid = ev["id"]
        url = (
            f"{P.ODDS_API_BASE}/sports/{config.sport_key}/events/{eid}/odds?apiKey={key}"
            f"&regions=us,uk,eu&markets={markets}&oddsFormat=american&bookmakers={books}"
        )
        try:
            od = P._get(url)
        except Exception as e:
            print(f"  [skip] {eid}: {e}")
            continue
        title = f"{od.get('home_team')} vs {od.get('away_team')}"
        got = 0
        for b in extract_bets(od, config):
            got += _insert(
                con,
                snapshot_at=snap_at,
                source="live",
                sport=config.name,
                event_id=eid,
                event_title=title,
                commence_time=ev.get("commence_time"),
                participant=b.participant,
                market=b.market,
                line=b.line,
                side=b.side,
                consensus_fair=b.consensus_fair,
                soft_book=b.soft_book,
                soft_implied=b.soft_implied,
                edge_pct=b.edge_pct,
                n_sharp=b.n_sharp,
                mins_to_kickoff=round(mins, 1),
            )
        if got:
            n_ev += 1
            n_rows += got
            print(f"  [snap] T-{mins / 60:.1f}h {title}: {got} bets")
    con.commit()
    print(f"[snapshot] {config.name}: {n_rows} rows across {n_ev} events @ {snap_at}")


# Minimum sharp books required before a row may influence a CLV verdict.
# Deliberately 1, not 2: MLB pitcher_strikeouts carries exactly ONE sharp book
# (Pinnacle) on every one of its 29,957 rows, and that is a legitimate anchor —
# its mean edge is a sane -0.68%. A blanket >=2 would zero the MLB gate outright.
# What actually needed removing was the two EPL markets with NO sharp coverage
# at all (see prop_clv_config); this threshold only drops genuinely anchorless
# rows, e.g. UFC's 3,576 n_sharp=0 h2h rows.
MIN_SHARP_FOR_GATE = 1


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def report(con, config, send=False):
    # MIN_SHARP gate (2026-08-22): a "consensus fair" derived from ONE book is
    # not a consensus, and edge measured against it is noise. Measured on
    # soccer_epl: rows with n_sharp<=1 showed +3.1% mean edge, n_sharp=2 +2.2%,
    # n_sharp=3 exactly 0.0% — apparent edge decaying to zero as books are added
    # is the signature of a stale-line artifact, not an inefficiency. Worse,
    # player_shots_on_target was 100% anchorless yet produced 18,181 of the
    # 22,958 flagged EPL bets. Excluded from the gate entirely; still SNAPSHOTTED
    # so the raw record stays complete and this is re-derivable.
    # Restrict to the markets this config CURRENTLY declares. Dropping a market
    # from the config stops collection but leaves its history in the table, and
    # report() keys on sport, not market — so without this the retired EPL
    # shots_on_target/assists rows (53,310 of them, 100% unanchored, carrying an
    # artifact +7.15% edge) would keep driving the EPL verdict forever.
    _mk = config.market_keys
    _ph = ",".join("?" * len(_mk))
    rows = con.execute(
        f"""SELECT event_id, event_title, commence_time, participant, market, line, side,
                   snapshot_at, soft_implied, edge_pct, mins_to_kickoff
            FROM prop_snapshot
           WHERE sport=? AND COALESCE(n_sharp,0) >= ? AND market IN ({_ph})""",
        (config.name, MIN_SHARP_FOR_GATE, *_mk),
    ).fetchall()

    series = defaultdict(list)  # (eid,part,market,line,side) -> [(snap,soft,edge,mins)]
    unit_of = {}  # series_key -> pairing unit (event_id or card-date)
    for eid, title, ct, part, market, line, side, snap, soft, edge, mins in rows:
        k = (eid, part, market, line, side)
        series[k].append((snap, soft, edge, mins))
        unit_of[k] = eid if config.control_unit == "event" else (ct or "")[:10]

    sel_live = defaultdict(list)  # unit -> [mv]   selected, live anchor (headline)
    sel_unif = defaultdict(list)  # unit -> [mv]   selected, uniform anchor
    ctl_unif = defaultdict(list)  # unit -> [mv]   control, uniform anchor
    dropped = 0
    for k, snaps in series.items():
        snaps.sort(key=lambda x: x[0])
        if len(snaps) < 2:
            dropped += 1
            continue
        u = unit_of[k]
        pre = [s for s in snaps if s[3] is not None and s[3] >= 0]
        ever = any(s[2] is not None and s[2] >= config.min_edge for s in snaps)
        e, c = snaps[0], (max(pre, key=lambda x: x[0]) if pre else snaps[-1])
        if c[0] > e[0] and e[1] is not None and c[1] is not None:
            (sel_unif if ever else ctl_unif)[u].append((c[1] - e[1]) * 100.0)
        if ever:
            flagged = [s for s in snaps if s[2] is not None and s[2] >= config.min_edge]
            en, cl = flagged[0], (max(pre, key=lambda x: x[0]) if pre else snaps[-1])
            if cl[0] > en[0] and en[1] is not None and cl[1] is not None:
                sel_live[u].append((cl[1] - en[1]) * 100.0)

    # headline (uncorrected): selected unit-level beat-rate vs 50%
    sl_mean = {u: sum(v) / len(v) for u, v in sel_live.items() if v}
    n_u = len(sl_mean)
    beats = sum(1 for m in sl_mean.values() if m > 0)
    lo, hi = wilson(beats, n_u)
    # honest baseline
    allmv = [m for v in sel_unif.values() for m in v] + [m for v in ctl_unif.values() for m in v]
    base_pos = sum(1 for m in allmv if m > 0)
    base_n = len(allmv)
    # DECISIVE: within-unit paired delta. Sign-test convention — DROP ties (delta≈0),
    # else a perfect tie (no edge) miscounts as a loss and fires a spurious KILL.
    EPS = 1e-9
    su = {u: sum(v) / len(v) for u, v in sel_unif.items() if v}
    cu = {u: sum(v) / len(v) for u, v in ctl_unif.items() if v}
    all_deltas = [su[u] - cu[u] for u in su if u in cu]
    ties = sum(1 for d in all_deltas if abs(d) <= EPS)
    paired = [d for d in all_deltas if abs(d) > EPS]
    n_pair = len(paired)
    pair_pos = sum(1 for d in paired if d > 0)
    plo, phi = wilson(pair_pos, n_pair)
    mean_delta = (sum(paired) / n_pair) if n_pair else 0.0

    unit_label = config.control_unit
    print("\n" + "═" * 74)
    print(f"  PROP CLV — {config.name}  (control-corrected gate, unit={unit_label})")
    print("═" * 74)
    print(
        f"  [headline, NOT decisive] selected vs 50%: {beats}/{n_u} {unit_label}s"
        f"{(' = %.0f%%' % (100 * beats / n_u)) if n_u else ''}, Wilson [{lo:.2f}, {hi:.2f}]"
    )
    if base_n:
        print(
            f"  [honest baseline] ALL bets moved toward side: {base_pos}/{base_n} = "
            f"{100 * base_pos / base_n:.0f}%  <- the real null, not 50%"
        )
    print(f"\n  [DECISIVE] within-{unit_label}  delta = mean(selected) - mean(control):")
    print(f"    {unit_label}s with both groups: {n_pair}")
    if n_pair:
        print(
            f"    selected beat control: {pair_pos}/{n_pair} = {100 * pair_pos / n_pair:.0f}%"
            f"   Wilson95 [{plo:.2f}, {phi:.2f}]   mean delta {mean_delta:+.2f}pp"
        )
    if dropped or ties:
        print(f"  (dropped {dropped} single-snapshot/line-moved series, {ties} tied units — not silently)")

    print("\n" + "═" * 74)
    if n_pair < 12:
        tag = "CONTINUE"
        v = f"CONTINUE — only {n_pair} {unit_label}s have both selected & control (need ~12+)."
    elif plo > 0.50:
        tag = "CONFIRM"
        v = f"CONFIRM — selected beat same-{unit_label} control {pair_pos}/{n_pair}, CI lower {plo:.2f} > 0.50."
    elif phi < 0.50:
        tag = "KILL"
        v = f"KILL — selected do NOT beat control (CI upper {phi:.2f} < 0.50). Edge is drift."
    else:
        tag = "CONTINUE"
        v = f"CONTINUE — paired CI [{plo:.2f},{phi:.2f}] straddles 0.50; accumulate more {unit_label}s."
    print(f"  VERDICT: {v}")
    print("═" * 74 + "\n")

    beat_rate = (pair_pos / n_pair) if n_pair else None
    stats = {
        "sport": config.name,
        "unit": unit_label,
        "verdict": v,
        "tag": tag,
        "n_pair": n_pair,
        "pair_pos": pair_pos,
        "beat_rate": beat_rate,
        "plo": plo,
        "phi": phi,
        "mean_delta": mean_delta,
        "base_rate": (base_pos / base_n) if base_n else None,
        "base_n": base_n,
    }
    if send:
        send_telegram(_fmt_msg(stats, prefix="⚽ Prop CLV"))
    return stats


def _fmt_msg(s, prefix):
    """One clean Telegram block for a sport's gate state."""
    br = f"{100 * s['beat_rate']:.0f}%" if s["beat_rate"] is not None else "n/a"
    base = f"{100 * s['base_rate']:.0f}%" if s["base_rate"] is not None else "n/a"
    decisive = (
        f"selected beat control {s['pair_pos']}/{s['n_pair']} ({br}), "
        f"CI [{s['plo']:.2f},{s['phi']:.2f}], Δ{s['mean_delta']:+.2f}pp"
        if s["n_pair"]
        else f"only {s['n_pair']} {s['unit']}s with both groups — accumulating"
    )
    return f"{prefix} [{s['sport']}] — {s['tag']}\nwithin-{s['unit']}: {decisive}\n(baseline: all bets moved {base})"


def _significant(cur, prev) -> str | None:
    """Return a short reason string if the change is worth a Telegram ping, else None."""
    if prev is None:
        # first ever: only ping if already decisive (don't spam the initial CONTINUE)
        return "first decisive read" if cur["tag"] != "CONTINUE" or cur["n_pair"] >= 12 else None
    p_verdict, p_n, p_rate = prev  # stored verdict is the tag (CONTINUE/CONFIRM/KILL)
    if cur["tag"] != p_verdict:
        return f"verdict {p_verdict} → {cur['tag']}"
    if cur["n_pair"] >= 12 and (p_n or 0) < 12:
        return "gate became decisive (n≥12)"
    if cur["beat_rate"] is not None and p_rate is not None and abs(cur["beat_rate"] - p_rate) >= 0.15:
        return f"beat-rate swing {100 * p_rate:.0f}%→{100 * cur['beat_rate']:.0f}%"
    return None


def run_alert(con, config, force=False):
    """Compute the gate; send a Telegram ping only on a significant change (or --force)."""
    stats = report(con, config, send=False)
    row = con.execute("SELECT verdict, n_pair, beat_rate FROM gate_state WHERE sport=?", (config.name,)).fetchone()
    reason = _significant(stats, row)
    if force and reason is None:
        reason = "weekly heartbeat"
    if reason:
        send_telegram(
            f"🎯 {('[heartbeat] ' if reason == 'weekly heartbeat' else 'CHANGE: ' + reason + chr(10))}"
            + _fmt_msg(stats, prefix="Prop CLV")
        )
    else:
        print(f"  [alert] no significant change for {config.name} — staying quiet")
    con.execute(
        """INSERT INTO gate_state (sport, verdict, n_pair, beat_rate, alerted_at)
                   VALUES (?,?,?,?,datetime('now'))
                   ON CONFLICT(sport) DO UPDATE SET verdict=excluded.verdict,
                     n_pair=excluded.n_pair, beat_rate=excluded.beat_rate, alerted_at=excluded.alerted_at""",
        (config.name, stats["tag"], stats["n_pair"], stats["beat_rate"]),
    )
    con.commit()


def run_digest(con):
    """One combined heartbeat message across all sports (weekly; proves the rig is alive)."""
    lines = ["🎯 Prop CLV weekly digest"]
    for name, cfg in CONFIGS.items():
        if name == "soccer_wc":
            continue  # handled by the old scorer logger
        s = report(con, cfg, send=False)
        if s["n_pair"]:
            br = f"{100 * s['beat_rate']:.0f}%"
            lines.append(f"• {name}: {s['tag']} — {s['pair_pos']}/{s['n_pair']} ({br}), Δ{s['mean_delta']:+.1f}pp")
        else:
            lines.append(f"• {name}: {s['tag']} — accumulating ({s['n_pair']} {s['unit']}s w/ both groups)")
    send_telegram("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description="Generalized multi-sport prop CLV logger.")
    ap.add_argument("--config", help="sport config name (see --list)")
    ap.add_argument("--db", default="storage/prop_clv.db")
    ap.add_argument("--window-hours", type=float, default=8.0)
    ap.add_argument("--max-events", type=int, default=20, help="cap events per snapshot (credit guard)")
    ap.add_argument("--min-credits", type=int, default=1000, help="abort snapshot below this credit floor")
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--send", action="store_true", help="force-send this report to Telegram")
    ap.add_argument("--alert", action="store_true", help="send Telegram ONLY on significant change")
    ap.add_argument("--force", action="store_true", help="with --alert: send even if unchanged (heartbeat)")
    ap.add_argument("--digest", action="store_true", help="one combined weekly heartbeat across all sports")
    ap.add_argument("--list", action="store_true", help="list available sport configs")
    args = ap.parse_args()

    if args.list:
        for name, c in CONFIGS.items():
            print(f"  {name:12s} {c.sport_key:28s} markets={c.market_keys} unit={c.control_unit}")
        return

    con = db_connect(args.db)
    if args.digest:
        run_digest(con)
        con.close()
        return
    if not args.config or args.config not in CONFIGS:
        sys.exit(f"--config must be one of: {', '.join(CONFIGS)}")
    config = CONFIGS[args.config]

    if args.snapshot:
        live_snapshot(con, config, args.window_hours, args.max_events, args.min_credits)
    if args.alert:
        run_alert(con, config, force=args.force)
    elif args.report or not args.snapshot:
        report(con, config, send=args.send)
    con.close()


if __name__ == "__main__":
    main()
