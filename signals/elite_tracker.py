#!/usr/bin/env python3
"""
Elite Wallet Tracker — monitors trades by top Polymarket wallets and fires:

1. **Individual alerts** — when any $100K+ net wallet makes a trade ≥ $500
2. **Consensus alerts** — when 2+ elite wallets enter the same market
   within a time window, weighted by PnL × WR

Runs on the scheduler every 60s. Polls PM Data API for recent trades,
cross-references against the elite wallet list in whale_meta.db.

Dedup: same wallet × same market suppressed for 2h.
Consensus window: 6h (if RN1 buys at 3pm and cigarettes buys at 7pm,
that's still consensus).
"""

import json
import logging
import os
import sqlite3
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
META_DB = BASE_DIR / "storage" / "whale_meta.db"
PM_DATA_API = "https://data-api.polymarket.com"

# ── Config ──────────────────────────────────────────────────────────
MIN_TRADE_USD = 500           # Min trade size to alert on
DEDUP_WINDOW = 2 * 3600       # 2h — same wallet × same market
CONSENSUS_WINDOW = 6 * 3600   # 6h — window to detect convergence
CONSENSUS_MIN_WALLETS = 2     # 2+ elites = consensus
STATE_FILE = Path("/tmp/elite_tracker_state.json")


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": {}, "market_wallets": {}}


def _save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def _fetch_json(url: str, timeout: int = 12):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        logger.warning("Fetch failed %s: %s", url[:60], e)
        return None


def get_elite_wallets() -> dict:
    """Load elite wallets ($100K+ net, 55%+ WR) from whale_meta.db.
    Returns {wallet_addr: {name, net_pnl, win_rate, weight}}."""
    try:
        conn = sqlite3.connect(str(META_DB), timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT wallet, name, net_pnl, win_rate FROM pm_wallets WHERE smart=1"
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.error("Failed to load elite wallets: %s", e)
        return {}

    elites = {}
    for r in rows:
        net = r["net_pnl"] or 0
        wr = r["win_rate"] or 0
        elites[r["wallet"]] = {
            "name": r["name"] or r["wallet"][:12],
            "net_pnl": net,
            "win_rate": round(wr, 2),
            "weight": round(net * wr),
        }
    return elites


SCAN_LOOKBACK = 600  # Only alert on trades from last 10 minutes
GAMMA_API  = "https://gamma-api.polymarket.com"
ODDS_API   = "https://api.the-odds-api.com/v4"
ODDS_KEY   = os.environ.get("ODDS_API_KEY", "")

_end_date_cache: dict  = {}  # slug → end_date_str, process-lifetime
_odds_cache: dict      = {}  # sport_key → (fetched_at, [events])
ODDS_CACHE_TTL = 300          # 5 min — don't hammer the API per-trade

# Tournament keyword → Odds API sport key
TENNIS_SPORT_MAP: list[tuple[str, str]] = [
    ("bad homburg",     "tennis_wta_bad_homburg_open"),
    ("wta wimbledon",   "tennis_wta_wimbledon"),
    ("atp wimbledon",   "tennis_atp_wimbledon"),
    ("wimbledon",       "tennis_atp_wimbledon"),   # default to ATP if ambiguous
]


def _fetch_event_end(slug: str) -> str | None:
    """Fetch event end_date from PM Gamma API. Cached per process."""
    if not slug:
        return None
    if slug in _end_date_cache:
        return _end_date_cache[slug]
    data = _fetch_json(f"{GAMMA_API}/events?slug={slug}&limit=1")
    end = None
    if data and isinstance(data, list) and data:
        end = data[0].get("endDate") or data[0].get("end_date_iso")
    _end_date_cache[slug] = end
    return end


def _mins_to_end(end_date_str: str) -> float | None:
    """Parse ISO end_date → minutes until resolution. Negative = already resolved."""
    if not end_date_str:
        return None
    try:
        import datetime as dt
        s = str(end_date_str)[:19].replace(" ", "T")
        if not s.endswith("Z"):
            s += "Z"
        end = dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        delta = (end - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60
        return delta
    except Exception:
        return None


def _devig_pinnacle(outcomes: list[dict]) -> dict[str, float]:
    """Return {player_name: devigged_prob} from Pinnacle h2h outcomes."""
    raw = {o["name"]: 1.0 / o["price"] for o in outcomes if o.get("price", 0) > 0}
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {name: prob / total for name, prob in raw.items()}


def _get_vegas_prob(title: str, participant: str) -> float | None:
    """
    Look up Pinnacle devigged probability for `participant` in a tennis match.

    Matches by last name (handles name-order differences between PM and Odds API).
    Returns None if sport not mapped, match not found, or API fails.
    Responses are cached 5min per sport key to avoid per-trade API hits.
    """
    tl = title.lower()
    sport_key = next((v for kw, v in TENNIS_SPORT_MAP if kw in tl), None)
    if not sport_key:
        return None

    now = time.time()
    if sport_key in _odds_cache:
        cached_at, events = _odds_cache[sport_key]
        if now - cached_at > ODDS_CACHE_TTL:
            del _odds_cache[sport_key]
            events = None
    else:
        events = None

    if events is None:
        url = (f"{ODDS_API}/sports/{sport_key}/odds/"
               f"?apiKey={ODDS_KEY}&regions=us&markets=h2h&bookmakers=pinnacle")
        events = _fetch_json(url) or []
        _odds_cache[sport_key] = (now, events)

    # Last-name fuzzy match
    last = participant.split()[-1].lower()
    for event in events:
        teams = [event.get("home_team", ""), event.get("away_team", "")]
        if not any(last in t.lower() for t in teams):
            continue
        for bm in event.get("bookmakers", []):
            if bm["key"] != "pinnacle":
                continue
            for market in bm.get("markets", []):
                if market["key"] != "h2h":
                    continue
                probs = _devig_pinnacle(market["outcomes"])
                # Find matching player
                matched = next(
                    (prob for name, prob in probs.items() if last in name.lower()),
                    None
                )
                return matched
    return None


def fetch_elite_trades(elite_addrs: list, limit: int = 100) -> list:
    """Fetch recent trades for elite wallets from PM Data API.
    Only returns trades from the last SCAN_LOOKBACK seconds."""
    all_trades = []
    cutoff = time.time() - SCAN_LOOKBACK
    for addr in elite_addrs:
        data = _fetch_json(
            f"{PM_DATA_API}/trades?user={addr}&limit=20"
        )
        if data:
            for t in data:
                ts = t.get("timestamp", 0)
                if ts < cutoff:
                    continue  # Skip old trades
                size = float(t.get("size", 0) or 0)
                price = float(t.get("price", 0) or 0)
                vol = size * price
                if vol < MIN_TRADE_USD:
                    continue
                all_trades.append({
                    "wallet": addr,
                    "side": t.get("side", ""),
                    "size": size,
                    "price": price,
                    "volume_usdc": vol,
                    "outcome": t.get("outcome", ""),
                    "title": t.get("title", ""),
                    "timestamp": ts,
                    "slug": t.get("eventSlug", ""),
                    "condition_id": t.get("conditionId", ""),
                    "market": t.get("market", ""),
                })
    return all_trades


def _send_tg(msg: str) -> bool:
    """Send alert via the shared alert formatter."""
    try:
        from scripts.alert_formatter import send_telegram
        return send_telegram(msg)
    except Exception as e:
        logger.error("TG send failed: %s", e)
        return False


def _format_individual(
    wallet_info: dict,
    trade: dict,
    position: dict | None = None,
    counter_flow: list | None = None,
    end_date: str | None = None,
    vegas_fair: float | None = None,
) -> str:
    """Format individual elite wallet trade alert.

    Args:
        position:     {count, total_vol} if adding to existing; None if new.
        counter_flow: list of {name, side, volume, ts} for opposite-side elites
                      seen in the consensus window on this market.
        end_date:     ISO end_date string from PM Gamma API.
    """
    name = wallet_info["name"]
    net  = wallet_info["net_pnl"]
    wr   = wallet_info["win_rate"]
    side = "YES" if trade["side"] == "BUY" else "NO" if trade["side"] == "SELL" else trade["side"]
    price = int(trade["price"] * 100)
    vol   = trade["volume_usdc"]
    title = trade["title"][:80] if trade["title"] else trade["slug"][:40]

    net_str = f"${net/1_000_000:.1f}M" if net >= 1_000_000 else f"${net/1_000:.0f}K"

    # NEW vs ADDING badge
    if position and position["count"] > 1:
        total_str = f"${position['total_vol']:,.0f}"
        pos_badge = f"➕ ADDING (entry #{position['count']}, {total_str} total)"
    else:
        pos_badge = "🆕 NEW POSITION"

    url  = f"https://polymarket.com/event/{trade['slug']}" if trade["slug"] else ""
    link = f"<a href='{url}'>View market</a>" if url else ""

    # Vegas alignment line
    if vegas_fair is not None:
        # For YES/BUY: whale thinks YES is underpriced vs books
        # For NO/SELL: PM YES implied = 1 - NO_price; compare to vegas_fair
        pm_yes_implied = (trade["price"] if side == "YES" else 1.0 - trade["price"])
        gap_pp = (vegas_fair - pm_yes_implied) * 100
        if side == "YES":
            align = "WITH books" if gap_pp > 0 else "FADING books"
        else:
            align = "WITH books" if gap_pp < 0 else "FADING books"
        vegas_line = (
            f"📊 Vegas: {vegas_fair:.0%}  PM: {pm_yes_implied:.0%}  "
            f"[{align}  {abs(gap_pp):.0f}pp]"
        )
    else:
        vegas_line = None

    lines = [
        f"🐋 <b>ELITE MOVE</b>  —  {pos_badge}",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"<b>{name}</b>  ({net_str} net · {wr:.0%} WR)",
        f"{side} @ {price}¢  ·  ${vol:,.0f}",
    ]
    if vegas_line:
        lines.append(vegas_line)
    lines += ["", title]

    # Time to resolution
    mins = _mins_to_end(end_date) if end_date else None
    if mins is not None and mins > 0:
        if mins < 60:
            time_str = f"{int(mins)}m"
        elif mins < 1440:
            h, m = divmod(int(mins), 60)
            time_str = f"{h}h {m}m" if m else f"{h}h"
        else:
            days = int(mins / 1440)
            time_str = f"{days}d"
        lines.append(f"⏰ Resolves in {time_str}")

    # Counter-flow warning (highest priority signal)
    if counter_flow:
        lines.append("")
        for cf in counter_flow[:2]:  # show max 2
            cf_side = "YES" if cf["side"] in ("BUY", "YES") else "NO"
            age_min = int((time.time() - cf["ts"]) / 60)
            age_str = f"{age_min}m ago" if age_min < 60 else f"{age_min // 60}h ago"
            lines.append(
                f"⚠️ COUNTER-FLOW: <b>{cf['name']}</b> on {cf_side}  "
                f"${cf['volume']:,.0f}  —  {age_str}"
            )

    lines += ["", link]
    return "\n".join(lines)


def _format_consensus(market_title: str, wallets: list, slug: str = "") -> str:
    """Format consensus alert when 2+ elites converge on same market."""
    n = len(wallets)
    level = "🔴 STRONG" if n >= 3 else "🟡"

    # Sort by weight descending
    wallets.sort(key=lambda w: -w["weight"])
    total_weight = sum(w["weight"] for w in wallets)
    total_vol = sum(w["volume"] for w in wallets)

    # Determine consensus direction
    yes_weight = sum(w["weight"] for w in wallets if w["side"] in ("BUY", "YES"))
    no_weight = sum(w["weight"] for w in wallets if w["side"] in ("SELL", "NO"))
    if yes_weight > no_weight * 2:
        direction = "YES"
    elif no_weight > yes_weight * 2:
        direction = "NO"
    else:
        direction = "SPLIT"

    url = f"https://polymarket.com/event/{slug}" if slug else ""
    link = f"<a href='{url}'>View</a>" if url else ""

    lines = [
        f"{level} <b>CONSENSUS — {n} elite wallets</b>",
        "",
        market_title[:80] if market_title else "Unknown market",
        f"Direction: <b>{direction}</b> · ${total_vol:,.0f} total",
        "",
    ]
    for w in wallets:
        side = "YES" if w["side"] in ("BUY", "YES") else "NO"
        if w["net_pnl"] >= 1_000_000:
            net_str = f"${w['net_pnl']/1_000_000:.1f}M"
        else:
            net_str = f"${w['net_pnl']/1_000:.0f}K"
        lines.append(f"  🐋 <b>{w['name']}</b> · {side} · ${w['volume']:,.0f} · {net_str} net · {w['wr']:.0%} WR")

    lines.append("")
    lines.append(link)
    return "\n".join(lines)


def run_scan() -> dict:
    """Main scan loop. Returns stats dict."""
    elites = get_elite_wallets()
    if not elites:
        return {"error": "no elite wallets"}

    state = _load_state()
    seen = state.get("seen", {})
    market_wallets = state.get("market_wallets", {})
    # position_history: {wallet:condition → {count, total_vol}} — survives dedup window
    position_history = state.get("position_history", {})
    now = time.time()

    # Clean stale entries (position_history uses longer 24h TTL)
    seen = {k: v for k, v in seen.items() if now - v < DEDUP_WINDOW}
    market_wallets = {
        k: [w for w in v if now - w.get("ts", 0) < CONSENSUS_WINDOW]
        for k, v in market_wallets.items()
    }
    market_wallets = {k: v for k, v in market_wallets.items() if v}
    position_history = {
        k: v for k, v in position_history.items()
        if now - v.get("last_ts", 0) < 24 * 3600
    }

    addrs = list(elites.keys())
    trades = fetch_elite_trades(addrs)

    individual_sent = 0
    consensus_sent = 0

    for trade in trades:
        wallet = trade["wallet"]
        if wallet not in elites:
            continue

        info = elites[wallet]
        slug = trade.get("slug", "")
        condition = trade.get("condition_id", slug)
        dedup_key = f"{wallet}:{condition}"
        pos_key   = f"{wallet}:{condition}"

        # Update position history regardless of dedup (track accumulation)
        ph = position_history.get(pos_key, {"count": 0, "total_vol": 0.0, "last_ts": 0})
        ph["count"]     += 1
        ph["total_vol"] += trade["volume_usdc"]
        ph["last_ts"]    = now
        position_history[pos_key] = ph

        # Skip alert if already sent for this wallet×market in dedup window
        if dedup_key in seen:
            continue

        # Mark seen
        seen[dedup_key] = now

        # Track for consensus
        if condition:
            if condition not in market_wallets:
                market_wallets[condition] = []
            existing = [w["wallet"] for w in market_wallets[condition]]
            if wallet not in existing:
                market_wallets[condition].append({
                    "wallet": wallet,
                    "name": info["name"],
                    "side": trade["side"],
                    "volume": trade["volume_usdc"],
                    "net_pnl": info["net_pnl"],
                    "wr": info["win_rate"],
                    "weight": info["weight"],
                    "ts": now,
                    "title": trade.get("title", ""),
                    "slug": slug,
                })

        # ── Build alert context ──────────────────────────────────────────
        # Counter-flow: opposite-side elites on same market in consensus window
        opposite = "SELL" if trade["side"] == "BUY" else "BUY"
        counter_flow = [
            {"name": w["name"], "side": w["side"], "volume": w["volume"], "ts": w["ts"]}
            for w in market_wallets.get(condition, [])
            if w["side"] == opposite and w["wallet"] != wallet
        ]
        # Sort by most recent first
        counter_flow.sort(key=lambda x: -x["ts"])

        # End date (Gamma API — cached per slug)
        end_date = _fetch_event_end(slug)

        # Vegas alignment — participant is the player/team the market is about
        participant = trade.get("outcome") or trade.get("title", "").split(":")[0].strip()
        vegas_fair = _get_vegas_prob(trade.get("title", ""), participant)

        # Individual alert
        msg = _format_individual(
            info, trade,
            position=ph,
            counter_flow=counter_flow or None,
            end_date=end_date,
            vegas_fair=vegas_fair,
        )
        if _send_tg(msg):
            individual_sent += 1
            logger.info("Elite move: %s %s $%.0f on %s",
                        info["name"], trade["side"], trade["volume_usdc"],
                        trade.get("title", "")[:40])

    # Check for consensus
    for condition, entries in market_wallets.items():
        unique_wallets = {e["wallet"] for e in entries}
        if len(unique_wallets) >= CONSENSUS_MIN_WALLETS:
            # Check if we already sent consensus for this combo
            consensus_key = f"consensus:{condition}:{len(unique_wallets)}"
            if consensus_key in seen:
                continue
            seen[consensus_key] = now

            title = entries[0].get("title", "") if entries else ""
            slug = entries[0].get("slug", "") if entries else ""
            msg = _format_consensus(title, entries, slug)
            if _send_tg(msg):
                consensus_sent += 1
                names = [e["name"] for e in entries]
                logger.info("CONSENSUS: %d elites on %s — %s",
                            len(unique_wallets), title[:40], ", ".join(names))

    # Save state
    state["seen"] = seen
    state["market_wallets"] = market_wallets
    state["position_history"] = position_history
    _save_state(state)

    return {
        "elite_count": len(elites),
        "trades_found": len(trades),
        "individual_sent": individual_sent,
        "consensus_sent": consensus_sent,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_scan()
    print(json.dumps(result, indent=2))
