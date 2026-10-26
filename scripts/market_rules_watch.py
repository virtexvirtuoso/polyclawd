#!/usr/bin/env python3
"""market_rules_watch: rules, resolution source and dispute state of every tracked prediction market.

Tracked = open Kalshi shadow trades (by ticker), open Polymarket shadow trades (by condition id, Gamma
numeric id, or for an empty market_id the CLOB token id title_cache holds for that exact title) and open
live positions (by condition id, else by CLOB token id). Rows with no usable id are counted (n_untracked).

Each run fetches those markets, compares the rules fields with the last stored copy, records every changed
field in rule_events and pages on: a rules / resolution-source / end-date edit, a new UMA dispute, and once
for a market that is already disputed when first seen.
  Polymarket (Gamma /markets, keyed by conditionId): question, description, resolutionSource, endDate,
    umaResolutionStatus(es). UMA status steps are recorded, not paged; only a new dispute pages.
  Kalshi (/markets/{ticker}): title, rules_primary, rules_secondary, close_time, status; settlement_sources
    is never on the market object, so it comes from /events/{event_ticker}, fetched once per distinct event.
    status is recorded, not paged; a close_time change pages only once the market is no longer active.
A doc whose field list changed (a newer version of this script) is re-seeded silently: the differences are
recorded with reseed=1 and only a new dispute still pages. A missing event (404, or no event_ticker) is
counted; its market keeps the settlement_sources it had (or none), so its later arrival is a silent re-seed.

Delivery: messages are written to the outbox in the same transaction that advances rules.doc_json, sent
after commit, and deleted only once sent. Each run first resends outbox rows younger than 24 h and prints,
then drops, older ones.

Failure handling: fetch errors are recorded in runs.error and printed, and the run exits 0, still diffing
what was fetched. A run is FAILING when it fetched fewer than half of the tracked markets; 3 failing runs in a
row page once and the next run that is not failing pages "recovered". Partial errors never page. A Kalshi
ticker that 404s is skipped and counted (n_missing). A non-zero exit means the run itself crashed.
Added 2026-10-26 (vault 05-Infrastructure/Plans/2026-10-23-Venue-Event-Capture-Plan, Task 7).

Cron (every 15 min; no percent sign in the line):
  7,22,37,52 * * * * cd /var/www/virtuosocrypto.com/polyclawd && SENTINEL_TOPIC=data venv/bin/python3 scripts/market_rules_watch.py >> logs/market_rules_watch.log 2>&1 || SENTINEL_TOPIC=data /home/linuxuser/bin/infra-notify "market_rules_watch failed, see polyclawd logs/market_rules_watch.log"
"""
import difflib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHADOW_DB = os.path.join(ROOT, "storage", "shadow_trades.db")
DB_PATH = os.path.join(ROOT, "storage", "market_rules.db")
GAMMA = "https://gamma-api.polymarket.com/markets"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets/"
KALSHI_EVENTS = "https://api.elections.kalshi.com/trade-api/v2/events/"
NOTIFY = os.getenv("MARKET_RULES_NOTIFY", "/home/linuxuser/bin/infra-notify")
UA = "polyclawd-market-rules/1"
TIMEOUT_S = 20
CHUNK = 20
FAIL_STREAK_ALERT = 3
OUTBOX_TTL_MS = 24 * 3600 * 1000
MAX_MSG = 3500          # one Telegram message holds 4096 characters
PM_FIELDS = ("question", "description", "resolutionSource", "endDate", "umaResolutionStatus", "umaResolutionStatuses")
K_FIELDS = ("title", "rules_primary", "rules_secondary", "close_time", "status")
K_EVENT_FIELDS = ("settlement_sources",)
SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (platform TEXT NOT NULL, market TEXT NOT NULL, label TEXT, doc_json TEXT NOT NULL,
    since_ms INTEGER NOT NULL, PRIMARY KEY (platform, market));
CREATE TABLE IF NOT EXISTS rule_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL,
    platform TEXT NOT NULL, market TEXT NOT NULL, field TEXT NOT NULL, before TEXT, after TEXT,
    reseed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, n_tracked INTEGER,
    n_untracked INTEGER, n_fetched INTEGER, n_missing INTEGER, n_ev_fetched INTEGER, n_ev_missing INTEGER,
    n_events INTEGER, failing INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS alert_state (name TEXT PRIMARY KEY, alerted INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, text TEXT NOT NULL);
"""


def _classify(v):
    """('cid' | 'id' | 'token' | None, value). Gamma ids are short integers, CLOB token ids 70+ digits."""
    v = (v or "").strip()
    if v.startswith("0x") and len(v) > 2:
        return "cid", v
    if v.isdigit():
        return ("id" if len(v) < 20 else "token"), v
    return None, v


def tracked(shadow_db=SHADOW_DB):
    """(kalshi tickers, PM condition ids, PM Gamma ids, PM CLOB token ids, n_untracked open PM rows)."""
    con = sqlite3.connect(f"file:{shadow_db}?mode=ro", uri=True, timeout=30)
    try:
        k = {r[0] for r in con.execute("SELECT DISTINCT market_id FROM shadow_trades WHERE resolved=0 "
                                       "AND platform='kalshi' AND market_id<>''")}
        found = {"cid": set(), "id": set(), "token": set()}
        n_untracked = 0
        for market_id, title in con.execute("SELECT market_id, market FROM shadow_trades WHERE resolved=0 "
                                            "AND platform='polymarket'").fetchall():
            kind, v = _classify(market_id)
            if kind is None and not (market_id or "").strip() and title:
                row = con.execute("SELECT market_id FROM title_cache WHERE title=? ORDER BY ts DESC LIMIT 1",
                                  (title,)).fetchone()
                kind, v = _classify(row[0] if row else None)
            if kind is None:
                n_untracked += 1
            else:
                found[kind].add(v)
        for market_id, token_id in con.execute("SELECT market_id, token_id FROM live_positions "
                                               "WHERE status='open'").fetchall():
            kind, v = _classify(market_id)
            if kind == "cid":
                found["cid"].add(v)
            elif _classify(token_id)[0] == "token":
                found["token"].add(token_id.strip())
            else:
                n_untracked += 1
    finally:
        con.close()
    return k, found["cid"], found["id"], found["token"], n_untracked


def _get(url, timeout=TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_pm(cids, ids, tokens, errors):
    """Gamma markets for these ids, deduped on conditionId. Gamma returns a closed market only with
    closed=true, so every chunk is asked twice. A failing request appends to errors; the rest still run."""
    out, seen = [], set()
    for key, values in (("condition_ids", sorted(cids)), ("id", sorted(ids)), ("clob_token_ids", sorted(tokens))):
        for i in range(0, len(values), CHUNK):
            for closed in ("false", "true"):
                params = [(key, v) for v in values[i:i + CHUNK]] + [("closed", closed), ("limit", CHUNK)]
                try:
                    markets = _get(GAMMA + "?" + urllib.parse.urlencode(params))
                except Exception as e:
                    errors.append(f"gamma {key} chunk {i // CHUNK} closed={closed}: {type(e).__name__}: {e}"[:300])
                    continue
                for m in markets:
                    cid = m.get("conditionId")
                    if cid and cid not in seen:
                        seen.add(cid)
                        out.append(m)
    return out


def _kalshi(base, ticker, key):
    try:
        return _get(base + urllib.parse.quote(ticker))[key]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def fetch_k(ticker):
    """The Kalshi market, or None when Kalshi no longer has it (404)."""
    return _kalshi(KALSHI, ticker, "market")


def fetch_event(event_ticker):
    """The Kalshi event (it carries settlement_sources), or None when Kalshi has no such event (404)."""
    return _kalshi(KALSHI_EVENTS, event_ticker, "event")


def infra_notify(text):
    """True only when infra-notify reports delivery (exit 0). SENTINEL_TOPIC on the cron line picks the topic."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        print("notifier suppressed under pytest", file=sys.stderr)
        return False
    try:
        return subprocess.run([NOTIFY, text], timeout=60, capture_output=True).returncode == 0
    except Exception as e:
        print(f"notify failed: {e}", file=sys.stderr)
        return False


def _n_disputes(doc):
    raw = doc.get("umaResolutionStatuses") or "[]"
    try:
        statuses = json.loads(raw) if isinstance(raw, str) else raw
        return sum(1 for s in statuses if s == "disputed")
    except (TypeError, ValueError):
        return str(raw).count("disputed")


def _pages(platform, field, doc):
    """Whether a change to this field is worth a page (every change is recorded either way)."""
    if platform == "polymarket":
        return field not in ("umaResolutionStatus", "umaResolutionStatuses")   # disputes page on their own
    if field == "status":
        return False
    if field == "close_time":
        return doc.get("status") != "active"
    return True


def _text(v):
    return v if isinstance(v, str) else json.dumps(v, sort_keys=True, ensure_ascii=False)


def _snippet(before, after, cap=600):
    """The removed and added words, not the whole field (a description runs to thousands of characters)."""
    a, b = re.split(r"(\s+)", _text(before)), re.split(r"(\s+)", _text(after))
    parts = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if "".join(a[i1:i2]).strip():
            parts.append("- " + "".join(a[i1:i2]).strip()[:300])
        if "".join(b[j1:j2]).strip():
            parts.append("+ " + "".join(b[j1:j2]).strip()[:300])
    return "\n".join(parts)[:cap]


def _pack(msgs):
    """Messages packed into texts of at most MAX_MSG characters."""
    texts, cur = [], "market rules watch:"
    for m in msgs:
        m = m[:MAX_MSG - 30]
        if len(cur) + 2 + len(m) > MAX_MSG:
            texts.append(cur)
            cur = "market rules watch (cont.):"
        cur += "\n\n" + m
    texts.append(cur)
    return texts


def _flush_outbox(con, notify, now_ms, ids=None):
    """Print and drop rows older than OUTBOX_TTL_MS, then send the rest (only `ids` when given); a row is
    deleted only once its send succeeded."""
    with con:
        for oid, text in con.execute("SELECT id, text FROM outbox WHERE ts_ms < ? ORDER BY id",
                                     (now_ms - OUTBOX_TTL_MS,)).fetchall():
            print(f"OUTBOX DROPPED (unsent for over 24 h), message follows:\n{text}", flush=True)
            con.execute("DELETE FROM outbox WHERE id=?", (oid,))
    for oid, text in con.execute("SELECT id, text FROM outbox ORDER BY id").fetchall():
        if ids is not None and oid not in ids:
            continue
        if notify(text):
            with con:
                con.execute("DELETE FROM outbox WHERE id=?", (oid,))
        else:
            print(f"notify failed; outbox row {oid} kept for the next run", flush=True)


def _check_streak(con, failing, error, notify):
    """Page once when FAIL_STREAK_ALERT runs in a row were failing, once when a run is not failing again."""
    row = con.execute("SELECT alerted FROM alert_state WHERE name='fetch'").fetchone()
    alerted = bool(row and row[0])
    if not failing:
        if alerted and notify("market rules watch recovered: fetching rules again."):
            with con:
                con.execute("INSERT OR REPLACE INTO alert_state VALUES ('fetch', 0)")
        return
    recent = [r[0] for r in con.execute("SELECT failing FROM runs ORDER BY id DESC LIMIT ?", (FAIL_STREAK_ALERT,))]
    if not alerted and len(recent) == FAIL_STREAK_ALERT and all(recent):
        if notify(f"market rules watch FAILING: {FAIL_STREAK_ALERT} runs in a row fetched under half of the "
                  f"tracked markets, so rule edits and disputes may be missed.\nlast error: {error}\ndb: {DB_PATH}"):
            with con:
                con.execute("INSERT OR REPLACE INTO alert_state VALUES ('fetch', 1)")


def _fetch_kalshi(tickers, fetch_k, fetch_ev, errors):
    """([(ticker, label, doc, carry_fields)], n_missing markets, n events fetched, n events missing)."""
    markets, n_missing = [], 0
    for t in sorted(tickers):
        try:
            m = fetch_k(t)
        except Exception as e:
            errors.append(f"kalshi {t}: {type(e).__name__}: {e}"[:300])
            continue
        if m is None:
            n_missing += 1
        else:
            markets.append((t, m))
    events, n_ev_missing = {}, sum(1 for _, m in markets if not m.get("event_ticker"))
    for et in sorted({m["event_ticker"] for _, m in markets if m.get("event_ticker")}):
        try:
            ev = fetch_ev(et)
        except Exception as e:
            errors.append(f"kalshi event {et}: {type(e).__name__}: {e}"[:300])
            continue
        if ev is None:
            n_ev_missing += 1
        else:
            events[et] = ev
    out = []
    for t, m in markets:
        doc = {f: m.get(f) for f in K_FIELDS}
        ev = events.get(m.get("event_ticker"))
        if ev is not None:
            doc.update({f: ev.get(f) for f in K_EVENT_FIELDS})
        out.append((t, m.get("title") or t, doc, () if ev is not None else K_EVENT_FIELDS))
    return out, n_missing, len(events), n_ev_missing


def run(shadow_db=SHADOW_DB, db_path=DB_PATH, fetch_pm=None, fetch_k=None, fetch_ev=None, notify=None, now_ms=None):
    fetch_pm = fetch_pm or globals()["fetch_pm"]   # resolved at call time, not bound as defaults at import
    fetch_k = fetch_k or globals()["fetch_k"]
    fetch_ev = fetch_ev or fetch_event
    notify = notify or infra_notify
    now_ms = now_ms or int(time.time() * 1000)
    con = sqlite3.connect(db_path, timeout=30)
    con.executescript(SCHEMA)
    _flush_outbox(con, notify, now_ms)   # what an earlier run could not send

    k, cids, ids, tokens, n_untracked = tracked(shadow_db)
    n_tracked = len(k) + len(cids) + len(ids) + len(tokens)
    docs, errors = [], []
    try:
        for m in fetch_pm(cids, ids, tokens, errors):
            docs.append(("polymarket", m["conditionId"], m.get("slug") or m.get("question"),
                         {f: m.get(f) for f in PM_FIELDS}, ()))
    except Exception as e:
        errors.append(f"gamma: {type(e).__name__}: {e}"[:300])
    k_docs, n_missing, n_ev_fetched, n_ev_missing = _fetch_kalshi(k, fetch_k, fetch_ev, errors)
    docs += [("kalshi", t, label, doc, carry) for t, label, doc, carry in k_docs]
    failing = bool(n_tracked) and 2 * len(docs) < n_tracked
    if failing:
        errors.insert(0, f"fetched {len(docs)} of {n_tracked} tracked markets")
    error = "; ".join(errors)[:1000] if errors else None

    msgs, n_events, new_ids = [], 0, set()
    with con:
        for platform, market, label, doc, carry in docs:
            row = con.execute("SELECT doc_json FROM rules WHERE platform=? AND market=?", (platform, market)).fetchone()
            old = json.loads(row[0]) if row else None
            for f in carry:   # not fetched this run: keep what is stored, or leave the field out
                if old is not None and f in old:
                    doc[f] = old[f]
            js = json.dumps(doc, sort_keys=True, ensure_ascii=False)
            if old is None:
                con.execute("INSERT INTO rules VALUES (?,?,?,?,?)", (platform, market, label, js, now_ms))
                if platform == "polymarket" and _n_disputes(doc):
                    msgs.append(f"Polymarket {label} [{market}]: already disputed when first tracked "
                                f"({doc.get('umaResolutionStatuses')}).")
                continue
            if old == doc:
                continue
            reseed = set(old) != set(doc)   # a newer field list, not a market event
            for f in sorted(set(old) | set(doc)):
                if old.get(f) == doc.get(f):
                    continue
                con.execute("INSERT INTO rule_events (ts_ms, platform, market, field, before, after, reseed) "
                            "VALUES (?,?,?,?,?,?,?)", (now_ms, platform, market, f, json.dumps(old.get(f)),
                                                       json.dumps(doc.get(f)), int(reseed)))
                n_events += 1
                if f == "umaResolutionStatuses" and _n_disputes(doc) > _n_disputes(old):
                    msgs.append(f"Polymarket {label} [{market}]: DISPUTED ({doc.get(f)}).")
                elif not reseed and _pages(platform, f, doc):
                    msgs.append(f"{platform.capitalize()} {label} [{market}]: {f} changed\n"
                                f"{_snippet(old.get(f), doc.get(f))}")
            con.execute("UPDATE rules SET doc_json=?, label=?, since_ms=? WHERE platform=? AND market=?",
                        (js, label, now_ms, platform, market))
        for text in (_pack(msgs) if msgs else []):
            new_ids.add(con.execute("INSERT INTO outbox (ts_ms, text) VALUES (?,?)", (now_ms, text)).lastrowid)
        con.execute("INSERT INTO runs (ts_ms, n_tracked, n_untracked, n_fetched, n_missing, n_ev_fetched, "
                    "n_ev_missing, n_events, failing, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (now_ms, n_tracked, n_untracked, len(docs), n_missing, n_ev_fetched, n_ev_missing, n_events,
                     int(failing), error))
    if new_ids:
        _flush_outbox(con, notify, now_ms, ids=new_ids)
    _check_streak(con, failing, error, notify)
    n_outbox = con.execute("SELECT count(*) FROM outbox").fetchone()[0]
    con.close()
    print(f"{datetime.fromtimestamp(now_ms / 1000, timezone.utc):%Y-%m-%dT%H:%M:%SZ} tracked={n_tracked} "
          f"untracked={n_untracked} fetched={len(docs)} missing={n_missing} events_fetched={n_ev_fetched} "
          f"events_missing={n_ev_missing} changes={n_events} msgs={len(msgs)} outbox={n_outbox}"
          + (" FAILING" if failing else "") + (f" ERROR {error}" if error else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(run())
