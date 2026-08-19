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
