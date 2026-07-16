#!/usr/bin/env python3
"""
OpenClaw Alert Integration for Polyclawd
Sends trading signals and alerts via OpenClaw gateway.
"""

import json
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional

# OpenClaw Gateway (check with: openclaw gateway status)
OPENCLAW_GATEWAY = "http://localhost:18789"

DEFAULT_CHAT_ID = "468298295"  # Mr. V


def _telegram_http_send(message: str, silent: bool = False, parse_mode: str = "Markdown") -> tuple:
    """Direct Telegram Bot API send — the delivery path on hosts without the
    openclaw CLI (the VPS). Token comes from the service EnvironmentFile
    (TELEGRAM_BOT_TOKEN in /etc/default/polyclawd); never hardcoded.

    Returns (ok, err): err is "" on success, else a short machine-parseable
    failure class ("no_token", "http_<code>:...", "net:...", "tg_api:...")
    recorded in the send ledger for failure diagnosis."""
    import os
    import urllib.parse

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("[OpenClaw] no openclaw CLI and TELEGRAM_BOT_TOKEN unset — telegram alert dropped")
        return False, "no_token"
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", DEFAULT_CHAT_ID)
    fields = {
        "chat_id": chat_id,
        "text": message,
        "disable_notification": "true" if silent else "false",
    }
    if parse_mode:  # omit entirely for plain text (avoids 400 on stray _ / * in data)
        fields["parse_mode"] = parse_mode
    payload = urllib.parse.urlencode(fields).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload)
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
        if not json.loads(body).get("ok", False):
            print("[OpenClaw] telegram HTTP send returned ok=false")
            return False, f"tg_api:{body[:120]}"
        return True, ""
    except urllib.error.HTTPError as e:
        try:
            detail = e.read()[:120].decode("utf-8", "replace")
        except Exception:
            detail = ""
        print(f"[OpenClaw] telegram HTTP send failed: {e}")
        return False, f"http_{e.code}:{detail}"
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"[OpenClaw] telegram HTTP send failed: {e}")
        return False, f"net:{e}"
    except Exception as e:
        print(f"[OpenClaw] telegram HTTP send failed: {e}")
        return False, f"err:{e}"


def _ledger_log(ok: bool, channel: str, parse_mode, msg_len: int, err: str = "") -> None:
    """Append one JSON line per delivery attempt — the fleet send ledger
    (logs/telegram_sent.jsonl; consumed by scripts/send_ledger_watchdog.py).
    NEVER raises: ledger I/O must not break delivery (audit 2026-07-10)."""
    try:
        import os as _os
        import sys as _sys
        from datetime import datetime as _dt, timezone as _tz

        path = _os.environ.get("POLYCLAWD_LEDGER_PATH") or _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "logs", "telegram_sent.jsonl"
        )
        line = {
            "ts": _dt.now(_tz.utc).isoformat(timespec="seconds"),
            "caller": _os.path.basename(_sys.argv[0] or "") or "unknown",
            "channel": channel,
            "ok": bool(ok),
            "parse_mode": parse_mode,
            "len": msg_len,
        }
        if err:
            line["err"] = str(err)[:200]
        with open(path, "a") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:  # noqa: BLE001
        pass


def alert_openclaw(message: str, channel: str = "telegram", silent: bool = False, parse_mode: str = "Markdown") -> bool:
    """Ledger-wrapped sender: records every delivery attempt (with failure
    reason), then returns only the boolean result — the public signature is
    frozen (9 pipelines call it). See _alert_openclaw_inner for delivery."""
    ok, err = _alert_openclaw_inner(message, channel=channel, silent=silent, parse_mode=parse_mode)
    _ledger_log(ok, channel, parse_mode, len(message or ""), err=err)
    return ok


def _alert_openclaw_inner(
    message: str, channel: str = "telegram", silent: bool = False, parse_mode: str = "Markdown"
) -> tuple:
    """
    Send an alert via OpenClaw CLI.

    Args:
        message: The alert message to send
        channel: Target channel (telegram, discord, etc.)
        silent: If True, send without notification sound

    Returns:
        (ok, err) — err is "" on success, else a short failure class.
    """
    import subprocess

    try:
        # Target is the chat ID or @username for Telegram
        # Default to Mr. V's Telegram ID
        target = "468298295" if channel == "telegram" else channel

        cmd = [
            "openclaw",
            "message",
            "send",
            "--channel",
            channel,
            "--account",
            "polyclawd",
            "--target",
            target,
            "--message",
            message,
        ]
        if silent:
            cmd.append("--silent")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode == 0:
            return True, ""
        else:
            print(f"[OpenClaw] CLI error: {result.stderr}")
            return False, f"cli:{(result.stderr or '')[:120]}"

    except FileNotFoundError:
        if channel == "telegram":
            return _telegram_http_send(message, silent=silent, parse_mode=parse_mode)
        print("[OpenClaw] CLI not found - openclaw not in PATH")
        return False, "cli_not_found"
    except subprocess.TimeoutExpired:
        print("[OpenClaw] CLI timeout")
        return False, "cli_timeout"
    except Exception as e:
        print(f"[OpenClaw] Alert failed: {e}")
        return False, f"err:{e}"


def format_signal_alert(
    market: str, side: str, price: float, edge: float, confidence: float, source: Optional[str] = None
) -> str:
    """
    Format a trading signal as an alert message.

    Args:
        market: Market name
        side: YES or NO
        price: Current price (0-1)
        edge: Edge percentage
        confidence: Confidence score (0-100)
        source: Signal source (optional)

    Returns:
        Formatted alert string
    """
    # Emoji based on edge strength
    if edge >= 10:
        emoji = "🔥"
    elif edge >= 7:
        emoji = "🎯"
    else:
        emoji = "📊"

    msg = f"{emoji} {market[:60]}: {side} @ {price:.2f} | Edge: +{edge:.1f}% | Conf: {confidence:.0f}"

    if source:
        msg += f" | {source}"

    return msg


def alert_high_edge_signal(signal: dict, min_edge: float = 5.0) -> bool:
    """
    Check if signal meets edge threshold and send alert if so.

    Args:
        signal: Signal dict with market, side, price, edge, confidence
        min_edge: Minimum edge to trigger alert

    Returns:
        True if alert was sent, False otherwise
    """
    edge = signal.get("edge", 0)

    if edge < min_edge:
        return False

    message = format_signal_alert(
        market=signal.get("market", "Unknown"),
        side=signal.get("side", "?"),
        price=signal.get("price", 0.5),
        edge=edge,
        confidence=signal.get("confidence", 0),
        source=signal.get("source"),
    )

    return alert_openclaw(message)


def alert_rotation(exited_market: str, entered_market: str, pnl: float, ev_improvement: float) -> bool:
    """
    Send alert for position rotation.
    """
    emoji = "🔄" if pnl >= 0 else "⚠️"
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"

    message = (
        f"{emoji} Position Rotated\n"
        f"📤 Exited: {exited_market[:40]} ({pnl_str})\n"
        f"📥 Entered: {entered_market[:40]}\n"
        f"📈 EV Improvement: +{ev_improvement:.1f}%"
    )

    return alert_openclaw(message)


def alert_drawdown_halt(current_drawdown: float, halt_threshold: float) -> bool:
    """
    Send alert when drawdown halt triggers.
    """
    message = (
        f"🛑 DRAWDOWN HALT TRIGGERED\n"
        f"Current: -{current_drawdown:.1f}% | Threshold: -{halt_threshold:.1f}%\n"
        f"Trading paused until recovery."
    )

    return alert_openclaw(message)


# For command-line testing
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        msg = " ".join(sys.argv[1:])
        success = alert_openclaw(msg)
        print(f"Alert {'sent' if success else 'failed'}")
    else:
        # Test message
        test_signal = {
            "market": "Will BTC hit $100k by March?",
            "side": "YES",
            "price": 0.65,
            "edge": 7.5,
            "confidence": 72,
            "source": "whale_tracker",
        }

        print("Testing OpenClaw alert...")
        msg = format_signal_alert(**{k: v for k, v in test_signal.items()})
        print(f"Message: {msg}")

        success = alert_openclaw(msg)
        print(f"Result: {'✅ Sent' if success else '❌ Failed'}")
