#!/bin/bash
#
# Elon Musk Tweet Polling Script
# Runs every 6 hours via cron or systemd timer
# Stores snapshots for rate calculation

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$SCRIPT_DIR/polls"
BASELINE_FILE="$SCRIPT_DIR/elon_tracker.json"
LOG_FILE="$DATA_DIR/poll.log"

# Create data directory
mkdir -p "$DATA_DIR"

# Timestamp
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
DATE_HOUR=$(date -u +"%Y-%m-%dT%H-00")
OUTPUT_FILE="$DATA_DIR/${DATE_HOUR}.json"

echo "[$TIMESTAMP] Polling Elon Musk tweets..." | tee -a "$LOG_FILE"

# Fetch from FixTweet API (direct user endpoint)
RESPONSE=$(curl -s --max-time 30 \
    -H "User-Agent: Mozilla/5.0" \
    "https://api.fxtwitter.com/elonmusk" 2>/dev/null || echo '{}')

# Check if we got valid data
if [ -z "$RESPONSE" ] || [ "$RESPONSE" = '{}' ]; then
    echo "[$TIMESTAMP] ERROR: Failed to fetch data" | tee -a "$LOG_FILE"
    exit 1
fi

# Extract tweet count (handle various API response formats)
TWEET_COUNT=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    # New format: /elonmusk endpoint
    if 'user' in data:
        count = data['user'].get('tweets')
    else:
        # Fallback: try various field names
        count = data.get('tweetCount') or data.get('tweets') or data.get('statuses_count')
    if count:
        print(int(count))
    else:
        print('ERROR: No tweet count found', file=sys.stderr)
        sys.exit(1)
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null)

if [ -z "$TWEET_COUNT" ] || [ "$TWEET_COUNT" = "ERROR" ]; then
    echo "[$TIMESTAMP] ERROR: Could not parse tweet count" | tee -a "$LOG_FILE"
    exit 1
fi

# Calculate rate from baseline
BASELINE_COUNT=$(jq -r '.baseline.tweet_count' "$BASELINE_FILE" 2>/dev/null || echo "99692")
BASELINE_TIME=$(jq -r '.baseline.timestamp' "$BASELINE_FILE" 2>/dev/null || echo "2026-03-20T17:00:00-04:00")

# Calculate hours since baseline (Python handles timezone math)
RATE_CALC=$(python3 << PYEOF
from datetime import datetime
import sys

baseline_str = "$BASELINE_TIME"
current_str = "$TIMESTAMP"
tweet_count = $TWEET_COUNT
baseline_count = int($BASELINE_COUNT)

# Parse timestamps
try:
    baseline = datetime.fromisoformat(baseline_str.replace('Z', '+00:00'))
    current = datetime.fromisoformat(current_str.replace('Z', '+00:00'))
    
    hours_elapsed = (current - baseline).total_seconds() / 3600
    tweets_since = tweet_count - baseline_count
    
    if hours_elapsed > 0:
        daily_rate = (tweets_since / hours_elapsed) * 24
        weekly_rate = daily_rate * 7
    else:
        daily_rate = 0
        weekly_rate = 0
    
    print(f"{hours_elapsed:.2f}|{tweets_since}|{daily_rate:.2f}|{weekly_rate:.2f}")
except Exception as e:
    print(f"ERROR|{e}", file=sys.stderr)
    sys.exit(1)
PYEOF
)

HOURS_ELAPSED=$(echo "$RATE_CALC" | cut -d'|' -f1)
TWEETS_SINCE=$(echo "$RATE_CALC" | cut -d'|' -f2)
DAILY_RATE=$(echo "$RATE_CALC" | cut -d'|' -f3)
WEEKLY_RATE=$(echo "$RATE_CALC" | cut -d'|' -f4)

# Build JSON output
cat > "$OUTPUT_FILE" << EOF
{
  "timestamp": "$TIMESTAMP",
  "username": "ElonMusk",
  "tweet_count": $TWEET_COUNT,
  "baseline": {
    "count": $BASELINE_COUNT,
    "time": "$BASELINE_TIME"
  },
  "calculated": {
    "hours_since_baseline": $HOURS_ELAPSED,
    "tweets_since_baseline": $TWEETS_SINCE,
    "current_daily_rate": $DAILY_RATE,
    "projected_weekly": $WEEKLY_RATE
  },
  "raw_response": $RESPONSE
}
EOF

echo "[$TIMESTAMP] Saved: $OUTPUT_FILE" | tee -a "$LOG_FILE"
echo "[$TIMESTAMP] Current rate: ${DAILY_RATE} tweets/day (projected ${WEEKLY_RATE}/week)" | tee -a "$LOG_FILE"

# Update calibration status if this is first poll
WEEKS_COLLECTED=$(jq -r '.calibration.weeks_collected' "$BASELINE_FILE" 2>/dev/null || echo "0")
if [ "$WEEKS_COLLECTED" = "0" ]; then
    # Check if we have 7+ days of data
    FIRST_POLL=$(ls -1 "$DATA_DIR"/*.json 2>/dev/null | head -1)
    if [ -n "$FIRST_POLL" ]; then
        python3 << PYEOF
import json
from datetime import datetime

with open('$BASELINE_FILE', 'r') as f:
    config = json.load(f)

# Count polls
import os
polls = [f for f in os.listdir('$DATA_DIR') if f.endswith('.json') and f != 'poll.log']
config['calibration']['polls_collected'] = len(polls)

if len(polls) >= 28:  # 4 per day × 7 days
    config['calibration']['weeks_collected'] = 1
    config['calibration']['status'] = 'one_week'
elif len(polls) >= 56:  # 2 weeks
    config['calibration']['weeks_collected'] = 2
    config['calibration']['status'] = 'ready'

with open('$BASELINE_FILE', 'w') as f:
    json.dump(config, f, indent=2)
PYEOF
    fi
fi

echo "[$TIMESTAMP] Done." | tee -a "$LOG_FILE"
