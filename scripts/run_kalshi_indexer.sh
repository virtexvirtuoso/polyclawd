#!/bin/bash
# Resilient Kalshi indexer runner — auto-retries on any failure
cd /Users/ffv_macmini/Desktop/prediction-market-analysis

MAX_RETRIES=20
RETRY=0
LOG=/tmp/kalshi-markets-indexer.log

echo "[$(date)] Starting resilient Kalshi indexer (max $MAX_RETRIES retries)" > "$LOG"

while [ $RETRY -lt $MAX_RETRIES ]; do
    echo "[$(date)] Attempt $((RETRY+1))/$MAX_RETRIES" >> "$LOG"
    
    uv run python -c "
from src.indexers.kalshi.markets import KalshiMarketsIndexer
idx = KalshiMarketsIndexer()
idx.run()
print('KALSHI_MARKETS_DONE')
" >> "$LOG" 2>&1
    
    EXIT_CODE=$?
    
    if grep -q "KALSHI_MARKETS_DONE" "$LOG"; then
        echo "[$(date)] Markets indexer completed successfully!" >> "$LOG"
        
        # Now run trades
        echo "[$(date)] Starting trades indexer..." >> "$LOG"
        uv run python -c "
from src.indexers.kalshi.trades import KalshiTradesIndexer
idx = KalshiTradesIndexer()
idx.run()
print('KALSHI_TRADES_DONE')
" >> "$LOG" 2>&1
        
        if grep -q "KALSHI_TRADES_DONE" "$LOG"; then
            echo "[$(date)] All Kalshi indexing complete!" >> "$LOG"
        fi
        exit 0
    fi
    
    RETRY=$((RETRY+1))
    SLEEP=$((RETRY * 10))
    echo "[$(date)] Failed (exit $EXIT_CODE), sleeping ${SLEEP}s before retry..." >> "$LOG"
    sleep $SLEEP
done

echo "[$(date)] FAILED after $MAX_RETRIES retries" >> "$LOG"
exit 1
