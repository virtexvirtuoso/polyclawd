#!/usr/bin/env python3
"""canary_watch — durable Telegram watch for the live canary's first fill.

Replaces a session-bound agent Monitor (which dies when the chat closes) with a
VPS cron job that self-sends via alert_openclaw — the same pattern as
live_monitor_report.py. "Logs always, pings only on change", plus a daily
heartbeat so silence is never ambiguous (dead watcher == quiet watcher).

Watches, since the 2026-08-21T15:00Z IPv6 fix:
  • new live_fills rows        -> ALERT (each one; ~30 needed for the gate)
  • "Trading restricted"       -> ALERT (geoblock returned)
  • "egress guard" refusals    -> ALERT (fail-closed fired; routing leaked)
  • "governor denied"          -> ALERT (sizing/allowlist rejected a trade)
  • egress src != tunnel       -> ALERT (pre-order warning; leak is back)
  • 24h with nothing to say    -> heartbeat

Cron (every 5 min):
  */5 * * * * cd /var/www/virtuosocrypto.com/polyclawd && set -a && \
    . /home/linuxuser/.config/polyclawd/alerts.env && set +a && \
    venv/bin/python3 scripts/canary_watch.py >> logs/canary_watch.log 2>&1
"""
from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.openclaw_alerts import alert_openclaw  # noqa: E402

DB = ROOT / "storage" / "shadow_trades.db"
STATE = Path("/home/linuxuser/logs/canary_watch.state.json")
ARM_ISO = "2026-08-21T15:00:00"          # IPv6 leak fixed; orders first reachable
ARM_JOURNAL = "2026-08-21 15:00"
EXPECT_SRC = "10.2.0.2"                   # proton-ie tunnel source
GATE_TARGET = 30                          # fills needed for the canary gate
HEARTBEAT_S = 24 * 3600

COOLDOWN_S = {
    "geoblock": 3600,
    "egress_guard": 3600,
    "egress_leak": 1800,
    "governor_denied": 6 * 3600,
}


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(st: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(st, indent=2))
    except Exception as exc:
        log(f"WARN: state write failed: {exc}")


def q(sql: str, default=None):
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
        try:
            return conn.execute(sql).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        log(f"WARN: query failed: {exc}")
        return default


def journal_count(pattern: str) -> int:
    """Case-insensitive count of matching scheduler journal lines since ARM."""
    try:
        out = subprocess.run(
            ["journalctl", "-u", "polyclawd-scheduler", "--since", ARM_JOURNAL, "--no-pager"],
            capture_output=True, text=True, timeout=60,
        ).stdout
        return sum(1 for line in out.splitlines() if pattern.lower() in line.lower())
    except Exception as exc:
        log(f"WARN: journal read failed: {exc}")
        return 0


def egress_src(host: str = "relayer-v2.polymarket.com", port: int = 443) -> str | None:
    """Source IP the kernel would use — AF_UNSPEC so an IPv6 leak is visible.
    Connected UDP socket: route selection only, no packets sent."""
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_DGRAM)
        if not infos:
            return None
        family, _st, _pr, _cn, addr = infos[0]
        s = socket.socket(family, socket.SOCK_DGRAM)
        try:
            s.connect(addr)
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception as exc:
        log(f"WARN: egress probe failed: {exc}")
        return None


def cooled_down(st: dict, key: str, now: float) -> bool:
    """True if this alert kind may fire again (never suppresses the first)."""
    last = st.get("last_alert", {}).get(key, 0)
    return (now - last) >= COOLDOWN_S.get(key, 3600)


def mark(st: dict, key: str, now: float) -> None:
    st.setdefault("last_alert", {})[key] = now


def main() -> int:
    now = datetime.now(timezone.utc).timestamp()
    st = load_state()
    alerts: list[str] = []

    # ---- fills (the thing we're waiting for) -------------------------------
    row = q(f"SELECT COUNT(*) FROM live_fills WHERE ts >= '{ARM_ISO}'")
    fills = int(row[0]) if row else 0
    prev_fills = int(st.get("fills", 0))

    if fills > prev_fills:
        det = q(
            "SELECT side, round(shares,2), price, round(usd,2), liquidity, ts "
            f"FROM live_fills WHERE ts >= '{ARM_ISO}' ORDER BY id DESC LIMIT 1"
        )
        first = prev_fills == 0
        head = "🎉 <b>FIRST LIVE FILL</b>" if first else "✅ <b>Live fill</b>"
        body = ""
        if det:
            side, sh, px, usd, liq, ts = det
            body = (f"\n{side} {sh} shares @ {px} = ${usd} ({liq})"
                    f"\n<i>{str(ts)[:19]}Z</i>")
        alerts.append(
            f"{head}{body}\nCanary gate progress: <b>{fills}/{GATE_TARGET}</b> fills"
        )
        st["fills"] = fills

    # ---- failure signals ---------------------------------------------------
    checks = [
        ("geoblock", "Trading restricted",
         "🚫 <b>GEOBLOCK RETURNED</b> — orders rejected by Polymarket.\n"
         "Egress likely leaked off the tunnel. Check: "
         "<code>curl -o /dev/null -w '%{local_ip}' https://relayer-v2.polymarket.com/</code> "
         f"(must be {EXPECT_SRC})"),
        ("egress_guard", "egress guard",
         "🛡️ <b>EGRESS GUARD REFUSED AN ORDER</b> — fail-closed worked; routing "
         "is off-tunnel so no order leaked. Run <code>sudo /usr/local/sbin/polymarket-wg-sync</code>."),
        ("governor_denied", "governor denied",
         "⚖️ <b>Governor denied a live trade</b> — sizing or allowlist rejected it. "
         "Check the reason in the scheduler journal."),
    ]
    for key, pattern, message in checks:
        count = journal_count(pattern)
        if count > int(st.get(key, 0)) and cooled_down(st, key, now):
            alerts.append(f"{message}\n<i>{count} occurrence(s) since arming</i>")
            mark(st, key, now)
        st[key] = count

    # ---- egress health (pre-order warning) ---------------------------------
    src = egress_src()
    if src and src != EXPECT_SRC:
        if cooled_down(st, "egress_leak", now):
            alerts.append(
                f"⚠️ <b>EGRESS LEAK</b> — order traffic would leave from <code>{src}</code>, "
                f"expected <code>{EXPECT_SRC}</code>.\nOrders will be geo-blocked until fixed. "
                "The 5-min <code>polymarket-wg-sync</code> timer should self-heal; "
                "if it persists, check IPv6 blackhole routes."
            )
            mark(st, "egress_leak", now)
    st["egress_src"] = src or "unknown"

    # ---- heartbeat so silence is never ambiguous ---------------------------
    last_msg = float(st.get("last_message_ts", 0))
    if not alerts and (now - last_msg) >= HEARTBEAT_S:
        alerts.append(
            f"💤 <b>Canary heartbeat</b> — still waiting for first fill.\n"
            f"Fills: {fills}/{GATE_TARGET} · egress <code>{src or 'unknown'}</code> · no errors."
        )

    # ---- send --------------------------------------------------------------
    if alerts:
        msg = "\n\n".join(alerts)
        ok = alert_openclaw(msg, parse_mode="HTML")
        log(f"alert sent={ok} ({len(alerts)} item(s))")
        if ok:
            st["last_message_ts"] = now
    else:
        log(f"quiet: fills={fills}/{GATE_TARGET} egress={src} (no change)")

    save_state(st)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        tb = traceback.format_exc()
        log(f"FATAL:\n{tb}")
        try:
            alert_openclaw(
                "💥 <b>canary_watch CRASHED</b>\n<pre>" + tb[-600:] + "</pre>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        sys.exit(2)
