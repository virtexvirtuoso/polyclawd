import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
import pandas as pd

# Mocking or importing from the actual structure if we were running this locally
# But since we are writing to the VPS, we use the relative imports as they exist there.
from src.common.indexer import Indexer
from src.indexers.polymarket.client import PolymarketClient
from src.indexers.polymarket.models import Market

DATA_DIR = Path("data/polymarket/markets")
OFFSET_FILE = Path("data/polymarket/.backfill_offset")
CHUNK_SIZE = 10000

class PolymarketMarketsIndexer(Indexer):
    """Fetches and stores Polymarket markets data."""

    def __init__(self):
        super().__init__(
            name="polymarket_markets",
            description="Backfills Polymarket markets data to parquet files",
        )

    def run(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)

        client = PolymarketClient()
        all_markets = []

        # --- Social Volume Alpha Recovery: Force-index recurring slugs ---
        force_slugs = [
            'donald-trump-of-truth-social-posts',
            'will-elon-musk-post',
            'will-trump-post-on-x-twitter'
        ]
        print(f"Starting force-indexing for {len(force_slugs)} slug prefixes...")
        for slug_prefix in force_slugs:
            try:
                # Search for any active events matching the prefix via slug parameter
                events = client.get_events(slug=slug_prefix)
                if events:
                    fetched_at = datetime.utcnow()
                    for event in events:
                        # Only process active, non-closed events
                        if event.get('active') and not event.get('closed'):
                            for m_data in event.get('markets', []):
                                market = Market.from_dict(m_data)
                                record = asdict(market)
                                record['_fetched_at'] = fetched_at
                                # Tag as force-indexed for strategy filtering
                                record['force_indexed'] = True
                                # Inject event-level metadata (like tweetCount) if available
                                # This is the "Golden Source" for social markets
                                if 'tweetCount' in event:
                                    record['event_metadata_count'] = event['tweetCount']
                                all_markets.append(record)
                            print(f"Force-indexed event: {event.get('slug')} ({len(event.get('markets', []))} markets)")
            except Exception as e:
                print(f"Error force-indexing {slug_prefix}: {e}")
        # -----------------------------------------------------------------

        offset = 0
        if OFFSET_FILE.exists():
            try:
                offset = int(OFFSET_FILE.read_text().strip())
                if offset > 0:
                    print(f"Resuming from offset: {offset}")
            except (ValueError, TypeError):
                offset = 0

        total = offset

        for markets, next_offset in client.iter_markets(offset=offset):
            if markets:
                fetched_at = datetime.utcnow()
                for market in markets:
                    record = asdict(market)
                    record["_fetched_at"] = fetched_at
                    record['force_indexed'] = False
                    record['event_metadata_count'] = None
                    all_markets.append(record)

                total += len(markets)
                print(f"Fetched {len(markets)} markets (total: {total})")

                # Save in chunks
                while len(all_markets) >= CHUNK_SIZE:
                    chunk = all_markets[:CHUNK_SIZE]
                    chunk_start = total - len(all_markets)
                    chunk_path = DATA_DIR / f"markets_{chunk_start}_{chunk_start + CHUNK_SIZE}.parquet"
                    pd.DataFrame(chunk).to_parquet(chunk_path)
                    all_markets = all_markets[CHUNK_SIZE:]

            if next_offset > 0:
                OFFSET_FILE.write_text(str(next_offset))
            else:
                break

        # Save remaining markets
        if all_markets:
            chunk_start = total - len(all_markets)
            chunk_path = DATA_DIR / f"markets_{chunk_start}_{chunk_start + len(all_markets)}.parquet"
            pd.DataFrame(all_markets).to_parquet(chunk_path)

        if OFFSET_FILE.exists():
            OFFSET_FILE.unlink()

        client.close()
        print(f"\nBackfill complete: {total} markets fetched")

if __name__ == '__main__':
    indexer = PolymarketMarketsIndexer()
    indexer.run()
