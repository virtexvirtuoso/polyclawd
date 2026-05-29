#!/bin/bash
# Push fresh PredictIt data from Mac to VPS.
# PredictIt blocks datacenter IPs (403) but works from residential.
# Mac fetches, then rsyncs to VPS pushed cache location.
#
# Cron: */30 * * * * /Users/ffv_macmini/Desktop/polyclawd/scripts/push-predictit-cache.sh

set -euo pipefail

API="https://www.predictit.org/api/marketdata/all/"
LOCAL_CACHE="/tmp/predictit_pushed.json"
VPS_DEST="vps:/var/www/virtuosocrypto.com/polyclawd/storage/predictit_cache/pushed_markets.json"

# Fetch from PredictIt
DATA=$(curl -s --max-time 15 \
  -H "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)" \
  -H "Accept: application/json" \
  "$API" 2>/dev/null)

if [ -z "$DATA" ]; then
  echo "FAIL: empty response from PredictIt"
  exit 1
fi

# Validate JSON and count markets
COUNT=$(echo "$DATA" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('markets',[])))" 2>/dev/null)
if [ -z "$COUNT" ] || [ "$COUNT" -lt 10 ]; then
  echo "FAIL: invalid data or too few markets ($COUNT)"
  exit 1
fi

# Write locally then push to VPS
echo "$DATA" > "$LOCAL_CACHE"
rsync -q "$LOCAL_CACHE" "$VPS_DEST"

echo "OK: pushed $COUNT PredictIt markets to VPS"
