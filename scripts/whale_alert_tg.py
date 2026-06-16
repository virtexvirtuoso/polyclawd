#!/usr/bin/env python3
"""
Whale alert Telegram notifier — sends actionable CRITICAL/HIGH signals.
Runs inline after every whale scanner cycle (~5 min).

Dedup logic:
  - Same market suppressed for 4h UNLESS:
    a) Score jumped >0.15 since last send (escalation)
    b) HTR < 2h (closing soon — urgency bypass)
  - CLOB×scanner fusion: if whale_clob fired on same market within 15 min,
    header becomes "DOUBLE CONFIRMATION"
"""
import requests, json, os, time, re

API = "http://127.0.0.1:8420/api"
TG_BOT = "8281304606:AAHZRF9Oef5Ys5cG5bT_cs2vpjTfPlk2BkM"
TG_CHAT = "468298295"
STATE_FILE = "/tmp/whale_alert_tg_state.json"
CLOB_LAST_FILE = "/tmp/whale_clob_last.json"

MIN_SCORE = 0.48
MIN_HTR = 0.5         # hours — skip only if resolves in < 30 min
MAX_HTR = 72
MIN_FLOW = 5000
MIN_WALLET_WR = 0.45
MIN_WALLET_N = 5
DEDUP_WINDOW = 4 * 3600   # 4h standard
SCORE_ESCALATION_DELTA = 0.15  # re-alert if score jumps this much
HTR_URGENCY_THRESHOLD = 2.0    # bypass dedup if <2h to resolve
CLOB_FUSION_WINDOW = 15 * 60   # 15 min — CLOB×scanner fusion window


def load_state():
    """State: {market: {ts, score}} — tracks last send time + score."""
    try:
        with open(STATE_FILE) as f:
            raw = json.load(f)
        # Migrate old format {sent: {market: ts}} → new format
        if "sent" in raw:
            migrated = {}
            for mkt, val in raw["sent"].items():
                migrated[mkt] = {"ts": val, "score": 0.0}
            return migrated
        return raw
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def load_clob_fired() -> set:
    """Return set of markets that whale_clob fired on within fusion window."""
    try:
        with open(CLOB_LAST_FILE) as f:
            data = json.load(f)
        if time.time() - data.get("ts", 0) > CLOB_FUSION_WINDOW:
            return set()
        return {e["market"] for e in data.get("fired", [])}
    except Exception:
        return set()


def send_tg(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
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
    wallet_n = alert.get("wallet_n")
    if wr is not None and wallet_n is not None and wallet_n >= MIN_WALLET_N:
        if wr < MIN_WALLET_WR:
            return False
    return True


def should_send(alert, state, now) -> tuple[bool, str]:
    """Returns (send, reason). reason is 'new'|'escalation'|'urgency'|'suppressed'."""
    mkt = alert.get("market", "")
    score = alert.get("score", 0)
    htr = alert.get("hours_to_resolve")
    prev = state.get(mkt)

    if prev is None:
        return True, "new"

    # HTR urgency bypass — closing soon, always re-alert
    if htr is not None and htr < HTR_URGENCY_THRESHOLD:
        if now - prev["ts"] > 1800:  # but at most every 30 min even for urgent
            return True, "urgency"

    # Score escalation bypass
    prev_score = prev.get("score", 0)
    if score - prev_score >= SCORE_ESCALATION_DELTA:
        return True, "escalation"

    # Standard dedup window
    if now - prev["ts"] > DEDUP_WINDOW:
        return True, "new"

    return False, "suppressed"


def _clean_market_name(mkt: str) -> str:
    cleaned = re.sub(r'-[a-f0-9]{8,}$', '', mkt)
    cleaned = cleaned.replace('-', ' ').title()
    if len(cleaned) > 60:
        cleaned = cleaned[:57] + '...'
    return cleaned


def format_alert(alert, rank, send_reason: str, clob_match: bool) -> str:
    platform = "PM" if alert.get("platform") == "polymarket" else "KX"
    mkt = alert.get("market", "")
    title = alert.get("title") or _clean_market_name(mkt)
    if len(title) > 65:
        title = title[:62] + "..."

    score = alert.get("score", 0)
    prev_score = alert.get("_prev_score", 0)
    flow = alert.get("flow_dollars", 0)
    htr = alert.get("hours_to_resolve")
    wr = alert.get("wallet_win_rate")
    price = alert.get("price")
    sev = alert.get("severity", "")
    url = alert.get("url", "")
    direction = alert.get("direction")  # +1=YES, -1=NO, None=ambiguous

    # ── Header line ──────────────────────────────────────────────────
    tags = []
    if htr is not None and htr < HTR_URGENCY_THRESHOLD:
        tags.append("🔴 CLOSING SOON")
    if send_reason == "escalation":
        tags.append("⬆️ ESCALATING")
    if clob_match:
        tags.append("🦈 DOUBLE CONF")

    tag_str = " · ".join(tags) + "\n" if tags else ""

    # Direction label
    if direction == 1:
        dir_str = "BET YES"
    elif direction == -1:
        dir_str = "BET NO"
    else:
        dir_str = ""

    # ── Score line ────────────────────────────────────────────────────
    score_str = f"{score:.3f}"
    if send_reason == "escalation" and prev_score:
        delta = score - prev_score
        score_str += f" <b>(+{delta:.2f}↑)</b>"

    # ── Detail line ───────────────────────────────────────────────────
    details = []
    if dir_str:
        details.append(f"<b>{dir_str}</b>")
    if price is not None:
        details.append(f"@{price:.2f}")
    if htr is not None:
        details.append(f"{htr:.1f}h left")
    else:
        details.append("open")
    if flow:
        details.append(f"${flow:,.0f} flow")
    if wr is not None:
        details.append(f"WR {wr:.0%}")

    lines = [
        f"{tag_str}<b>#{rank} [{platform}] {sev}</b>",
        title,
        " · ".join(details),
        f"Score: {score_str}",
        f"<a href='{url}'>Open</a>",
    ]
    return "\n".join(lines)


def main():
    state = load_state()
    clob_fired = load_clob_fired()
    alerts = get_top_alerts()

    if not alerts:
        print("No alerts returned")
        return

    actionable = [a for a in alerts if is_actionable(a)]
    print(f"Total alerts: {len(alerts)}, actionable: {len(actionable)}")

    now = time.time()
    to_send = []
    for a in actionable:
        mkt = a.get("market", "")
        ok, reason = should_send(a, state, now)
        if ok:
            prev = state.get(mkt, {})
            a["_prev_score"] = prev.get("score", 0)
            a["_send_reason"] = reason
            to_send.append(a)

    if not to_send:
        print("No new actionable alerts (all deduplicated)")
        return

    # ── Build message ─────────────────────────────────────────────────
    n = len(to_send)
    double_conf = [a for a in to_send if a.get("market", "") in clob_fired]

    if double_conf:
        header = f"🦈 <b>DOUBLE CONFIRMATION — {len(double_conf)} market(s) confirmed by CLOB + scanner</b>"
    else:
        header = f"🎯 <b>WHALE ALERT — {n} signal(s)</b>"

    lines = [header, ""]
    for i, a in enumerate(to_send[:8], 1):
        clob_match = a.get("market", "") in clob_fired
        lines.append(format_alert(a, i, a["_send_reason"], clob_match))
        lines.append("")

    lines.append("<i>CRITICAL precision: 65.6% | sports: 35.7% (Kalshi, N=411)</i>")
    msg = "\n".join(lines)

    ok = send_tg(msg)
    if ok:
        for a in to_send:
            state[a.get("market", "")] = {"ts": now, "score": a.get("score", 0)}
        save_state(state)
        print(f"Sent {len(to_send)} alert(s)")
    else:
        print("TG send failed")


if __name__ == "__main__":
    main()
