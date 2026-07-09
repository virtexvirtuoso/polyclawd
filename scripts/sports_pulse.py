#!/usr/bin/env python3
"""
Sports Pulse — automated Polymarket/Kalshi intelligence report.

Usage:
    python3 scripts/sports_pulse.py              # print to stdout
    python3 scripts/sports_pulse.py --telegram   # send to Telegram
    python3 scripts/sports_pulse.py --json       # raw JSON for piping
"""

import json, sqlite3, sys, time, urllib.request, urllib.parse, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────
API         = "http://127.0.0.1:8420"
GAMMA       = "https://gamma-api.polymarket.com"
DB_PATH     = "/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db"
WATCH_PATH  = "/var/www/virtuosocrypto.com/polyclawd/storage/watch_list.json"
WC_EVENT_ID = "30615"
TG_CHAT_ID  = "468298295"
CONVERGENCE_LOOKBACK_HOURS = 8


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get(url: str, timeout: int = 12) -> Optional[dict | list]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[WARN] GET {url[:80]} failed: {e}", file=sys.stderr)
        return None


def _api(path: str, **params) -> Optional[dict | list]:
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    return _get(f"{API}{path}{qs}")


def _c(price: float) -> str:
    """Format 0–1 price as cents string."""
    cents = price * 100
    if cents < 1:
        return f"{cents:.2f}¢"
    if cents < 10:
        return f"{cents:.1f}¢"
    return f"{int(round(cents))}¢"


def _usd(n: float) -> str:
    if n >= 1_000_000:
        return f"${n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n/1_000:.0f}K"
    return f"${n:.0f}"


def _pnl_str(entry: float, current: float, usd_in: float) -> str:
    """Compute paper P&L for a YES position: shares × (current - entry)."""
    if not entry or not current or not usd_in:
        return "?"
    shares = usd_in / entry
    pnl = shares * (current - entry)
    sign = "+" if pnl >= 0 else ""
    return f"{sign}{_usd(pnl)}"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


# ── Data fetchers ──────────────────────────────────────────────────────────────

def fetch_wc_outrights(top_n: int = 8) -> list[dict]:
    """Top N teams by price from WC Winner event."""
    data = _get(f"{GAMMA}/events/{WC_EVENT_ID}")
    if not data:
        return []
    mkts = data.get("markets", [])
    results = []
    for m in mkts:
        team = m.get("groupItemTitle") or m.get("question", "").replace("Will ", "").replace(" win the 2026 FIFA World Cup?", "").strip()
        prices_raw = m.get("outcomePrices", "")
        try:
            prices = json.loads(prices_raw)
            yes_price = float(prices[0])
        except Exception:
            continue
        if yes_price < 0.001:
            continue
        results.append({
            "team": team,
            "price": yes_price,
            "vol24h": m.get("volume24hr") or 0,
            "slug": m.get("slug", ""),
        })
    results.sort(key=lambda x: -x["price"])
    return results[:top_n]


def fetch_hot_markets(limit: int = 8) -> list[dict]:
    """Top markets by 24h volume from trending endpoint.
    Filters out WC per-team outright markets (shown separately in WC section).
    Fetches a larger page to find enough non-WC markets.
    """
    data = _api("/api/markets/trending")
    if not data:
        return []
    markets = data.get("markets", [])

    # Also fetch the next page from Gamma directly with higher limit
    gamma_url = (f"{GAMMA}/markets?active=true&closed=false&limit=50"
                 f"&order=volume24hr&ascending=false")
    gamma_data = _get(gamma_url) or []
    # Merge — deduplicate by slug
    seen_slugs = {m.get("slug", "") for m in markets}
    for gm in gamma_data:
        if gm.get("slug", "") not in seen_slugs:
            # Normalize field names
            gm["volume_24h"] = gm.get("volume24hr") or gm.get("volume_24h") or 0
            gm["yes_price"] = float(json.loads(gm.get("outcomePrices", "[0.5]"))[0])
            markets.append(gm)
            seen_slugs.add(gm.get("slug", ""))

    markets.sort(key=lambda x: -(x.get("volume_24h") or x.get("volume24hr") or 0))

    filtered = []
    for m in markets:
        q = (m.get("question") or "").lower()
        # Skip WC per-team outright markets (shown in WC section)
        if "win the 2026 fifa world cup" in q:
            continue
        # Skip obviously decided markets (>98¢ or <2¢)
        yes_p = m.get("yes_price") or 0.5
        if yes_p > 0.98 or yes_p < 0.02:
            continue
        filtered.append(m)
        if len(filtered) >= limit:
            break

    return filtered


def fetch_whale_flow(limit: int = 25) -> tuple[list[dict], list[dict]]:
    """Returns (live_flow, resolved_flow) — separated by market status."""
    now = _now_utc()
    data = _api("/api/whale/top", limit=limit, severity="CRITICAL")
    if not data:
        return [], []
    alerts = data.get("alerts", [])
    # Also grab HIGH
    high = _api("/api/whale/top", limit=10, severity="HIGH")
    if high:
        alerts += [a for a in high.get("alerts", []) if a.get("score", 0) >= 0.5]

    live, resolved = [], []
    for a in alerts:
        close_iso = a.get("close_time", "")
        is_resolved = False
        if close_iso:
            try:
                close_dt = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
                is_resolved = now > close_dt
            except Exception:
                pass
        # Skip near-decided markets (price >92¢ or <8¢) — no edge
        bid = a.get("best_bid")
        if bid is not None and (bid > 0.92 or bid < 0.08):
            continue
        (resolved if is_resolved else live).append(a)

    return live, resolved


def fetch_resolving_today() -> list[dict]:
    """Markets closing in the next 24h — pulled directly from Gamma API."""
    now = _now_utc()
    end = now + timedelta(hours=24)
    now_s = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_s = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (f"{GAMMA}/markets?active=true&closed=false&limit=30"
           f"&end_date_min={now_s}&end_date_max={end_s}"
           f"&order=volume&ascending=false")
    data = _get(url)
    if not data:
        return []
    _WEATHER_SKIP = ("temperature", "highest temperature", "will it rain", "precipitation",
                     "humidity", "°c on", "°f on", "degrees on")

    results = []
    for m in data:
        vol = m.get("volume24hr") or 0
        if vol < 2_000:
            continue
        q_lower = (m.get("question") or "").lower()
        # Skip weather/temperature markets — not actionable for prediction trading
        if any(kw in q_lower for kw in _WEATHER_SKIP):
            continue
        prices_raw = m.get("outcomePrices", "")
        try:
            prices = json.loads(prices_raw)
            yes_p = float(prices[0])
        except Exception:
            yes_p = 0.5
        results.append({
            "question": m.get("question", "")[:70],
            "end_date": m.get("endDate", ""),
            "yes_price": yes_p,
            "vol24h": vol,
            "slug": m.get("slug", ""),
        })
    # Sort by soonest close time — urgency first
    results.sort(key=lambda x: x["end_date"])
    return results[:8]


def fetch_convergences(hours: int = CONVERGENCE_LOOKBACK_HOURS) -> list[dict]:
    """Recent smart wallet convergences from DB, with market titles."""
    cutoff = int(time.time()) - hours * 3600
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT market, direction, alerted_at, n_wallets, total_usd "
                "FROM smart_wallet_convergence_dedup "
                "WHERE alerted_at > ? ORDER BY alerted_at DESC LIMIT 15",
                (cutoff,)
            ).fetchall()
    except Exception as e:
        print(f"[WARN] convergence DB query failed: {e}", file=sys.stderr)
        return []

    if not rows:
        return []

    # Batch-resolve condition IDs → market titles via Gamma API
    condition_ids = [r[0] for r in rows if r[0].startswith("0x")]
    slug_ids      = [r[0] for r in rows if not r[0].startswith("0x")]
    title_map = {}

    # Gamma API doesn't support batch condition_ids — look up individually in parallel
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _resolve_cid(cid: str) -> tuple[str, str]:
        # Try Gamma first (works for active markets)
        data = _get(f"{GAMMA}/markets?condition_ids={cid}&limit=1")
        if data and len(data) > 0:
            return cid, data[0].get("question", cid[:30] + "…")
        # Fallback: CLOB API (works for closed/resolved markets too)
        clob = _get(f"https://clob.polymarket.com/markets/{cid}")
        if clob and isinstance(clob, dict) and clob.get("question"):
            return cid, clob["question"]
        return cid, cid[:30] + "…"

    def _resolve_slug(s: str) -> tuple[str, str]:
        data = _get(f"{GAMMA}/markets?slug={s}&limit=1")
        if data and len(data) > 0:
            return s, data[0].get("question", s[:40])
        return s, s[:40]

    ids_to_resolve = condition_ids[:12] + slug_ids[:5]
    resolve_fns = {cid: _resolve_cid for cid in condition_ids[:12]}
    resolve_fns.update({s: _resolve_slug for s in slug_ids[:5]})

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(resolve_fns[k], k): k for k in ids_to_resolve}
        for fut in as_completed(futures, timeout=15):
            try:
                k, title = fut.result()
                title_map[k] = title
            except Exception:
                pass

    results = []
    for (market, direction, alerted_at, n_wallets, total_usd) in rows:
        title = title_map.get(market, market[:40] + "…")
        ts = datetime.fromtimestamp(alerted_at, tz=timezone.utc)
        ago_min = int((_now_utc() - ts).total_seconds() / 60)
        results.append({
            "market": market,
            "title": title,
            "direction": direction,
            "n_wallets": n_wallets,
            "total_usd": total_usd,
            "ago_min": ago_min,
            "ts": ts,
        })

    return results


def _gamma_price(slug_or_condition: str) -> Optional[float]:
    """Fetch current YES price for a PM market."""
    if slug_or_condition.startswith("0x"):
        data = _get(f"{GAMMA}/markets?condition_ids={slug_or_condition}&limit=1")
    else:
        data = _get(f"{GAMMA}/markets?slug={slug_or_condition}&limit=1")
    if data and len(data) > 0:
        prices_raw = data[0].get("outcomePrices", "")
        try:
            return float(json.loads(prices_raw)[0])
        except Exception:
            pass
    return None


def _kalshi_price(ticker: str) -> tuple[Optional[float], str]:
    """Fetch current Kalshi YES price.
    Returns (price, status) where status is 'ok' | 'illiquid' | 'not_found'.
    """
    data = _api("/api/whale/book", platform="kalshi", market=ticker)
    if data is None:
        return None, "not_found"
    bids = data.get("bids", [])
    if bids:
        return float(bids[0][0]), "ok"
    # Returned a response but no bids — market exists but is illiquid
    return None, "illiquid"


def load_watch_list() -> dict:
    """Load watch_list.json — gracefully handle missing file."""
    try:
        with open(WATCH_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"positions": [], "arbs": [], "wallets": []}
    except Exception as e:
        print(f"[WARN] watch_list.json load failed: {e}", file=sys.stderr)
        return {"positions": [], "arbs": [], "wallets": []}


def grade_positions(positions: list[dict]) -> list[dict]:
    """Fetch live prices and compute P&L for each watched position."""
    graded = []
    for p in positions:
        if p.get("status") == "CLOSED":
            graded.append({**p, "current_price": None, "pnl_str": "CLOSED"})
            continue

        pm_slug = p.get("pm_slug")
        current = _gamma_price(pm_slug) if pm_slug else None

        entry = p.get("entry_price")
        usd_in = p.get("entry_usd", 0)
        direction = p.get("direction", "YES")

        if current is not None and entry and usd_in and direction == "YES":
            pnl_str = _pnl_str(entry, current, usd_in)
        elif current is not None:
            pnl_str = f"now {_c(current)}"
        else:
            pnl_str = "?"

        graded.append({**p, "current_price": current, "pnl_str": pnl_str})

    return graded


def grade_arbs(arbs: list[dict]) -> list[dict]:
    """Fetch live prices for tracked arbs and compute current spread."""
    graded = []
    for a in arbs:
        pm_price = _gamma_price(a.get("pm_slug", "")) if a.get("pm_slug") else None
        kalshi_ticker = a.get("kalshi_ticker", "")
        if kalshi_ticker:
            kalshi_price, kalshi_status = _kalshi_price(kalshi_ticker)
        else:
            kalshi_price, kalshi_status = None, "not_found"

        spread_pp = None
        if pm_price is not None and kalshi_price is not None:
            spread_pp = round((kalshi_price - pm_price) * 100, 1)

        graded.append({
            **a,
            "pm_price": pm_price,
            "kalshi_price": kalshi_price,
            "kalshi_status": kalshi_status,
            "current_spread_pp": spread_pp,
        })

    return graded


# ── Formatting ─────────────────────────────────────────────────────────────────

def _fmt_ago(ago_min: int) -> str:
    if ago_min < 60:
        return f"{ago_min}m ago"
    h = ago_min // 60
    m = ago_min % 60
    return f"{h}h{m:02d}m ago" if m else f"{h}h ago"


def _fmt_time_et(iso: str) -> str:
    if not iso:
        return ""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        local = dt.astimezone(ZoneInfo("America/New_York"))
        return local.strftime("%-I:%M %p ET")
    except Exception:
        return iso[:16]


def _convergence_tier(ago_min: int, n_wallets: int) -> str:
    """Signal quality tier based on recency and wallet count."""
    if ago_min < 5 and n_wallets >= 3:
        return "⚡ Flash"
    if ago_min < 15:
        return "🟢 Tight"
    if ago_min < 30:
        return "🟡 Broad"
    return "📊 Gradual"


def format_pulse(
    wc: list,
    hot: list,
    whale_live: list,
    whale_post: list,
    resolving: list,
    convergences: list,
    positions: list,
    arbs: list,
    watch_wallets: list,
) -> str:
    now = _now_utc()
    now_et = now.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
    ts = now_et.strftime("%b %-d, %Y · %-I:%M %p ET")

    lines = [f"Polymarket Sports Pulse · {ts}", "─" * 36]

    # ── ∆ Watch Grades ─────────────────────────────────────────────────
    if positions:
        lines.append("\n∆ Position Tracker")
        for p in positions:
            status = p.get("pnl_str", "?")
            entry_c = _c(p["entry_price"]) if p.get("entry_price") else "?"
            cur_c   = _c(p["current_price"]) if p.get("current_price") else "?"
            label   = p.get("label", "?")
            usd_in  = _usd(p.get("entry_usd", 0)) if p.get("entry_usd") else ""
            pnl     = p.get("pnl_str", "")

            if p.get("status") == "CLOSED":
                lines.append(f"• {label} — CLOSED")
            elif p.get("current_price") is not None and p.get("entry_price"):
                lines.append(f"• {label} · {usd_in} @ {entry_c} entry · now {cur_c} · P&L {pnl}")
            else:
                lines.append(f"• {label} · {usd_in} @ {entry_c} entry · {status}")

    # ── Arbs ───────────────────────────────────────────────────────────
    if arbs:
        lines.append("\n🔄 Live Arbs")
        for a in arbs:
            label = a.get("label", "?")
            pm    = _c(a["pm_price"]) if a.get("pm_price") else "?"
            kal   = _c(a["kalshi_price"]) if a.get("kalshi_price") else "?"
            spread = a.get("current_spread_pp")
            entry_spread = a.get("entry_spread_pp")

            kalshi_status = a.get("kalshi_status", "not_found")
            if spread is not None:
                arrow = "▲" if (entry_spread and spread > entry_spread) else ("▼" if (entry_spread and spread < entry_spread) else "")
                spread_str = f"{spread:+.1f}pp {arrow}".strip()
                lines.append(f"• {label}: PM {pm} · Kalshi {kal} · spread {spread_str}")
            elif kalshi_status == "illiquid":
                lines.append(f"• {label}: PM {pm} · Kalshi ⚠️ no bids (illiquid) — arb untradeable")
            elif kalshi_status == "not_found":
                lines.append(f"• {label}: PM {pm} · Kalshi not found")
            else:
                lines.append(f"• {label}: PM {pm} · Kalshi {kal} · spread unknown")

    # ── Hot Markets ────────────────────────────────────────────────────
    lines.append("\n🔥 Hot Markets — Top by 24h Vol")
    for m in hot[:6]:
        q = m.get("question", "")[:65]
        vol = m.get("volume_24h") or 0
        yes_p = m.get("yes_price", 0.5)
        lines.append(f"• {q}  {_c(yes_p)} · {_usd(vol)}")

    # ── WC Outrights ──────────────────────────────────────────────────
    if wc:
        lines.append("\n🌍 WC Winner — Current Odds")
        for t in wc:
            bar_len = max(1, int(t["price"] * 200))
            bar = "█" * bar_len
            lines.append(f"  {t['team']:<18} {_c(t['price'])}  {bar}")

    # ── Smart Wallet Convergences ─────────────────────────────────────
    if convergences:
        lines.append(f"\n🔥 Smart Wallet Convergences (last {CONVERGENCE_LOOKBACK_HOURS}h)")
        for c in convergences[:6]:
            tier = _convergence_tier(c["ago_min"], c["n_wallets"])
            title_short = c["title"][:55]
            lines.append(
                f"  {tier} · {c['n_wallets']} wallets · {c['direction']} · "
                f"{_usd(c['total_usd'])} · {_fmt_ago(c['ago_min'])}\n"
                f"  └ {title_short}"
            )
    else:
        lines.append(f"\n🔥 Smart Wallet Convergences — none in last {CONVERGENCE_LOOKBACK_HOURS}h")

    # ── Whale Flow (Live) ─────────────────────────────────────────────
    lines.append("\n🐳 Whale Flow — LIVE Markets")
    if whale_live:
        seen = set()
        count = 0
        for a in whale_live:
            title = " ".join((a.get("title") or a.get("market", "")).split())[:55]
            if title in seen:
                continue
            seen.add(title)
            flow = _usd(a.get("flow_dollars", 0))
            score = a.get("score", 0)
            fy = a.get("flow_yes", 0) or 0
            fn = a.get("flow_no", 0) or 0
            total = fy + fn
            dir_str = ""
            if total > 0:
                if fy / total >= 0.65:
                    dir_str = f"  {int(fy/total*100)}% YES"
                elif fn / total >= 0.65:
                    dir_str = f"  {int(fn/total*100)}% NO"
            bid = a.get("best_bid")
            px_str = f"  {_c(bid)}" if bid else ""
            smart = ""
            for r in a.get("reasons", "").split(","):
                if r.strip().startswith("smart_wallet_"):
                    smart = "  🐋 " + r.strip().replace("smart_wallet_", "").replace("_", " ")
                    break
            lines.append(f"• {title}{px_str} · {flow}{dir_str}{smart}")
            count += 1
            if count >= 6:
                break
    else:
        lines.append("  No active whale flow")

    # ── Whale Flow (Post-mortem) ──────────────────────────────────────
    if whale_post:
        lines.append("\n📊 Whale Flow — POST-MORTEM (resolved)")
        seen = set()
        count = 0
        for a in whale_post[:5]:
            title = " ".join((a.get("title") or a.get("market", "")).split())[:55]
            if title in seen:
                continue
            seen.add(title)
            flow = _usd(a.get("flow_dollars", 0))
            fy = a.get("flow_yes", 0) or 0
            fn = a.get("flow_no", 0) or 0
            total = fy + fn
            dir_str = ""
            if total > 0:
                if fy / total >= 0.6:
                    dir_str = " → YES"
                elif fn / total >= 0.6:
                    dir_str = " → NO"
            lines.append(f"• {title} · {flow}{dir_str}")
            count += 1

    # ── Resolving Today ───────────────────────────────────────────────
    lines.append("\n📅 Resolving Today")
    if resolving:
        for m in resolving[:6]:
            title = m["question"][:60]
            yes_p = m["yes_price"]
            close_t = _fmt_time_et(m["end_date"])
            vol = _usd(m["vol24h"])
            lines.append(f"• {title}  {_c(yes_p)} · closes {close_t} · {vol} 24h")
    else:
        lines.append("  No data — Gamma API returned 0 markets")

    # ── Watch Wallets ─────────────────────────────────────────────────
    if watch_wallets:
        lines.append("\n👁 Watch Wallets")
        for w in watch_wallets:
            wallet  = w.get("wallet", "?")
            trigger = w.get("trigger", "any activity")
            reason  = w.get("reason", "")
            lines.append(f"• {wallet} — trigger: {trigger}")
            if reason:
                lines.append(f"  └ {reason}")

    # ── Action Block ──────────────────────────────────────────────────
    actions = _derive_actions(wc, convergences, arbs, whale_live)
    if actions:
        lines.append("\n⚡ Action Items")
        for a in actions:
            lines.append(f"• {a}")

    return "\n".join(lines)


def _derive_actions(wc, convergences, arbs, whale_live) -> list[str]:
    """Heuristic action derivation from live data."""
    actions = []

    # Arbs with positive spread
    for a in arbs:
        spread = a.get("current_spread_pp")
        if spread is not None and spread >= 3:
            label = a.get("label", "Arb")
            pm    = _c(a["pm_price"]) if a.get("pm_price") else "?"
            kal   = _c(a["kalshi_price"]) if a.get("kalshi_price") else "?"
            actions.append(f"{label}: {spread:+.1f}pp spread open — PM {pm} vs Kalshi {kal}")

    # Flash convergences
    for c in convergences:
        if c["ago_min"] < 15 and c["n_wallets"] >= 3:
            title_short = c["title"][:45]
            actions.append(
                f"Convergence signal: {c['n_wallets']} wallets {c['direction']} {title_short} "
                f"({_fmt_ago(c['ago_min'])})"
            )

    # High-score live whale flow with smart wallet
    for a in whale_live[:3]:
        score = a.get("score", 0)
        reasons = a.get("reasons", "")
        if score >= 0.7 and "smart_wallet" in reasons:
            title = (a.get("title") or "")[:45]
            flow  = _usd(a.get("flow_dollars", 0))
            bid   = a.get("best_bid")
            px    = f" @ {_c(bid)}" if bid else ""
            actions.append(f"Smart wallet whale: {title}{px} · {flow}")

    if not actions:
        actions.append("No high-conviction action items at this time")

    return actions


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    send_tg   = "--telegram" in sys.argv
    json_mode = "--json"     in sys.argv

    print("[1/7] WC outrights...", file=sys.stderr)
    wc = fetch_wc_outrights()

    print("[2/7] Hot markets...", file=sys.stderr)
    hot = fetch_hot_markets()

    print("[3/7] Whale flow...", file=sys.stderr)
    whale_live, whale_post = fetch_whale_flow()

    print("[4/7] Resolving today...", file=sys.stderr)
    resolving = fetch_resolving_today()

    print("[5/7] Smart wallet convergences...", file=sys.stderr)
    convergences = fetch_convergences()

    print("[6/7] Watch list...", file=sys.stderr)
    watch = load_watch_list()
    positions = grade_positions(watch.get("positions", []))
    arbs      = grade_arbs(watch.get("arbs", []))

    print("[7/7] Formatting...", file=sys.stderr)
    pulse = format_pulse(
        wc=wc,
        hot=hot,
        whale_live=whale_live,
        whale_post=whale_post,
        resolving=resolving,
        convergences=convergences,
        positions=positions,
        arbs=arbs,
        watch_wallets=watch.get("wallets", []),
    )

    if json_mode:
        print(json.dumps({
            "wc": wc, "hot": [{"question": h.get("question"), "vol": h.get("volume_24h")} for h in hot],
            "convergences": len(convergences), "resolving": len(resolving),
            "whale_live": len(whale_live), "whale_post": len(whale_post),
        }))
        return

    print(pulse)

    if send_tg:
        _send_telegram(pulse)


def _send_telegram(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("[WARN] TELEGRAM_BOT_TOKEN not set — skipping TG send", file=sys.stderr)
        return
    # Split into chunks ≤4000 chars
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        payload = urllib.parse.urlencode({
            "chat_id": TG_CHAT_ID,
            "text": chunk,
        }).encode()
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read())
                if not resp.get("ok"):
                    print(f"[WARN] TG send returned ok=false: {resp}", file=sys.stderr)
        except Exception as e:
            print(f"[ERROR] TG send failed: {e}", file=sys.stderr)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
