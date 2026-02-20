#!/bin/bash
# Polyclawd API Watchdog v8
# v8: IC tracker + calibrator + basket arb + copy-trade + archetype backfill

LOG="/var/log/polyclawd-watchdog.log"
HEALTH_URL="http://127.0.0.1:8420/health"
SERVICE="polyclawd-api"
MAX_ATTEMPTS=3
CURL_TIMEOUT=8
STATE_FILE="/tmp/polyclawd-watchdog.state"
VENV="/var/www/virtuosocrypto.com/polyclawd/venv/bin/python3"
WORKDIR="/var/www/virtuosocrypto.com/polyclawd"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# Read consecutive restart count
RESTART_COUNT=0
if [ -f "$STATE_FILE" ]; then
    RESTART_COUNT=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
fi

if [ "$RESTART_COUNT" -ge 5 ]; then
    log "BACKOFF: $RESTART_COUNT consecutive restarts, skipping"
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
        echo 0 > "$STATE_FILE" 2>/dev/null
        HEALTHY=1
        break
    fi
    [ "$i" -lt "$MAX_ATTEMPTS" ] && sleep 5
done

if [ "$HEALTHY" -eq 0 ]; then
    RESTART_COUNT=$((RESTART_COUNT + 1))
    echo "$RESTART_COUNT" > "$STATE_FILE" 2>/dev/null
    log "RESTART: Health check failed $MAX_ATTEMPTS times (#$RESTART_COUNT)"
    systemctl restart "$SERVICE"
    log "RESTART: $SERVICE restarted (exit=$?)"
    exit 0
fi

cd "$WORKDIR"

# === EVERY 5 MIN: Resolution + IC resolution ===
$VENV signals/shadow_tracker.py resolve >> "$LOG" 2>&1 || true
$VENV signals/shadow_tracker.py snapshot >> "$LOG" 2>&1 || true
$VENV signals/shadow_tracker.py summary >> "$LOG" 2>&1 || true
$VENV signals/resolution_scanner.py scan >> "$LOG" 2>&1 || true
$VENV -c "from signals.paper_portfolio import resolve_open_positions; resolve_open_positions()" >> "$LOG" 2>&1 || true

# Resolve IC predictions from shadow trade outcomes
$VENV -c "
from signals.ic_tracker import resolve_from_shadow_trades
result = resolve_from_shadow_trades()
if result.get('resolved', 0) > 0:
    print(f'IC-RESOLVE: {result[\"resolved\"]} predictions resolved')
" >> "$LOG" 2>&1 || true

log "OK: resolution cycle complete"

# === EVERY 10 MIN: Signal scan + IC record + portfolio + alpha ===
MINUTE=$(date -u +%M)
MOD10=$((10#$MINUTE % 10))
if [ "$MOD10" -eq 0 ]; then
    SCAN_OUT=$($VENV -c "
import sys
sys.path.insert(0, 'signals')
from signals.paper_portfolio import process_signals
from signals.mispriced_category_signal import get_mispriced_category_signals
from signals.ic_tracker import record_signal_prediction

result_data = get_mispriced_category_signals()
signals = result_data.get('signals', [])

# Record predictions for IC tracking
ic_count = 0
for sig in signals:
    if sig.get('market_id') and sig.get('side') not in ['NEUTRAL', 'RESEARCH', '']:
        try:
            record_signal_prediction(sig)
            ic_count += 1
        except Exception:
            pass

result = process_signals(signals)
print(f'signals={len(signals)} opened={result.get(\"opened\",0)} skipped={result.get(\"skipped\",0)} ic_recorded={ic_count}')
" 2>&1)
    log "SCAN: $SCAN_OUT"

    # Alpha snapshot
    $VENV -c "from signals.alpha_score_tracker import run_snapshot; run_snapshot()" >> "$LOG" 2>&1 || true
    log "ALPHA: done"
fi

# === EVERY 30 MIN: IC calculation + calibration + source weights ===
MOD30=$((10#$MINUTE % 30))
if [ "$MOD30" -eq 0 ]; then
    $VENV -c "
import sys
sys.path.insert(0, 'signals')
from signals.ic_tracker import ic_report
from signals.calibrator import full_calibration_report, compute_source_weights

# Calculate IC for all sources
ic = ic_report(window_days=30)
sources_measured = len(ic.get('sources', {}))
print(f'IC: {sources_measured} sources measured')

# Build calibration curves
cal = full_calibration_report()
print(f'CALIBRATION: status={cal.get(\"overall_status\", \"unknown\")}')

# Update source weights from IC
weights = compute_source_weights()
print(f'WEIGHTS: {len(weights.get(\"weights\", {}))} sources weighted')
" >> "$LOG" 2>&1 || true
    log "FEEDBACK: IC + calibration + weights cycle complete"
fi

# === EVERY 6 HOURS: Arena leaderboard snapshot ===
HOUR=$(date -u +%H)
if [ "$MOD30" -lt 5 ] && ([ "$HOUR" = "00" ] || [ "$HOUR" = "06" ] || [ "$HOUR" = "12" ] || [ "$HOUR" = "18" ]); then
    $VENV -c "
try:
    from signals.arena_tracker import snapshot_leaderboard
    snapshot_leaderboard()
except Exception as e:
    print(f'Arena snapshot failed: {e}')
" >> "$LOG" 2>&1
    log "ARENA: snapshot complete"
fi

# Log rotation: keep last 2000 lines
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 2000 ]; then
    tail -1000 "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
fi
