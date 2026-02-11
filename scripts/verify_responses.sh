#!/bin/bash
# Verify current API responses match baseline structure

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BASELINE_DIR="$PROJECT_DIR/tests/baseline_snapshots"
BASE_URL="${API_BASE_URL:-http://localhost:8420}"
TEMP_DIR=$(mktemp -d)

trap "rm -rf $TEMP_DIR" EXIT

echo "Verifying responses against baselines..."

FAILED=0
PASSED=0

verify_endpoint() {
    local endpoint="$1"
    local baseline_file="$2"

    if [ ! -f "$BASELINE_DIR/$baseline_file" ]; then
        echo "SKIP: $endpoint (no baseline)"
        return
    fi

    # Fetch current response
    curl -sf "$BASE_URL$endpoint" > "$TEMP_DIR/current.json" 2>/dev/null || echo '{}' > "$TEMP_DIR/current.json"

    # Compare keys using jq
    BASELINE_KEYS=$(jq -r 'if type == "array" then "array" else keys | sort | join(",") end' "$BASELINE_DIR/$baseline_file" 2>/dev/null || echo "error")
    CURRENT_KEYS=$(jq -r 'if type == "array" then "array" else keys | sort | join(",") end' "$TEMP_DIR/current.json" 2>/dev/null || echo "error")

    if [ "$BASELINE_KEYS" = "$CURRENT_KEYS" ]; then
        echo "PASS: $endpoint"
        ((PASSED++))
    else
        echo "FAIL: $endpoint"
        echo "  Baseline keys: $BASELINE_KEYS"
        echo "  Current keys:  $CURRENT_KEYS"
        ((FAILED++))
    fi
}

verify_endpoint "/health" "health.json"
verify_endpoint "/api/balance" "balance.json"
verify_endpoint "/api/positions" "positions.json"
verify_endpoint "/api/trades" "trades.json"
verify_endpoint "/api/signals" "signals.json"
verify_endpoint "/api/engine/status" "engine_status.json"

echo ""
echo "Results: $PASSED passed, $FAILED failed"

if [ $FAILED -gt 0 ]; then
    exit 1
fi
