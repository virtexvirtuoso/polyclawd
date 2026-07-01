#!/usr/bin/env python3
"""Cache warmer — hits slow endpoints to keep responses pre-cached."""
import httpx
import time
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("cache_warmer")

BASE = "http://localhost:8420"
# Each entry: "path" (90s default timeout) or ("path", timeout_seconds)
SLOW_ENDPOINTS = [
    "/api/signals",
    "/api/signals/copy-trade",
    "/api/arb-scan",
    # Elections overlay populates policy_pulse_cache.json + crypto_money_cache.json,
    # which /api/signals/clarity reads from. 180s because full overlay is expensive.
    ("/api/signals/elections", 180),
    "/api/signals/clarity",
]

def warm():
    results = []
    for entry in SLOW_ENDPOINTS:
        if isinstance(entry, tuple):
            ep, timeout = entry
        else:
            ep, timeout = entry, 90
        url = f"{BASE}{ep}"
        t0 = time.time()
        try:
            r = httpx.get(url, timeout=timeout)
            elapsed = time.time() - t0
            status = "OK" if r.status_code == 200 else f"HTTP {r.status_code}"
            log.info(f"{ep} → {status} in {elapsed:.1f}s")
            results.append((ep, status, elapsed))
        except Exception as e:
            elapsed = time.time() - t0
            log.info(f"{ep} → FAIL ({e}) in {elapsed:.1f}s")
            results.append((ep, "FAIL", elapsed))
    return results

if __name__ == "__main__":
    warm()
