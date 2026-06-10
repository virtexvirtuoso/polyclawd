#!/usr/bin/env python3
"""
Edge Scanner → Discord Alert
Runs as cron, replaces the OpenClaw agent-based edge-scanner-6h.
Calls /api/edge/scan, filters real edges (>10%, > volume), posts to Discord.
"""
import sys
import json
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from signals.discord_alerts import alert_edge_batch, _send, COLOR_BLUE

API = 'http://localhost:8420'
MIN_EDGE = 10.0
MIN_VOLUME = 100


def scan():
    try:
        r = urllib.request.urlopen(f'{API}/api/edge/scan', timeout=120)
        data = json.loads(r.read())
    except Exception as e:
        print(f'Edge scan failed: {e}')
        return

    edges = data.get('edges', data.get('opportunities', data.get('results', [])))
    if not edges:
        print(f'No edges found. Scanned {data.get("markets_scanned", "?")} markets.')
        return

    # Filter real edges
    valid = []
    for e in edges:
        edge_val = e.get('edge_pct', e.get('edge', e.get('spread', 0))) or '0'; edge = float(str(edge_val).replace('%',''))
        vol = float(e.get('volume', e.get('total_volume', 0)) or 0)
        
        if edge < MIN_EDGE:
            continue
        if vol < MIN_VOLUME:
            continue

        valid.append({
            'market': e.get('market', e.get('question', e.get('title', '?')))[:100],
            'side': e.get('side', e.get('recommended_side', '?')),
            'edge': edge,
            'price': e.get('price', e.get('best_price', 0)),
            'strategy': 'cross_platform',
            'platform': e.get('platform', ''),
            'url': e.get('url', ''),
        })

    scanned = data.get('markets_scanned', data.get('total_markets', '?'))
    clusters = data.get('topic_clusters', data.get('clusters', 0))

    if valid:
        print(f'Found {len(valid)} valid edges (>{MIN_EDGE}%, > vol)')
        alert_edge_batch(valid)
    else:
        # Silent — just log, no Discord spam
        print(f'No edges >{MIN_EDGE}% with volume. Scanned {scanned} markets, {clusters} clusters.')


if __name__ == '__main__':
    scan()
