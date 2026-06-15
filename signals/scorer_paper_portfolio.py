#!/usr/bin/env python3
"""scorer_paper_portfolio.py — PAPER-ONLY portfolio + alerting for goalscorer props.

Spec: 02-Projects/Polyclawd/Development/prop-edge-system-spec.md §6 Build-Order
Step 4/5 (the *paper* slice only).

WHAT THIS IS
------------
The execution-layer SIMULATION for anytime-goalscorer prop edges. It records
PAPER positions from already-sized tradeable edges, settles them off the
two-source resolver (signals.scorer_resolution.resolve_scorer), and reports
paper P&L / win-rate / ROI. It also formats + (opt-in) sends a clearly-labelled
"PAPER" Telegram alert for newly-flagged tradeable edges.

WHAT THIS IS NOT — load-bearing
-------------------------------
PAPER ONLY. There is NO real-money path here, by design:
  - US sportsbooks expose no bet-placement API; the real bet is human-manual and
    explicitly OUT OF SCOPE (spec §0).
  - This module never touches the live Polyclawd aggregation, the running CLV
    logger, or its `scorer_snapshot` table. It owns its OWN table
    (`scorer_paper_positions`) in its OWN injectable DB (default `scorer_paper.db`).
  - No live network calls happen at import or in any pure function. The resolver
    is INJECTED (`resolve_open_positions(..., resolver_fn=...)`) so tests pass
    synthetic settlements and burn zero API credits. The Telegram send is opt-in
    behind an explicit `send=True` flag.

Position lifecycle:
    record_positions(sized_bets) → status="open"
    resolve_open_positions(resolver_fn) → won | lost | void | disputed (+ pnl)
    portfolio_report() → counts, paper P&L, win-rate (won/lost only), ROI

P&L convention (decimal odds, 1 unit staked = `stake`):
    won      → pnl = stake * (decimal_price - 1)   (profit only, stake returned)
    lost     → pnl = -stake
    void     → pnl = 0   (push; excluded from win-rate)
    disputed → pnl = 0   (sources disagree; excluded from win-rate)
"""

from __future__ import annotations

import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

# Resolution states reused from the canonical two-source resolver. Imported
# defensively so this module also loads when run standalone outside the package.
try:
    from signals.scorer_resolution import ResolveState  # type: ignore
except Exception:  # pragma: no cover - import-path fallback for standalone runs
    try:
        from scorer_resolution import ResolveState  # type: ignore
    except Exception:  # pragma: no cover

        class ResolveState:  # minimal mirror of the canonical constant set
            YES = "YES"
            NO = "NO"
            DISPUTED = "DISPUTED"
            PENDING = "PENDING"
            UNMATCHED = "UNMATCHED"
            VOID = "VOID"


DEFAULT_DB_PATH = "scorer_paper.db"

# Position statuses (the paper ledger's own lifecycle, distinct from ResolveState).
STATUS_OPEN = "open"
STATUS_WON = "won"
STATUS_LOST = "lost"
STATUS_VOID = "void"
STATUS_DISPUTED = "disputed"

SETTLED_STATUSES = (STATUS_WON, STATUS_LOST, STATUS_VOID, STATUS_DISPUTED)
# Only won/lost count toward win-rate; void/disputed are pushes/no-contests.
DECISIVE_STATUSES = (STATUS_WON, STATUS_LOST)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── SQLite (own table, injectable path) ──────────────────────────────────────
def db_connect(path: Optional[str] = None) -> sqlite3.Connection:
    """Open (creating if needed) the PAPER positions DB and ensure the schema.

    Owns `scorer_paper_positions` ONLY — it never reads or writes the CLV
    logger's `scorer_snapshot` table. The path is injectable; default is a local
    `scorer_paper.db` so tests can hand it a tmp path.
    """
    path = path or DEFAULT_DB_PATH
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE IF NOT EXISTS scorer_paper_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_at TEXT NOT NULL,
            event_title TEXT NOT NULL,
            commence_time TEXT,
            player TEXT NOT NULL,
            decimal_price REAL NOT NULL,
            stake REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            result_value TEXT,
            pnl REAL,
            resolved_at TEXT,
            UNIQUE(event_title, player))"""
    )
    con.commit()
    return con


# ── price coercion ────────────────────────────────────────────────────────────
def _to_decimal_price(bet) -> Optional[float]:
    """Pull a DECIMAL price out of a sized-bet dict.

    Accepts an explicit `decimal_price`/`price` (decimal, > 1.0), or converts an
    American `american`/`best_soft_price` price to decimal. Returns None when no
    usable price is present (the bet is then skipped — a paper position with no
    price can't have P&L)."""
    # object-safe (_get handles dicts AND dataclasses like SizedBet)
    for key in ("decimal_price", "decimal_odds"):
        v = _get(bet, key)
        if v is not None:
            try:
                fv = float(v)
                if fv > 1.0:
                    return fv
            except (TypeError, ValueError):
                pass
    # American → decimal
    am = _get(bet, "american")
    if am is not None:
        try:
            a = float(am)
            if a > 0:
                return 1.0 + a / 100.0
            if a < 0:
                return 1.0 + 100.0 / abs(a)
        except (TypeError, ValueError):
            pass
    # `price`/`best_soft_price` may be decimal or american — auto-detect.
    for key in ("price", "best_soft_price"):
        v = _get(bet, key)
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if abs(fv) >= 100:  # american
            return 1.0 + (fv / 100.0 if fv > 0 else 100.0 / abs(fv))
        if fv > 1.0:  # decimal
            return fv
    return None


def _get(bet, *keys, default=None):
    for k in keys:
        if isinstance(bet, dict) and bet.get(k) is not None:
            return bet[k]
        if not isinstance(bet, dict) and getattr(bet, k, None) is not None:
            return getattr(bet, k)
    return default


# ── record (insert paper positions, dedup one per event/player) ───────────────
def record_positions(sized_bets: Iterable, db: sqlite3.Connection) -> int:
    """Insert PAPER positions from sized tradeable edges. Returns the number of
    NEW positions inserted.

    Each `sized_bet` carries (dict keys, or ScorerEdge-like attrs):
      event_title, player, stake, and a price (decimal_price OR american OR a
      `best_soft_price` that's auto-detected), optional commence_time.

    Dedup is enforced one-per-(event_title, player) via the UNIQUE constraint:
    re-recording the same edge (e.g. a later snapshot of the same prop) is a
    no-op, so the ledger never double-counts a single paper bet.
    """
    inserted = 0
    for bet in sized_bets:
        event_title = _get(bet, "event_title")
        player = _get(bet, "player")
        stake = _get(bet, "stake")
        if event_title is None or player is None or stake is None:
            continue
        try:
            stake = float(stake)
        except (TypeError, ValueError):
            continue
        if stake <= 0:
            continue
        decimal_price = _to_decimal_price(bet)
        if decimal_price is None:
            continue
        commence_time = _get(bet, "commence_time")
        try:
            db.execute(
                """INSERT INTO scorer_paper_positions
                   (opened_at, event_title, commence_time, player, decimal_price,
                    stake, status, result_value, pnl, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)""",
                (_utcnow_iso(), str(event_title), commence_time, str(player), decimal_price, stake, STATUS_OPEN),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            # Duplicate (event_title, player) — already on the paper ledger.
            continue
    db.commit()
    return inserted


# ── settlement (map ResolveState → status + pnl) ──────────────────────────────
def _settle_pnl(status: str, stake: float, decimal_price: float) -> float:
    if status == STATUS_WON:
        return stake * (decimal_price - 1.0)
    if status == STATUS_LOST:
        return -stake
    # void / disputed → push, no P&L.
    return 0.0


def _status_for_state(state: str) -> Optional[str]:
    """Map a resolver verdict to a settled position status, or None if the
    position should stay OPEN (not yet decidable)."""
    if state == ResolveState.YES:
        return STATUS_WON
    if state == ResolveState.NO:
        return STATUS_LOST
    if state == ResolveState.VOID:
        return STATUS_VOID
    if state == ResolveState.DISPUTED:
        return STATUS_DISPUTED
    # PENDING / UNMATCHED → leave open (still resolving / no fixture yet).
    return None


def resolve_open_positions(
    db: sqlite3.Connection,
    resolver_fn: Callable[[dict], str],
) -> int:
    """Settle OPEN paper positions whose match is final.

    `resolver_fn(position_dict) -> ResolveState string` is INJECTED — it wraps
    signals.scorer_resolution.resolve_scorer (which itself takes injected fetched
    events, so no network ever fires from here). It receives the position row as
    a dict {id, event_title, commence_time, player, decimal_price, stake} and
    must return one of the ResolveState constants. A PENDING/UNMATCHED verdict
    leaves the position OPEN (re-checked next run).

    Settlement:
      YES → won  (pnl = stake*(decimal_price-1))
      NO  → lost (pnl = -stake)
      VOID/DISPUTED → push (pnl = 0; excluded from win-rate)

    Returns the number of positions newly settled this call.
    """
    rows = db.execute(
        """SELECT id, event_title, commence_time, player, decimal_price, stake
           FROM scorer_paper_positions WHERE status = ?""",
        (STATUS_OPEN,),
    ).fetchall()

    settled = 0
    for pid, event_title, commence_time, player, decimal_price, stake in rows:
        position = {
            "id": pid,
            "event_title": event_title,
            "commence_time": commence_time,
            "player": player,
            "decimal_price": decimal_price,
            "stake": stake,
        }
        state = resolver_fn(position)
        status = _status_for_state(state)
        if status is None:
            continue  # still pending / unmatched — leave open
        pnl = _settle_pnl(status, float(stake), float(decimal_price))
        db.execute(
            """UPDATE scorer_paper_positions
               SET status = ?, result_value = ?, pnl = ?, resolved_at = ?
               WHERE id = ?""",
            (status, state, pnl, _utcnow_iso(), pid),
        )
        settled += 1
    db.commit()
    return settled


# ── report ────────────────────────────────────────────────────────────────────
def portfolio_report(db: sqlite3.Connection) -> dict:
    """Aggregate the PAPER ledger.

    Returns a dict:
      open               open position count
      settled            settled count (won+lost+void+disputed)
      won / lost         decisive outcomes
      void / disputed    pushes (excluded from win-rate)
      paper_pnl          summed P&L over settled positions
      total_staked       stake summed over DECISIVE (won/lost) positions
      win_rate           won / (won+lost), or None if no decisive settlements
      roi                paper_pnl_decisive / total_staked, or None
    """
    rows = db.execute("SELECT status, stake, pnl FROM scorer_paper_positions").fetchall()

    counts = {STATUS_OPEN: 0, STATUS_WON: 0, STATUS_LOST: 0, STATUS_VOID: 0, STATUS_DISPUTED: 0}
    paper_pnl = 0.0
    decisive_staked = 0.0
    decisive_pnl = 0.0
    for status, stake, pnl in rows:
        counts[status] = counts.get(status, 0) + 1
        if pnl is not None:
            paper_pnl += pnl
        if status in DECISIVE_STATUSES:
            decisive_staked += float(stake or 0.0)
            decisive_pnl += float(pnl or 0.0)

    decisive_n = counts[STATUS_WON] + counts[STATUS_LOST]
    settled_n = sum(counts[s] for s in SETTLED_STATUSES)
    win_rate = (counts[STATUS_WON] / decisive_n) if decisive_n else None
    roi = (decisive_pnl / decisive_staked) if decisive_staked > 0 else None

    return {
        "open": counts[STATUS_OPEN],
        "settled": settled_n,
        "won": counts[STATUS_WON],
        "lost": counts[STATUS_LOST],
        "void": counts[STATUS_VOID],
        "disputed": counts[STATUS_DISPUTED],
        "paper_pnl": round(paper_pnl, 4),
        "total_staked": round(decisive_staked, 4),
        "win_rate": (round(win_rate, 4) if win_rate is not None else None),
        "roi": (round(roi, 4) if roi is not None else None),
    }


# ── alerting (PAPER-labelled, opt-in send) ────────────────────────────────────
def format_alert(sized_bets: Iterable) -> str:
    """Build the plain-text Telegram body for newly-flagged PAPER tradeable edges.

    The message is UNMISTAKABLY a paper/simulation signal — it leads with a
    "[PAPER]" tag and an explicit no-real-money disclaimer — so it can never be
    mistaken for a real-money instruction (US books have no placement API; real
    bets are human-manual)."""
    bets = list(sized_bets)
    lines = [
        "[PAPER] Goalscorer tradeable edges (SIMULATION — no real money)",
    ]
    if not bets:
        lines.append("(no new tradeable edges)")
        return "\n".join(lines)
    for bet in bets:
        event_title = _get(bet, "event_title", default="?")
        player = _get(bet, "player", default="?")
        stake = _get(bet, "stake")
        edge = _get(bet, "edge_pct")
        dec = _to_decimal_price(bet)
        parts = [f"• {player} — {event_title}"]
        if dec is not None:
            parts.append(f"@ {dec:.2f}")
        if stake is not None:
            try:
                parts.append(f"paper-stake {float(stake):.2f}u")
            except (TypeError, ValueError):
                pass
        if edge is not None:
            try:
                parts.append(f"edge {float(edge):+.1f}pp")
            except (TypeError, ValueError):
                pass
        lines.append("  ".join(parts))
    lines.append("Paper ledger only — place manually if you choose to act.")
    return "\n".join(lines)


def send_alert(sized_bets: Iterable, send: bool = False) -> Optional[str]:
    """Format and (opt-in) send the PAPER edges alert to Telegram.

    Sending is OPT-IN behind `send=True` — the default is format-only, so calling
    this in tests (or a dry run) NEVER touches the network. Mirrors the logger's
    send pattern: TELEGRAM_BOT_TOKEN/CHAT_ID from env, plain text via urllib, no
    parse_mode (dodges the Markdown-400 trap). Returns the formatted message.
    """
    msg = format_alert(sized_bets)
    if not send:
        return msg
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("[send] TELEGRAM_BOT_TOKEN/CHAT_ID not set — skipping")
        return msg
    data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=20)
        print("[send] PAPER alert sent")
    except Exception as e:  # pragma: no cover - network failure path
        print(f"[send] failed: {e}")
    return msg


if __name__ == "__main__":  # pragma: no cover
    print("scorer_paper_portfolio: PAPER-ONLY ledger + alerting (no network at import).")
