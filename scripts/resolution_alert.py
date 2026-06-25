#!/usr/bin/env python3
"""Resolution-timing alert — LLM-free replacement for the OpenClaw agent cron.

Curls the live /api/resolution/imminent endpoint (HIGH-uncertainty markets
resolving within 24h) and, if any are returned, pushes a formatted alert
straight to Telegram via the Bot API. No alerts -> exits silently.

Run from VPS cron:
    set -a && . ~/.config/polyclawd/alerts.env && set +a
    venv/bin/python3 scripts/resolution_alert.py --send
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
API = "https://virtuosocrypto.com/polyclawd/api/resolution/imminent"
MAX_FULL = 8


def fetch():
    req = urllib.request.Request(API, headers={"User-Agent": "polyclawd-cron/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="push to Telegram (Bot API, no LLM)")
    args = parser.parse_args()

    try:
        data = fetch()
    except Exception as e:
        print(f"ERROR: resolution fetch failed: {e}")
        return

    markets = data.get("markets") or []
    if not markets:
        print("NO_NEW_RESOLUTION")
        return

    out = [f"⏳ RESOLUTION TIMING -- {len(markets)} market(s) resolving <24h"]
    for m in markets[:MAX_FULL]:
        price = m.get("yes_price", 0.5)
        out.append(
            f"• {str(m.get('title', 'Unknown'))[:80]}\n"
            f"  YES {price * 100:.0f}¢ | {m.get('hours_until_resolution', '?')}h left | "
            f"vol24h ${float(m.get('volume_24h', 0)):,.0f} | unc {m.get('uncertainty_score', '?')}\n"
            f"  {m.get('url', '')}"
        )
    if len(markets) > MAX_FULL:
        out.append(f"(+{len(markets) - MAX_FULL} more)")

    text = "\n".join(out)
    print(text)

    if args.send:
        try:
            sys.path.insert(0, str(BASE))
            from scripts.openclaw_alerts import alert_openclaw

            ok = alert_openclaw(text, parse_mode=None)
            print(f"[send] telegram ok={ok}")
        except Exception as e:
            print(f"[send] failed: {e}")


if __name__ == "__main__":
    main()
