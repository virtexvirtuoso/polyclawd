"""
smart_wallet_fast_poll.py
─────────────────────────
Lightweight 90-second smart wallet fill scanner.

Decoupled from the heavy whale_scanner (~5 min cycle). Only fetches PM trades
for the last 3 minutes, filters to tracked smart wallet addresses, and passes
fills to smart_wallet_alert.scanner_hook().

At 90s poll cadence:
  - Same-poll fills: ≤ 90s clustering (flash convergence)
  - Adjacent-poll fills: ≤ 3 min clustering (strong)
  - Skip-one-poll fills: ≤ 4.5 min clustering (good)

This gives us genuinely instantaneous convergence detection for live events
compared to the 5-min effective cycle of the full whale scanner.

Called by: scheduler.py task_smart_wallet_fast()
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict, deque
from typing import Optional

logger = logging.getLogger(__name__)

# ── Exit cooldown: don't re-enter a market within 2h of stop-loss exit ────────
_EXIT_COOLDOWN_SECS = 7200  # 2 hours
_EXIT_COOLDOWN_FILE = "/tmp/sw_exit_cooldown.json"


def _is_in_exit_cooldown(token_id: str) -> bool:
    """Return True if this token was recently stopped out and is in cooldown."""
    import json, os, time

    try:
        if not os.path.exists(_EXIT_COOLDOWN_FILE):
            return False
        data = json.loads(open(_EXIT_COOLDOWN_FILE).read())
        ts = data.get(token_id, 0)
        return (time.time() - ts) < _EXIT_COOLDOWN_SECS
    except Exception:
        return False


def register_exit_cooldown(token_id: str) -> None:
    """Record that a position was closed — block re-entry for cooldown window."""
    import json, os, time

    try:
        data = {}
        if os.path.exists(_EXIT_COOLDOWN_FILE):
            data = json.loads(open(_EXIT_COOLDOWN_FILE).read())
        data[token_id] = time.time()
        open(_EXIT_COOLDOWN_FILE, "w").write(json.dumps(data))
    except Exception:
        pass


# How far back to look for trades each poll.
# 2026-08-26: was 180s ("2x poll interval for overlap safety"). That assumed the
# feed's head is current. It is not — the PM trades endpoint pages oldest-first
# and caps at 20 pages (10,000 trades), which now covers only ~8.6 minutes, so the
# freshest trade it can return is ~3.5 min old. A 180s window sat entirely inside
# that lag and fetched 0 trades on EVERY poll (measured: 180s->0, 600s->5297).
# Alert volume decayed 99/day -> 0 as PM volume grew and coverage shrank.
# Re-ingestion across the wider window is handled by _dedupe_unseen_trades below.
_LOOKBACK_SECS = 600  # 10 minutes — must exceed the feed's head lag
# NOTE: _SMART_WALLET_MIN_USD is DEAD — defined here but referenced nowhere. The
# real size gate is THRESHOLD ($1000 cumulative per wallet/market/direction) in
# scripts/smart_wallet_alert.py. Kept only to avoid breaking any external import.
_SMART_WALLET_MIN_USD = 1000  # DEAD — see note above

# Live execution config for smart wallet signals
# Only "entry" alerts are wired — refire has -8.37% avg CLV (2026-06-25 audit, n=70)
_SW_LIVE_ALERT_TYPES = {"entry"}
# Dynamic sizing: fraction of bankroll so it scales with wins.
# At $34 bankroll → $3.45/trade; $150 → $15/trade (cap).
# MUST stay <= POLYCLAWD_PER_TRADE_FRAC (0.11, /etc/default/polyclawd) or the
# risk governor denies every order. Prior 0.25/$5 deadlocked the canary from
# 2026-08-21→25: bankroll decayed below $45.45 and the static $5 floor rose
# above the governor's 11%-of-bankroll cap ($3.79), so 100% of orders were
# denied with "per_trade_cap: 8.62 > 3.79" and nothing alerted.
_SW_LIVE_FRACTION = 0.10  # 10% of bankroll per trade
_SW_LIVE_MIN_USD = 2.0  # floor — must stay <= 0.11 * bankroll (deadlock-free to $18.18)
_SW_LIVE_MAX_USD = 15.0  # cap — safety limit per trade

# Category gate — only follow smart wallets into approved market verticals.
# Gamma sometimes returns category=None (e.g. entertainment/pop-culture markets).
# In that case we require a positive slug/question keyword match to proceed.
_ALLOWED_CATEGORIES = {"sports", "crypto", "politics", "weather", "finance", "economics"}
_ALLOWED_SLUG_KEYWORDS = (
    # Sports
    "mlb",
    "nfl",
    "nba",
    "nhl",
    "ufc",
    "mls",
    "wc",
    "fwc",
    "fifwc",
    "soccer",
    "football",
    "basketball",
    "baseball",
    "tennis",
    "golf",
    "nascar",
    "boxing",
    "mma",
    "hockey",
    # Crypto / finance
    "btc",
    "eth",
    "bitcoin",
    "ethereum",
    "crypto",
    "sol",
    "xrp",
    "doge",
    "fed-rate",
    "gdp",
    "inflation",
    "cpi",
    # Politics
    "trump",
    "biden",
    "election",
    "senate",
    "congress",
    "president",
    "democrat",
    "republican",
    "vote",
)


# Vendor-minimum skip alerting — one Telegram per _MIN_SIZE_ALERT_COOLDOWN.
_MIN_SIZE_ALERT_COOLDOWN = 3600.0
_min_size_alert_last = 0.0


def _alert_min_size_skip(result: dict, size_usd: float, entry_price: float, ref: str) -> None:
    """Page on a vendor-minimum skip (rate-limited). Never raises."""
    global _min_size_alert_last
    try:
        now = time.time()
        if (now - _min_size_alert_last) < _MIN_SIZE_ALERT_COOLDOWN:
            return
        _min_size_alert_last = now
        min_shares = float(result.get("min_shares") or 0)
        got = float(result.get("intended_shares") or 0)
        max_price = (size_usd / min_shares) if min_shares > 0 else 0.0
        from scripts.alert_formatter import send_telegram

        send_telegram(
            "\n".join(
                [
                    "⚠️ <b>ORDER SKIPPED — below Polymarket minimum</b>",
                    f"Ref: {ref}",
                    f"Size ${size_usd:.2f} @ {entry_price:.3f} = {got:.2f} shares",
                    f"Vendor minimum: {min_shares:g} shares",
                    f"At this size only markets priced <= {max_price:.3f} are tradeable.",
                    "Raise per-trade size or accept the reduced universe.",
                ]
            )
        )
    except Exception as exc:  # pragma: no cover - alerting must never break routing
        logger.warning("sw_live: min-size skip alert failed: %s", exc)


# Structural sizing-deadlock tripwire.
# The 2026-08-21..25 outage: bankroll decayed until the static floor exceeded the
# governor's proportional cap, so EVERY order was denied "per_trade_cap" forever.
# Nothing alerted because the watchdog only printed "quiet". A gate that rejects
# 100% of actions is indistinguishable from no signal — so assert the invariant
# floor <= cap directly, at sizing time, before any alert routes.
_DEADLOCK_ALERT_COOLDOWN = 21600.0  # 6h
_deadlock_alert_last = 0.0


def _check_sizing_deadlock(bankroll: float, size_usd: float) -> None:
    """WARN + page if the sizer cannot ever clear the governor. Never raises."""
    global _deadlock_alert_last
    try:
        from execution import live_config

        frac = live_config.per_trade_frac()
        cap = min(live_config.per_trade_cap(), bankroll * frac)
        if size_usd <= cap:
            return

        floor_deadlock = _SW_LIVE_MIN_USD > cap
        recover_at = (_SW_LIVE_MIN_USD / frac) if frac > 0 else float("inf")
        logger.warning(
            "sw_live: SIZING DEADLOCK — size $%.2f > cap $%.2f (bankroll $%.2f, "
            "frac %.0f%%, floor $%.2f). %s",
            size_usd,
            cap,
            bankroll,
            frac * 100,
            _SW_LIVE_MIN_USD,
            (
                f"Floor exceeds cap; NO order can pass until bankroll >= ${recover_at:.2f}."
                if floor_deadlock
                else "Sizer fraction exceeds governor fraction."
            ),
        )

        now = time.time()
        if (now - _deadlock_alert_last) < _DEADLOCK_ALERT_COOLDOWN:
            return
        _deadlock_alert_last = now
        from scripts.alert_formatter import send_telegram

        lines = [
            "\u26d4 <b>CANARY SIZING DEADLOCK — 100% of orders will be denied</b>",
            f"Bankroll ${bankroll:.2f} \u2192 size ${size_usd:.2f}",
            f"Governor cap ${cap:.2f} (min of flat ${live_config.per_trade_cap():.2f}, "
            f"{frac * 100:.0f}% of bankroll)",
        ]
        if floor_deadlock:
            lines += [
                f"Floor ${_SW_LIVE_MIN_USD:.2f} is ABOVE the cap \u2014 no order can pass.",
                f"Self-recovery impossible; needs bankroll >= ${recover_at:.2f} "
                "or a lower floor.",
            ]
        else:
            lines.append(
                f"Sizer fraction {_SW_LIVE_FRACTION * 100:.0f}% exceeds governor {frac * 100:.0f}%."
            )
        send_telegram("\n".join(lines))
    except Exception as exc:  # pragma: no cover - alerting must never break routing
        logger.warning("sw_live: deadlock tripwire failed: %s", exc)


# --------------------------------------------------------------------------- #
# Cross-poll trade dedup
# --------------------------------------------------------------------------- #
_SEEN_TABLE = "smart_wallet_trade_seen"


# Bounded in-process fallback, so a ledger-DB failure still dedups within the
# process instead of passing every trade through to the cumulative threshold.
_MEM_SEEN_MAX = 5000
_mem_seen: "OrderedDict[str, int]" = OrderedDict()  # key -> last sighting ts


def _norm(v) -> str:
    """Normalise a numeric field so the same value keys identically across polls.

    The PM feed is JSON: size/price can arrive as int, float or string, so a bare
    str() makes 100, 100.0 and "100" three different keys for ONE fill — which
    defeats the dedup on exactly the repeats it exists to catch. %.10g collapses
    those to "100" while keeping genuinely different sizes distinct.
    """
    if v is None or v == "":
        return ""
    try:
        return f"{float(v):.10g}"
    except (TypeError, ValueError):
        return str(v)


def _trade_key(t: dict) -> str:
    """Stable identity for one fill.

    transactionHash alone is not unique — one transaction can carry several fills
    (measured: 7,500 trades / 4,856 distinct hashes) — so qualify it with the
    asset, wallet, side and size. Numeric fields go through _norm so JSON type
    drift across polls cannot fork the key.
    """
    return "|".join((
        str(t.get("transactionHash", "")),
        str(t.get("asset", "")),
        str(t.get("proxyWallet", "")),
        str(t.get("side", "")),
        _norm(t.get("size")),
        _norm(t.get("price")),
    ))


def _collapse_intra_batch(keys: list, trades: list) -> tuple:
    """Drop repeats WITHIN one fetch, keeping the first occurrence.

    `fetch_pm_trades_since` pages the live trades feed by `offset` over a
    newest-first stream, so every trade arriving mid-fetch shifts the window and
    re-serves rows across the page boundary. Measured on the production feed
    2026-08-26 (600s window, three trials): 4 pages -> 0 extra rows; 14 pages ->
    1,878 extra rows of 6,500 kept (29%). It is a pagination artefact, not two
    real fills, and it scales with volume — so it lands hardest during exactly
    the bursts the $1000 cumulative threshold exists to catch. Those rates are
    measured on the FULL feed; this function runs on the wallet-filtered subset,
    where per-poll volume is far too small to measure a rate, but the artefact is
    wallet-agnostic. Independently re-measured by a second reviewer: 22,960 rows,
    zero INTRA-page duplicates, every duplicate cross-page and byte-identical on
    all 19 fields including timestamp.

    Applied to BOTH the ledger path and the in-process fallback, so dedup
    quality cannot change character depending on whether sqlite happens to be up.
    """
    seen, k_out, t_out = set(), [], []
    for t, k in zip(trades, keys):
        if k in seen:
            continue
        seen.add(k)
        k_out.append(k)
        t_out.append(t)
    return k_out, t_out


def _mem_record(keys, now: int) -> None:
    """Record keys in the bounded LRU with their sighting time. Never raises."""
    for k in keys:
        _mem_seen[k] = now
        _mem_seen.move_to_end(k)
    while len(_mem_seen) > _MEM_SEEN_MAX:
        _mem_seen.popitem(last=False)


def _mem_prune(now: int) -> None:
    """Expire LRU entries on the SAME retention as the ledger table.

    Without this the LRU never forgets, so after the DB's 4x-window prune the
    fallback path is STRICTER than the primary — it suppresses a trade the
    primary would re-admit. That is an under-count, i.e. the wrong side of the
    fail-open contract.
    """
    cutoff = now - 4 * _LOOKBACK_SECS
    while _mem_seen:
        k, ts = next(iter(_mem_seen.items()))
        if ts >= cutoff:
            break
        _mem_seen.pop(k)


def _mem_filter(keys: list, trades: list, now: int) -> list:
    """Second-layer dedup that survives a ledger-DB failure. Bounded LRU.

    Batch semantics match the DB path exactly: both prune on the same retention,
    collapse intra-batch repeats, then filter against what was already seen. The
    original version filtered against a set it mutated mid-loop, so it returned 1
    where the primary returned 2 — a silent drop occurring only while the ledger
    was already broken. The two paths now agree by construction, not coincidence.
    """
    _mem_prune(now)
    keys, trades = _collapse_intra_batch(keys, trades)
    out = [t for t, k in zip(trades, keys) if k not in _mem_seen]
    _mem_record(keys, now)
    return out


# Fallback-persistence alarm.
#
# One dedup-DB failure is transient noise. A ledger that is gone leaves only a
# process-local LRU suppressing repeats, and the entire symptom set is "the
# accumulator quietly runs hot against the $1000 threshold" — a degraded mode
# that only whispers into a log is the failure class this sweep exists to kill.
#
# ONE signal, deliberately. Earlier revisions combined a consecutive streak, a
# window, a re-arm-on-recovery flag and a delivery flag; every automated review
# round found a fresh defect in the INTERACTIONS (a flapping ledger paged 48x a
# day, 12x a total outage) and none in the dedup itself. A rate over a window
# subsumes what the streak caught — 5 consecutive failures is also 5 within the
# window — and catches the intermittent SQLITE_BUSY contention a consecutive
# counter never sees, because every lucky poll reset it.
_DEDUP_FALLBACK_WINDOW = 40      # recent trade-carrying polls kept
_DEDUP_FALLBACK_ALARM_AT = 5     # degraded polls within that window -> page
_DEDUP_FALLBACK_ALERT_COOLDOWN = 21600.0  # 6h; no re-arm, no shortcut
_DEDUP_RETRY_MAX = 3             # delivery attempts per burst
_DEDUP_RETRY_BACKOFF = 300.0     # 5 min, doubling
_DEDUP_RETRY_PAUSE = 1800.0      # wait before the next burst if all attempts fail
_dedup_recent: "deque" = deque(maxlen=_DEDUP_FALLBACK_WINDOW)
_dedup_alert_last = 0.0
_dedup_retry_attempts = 0
_dedup_retry_next = 0.0


def _dedup_alarm_due() -> bool:
    """True once degradation has stopped being a blip."""
    return sum(_dedup_recent) >= _DEDUP_FALLBACK_ALARM_AT


def _note_dedup_ok() -> None:
    """Record a healthy poll. Called only on a poll the ledger actually served.

    Recording successes is what lets the window DRAIN. Without it the window
    fills with failures and never recovers, so the alarm latches on forever.
    """
    _dedup_recent.append(0)


def _note_dedup_fallback(exc, kept: int, total: int) -> None:
    """Record a degraded poll; page once it stops being a blip. Never raises."""
    global _dedup_alert_last, _dedup_retry_attempts, _dedup_retry_next
    _dedup_recent.append(1)
    logger.warning(
        "smart_wallet_fast_poll: trade dedup DB failed (%s) — fell back to "
        "in-process dedup, %d/%d trades passed (%d/%d recent polls degraded)",
        exc, kept, total, sum(_dedup_recent), len(_dedup_recent),
    )
    if not _dedup_alarm_due():
        return
    try:
        now = time.time()
        if (now - _dedup_alert_last) < _DEDUP_FALLBACK_ALERT_COOLDOWN:
            return
        # Bounded retry. Each send is a subprocess with a 30s timeout (x2 for the
        # plain-text fallback), so retrying at poll cadence would block the 90s
        # scan behind a dead alert channel during the very incident it reports.
        if now < _dedup_retry_next:
            return
        from scripts.alert_formatter import send_telegram

        # Stamp the cooldown only on a DELIVERED page — stamping before the send
        # let one flaky call buy 6h of silence while the fault ran on.
        delivered = send_telegram(
            "\n".join(
                [
                    "\u26a0\ufe0f <b>SMART-WALLET DEDUP DEGRADED — ledger DB unavailable</b>",
                    f"{sum(_dedup_recent)}/{len(_dedup_recent)} recent polls fell "
                    "back to in-process dedup.",
                    f"Last error: {exc}",
                    "Cross-process repeats are NO LONGER suppressed. Every "
                    "fallback ONSET re-admits the overlapping lookback window and "
                    "inflates total_usd against the $1000 cumulative threshold.",
                    "Check shadow_trades.db locks/permissions.",
                ]
            )
        )
        if delivered:
            _dedup_alert_last = now
            _dedup_retry_attempts = 0
            _dedup_retry_next = 0.0
            return
        _dedup_retry_attempts += 1
        if _dedup_retry_attempts >= _DEDUP_RETRY_MAX:
            # Never convert a DELIVERY failure into a 6h silence — pause the
            # retries, leave the cooldown unstamped so the page is still owed.
            _dedup_retry_attempts = 0
            _dedup_retry_next = now + _DEDUP_RETRY_PAUSE
            logger.error(
                "smart_wallet_fast_poll: dedup-degraded page undelivered after "
                "%d attempts — pausing retries for %.0fs",
                _DEDUP_RETRY_MAX, _DEDUP_RETRY_PAUSE,
            )
        else:
            _dedup_retry_next = now + _DEDUP_RETRY_BACKOFF * (
                2 ** (_dedup_retry_attempts - 1)
            )
            logger.warning(
                "smart_wallet_fast_poll: dedup-degraded page NOT delivered "
                "(attempt %d/%d) — retrying after %.0fs",
                _dedup_retry_attempts, _DEDUP_RETRY_MAX,
                _dedup_retry_next - now,
            )
    except Exception as alert_exc:  # pragma: no cover - alerting must never break the scan
        logger.warning(
            "smart_wallet_fast_poll: dedup-fallback alert failed: %s", alert_exc
        )


def _dedupe_unseen_trades(conn, trades: list, now: int) -> list:
    """Return only trades not seen on a previous poll. Never raises.

    Persisted rather than in-memory so a scheduler restart does not re-ingest the
    whole window, and pruned so it cannot grow without bound.

    On a ledger-DB failure this fails OPEN (passes trades through) rather than
    dropping them — silent under-alerting is the failure mode this subsystem was
    built to escape. A bounded in-process LRU backs that path, and the SUCCESS
    path mirrors into it, so the fallback is warm rather than empty at the moment
    the DB first fails.

    Residual, stated honestly: the LRU is per-process, so trades seen by a
    PREVIOUS process — i.e. across a scheduler restart — are not caught while the
    DB is unavailable. The alarm above exists because that residual grows with
    outage length. Note the alarm counts TRADE-CARRYING polls: this function
    returns before touching the DB when a poll is empty, so the window is not a
    wall-clock window. That coupling is correct (no trades, no inflation) but it
    means the time-to-page depends on smart-wallet activity.
    """
    if not trades:
        return trades
    incoming = trades          # pre-collapse, for honest logging on the failure path
    total_in = len(trades)
    try:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_SEEN_TABLE} "
            "(key TEXT PRIMARY KEY, ts INTEGER)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_SEEN_TABLE}_ts ON {_SEEN_TABLE}(ts)"
        )
        # Prune BEFORE the seen-check: doing it after means a key whose retention
        # has just expired still filters its trade on this poll, and only becomes
        # eligible on the next one.
        conn.execute(f"DELETE FROM {_SEEN_TABLE} WHERE ts < ?", (now - 4 * _LOOKBACK_SECS,))
        keys = [_trade_key(t) for t in trades]
        keys, trades = _collapse_intra_batch(keys, trades)
        placeholders = ",".join("?" * len(keys))
        seen = {
            r[0] for r in conn.execute(
                f"SELECT key FROM {_SEEN_TABLE} WHERE key IN ({placeholders})", keys
            )
        }
        fresh = [t for t, k in zip(trades, keys) if k not in seen]
        if fresh:
            conn.executemany(
                f"INSERT OR IGNORE INTO {_SEEN_TABLE} (key, ts) VALUES (?, ?)",
                [(_trade_key(t), now) for t in fresh],
            )
        conn.commit()
        # Mirror into the LRU so the fallback is WARM when the DB first fails.
        # Without this the LRU is empty at every fallback onset and the whole
        # overlapping window is re-admitted in one poll.
        _mem_prune(now)
        _mem_record(keys, now)
        _note_dedup_ok()
        return fresh
    except Exception as exc:  # dedup must never break the scan
        # Fail OPEN, but not blind: the in-process LRU still suppresses repeats
        # seen by THIS process, so a transient DB error cannot silently inflate
        # the cumulative $1000 threshold with a whole re-ingested window.
        try:
            kept = _mem_filter([_trade_key(t) for t in incoming], incoming, now)
        except Exception:
            kept = incoming
        # total_in is the PRE-collapse count: if the exception fired after the
        # collapse, len(trades) is already reduced and under-reports to whoever
        # reads this line during an incident.
        _note_dedup_fallback(exc, len(kept), total_in)
        return kept


# Governor-rail tripwire.
#
# _check_sizing_deadlock covers rule 3 (per_trade_cap) only. The governor has
# FIVE rails and each of the other four can deny 100% of orders while staying
# completely silent — the same shape as the 2026-08-21..25 outage, which ran
# four days because the watchdog only printed "quiet". A gate rejecting 100% of
# actions is indistinguishable from no signal, so assert each rail directly at
# sizing time, before any order routes.
#
# Read-only by construction: this reads the persisted governor row and NEVER
# calls RiskGovernor.check(), which mutates state (it transitions to KILL /
# DAILY_HALT as a side effect of being asked).
_RAIL_ALERT_COOLDOWN = 21600.0  # 6h per rail
_rail_alert_last: dict = {}
_SW_LIVE_STRATEGY = "smart_wallet"  # the intent category the executor sends


def _governor_rail_blocks(bankroll: float, size_usd: float) -> list:
    """Return [(rail, detail, recovery)] for every rail that denies ALL orders.

    Pure apart from one read of the persisted governor row — split out from the
    alerting so it can be tested without a Telegram stub.
    """
    from execution import live_config, live_db

    conn = live_db.connect()
    try:
        st = live_db.get_state(conn) or {}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    state = str(st.get("governor_state") or "ACTIVE").upper()
    deployed = float(st.get("deployed_usd") or 0.0)
    blocked = []

    # Rule 0 — strategy allowlist, fail-closed.
    allow = set(live_config.live_strategy_allowlist())
    if _SW_LIVE_STRATEGY not in allow:
        blocked.append((
            "strategy_allowlist",
            f"'{_SW_LIVE_STRATEGY}' not in {sorted(allow)}",
            "add it to POLYCLAWD_LIVE_STRATEGY_ALLOWLIST",
        ))

    # Rule 1 — KILL. Sticky: survives restarts, needs a MANUAL reset_kill().
    if state == "KILL":
        blocked.append((
            "kill_sticky",
            "governor state is KILL",
            "sticky — requires a manual reset_kill(); nothing clears it automatically",
        ))
    elif bankroll < live_config.kill_floor():
        blocked.append((
            "kill_floor",
            f"bankroll ${bankroll:.2f} < kill_floor ${live_config.kill_floor():.2f}",
            f"needs bankroll >= ${live_config.kill_floor():.2f}",
        ))

    # Rule 2 — DAILY_HALT. position_sync auto-clears it once the loss recedes;
    # if position_sync is not running, this is a permanent unattended halt.
    if state == "DAILY_HALT":
        blocked.append((
            "daily_halt",
            f"governor state is DAILY_HALT (limit ${live_config.daily_loss_halt():.2f})",
            "cleared by position_sync's auto-reset, or a manual reset_day() — "
            "verify position_sync is actually running",
        ))

    # Rule 4 — max_deployed. Denies every order once the book is full.
    cap = live_config.max_deployed_frac() * bankroll
    abs_cap = live_config.max_deployed_usd()
    if abs_cap is not None:
        cap = min(cap, abs_cap)
    if deployed + size_usd > cap:
        blocked.append((
            "max_deployed",
            f"deployed ${deployed:.2f} + ${size_usd:.2f} > cap ${cap:.2f}",
            "close or resolve open positions to free capacity",
        ))

    return blocked


def _check_governor_rails(bankroll: float, size_usd: float) -> None:
    """WARN + page on any rail that denies 100% of orders. Never raises."""
    _check_sizing_deadlock(bankroll, size_usd)  # rule 3
    try:
        blocked = _governor_rail_blocks(bankroll, size_usd)
        if not blocked:
            return
        now = time.time()
        for rail, detail, recovery in blocked:
            logger.warning(
                "sw_live: GOVERNOR RAIL BLOCKING — %s: %s. %s", rail, detail, recovery
            )
            # Rate-limit per RAIL, not globally: a single global cooldown lets
            # the first rail to fire mask a second one for 6h.
            if (now - _rail_alert_last.get(rail, 0.0)) < _RAIL_ALERT_COOLDOWN:
                continue
            from scripts.alert_formatter import send_telegram

            delivered = send_telegram("\n".join([
                "\u26d4 <b>GOVERNOR RAIL BLOCKING — 100% of orders will be denied</b>",
                f"Rail: <b>{rail}</b>",
                detail,
                f"Bankroll ${bankroll:.2f}, intended size ${size_usd:.2f}",
                f"Recovery: {recovery}",
            ]))
            if delivered:
                _rail_alert_last[rail] = now
    except Exception as exc:  # pragma: no cover - alerting must never break routing
        logger.warning("sw_live: governor-rail tripwire failed: %s", exc)


def _route_live_smart_wallet(fired: list, gamma: dict) -> None:
    """Route qualifying smart wallet entry signals to the live executor.

    Only runs when POLYCLAWD_MODE=LIVE. Only wires 'entry' alert_type.
    Uses hybrid maker+taker path: maker-first, taker fallback if net_edge_taker >= min_taker_edge. On
    signals where we don't have a precise taker-edge calculation.

    Calibration basis (2026-06-25): entry alerts 62.1% WR, +6.82% avg CLV, n=177.
    Refire excluded: 48.6% WR, -8.37% avg CLV.
    """
    try:
        from execution import live_config

        if live_config.mode() != "LIVE":
            return
    except Exception:
        return

    logger.info("sw_live: routing %d fired alerts to live executor", len(fired))

    from datetime import datetime, timezone

    # Dynamic sizing: compute once per sweep based on current bankroll
    try:
        from execution.live_db import connect as _ldb_connect

        _ldb = _ldb_connect()
        row = _ldb.execute("SELECT bankroll FROM live_portfolio_state ORDER BY id DESC LIMIT 1").fetchone()
        bankroll = row["bankroll"] if row else 0.0
        _ldb.close()
    except Exception:
        bankroll = 0.0
    size_usd = max(_SW_LIVE_MIN_USD, min(_SW_LIVE_MAX_USD, bankroll * _SW_LIVE_FRACTION))
    logger.info(
        "sw_live: bankroll=$%.2f → size=$%.2f (frac=%.0f%% floor=$%.0f cap=$%.0f)",
        bankroll,
        size_usd,
        _SW_LIVE_FRACTION * 100,
        _SW_LIVE_MIN_USD,
        _SW_LIVE_MAX_USD,
    )
    _check_governor_rails(bankroll, size_usd)

    for rec in fired:
        if rec.get("alert_type") not in _SW_LIVE_ALERT_TYPES:
            logger.info(
                "sw_live: dropped — alert_type %r not in %s",
                rec.get("alert_type"),
                sorted(_SW_LIVE_ALERT_TYPES),
            )
            continue

        condition_id = rec.get("market", "")
        outcome_index = rec.get("outcome_index", 0)  # 0=YES token, 1=NO token
        price_at_alert = float(rec.get("price_at_alert") or 0)
        if not condition_id or price_at_alert <= 0:
            logger.info(
                "sw_live: dropped — unusable rec (condition_id=%r price_at_alert=%s)",
                condition_id[:16],
                price_at_alert,
            )
            continue

        # Category gate: only execute in approved market verticals (Option B).
        # Blocks pop-culture/entertainment markets like the 2026-07-01 Rihanna incident.
        # Uses Gamma API category first, falls back to the rec dict's category (pre-classified
        # by smart_wallet_alert.py), then slug keyword matching.
        gm_data = gamma.get(condition_id, {})
        mkt_category = (gm_data.get("category") or "").lower().strip()
        mkt_slug = (gm_data.get("slug") or "").lower()
        mkt_question = (gm_data.get("question") or "").lower()
        if mkt_category:
            if mkt_category not in _ALLOWED_CATEGORIES:
                logger.info(
                    "sw_live: blocked — category '%s' not in allowlist for %s, skipping",
                    mkt_category,
                    condition_id[:16],
                )
                continue
        else:
            # Fallback 1: use rec dict's pre-classified category (populated by smart_wallet_alert.py)
            rec_category = (rec.get("category") or "").lower().strip()
            if rec_category in _ALLOWED_CATEGORIES:
                pass  # allowed
            # Fallback 2: keyword match in slug/question
            elif any(kw in mkt_slug or kw in mkt_question for kw in _ALLOWED_SLUG_KEYWORDS):
                pass  # allowed
            else:
                logger.info(
                    "sw_live: blocked — no category (rec='%s') + no known pattern (slug='%s') for %s, skipping",
                    rec_category,
                    mkt_slug[:40],
                    condition_id[:16],
                )
                continue

        # Per-archetype track-record gate (2026-08-21). The category gate above
        # asks "is this market vertical allowed?"; this asks "has THIS wallet
        # ever made money in it?". The canary's first live trade followed a
        # wallet into sports while wallet_archetype_pnl already recorded it at
        # -$1,807.97 over 127 sports trades. Abstains when the record is thin.
        try:
            from signals.whale_follower import classify_archetype
            from signals.whale_wallets import archetype_gate_ok, get_meta_db

            _arch = classify_archetype(
                "polymarket", condition_id,
                gm_data.get("question") or rec.get("question") or "")
            _mconn = get_meta_db()
            try:
                _ok, _why = archetype_gate_ok(_mconn, rec.get("wallet", ""), _arch)
            finally:
                _mconn.close()
            if not _ok:
                logger.info("sw_live: blocked — %s for %s, skipping",
                            _why, condition_id[:16])
                continue
            logger.debug("sw_live: archetype gate %s (%s)", _why, _arch)
        except Exception as _ag_exc:  # noqa: BLE001
            # Fail OPEN: a gate that errors must not silently halt all trading,
            # but it must say so rather than look like a clean pass.
            logger.warning("sw_live: archetype gate errored (allowing): %s", _ag_exc)

        try:
            from odds.poly_executable_edge import condition_id_to_token_ids

            token_ids = condition_id_to_token_ids(condition_id)
            if not token_ids or len(token_ids) < 2:
                logger.warning("sw_live: no token_ids for condition %s", condition_id[:16])
                continue
            token_id = token_ids[0] if outcome_index == 0 else token_ids[1]
        except Exception as exc:
            logger.warning("sw_live: token resolution failed for %s: %s", condition_id[:16], exc)
            continue

        # Exit cooldown guard: skip re-entry if we recently stopped out of this market
        if _is_in_exit_cooldown(token_id):
            logger.info("sw_live: %s in exit cooldown (stopped within 2h), skipping", token_id[:16])
            continue

        try:
            from execution import clob_client, live_db, live_executor
            from execution.risk_governor import RiskGovernor

            tick_size = clob_client.get_tick_size(token_id)
        except Exception as exc:
            logger.warning("sw_live: clob setup failed: %s", exc)
            continue

        # Use live BBO instead of stale alert price to avoid maker post-only rejection.
        # If BBO fetch fails, fall back to price_at_alert.
        # Also compute net_edge_taker from the ask side so the taker fallback
        # fires after the maker window if the signal is still fresh.
        net_edge_taker = 0.0
        try:
            from odds.polymarket_clob import get_orderbook

            book = get_orderbook(token_id)
            if book and getattr(book, "bids", None):
                live_bid = float(book.bids[0].price)
                # Post AT the bid — still a resting maker order (does not cross
                # the ask), but gets queue priority over bid-1-tick.
                entry_price = round(live_bid, 2)
                entry_price = max(0.01, min(0.99, entry_price))
            else:
                entry_price = price_at_alert
            # Taker edge: smart wallet entry vs current ask minus ~2% taker fee.
            # Goes negative if price has moved past their fill => taker gate blocks it.
            if book and getattr(book, "asks", None):
                live_ask = float(book.asks[0].price)
                net_edge_taker = round(price_at_alert - live_ask - 0.02, 4)
        except Exception:
            entry_price = price_at_alert

        # Suppress if market has moved too far from alert price (>15pp drift = stale signal)
        drift = abs(entry_price - price_at_alert)
        if drift > 0.15:
            logger.info(
                "sw_live: price drifted %.2f→%.2f (%.0fpp), skipping %s",
                price_at_alert,
                entry_price,
                drift * 100,
                condition_id[:16],
            )
            continue

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        client_order_ref = f"sw-{date_str}-{condition_id[:16]}-{outcome_index}"

        # Dedup gate: block re-entry if this ref already exists in live_open_orders
        # (catches cancelled maker → taker re-fire on next poll, e.g. 2026-07-01 Rihanna pos #7)
        try:
            from execution import live_db as _live_db

            _chk_conn = _live_db.connect()
            try:
                row = _chk_conn.execute(
                    "SELECT status FROM live_open_orders WHERE client_order_ref = ? LIMIT 1", (client_order_ref,)
                ).fetchone()
                if row:
                    logger.info(
                        "sw_live: dedup — ref %s already in live_open_orders (status=%s), skipping",
                        client_order_ref,
                        row[0],
                    )
                    continue
            finally:
                _chk_conn.close()
        except Exception as _dup_exc:
            logger.warning("sw_live: dedup check failed: %s", _dup_exc)

        # Extract event_id for correlation guard (bypassed if gamma doesn't have it)
        event_id = ""
        if condition_id in gamma:
            gm = gamma[condition_id]
            event_id = str(gm.get("eventId") or gm.get("event_id") or "")

        intent = {
            "size_usd": size_usd,
            "market_id": token_id,
            "token_id": token_id,
            "side": "BUY",
            "event_id": event_id,
            "category": "smart_wallet",
        }

        conn = live_db.connect()
        try:
            governor = RiskGovernor(conn, mode="LIVE")
            decision = governor.check(intent)
            if not decision.allowed:
                logger.info("sw_live: governor denied %s: %s", client_order_ref, decision.reason)
                continue

            result = live_executor.execute_intent(
                conn,
                governor,
                token_id=token_id,
                side="BUY",
                fair_price=entry_price,
                size_usd=size_usd,
                tick_size=tick_size,
                neg_risk=bool(rec.get("neg_risk", False)),
                net_edge_taker=net_edge_taker,  # positive when fresh; taker fires after maker window if edge >= min_taker_edge
                client_order_ref=client_order_ref,
                category="smart_wallet",
                market_title=(gm_data.get("question") or rec.get("question") or "")[:120],
                event_id=event_id,
                reasoning={
                    "trigger_source": "smart_wallet",
                    "wallet_address": rec.get("wallet", ""),
                    # wallet_win_rate/wallet_net_pnl intentionally left unset —
                    # not looked up on this hot path (would add a DB query per
                    # trigger); raw_json below preserves the full alert record
                    # so nothing is lost, just not pre-joined.
                    "edge_pct": net_edge_taker,
                    "raw_json": json.dumps(rec, default=str),
                },
            )
            action = result.get("action")
            logger.info(
                "sw_live: %s → %s (entry=%.2f, alert=%.2f) reason=%s",
                client_order_ref,
                action,
                entry_price,
                price_at_alert,
                result.get("reason", ""),
            )
            # Vendor-minimum skip: this is a 100%-rejection condition for every
            # market above the break-even price, so it MUST page, not just log.
            # Rate-limited to one alert per hour per process so a busy sweep
            # cannot spam the channel.
            if action == "skipped_min_size":
                _alert_min_size_skip(result, size_usd, entry_price, client_order_ref)

            # Instant Telegram alert on any fill
            if action in ("maker_filled", "taker_filled"):
                try:
                    from scripts.alert_formatter import send_telegram

                    liq = result.get("liquidity", action)
                    fill_price = result.get("price", entry_price)
                    usd = result.get("usd", 0.0)
                    fee = result.get("fee_paid", 0.0)
                    market_name = rec.get("question") or rec.get("market", token_id[:16])
                    # Enrich with full market name from gamma data
                    gm = gamma.get(condition_id, {})
                    gm_question = gm.get("question") or ""
                    gm_slug = gm.get("slug") or ""
                    display_name = gm_question or market_name
                    # Enrich with category + edge info from gamma data
                    gm = gamma.get(condition_id, {})
                    gm_category = gm.get("category") or ""
                    gm_slug = gm.get("slug") or ""
                    gm_question = gm.get("question") or ""
                    # Use the most descriptive name available
                    display_name = gm_question or market_name
                    emoji = "✅" if action == "maker_filled" else "⚡"
                    lines = [
                        f"{emoji} <b>LIVE FILL</b> ({liq.upper()})",
                        f"Market: {display_name}",
                        f"Side: BUY | Price: {fill_price:.2f} | Size: ${usd:.2f}",
                        f"Fee: ${fee:.4f} | Ref: {client_order_ref}",
                    ]
                    send_telegram("\n".join(lines))
                except Exception as tg_exc:
                    logger.warning("sw_live: telegram fill alert failed: %s", tg_exc)
        except Exception as exc:
            logger.warning("sw_live: execute_intent failed for %s: %s", client_order_ref, exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass


def run() -> dict:
    """Fetch recent PM trades, filter to smart wallets, fire alerts.

    Returns dict with summary stats for logging.
    """
    now = int(time.time())
    since = now - _LOOKBACK_SECS

    # --- Load smart wallet ledger ---
    try:
        from signals.whale_wallets import get_meta_db, get_smart_wallets

        meta_conn = get_meta_db()
        smart = get_smart_wallets(meta_conn)
    except Exception as e:
        logger.warning("smart_wallet_fast_poll: wallet ledger unavailable: %s", e)
        return {"error": str(e)}

    if not smart:
        meta_conn.close()
        return {"smart_wallets": 0, "fills": 0}

    smart_addrs = set(smart.keys())

    # --- Fetch recent PM trades (lightweight — last 3 min only) ---
    try:
        from signals.whale_scanner import fetch_pm_trades_since, fetch_gamma_by_condition
    except ImportError as e:
        meta_conn.close()
        logger.warning("smart_wallet_fast_poll: import error: %s", e)
        return {"error": str(e)}

    try:
        trades = fetch_pm_trades_since(since)
    except Exception as e:
        meta_conn.close()
        logger.warning("smart_wallet_fast_poll: trade fetch failed: %s", e)
        return {"error": str(e)}

    # Filter to smart wallet trades only
    sw_trades = [t for t in trades if t.get("proxyWallet") in smart_addrs]

    # Drop trades already ingested on an earlier poll. The lookback deliberately
    # overlaps successive polls, and the downstream accumulator has no per-trade
    # dedup (trade identity is lost before it reaches _accumulate), so without
    # this the same fill inflates total_usd against the $1000 THRESHOLD.
    _seen_before = len(sw_trades)
    sw_trades = _dedupe_unseen_trades(meta_conn, sw_trades, now)
    _deduped = _seen_before - len(sw_trades)

    if not sw_trades:
        # Previously returned silently. A scanner that can see nothing looked
        # identical to a quiet market for days — say which it is.
        if not trades:
            logger.warning(
                "smart_wallet_fast_poll: feed returned 0 trades for a %ds window — "
                "scanner is BLIND (check feed head lag vs _LOOKBACK_SECS)",
                _LOOKBACK_SECS,
            )
        else:
            logger.info(
                "smart_wallet_fast_poll: %d trades scanned, 0 new smart-wallet fills "
                "(%d already seen)",
                len(trades), _deduped,
            )
        meta_conn.close()
        return {"smart_wallets": len(smart), "trades_scanned": len(trades),
                "fills": 0, "deduped": _deduped}

    # Fetch gamma metadata for markets these wallets traded
    cids = list({t.get("conditionId") for t in sw_trades if t.get("conditionId")})
    try:
        gamma = fetch_gamma_by_condition(cids)
    except Exception as e:
        gamma = {}
        logger.warning("smart_wallet_fast_poll: gamma fetch failed: %s", e)

    # Pass to scanner_hook (handles accumulation, dedup, alert firing, convergence)
    try:
        from scripts.smart_wallet_alert import scanner_hook

        fired = scanner_hook(meta_conn, sw_trades, gamma, smart)
    except Exception as e:
        logger.warning("smart_wallet_fast_poll: scanner_hook failed: %s", e)
        fired = []
    finally:
        try:
            meta_conn.close()
        except Exception:
            pass

    # Route entry-type alerts to live executor (no-op in PAPER mode)
    if fired:
        try:
            _route_live_smart_wallet(fired, gamma)
        except Exception as exc:
            logger.warning("smart_wallet_fast_poll: live routing failed: %s", exc)

    logger.info(
        "smart_wallet_fast_poll: %d trades scanned, %d sw fills, %d alerts fired",
        len(trades),
        len(sw_trades),
        len(fired),
    )
    return {
        "smart_wallets": len(smart),
        "trades_scanned": len(trades),
        "sw_fills": len(sw_trades),
        "alerts_fired": len(fired),
    }
