#!/usr/bin/env python3
"""
Live trading monitor — LLM-free replacement for the OpenClaw
`live-trading-monitor` cron (job 233a8e4c, disabled when this went live).

Every 4h: fetch /api/live/{governor,positions,fills}, hash the state, and
send a Telegram report via alert_openclaw ONLY when something changed.
When nothing changes, stays silent except one daily heartbeat on the
20:00 ET run so silence never becomes ambiguous with a dead cron.

State lives in cache/live_monitor_state.json (durable — /tmp resets on
reboot and would fire a spurious "changed" report every restart).

Vault doc: 02-Projects/Polyclawd/Strategy/Live-Monitor-Cron-Optimization-2026-07-17.md
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from openclaw_alerts import alert_openclaw

API_BASE = "http://127.0.0.1:8420/api/live"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(PROJECT_ROOT, "cache", "live_monitor_state.json")
ET = ZoneInfo("America/New_York")
# Heartbeat on the first run after 24h of silence (DST-proof — a fixed
# "20:00 ET" check never matches the 0 */4 UTC cron grid in winter).
HEARTBEAT_MIN_GAP_H = 24


def fetch(path: str):
    with urllib.request.urlopen(f"{API_BASE}/{path}", timeout=15) as resp:
        return json.loads(resp.read().decode())


def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def state_fingerprint(gov: dict, positions: list, fills: list, api_error: str) -> str:
    """Deterministic summary of everything worth waking Mr. V for."""
    return json.dumps(
        {
            "error": api_error,
            "gov_state": gov.get("state"),
            "mode": gov.get("mode"),
            "bankroll": round(gov.get("bankroll") or 0, 2),
            "deployed": round(gov.get("deployed_usd") or 0, 2),
            "daily_loss": round(gov.get("daily_loss") or 0, 2),
            "positions": sorted((p.get("id"), p.get("entry"), round(p.get("shares") or 0, 2)) for p in positions),
            "last_fill_id": max((f.get("id") or 0 for f in fills), default=0),
        },
        sort_keys=True,
    )


def age_days(opened_at: str) -> str:
    try:
        opened = datetime.fromisoformat(opened_at)
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        return f"{(datetime.now(timezone.utc) - opened).total_seconds() / 86400:.1f}d"
    except (ValueError, TypeError):
        return "?"


def build_report(gov: dict, positions: list, new_fills: list, now_et: datetime) -> str:
    bankroll = gov.get("bankroll") or 0
    deployed = gov.get("deployed_usd") or 0
    pct = f"{deployed / bankroll * 100:.0f}%" if bankroll else "n/a"
    lines = [
        f"📡 Live Monitor — {now_et:%Y-%m-%d %H:%M} ET",
        f"Governor: {gov.get('state')} ({gov.get('mode')}) | Bankroll ${bankroll:.2f} | "
        f"Deployed ${deployed:.2f} ({pct}) | Daily loss ${gov.get('daily_loss') or 0:.2f}",
    ]
    lines.append(f"Positions ({len(positions)}):" if positions else "Positions: none")
    for p in positions:
        lines.append(
            f"• {p.get('market_title') or p.get('market')} — {p.get('side')} @{p.get('entry')}, "
            f"{p.get('shares')}sh (${p.get('cost_usd') or 0:.2f}), {age_days(p.get('opened_at'))}"
        )
    if new_fills:
        lines.append(f"New fills since last report ({len(new_fills)}):")
        for f in new_fills:
            lines.append(
                f"• {f.get('side')} {f.get('liquidity')} {f.get('shares')}sh @{f.get('price')} "
                f"(${f.get('usd') or 0:.2f}) slip {f.get('slippage_vs_fair')}"
            )
    return "\n".join(lines)


def main() -> int:
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ET)
    prev = load_state()

    api_error = ""
    gov, positions, fills = {}, [], []
    try:
        gov = fetch("governor")
        positions = fetch("positions").get("positions", [])
        fills = fetch("fills?limit=20").get("fills", [])
    except Exception as e:  # noqa: BLE001 — any API failure is itself the alert
        api_error = f"{type(e).__name__}: {e}"

    fp = state_fingerprint(gov, positions, fills, api_error)
    changed = fp != prev.get("fingerprint")
    last_sent = prev.get("last_sent_utc", 0)
    hours_since_sent = (now_utc.timestamp() - last_sent) / 3600 if last_sent else 1e9

    if api_error:
        message = f"📡 Live Monitor ERROR — {now_et:%Y-%m-%d %H:%M} ET\nAPI unreachable: {api_error}"
    else:
        prev_last_fill = prev.get("last_fill_id", 0)
        new_fills = [f for f in fills if (f.get("id") or 0) > prev_last_fill]
        message = build_report(gov, positions, new_fills, now_et)

    if changed:
        reason = "state changed"
    elif hours_since_sent >= HEARTBEAT_MIN_GAP_H:
        reason = "daily heartbeat"
        if not api_error:
            message = (
                f"📡 Live Monitor: no change in {hours_since_sent:.0f}h — "
                f"{gov.get('state')}, bankroll ${gov.get('bankroll') or 0:.2f}, "
                f"{len(positions)} position(s)."
            )
    else:
        print(f"[{now_utc.isoformat()}] unchanged, no heartbeat due — silent exit")
        return 0

    sent = alert_openclaw(message)  # plain text: no parse_mode (Markdown-400 trap)
    print(f"[{now_utc.isoformat()}] {reason}; sent={sent}")
    if not sent:
        return 1  # keep old state so the next run retries

    save_state(
        {
            "fingerprint": fp,
            "last_fill_id": max((f.get("id") or 0 for f in fills), default=prev.get("last_fill_id", 0)),
            "last_sent_utc": now_utc.timestamp(),
            "last_reason": reason,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
