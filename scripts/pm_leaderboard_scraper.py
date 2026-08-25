#!/usr/bin/env python3
"""
pm_leaderboard_scraper.py — Polymarket leaderboard discovery + smart wallet seeding.

Discovery paths:
  1. General leaderboard /leaderboard (top by volume) — seeds broad whale universe
  2. Category leaderboards /leaderboard/{cat}/monthly/profit — category-specific
     profit leaders (sports, politics, crypto, tech, culture, finance, economics).
     These pages embed wallet data directly in HTML (not __NEXT_DATA__); parsed
     via regex on the escaped JSON blob.

Also fires Telegram alerts when:
  - A new wallet is discovered on the leaderboard (not in pm_wallets)
  - A wallet graduates to smart status during refresh

Fast-track (promotion on net_pnl alone) was REMOVED 2026-08-21 — see
alert_graduations() for the evidence.

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
from config.polymarket_urls import data_url  # polyproxy: central URL config

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from scripts.alert_formatter import send_telegram

META_DB_PATH = BASE_DIR / "storage" / "whale_meta.db"


def _shadow_dispatch(pipeline: str, message: str, dedup_key: str) -> None:
    """LIVE since 2026-08-21 (was shadow from 2026-07-22). Routes through the
    tier-2 batch queue: one grouped message per pipeline per ~15 min instead of
    one Telegram push per event.

    The caller's direct send MUST be removed when calling this — leaving both
    double-delivers. That is exactly what the Gate-2 runbook got wrong for
    mlb odds_moved: it said "remove shadow=True" without noting the send below
    it was unconditional.
    dedup_key encodes entity+state (F3), never just the pipeline name."""
    try:
        from signals.alert_dispatch import dispatch
        dispatch(pipeline, message, tier=2, dedup_key=dedup_key)
    except Exception as e:  # never let shadow plumbing break a live pipeline
        print(f"[shadow-dispatch] {pipeline} failed: {e}", flush=True)


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


# Category leaderboards with unique profit-leader data (verified 2026-06-24).
# entertainment/business/elections fall back to generic — excluded.
_CATEGORY_LEADERBOARD_URLS = [
    "/leaderboard/sports/monthly/profit",
    "/leaderboard/sports/weekly/profit",
    "/leaderboard/politics/monthly/profit",
    "/leaderboard/crypto/monthly/profit",
    "/leaderboard/tech/monthly/profit",
    "/leaderboard/culture/monthly/profit",
    "/leaderboard/finance/monthly/profit",
    "/leaderboard/economics/monthly/profit",
]

# Fast-track promotion (net_pnl alone, no win_rate/trades requirement) was
# REMOVED 2026-08-21. `net_pnl` for a wallet with no closed positions is 100%
# mark-to-market on OPEN bets, and those marks evaporate: the 12 zero-closed
# fast-tracked wallets carried a stored $47.8M and are worth $234K live today
# (11/12 under $1K). Kept as documentation only — do not reintroduce without
# re-running the falsifier in the vault write-up.
_FAST_TRACK_PNL_RETIRED = 500_000


def scrape_category_leaderboard(path: str) -> List[Dict]:
    """Scrape a category leaderboard page using HTML regex extraction.

    These pages embed wallet data directly in the HTML (not __NEXT_DATA__) as
    escaped JSON. Pattern: proxyWallet\\":\\"0x...\\",...
    Returns list of {wallet, name, pnl, volume, source_path}.
    """
    url = f"https://polymarket.com{path}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120"
    })
    try:
        text = urllib.request.urlopen(req, timeout=20).read().decode()
    except Exception as e:
        print(f"[leaderboard] Failed to fetch {url}: {e}", flush=True)
        return []

    # Unescape the embedded JSON and extract wallet entries
    idx = text.find("proxyWallet")
    if idx < 0:
        return []
    start = text.rfind("[", 0, idx)
    chunk = text[start:start + 80000].replace('\\"', '"')
    pattern = (r'"proxyWallet":"(0x[0-9a-fA-F]+)",'
               r'"name":"([^"]*)","pseudonym":"([^"]*)",'
               r'"amount":([^,]+),"pnl":([^,}]+)')
    raw = re.findall(pattern, chunk)

    seen: set = set()
    entries = []
    for wallet, name, pseudo, amount_str, pnl_str in raw:
        w = wallet.lower()
        if w in seen:
            continue
        seen.add(w)
        display = name if (name and not name.startswith("0x")) else (pseudo if (pseudo and not pseudo.startswith("0x")) else "")
        try:
            pnl = float(pnl_str)
        except ValueError:
            continue
        try:
            volume = float(amount_str)
        except ValueError:
            volume = 0.0
        entries.append({
            "wallet": w,
            "name": display,
            "pnl": pnl,
            "volume": volume,
            "source_path": path,
            "rank": len(entries) + 1,  # position within this leaderboard page
        })
    return entries


def _extract_category(source_path: str) -> Optional[str]:
    """Extract category name from a leaderboard path.
    /leaderboard/sports/monthly/profit → sports
    /leaderboard → general
    """
    parts = source_path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "leaderboard":
        return parts[1] if parts[1] not in ("monthly", "weekly", "profit") else "general"
    return "general"


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
    rank_risers = []  # wallets that jumped ≥10 positions since seeding

    for entry in entries:
        wallet = entry["wallet"]
        name = entry["name"]
        pnl = entry["pnl"]
        volume = entry["volume"]
        category = _extract_category(entry.get("source_path", ""))
        rank = entry.get("rank")

        existing = conn.execute(
            "SELECT wallet, name, smart, rank_at_seed, rank_last_seen, source_category FROM pm_wallets WHERE wallet=?",
            (wallet,)
        ).fetchone()

        if existing is None:
            # New wallet — insert with leaderboard data
            conn.execute(
                "INSERT INTO pm_wallets (wallet, name, first_seen, last_seen,"
                " closed_positions, wins, win_rate, realized_pnl, net_pnl,"
                " zombies, concentration, smart, refreshed, source_category,"
                " rank_at_seed, rank_last_seen, rank_scraped_at)"
                " VALUES (?,?,?,?, 0,0,NULL,0,?, 0,0,0,0, ?,?,?,?)",
                (wallet, name, now, now, pnl, category, rank, rank, int(now))
            )
            new_wallets.append({"wallet": wallet, "name": name, "pnl": pnl, "volume": volume})
        else:
            # Update name, category (keep original if already set), rank
            updates = []
            params = []
            if not existing["name"] and name:
                updates.append("name=?")
                params.append(name)
            if not existing["source_category"] and category:
                updates.append("source_category=?")
                params.append(category)
            if rank is not None:
                updates.extend(["rank_last_seen=?", "rank_scraped_at=?"])
                params.extend([rank, int(now)])
                # Check for rank improvement ≥10 positions (lower = better).
                # SAME-BOARD ONLY (2026-08-21): rank_at_seed belongs to the
                # wallet's source_category, but rank_last_seen is overwritten by
                # whichever category page was scraped most recently. Comparing
                # across boards manufactures phantom jumps — e.g. seeded #37 in
                # economics, later seen #3 on the crypto board, reads as +34.
                seed_rank = existing["rank_at_seed"]
                same_board = (existing["source_category"] or category) == category
                if same_board and seed_rank and rank and (seed_rank - rank) >= 10:
                    rank_risers.append({
                        "wallet": wallet,
                        "name": name or existing["name"],
                        "seed_rank": seed_rank,
                        "current_rank": rank,
                        "category": category,
                        "pnl": pnl,
                    })
            if updates:
                conn.execute(
                    "UPDATE pm_wallets SET " + ", ".join(updates) + " WHERE wallet=?",
                    params + [wallet]
                )
            updated += 1

    conn.commit()
    return {"new": new_wallets, "updated": updated, "rank_risers": rank_risers}


# ── Alerts ────────────────────────────────────────────────────────────────────
def alert_new_discoveries(new_wallets: List[Dict]) -> None:
    """Fire Telegram alert for newly discovered leaderboard wallets."""
    if not new_wallets:
        return

    # Only alert for significant wallets with POSITIVE edge:
    #   - PnL > $10k (profitable), OR
    #   - Volume > $100k AND PnL > 0 (high-volume but must be green)
    # Volume traps (negative PnL, zero closed) are excluded.
    significant = [w for w in new_wallets
                   if w["pnl"] > 10_000 or (w["volume"] > 100_000 and w["pnl"] > 0)]
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
        pnl_tag = f"📈 <b>+${w['pnl']:,.0f}</b>"
        lines.append(f"<b>{name}</b>  {pnl_tag}")
        lines.append(f"   Vol ${w['volume']:,.0f} · queued for evaluation")
    lines.append("")
    lines.append("⏳ Will promote to smart-wallet tier if they meet win-rate criteria.")

    _shadow_dispatch(
        "leaderboard_wallets", "\n".join(lines),
        "discovered:" + "|".join(sorted(w["wallet"] for w in significant)))


# ── Rank velocity alert ───────────────────────────────────────────────────────
_RANK_RISER_DEDUP_FILE = Path("/tmp/rank_riser_dedup.json")
_RANK_RISER_TTL = 24 * 3600
_RISER_MIN_PNL = 10_000     # same positive-edge bar as alert_new_discoveries —
                            # thin categories let negative-PnL wallets climb 20+ spots
_GRINDER_SHARE_MAX = 0.5    # >50% of open positions in 5m/15m up/down = grinder
_UP_DOWN_RE = re.compile(r"up[\s-]*or[\s-]*down|updown|up-down", re.IGNORECASE)


def _fetch_positions(wallet: str) -> Optional[List[Dict]]:
    """Open positions for a wallet from the PM data-api (None on failure)."""
    url = data_url(f"/positions?user={wallet}&limit=100")
    req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode())
        return data if isinstance(data, list) else None
    except Exception as e:
        print(f"[leaderboard] positions fetch failed {wallet[:10]}…: {e}", flush=True)
        return None


def _short_cycle_share(wallet: str) -> Optional[float]:
    """Fraction of open positions in short-cycle up/down markets (None = unknown)."""
    positions = _fetch_positions(wallet)
    if not positions:
        return None
    hits = sum(
        1 for p in positions
        if _UP_DOWN_RE.search(f"{p.get('title', '')} {p.get('slug', '')} {p.get('eventSlug', '')}")
    )
    return hits / len(positions)


def _ensure_grinder_column(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pm_wallets)")}
    if "grinder" not in cols:
        conn.execute("ALTER TABLE pm_wallets ADD COLUMN grinder INTEGER DEFAULT 0")
        conn.commit()


def _rank_riser_recently_alerted(wallet: str) -> bool:
    try:
        with open(_RANK_RISER_DEDUP_FILE) as f:
            cache = json.load(f)
        return (time.time() - cache.get(wallet, 0)) < _RANK_RISER_TTL
    except Exception:
        return False


def _rank_riser_mark_alerted(wallet: str) -> None:
    try:
        try:
            with open(_RANK_RISER_DEDUP_FILE) as f:
                cache = json.load(f)
        except Exception:
            cache = {}
        cache[wallet] = time.time()
        with open(_RANK_RISER_DEDUP_FILE, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass


def alert_rank_risers(risers: List[Dict]) -> None:
    """Alert when a wallet jumps ≥10 leaderboard positions since initial seeding.

    Noise filters (2026-08-18, alert-layer only — seeding + graduation math
    still see every wallet):
      - PnL floor: thin categories (economics/culture) let negative-PnL wallets
        climb 20+ spots by being "less red" than a tiny field.
      - Grinder tag: monthly-green wallets farming 5m/15m up/down markets are
        not smart money; tagged grinder=1 once so they never re-alert.
    API failure fails OPEN — never lose a real riser to a data-api blip.
    """
    if not risers:
        return
    candidates = [r for r in risers if (r.get("pnl") or 0) >= _RISER_MIN_PNL]
    candidates = [r for r in candidates if not _rank_riser_recently_alerted(r["wallet"])]
    if not candidates:
        return

    conn = get_db()
    _ensure_grinder_column(conn)
    new_risers = []
    for r in candidates:
        row = conn.execute(
            "SELECT grinder FROM pm_wallets WHERE wallet=?", (r["wallet"],)
        ).fetchone()
        if row and row["grinder"]:
            continue
        share = _short_cycle_share(r["wallet"])
        if share is not None and share > _GRINDER_SHARE_MAX:
            conn.execute("UPDATE pm_wallets SET grinder=1 WHERE wallet=?", (r["wallet"],))
            conn.commit()
            print(f"[leaderboard] riser {r['wallet'][:10]}… tagged grinder "
                  f"({share:.0%} up/down) — suppressed", flush=True)
            continue
        new_risers.append(r)
    conn.close()
    if not new_risers:
        return

    for r in new_risers:
        _rank_riser_mark_alerted(r["wallet"])

    lines = [
        f"📈 <b>RISING WALLETS</b>",
        f"<i>{len(new_risers)} wallet(s) climbing fast</i>",
        "",
    ]
    for r in sorted(new_risers, key=lambda x: x["seed_rank"] - x["current_rank"], reverse=True):
        name = r["name"] or r["wallet"][:10]
        jump = r["seed_rank"] - r["current_rank"]
        lines.append(
            f"<b>{name}</b> [{r['category']}]\n"
            f"   #{r['seed_rank']} → #{r['current_rank']}  (+{jump} positions)  ${r['pnl']:,.0f} PnL"
        )
    lines.append("\n⚠️ Not yet in smart-wallet tier — tracking for graduation.")
    _shadow_dispatch(
        "rising_wallets", "\n".join(lines),
        "riser:" + "|".join(sorted(
            f"{r['wallet']}@{r['current_rank']}" for r in new_risers)))


_GRAD_DEDUP_FILE = Path("/tmp/graduation_dedup.json")
_GRAD_DEDUP_TTL  = 24 * 3600   # suppress re-alert for the same batch for 24h


def _grad_recently_alerted(top_wallet: str) -> bool:
    """Return True if this wallet was already alerted within the dedup TTL."""
    try:
        with open(_GRAD_DEDUP_FILE) as f:
            cache = json.load(f)
        last_ts = cache.get(top_wallet, 0)
        return (time.time() - last_ts) < _GRAD_DEDUP_TTL
    except Exception:
        return False


def _grad_mark_alerted(top_wallet: str) -> None:
    try:
        try:
            with open(_GRAD_DEDUP_FILE) as f:
                cache = json.load(f)
        except Exception:
            cache = {}
        cache[top_wallet] = time.time()
        with open(_GRAD_DEDUP_FILE, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass


def alert_graduations(conn: sqlite3.Connection) -> int:
    """Check for wallets that meet smart criteria but aren't flagged. Promote + alert.

    Two graduation paths (fast-track removed 2026-08-21):
      1. Standard: closed_positions >= 20 AND win_rate >= 0.62 AND net_pnl >= 100000
         (threshold matches whale_wallets.SMART_MIN_NET — a lower bar here just
         promotes wallets that refresh_wallets().is_smart demotes on the next pass)
      2. Skill: sign-randomization gate (skill_n >= 30, p <= 0.05, positive mean
         probability-point return) — luck-controlled, catches low-WR longshot
         specialists the WR path can't see. Fields written by refresh_wallets.

    REMOVED: fast-track on `net_pnl >= 500_000` alone. For a wallet with no closed
    positions that figure is entirely mark-to-market on open bets — the 12 such
    wallets promoted this way carried a stored $47.8M and are worth $234K live
    (11/12 under $1K; Fisher vs control p=1.1e-8). High-PnL wallets that DO have a
    record average 36.5% WR against this function's own 0.62 bar (n=15, p=3.3e-5).

    ORDER BY is skill_ret, NOT net_pnl. That ordering was the actual cause of the
    frozen roster: all 20 LIMIT slots were consumed every cycle by high-net_pnl
    fast-trackers, which the staleness gate then demoted, while ~109 legitimate
    skill/standard candidates never reached the LIMIT at all. SQLite sorts NULL
    last under DESC, so skill-gated wallets rank ahead of standard-path ones.
    """
    rows = conn.execute("""
        SELECT wallet, name, closed_positions, ROUND(win_rate*100,1) as wr_pct,
               ROUND(net_pnl,0) as net, skill_n, skill_ret, skill_p
        FROM pm_wallets
        WHERE smart = 0
          AND (
            (closed_positions >= 20 AND win_rate >= 0.62 AND net_pnl >= 100000)
            OR (skill_n >= 30 AND skill_p <= 0.05 AND skill_ret > 0)
          )
        ORDER BY skill_ret DESC, net_pnl DESC
        LIMIT 20
    """).fetchall()

    if not rows:
        return 0

    # Promote — use executemany on a fresh connection to avoid transaction
    # contamination from the caller's seed_wallets() writes.
    wallets_to_promote = [r["wallet"] for r in rows]
    promo_conn = get_db()
    try:
        # Stamp refreshed/last_seen: a leaderboard-seeded row carries
        # refreshed=0 and the update branch never touches last_seen, so
        # demote_stale_wallets() saw every promotion as instantly stale and
        # reverted it — promote 20 / demote 20, every cycle, forever.
        _now = time.time()
        _cur = promo_conn.executemany(
            "UPDATE pm_wallets SET smart=1, refreshed=?, last_seen=? WHERE wallet=?",
            [(_now, _now, w) for w in wallets_to_promote],
        )
        _rows_promoted = _cur.rowcount
        # Enqueue so refresh_wallets() re-verifies against LIVE stats inside the
        # staleness TTL, rather than the leaderboard snapshot it was promoted on.
        promo_conn.executemany(
            "INSERT INTO pm_wallet_seen (wallet, name, dollars, last_seen)"
            " VALUES (?,?,0,?)"
            " ON CONFLICT(wallet) DO UPDATE SET last_seen=excluded.last_seen",
            [(r["wallet"], r["name"], _now) for r in rows],
        )
        promo_conn.commit()
        # Report the OBSERVED row count, not the intent — this line previously
        # printed "Promoted 20" on every cycle while smart=1 stayed frozen at 76.
        # rowcount of the UPDATE only — NOT total_changes, which would also
        # count the pm_wallet_seen upsert and overstate the promotion by ~2x.
        if _rows_promoted != len(wallets_to_promote):
            print(f"[leaderboard] WARNING: selected {len(wallets_to_promote)} for promotion "
                  f"but only {_rows_promoted} rows changed", flush=True)
        print(f"[leaderboard] Promoted {_rows_promoted} wallets to smart=1", flush=True)
    finally:
        promo_conn.close()

    # Only alert when a genuine whale graduated — top wallet clears $50K net PnL.
    # Batches of $5K–$25K wallets are silently promoted to avoid noise.
    WHALE_ALERT_MIN = 50_000
    top_wallet = rows[0]["wallet"]

    # Dedup guard: scheduler restarts can trigger multiple back-to-back runs
    # before any promotion commit is visible. Suppress repeat alerts for 24h
    # on the same top wallet.
    if _grad_recently_alerted(top_wallet):
        print(f"[leaderboard] Graduation alert suppressed (dedup): {top_wallet[:10]}…", flush=True)
        return len(rows)

    if rows[0]["net"] < WHALE_ALERT_MIN:
        print(f"[leaderboard] {len(rows)} wallets promoted silently (top PnL ${rows[0]['net']:,.0f} < ${WHALE_ALERT_MIN:,} threshold)", flush=True)
        return len(rows)

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    lines = [
        f"🎓 <b>SMART WALLET GRADUATION</b>",
        f"<i>{len(rows)} wallets promoted to signal-boost tier</i>",
        "",
    ]
    for i, r in enumerate(rows[:5]):
        raw_name = r["name"] or r["wallet"]
        if raw_name.startswith("0x") and len(raw_name) > 12:
            addr = raw_name.split("-")[0]
            name = f"{addr[:6]}…{addr[-4:]}"
        else:
            name = raw_name
        medal = medals[i]
        skill = ((r["skill_n"] or 0) >= 30 and r["skill_p"] is not None
                 and r["skill_p"] <= 0.05 and (r["skill_ret"] or 0) > 0)
        tag = f" 🎯 skill p={r['skill_p']:.3f}" if skill else " 📊 standard"
        lines.append(f"{medal} <b>{name}</b>{tag}")
        wr_str = f"{r['wr_pct']}% WR · " if r["wr_pct"] else ""
        lines.append(f"   {wr_str}{r['closed_positions']} trades · <b>${r['net']:+,.0f}</b>")
    if len(rows) > 5:
        lines.append(f"\n<i>+{len(rows) - 5} more promoted</i>")
    lines.append("")
    lines.append("✅ Signal scores will now be boosted when these wallets enter markets.")

    _grad_mark_alerted(top_wallet)
    _shadow_dispatch(
        "graduation", "\n".join(lines),
        f"graduated:{top_wallet}:{len(rows)}")
    return len(rows)


# ── Main ──────────────────────────────────────────────────────────────────────
def run() -> Dict:
    conn = get_db()
    total_scraped = 0
    total_new: List[Dict] = []
    total_updated = 0

    # 1. General leaderboard (volume-sorted, __NEXT_DATA__ path)
    volume_entries = scrape_leaderboard("/leaderboard")
    print(f"[leaderboard] General: {len(volume_entries)} entries", flush=True)
    if volume_entries:
        result = seed_wallets(conn, volume_entries)
        total_new.extend(result["new"])
        total_updated += result["updated"]
        total_scraped += len(volume_entries)

    # 2. Category leaderboards (HTML regex path, profit-sorted)
    all_rank_risers: List[Dict] = []
    seen_wallets: set = set(e["wallet"] for e in volume_entries)
    for path in _CATEGORY_LEADERBOARD_URLS:
        cat_entries = scrape_category_leaderboard(path)
        # Seed ALL category wallets (including those in general scrape) so ranks are updated
        if cat_entries:
            result = seed_wallets(conn, cat_entries)
            # Only count truly new wallets (not already in general scrape) as new discoveries
            new_this_cat = [w for w in result["new"] if w["wallet"] not in seen_wallets]
            total_new.extend(new_this_cat)
            total_updated += result["updated"]
            total_scraped += len(cat_entries)
            seen_wallets.update(e["wallet"] for e in cat_entries)
            all_rank_risers.extend(result.get("rank_risers", []))
        print(f"[leaderboard] {path}: {len(cat_entries)} entries", flush=True)
        time.sleep(0.3)  # be gentle to PM servers

    # Alert on new high-value discoveries and rank risers
    alert_new_discoveries(total_new)
    alert_rank_risers(all_rank_risers)
    print(f"[leaderboard] Total: scraped={total_scraped} new={len(total_new)} updated={total_updated} risers={len(all_rank_risers)}", flush=True)

    # Check for graduation candidates (standard + fast-track)
    graduated = alert_graduations(conn)
    print(f"[leaderboard] Graduated: {graduated}", flush=True)

    conn.close()
    return {
        "scraped": total_scraped,
        "new": len(total_new),
        "graduated": graduated,
    }


if __name__ == "__main__":
    result = run()
    print(f"\nDone: {result}")
