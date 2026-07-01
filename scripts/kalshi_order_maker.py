#!/usr/bin/env python3
"""
kalshi_order_maker.py — Post limit orders on illiquid Kalshi markets.

Strategy: scan Hit/HR/K markets with wide bid-ask spreads and post limit
orders at mid-price. Maker orders are fee-free on Kalshi.

Safety rails:
  - Max $MAX_ORDER_SIZE per contract fill
  - Max $MAX_DAILY_EXPOSURE total notional across all open maker orders
  - Only post when spread >= MIN_SPREAD_C cents (ensures maker edge)
  - DRY_RUN=True by default — set --execute to actually place orders

Usage:
  python3 scripts/kalshi_order_maker.py --dry-run          (default, safe)
  python3 scripts/kalshi_order_maker.py --execute           (places real orders)
  python3 scripts/kalshi_order_maker.py --cancel-all        (cancel all open orders)

Cron (every 30 min during US market hours):
  */30 13-23 * * * cd /var/.../polyclawd && python3 scripts/kalshi_order_maker.py --execute
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from services.kalshi_auth import sign_request, kalshi_get
from scripts.alert_formatter import send_telegram

# ── Config ─────────────────────────────────────────────────────────────────
KALSHI_BASE     = "https://api.elections.kalshi.com"
MIN_SPREAD_C    = 4     # cents — minimum spread to bother posting
MAX_ORDER_NOTL  = 25    # dollars max notional per order (count × price)
MAX_DAILY_NOTL  = 200   # dollars max total open maker exposure
MIN_DEPTH_SZ    = 0     # don't require existing depth (we're making it)
SERIES_TARGETS  = ["KXMLBHIT", "KXMLBHR", "KXMLBKS"]  # prop series to scan
ORDER_INTERVAL_S = 0.3  # rate limit between order placements
LOG_PATH        = BASE_DIR / "storage" / "maker_orders.jsonl"


def kalshi_post(path: str, body: dict) -> dict:
    headers = sign_request("POST", path)
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        KALSHI_BASE + path, data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode()[:300]}


def kalshi_delete(path: str) -> dict:
    headers = sign_request("DELETE", path)
    req = urllib.request.Request(
        KALSHI_BASE + path, headers=headers, method="DELETE"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode()[:200]}


def get_open_orders() -> List[dict]:
    """Fetch all resting maker orders."""
    try:
        data = kalshi_get("/trade-api/v2/portfolio/orders?status=resting&limit=200")
        return data.get("orders", [])
    except Exception as e:
        print(f"[maker] get_open_orders: {e}")
        return []


def cancel_all_orders() -> int:
    """Cancel all open maker orders. Returns count cancelled."""
    orders = get_open_orders()
    cancelled = 0
    for o in orders:
        oid = o.get("order_id", "")
        if not oid:
            continue
        path = f"/trade-api/v2/portfolio/orders/{oid}"
        result = kalshi_delete(path)
        if "error" not in result:
            cancelled += 1
            print(f"[maker] Cancelled {oid} ({o.get('ticker','')})")
        time.sleep(0.2)
    return cancelled


def scan_targets() -> List[dict]:
    """Scan SERIES_TARGETS for markets with wide spreads worth making on."""
    candidates = []
    for series in SERIES_TARGETS:
        try:
            data = kalshi_get(
                f"/trade-api/v2/markets?limit=200&status=open&series_ticker={series}"
            )
            markets = data.get("markets", [])
        except Exception as e:
            print(f"[maker] scan {series}: {e}")
            continue

        for m in markets:
            ticker = m.get("ticker", "")
            bid = float(m.get("yes_bid_dollars", 0))
            ask = float(m.get("yes_ask_dollars", 0))
            if ask <= 0:
                continue  # no ask = no tradeable market
            spread_c = round((ask - bid) * 100, 1)
            mid = (bid + ask) / 2
            if spread_c < MIN_SPREAD_C:
                continue
            if mid <= 0.02 or mid >= 0.98:
                continue  # avoid extreme-prob markets
            title = m.get("title", "")[:60]
            candidates.append({
                "ticker": ticker,
                "series": series,
                "title": title,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread_c": spread_c,
            })

    # Sort by spread descending (most illiquid = most maker edge)
    candidates.sort(key=lambda x: -x["spread_c"])
    return candidates


def compute_order(m: dict, side: str = "bid") -> Optional[dict]:
    """
    Compute a limit order at mid-price for a market.
    side='bid' = buy YES at mid  (we think YES is ≥ mid)
    side='ask' = sell YES at mid (we think NO is ≥ 1-mid)
    
    Returns order dict or None if outside safe bounds.
    """
    mid = m["mid"]
    # Post 1 tick inside the mid to improve fill priority
    tick = 0.01  # Kalshi linear_cent markets step in $0.01
    if side == "bid":
        price = round(mid - tick / 2, 2)
        price = max(0.02, price)
    else:
        price = round(mid + tick / 2, 2)
        price = min(0.98, price)

    # Size: target ~$10 notional per order (count × price)
    count = max(1.0, round(10.0 / price, 0))
    notional = count * price
    if notional > MAX_ORDER_NOTL:
        count = int(MAX_ORDER_NOTL / price)
    if count < 1:
        return None

    return {
        "ticker": m["ticker"],
        "client_order_id": str(uuid.uuid4()),
        "side": side,
        "count": f"{count:.2f}",
        "price": f"{price:.4f}",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": True,
    }


def _log_order(order_body: dict, result: dict) -> None:
    entry = {
        "ts": time.time(),
        "order": order_body,
        "result": result,
    }
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def place_orders(candidates: List[dict], dry_run: bool = True) -> List[dict]:
    """Place bid+ask limit orders on top candidates. Returns placed order list."""
    open_orders = get_open_orders()
    already_posted = {o.get("ticker") for o in open_orders}
    open_notional = sum(
        float(o.get("remaining_count", 0)) * float(o.get("price", 0))
        for o in open_orders
    )

    if open_notional >= MAX_DAILY_NOTL:
        print(f"[maker] Open notional ${open_notional:.0f} >= limit ${MAX_DAILY_NOTL}. Skipping.")
        return []

    placed = []
    for m in candidates:
        if m["ticker"] in already_posted:
            continue
        remaining_budget = MAX_DAILY_NOTL - open_notional

        for side in ["bid", "ask"]:
            order = compute_order(m, side)
            if not order:
                continue
            notional = float(order["count"]) * float(order["price"])
            if notional > remaining_budget:
                continue

            print(
                f"[maker] {'DRY ' if dry_run else ''}POST {side.upper()} "
                f"{order['count']} × {m['ticker']} @ {float(order['price']):.2f}  "
                f"(spread={m['spread_c']}¢  notional=${notional:.1f})"
            )

            if not dry_run:
                result = kalshi_post("/trade-api/v2/portfolio/events/orders", order)
                _log_order(order, result)
                if "error" in result:
                    print(f"[maker] Error: {result}")
                else:
                    open_notional += notional
                    placed.append({"order": order, "result": result})
                    time.sleep(ORDER_INTERVAL_S)
            else:
                placed.append({"order": order, "result": {"dry_run": True}})

    return placed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="Actually place orders")
    ap.add_argument("--cancel-all", action="store_true", help="Cancel all open maker orders")
    ap.add_argument("--report", action="store_true", help="Show open orders + P&L snapshot")
    args = ap.parse_args()

    if args.cancel_all:
        n = cancel_all_orders()
        print(f"[maker] Cancelled {n} orders.")
        return

    if args.report:
        orders = get_open_orders()
        print(f"Open maker orders: {len(orders)}")
        total_notl = 0.0
        for o in orders:
            notl = float(o.get("remaining_count", 0)) * float(o.get("price", 0))
            total_notl += notl
            print(f"  {o.get('ticker','?'):50s} {o.get('side','?')} @ {o.get('price','?')}  notl=${notl:.1f}")
        print(f"Total open notional: ${total_notl:.1f}")
        return

    dry_run = not args.execute

    print(f"[maker] Scanning {SERIES_TARGETS} for wide-spread markets...")
    candidates = scan_targets()
    print(f"[maker] {len(candidates)} candidates (MIN_SPREAD={MIN_SPREAD_C}¢)")

    if not candidates:
        print("[maker] Nothing to post.")
        return

    # Show top 10
    print("\nTop candidates:")
    for m in candidates[:10]:
        print(f"  {m['ticker'][:50]:50s} spread={m['spread_c']}¢  mid={m['mid']:.2f}  {m['title']}")

    placed = place_orders(candidates[:20], dry_run=dry_run)
    print(f"\n[maker] {'Would place' if dry_run else 'Placed'} {len(placed)} order(s).")

    if placed and not dry_run:
        msg = (
            f"🤖 <b>KALSHI MAKER BOT</b>\n\n"
            f"Posted {len(placed)} limit orders\n"
            f"Markets: {', '.join(set(p['order']['ticker'].split('-')[0][:10] for p in placed[:5]))}\n"
            f"Series: {', '.join(SERIES_TARGETS)}"
        )
        send_telegram(msg)


if __name__ == "__main__":
    main()
