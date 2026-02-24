"""
Resilient Fetch Wrapper — Retries, circuit breaker, health tracking.

Usage:
    @resilient("polymarket_clob", retries=2, backoff_base=2)
    def fetch_something():
        ...

Or as a wrapper:
    result = resilient_call("kalshi", lambda: httpx.get(url), retries=2)
"""

import functools
import logging
import random
import time
from datetime import datetime, timedelta, timezone

from api.services.source_health import (
    is_circuit_open,
    record_failure,
    record_success,
    set_circuit_open,
)

logger = logging.getLogger(__name__)

# Circuit breaker settings
CIRCUIT_BREAKER_THRESHOLD = 5   # consecutive failures to trip
CIRCUIT_BREAKER_COOLDOWN = 1800  # 30 minutes in seconds


def resilient(source_name: str, retries: int = 2, backoff_base: float = 2.0):
    """Decorator that adds retries, circuit breaker, and health tracking.
    
    Args:
        source_name: Name of the data source (must match TRACKED_SOURCES)
        retries: Number of retry attempts (0 = no retries)
        backoff_base: Base for exponential backoff in seconds
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return resilient_call(
                source_name,
                lambda: func(*args, **kwargs),
                retries=retries,
                backoff_base=backoff_base,
            )
        return wrapper
    return decorator


def resilient_call(source_name: str, fn, retries: int = 2, backoff_base: float = 2.0, default=None):
    """Execute a callable with retries, circuit breaker, and health tracking.
    
    Args:
        source_name: Name of the data source
        fn: Callable to execute
        retries: Number of retry attempts
        backoff_base: Base for exponential backoff
        default: Value to return on total failure (None by default)
    
    Returns:
        Result of fn() on success, or default on failure
    """
    # Circuit breaker check
    if is_circuit_open(source_name):
        logger.warning("resilient: %s circuit breaker OPEN — skipping", source_name)
        return default
    
    last_error = None
    
    for attempt in range(retries + 1):
        t0 = time.monotonic()
        try:
            result = fn()
            latency_ms = (time.monotonic() - t0) * 1000
            record_success(source_name, latency_ms)
            
            if attempt > 0:
                logger.info("resilient: %s succeeded on attempt %d (%.0fms)", source_name, attempt + 1, latency_ms)
            else:
                logger.debug("resilient: %s OK (%.0fms)", source_name, latency_ms)
            
            return result
            
        except Exception as e:
            latency_ms = (time.monotonic() - t0) * 1000
            last_error = e
            error_msg = f"{type(e).__name__}: {e}"
            record_failure(source_name, error_msg)
            
            logger.warning(
                "resilient: %s attempt %d/%d FAILED (%.0fms): %s",
                source_name, attempt + 1, retries + 1, latency_ms, error_msg[:200]
            )
            
            # Check if we should trip the circuit breaker
            _maybe_trip_circuit(source_name)
            
            # Backoff before retry (not on last attempt)
            if attempt < retries:
                delay = backoff_base ** attempt + random.uniform(0, 1)
                logger.debug("resilient: %s backing off %.1fs before retry", source_name, delay)
                time.sleep(delay)
    
    logger.error("resilient: %s ALL %d attempts failed: %s", source_name, retries + 1, last_error)
    return default


def _maybe_trip_circuit(source_name: str):
    """Trip circuit breaker if consecutive failures exceed threshold."""
    from api.services.source_health import get_source_health
    
    health = get_source_health(source_name)
    if not health:
        return
    
    if health["consecutive_failures"] >= CIRCUIT_BREAKER_THRESHOLD:
        until = datetime.now(timezone.utc) + timedelta(seconds=CIRCUIT_BREAKER_COOLDOWN)
        set_circuit_open(source_name, until.isoformat())
        logger.error(
            "resilient: %s CIRCUIT BREAKER TRIPPED after %d failures — cooldown until %s",
            source_name, health["consecutive_failures"], until.isoformat()
        )
