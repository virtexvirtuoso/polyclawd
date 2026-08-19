"""
Single source of truth for the POLYCLAWD_MODE flag, risk caps, and credential
getters for the Polymarket CLOB execution layer (Phase C — live weather).

Environment variables are read at call time (not at import) so that tests can
monkeypatch them freely and so that a deployed process can override defaults
via the environment without modifying this file.

Dotenv loading:
  On import, this module tries to load config/polymarket.env if it is present,
  mapping each "KEY=VALUE" line into os.environ WITHOUT overwriting any variable
  that is already set by the process/shell.  The file is optional — its absence
  is silently ignored.  It is listed in .gitignore and must never be committed.
"""

from __future__ import annotations

import os
import math
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: load config/polymarket.env into os.environ (non-destructive)
# ---------------------------------------------------------------------------

# Override via POLYCLAWD_ENV_FILE — used by tests to point at an empty file so
# reloads never inject the real secrets file, and available for a canary env.
_ENV_FILE = Path(
    os.environ.get(
        "POLYCLAWD_ENV_FILE",
        str(Path(__file__).parent.parent / "config" / "polymarket.env"),
    )
)


def _load_env_file() -> None:
    """Parse config/polymarket.env and populate os.environ for any key not
    already set.  Silently skips if the file does not exist.

    Handles:
    - Lines starting with '#' (full-line comments).
    - Values wrapped in matching single or double quotes (inline '#' inside
      quotes is preserved as literal data, not treated as a comment).
    - Inline '#' comments after unquoted values (e.g. KEY=value # comment).
    """
    if not _ENV_FILE.exists():
        return
    for raw_line in _ENV_FILE.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Quoted value: strip enclosing quotes; inline '#' inside is literal.
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        else:
            # Unquoted value: strip inline comment (" #" or "\t#" boundary).
            for sep in (" #", "\t#"):
                if sep in value:
                    value = value[: value.index(sep)]
                    break
            value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()


# ---------------------------------------------------------------------------
# Mode flag
# ---------------------------------------------------------------------------


def mode() -> str:
    """Return "LIVE" only when POLYCLAWD_MODE is explicitly set to "LIVE"
    (case-insensitive).  Everything else — including "STAGING", typos, or
    absent variable — returns "PAPER" (fail-safe default)."""
    raw = os.environ.get("POLYCLAWD_MODE", "PAPER").strip().upper()
    return "LIVE" if raw == "LIVE" else "PAPER"


# ---------------------------------------------------------------------------
# Risk caps (env-overridable, typed, fail-safe defaults)
# ---------------------------------------------------------------------------


def _parse_float(var: str, default: str) -> float:
    """Read an env var and convert to float.  Raises a clear ValueError (not a
    bare conversion error) when the value is present but not numeric."""
    raw = os.environ.get(var, default)
    try:
        return float(raw)
    except (ValueError, TypeError):
        raise ValueError(f"{var}={raw!r} is not a valid number") from None


def _parse_int(var: str, default: str) -> int:
    """Read an env var and convert to int.  Raises a clear ValueError when the
    value is present but not numeric."""
    raw = os.environ.get(var, default)
    try:
        return int(raw)
    except (ValueError, TypeError):
        raise ValueError(f"{var}={raw!r} is not a valid number") from None


def per_trade_cap() -> float:
    """Maximum USD notional per single trade.  Default $100."""
    return _parse_float("POLYCLAWD_WEATHER_PER_TRADE_CAP", "100.0")


def per_trade_frac() -> float:
    """Per-trade cap as a fraction of current bankroll (default 10%).
    The June Mariners trade was 46% of bankroll; one loss like that is fatal."""
    return _parse_float("POLYCLAWD_PER_TRADE_FRAC", "0.10")


def daily_loss_halt() -> float:
    """Halt threshold: stop trading today once realised+unrealised loss
    exceeds this amount in USD.  Default $50."""
    return _parse_float("POLYCLAWD_DAILY_LOSS_HALT", "50.0")


def kill_floor() -> float:
    """Portfolio-level kill floor: stop ALL trading if total equity drops
    below this USD amount.  Default $250."""
    return _parse_float("POLYCLAWD_KILL_FLOOR", "250.0")


def max_deployed_frac() -> float:
    """Maximum fraction of available balance that may be deployed in open
    positions at any one time.  Default 0.60 (60%)."""
    return _parse_float("POLYCLAWD_MAX_DEPLOYED_FRAC", "0.60")


def maker_wait_secs() -> int:
    """How long (seconds) to keep a maker limit order resting before
    cancelling and re-evaluating.  Default 600 (10 min)."""
    return _parse_int("POLYCLAWD_MAKER_WAIT_SECS", "600")


def maker_wait_secs_for(category: str) -> int:
    """Category-aware maker wait window.

    Sports/smart_wallet markets move fast — a 10-min wait means the game
    can start before the order fills or gets cancelled. Use a shorter window
    so the taker fallback fires while the signal is still fresh.

    | category              | default wait |
    |-----------------------|--------------|
    | sports / smart_wallet | 180s (3 min) |
    | weather               | 600s (10 min)|
    | *other*               | maker_wait_secs() global default |
    """
    cat = (category or "").lower()
    if cat in ("sports", "smart_wallet", "soccer", "baseball", "ufc", "nfl", "nba"):
        return _parse_int("POLYCLAWD_MAKER_WAIT_SPORTS_SECS", "180")
    if cat == "weather":
        return _parse_int("POLYCLAWD_MAKER_WAIT_WEATHER_SECS", "600")
    return maker_wait_secs()


def min_taker_edge() -> float:
    """Minimum net-of-fee edge (as a decimal fraction, e.g. 0.02 = 2%) required
    before the executor will cross a TAKER order after a maker leg fails to fill.

    The taker path pays the 5% weather fee, so a positive maker edge can turn
    negative net of fees — this gate drops the trade rather than cross at a loss.

    Default 0.01 (1% net-of-fee floor): crossing taker at ~0 net edge guarantees
    a loss after real slippage on thin weather books, so we require at least a 1%
    net-of-fee cushion before paying the spread + taker fee. Still env-overridable
    via POLYCLAWD_MIN_TAKER_EDGE."""
    return _parse_float("POLYCLAWD_MIN_TAKER_EDGE", "0.01")


# Optional caps — returned as None when unset (Phase J will use these)


# ---------------------------------------------------------------------------
# Entry timing windows — sport-specific close-time gating
# ---------------------------------------------------------------------------
# Prevents entries too far from close (stale lines) or too close (frozen book,
# no time for maker fill or stop-loss to work).
#
# Each window is [min_minutes, max_minutes] before market resolution:
#   min_minutes: don't enter if < this many minutes to close (book freezing,
#                 no time for maker leg or stop evaluation)
#   max_minutes: don't enter if > this many minutes to close (edge not yet
#                 matured, early lines are soft and move against you)
#
# All env-overridable. Defaults match proven paper_portfolio gates.


def weather_close_window() -> tuple[int, int]:
    """Weather: 3h-24h before resolution (matches paper_portfolio proven gates).
    RMSE drops ~60% from 72h to 12h; late entry = sharper forecast."""
    min_min = _parse_int("POLYCLAWD_WEATHER_CLOSE_MIN_MIN", "180")    # 3h
    max_min = _parse_int("POLYCLAWD_WEATHER_CLOSE_MAX_MIN", "1440")  # 24h
    return (min_min, max_min)


def soccer_close_window() -> tuple[int, int]:
    """Soccer match: 30min-7h before kickoff.
    Min 30min matches sports_edge_common.is_stale_event default.
    Max 7h avoids trading on soft early-week lines that haven't sharpened."""
    min_min = _parse_int("POLYCLAWD_SOCCER_CLOSE_MIN_MIN", "30")   # 30min
    max_min = _parse_int("POLYCLAWD_SOCCER_CLOSE_MAX_MIN", "420")  # 7h
    return (min_min, max_min)


def baseball_close_window() -> tuple[int, int]:
    """Baseball (moneyline): 30min-6h before first pitch.
    Min 30min: book freezes in last 15min, need time for maker fill.
    Max 6h: avoids early-morning soft lines that sharpen by afternoon."""
    min_min = _parse_int("POLYCLAWD_BASEBALL_CLOSE_MIN_MIN", "30")  # 30min
    max_min = _parse_int("POLYCLAWD_BASEBALL_CLOSE_MAX_MIN", "360") # 6h
    return (min_min, max_min)


def ufc_close_window() -> tuple[int, int]:
    """UFC: 30min-4h before fight start.
    Shorter max because UFC lines sharpen late (weigh-in info, late money)."""
    min_min = _parse_int("POLYCLAWD_UFC_CLOSE_MIN_MIN", "30")    # 30min
    max_min = _parse_int("POLYCLAWD_UFC_CLOSE_MAX_MIN", "240")   # 4h
    return (min_min, max_min)


def close_window_for(category: str) -> tuple[int, int] | None:
    """Return (min_minutes, max_minutes) before close for the given category,
    or None if no window is configured (trade freely)."""
    cat = (category or "").lower().strip()
    if cat == "weather":
        return weather_close_window()
    if cat in ("soccer", "soccer_match"):
        return soccer_close_window()
    if cat in ("baseball", "mlb"):
        return baseball_close_window()
    if cat in ("ufc", "mma"):
        return ufc_close_window()
    return None


def in_close_window(minutes_to_close: float | None, category: str) -> tuple[bool, str]:
    """Check if `minutes_to_close` falls within the category's entry window.

    Returns (allowed, reason). reason is empty when allowed=True.
    ``minutes_to_close`` can be negative (market already closed) — always rejected.
    None or NaN → allow (no timing info, don't block on missing data).
    """
    if minutes_to_close is None:
        return (True, "")  # no timing info → don't block
    if isinstance(minutes_to_close, float) and math.isnan(minutes_to_close):
        return (True, "")  # NaN → don't block
    if minutes_to_close <= 0:
        return (False, f"market already closed ({minutes_to_close:.0f}min)")
    window = close_window_for(category)
    if window is None:
        return (True, "")  # no window configured → allow
    min_min, max_min = window
    if minutes_to_close < min_min:
        return (False, f"too close: {minutes_to_close:.0f}min < {min_min}min floor for {category}")
    if minutes_to_close > max_min:
        return (False, f"too far: {minutes_to_close:.0f}min > {max_min}min ceiling for {category}")
    return (True, "")


# ---------------------------------------------------------------------------
# Tiered position sizing — scale trade size by executable edge magnitude
# ---------------------------------------------------------------------------
# Instead of flat $10 regardless of edge strength, scale size by edge tier:
#   3-5pp:  base size (conservative, thin edge)
#   5-8pp:  1.5x base (solid edge)
#   8pp+:   2x base (strong edge, capped)
#
# All env-overridable. Caps at per_trade_cap() to respect the global notional limit.
# Soccer also caps at edge.fillable_usd (book depth) — the executor handles that.


def _tiered_size_base() -> float:
    """Base trade size in USD for the lowest edge tier (3-5pp).
    Default $10. Env: POLYCLAWD_TIER_BASE_USD."""
    return _parse_float("POLYCLAWD_TIER_BASE_USD", "10.0")


def _tiered_size_multipliers() -> tuple[float, float, float]:
    """Edge-tier multipliers: (low, mid, high) for 3-5pp / 5-8pp / 8pp+.
    Defaults: 1.0, 1.5, 2.0. Env: POLYCLAWD_TIER_MULT_LOW / MID / HIGH."""
    low = _parse_float("POLYCLAWD_TIER_MULT_LOW", "1.0")
    mid = _parse_float("POLYCLAWD_TIER_MULT_MID", "1.5")
    high = _parse_float("POLYCLAWD_TIER_MULT_HIGH", "2.0")
    return (low, mid, high)


def tiered_size_usd(executable_edge: float, category: str = "") -> float:
    """Return the USD trade size for a given executable edge.

    Tiers (edge as decimal fraction, e.g. 0.05 = 5pp):
      < 0.03 (3pp):  0 — too thin, don't trade
      0.03-0.05:     base × low_mult  (conservative)
      0.05-0.08:     base × mid_mult  (solid)
      >= 0.08:       base × high_mult (strong, capped)

    Capped at tier_size_cap() (separate from per_trade_cap to allow
    tier separation even when per_trade_cap is low). Default $25.
    Env: POLYCLAWD_TIER_SIZE_CAP.
    """
    if executable_edge is None:
        return 0.0  # no edge info — don't trade
    if isinstance(executable_edge, float) and math.isnan(executable_edge):
        return 0.0  # NaN — don't trade
    if executable_edge < 0.03:
        return 0.0  # too thin — caller should drop

    base = _tiered_size_base()
    low_m, mid_m, high_m = _tiered_size_multipliers()

    if executable_edge < 0.05:
        size = base * low_m
    elif executable_edge < 0.08:
        size = base * mid_m
    else:
        size = base * high_m

    # Cap at tier-specific ceiling (separate from per_trade_cap)
    cap = _parse_float("POLYCLAWD_TIER_SIZE_CAP", "25.0")
    if size > cap:
        size = cap

    return round(size, 2)


# ---------------------------------------------------------------------------
# Velocity filter — block entry when edge is collapsing
# ---------------------------------------------------------------------------
# Uses price_movement.classify_movement to detect "converging" pattern
# (soft book catching up to sharp, edge shrinking). When edge is collapsing,
# the edge we see now will be gone by fill time — block entry.
#
# Gate logic:
#   - "converging" with negative consensus delta → BLOCK (edge shrinking)
#   - "diverging" → ALLOW (edge growing, good entry)
#   - "stable" → ALLOW (no movement, edge is real)
#   - "sharp_lead" → ALLOW (sharp moved first, edge is real)
#   - "insufficient" → ALLOW (not enough data, don't block on missing history)
#
# Env: POLYCLAWD_VELOCITY_FILTER (default "1" = on, "0" = off)


def velocity_filter_enabled() -> bool:
    """Master switch for velocity filter. Env: POLYCLAWD_VELOCITY_FILTER=1."""
    return os.environ.get("POLYCLAWD_VELOCITY_FILTER", "1") == "1"


def velocity_check(
    sport: str,
    event_id: str,
    participant: str,
    market_type: str = "moneyline",
) -> tuple[bool, str]:
    """Check price movement velocity for an event.

    Returns (allowed, reason).
    - (True, "") → entry allowed
    - (False, "velocity: <pattern> delta=<X>pp") → blocked

    Only blocks on "converging" with negative consensus delta (edge shrinking).
    All other patterns (including insufficient data) are allowed.
    """
    if not velocity_filter_enabled():
        return (True, "")

    try:
        from odds.price_movement import get_movement, classify_movement

        snapshots = get_movement(sport, event_id, participant, market_type)
        if len(snapshots) < 3:
            return (True, "")  # insufficient data — don't block

        classification = classify_movement(snapshots)
        pattern = classification.get("pattern", "insufficient")
        delta_pp = classification.get("consensus_delta_pp", 0.0)

        # Block only when edge is actively collapsing
        if pattern == "converging" and delta_pp < -1.0:
            return (False, f"velocity: {pattern} delta={delta_pp}pp")

        return (True, "")
    except Exception as e:
        # Never block on velocity check errors — log and allow
        return (True, "")


def max_deployed_usd() -> float | None:
    """Hard cap on total USD deployed across all open positions.
    None when unset (no additional absolute cap beyond max_deployed_frac)."""
    raw = os.environ.get("POLYCLAWD_MAX_DEPLOYED_USD")
    return float(raw) if raw is not None else None


def max_open_markets() -> int | None:
    """Maximum number of simultaneously open positions.
    None when unset."""
    raw = os.environ.get("POLYCLAWD_MAX_OPEN_MARKETS")
    return int(raw) if raw is not None else None


# ---------------------------------------------------------------------------
# Credential getters — return None when the env var is absent so that
# PAPER-mode code can import this module without any credentials configured.
# NEVER log or print these values.
# ---------------------------------------------------------------------------


def bot_eoa_private_key() -> str | None:
    """EOA private key for on-chain order signing (Polygon / CLOB self-custody).
    Source: BOT_EOA_PRIVATE_KEY in config/polymarket.env."""
    return os.environ.get("BOT_EOA_PRIVATE_KEY") or None


def clob_api_key() -> str | None:
    """Polymarket CLOB API key.  Source: CLOB_API_KEY."""
    return os.environ.get("CLOB_API_KEY") or None


def clob_api_secret() -> str | None:
    """Polymarket CLOB API secret.  Source: CLOB_API_SECRET."""
    return os.environ.get("CLOB_API_SECRET") or None


def clob_api_passphrase() -> str | None:
    """Polymarket CLOB API passphrase.  Source: CLOB_API_PASSPHRASE."""
    return os.environ.get("CLOB_API_PASSPHRASE") or None


def live_strategy_allowlist() -> set:
    """Strategy categories allowed to touch real money. Empty set = trade NOTHING.
    Fail-closed by design: a strategy earns its slot via the canary gate doc
    (vault: Live-Canary-Gate-2026-08-18).

    Default covers smart_wallet, baseball_total, soccer_match_3way — the
    canonical category strings used by their respective live executors."""
    raw = os.environ.get("POLYCLAWD_LIVE_STRATEGY_ALLOWLIST", "smart_wallet,baseball_total,soccer_match_3way")
    return {s.strip() for s in raw.split(",") if s.strip()}


def signature_type() -> int:
    """CLOB signature type integer (0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE).
    Source: POLYCLAWD_SIG_TYPE.  Default 0 (EOA / direct self-custody)."""
    return int(os.environ.get("POLYCLAWD_SIG_TYPE", "0"))
