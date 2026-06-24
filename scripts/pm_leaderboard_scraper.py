#!/usr/bin/env python3
"""
pm_leaderboard_scraper.py — Polymarket leaderboard discovery + smart wallet seeding.

Two discovery paths:
  1. Scrape PM leaderboard page (top 100 by volume) — seeds new whales into pm_wallets
  2. Scrape PM leaderboard by profit (top 100 by PnL) — catches profitable traders

Also fires Telegram alerts when:
  - A new wallet is discovered on the leaderboard (not in pm_wallets)
  - A wallet graduates to smart status during refresh

Cron: every 6 hours (scheduler tick_6h) — leaderboard doesn't change fast.
State: storage/whale_meta.db (pm_wallets table)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from scripts.alert_formatter import send_telegram

META_DB_PATH = BASE_DIR / "storage" / "whale_meta.db"


# ── DB ────────────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(META_DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


# ── Leaderboard scraping ─────────────────────────────────────────────────────
def scrape_leaderboard(path: str = "/leaderboard") -> List[Dict]:
    """Scrape PM leaderboard page and extract wallet data from __NEXT_DATA__."""
    url = f"https://polymarket.com{path}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    })
    try:
        r = urllib.request.urlopen(req, timeout=30)
        text = r.read().decode()
    except Exception as e:
        print(f"[leaderboard] Failed to fetch {url}: {e}", flush=True)
        return []

    # Extract __NEXT_DATA__ JSON
    nd = re.search(r'__NEXT_DATA__[^{]*(\{.*?\})\s*</script>', text, re.DOTALL)
    if not nd:
        print("[leaderboard] No __NEXT_DATA__ found", flush=True)
        return []

    try:
        j = json.loads(nd.group(1))
    except Exception as e:
        print(f"[leaderboard] JSON parse error: {e}", flush=True)
        return []

    queries = j.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", [])

    entries = []
    for q in queries:
        qdata = q.get("state", {}).get("data", {})
        if not isinstance(qdata, list) or not qdata:
            continue
        first = qdata[0]
        if not isinstance(first, dict):
            continue
        if "pnl" in first and "proxyWallet" in first:
            for entry in qdata:
                wallet = entry.get("proxyWallet", "")
                if not wallet:
                    continue
                entries.append({
                    "wallet": wallet.lower(),
                    "name": entry.get("name", "") or entry.get("pseudonym", ""),
                    "pnl": float(entry.get("pnl", 0) or 0),
                    "volume": float(entry.get("amount", 0) or entry.get("volume", 0) or 0),
                    "rank": entry.get("rank", 0),
                })
            break

    return entries


def scrape_profile_pnl(username: str) -> Optional[Dict]:
    """Scrape a PM profile page for PnL + wallet data."""
    url = f"https://polymarket.com/@{username}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    })
    try:
        r = urllib.request.urlopen(req, timeout=15)
        text = r.read().decode()
    except Exception as e:
        print(f"[leaderboard] Failed to fetch profile @{username}: {e}", flush=True)
        return None

    nd = re.search(r'__NEXT_DATA__[^{]*(\{.*?\})\s*</script>', text, re.DOTALL)
    if not nd:
        return None

    try:
        j = json.loads(nd.group(1))
    except Exception:
        return None

    queries = j.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
    for q in queries:
        qdata = q.get("state", {}).get("data", {})
        if isinstance(qdata, dict) and "pnl" in qdata:
            return {
                "wallet": (qdata.get("proxyWallet", "") or "").lower(),
                "name": qdata.get("name", ""),
                "pnl": float(qdata.get("pnl", 0) or 0),
                "volume": float(qdata.get("amount", 0) or 0),
            }

    return None


# ── Seeding ───────────────────────────────────────────────────────────────────
def seed_wallets(conn: sqlite3.Connection, entries: List[Dict]) -> Dict:
    """Insert new leaderboard wallets into pm_wallets. Returns stats."""
    now = time.time()
    new_wallets = []
    updated = 0

    for entry in entries:
        wallet = entry["wallet"]
        name = entry["name"]
        pnl = entry["pnl"]
        volume = entry["volume"]

        existing = conn.execute(
            "SELECT wallet, name, smart FROM pm_wallets WHERE wallet=?", (wallet,)
        ).fetchone()

        if existing is None:
            # New wallet — insert with leaderboard data
            conn.execute(
                "INSERT INTO pm_wallets (wallet, name, first_seen, last_seen,"
                " closed_positions, wins, win_rate, realized_pnl, net_pnl,"
                " zombies, concentration, smart, refreshed)"
                " VALUES (?,?,?,?, 0,0,NULL,0,?, 0,0,0,0)",
                (wallet, name, now, now, pnl)
            )
            new_wallets.append({"name": name, "pnl": pnl, "volume": volume})
        else:
            # Update name if blank
            if not existing["name"] and name:
                conn.execute("UPDATE pm_wallets SET name=? WHERE wallet=?", (name, wallet))
            updated += 1

    conn.commit()
    return {"new": new_wallets, "updated": updated}


# ── Alerts ────────────────────────────────────────────────────────────────────
def alert_new_discoveries(new_wallets: List[Dict]) -> None:
    """Fire Telegram alert for newly discovered leaderboard wallets."""
    if not new_wallets:
        return

    # Only alert for significant wallets (>$100k volume or >$10k PnL)
    significant = [w for w in new_wallets
                   if abs(w["pnl"]) > 10_000 or w["volume"] > 100_000]
    if not significant:
        return

    top = sorted(significant, key=lambda x: x["pnl"], reverse=True)[:10]
    lines = [
        f"🔭 <b>NEW LEADERBOARD WALLETS</b>",
        f"<i>{len(significant)} high-value wallets discovered</i>",
        "",
    ]
    for w in top:
        raw_name = w["name"] or w["wallet"]
        if raw_name.startswith("0x") and len(raw_name) > 12:
            addr = raw_name.split("-")[0]
            name = f"{addr[:6]}…{addr[-4:]}"
        else:
            name = raw_name
        pnl_tag = f"📈 <b>+${w['pnl']:,.0f}</b>" if w["pnl"] > 0 else f"📉 -${abs(w['pnl']):,.0f}"
        lines.append(f"<b>{name}</b>  {pnl_tag}")
        lines.append(f"   Vol ${w['volume']:,.0f} · queued for evaluation")
    lines.append("")
    lines.append("⏳ Will promote to smart-wallet tier if they meet win-rate criteria.")

    send_telegram("\n".join(lines))


def alert_graduations(conn: sqlite3.Connection) -> int:
    """Check for wallets that meet smart criteria but aren't flagged. Promote + alert."""
    rows = conn.execute("""
        SELECT wallet, name, closed_positions, ROUND(win_rate*100,1) as wr_pct,
               ROUND(net_pnl,0) as net
        FROM pm_wallets
        WHERE smart = 0
          AND closed_positions >= 20
          AND win_rate >= 0.60
          AND net_pnl >= 5000
        ORDER BY net_pnl DESC
        LIMIT 20
    """).fetchall()

    if not rows:
        return 0

    # Promote
    wallets_to_promote = [r["wallet"] for r in rows]
    conn.execute(
        f"UPDATE pm_wallets SET smart=1 WHERE wallet IN ({','.join('?' * len(wallets_to_promote))})",
        wallets_to_promote
    )
    conn.commit()

    # Alert (only top 5)
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    lines = [
        f"🎓 <b>SMART WALLET GRADUATION</b>",
        f"<i>{len(rows)} wallets promoted to signal-boost tier</i>",
        "",
    ]
    for i, r in enumerate(rows[:5]):
        raw_name = r["name"] or r["wallet"]
        # Shorten ETH addresses / long hex IDs used as PM display names
        if raw_name.startswith("0x") and len(raw_name) > 12:
            addr = raw_name.split("-")[0]  # strip -timestamp suffix
            name = f"{addr[:6]}…{addr[-4:]}"
        else:
            name = raw_name
        medal = medals[i]
        lines.append(f"{medal} <b>{name}</b>")
        lines.append(f"   {r['wr_pct']}% WR · {r['closed_positions']} trades · <b>${r['net']:+,.0f}</b>")
    if len(rows) > 5:
        lines.append(f"\n<i>+{len(rows) - 5} more promoted</i>")
    lines.append("")
    lines.append("✅ Signal scores will now be boosted when these wallets enter markets.")

    send_telegram("\n".join(lines))
    return len(rows)


# ── Main ──────────────────────────────────────────────────────────────────────
def run() -> Dict:
    conn = get_db()

    # Scrape both volume and profit leaderboards
    volume_entries = scrape_leaderboard("/leaderboard")
    print(f"[leaderboard] Volume leaderboard: {len(volume_entries)} entries", flush=True)

    # Profit leaderboard (same page, different sort — PM uses client-side sort)
    # The __NEXT_DATA__ contains the same dataset, so volume scrape covers both

    # Seed new wallets
    result = seed_wallets(conn, volume_entries)
    print(f"[leaderboard] New: {len(result['new'])}, Updated: {result['updated']}", flush=True)

    # Alert on new discoveries
    alert_new_discoveries(result["new"])

    # Check for graduation candidates
    graduated = alert_graduations(conn)
    print(f"[leaderboard] Graduated: {graduated}", flush=True)

    conn.close()
    return {
        "scraped": len(volume_entries),
        "new": len(result["new"]),
        "graduated": graduated,
    }


if __name__ == "__main__":
    result = run()
    print(f"\nDone: {result}")
