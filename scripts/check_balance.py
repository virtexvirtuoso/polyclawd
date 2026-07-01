#!/usr/bin/env python3
"""Phase D5 — Check the bot wallet is trade-ready (READ-ONLY, no transactions).

Prints the EOA address, USDC collateral balance, current allowances, and whether
CLOB API creds are configured. Use it after funding (D2) + creds (D3) + allowances
(D4) to confirm everything is in place before the Phase J canary.

Run on the VPS:
    cd /var/www/virtuosocrypto.com/polyclawd
    ./venv/bin/python scripts/check_balance.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable when run as `./venv/bin/python scripts/check_balance.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution import live_config


def _build_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    key = live_config.bot_eoa_private_key()
    c = ClobClient(
        "https://clob.polymarket.com",
        key=key,
        chain_id=137,
        signature_type=live_config.signature_type(),
    )
    ak, asec, ap = (
        live_config.clob_api_key(),
        live_config.clob_api_secret(),
        live_config.clob_api_passphrase(),
    )
    if ak and asec and ap:
        c.set_api_creds(ApiCreds(api_key=ak, api_secret=asec, api_passphrase=ap))
    return c


def main() -> int:
    key = live_config.bot_eoa_private_key()
    if not key:
        print("BOT_EOA_PRIVATE_KEY not set in config/polymarket.env — do Phase D1 first.")
        return 1

    from eth_account import Account

    addr = Account.from_key(key).address
    print(f"EOA address    : {addr}")
    print(f"signature_type : {live_config.signature_type()}  (0 = EOA self-custody)")

    creds_ok = all([live_config.clob_api_key(), live_config.clob_api_secret(), live_config.clob_api_passphrase()])
    print(f"CLOB creds set : {creds_ok}")
    if not creds_ok:
        print("\n-> Creds missing. Run scripts/derive_clob_creds.py (D3) and paste them in, then re-run this.")
        return 2

    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    c = _build_client()
    try:
        res = c.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=live_config.signature_type())
        )
    except Exception as e:  # noqa: BLE001
        print(f"\nbalance/allowance check FAILED: {e}")
        print("-> Usually means creds are wrong or the wallet has never been seen by the CLOB. Re-check D3.")
        return 3

    bal_raw = res.get("balance") if isinstance(res, dict) else None
    allow_raw = res.get("allowance") if isinstance(res, dict) else None
    try:
        bal = float(bal_raw)
    except (TypeError, ValueError):
        bal = None

    print(f"USDC balance   : {bal_raw}")
    print(f"USDC allowance : {allow_raw}  (must be > 0 to BUY — set in D4)")

    funded = bal is not None and bal > 0
    approved = False
    try:
        approved = float(allow_raw) > 0
    except (TypeError, ValueError):
        approved = False

    print("\n" + "=" * 48)
    if funded and approved and creds_ok:
        print("READY ✅  — funded, creds set, COLLATERAL allowance set.")
        print("Next: Phase J (canary). NOTE: selling an outcome token also needs a")
        print("CONDITIONAL allowance for that token — see scripts/set_allowances.py --token-id.")
        return 0
    missing = []
    if not funded:
        missing.append("funds (D2)")
    if not approved:
        missing.append("COLLATERAL allowance (D4)")
    print("NOT READY ⛔ — still need: " + ", ".join(missing))
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
