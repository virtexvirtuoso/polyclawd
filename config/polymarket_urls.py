"""
Central Polymarket URL configuration.

Single source of truth for all Polymarket API endpoints. All modules should
import from here instead of hardcoding URLs.

Override behavior:
- Set POLYPROXY_BASE env var to route reads through the CF Worker proxy.
- If POLYPROXY_BASE is unset or empty, reads go directly to Polymarket
  (via the WireGuard tunnel). This is the safe default.
- Writes ALWAYS go directly to Polymarket via the tunnel — the Worker
  geo-blocks writes, so we never route writes through it.

Example:
    # .env or systemd Environment=
    POLYPROXY_BASE=https://polyproxy.virtuoso-polyproxy.workers.dev

Phase 2: deployed in shadow mode (no env var set = direct path).
Phase 3: env var set on scheduler service → reads go through Worker.

Design decisions (2026-08-24 scope review):
- /clob/prices is NOT cached (sub-5s TTLs net-negative on CF edge).
- /relayer-v2 is NEVER routed through Worker (writes/relayer must stay on tunnel).
- POST/DELETE methods NEVER go through Worker (writes geo-blocked).
- Worker hard-blocks /relayer/* paths (403).
"""

import os
import re
from typing import Final

# --- Worker proxy base (set via env var) -------------------------------------

_POLYPROXY_BASE = os.environ.get("POLYPROXY_BASE", "").rstrip("/").strip()
PROXY_ENABLED: Final[bool] = bool(_POLYPROXY_BASE)

# --- Upstream origins (always direct, never proxied) -------------------------

# Writes — these MUST go via the WireGuard tunnel. Worker geo-blocks writes.
CLOB_ORIGIN: Final[str] = "https://clob.polymarket.com"
RELAYER_ORIGIN: Final[str] = "https://relayer-v2.polymarket.com"

# --- Read endpoints (proxied when PROXY_ENABLED, direct otherwise) ------------
#
# Map each upstream origin to a path-prefix on the Worker.
# Worker routes:
#   /clob/*      → https://clob.polymarket.com/*
#   /gamma/*     → https://gamma-api.polymarket.com/*
#   /data/*      → https://data-api.polymarket.com/*
#   /xtracker/*  → https://xtracker.polymarket.com/*

_READ_ORIGINS = {
    "gamma-api": "https://gamma-api.polymarket.com",
    "clob":      "https://clob.polymarket.com",
    "data-api":  "https://data-api.polymarket.com",
    "xtracker":  "https://xtracker.polymarket.com",
}

# When proxy is disabled, reads use the origin directly.
# When enabled, reads use POLYPROXY_BASE/<prefix>/<path>.

def _make_read_urls(origin_key: str) -> tuple[str, str]:
    """Return (direct_url, proxied_url) for the given origin key."""
    direct = _READ_ORIGINS[origin_key]
    if PROXY_ENABLED:
        proxied = f"{_POLYPROXY_BASE}/{origin_key.split('-')[0]}"
    else:
        proxied = direct
    return direct, proxied


# Effective read bases (what callers should use)
_DIRECT_GAMMA, GAMMA_API = _make_read_urls("gamma-api")
_DIRECT_CLOB,  CLOB_API  = _make_read_urls("clob")        # reads only
_DIRECT_DATA,  POLYMARKET_DATA_API = _make_read_urls("data-api")
_DIRECT_XTRACKER, XTRACKER_API = _make_read_urls("xtracker")

# Writes — always direct via tunnel, never via Worker
# Exposed as RELAYER_URL and CLOB_WRITE_URL for clarity.
RELAYER_URL: Final[str] = RELAYER_ORIGIN
CLOB_WRITE_URL: Final[str] = CLOB_ORIGIN


# --- Method-aware URL builder (the safe API) ---------------------------------

def clob_url(path: str, *, write: bool = False) -> str:
    """Build a clob.polymarket.com URL.

    write=True  → always direct via tunnel (POST/DELETE, /data/orders, etc.)
    write=False → via Worker if PROXY_ENABLED, else direct.

    path should start with '/' e.g. '/tick-size?token_id=X'
    """
    if write:
        return f"{CLOB_WRITE_URL}{path}"
    return f"{CLOB_API}{path}"


def gamma_url(path: str) -> str:
    """Build a gamma-api.polymarket.com URL (read-only, cacheable)."""
    return f"{GAMMA_API}{path}"


def data_url(path: str) -> str:
    """Build a data-api.polymarket.com URL (read-only, cacheable)."""
    return f"{POLYMARKET_DATA_API}{path}"


def xtracker_url(path: str) -> str:
    """Build an xtracker.polymarket.com URL (read-only, cacheable)."""
    return f"{XTRACKER_API}{path}"


def relayer_url(path: str) -> str:
    """Build a relayer-v2.polymarket.com URL (always direct, never proxied)."""
    return f"{RELAYER_URL}{path}"


# --- Egress guard (see scope Egress Guard Regression section) ----------------

_WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
_WRITE_CLOB_PATHS = (
    "/order",          # POST/DELETE order
    "/data/orders",    # our own orders (real-time, not cacheable)
    "/data/order",     # single order status
    "/auth",            # API key derive
    "/balance-allowance",
)


def assert_safe_url(url: str, method: str = "GET") -> None:
    """Raise if a write is routed through the Worker, or a relayer call is proxied.

    This guard complements assert_egress_tunneled() — it catches the case where
    a write accidentally gets the proxied URL via the read helpers.
    """
    method = method.upper()
    parsed = url.split("?", 1)[0]

    # Relayer must NEVER be proxied
    if PROXY_ENABLED and parsed.startswith(_POLYPROXY_BASE):
        if "/relayer/" in parsed or parsed.endswith("/relayer"):
            raise RuntimeError(
                f"relayer path routed through Worker — this must not happen: {url}"
            )

    # Write methods must NEVER go through Worker
    if method in _WRITE_METHODS and PROXY_ENABLED and parsed.startswith(_POLYPROXY_BASE):
        # The only acceptable proxied write would be... none. All writes must tunnel.
        raise RuntimeError(
            f"{method} write routed through Worker — writes must go via tunnel: {url}"
        )

    # Proxied clob write paths (even on GET) are also forbidden
    # (e.g. /data/orders is real-time, must not be cached)
    if PROXY_ENABLED and parsed.startswith(_POLYPROXY_BASE):
        for wp in _WRITE_CLOB_PATHS:
            if wp in parsed:
                raise RuntimeError(
                    f"write-path {wp} routed through Worker — must stay on tunnel: {url}"
                )


# --- Debug introspection -----------------------------------------------------

def status() -> dict:
    """Return current URL routing state — useful for logs and health checks."""
    return {
        "proxy_enabled": PROXY_ENABLED,
        "proxy_base": _POLYPROXY_BASE or None,
        "gamma_api": GAMMA_API,
        "clob_api": CLOB_API,
        "data_api": POLYMARKET_DATA_API,
        "xtracker_api": XTRACKER_API,
        "relayer_url": RELAYER_URL,
        "clob_write_url": CLOB_WRITE_URL,
    }


__all__ = [
    "PROXY_ENABLED",
    "GAMMA_API",
    "CLOB_API",
    "POLYMARKET_DATA_API",
    "XTRACKER_API",
    "RELAYER_URL",
    "CLOB_WRITE_URL",
    "clob_url",
    "gamma_url",
    "data_url",
    "xtracker_url",
    "relayer_url",
    "assert_safe_url",
    "status",
]

# --- SDK monkey-patch (Phase 2d) ---------------------------------------------
# The polymarket-client SDK has hardcoded URLs in environments.PRODUCTION.
# Patch them at import time so SDK calls also route through the Worker.
# Uses object.__setattr__ to bypass the frozen=True dataclass restriction.
# Safety: only patches when POLYPROXY_BASE is set; otherwise no-op.
# Relayer URL is NEVER patched (writes must stay on tunnel).

def _patch_polymarket_sdk() -> None:
    """Patch polymarket-client SDK to use Worker URLs when proxy is enabled."""
    if not PROXY_ENABLED:
        return
    try:
        from polymarket import environments as _env
        prod = _env.PRODUCTION
        # Bypass frozen dataclass
        object.__setattr__(prod, "clob_url", clob_url(""))
        object.__setattr__(prod, "gamma_url", gamma_url(""))
        object.__setattr__(prod, "data_url", data_url(""))
        # Do NOT patch relayer_url — relayer must stay on tunnel
        # Do NOT patch WS URLs — Worker does not proxy websockets
    except ImportError:
        # SDK not installed (e.g. test env) — no-op
        pass
    except Exception as e:
        # Log but do not crash — the patched code paths will fall back to direct
        import logging
        logging.getLogger("polyproxy").warning(
            "Failed to monkey-patch polymarket-client SDK: %s", e
        )


_patch_polymarket_sdk()
