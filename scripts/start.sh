#!/bin/bash
# Start Polymarket Trading Bot Web Frontend

cd "$(dirname "$0")"

# Check for virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate and install deps
source venv/bin/activate
pip install -q -r requirements.txt 2>/dev/null

# Start server
echo ""
echo "⚡ Polymarket Trading Bot"
echo "========================="
echo "Starting server at http://127.0.0.1:8000"
echo "Press Ctrl+C to stop"
echo ""

uvicorn api.main:app --host 127.0.0.1 --port 8000
