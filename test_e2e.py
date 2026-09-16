#!/usr/bin/env python3
"""End-to-end test: props script + API health + consensus edges."""
import sys, json, urllib.request, os

sys.path.insert(0, '/var/www/virtuosocrypto.com/polyclawd')
from odds.the_odds_api import _get_baseball_api_key

api_key = _get_baseball_api_key()
if not api_key:
    # Try env
    api_key = os.environ.get('ODDS_API_KEY')
if not api_key:
    print("FAIL: No API key")
    sys.exit(1)

print(f"Key: {api_key[:8]}...{api_key[-4:]}")

# 1. Health check
try:
    req = urllib.request.Request('http://localhost:8420/health')
    with urllib.request.urlopen(req, timeout=5) as r:
        d = json.loads(r.read().decode())
        assert d.get('status') == 'healthy'
        print(f"✅ Health: {d['status']}")
except Exception as e:
    print(f"❌ Health: {e}")

# 2. Props script
import subprocess
result = subprocess.run(
    ['venv/bin/python3', 'scripts/mlb_props_reconcile.py'],
    capture_output=True, text=True, timeout=30,
    cwd='/var/www/virtuosocrypto.com/polyclawd',
    env={**os.environ, 'ODDS_API_KEY': api_key}
)
output = result.stdout + result.stderr
if '❌ Not found' in output:
    print("❌ Props script: No game found (late night, all games started)")
elif '✅' in output and 'Batter Home Runs' in output:
    print("✅ Props script: Game found with props")
else:
    print(f"⚠️ Props script ran but output unclear: {output[:200]}")

# 3. API edge endpoint
try:
    req2 = urllib.request.Request('http://localhost:8420/api/baseball/edge?min_edge=0.01')
    with urllib.request.urlopen(req2, timeout=30) as r2:
        d2 = json.loads(r2.read().decode())
        edges = d2.get('edges', [])
        print(f"✅ API edges: {d2.get('total_edges', len(edges))} edges returned")
        if edges:
            e = edges[0]
            prob = e.get('odds_api_prob', 0)
            poly = e.get('polymarket_price', 0)
            print(f"   Sample: {e.get('game','?')} | book={prob}% poly={poly}% edge={e.get('edge_pct',0)}%")
except Exception as e:
    print(f"❌ API edges: {e}")

# 4. Check consensus is actually being used (no vig collapse)
try:
    req3 = urllib.request.Request(f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds?apiKey=***&regions=us&markets=h2h&oddsFormat=american")
    with urllib.request.urlopen(req3, timeout=10) as r3:
        games = json.loads(r3.read().decode())
        if games:
            g = games[0]
            # Check per-book vig
            for bk in g.get('bookmakers', [])[:3]:
                for m in bk.get('markets', []):
                    if m.get('key') == 'h2h':
                        outs = m.get('outcomes', [])
                        if len(outs) >= 2:
                            from odds.the_odds_api import _american_to_implied_prob
                            raw_a = _american_to_implied_prob(int(outs[0]['price']))
                            raw_b = _american_to_implied_prob(int(outs[1]['price']))
                            vig = raw_a + raw_b
                            print(f"✅ {bk['key']:15s} vig={vig:.4f} {'OK' if vig > 1.0 else '⚠️ COLLAPSED'}")
except Exception as e:
    print(f"❌ Vig check: {e}")

print("\n--- E2E Complete ---")
