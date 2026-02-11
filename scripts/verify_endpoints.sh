#!/bin/bash
# Verify all endpoints return HTTP 2xx

set -e

BASE_URL="${API_BASE_URL:-http://localhost:8420}"

echo "Verifying endpoints return 2xx status..."

FAILED=0
PASSED=0

check_endpoint() {
    local endpoint="$1"
    local method="${2:-GET}"

    STATUS=$(curl -sf -o /dev/null -w "%{http_code}" -X "$method" "$BASE_URL$endpoint" 2>/dev/null || echo "000")

    if [[ "$STATUS" =~ ^2[0-9][0-9]$ ]]; then
        echo "PASS: $method $endpoint ($STATUS)"
        ((PASSED++))
    else
        echo "FAIL: $method $endpoint ($STATUS)"
        ((FAILED++))
    fi
}

# Core API endpoints
check_endpoint "/health"
check_endpoint "/api/balance"
check_endpoint "/api/positions"
check_endpoint "/api/trades"
check_endpoint "/api/signals"
check_endpoint "/api/engine/status"

echo ""
echo "Results: $PASSED passed, $FAILED failed"

if [ $FAILED -gt 0 ]; then
    exit 1
fi
