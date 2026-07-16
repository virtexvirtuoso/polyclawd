#!/usr/bin/env python3
"""
Sports Pulse — automated Polymarket/Kalshi intelligence report.

Usage:
    python3 scripts/sports_pulse.py              # print to stdout
    python3 scripts/sports_pulse.py --telegram   # send to Telegram
    python3 scripts/sports_pulse.py --json       # raw JSON for piping
"""

import hashlib, json, re, sqlite3, sys, time, urllib.request, urllib.parse, os
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
    """
    Fetch URL. For external Gamma API calls, routes via eth0 to bypass WireGuard
    tunnel (which would otherwise saturate with read traffic). Local API calls use
    urllib as normal.
    """
    import subprocess as _sp
    if url.startswith("http://"):
        # Local API - use urllib directly (no tunnel routing issue)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            print(f"[WARN] GET {url[:80]} failed: {e}", file=sys.stderr)
            return None
    else:
        # External Gamma API - use curl --interface eth0 to bypass WireGuard
        try:
            result = _sp.run(
                ["curl", "-s", "--interface", "eth0", "--max-time", str(timeout),
                 "-H", "User-Agent: Polyclawd/1.0", url],
                capture_output=True, text=True, timeout=timeout + 2
            )
            if result.returncode == 0 and result.stdout:
                return json.loads(result.stdout)
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


def fetch_recent_results(hours: int = 36, tag_slug: str = "fifa-world-cup") -> list[dict]:
    """
    Pull recently-resolved WC game markets from PM.
    ONLY uses outcomePrices=["1","0"] -- never infers or guesses results.
    """
    import json as _json
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    since = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    since = (now - timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
    data = _get(f"{GAMMA}/events?tag_slug={tag_slug}&limit=80&closed=true&end_date_min={since}")
    if not data:
        return []

    results = []
    for e in data:
        title = e.get("title", "")
        for m in e.get("markets", []):
            q = m.get("question", "")
            p = m.get("outcomePrices", "")
            end_iso = m.get("endDateIso") or m.get("endDate") or ""
            closed_time = m.get("closedTime") or end_iso
            try:
                ct = datetime.fromisoformat(closed_time.replace("Z", "+00:00"))
                if ct < cutoff:
                    continue
            except Exception:
                continue
            if not p or not q:
                continue
            try:
                parsed = _json.loads(p)
                if float(parsed[0]) < 0.99:
                    continue
            except Exception:
                continue
            # Only keep direct game results: 'Will X win' or 'end in a draw' per-game questions
            import re as _re
            q_lower = q.lower()
            is_game_result = (
                bool(_re.search(r'will .+ win on \d{4}-\d{2}-\d{2}', q_lower)) or
                'end in a draw' in q_lower or
                bool(_re.match(r'exact score:', q_lower))
            )
            if not is_game_result:
                continue
            results.append({"event": title, "question": q, "closed_time": closed_time})

    # One entry per event, sorted by time
    seen = {}
    for r in results:
        ev = r["event"]
        if ev not in seen:
            seen[ev] = r
    return sorted(seen.values(), key=lambda x: x["closed_time"])

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
    """Top markets by 24h volume — direct from Gamma API only."""
    gamma_url = (f"{GAMMA}/markets?active=true&closed=false&limit=10"
                 f"&order=volume24hr&ascending=false")
    markets = _get(gamma_url) or []
    for gm in markets:
        gm["volume_24h"] = gm.get("volume24hr") or gm.get("volume_24h") or 0
        try:
            gm["yes_price"] = float(json.loads(gm.get("outcomePrices", "[0.5]"))[0])
        except Exception:
            gm["yes_price"] = 0.5

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

    # Use condition ID short labels — avoid per-ID network resolution (too slow)
    title_map = {}

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
        data = _get(f"{GAMMA}/markets?condition_ids={slug_or_condition}&limit=1", timeout=6)
    else:
        data = _get(f"{GAMMA}/markets?slug={slug_or_condition}&limit=1", timeout=6)
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
    recent_results: list,
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

    # ── Recent Results (PM-verified only) ────────────────────────────
    if recent_results:
        lines.append("\n✅ Recent Results (PM-confirmed)")
        # Dedupe by game — normalize title, prefer exact scores with real scorelines
        import re as _re_d
        seen_events = {}
        for r in recent_results:
            ev = r['event']
            q = r['question']
            # Normalize event title (strip ' - Exact Score', ' - Halftime Result' etc.)
            game_key = _re_d.sub(r' - (Exact Score|Halftime Result|More Markets|.* Score)$', '', ev).strip()
            # Skip useless exact score lines
            if q.lower().startswith('exact score:') and 'any other score' in q.lower():
                continue
            # Prefer exact score line (has actual scoreline vs just 'X won')
            priority = 1 if q.lower().startswith('exact score:') else 0
            if game_key not in seen_events or priority > seen_events[game_key][0]:
                seen_events[game_key] = (priority, q)
        for ev, (_, q) in seen_events.items():
            if q.lower().startswith('exact score:'):
                # Format: "Ecuador vs Germany: 2-1 Ecuador"
                import re as _re
                m = _re.match(r'exact score: (.+)\?$', q, _re.IGNORECASE)
                if m:
                    lines.append(f"• {m.group(1)}")
            elif 'end in a draw' in q.lower():
                import re as _re
                m = _re.match(r'will (.+) end in a draw', q, _re.IGNORECASE)
                if m:
                    lines.append(f"• {m.group(1)}: DRAW")
            else:
                # 'Will X win on DATE?' → 'X won'
                import re as _re
                m = _re.match(r'will (.+) win on \d{4}-\d{2}-\d{2}', q, _re.IGNORECASE)
                if m:
                    lines.append(f"• {ev}: {m.group(1)} won")
                else:
                    lines.append(f"• {ev}: {q}")

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

    print("[0/7] Recent results...", file=sys.stderr)
    recent_results = fetch_recent_results()

    print("[1/7] WC outrights...", file=sys.stderr)
    wc = fetch_wc_outrights()
    time.sleep(1)  # avoid Gamma rate-limit after bulk recent_results fetch

    print("[2/7] Hot markets...", file=sys.stderr)
    hot = fetch_hot_markets()

    print("[3/7] Whale flow...", file=sys.stderr)
    whale_live, whale_post = fetch_whale_flow()

    print("[4/7] Resolving today...", file=sys.stderr)
    resolving = fetch_resolving_today()

    print("[5/7] Smart wallet convergences...", file=sys.stderr)
    convergences = fetch_convergences()

    time.sleep(2)  # let Gamma rate-limit recover before watch list price fetches
    print("[6/7] Watch list...", file=sys.stderr)
    watch = load_watch_list()
    # Use thread timeout to avoid hanging on Gamma rate-limit during price lookups
    from concurrent.futures import ThreadPoolExecutor as _TPE, TimeoutError as _TE
    _positions = []
    _arbs = []
    try:
        with _TPE(max_workers=1) as _ex:
            _pf = _ex.submit(grade_positions, watch.get("positions", []))
            _af = _ex.submit(grade_arbs, watch.get("arbs", []))
            try:
                _positions = _pf.result(timeout=8)
            except Exception:
                _positions = [{**p, 'current_price': None, 'pnl_str': '?'} for p in watch.get('positions', [])]
            try:
                _arbs = _af.result(timeout=8)
            except Exception:
                _arbs = [{**a, 'pm_price': None, 'kalshi_price': None, 'kalshi_status': 'timeout', 'current_spread_pp': None} for a in watch.get('arbs', [])]
    except Exception:
        pass
    positions = _positions
    arbs      = _arbs

    print("[7/7] Formatting...", file=sys.stderr)
    pulse = format_pulse(
        recent_results=recent_results,
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
        if should_send_status(pulse):
            _send_telegram(pulse)
            record_status_sent(pulse)
        else:
            print("[skip] status unchanged since last send (kv state hash)", file=sys.stderr)


# ── Status change detection (Task 4.2, 2026-07-16 alert overhaul) ────────────
# Skip the send when the report (timestamps stripped) hashes identically to the
# last sent one. Hash lives in a kv row in shadow_trades.db — restart-proof,
# unlike process memory. The 20:00 ET slot always sends (daily proof-of-life).

_STATUS_HASH_KEY = "status_report_hash"
_TS_STRIP_RE = re.compile(
    r"\d{1,2}:\d{2}(?:\s?[AP]M)?(?:\s?ET)?"          # 12:05, 4:05 PM, 8:00 PM ET
    r"|[A-Z][a-z]{2} \d{1,2}, \d{4}"                   # Jul 16, 2026
)


def _normalize_for_hash(text: str) -> str:
    return _TS_STRIP_RE.sub("", text or "")


def _status_hash(text: str) -> str:
    return hashlib.sha256(_normalize_for_hash(text).encode()).hexdigest()


def _kv_conn(db_path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH), timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    return conn


def should_send_status(text: str, db_path=None, now=None) -> bool:
    """False only when the normalized report matches the stored hash AND we are
    not in the 20:00 ET always-send slot. Fails open (send) on any DB error."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif isinstance(now, (int, float)):
        now = datetime.fromtimestamp(now, tz=timezone.utc)
    import zoneinfo
    if now.astimezone(zoneinfo.ZoneInfo("America/New_York")).hour == 20:
        return True
    try:
        conn = _kv_conn(db_path)
        try:
            row = conn.execute(
                "SELECT v FROM kv WHERE k=?", (_STATUS_HASH_KEY,)).fetchone()
        finally:
            conn.close()
    except Exception as e:
        print(f"[WARN] status-hash read failed ({e}) — sending", file=sys.stderr)
        return True
    return row is None or row[0] != _status_hash(text)


def record_status_sent(text: str, db_path=None) -> None:
    """Persist the hash of a successfully composed+sent report. Best-effort."""
    try:
        conn = _kv_conn(db_path)
        try:
            conn.execute(
                "INSERT INTO kv (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (_STATUS_HASH_KEY, _status_hash(text)))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[WARN] status-hash store failed: {e}", file=sys.stderr)


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
