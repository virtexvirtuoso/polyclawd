#!/bin/bash
# Pre-flight check for ralph-tui execution
# Run this BEFORE starting ralph-tui

set -e
cd "$(dirname "$0")/.."

echo "=== Polyclawd Refactoring Pre-Flight Check ==="
echo ""

# Check 1: Python deps
echo "[1/6] Checking Python dependencies..."
python3 -c "import aiofiles, slowapi, httpx, pytest" 2>/dev/null && echo "  Core deps: OK" || {
    echo "  Installing missing deps..."
    pip install aiofiles slowapi httpx pytest pytest-asyncio locust --quiet
}

# Check 2: Directory structure
echo "[2/6] Creating target directories..."
mkdir -p api/routes api/services tests/baseline_snapshots scripts
touch api/__init__.py api/routes/__init__.py api/services/__init__.py
echo "  Directories: OK"

# Check 3: API running (check /api/balance since /health doesn't exist yet)
echo "[3/6] Checking if API is running..."
if curl -sf http://localhost:8420/api/balance > /dev/null 2>&1; then
    echo "  API: Running"
else
    echo "  API: Not running - starting now..."
    mkdir -p ~/.polyclawd/logs
    nohup uvicorn api.main:app --host 0.0.0.0 --port 8420 > ~/.polyclawd/logs/api.log 2>&1 &
    sleep 8
    if curl -sf http://localhost:8420/api/balance > /dev/null 2>&1; then
        echo "  API: Started successfully"
    else
        echo "  API: FAILED TO START - check ~/.polyclawd/logs/api.log"
        exit 1
    fi
fi

# Check 4: Git status
echo "[4/6] Checking git status..."
if git diff --quiet 2>/dev/null; then
    echo "  Git: Clean"
else
    echo "  Git: Has uncommitted changes (consider committing or stashing)"
fi

# Check 5: main.py exists
echo "[5/6] Checking source files..."
if [ -f "api/main.py" ]; then
    lines=$(wc -l < api/main.py)
    echo "  api/main.py: $lines lines"
else
    echo "  api/main.py: NOT FOUND"
    exit 1
fi

# Check 6: REFACTORING_PLAN.md exists
echo "[6/6] Checking refactoring plan..."
if [ -f "docs/REFACTORING_PLAN.md" ]; then
    echo "  docs/REFACTORING_PLAN.md: Found"
else
    echo "  docs/REFACTORING_PLAN.md: NOT FOUND"
    exit 1
fi

echo ""
echo "=== Pre-Flight Complete ==="
echo ""
echo "Ready to run:"
echo "  cd ~/Desktop/polyclawd"
echo "  rm -f .ralph-tui/session.json"
echo "  ralph-tui run --prd prd.json"
