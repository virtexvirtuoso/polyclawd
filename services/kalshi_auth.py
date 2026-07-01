"""
Kalshi RSA authentication helper.

Signing format: RSA-PSS, SHA-256, DIGEST_LENGTH salt
Message:        timestamp_ms (str) + METHOD.upper() + path_WITHOUT_query_string
Headers:        KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE

Note: sign the path without query params, but send request with full URL including params.
"""
from __future__ import annotations

import base64
import os
import time
import urllib.request
import json
from functools import lru_cache
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
_DEFAULT_KEY_PATH = "/home/linuxuser/.kalshi/private_key.pem"
_DEFAULT_ENV_FILE = "/home/linuxuser/.config/polyclawd/alerts.env"


def _load_env(path: str = _DEFAULT_ENV_FILE) -> Dict[str, str]:
    env: Dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


@lru_cache(maxsize=1)
def _get_private_key(pem_path: str):
    with open(pem_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign_request(method: str, path: str, key_id: Optional[str] = None, pem_path: Optional[str] = None) -> Dict[str, str]:
    """Return auth headers. path may include query string (stripped for signing)."""
    env = _load_env()
    key_id = key_id or os.environ.get("KALSHI_KEY_ID") or env.get("KALSHI_KEY_ID", "")
    pem_path = pem_path or os.environ.get("KALSHI_PEM_PATH") or env.get("KALSHI_PEM_PATH", _DEFAULT_KEY_PATH)

    private_key = _get_private_key(pem_path)
    ts = str(int(time.time() * 1000))
    # Strip query string from signing path
    sign_path = path.split("?")[0]
    msg = (ts + method.upper() + sign_path).encode("utf-8")
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type": "application/json",
    }


def kalshi_get(path: str, **kwargs) -> Any:
    """Authenticated GET against Kalshi trade API. path = '/trade-api/v2/...'"""
    headers = sign_request("GET", path, **kwargs)
    url = "https://api.elections.kalshi.com" + path
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def get_balance() -> float:
    """Return portfolio balance in dollars."""
    data = kalshi_get("/trade-api/v2/portfolio/balance")
    return float(data.get("balance_dollars", 0))


def get_positions(limit: int = 100) -> list:
    """Return open market positions."""
    data = kalshi_get(f"/trade-api/v2/portfolio/positions?limit={limit}")
    return data.get("market_positions", [])
