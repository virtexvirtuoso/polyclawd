#!/usr/bin/env python3
"""Phase D4 — Set the on-chain allowances the CLOB needs to trade the bot wallet.

SAFE BY DEFAULT: with no flags it only REPORTS the current allowances and the
plan. Pass --execute to actually send the approval transaction(s) (costs gas).

  COLLATERAL allowance  -> lets the bot BUY (spend USDC). Always set this.
  CONDITIONAL allowance -> lets the bot SELL a specific outcome token. Pass
                           --token-id <id> to approve one; the live executor
                           should also ensure this before its first SELL on a
                           new token (see note at bottom).

Uses the vendor's `update_balance_allowance` (verified in py-clob-client 0.34.6),
which resolves the Exchange/USDC/CTF contract addresses internally — no
hardcoded addresses or raw web3 needed.

Run on the VPS:
    cd /var/www/virtuosocrypto.com/polyclawd
    ./venv/bin/python scripts/set_allowances.py            # report only (safe)
    ./venv/bin/python scripts/set_allowances.py --execute  # set COLLATERAL allowance
    ./venv/bin/python scripts/set_allowances.py --execute --token-id <ID>  # + CONDITIONAL
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the repo root importable when run as `./venv/bin/python scripts/set_allowances.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution import live_config


def _client():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    c = ClobClient(
        "https://clob.polymarket.com",
        key=live_config.bot_eoa_private_key(),
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


def _report(c, AssetType, BalanceAllowanceParams, sig, token_id):
    print("--- current allowances ---")
    try:
        col = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig))
        print(f"COLLATERAL (USDC): balance={col.get('balance')} allowance={col.get('allowance')}")
    except Exception as e:  # noqa: BLE001
        print(f"COLLATERAL read failed: {e}")
    if token_id:
        try:
            con = c.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id, signature_type=sig)
            )
            print(f"CONDITIONAL ({token_id[:10]}…): balance={con.get('balance')} allowance={con.get('allowance')}")
        except Exception as e:  # noqa: BLE001
            print(f"CONDITIONAL read failed: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Set/report Polymarket CLOB allowances (safe by default).")
    ap.add_argument("--execute", action="store_true", help="actually send approval tx(s)")
    ap.add_argument("--token-id", default=None, help="also approve CONDITIONAL allowance for this outcome token")
    args = ap.parse_args()

    if not live_config.bot_eoa_private_key():
        print("BOT_EOA_PRIVATE_KEY not set — do Phase D1 first.")
        return 1
    if not all([live_config.clob_api_key(), live_config.clob_api_secret(), live_config.clob_api_passphrase()]):
        print("CLOB creds not set — run scripts/derive_clob_creds.py (D3) first.")
        return 2

    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    sig = live_config.signature_type()
    c = _client()
    _report(c, AssetType, BalanceAllowanceParams, sig, args.token_id)

    if not args.execute:
        print("\nDRY RUN — no transactions sent.")
        print("Plan with --execute:")
        print("  1) update_balance_allowance(COLLATERAL)            # enables BUY")
        if args.token_id:
            print(f"  2) update_balance_allowance(CONDITIONAL, {args.token_id[:10]}…)  # enables SELL of that token")
        print("Re-run with --execute to apply. Make sure the wallet has a little POL/MATIC for gas.")
        return 0

    print("\n--- EXECUTING (sending on-chain approval tx) ---")
    try:
        c.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig))
        print("COLLATERAL allowance: submitted ✅")
    except Exception as e:  # noqa: BLE001
        print(f"COLLATERAL allowance FAILED: {e}")
        return 3

    if args.token_id:
        try:
            c.update_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=args.token_id, signature_type=sig)
            )
            print(f"CONDITIONAL allowance ({args.token_id[:10]}…): submitted ✅")
        except Exception as e:  # noqa: BLE001
            print(f"CONDITIONAL allowance FAILED: {e}")
            return 4

    print("\n--- allowances after ---")
    _report(c, AssetType, BalanceAllowanceParams, sig, args.token_id)
    print("\nDone. Verify with scripts/check_balance.py (D5).")
    print("NOTE: each outcome token you SELL needs its own CONDITIONAL allowance.")
    print("      Before full-scale, confirm the live executor sets it before a first SELL,")
    print("      or pre-approve target weather tokens with --token-id.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
