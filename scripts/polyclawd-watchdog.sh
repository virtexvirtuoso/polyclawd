#!/bin/bash
# Polyclawd API Watchdog v5
# Health check every 5min, signal scan every 10min, resolution every 5min
# v5: accelerated feedback — 10min signals, daily-market priority, alpha snapshots every 10min

HEALTH_URL="http://127.0.0.1:8420/health"
SERVICE="polyclawd-api"
MAX_ATTEMPTS=3
CURL_TIMEOUT=8
STATE_FILE="/tmp/polyclawd-watchdog.state"
VENV="/var/www/virtuosocrypto.com/polyclawd/venv/bin/python3"
WORKDIR="/var/www/virtuosocrypto.com/polyclawd"

# Read consecutive restart count
RESTART_COUNT=0
if [ -f "$STATE_FILE" ]; then
    RESTART_COUNT=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
fi

if [ "$RESTART_COUNT" -ge 5 ]; then
    logger -t polyclawd-watchdog "Backing off: $RESTART_COUNT consecutive restarts."
    exit 0
fi

if ! systemctl is-enabled "$SERVICE" &>/dev/null; then
    exit 0
fi

# Health check with retries
HEALTHY=0
for i in $(seq 1 $MAX_ATTEMPTS); do
    RESP=$(curl -sf --max-time "$CURL_TIMEOUT" "$HEALTH_URL" 2>/dev/null)
    if [ $? -eq 0 ] && echo "$RESP" | grep -q '"healthy"'; then
        echo 0 > "$STATE_FILE"
        HEALTHY=1
        break
    fi
    [ "$i" -lt "$MAX_ATTEMPTS" ] && sleep 5
done

if [ "$HEALTHY" -eq 0 ]; then
    RESTART_COUNT=$((RESTART_COUNT + 1))
    echo "$RESTART_COUNT" > "$STATE_FILE"
    logger -t polyclawd-watchdog "Health check failed $MAX_ATTEMPTS times (restart #$RESTART_COUNT), restarting $SERVICE"
    systemctl restart "$SERVICE"
    logger -t polyclawd-watchdog "$SERVICE restarted (exit code: $?)"
    exit 0
fi

cd "$WORKDIR"

# === EVERY 5 MIN: Shadow trade resolution + resolution certainty ===
$VENV signals/shadow_tracker.py resolve > /dev/null 2>&1 || true
$VENV signals/shadow_tracker.py snapshot > /dev/null 2>&1 || true
$VENV signals/shadow_tracker.py summary > /dev/null 2>&1 || true
$VENV signals/resolution_scanner.py scan > /dev/null 2>&1 || true

# === EVERY 10 MIN: Signal scan + portfolio + alpha snapshot ===
MINUTE=$(date -u +%M)
MOD10=$((10#$MINUTE % 10))
if [ "$MOD10" -lt 6 ]; then
    $VENV -c "
from signals.paper_portfolio import process_signals
from signals.mispriced_category_signal import get_mispriced_category_signals
result_data = get_mispriced_category_signals()
signals = result_data.get('signals', [])
result = process_signals(signals)
" > /dev/null 2>&1 || true
    logger -t polyclawd-watchdog "Signal scan + portfolio processing complete"

    # Alpha score snapshot (every 10min = 144/day)
    $VENV -c "from signals.alpha_score_tracker import run_snapshot; run_snapshot()" > /dev/null 2>&1 || true
    logger -t polyclawd-watchdog "Alpha score snapshot complete"
fi

# === EVERY 6 HOURS: Arena leaderboard snapshot ===
HOUR=$(date -u +%H)
MOD30=$((10#$MINUTE % 30))
if [ "$MOD30" -lt 10 ] && ([ "$HOUR" = "00" ] || [ "$HOUR" = "06" ] || [ "$HOUR" = "12" ] || [ "$HOUR" = "18" ]); then
    $VENV signals/ai_model_tracker.py snapshot > /dev/null 2>&1 || true
    logger -t polyclawd-watchdog "Arena leaderboard snapshot taken"
fi
