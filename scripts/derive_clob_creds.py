#!/usr/bin/env python3
"""Phase D3 — Derive the Polymarket CLOB API credentials from the bot EOA key.

Idempotent: returns the same creds if they already exist. Prints three
`KEY=value` lines ready to paste into config/polymarket.env. Writes nothing
to disk and never prints the private key.

Run on the VPS:
    cd /var/www/virtuosocrypto.com/polyclawd
    ./venv/bin/python scripts/derive_clob_creds.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable when run as `./venv/bin/python scripts/derive_clob_creds.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution import live_config


def main() -> int:
    key = live_config.bot_eoa_private_key()
    if not key:
        print("BOT_EOA_PRIVATE_KEY not set in config/polymarket.env — do Phase D1 first.")
        return 1

    from py_clob_client.client import ClobClient

    c = ClobClient(
        "https://clob.polymarket.com",
        key=key,
        chain_id=137,
        signature_type=live_config.signature_type(),
    )

    # create_or_derive_api_creds is idempotent (verified against py-clob-client 0.34.6).
    # Fall back to derive/create if a future vendor version renames it.
    creds = None
    last_err = None
    for method in ("create_or_derive_api_creds", "derive_api_key", "create_api_key"):
        fn = getattr(c, method, None)
        if not callable(fn):
            continue
        try:
            creds = fn()
            used = method
            break
        except Exception as e:  # noqa: BLE001
            last_err = f"{method}() -> {e}"

    if creds is None:
        print("Could not derive CLOB creds.")
        if last_err:
            print("last error:", last_err)
        print("available creds methods:", [m for m in dir(c) if "api" in m.lower() or "cred" in m.lower()])
        return 2

    print(f"# derived via ClobClient.{used}()  (idempotent — safe to re-run)")
    print("CLOB_API_KEY=" + creds.api_key)
    print("CLOB_API_SECRET=" + creds.api_secret)
    print("CLOB_API_PASSPHRASE=" + creds.api_passphrase)
    print("\n# Paste the three lines above into config/polymarket.env, then:")
    print("#   chmod 600 config/polymarket.env")
    print("# Next: scripts/set_allowances.py  (D4)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
