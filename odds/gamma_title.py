"""gamma_title.py — resolve a Polymarket condition id (0x...) to its market question.

Used by alert formatters so Telegram messages show human-readable titles
instead of raw hex ids (Alert System Overhaul plan, Task 3.2).

Contract:
* ``resolve_title(market_id) -> str | None`` — NEVER raises.
* Accepts a 0x condition id OR a decimal CLOB token id (>=30 digits —
  live_positions stores token ids in market_id; see plan Task 3.4 audit).
  Anything else (e.g. Kalshi tickers, already human-readable) returns None.
* Caches hits in a ``title_cache`` table in ``storage/shadow_trades.db``
  (same DB as signals/alert_governor.py); failures are NOT cached.
* Gamma lookup: GET /markets?condition_ids=<id>, extract ``question``,
  5-second timeout.
"""

from __future__ import annotations
from config.polymarket_urls import gamma_url  # polyproxy: central URL config

import json
import sqlite3
import time
import urllib.request
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "storage" / "shadow_trades.db"
GAMMA_URL = gamma_url("/markets?condition_ids={}")
GAMMA_TOKEN_URL = gamma_url("/markets?clob_token_ids={}")
TIMEOUT_S = 5


def _cache_get(db_path: Path, market_id: str) -> str | None:
    con = sqlite3.connect(str(db_path), timeout=0.5)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS title_cache ("
            " market_id TEXT PRIMARY KEY,"
            " title     TEXT NOT NULL,"
            " ts        INTEGER NOT NULL)"
        )
        row = con.execute(
            "SELECT title FROM title_cache WHERE market_id = ?", (market_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _cache_put(db_path: Path, market_id: str, title: str) -> None:
    con = sqlite3.connect(str(db_path), timeout=0.5)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS title_cache ("
            " market_id TEXT PRIMARY KEY,"
            " title     TEXT NOT NULL,"
            " ts        INTEGER NOT NULL)"
        )
        con.execute(
            "INSERT OR REPLACE INTO title_cache(market_id, title, ts) VALUES (?, ?, ?)",
            (market_id, title, int(time.time())),
        )
        con.commit()
    finally:
        con.close()


def _fetch_one(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        body = json.loads(resp.read().decode("utf-8", errors="replace"))
    if isinstance(body, list) and body and isinstance(body[0], dict):
        question = body[0].get("question")
        if isinstance(question, str) and question.strip():
            return question.strip()
    return None


def _fetch_question(market_id: str) -> str | None:
    base = GAMMA_TOKEN_URL if market_id.isdigit() else GAMMA_URL
    title = _fetch_one(base.format(market_id))
    if title is None:
        # Gamma's default listing omits closed markets; closed=true FILTERS
        # to closed-only, so it is a fallback, not a default.
        title = _fetch_one(base.format(market_id) + "&closed=true")
    return title


def resolve_title(market_id, db_path: Path | None = None) -> str | None:
    """Resolve a 0x condition id to its Gamma market question. Never raises."""
    try:
        if not isinstance(market_id, str):
            return None
        is_token_id = market_id.isdigit() and len(market_id) >= 30
        if not market_id.startswith("0x") and not is_token_id:
            return None
        path = db_path or DB_PATH

        try:
            cached = _cache_get(path, market_id)
        except Exception:
            cached = None
        if cached:
            return cached

        title = _fetch_question(market_id)
        if title:
            try:
                _cache_put(path, market_id, title)
            except Exception:
                pass  # cache is best-effort; the title is still good
        return title
    except Exception:
        return None


def _outcome_cache_get(db_path: Path, token_id: str) -> str | None:
    con = sqlite3.connect(str(db_path), timeout=0.5)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS outcome_cache ("
            " token_id TEXT PRIMARY KEY,"
            " outcome  TEXT NOT NULL,"
            " ts       INTEGER NOT NULL)"
        )
        row = con.execute(
            "SELECT outcome FROM outcome_cache WHERE token_id = ?", (token_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _outcome_cache_put(db_path: Path, token_id: str, outcome: str) -> None:
    con = sqlite3.connect(str(db_path), timeout=0.5)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS outcome_cache ("
            " token_id TEXT PRIMARY KEY,"
            " outcome  TEXT NOT NULL,"
            " ts       INTEGER NOT NULL)"
        )
        con.execute(
            "INSERT OR REPLACE INTO outcome_cache(token_id, outcome, ts) VALUES (?, ?, ?)",
            (token_id, outcome, int(time.time())),
        )
        con.commit()
    finally:
        con.close()


def _fetch_outcome(token_id: str) -> str | None:
    for suffix in ("", "&closed=true"):
        req = urllib.request.Request(
            GAMMA_TOKEN_URL.format(token_id) + suffix, headers={"User-Agent": "polyclawd/1.0"}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        if not (isinstance(body, list) and body and isinstance(body[0], dict)):
            continue
        toks, outs = body[0].get("clobTokenIds"), body[0].get("outcomes")
        if isinstance(toks, str):
            toks = json.loads(toks)
        if isinstance(outs, str):
            outs = json.loads(outs)
        for t, o in zip(toks or [], outs or []):
            if str(t) == token_id and isinstance(o, str) and o.strip():
                return o.strip()
    return None


def resolve_outcome(token_id, db_path: Path | None = None) -> str | None:
    """Resolve a decimal CLOB token id to the outcome it represents ("Yes", "No",
    a team name, ...). Never raises; failures are not cached."""
    try:
        if not (isinstance(token_id, str) and token_id.isdigit() and len(token_id) >= 30):
            return None
        path = db_path or DB_PATH
        try:
            cached = _outcome_cache_get(path, token_id)
        except Exception:
            cached = None
        if cached:
            return cached
        outcome = _fetch_outcome(token_id)
        if outcome:
            try:
                _outcome_cache_put(path, token_id, outcome)
            except Exception:
                pass
        return outcome
    except Exception:
        return None


def outcome_label(token_id) -> str:
    """Side label for alerts: the outcome the token represents, or "?". Never raises."""
    return resolve_outcome(str(token_id or "")) or "?"
