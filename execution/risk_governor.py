"""Phase E — Risk Governor.

Pre-trade gate + KILL/HALT state machine.  No money, no network.
All state is persisted to live_portfolio_state so a restart reloads it.

Rule order enforced by check() — first failing rule wins:
  0. strategy_allowlist: intent["category"] not in live_strategy_allowlist()
                      (fail-closed: missing/empty category is rejected)
  1. KILL      : bankroll < kill_floor()
  2. DAILY_HALT: daily_loss + unrealized_loss >= daily_loss_halt()
                 (realised + unrealised, as the docstring promises)
  3. per_trade_cap  : intent["size_usd"] > min(per_trade_cap(), bankroll * per_trade_frac())
  4. max_deployed   : deployed + size > max_deployed_frac() * bankroll
                      (and absolute max_deployed_usd() if set)
  5. max_open_markets: open distinct markets >= max_open_markets() (if set)
  6. ok

KILL is sticky — persists until reset_kill() is called explicitly.
DAILY_HALT persists until reset_day() is called explicitly.

Alerts are fired on ACTIVE→DAILY_HALT and ACTIVE→KILL transitions via the
module-level _alert() function, which tests monkeypatch freely.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from execution import live_config, live_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Alert function — module-level so tests can monkeypatch it.
# Uses the repo's real Discord sender when available; silently degrades.
# ---------------------------------------------------------------------------


def _alert(msg: str) -> None:
    """Send a risk-governor alert. Never raises — alert failures must never
    block a trade decision."""
    try:
        from signals.discord_alerts import _send, COLOR_RED

        embeds = [
            {
                "title": "RISK GOVERNOR",
                "description": msg,
                "color": COLOR_RED,
            }
        ]
        _send(embeds, alert_type="risk_governor", alert_meta={"msg": msg})
    except Exception as exc:
        logger.warning("risk_governor alert failed (non-fatal): %s", exc)

    # Telegram mirror (added 2026-08-21 after /qa audit found this path was
    # Discord-ONLY). State transitions here include ACTIVE->KILL and
    # ACTIVE->DAILY_HALT, i.e. the kill switch firing on live capital and
    # cancel_all() running. If the Discord webhook 4xx'd, that fired silently.
    # Mirrors the pattern already used by alert_position_opened/closed.
    # Deliberately a SEPARATE try: a Discord failure must not skip the mirror.
    try:
        from scripts.alert_formatter import send_telegram

        send_telegram(f"🛑 <b>RISK GOVERNOR</b>\n\n{msg}")
    except Exception as exc:  # noqa: BLE001 — never let alerting break risk control
        logger.warning("risk_governor telegram mirror failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Decision dataclass
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    allowed: bool
    reason: str


# ---------------------------------------------------------------------------
# RiskGovernor
# ---------------------------------------------------------------------------

_VALID_STATES = {"ACTIVE", "DAILY_HALT", "KILL"}


class RiskGovernor:
    """Stateful pre-trade risk gate.

    Constructor:
        RiskGovernor(conn: sqlite3.Connection, mode: str = "PAPER")

    On init, loads the latest live_portfolio_state row if present; otherwise
    starts with ACTIVE state and zero numeric fields (bankroll seeded by
    set_bankroll()).

    Public mutators (each persists state immediately):
        set_bankroll(x: float)
        set_deployed(x: float)
        record_realized_loss(amount: float)   # cumulative for the day
        record_fill(market_id: str, usd: float, **kw)
        reset_kill()    # manual re-enable after KILL
        reset_day()     # reset daily loss counter + clear DAILY_HALT

    Query:
        check(intent: dict) -> Decision
        state() -> str   # "ACTIVE" | "DAILY_HALT" | "KILL"
    """

    def __init__(self, conn: sqlite3.Connection, mode: str = "PAPER") -> None:
        self._conn = conn
        # mode is stored for future use / observability.  The governor enforces
        # all caps in BOTH paper and live mode by design — the flag does not
        # relax any rule.  Phase F/G may use it for logging or alerting.
        self._mode = mode

        # In-memory state — authoritative source for this session.
        # Loaded from DB on init, then kept in sync via _persist().
        saved = live_db.get_state(conn)
        if saved is not None:
            self._bankroll: float = float(saved.get("bankroll") or 0.0)
            self._deployed_usd: float = float(saved.get("deployed_usd") or 0.0)
            self._realized_pnl: float = float(saved.get("realized_pnl") or 0.0)
            # Normalize governor_state: guard against lowercase or unknown DB values
            # so a malformed/lowercase row can't silently bypass a sticky KILL.
            raw = (saved.get("governor_state") or "ACTIVE").upper()
            self._governor_state: str = raw if raw in _VALID_STATES else "ACTIVE"
            self._daily_loss: float = float(saved.get("daily_loss") or 0.0)
            self._ramp_stage: str | None = saved.get("ramp_stage")
        else:
            self._bankroll = 0.0
            self._deployed_usd = 0.0
            self._realized_pnl = 0.0
            self._governor_state = "ACTIVE"
            self._daily_loss = 0.0
            self._ramp_stage = None

        # Unrealised loss — transient mark, NOT persisted; recomputed each cycle
        # by Phase F/G after live_position_tracker.recompute_equity().
        # Positive value = current unrealised loss magnitude; 0 if flat or up.
        self._unrealized_loss: float = 0.0

        # Open market IDs — in-memory only (not persisted; rebuilt on restart
        # by the executor which will call record_fill() for each open position).
        self._open_market_ids: set[str] = set()
        # event_id → market_id map for correlation guard (in-memory, not persisted)
        self._event_id_by_market: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """Append current in-memory state to live_portfolio_state."""
        live_db.set_state(
            self._conn,
            commit=True,
            ts=datetime.now(timezone.utc).isoformat(),
            bankroll=self._bankroll,
            deployed_usd=self._deployed_usd,
            realized_pnl=self._realized_pnl,
            governor_state=self._governor_state,
            daily_loss=self._daily_loss,
            ramp_stage=self._ramp_stage,
        )

    def _transition(self, new_state: str) -> None:
        """Set governor_state and fire an alert on ACTIVE→HALT/KILL transition."""
        if new_state not in _VALID_STATES:
            raise ValueError(f"_transition: invalid state {new_state!r}; must be one of {_VALID_STATES}")
        old = self._governor_state
        self._governor_state = new_state
        if old != new_state and new_state in ("DAILY_HALT", "KILL"):
            # Defense-in-depth: a raising/replaced _alert must NEVER break a
            # trade Decision. _alert is also internally guarded, but guard the
            # call site too so a monkeypatched/buggy _alert can't propagate.
            # ORDER MATTERS: cancel FIRST, notify second. _alert() does up to
            # ~35s of blocking network I/O (Discord 10s + Telegram 15s+5s+15s);
            # delaying order cancellation on live capital to send a message is
            # the wrong trade-off. (/qa critic, 2026-08-21.)
            # On KILL: cancel all open CLOB orders to prevent further exposure
            if new_state == "KILL":
                try:
                    from execution.clob_client import _get_client
                    result = _get_client().cancel_all()
                    cancelled = getattr(result, "canceled", None) or getattr(result, "cancelled", None) or []
                    logger.info("risk_governor: KILL → cancel_all() cancelled %d orders", len(cancelled) if isinstance(cancelled, (list, tuple)) else 0)
                except Exception as cancel_exc:
                    logger.warning("risk_governor: KILL cancel_all failed (non-fatal): %s", cancel_exc)

            try:
                _alert(
                    f"Risk Governor transition: {old} → {new_state} | "
                    f"bankroll={self._bankroll:.2f} daily_loss={self._daily_loss:.2f} "
                    f"deployed={self._deployed_usd:.2f}"
                )
            except Exception as exc:
                logger.warning("risk_governor transition alert failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def set_bankroll(self, x: float) -> None:
        """Set current bankroll. Does NOT auto-clear KILL state."""
        self._bankroll = float(x)
        self._persist()

    def set_deployed(self, x: float) -> None:
        """Set total deployed USD (sum of open position costs)."""
        self._deployed_usd = float(x)
        self._persist()

    def record_realized_loss(self, amount: float) -> None:
        """Add *amount* (positive = loss) to today's cumulative realised loss
        counter.  Transitions to DAILY_HALT if realised + unrealised >= threshold."""
        self._daily_loss += float(amount)
        if self._daily_loss + self._unrealized_loss >= live_config.daily_loss_halt():
            self._transition("DAILY_HALT")
        self._persist()

    def record_fill(self, market_id: str, usd: float, **kw) -> None:
        """Record a confirmed fill: bumps deployed_usd, tracks the open
        market ID, and registers event_id for the correlation guard."""
        self._deployed_usd += float(usd)
        self._open_market_ids.add(market_id)
        event_id = str(kw.get("event_id", "") or "")
        if event_id:
            self._event_id_by_market[market_id] = event_id
        self._persist()

    def record_close(self, market_id: str, usd: float) -> None:
        """Symmetric counterpart to record_fill — decrements deployed_usd,
        removes market from open set, and clears event_id correlation entry."""
        self._deployed_usd = max(0.0, self._deployed_usd - float(usd))
        self._open_market_ids.discard(market_id)
        self._event_id_by_market.pop(market_id, None)
        self._persist()

    def set_realized_pnl(self, value: float) -> None:
        """Set cumulative realised P&L (signed; negative = net loss).

        Observability only — no rule in check() reads this field. It exists so
        the persisted state and the /api/live/governor view agree with
        live_position_tracker.recompute_equity(), which is the authority.
        Before this setter existed the field was loaded once at init and echoed
        back by _persist() forever, freezing it at whatever the DB last held.
        """
        self._realized_pnl = float(value)
        self._persist()

    def set_daily_loss(self, amount: float) -> None:
        """Set today's cumulative realised loss (positive = loss magnitude).

        Idempotent counterpart to record_realized_loss(): callers that derive
        the day's loss from the ledger must NOT accumulate, or every sync cycle
        would double-count. Transitions to DAILY_HALT on the same combined
        realised+unrealised threshold record_realized_loss() uses.
        """
        self._daily_loss = max(0.0, float(amount))
        if self._daily_loss + self._unrealized_loss >= live_config.daily_loss_halt():
            self._transition("DAILY_HALT")
        self._persist()

    def apply_sync(self, *, bankroll: float | None = None,
                   deployed_usd: float | None = None,
                   realized_pnl: float | None = None,
                   daily_loss: float | None = None,
                   unrealized_loss: float | None = None) -> None:
        """Apply a full ledger sync in ONE persisted transaction.

        Every individual setter calls _persist(), so a cron syncing four fields
        opened four write transactions and appended four rows to
        live_portfolio_state every cycle. shadow_trades.db has demonstrated
        lock contention between concurrent sport monitors (mlb_live_monitor and
        cross_sport_drift both hit "database is locked"), so a caller that can
        batch its writes should.

        The DAILY_HALT decision is evaluated ONCE, after every field is
        applied, so it always sees a consistent snapshot. Calling the setters
        individually makes the outcome depend on their order — set_daily_loss()
        evaluates against whatever _unrealized_loss happened to hold at the
        time.
        """
        if bankroll is not None:
            self._bankroll = float(bankroll)
        if deployed_usd is not None:
            self._deployed_usd = float(deployed_usd)
        if realized_pnl is not None:
            self._realized_pnl = float(realized_pnl)
        if unrealized_loss is not None:
            self._unrealized_loss = max(0.0, float(unrealized_loss))
        if daily_loss is not None:
            self._daily_loss = max(0.0, float(daily_loss))

        if self._daily_loss + self._unrealized_loss >= live_config.daily_loss_halt():
            self._transition("DAILY_HALT")
        self._persist()

    def set_unrealized_loss(self, amount: float) -> None:
        """Update the current unrealised loss mark (positive = loss; 0 if flat
        or profitable).  Called by Phase F/G after each recompute_equity cycle.
        NOT persisted — this is a transient mark recomputed every cycle."""
        self._unrealized_loss = max(0.0, float(amount))

    def set_ramp_stage(self, stage: str) -> None:
        """Set the current ramp stage (e.g. "A", "B", "C").  Persisted
        immediately so it survives a restart."""
        self._ramp_stage = stage
        self._persist()

    def reset_kill(self) -> None:
        """Manual re-enable after KILL. Operator must call this explicitly."""
        if self._governor_state == "KILL":
            self._governor_state = "ACTIVE"
            self._persist()

    def reset_day(self) -> None:
        """Reset daily loss counter and clear DAILY_HALT (call at day boundary)."""
        self._daily_loss = 0.0
        if self._governor_state == "DAILY_HALT":
            self._governor_state = "ACTIVE"
        self._persist()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def state(self) -> str:
        """Return current governor state: "ACTIVE" | "DAILY_HALT" | "KILL"."""
        return self._governor_state

    def check(self, intent: dict) -> Decision:
        """Evaluate a trade intent against all risk caps.

        Returns Decision(allowed=True/False, reason=str).
        The reason string always CONTAINS the bracketed token so tests can
        match on it (e.g. "per_trade_cap", "kill_floor", "daily_loss_halt",
        "max_deployed", "max_open_markets").

        Rule order is strict — first failing rule wins.
        """
        size_usd: float = float(intent.get("size_usd", 0))
        market_id: str = str(intent.get("market_id", ""))

        # ── Rule 0: strategy allowlist (fail-closed) ────────────────────
        # The live account traded a K2-killed archetype in July because no
        # strategy gate existed on the live path. Missing category = reject.
        category = str(intent.get("category", "") or "")
        allowed_strategies = live_config.live_strategy_allowlist()
        if category not in allowed_strategies:
            return Decision(
                False,
                f"strategy_allowlist: category {category or '(missing)'} not in {sorted(allowed_strategies)}",
            )

        # ── Rule 1: KILL floor ──────────────────────────────────────────
        # Check KILL state OR newly crossed threshold.
        if self._governor_state == "KILL" or self._bankroll < live_config.kill_floor():
            prior = self._governor_state
            self._transition("KILL")
            if prior != self._governor_state:
                self._persist()
            if self._bankroll < live_config.kill_floor():
                reason = f"kill_floor: bankroll {self._bankroll:.2f} < {live_config.kill_floor():.2f}"
            else:
                reason = "kill_floor: KILL active (sticky — manual reset_kill() required)"
            return Decision(False, reason)

        # ── Rule 2: DAILY_HALT ──────────────────────────────────────────
        # Trips on realised + unrealised combined (as the docstring promises).
        total_loss = self._daily_loss + self._unrealized_loss
        if self._governor_state == "DAILY_HALT" or total_loss >= live_config.daily_loss_halt():
            prior = self._governor_state
            self._transition("DAILY_HALT")
            if prior != self._governor_state:
                self._persist()
            return Decision(
                False,
                f"daily_loss_halt: realised {self._daily_loss:.2f} + unrealised {self._unrealized_loss:.2f}"
                f" = {total_loss:.2f} >= {live_config.daily_loss_halt():.2f}",
            )

        # ── Rule 3: per-trade cap ───────────────────────────────────────
        # Tiered sizing (L2, 2026-07-24) may size up to POLYCLAWD_TIER_SIZE_CAP,
        # so honor whichever flat ceiling is higher — then bound by a fraction
        # of current bankroll (the June Mariners trade was 46% of bankroll; a
        # flat cap alone doesn't scale down as bankroll shrinks).
        try:
            tier_cap = live_config._parse_float("POLYCLAWD_TIER_SIZE_CAP", "25.0")
            flat_cap = max(tier_cap, live_config.per_trade_cap())
        except Exception:
            flat_cap = live_config.per_trade_cap()
        cap = min(flat_cap, self._bankroll * live_config.per_trade_frac())
        # Strict > so that exactly-at-cap is ALLOWED.
        if size_usd > cap:
            return Decision(
                False,
                f"per_trade_cap: {size_usd:.2f} > {cap:.2f} "
                f"(min of flat {live_config.per_trade_cap():.2f}, "
                f"{live_config.per_trade_frac():.0%} of bankroll {self._bankroll:.2f})",
            )

        # ── Rule 4: deployed cap ────────────────────────────────────────
        max_frac = live_config.max_deployed_frac()
        frac_limit = max_frac * self._bankroll
        if self._deployed_usd + size_usd > frac_limit:
            return Decision(
                False,
                f"max_deployed: deployed {self._deployed_usd:.2f} + {size_usd:.2f}"
                f" > frac_limit {frac_limit:.2f} ({int(max_frac * 100)}% of {self._bankroll:.2f})",
            )
        abs_cap = live_config.max_deployed_usd()
        if abs_cap is not None and self._deployed_usd + size_usd > abs_cap:
            return Decision(
                False,
                f"max_deployed: deployed {self._deployed_usd:.2f} + {size_usd:.2f} > absolute cap {abs_cap:.2f}",
            )

        # ── Rule 5: max open markets ────────────────────────────────────
        max_markets = live_config.max_open_markets()
        if max_markets is not None:
            # Count currently open — does NOT include the proposed market_id yet
            open_count = len(self._open_market_ids)
            if open_count >= max_markets:
                return Decision(
                    False,
                    f"max_open_markets: {open_count} open >= limit {max_markets}",
                )

        # ── Rule 5.5: correlation guard ─────────────────────────────────────
        # Block a second position on the same event_id (different market).
        # Bypassed when event_id is absent — safe default.
        event_id = str(intent.get("event_id", "") or "")
        if event_id:
            for open_mid, open_eid in self._event_id_by_market.items():
                if open_eid == event_id and open_mid != market_id:
                    return Decision(
                        False,
                        f"correlation_guard: event {event_id[:24]} already open in {open_mid[:16]}",
                    )

        # ── All rules passed ────────────────────────────────────────────
        return Decision(True, "ok")
