#!/usr/bin/env python3
"""
Whale alert daily digest — fetches top ranked alerts and formats for Telegram.
Run manually or from cron for morning signal check.
"""
import requests, json, sys

API = "http://127.0.0.1:8420/api"

def get_top_alerts(limit=10, platform=None, min_hours=0.5):
    params = {"limit": limit}
    if platform:
        params["platform"] = platform
    r = requests.get(f"{API}/whale/top", params=params, timeout=10)
    alerts = r.json().get("alerts", [])
    # Filter out already-expired and low-quality
    return [a for a in alerts if (a.get("hours_to_resolve") or 999) > min_hours]

def get_precision():
    r = requests.get(f"{API}/whale/precision", timeout=10)
    return r.json()

def fmt_alert(a, rank):
    score = a["composite_score"] if "composite_score" in a else a.get("score", 0)
    flow = a.get("flow_dollars", 0)
    wr = a.get("wallet_win_rate")
    spread = a.get("spread_bps")
    htr = a.get("hours_to_resolve")
    arch = a.get("archetype") or a.get("market_archetype") or "?"
    direction = a.get("direction")
    price = a.get("price")
    
    # Market label
    mkt = a.get("market", "")
    if len(mkt) > 40:
        mkt = mkt[:38] + ".."
    platform = a.get("platform", "")
    
    dir_str = ""
    if direction == 1 and price:
        dir_str = f"→ YES @ {price:.2f}"
    elif direction == -1 and price:
        dir_str = f"→ NO @ {1-price:.2f}"
    
    htr_str = f"{htr:.1f}h" if htr and htr > 0 else "open"
    wr_str = f"{wr:.0%}" if wr is not None else "?"
    spread_str = f"{spread:.0f}bp" if spread else "?"
    
    platform_tag = "PM" if platform == "polymarket" else "KX"
    
    lines = [
        f"#{rank} [{platform_tag}] {mkt}",
        f"  score={score:.3f} | flow=${flow:,.0f} | {dir_str}",
        f"  wallet_wr={wr_str} | spread={spread_str} | {arch} | {htr_str}",
    ]
    if a.get("wallet_name"):
        lines.append(f"  wallet: {a['wallet_name']}")
    lines.append(f"  {a.get('url', '')}")
    return "\n".join(lines)

def main():
    print("=== WHALE DIGEST ===\n")
    
    # All platforms
    alerts = get_top_alerts(limit=10)
    if not alerts:
        print("No open alerts.")
        return
    
    print(f"TOP {len(alerts)} WHALE ALERTS (ranked by composite score)\n")
    for i, a in enumerate(alerts, 1):
        print(fmt_alert(a, i))
        print()
    
    # Precision summary
    precision = get_precision()
    print("=== PRECISION SUMMARY ===")
    print("\nBy archetype (Kalshi, N=411):")
    for row in precision.get("by_archetype", []):
        if row.get("resolved", 0) >= 5:
            prec = row.get("precision")
            prec_str = f"{prec:.1%}" if prec is not None else "n/a"
            print(f"  {row.get("name") or row.get("market_archetype") or row.get("severity") or row.get("platform") or "?":<15} {row['total']:>5} alerts, {row['resolved']:>3} resolved → {prec_str}")
    
    print("\nBy severity:")
    for row in precision.get("by_severity", []):
        if row.get("resolved", 0) > 0:
            prec = row.get("precision")
            prec_str = f"{prec:.1%}" if prec is not None else "n/a"
            print(f"  {row.get("name") or row.get("market_archetype") or row.get("severity") or row.get("platform") or "?":<12} {row['total']:>6} alerts, {row['resolved']:>3} resolved → {prec_str}")

if __name__ == "__main__":
    main()
