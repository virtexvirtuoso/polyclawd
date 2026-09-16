#!/usr/bin/env python3
"""Check which market key conventions work for each active sport on The Odds API."""
import urllib.request, json, re
from pathlib import Path

# Read key from /etc/default/polyclawd
env_content = Path('/etc/default/polyclawd').read_text()
m = re.search(r'ODDS_API_KEY=(\S+)', env_content)
if not m:
    # Fallback: .env
    envp = Path('/var/www/virtuosocrypto.com/polyclawd/.env')
    if envp.exists():
        for line in envp.read_text().splitlines():
            if line.startswith('ODDS_API_KEY='):
                m = [None, line.split('=', 1)[1].strip()]
                break
if not m:
    print("No API key found")
    import sys; sys.exit(1)

api_key = m[1] if isinstance(m, list) else m.group(1)
print(f'Key: {api_key[:8]}...{api_key[-4:]}')

url = f'https://api.the-odds-api.com/v4/sports?apiKey=***'
req = urllib.request.Request(url)
with urllib.request.urlopen(req, timeout=15) as resp:
    remaining = resp.headers.get('x-requests-remaining')
    sports = json.loads(resp.read().decode())
    active = [s for s in sports if s.get('active')]
    print(f'Active sports: {len(active)} | Credits: {remaining}')

categories = {
    'baseball': {
        'prefix_filter': ['baseball_', 'mlb'],
        'keys': ['player_home_runs', 'batter_home_runs', 'pitcher_strikeouts']
    },
    'basketball': {
        'prefix_filter': ['basketball_', 'nba'],
        'keys': ['player_points', 'player_rebounds', 'player_assists']
    },
    'football': {
        'prefix_filter': ['football_', 'nfl'],
        'keys': ['player_pass_yds', 'player_rush_yds', 'player_receptions', 'player_anytime_td']
    },
    'soccer': {
        'prefix_filter': ['soccer_'],
        'keys': ['player_goals_scored', 'player_shots_on_target', 'player_assists']
    },
    'hockey': {
        'prefix_filter': ['hockey_'],
        'keys': ['player_points', 'player_goals', 'player_assists', 'player_shots_on_goal']
    },
    'fighting/mma': {
        'prefix_filter': ['fighting_', 'mma_', 'boxing_'],
        'keys': ['player_win_method', 'fighter_win_method', 'fighter_significant_strikes']
    },
    'golf': {
        'prefix_filter': ['golf_'],
        'keys': ['player_top_5', 'player_make_cut', 'player_round_score']
    },
    'tennis': {
        'prefix_filter': ['tennis_'],
        'keys': ['player_match_winner', 'player_set_winner', 'player_total_games']
    },
}

results = {}
all_creds = remaining

for cat, cfg in categories.items():
    matched = [s for s in active if any(p in s['key'] for p in cfg['prefix_filter'])]
    print(f'\n─── {cat.upper()} ({len(matched)}) ───')
    if not matched:
        continue
    for s in sorted(matched, key=lambda x: x['key']):
        sk, title = s['key'], s.get('title', sk)
        found = False
        for mkt in cfg['keys']:
            url2 = f'https://api.the-odds-api.com/v4/sports/{sk}/odds?apiKey=***&regions=us&markets={mkt}&oddsFormat=american'
            try:
                req2 = urllib.request.Request(url2)
                with urllib.request.urlopen(req2, timeout=10) as resp2:
                    data2 = json.loads(resp2.read().decode())
                    all_creds = resp2.headers.get('x-requests-remaining')
                    if data2:
                        n_out = sum(len(o.get('outcomes',[])) for g in data2 for b in g.get('bookmakers',[]) for o in b.get('markets',[]))
                        if n_out > 0:
                            desc = ''
                            for g in data2[:1]:
                                for b in g.get('bookmakers',[]):
                                    for m in b.get('markets',[]):
                                        if m.get('outcomes'):
                                            o = m['outcomes'][0]
                                            desc = o.get('description','') or o.get('name','?')
                                            break
                                    if desc: break
                                if desc: break
                            print(f'  ✅ {title:30s} [{sk:20s}] {mkt:30s} → {n_out} outcomes (eg: {desc[:50]})')
                            results.setdefault(cat, []).append((sk, mkt))
                            found = True
                            break
            except Exception:
                pass
        if not found:
            print(f'  ❌ {title:30s} — no working key')

print(f'\n─── Summary ───')
print(f'Sports with working props: {sum(len(v) for v in results.values())} sport/key combos')
for cat, hits in results.items():
    sports_set = set(h[0] for h in hits)
    keys_set = set(h[1] for h in hits)
    print(f'  {cat:15s}: {len(hits)} combos across {len(sports_set)} sports, working keys: {keys_set}')

print(f'\nCredits remaining: {all_creds}')