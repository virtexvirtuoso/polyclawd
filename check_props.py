import urllib.request, json
import sys
sys.path.insert(0, '/var/www/virtuosocrypto.com/polyclawd')
from odds.the_odds_api import _get_baseball_api_key

api_key = _get_baseball_api_key()
print(f'Key loaded via _get_baseball_api_key: {api_key[:8]}...{api_key[-4:]}')

url = f'https://api.the-odds-api.com/v4/sports?apiKey={api_key}'
req = urllib.request.Request(url)
with urllib.request.urlopen(req, timeout=10) as resp:
    sports = json.loads(resp.read().decode())
    for s in sports:
        if s.get('key') == 'baseball_mlb':
            print(f'baseball_mlb groups={s.get("groups")}')
            break

for mkt in ['batter_home_runs', 'pitcher_strikeouts', 'player_home_runs', 'player_strikeouts']:
    url2 = f'https://api.the-odds-api.com/v4/sports/baseball_mlb/odds?apiKey={api_key}&regions=us&markets={mkt}&oddsFormat=american'
    try:
        req2 = urllib.request.Request(url2)
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            d2 = json.loads(resp2.read().decode())
            n_out = sum(len(o.get('outcomes',[])) for g in d2 for b in g.get('bookmakers',[]) for o in b.get('markets',[])) if d2 else 0
            print(f'{mkt:30s}: {len(d2)} games, {n_out} outcomes | {resp2.headers.get("x-requests-remaining")} credits')
    except Exception as e:
        print(f'{mkt:30s}: ERROR {str(e)[:80]}')
