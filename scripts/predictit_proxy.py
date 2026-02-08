#!/usr/bin/env python3
"""
PredictIt Proxy - Fetches from local Mac, syncs to VPS
Run via cron every 30 min to keep data fresh
"""

import json
import urllib.request
import subprocess
from datetime import datetime
from pathlib import Path

PREDICTIT_API = "https://www.predictit.org/api/marketdata/all/"
LOCAL_CACHE = Path(__file__).parent.parent / "data" / "predictit_cache.json"
VPS_PATH = "/var/www/virtuosocrypto.com/polyclawd/data/predictit_cache.json"

def fetch_predictit():
    """Fetch all PredictIt markets."""
    try:
        req = urllib.request.Request(
            PREDICTIT_API,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            return data.get("markets", [])
    except Exception as e:
        print(f"Fetch error: {e}")
        return None

def main():
    print(f"[{datetime.now().isoformat()}] Fetching PredictIt...")
    
    markets = fetch_predictit()
    if markets is None:
        print("Failed to fetch, keeping existing cache")
        return 1
    
    # Save locally
    cache_data = {
        "fetched_at": datetime.utcnow().isoformat(),
        "markets": markets
    }
    
    LOCAL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCAL_CACHE, 'w') as f:
        json.dump(cache_data, f)
    
    print(f"Saved {len(markets)} markets locally")
    
    # Sync to VPS
    try:
        result = subprocess.run(
            ["rsync", "-az", str(LOCAL_CACHE), f"vps:{VPS_PATH}"],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            print("Synced to VPS ✓")
        else:
            print(f"Sync failed: {result.stderr}")
            return 1
    except Exception as e:
        print(f"Sync error: {e}")
        return 1
    
    return 0

if __name__ == "__main__":
    exit(main())
