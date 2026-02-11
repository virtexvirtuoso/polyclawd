#!/bin/bash
# Capture baseline API responses for regression testing

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BASELINE_DIR="$PROJECT_DIR/tests/baseline_snapshots"
BASE_URL="${API_BASE_URL:-http://localhost:8420}"

mkdir -p "$BASELINE_DIR"

echo "Capturing baseline responses from $BASE_URL..."

# Core endpoints
curl -sf "$BASE_URL/health" > "$BASELINE_DIR/health.json" 2>/dev/null || echo '{"status":"unknown"}' > "$BASELINE_DIR/health.json"
curl -sf "$BASE_URL/api/balance" > "$BASELINE_DIR/balance.json" 2>/dev/null || echo '{}' > "$BASELINE_DIR/balance.json"
curl -sf "$BASE_URL/api/positions" > "$BASELINE_DIR/positions.json" 2>/dev/null || echo '[]' > "$BASELINE_DIR/positions.json"
curl -sf "$BASE_URL/api/trades" > "$BASELINE_DIR/trades.json" 2>/dev/null || echo '[]' > "$BASELINE_DIR/trades.json"
curl -sf "$BASE_URL/api/signals" > "$BASELINE_DIR/signals.json" 2>/dev/null || echo '[]' > "$BASELINE_DIR/signals.json"
curl -sf "$BASE_URL/api/engine/status" > "$BASELINE_DIR/engine_status.json" 2>/dev/null || echo '{}' > "$BASELINE_DIR/engine_status.json"

# Count captured files
COUNT=$(ls -1 "$BASELINE_DIR"/*.json 2>/dev/null | wc -l)
echo "Captured $COUNT baseline snapshots to $BASELINE_DIR"
ls -la "$BASELINE_DIR"
