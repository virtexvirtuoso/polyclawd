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


def alert_openclaw(
    message: str,
    channel: str = "telegram",
    silent: bool = False
) -> bool:
    """
    Send an alert via OpenClaw CLI.
    
    Args:
        message: The alert message to send
        channel: Target channel (telegram, discord, etc.)
        silent: If True, send without notification sound
    
    Returns:
        True if successful, False otherwise
    """
    import subprocess
    
    try:
        # Target is the chat ID or @username for Telegram
        # Default to Mr. V's Telegram ID
        target = "468298295" if channel == "telegram" else channel
        
        cmd = ["openclaw", "message", "send", 
               "--channel", channel,
               "--target", target,
               "--message", message]
        if silent:
            cmd.append("--silent")
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0:
            return True
        else:
            print(f"[OpenClaw] CLI error: {result.stderr}")
            return False
            
    except FileNotFoundError:
        print("[OpenClaw] CLI not found - openclaw not in PATH")
        return False
    except subprocess.TimeoutExpired:
        print("[OpenClaw] CLI timeout")
        return False
    except Exception as e:
        print(f"[OpenClaw] Alert failed: {e}")
        return False


def format_signal_alert(
    market: str,
    side: str,
    price: float,
    edge: float,
    confidence: float,
    source: Optional[str] = None
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
        source=signal.get("source")
    )
    
    return alert_openclaw(message)


def alert_rotation(
    exited_market: str,
    entered_market: str,
    pnl: float,
    ev_improvement: float
) -> bool:
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
            "source": "whale_tracker"
        }
        
        print("Testing OpenClaw alert...")
        msg = format_signal_alert(**{k: v for k, v in test_signal.items()})
        print(f"Message: {msg}")
        
        success = alert_openclaw(msg)
        print(f"Result: {'✅ Sent' if success else '❌ Failed'}")
