#!/usr/bin/env python3
"""
Whale alert Telegram notifier — sends actionable CRITICAL/HIGH signals.
Designed to run every 30 minutes via cron. Deduplicates via state file.
"""
import requests, json, os, time, sys

API = "http://127.0.0.1:8420/api"
TG_BOT = "8281304606:AAHZRF9Oef5Ys5cG5bT_cs2vpjTfPlk2BkM"
TG_CHAT = "468298295"
STATE_FILE = "/tmp/whale_alert_tg_state.json"

# Thresholds for sending
MIN_SCORE = 0.48
MIN_HTR = 1.0         # hours — skip if resolves in < 1h (not actionable)
MAX_HTR = 72          # hours — only alert if resolves within 72h
MIN_FLOW = 5000       # minimum $5K flow to alert
MIN_WALLET_WR = 0.45  # wallet win rate threshold (or unknown)
# Extreme wallet WR (1.0 or 0.0) almost certainly means n=1 resolved trade — treat as unknown
EXTREME_WR_LO = 0.01
EXTREME_WR_HI = 0.99

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"sent": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def send_tg(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10
        )
        return r.json().get("ok", False)
    except Exception as e:
        print(f"TG error: {e}")
        return False

def get_top_alerts():
    try:
        r = requests.get(f"{API}/whale/top", params={"limit": 20, "severity": "CRITICAL"}, timeout=10)
        alerts = r.json().get("alerts", [])
    except Exception:
        return []
    
    # Also get HIGH alerts with very high scores
    try:
        r2 = requests.get(f"{API}/whale/top", params={"limit": 10, "severity": "HIGH"}, timeout=10)
        highs = [a for a in r2.json().get("alerts", []) if a.get("score", 0) >= 0.65]
        alerts += highs
    except Exception:
        pass
    
    return alerts

def is_actionable(alert):
    score = alert.get("score", 0)
    htr = alert.get("hours_to_resolve")
    flow = alert.get("flow_dollars", 0)
    wr = alert.get("wallet_win_rate")

    if score < MIN_SCORE:
        return False
    if htr is not None and (htr < MIN_HTR or htr > MAX_HTR):
        return False
    if flow < MIN_FLOW:
        return False
    # Treat extreme wallet WR (1.0 / 0.0) as unknown — almost certainly n=1
    if wr is not None and EXTREME_WR_LO < wr < EXTREME_WR_HI:
        if wr < MIN_WALLET_WR:
            return False  # Known bad wallet with sufficient sample
    return True

def format_alert(alert, rank):
    platform = "PM" if alert.get("platform") == "polymarket" else "KX"
    mkt = alert.get("market", "")
    mkt_short = mkt[:45] + ".." if len(mkt) > 47 else mkt
    score = alert.get("score", 0)
    flow = alert.get("flow_dollars", 0)
    htr = alert.get("hours_to_resolve")
    wr = alert.get("wallet_win_rate")
    spread = alert.get("spread_bps")
    arch = (alert.get("archetype") or "?")[:10]
    price = alert.get("price")
    sev = alert.get("severity", "")
    url = alert.get("url", "")
    
    wr_str = f"{wr:.0%}" if wr is not None else "?"
    spread_str = f"{spread:.0f}bp" if spread else "?"
    htr_str = f"{htr:.1f}h" if htr else "open"
    price_str = f"@ {price:.2f}" if price else ""
    
    lines = [
        f"<b>#{rank} [{platform}] {sev} — {mkt_short}</b>",
        f"Score: {score:.3f} | Flow: ${flow:,.0f} {price_str}",
        f"Wallet WR: {wr_str} | Spread: {spread_str} | {arch} | {htr_str}",
        f"<a href='{url}'>→ Open market</a>",
    ]
    return "\n".join(lines)

def main():
    state = load_state()
    alerts = get_top_alerts()
    
    if not alerts:
        print("No alerts returned")
        return
    
    actionable = [a for a in alerts if is_actionable(a)]
    print(f"Total alerts: {len(alerts)}, actionable: {len(actionable)}")
    
    # Deduplicate — don't resend same market within 4 hours
    now = time.time()
    to_send = []
    for a in actionable:
        mkt = a.get("market", "")
        last_sent = state["sent"].get(mkt, 0)
        if now - last_sent > 4 * 3600:
            to_send.append(a)
    
    if not to_send:
        print("No new actionable alerts (all deduplicated)")
        return
    
    # Build message
    lines = [f"🎯 <b>WHALE ALERT — {len(to_send)} signal(s)</b>", ""]
    for i, a in enumerate(to_send[:5], 1):  # max 5 per message
        lines.append(format_alert(a, i))
        lines.append("")
    
    # Precision context
    lines.append("<i>Precision: weather=72.7% | CRITICAL=65.6% | sports=35.7% (Kalshi, N=411)</i>")
    lines.append("<i>Weight: flow 30% + wallet 25% + spread 15% + urgency 15% + archetype 15%</i>")
    
    msg = "\n".join(lines)
    
    ok = send_tg(msg)
    if ok:
        # Update state
        for a in to_send:
            state["sent"][a.get("market", "")] = now
        save_state(state)
        print(f"Sent {len(to_send)} alert(s)")
    else:
        print("TG send failed")

if __name__ == "__main__":
    main()
