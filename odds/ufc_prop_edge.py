#!/usr/bin/env python3
"""
ufc_prop_edge.py — UFC prop edge comparison.

Compares Polymarket Gamma prop markets vs Kalshi prop markets for the same fight
and flags cross-platform pricing inefficiencies.

Matching is order-, accent-, and nickname-insensitive: fights are keyed by the
UNORDERED pair of normalized fighter SURNAMES. PM and Kalshi disagree on fighter
order ("A vs. B" vs "B vs A"), accents (Bolaños/Bolanos), and first-name form
(Bia/Beatriz Mesquita), so a surname-set key is the only thing that lines up.

Only FIGHT-LEVEL method props are compared (any KO/TKO, any submission, go the
distance) — these map 1:1 to Kalshi's MOF/DISTANCE series. Round O/U props are
collected on the PM side but not edged: PM "O/U N.5 rounds" maps to a SUM of
Kalshi round buckets, not a single market (left as a future enhancement).

Edges are computed on MIDPOINTS, then the PM "buy" side is reality-checked
against the live CLOB order book (poly_executable_edge) so a midpoint edge that
disappears at the ask is not flagged tradeable.

Note: Kalshi lists a fight's prop series (KXUFCMOF / KXUFCDISTANCE) only closer
to fight night, after the moneyline (KXUFCFIGHT) is already up — so the Kalshi
side is empty until then, and edges only appear once both platforms have props.

Usage:
  python3 odds/ufc_prop_edge.py              # full scan + executable reality-check
  python3 odds/ufc_prop_edge.py --dry         # skip the CLOB enrichment (faster)
"""

import os, sys, json, time, sqlite3, unicodedata, requests
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional
from config.polymarket_urls import GAMMA_API, CLOB_API  # polyproxy: central URL config

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from db import connect as db_connect  # noqa: E402
from execution.fee_model import taker_fee_fraction  # noqa: E402

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
DB_PATH = os.path.join(PROJECT_DIR, "storage", "shadow_trades.db")
MIN_EDGE_PP = 4.0
PM_MIN_VOLUME_USD = 500   # skip PM prop prices with no real trading activity
KA_MAX_SPREAD = 0.08      # Kalshi book wider than this -> mid is meaningless, not tradeable
KA_MIN_DEPTH_USD = 100.0  # top-of-book ask depth below this -> illiquid, not tradeable
COOLDOWN_MINUTES = 60  # props move slower than sportsbook lines
EDGE_CHANGE_PP = 3.0  # re-alert inside cooldown if edge moves >= this
MAX_ALERTS_PER_SCAN = 5
DRY_RUN = "--dry" in sys.argv

try:  # shared order-book executable-edge gating (reality-check vs midpoint)
    from odds import poly_executable_edge as pee
except ImportError:
    import poly_executable_edge as pee


@dataclass
class UFCPropEdge:
    fight: str
    prop_type: str
    label: str
    polymarket_price: float
    kalshi_price: float
    edge_pp: float
    direction: str
    tradeable: bool = False
    executable_edge_pp: Optional[float] = None  # ask-walked + fee-adjusted, both directions
    executable_price: Optional[float] = None    # price you'd actually pay on the buy side
    kalshi_ticker: Optional[str] = None
    kalshi_spread: Optional[float] = None
    kalshi_depth_usd: Optional[float] = None
    pm_condition_id: Optional[str] = None
    gate_reason: Optional[str] = None           # why tradeable=False despite big mid edge


# ---------------------------------------------------------------------------
# Name / fight-key normalization
# ---------------------------------------------------------------------------


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _norm(s: str) -> str:
    s = _strip_accents(s).lower()
    return "".join(c if (c.isalnum() or c.isspace()) else " " for c in s).strip()


def _surname(fighter: str) -> str:
    toks = _norm(fighter).split()
    return toks[-1] if toks else ""


def _fight_key(title: str) -> Optional[frozenset]:
    """Unordered pair of normalized surnames; robust to PM/Kalshi title forms.

    Handles "UFC <event>: A vs. B (Weightclass)" and bare "A vs B".
    Returns None when it can't extract two distinct surnames.
    """
    t = title
    if ":" in t:  # drop PM "UFC Fight Night: " prefix
        t = t.split(":", 1)[1]
    if "(" in t:  # drop "(Weightclass ...)" suffix
        t = t[: t.index("(")]
    low = t.lower()
    for sep in (" vs. ", " vs "):
        if sep in low:
            i = low.index(sep)
            sa, sb = _surname(t[:i]), _surname(t[i + len(sep) :])
            if sa and sb and sa != sb:
                return frozenset((sa, sb))
            return None
    return None


def _canonical_prop_label(question: str) -> Optional[str]:
    """Map a PM market question to a fight-level prop label, or None.

    Fight-level questions say "the fight be won by …"; per-fighter questions say
    "{name} win by …" — we deliberately keep only the fight-level ones so they
    line up with Kalshi's fight-level prop markets."""
    q = question.lower()
    if "go the distance" in q:
        return "go_the_distance"
    if "won by ko or tko" in q:
        return "any_ko_tko"
    if "won by submission" in q:
        return "any_submission"
    if "o/u" in q and "round" in q:
        for n in ("0.5", "1.5", "2.5", "3.5", "4.5"):
            if n in q:
                return f"rounds_{n.replace('.', '_')}"
    return None


# ---------------------------------------------------------------------------
# Polymarket side
# ---------------------------------------------------------------------------


def get_polymarket_props() -> dict:
    """{fight_key: {"title": str, "props": {label: {"price","condition_id"}}}}."""
    r = requests.get(
        f"{GAMMA_API}/events",
        params={"tag_slug": "ufc", "closed": "false", "limit": 100},
        timeout=15,
    )
    if r.status_code != 200:
        return {}
    out = {}
    for e in r.json():
        title = e.get("title", "")
        key = _fight_key(title)  # futures ("Who will…") have no pair -> skipped
        if not key:
            continue
        fps = {}
        for m in e.get("markets", []):
            if m.get("closed"):
                continue
            label = _canonical_prop_label(m.get("question") or "")
            if not label:
                continue
            try:
                p = float(json.loads(m.get("outcomePrices", "[]"))[0])
            except Exception:
                continue
            if float(m.get("volume") or 0) < PM_MIN_VOLUME_USD:
                continue  # ghost price — AMM seed, no real trading
            fps[label] = {"price": p, "condition_id": m.get("conditionId")}
        if fps:
            out[key] = {"title": title, "props": fps}
    return out


# ---------------------------------------------------------------------------
# Kalshi side
# ---------------------------------------------------------------------------


def _kalshi_book(market_ticker: str) -> Optional[dict]:
    """Top-of-book snapshot for one Kalshi market (fractional API: *_dollars).

    Returns {"mid", "ask", "spread", "ask_depth_usd"} for the YES side, or None.
    Kalshi books carry bids only: YES ask = 1 - best NO bid; depth at the ask is
    the size of that best NO level (contracts * ask price in dollars)."""
    r = requests.get(f"{KALSHI_API}/markets/{market_ticker}/orderbook", timeout=10)
    if r.status_code != 200:
        return None
    ob = r.json().get("orderbook_fp", {}) or {}
    yes, no = ob.get("yes_dollars", []), ob.get("no_dollars", [])
    best_yes = max((float(x[0]) for x in yes), default=0.0)
    if best_yes <= 0:
        return None
    best_no_lvl = max(no, key=lambda x: float(x[0]), default=None)
    if best_no_lvl is None:
        return None
    best_no = float(best_no_lvl[0])
    ask = 1.0 - best_no
    depth_usd = float(best_no_lvl[1]) * ask  # contracts at the ask * cost each
    return {
        "mid": (best_yes + ask) / 2,
        "ask": ask,
        "spread": round(ask - best_yes, 4),
        "ask_depth_usd": round(depth_usd, 2),
    }


def _kalshi_event_books(event_ticker: str) -> dict:
    """{market_ticker: book_dict} for every market under a Kalshi event."""
    r = requests.get(f"{KALSHI_API}/events/{event_ticker}", timeout=8)
    if r.status_code != 200:
        return {}
    out = {}
    for m in r.json().get("markets", []):
        tk = m.get("ticker", "")
        book = _kalshi_book(tk)
        if book is not None:
            out[tk] = book
    return out


def get_kalshi_props() -> dict:
    """{fight_key: {"title": str, "props": {label: {"mid","ask","spread","ask_depth_usd","ticker"}}}}.

    Discovers fights from the open KXUFCFIGHT events (no hardcoded fighter map),
    takes the shared {stub} (date+codes) from each event ticker, then reads the
    dedicated prop series that reuse that stub:
      KXUFCMOF-{stub}      -> -KOTKODQ (any KO/TKO), -SUB (any submission)
      KXUFCDISTANCE-{stub} -> -DIST    (goes the distance; same side as PM)
    """
    r = requests.get(
        f"{KALSHI_API}/events",
        params={"series_ticker": "KXUFCFIGHT", "status": "open", "limit": 100},
        timeout=10,
    )
    if r.status_code != 200:
        return {}

    fights = {}  # fight_key -> (stub, title)
    for e in r.json().get("events", []):
        et = e.get("event_ticker", "")
        if not et.startswith("KXUFCFIGHT-"):
            continue
        key = _fight_key(e.get("title", ""))
        if key and key not in fights:
            fights[key] = (et[len("KXUFCFIGHT-") :], e.get("title", ""))

    out = {}
    for key, (stub, title) in fights.items():
        fps = {}
        for tk, book in _kalshi_event_books(f"KXUFCMOF-{stub}").items():
            if tk.endswith("-KOTKODQ"):
                fps["any_ko_tko"] = {**book, "ticker": tk}
            elif tk.endswith("-SUB"):
                fps["any_submission"] = {**book, "ticker": tk}
        for tk, book in _kalshi_event_books(f"KXUFCDISTANCE-{stub}").items():
            if tk.endswith("-DIST"):
                fps["go_the_distance"] = {**book, "ticker": tk}
        if fps:
            out[key] = {"title": title, "props": fps}
    return out


# ---------------------------------------------------------------------------
# Edge computation
# ---------------------------------------------------------------------------


def compute_prop_edges(pm_props: dict, ka_props: dict) -> list:
    edges = []
    for key in pm_props:
        if key not in ka_props:
            continue
        title = pm_props[key]["title"]
        pm, ka = pm_props[key]["props"], ka_props[key]["props"]
        for label in ("any_ko_tko", "any_submission", "go_the_distance"):
            if label not in pm or label not in ka:
                continue
            pm_price = pm[label]["price"]
            kb = ka[label]
            ka_price = kb["mid"]
            # go_the_distance: PM "Go the Distance" YES and Kalshi DIST YES are the
            # SAME event — no inversion. (The earlier `1 - ka_price` flip compared
            # opposite sides and produced phantom edges.)
            edge_pp = (pm_price - ka_price) * 100
            direction = "BUY KALSHI" if edge_pp > 0 else "BUY POLYMARKET"
            tradeable = abs(edge_pp) >= MIN_EDGE_PP
            exec_edge_pp = None
            exec_price = None
            gate_reason = None

            # Spread/depth gate: a 10-90 book gives a "50%" mid that means nothing,
            # and the Kalshi mid is the fair anchor for BOTH directions.
            if kb["spread"] > KA_MAX_SPREAD:
                tradeable = False
                gate_reason = f"kalshi spread {kb['spread'] * 100:.0f}c > {KA_MAX_SPREAD * 100:.0f}c"
            elif kb["ask_depth_usd"] < KA_MIN_DEPTH_USD:
                tradeable = False
                gate_reason = f"kalshi ask depth ${kb['ask_depth_usd']:.0f} < ${KA_MIN_DEPTH_USD:.0f}"

            # Reality-check the KALSHI-buy side: walk to the actual ask and net out
            # the taker fee (0.07*p*(1-p)). A midpoint edge that vanishes at the
            # ask is not tradeable.
            if direction == "BUY KALSHI" and tradeable:
                fee = taker_fee_fraction(kb["ask"], "kalshi")
                ee = pm_price - kb["ask"] - fee
                exec_edge_pp = round(ee * 100, 1)
                exec_price = kb["ask"]
                if exec_edge_pp < MIN_EDGE_PP:
                    tradeable = False
                    gate_reason = f"executable {exec_edge_pp:+.1f}pp < {MIN_EDGE_PP:.0f}pp after ask+fee"

            # Reality-check the PM-buy side against the live CLOB ask.
            if direction == "BUY POLYMARKET" and tradeable and not DRY_RUN:
                cid = pm[label].get("condition_id")
                if cid:
                    res = pee.executable_edge(
                        true_prob=ka_price,
                        side="YES",
                        condition_id=cid,
                        outcome_index=0,
                        category="ufc",
                    )
                    if res.get("available"):
                        ee = res.get("executable_edge")
                        exec_edge_pp = round(ee * 100, 1) if ee is not None else None
                        exec_price = res.get("executable_price")
                        tradeable = bool(res.get("tradeable"))
                        if not tradeable and gate_reason is None:
                            gate_reason = "PM book: not executable at target size"

            edges.append(
                UFCPropEdge(
                    fight=title,
                    prop_type="method_of_victory",
                    label=label,
                    polymarket_price=pm_price,
                    kalshi_price=ka_price,
                    edge_pp=round(abs(edge_pp), 1),
                    direction=direction,
                    tradeable=tradeable,
                    executable_edge_pp=exec_edge_pp,
                    executable_price=exec_price,
                    kalshi_ticker=kb.get("ticker"),
                    kalshi_spread=kb["spread"],
                    kalshi_depth_usd=kb["ask_depth_usd"],
                    pm_condition_id=pm[label].get("condition_id"),
                    gate_reason=gate_reason,
                )
            )
    return sorted(edges, key=lambda e: e.edge_pp, reverse=True)


# ---------------------------------------------------------------------------
# Alerting (canonical helper + per-prop cooldown) â used by the scheduler task
# ---------------------------------------------------------------------------


def _send_alert(message: str) -> bool:
    """Fleet-canonical delivery (CLI-first, HTTP fallback, token from the service
    EnvironmentFile). parse_mode=None -> plain text, dodges the Markdown-400 trap."""
    from scripts.openclaw_alerts import alert_openclaw

    return alert_openclaw(message, channel="telegram", parse_mode=None)


def _alert_conn() -> sqlite3.Connection:
    conn = db_connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ufc_prop_alert_log (
            fight    TEXT NOT NULL,
            label    TEXT NOT NULL,
            last_ts  REAL NOT NULL,
            last_edge REAL NOT NULL,
            PRIMARY KEY (fight, label)
        )
        """
    )
    conn.commit()
    return conn


def _should_alert(conn, edge, now_ts: float) -> bool:
    row = conn.execute(
        "SELECT last_ts, last_edge FROM ufc_prop_alert_log WHERE fight=? AND label=?",
        (edge.fight, edge.label),
    ).fetchone()
    if row is None:
        return True
    last_ts, last_edge = row
    if (now_ts - last_ts) >= COOLDOWN_MINUTES * 60:
        return True
    return abs(edge.edge_pp - last_edge) >= EDGE_CHANGE_PP


def _record_alert(conn, edge, now_ts: float) -> None:
    conn.execute(
        "INSERT INTO ufc_prop_alert_log (fight, label, last_ts, last_edge) VALUES (?,?,?,?) "
        "ON CONFLICT(fight, label) DO UPDATE SET last_ts=excluded.last_ts, last_edge=excluded.last_edge",
        (edge.fight, edge.label, now_ts, edge.edge_pp),
    )
    conn.commit()


def _format_alert(e) -> str:
    msg = (
        f"BOLT UFC PROP EDGE — {e.fight}\n"
        f"{e.label}\n"
        f"PM: {e.polymarket_price:.1%} vs Kalshi: {e.kalshi_price:.1%}\n"
        f"Edge: {e.edge_pp:.1f}pp -> {e.direction}"
    )
    if e.executable_price is not None and e.executable_edge_pp is not None:
        msg += (f"\nExecutable: {e.executable_edge_pp:+.1f}pp fee-adj "
                f"@ {e.executable_price * 100:.0f}c ask")
    if e.direction == "BUY KALSHI" and e.kalshi_ticker:
        msg += (f"\nTicker: {e.kalshi_ticker}"
                f" | spread {e.kalshi_spread * 100:.0f}c"
                f" | depth ${e.kalshi_depth_usd:.0f}")
    return msg


def _log_shadow(e) -> bool:
    """Shadow-log a fired alert (same discipline as the MLB pipeline): entry at
    the EXECUTABLE price, resolution/CLV via the shared shadow tracker. Never
    executes real trades. Returns True if logged."""
    try:
        from signals.shadow_tracker import log_shadow_trade
        from odds.sports_edge_common import p1_confidence
    except Exception:
        return False
    if e.executable_price is None or e.executable_edge_pp is None:
        return False
    if e.direction == "BUY KALSHI":
        platform, market_id = "kalshi", e.kalshi_ticker
    else:
        platform, market_id = "polymarket", e.pm_condition_id
    if not market_id:
        return False
    try:
        return bool(log_shadow_trade({
            "market_id": market_id,
            "market": f"{e.fight[:160]} — {e.label}",
            "platform": platform,
            "side": "YES",
            "price": e.executable_price,
            "confidence": p1_confidence(e.executable_edge_pp / 100.0),
            "days_to_close": 3,
            "volume": 0,
            "confirmations": 1,
            "reasoning": (f"ufc_prop: PM {e.polymarket_price * 100:.0f}% vs Kalshi mid "
                          f"{e.kalshi_price * 100:.0f}% (mid edge {e.edge_pp:.1f}pp, "
                          f"exec {e.executable_edge_pp:+.1f}pp @ {e.executable_price * 100:.0f}c)"),
            "archetype": "sports_prop",
            "strategy": "ufc_prop_edge",
            "category": "ufc",
            "category_tier": "sports",
            "midpoint_price": e.polymarket_price if platform == "polymarket" else e.kalshi_price,
        }))
    except Exception:
        return False


def run_prop_edge_scan() -> dict:
    """Scan + alert entry point for the scheduler (mirrors run_ufc_edge_scan).

    Naturally self-gating on credits: Gamma, Kalshi and CLOB are all free, and
    Kalshi prop series only exist near fight night, so most runs find 0 and send
    nothing. Sole delivery path — do not also run as a cron (shared cooldown
    store is per-process best-effort)."""
    pm_props = get_polymarket_props()
    ka_props = get_kalshi_props()
    edges = compute_prop_edges(pm_props, ka_props)
    tradeable = sorted([e for e in edges if e.tradeable], key=lambda x: x.edge_pp, reverse=True)

    alerts_sent = suppressed = shadows_logged = 0
    if tradeable and not DRY_RUN:
        now_ts = time.time()
        conn = _alert_conn()
        try:
            for e in tradeable:
                if alerts_sent >= MAX_ALERTS_PER_SCAN:
                    suppressed += 1
                    continue
                if not _should_alert(conn, e, now_ts):
                    suppressed += 1
                    continue
                if _send_alert(_format_alert(e)):
                    _record_alert(conn, e, now_ts)
                    alerts_sent += 1
                    if _log_shadow(e):
                        shadows_logged += 1
        finally:
            conn.close()

    return {
        "scanned": bool(pm_props) or bool(ka_props),
        "fights_pm": len(pm_props),
        "fights_ka": len(ka_props),
        "edges_found": len(edges),
        "alerts_sent": alerts_sent,
        "suppressed": suppressed,
        "shadows_logged": shadows_logged,
    }


def display(edges: list):
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    print(f"\n{'=' * 70}")
    print(f"  UFC PROP EDGE SCAN — {now}")
    print(f"  Polymarket Gamma vs Kalshi")
    print(f"{'=' * 70}")
    if not edges:
        print("\n  No prop edges found.")
        return
    for e in edges:
        icon = "BOLT" if e.tradeable else "  "
        print(f"\n  {icon} {e.fight[:55]:55s}")
        print(f"     {e.label:25s} | PM: {e.polymarket_price:.1%} | KA: {e.kalshi_price:.1%}")
        line = f"     Edge: {e.edge_pp:.1f}pp -> {e.direction}"
        if e.executable_edge_pp is not None:
            line += f"  (executable: {e.executable_edge_pp:+.1f}pp @ {e.executable_price * 100:.0f}c)"
        print(line)
        if e.gate_reason:
            print(f"     GATED: {e.gate_reason}")
    actionable = [e for e in edges if e.tradeable]
    print(f"\n{'-' * 70}")
    print(f"  Total: {len(edges)} prop edges  |  Actionable: {len(actionable)}")
    print(f"{'=' * 70}\n")


def main():
    print("\n  Fetching Polymarket Gamma props...")
    pm_props = get_polymarket_props()
    print(f"  Found props for {len(pm_props)} fights")
    print("\n  Fetching Kalshi props...")
    ka_props = get_kalshi_props()
    print(f"  Found props for {len(ka_props)} fights")
    print("\n  Computing prop edges...")
    edges = compute_prop_edges(pm_props, ka_props)
    display(edges)


if __name__ == "__main__":
    main()
