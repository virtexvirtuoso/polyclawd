#!/usr/bin/env python3
"""Build Election Intelligence Brief PDF from election JSON data.
Usage: python3 build-election-pdf.py [input.json] [output.pdf]
"""

import json
import html
import sys
import os
from datetime import datetime

input_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/election_full.json'

# Generate timestamped filename in project reports folder
_now = datetime.now()
_date_str = _now.strftime('%Y-%m-%d_%H%M')
_script_dir = os.path.dirname(os.path.abspath(__file__))
_project_dir = os.path.dirname(_script_dir)
_reports_dir = os.path.join(_project_dir, 'reports', 'election-intel')
os.makedirs(_reports_dir, exist_ok=True)
_default_pdf = os.path.join(_reports_dir, f'Election-Intel-Brief_{_date_str}.pdf')

output_pdf = sys.argv[2] if len(sys.argv) > 2 else _default_pdf
output_html = output_pdf.replace('.pdf', '.html')

# ── Load data ──────────────────────────────────────────────────────────────
with open(input_path) as f:
    data = json.load(f)

summary = data['summary']
insights = data['insights']
top_movers = data['top_movers']
party_control = summary['party_control']
timestamp = data['timestamp']

try:
    ts = datetime.fromisoformat(timestamp)
    ts_str = ts.strftime('%B %d, %Y at %H:%M UTC')
except Exception:
    ts_str = timestamp[:19]

# ── Extract sections ───────────────────────────────────────────────────────
cross_platform = insights.get('cross_platform_spreads', [])[:4]
money_vs_odds = insights.get('money_vs_odds', [])[:4]
fundraising = insights.get('fundraising', {})
wiki_spikes = insights.get('wiki_spikes', [])
gtrends_spikes = insights.get('gtrends_spikes', [])
candidate_sentiment = insights.get('candidate_sentiment', [])
state_sentiment = insights.get('state_sentiment', [])
scoreboard = insights.get('midterm', {}).get('scoreboard', {})
presidential = insights.get('presidential_candidates', {})
generic_ballot = insights.get('poll_data', {}).get('generic_ballot', {}).get('average', {})
tipping_point = insights.get('tipping_point_races', [])[:6]
ticket_splits = insights.get('ticket_splits', [])[:5]
spending_surges = insights.get('spending_surges', [])
smart_money = insights.get('smart_money', [])[:5]
fec_cross_signals = insights.get('fec_cross_signals', [])[:4]
predictit_spreads = insights.get('predictit_spreads', [])[:4]

fund_list = sorted(fundraising.values(), key=lambda x: x.get('dem_cash', 0) + x.get('rep_cash', 0), reverse=True)[:5]

# Deduplicate spending surges
seen_surges = set()
deduped_surges = []
for s in spending_surges:
    key = (s.get('committee'), s.get('candidate'), s.get('amount'))
    if key not in seen_surges:
        seen_surges.add(key)
        deduped_surges.append(s)
spending_surges = deduped_surges[:3]

# ── Top Movers (filter out resolutions) ───────────────────────────────────
significant_movers = []
for m in top_movers:
    d = m.get('delta', 0)
    if abs(d) < 0.9 and abs(d) >= 0.01:  # Skip resolutions (near ±1.0) and noise
        significant_movers.append(m)
    if len(significant_movers) >= 5:
        break

# ── Alpha Opportunities (derived from spreads + FEC divergences) ──────────
alpha_opps = []

# 1. Biggest cross-platform arb with FEC confirmation
for sp in cross_platform[:3]:
    st = sp.get('state', '')
    race = sp.get('race_category', '')
    spread_pp = sp.get('spread_pp', 0)
    poly_d = (sp.get('poly_d') or 0)
    kalshi_d = (sp.get('kalshi_d') or 0)

    # Check if FEC data confirms one side
    fec_match = fundraising.get(st, {})
    dem_cash = fec_match.get('dem_cash', 0) or 0
    rep_cash = fec_match.get('rep_cash', 0) or 0

    if spread_pp >= 15 and dem_cash + rep_cash > 0:
        if dem_cash > rep_cash * 2 and poly_d > kalshi_d:
            alpha_opps.append({
                'title': f"{st} {race.title()} — D underpriced on Kalshi",
                'detail': f"Polymarket has D at {poly_d*100:.0f}% vs Kalshi's {kalshi_d*100:.0f}% ({spread_pp:.0f}pp gap). FEC cash-on-hand backs the higher D price: ${dem_cash/1e6:.1f}M D vs ${rep_cash/1e6:.1f}M R. The Kalshi price looks stale or liquidity-starved.",
                'edge': spread_pp,
            })
        elif rep_cash > dem_cash * 2 and kalshi_d > poly_d:
            alpha_opps.append({
                'title': f"{st} {race.title()} — R underpriced on Polymarket",
                'detail': f"Kalshi has D at {kalshi_d*100:.0f}% vs Polymarket's {poly_d*100:.0f}% ({spread_pp:.0f}pp gap). FEC cash favors R: ${rep_cash/1e6:.1f}M R vs ${dem_cash/1e6:.1f}M D.",
                'edge': spread_pp,
            })
        else:
            # Big spread, mixed FEC signal
            alpha_opps.append({
                'title': f"{st} {race.title()} — {spread_pp:.0f}pp cross-platform arb",
                'detail': f"Poly {poly_d*100:.0f}% D vs Kalshi {kalshi_d*100:.0f}% D. FEC: ${dem_cash/1e6:.1f}M D / ${rep_cash/1e6:.1f}M R. One platform is wrong — the spread alone is actionable.",
                'edge': spread_pp,
            })

# 2. FEC divergence where money contradicts market heavily
for mv in money_vs_odds[:3]:
    mv_state = mv.get('state', '')
    mv_pp = mv.get('divergence_pp', 0)
    # Avoid duplicates with spread-based opps
    if mv_pp >= 30 and mv_state not in [a['title'][:2] for a in alpha_opps]:
        alpha_opps.append({
            'title': f"{mv_state} — GOP spending {mv_pp:.0f}pp above market pricing",
            'detail': f"Republicans are pouring cash into {mv_state} despite the market giving them long odds. A {mv_pp:.0f}pp divergence between spending and pricing suggests internal polling or ground-game data the market hasn't absorbed.",
            'edge': mv_pp,
        })

alpha_opps = sorted(alpha_opps, key=lambda x: -x['edge'])[:2]

# ── Build movers strip HTML ───────────────────────────────────────────────
movers_strip_html = ''
if significant_movers:
    cards = ''
    for m in significant_movers[:5]:
        d = m.get('delta', 0)
        cls = 'up' if d > 0 else 'down'
        cards += f'<div class="mover"><div class="mover-delta {cls}">{d*100:+.1f}pp</div><div class="mover-outcome">{esc(m.get("outcome","?"))}</div><div class="mover-question">{esc(str(m.get("question","?"))[:40])}</div></div>'
    movers_strip_html = f'''<div style="font-family:'JetBrains Mono',monospace;font-size:7px;font-weight:600;color:#7A7568;text-transform:uppercase;letter-spacing:1.2px;margin-bottom:4px;">Biggest Market Moves</div><div class="movers-strip">{cards}</div>'''

# ── Build alpha box HTML ──────────────────────────────────────────────────
alpha_box_html = ''
if alpha_opps:
    items = ''
    for a in alpha_opps:
        items += f'<div class="alpha-item"><div class="alpha-title">{a["title"]} <span class="alpha-edge">{a["edge"]:.0f}pp edge</span></div><div class="alpha-detail">{a["detail"]}</div></div>'
    alpha_box_html = f'<div class="alpha-box"><div class="alpha-header"><div class="alpha-badge">High-Conviction Opportunities</div></div>{items}</div>'

# ── Helpers ────────────────────────────────────────────────────────────────
def pct(v):
    if v is None: return '\u2014'
    return f"{v * 100:.1f}%"

def pp(v):
    if v is None: return '\u2014'
    return f"{v:.0f}pp"

def money(v):
    if v is None or v == 0: return '\u2014'
    if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
    if v >= 1_000: return f"${v/1_000:.0f}K"
    return f"${v:,.0f}"

def esc(s):
    if s is None: return '\u2014'
    return html.escape(str(s))

# ── Control bars ───────────────────────────────────────────────────────────
def control_bar(label, d_pct, r_pct):
    d = d_pct * 100
    r = r_pct * 100
    return f'''
    <div class="control-row">
      <div class="control-label">{label}</div>
      <div class="control-bar-container">
        <div class="control-bar-track">
          <div class="control-bar dem-bar" style="width:{d:.1f}%"><span class="bar-label">{d:.0f}%</span></div>
          <div class="control-bar rep-bar" style="width:{r:.1f}%"><span class="bar-label">{r:.0f}%</span></div>
        </div>
      </div>
    </div>'''

control_bars_html = ''
for chamber, label in [('presidency', 'Presidency'), ('senate', 'Senate'), ('house', 'House')]:
    pc = party_control.get(chamber, {})
    control_bars_html += control_bar(label, pc.get('democrat', 0), pc.get('republican', 0))

# ── Cross-Platform Spreads ─────────────────────────────────────────────────
spreads_rows = ''
for s in cross_platform:
    hl = ' class="hl"' if s.get('spread_pp', 0) > 15 else ''
    spreads_rows += f'''<tr{hl}><td class="mono">{esc(s.get('state',''))}</td><td>{esc(s.get('race_category',''))}</td><td class="dem-text">{pct(s.get('poly_d'))}</td><td class="rep-text">{pct(s.get('poly_r'))}</td><td class="dem-text">{pct(s.get('kalshi_d'))}</td><td class="rep-text">{pct(s.get('kalshi_r'))}</td><td class="mono accent">{pp(s.get('spread_pp'))}</td></tr>'''

# ── Money vs Odds — enriched layout ───────────────────────────────────────
mvo_html = ''
for m in money_vs_odds:
    state = m.get('state', '?')
    div_val = m.get('divergence_pp', 0)
    signal = m.get('signal', '')
    dem_cash = m.get('dem_cash', 0) or 0
    rep_cash = m.get('rep_cash', 0) or 0
    total_cash = dem_cash + rep_cash
    dem_pct = (dem_cash / total_cash * 100) if total_cash > 0 else 50
    rep_pct = 100 - dem_pct
    dem_odds = (m.get('dem_market_odds', 0) or 0) * 100
    rep_odds = (m.get('rep_market_odds', 0) or 0) * 100
    dem_cand = m.get('dem_candidate', '?')
    rep_cand = m.get('rep_candidate', '?')
    # Who is outfunding?
    is_r = 'R outfunding' in signal
    arrow_cls = 'rep-text' if is_r else 'dem-text'
    funder = rep_cand if is_r else dem_cand
    # Cash bar widths
    d_bar = max(dem_pct, 2)
    r_bar = max(rep_pct, 2)
    mvo_html += f'''<div class="mvo-row">
      <div class="mvo-head"><span class="mvo-state mono">{esc(state)}</span><span class="mvo-div mono">{div_val:.0f}pp</span></div>
      <div class="mvo-bar-wrap"><div class="mvo-bar dem-bg" style="width:{d_bar}%"></div><div class="mvo-bar rep-bg" style="width:{r_bar}%"></div></div>
      <div class="mvo-detail">
        <span class="dem-text">D ${dem_cash/1e6:.1f}M ({dem_odds:.0f}%)</span>
        <span class="mvo-vs">vs</span>
        <span class="rep-text">R ${rep_cash/1e6:.1f}M ({rep_odds:.0f}%)</span>
      </div>
    </div>'''

if not mvo_html:
    mvo_html = '<div class="no-data">No FEC divergences detected</div>'

# ── Fundraising chart data ─────────────────────────────────────────────────
fund_states = json.dumps([f.get('state', '?') for f in fund_list])
fund_dem = json.dumps([round(f.get('dem_cash', 0) / 1_000_000, 1) for f in fund_list])
fund_rep = json.dumps([round(f.get('rep_cash', 0) / 1_000_000, 1) for f in fund_list])

# ── Attention signals — wiki + trends ─────────────────────────────────────
attention_html = ''

# Wiki section
if wiki_spikes:
    attention_html += '<div class="att-section-label">Wikipedia Traffic Spikes</div>'
    max_z = max(w.get('z_score', 0) for w in wiki_spikes) or 1
    for w in sorted(wiki_spikes, key=lambda x: x.get('z_score', 0), reverse=True):
        party = w.get('party', '')
        pcls = 'dem' if party == 'D' else 'rep' if party == 'R' else 'neu'
        z = w.get('z_score', 0)
        pct_above = w.get('pct_above_avg', 0)
        views = w.get('latest_views', 0)
        race = w.get('race', '').replace('_', ' ').title()
        bar_w = max(min(z / max_z * 100, 100), 8)
        bar_cls = 'att-bar-dem' if party == 'D' else 'att-bar-rep' if party == 'R' else 'att-bar-neu'
        # Format view count
        if views >= 10000:
            views_str = f'{views/1000:.0f}K'
        elif views >= 1000:
            views_str = f'{views/1000:.1f}K'
        else:
            views_str = str(views)
        attention_html += f'''<div class="att-row">
          <div class="att-name {pcls}">{esc(w.get('candidate',''))}</div>
          <div class="att-bar-wrap"><div class="att-bar {bar_cls}" style="width:{bar_w}%"></div></div>
          <div class="att-z mono">z={z:.1f}</div>
          <div class="att-meta">+{pct_above:.0f}% &middot; {views_str} views &middot; {race}</div>
        </div>'''

# Trends section
trends_items = gtrends_spikes + insights.get('gtrends_election_topics', [])
if trends_items:
    attention_html += '<div class="att-section-label" style="margin-top:5px">Google Trends</div>'
    for g in trends_items[:4]:
        topic = g.get('trending_topic', g.get('keyword', '?'))
        traffic = g.get('traffic_str', '')
        candidate = g.get('candidate', '')
        label = f'{candidate} — ' if candidate else ''
        attention_html += f'''<div class="att-trend"><span class="att-trend-topic">{label}"{esc(topic)}"</span><span class="att-trend-traffic">{esc(traffic)}</span></div>'''

if not attention_html:
    attention_html = '<div class="no-data">No attention spikes this period</div>'

# ── GDELT Sentiment — news coverage intel ─────────────────────────────────
sentiment_items = [s for s in (candidate_sentiment + state_sentiment) if s.get('total_articles', 0) > 0]
sentiment_html = ''

def _tone_label(tone):
    """Convert raw GDELT tone to human-readable coverage descriptor."""
    if tone <= -1.5: return 'Very negative coverage'
    if tone <= -0.5: return 'Negative coverage'
    if tone <= -0.1: return 'Slightly negative'
    if tone >= 1.5: return 'Very positive coverage'
    if tone >= 0.5: return 'Positive coverage'
    if tone >= 0.1: return 'Slightly positive'
    return 'Neutral coverage'

def _trend_desc(trend):
    """Convert tone trend to directional description."""
    if trend >= 0.3: return ', improving fast'
    if trend >= 0.1: return ', improving'
    if trend <= -0.3: return ', deteriorating fast'
    if trend <= -0.1: return ', deteriorating'
    return ', stable'

def _nice_label(label, state):
    """Convert internal labels to readable names."""
    names = {'senate_dem': 'Dem Senate', 'senate_gop': 'GOP Senate', 'midterms_2026': 'Midterms'}
    return names.get(label, state or label)

for s in sentiment_items:
    tone = s.get('avg_tone', 0)
    label = s.get('label', '')
    state = s.get('state', '')
    trend = s.get('tone_trend', 0)
    articles = s.get('total_articles', 0)
    nice = _nice_label(label, state)
    cls = 'neg' if tone < -0.1 else 'pos' if tone > 0.1 else 'neu'
    icon = '−' if tone < -0.1 else '+' if tone > 0.1 else '·'
    desc = _tone_label(tone) + _trend_desc(trend)
    vol_label = f'{articles} articles'
    sentiment_html += f'''<div class="sent-row"><div class="sent-label">{esc(nice)}</div><div class="sent-reading {cls}">{icon}</div><div class="sent-desc">{desc}</div><div class="sent-vol">{vol_label}</div></div>'''

if not sentiment_html:
    sentiment_html = '<div class="no-data">No GDELT data available</div>'

# ── Tipping Point Races ────────────────────────────────────────────────────
tipping_html = ''
for t in tipping_point:
    margin = t.get('margin', 0)
    leader = t.get('leader', '?')
    lclass = 'dem-text' if leader == 'D' else 'rep-text'
    bar_w = max(min(margin * 250, 80), 5)
    tipping_html += f'''<div class="tip-row"><div class="mono tip-state">{esc(t.get('state',''))}</div><div class="tip-bar-wrap"><div class="tip-bar {'dem-bg' if leader=='D' else 'rep-bg'}" style="width:{bar_w}%"></div></div><div class="{lclass} mono tip-margin">{leader}+{margin*100:.0f}</div><div class="tip-prices"><span class="dem-text">{pct(t.get('d_price'))}</span> / <span class="rep-text">{pct(t.get('r_price'))}</span></div></div>'''

# ── Smart Money Cross-Signals ──────────────────────────────────────────────
smart_money_html = ''
for s in fec_cross_signals:
    strength = s.get('strength', '')
    if strength == 'divergence':
        badge = '<span style="color:#C75B39;font-weight:700">DIVERGE</span>'
    elif strength == 'aligned':
        badge = '<span style="color:#5B9A6B;font-weight:700">ALIGNED</span>'
    else:
        badge = '<span style="color:#D4A843;font-weight:600">MIXED</span>'
    sm_party = s.get('yes_party', '?')
    sm_cls = 'dem-text' if sm_party == 'D' else 'rep-text'
    fec_party = s.get('fec_advantage', '—')
    fec_cls = 'dem-text' if fec_party == 'D' else 'rep-text' if fec_party == 'R' else ''
    detail = s.get('detail', '')[:80]
    smart_money_html += f'''<div style="padding:3px 0;border-bottom:1px solid #F5F2EE"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1px"><span style="font-size:8.5px;font-weight:700">{esc(s.get('state',''))}</span>{badge}</div><div style="display:flex;gap:8px;font-size:7px"><span>Whales: <span class="{sm_cls}" style="font-weight:600">{sm_party}</span></span><span>FEC: <span class="{fec_cls}" style="font-weight:600">{fec_party}</span></span></div><div style="font-size:6.5px;color:#7A7568;margin-top:1px">{esc(detail)}</div></div>'''
if not smart_money_html:
    smart_money_html = '<div style="font-size:7px;color:#9B9B9B">No cross-signals detected</div>'

# ── PredictIt 3-Way Spreads ───────────────────────────────────────────────
pi_spreads_html = ''
for s in predictit_spreads:
    pi_d = s.get('predictit_d')
    po_d = s.get('polymarket_d')
    ka_d = s.get('kalshi_d')
    parts = []
    if pi_d is not None: parts.append(f'PI={pi_d*100:.0f}%')
    if po_d is not None: parts.append(f'Poly={po_d*100:.0f}%')
    if ka_d is not None: parts.append(f'Kalshi={ka_d*100:.0f}%')
    spread_pp = s.get('max_spread_pp', 0)
    pi_spreads_html += f'''<div style="padding:2px 0;border-bottom:1px solid #F5F2EE;display:flex;justify-content:space-between;align-items:center"><span style="font-size:7.5px"><span style="font-weight:700">{esc(s.get('state',''))}</span> <span style="color:#7A7568">{esc(s.get('race',''))}</span></span><span class="mono" style="font-size:7px;color:#C75B39;font-weight:600">{spread_pp:.0f}pp</span></div><div style="font-size:6.5px;color:#7A7568;margin-bottom:1px">{' / '.join(parts)}</div>'''

# ── Spending surges — dedup + enrich ───────────────────────────────────────
_seen_surges = {}
for s in spending_surges:
    key = (s.get('committee', ''), s.get('candidate', ''), s.get('support_oppose', ''))
    if key not in _seen_surges:
        _seen_surges[key] = s

surges_deduped = sorted(_seen_surges.values(), key=lambda x: x.get('amount', 0), reverse=True)[:5]
surges_total = sum(s.get('amount', 0) for s in surges_deduped)
surges_html = ''

if surges_deduped:
    surges_html += f'<div class="surge-summary"><span class="mono">${surges_total/1e6:.1f}M</span> total across {len(surges_deduped)} filing{"s" if len(surges_deduped) != 1 else ""}</div>'

for s in surges_deduped:
    amt = s.get('amount', 0)
    party = s.get('party', '?')
    so = s.get('support_oppose', '').lower()
    action_cls = 'surge-oppose' if so == 'oppose' else 'surge-support'
    action_label = 'OPPOSE' if so == 'oppose' else 'SUPPORT'
    office = s.get('office', '').title()
    state = s.get('state', '')
    candidate = s.get('candidate', '?')
    committee = s.get('committee', '')[:35]
    pclass = 'rep-text' if party == 'R' else 'dem-text'
    surges_html += f'''<div class="surge-row">
      <div class="surge-amt mono">${amt/1e6:.1f}M</div>
      <div class="surge-body">
        <div class="surge-top"><span class="surge-action {action_cls}">{action_label}</span> <span class="{pclass} surge-candidate">{esc(candidate)}</span> <span class="surge-office">{state} {office}</span></div>
        <div class="surge-pac">{esc(committee)}</div>
      </div>
    </div>'''

# ── Presidential frontrunners ──────────────────────────────────────────────
fr_r = presidential.get('frontrunner_r', {})
fr_d = presidential.get('frontrunner_d', {})
sen = scoreboard.get('senate', {})
hou = scoreboard.get('house', {})

# ── Generate Analysis Text ─────────────────────────────────────────────────
# Build data-driven analysis paragraphs from the numbers

pres_d = party_control.get('presidency', {}).get('democrat', 0) * 100
pres_r = party_control.get('presidency', {}).get('republican', 0) * 100
sen_d = party_control.get('senate', {}).get('democrat', 0) * 100
sen_r = party_control.get('senate', {}).get('republican', 0) * 100
hou_d = party_control.get('house', {}).get('democrat', 0) * 100
hou_r = party_control.get('house', {}).get('republican', 0) * 100

total_mkts = summary.get('total_markets', 0)
poly_ct = summary.get('polymarket_count', 0)
kalshi_ct = summary.get('kalshi_count', 0)
fec_ct = summary.get('fec_candidates_tracked', 0)

top_spread = cross_platform[0] if cross_platform else {}
ts_state = top_spread.get('state', '?')
ts_spread = top_spread.get('spread_pp', 0)
ts_poly_d = (top_spread.get('poly_d', 0) or 0) * 100
ts_kalshi_d = (top_spread.get('kalshi_d') or 0) * 100

# Fundraising leaders
fund_sorted = sorted(fundraising.items(), key=lambda x: (x[1].get('dem_cash', 0) or 0) + (x[1].get('rep_cash', 0) or 0), reverse=True)
top_fund_states = []
for st, f in fund_sorted[:5]:
    dc = f.get('dem_cash', 0) or 0
    rc = f.get('rep_cash', 0) or 0
    top_fund_states.append((st, dc, rc))

# Top money divergence
top_div = money_vs_odds[0] if money_vs_odds else {}
div_state = top_div.get('state', '?')
div_pp = top_div.get('divergence_pp', 0)

# Generic ballot
gb_dem = generic_ballot.get('dem_pct', 0)
gb_rep = generic_ballot.get('rep_pct', 0)
gb_margin = generic_ballot.get('margin', 0)
gb_polls = generic_ballot.get('poll_count', 0)

# Wiki spikes
wiki_names = [w.get('candidate', '?') for w in wiki_spikes]

# Build sections
analysis_1 = f"""<p>The consensus across {total_mkts:,} tracked markets prices a <strong>divided government</strong> as the likeliest 2026 outcome: Democrats hold the Senate at {sen_d:.0f}% (razor-thin, essentially a coin flip with slight D lean), <strong>Democrats are heavy favorites to retake the House at {hou_d:.0f}%</strong>, and Republicans hold the White House at {pres_r:.0f}% for 2028. The generic ballot (RCP, {gb_polls} polls) shows D+{gb_margin:.1f}, consistent with the House pricing. Cross-platform spreads are screaming — the <strong>{ts_state} spread is a staggering {ts_spread:.0f}pp between Polymarket ({ts_poly_d:.0f}% D) and Kalshi ({ts_kalshi_d:.0f}% D)</strong>."""

if wiki_names:
    analysis_1 += f""" Wiki attention shows {' and '.join(wiki_names[:2])} spiking above baseline, consistent with 2028 presidential positioning.</p>"""
else:
    analysis_1 += """</p>"""

analysis_2 = f"""<p><strong>The big story is the House.</strong> At {hou_d:.0f}% D, the market has priced in that the backlash cycle is locked — the president's party loses House seats in midterms, and with Republicans holding a thin margin, the math is brutal. This is consistent with historical base rates (the president's party has lost House seats in 37 of 40 midterms since 1862). Smart money is not fighting gravity here.</p>
<p><strong>The Senate is where the real action is.</strong> The {sen_d:.0f}/{sen_r:.0f} D/R line is deceptively tight and masks enormous state-level divergence. The {ts_state} spread ({ts_poly_d:.0f}% D on Polymarket vs {ts_kalshi_d:.0f}% D on Kalshi) is either a massive arbitrage or one platform has structurally mispriced this race."""

# Add FEC context for top spread state
for st, dc, rc in top_fund_states:
    if st == ts_state:
        analysis_2 += f""" FEC data shows <strong>Democrats outfunding Republicans ${dc/1e6:.1f}M to ${rc/1e6:.1f}M in {ts_state} cash-on-hand</strong>, suggesting the higher D price may be more credible."""
        break

analysis_2 += """</p>
<p><strong>The FEC money divergences are the most actionable signal.</strong> """

div_details = []
for m in money_vs_odds[:3]:
    div_details.append(f"{m.get('state', '?')} ({m.get('divergence_pp', 0):.0f}pp)")
analysis_2 += f"""Republicans are dramatically outfunding market expectations in <strong>{', '.join(div_details)}</strong>. When internal campaign spending diverges from public market pricing, one side has information the other doesn't.</p>"""

analysis_3 = f"""<p><strong>For Democrats:</strong> The House is yours to lose — don't get cute with resources. The Senate is the ballgame. Your cash advantages in {', '.join(st for st, dc, rc in top_fund_states[:3] if dc > rc)} are massive, but the market is only pricing a handful as truly competitive. Watch for hidden vulnerabilities where Republican money is flowing into races the market treats as safe-D.</p>
<p><strong>For Republicans:</strong> The {ts_state} race is your biggest disconnect. If the lower D price is right, you have a real shot at denying Democrats a firewall seat. The {div_state} over-investment pattern ({div_pp:.0f}pp divergence) suggests your internal numbers show something the market doesn't. Redirect resources from states where cash-on-hand is negligible to where the spread suggests opportunity.</p>"""

analysis_4 = f"""<p>Two of the biggest prediction markets in the world disagree by <strong>{ts_spread:.0f} percentage points</strong> on who wins the {ts_state} {top_spread.get('race_category', 'race')} — the largest cross-platform spread in the entire 2026 cycle. Polymarket has it at {ts_poly_d:.0f}% D, Kalshi at {ts_kalshi_d:.0f}% D. This isn't a rounding error; it's a fundamental disagreement about whether {ts_state} is competitive or a lock. In financial markets, a spread this wide would be arbitraged away in seconds. In political markets, it's persisting — and that tells you something about how thin and immature these markets still are, or that one side has information the other hasn't priced in yet.</p>"""

analysis_5 = f"""<p><strong>Platform biases:</strong> Polymarket skews toward crypto-native users (younger, more engaged, slightly D-lean on social issues). Kalshi has more traditional finance users. Large cross-platform spreads could partially reflect a partisan liquidity gap rather than genuine disagreement.</p>
<p><strong>Favorite-longshot bias:</strong> The {hou_d:.0f}% House D price feels about right historically but is vulnerable to the classic "something would have to go wrong" trap. True probability closer to {max(hou_d - 6, 70):.0f}-{max(hou_d - 4, 72):.0f}%.</p>
<p><strong>FEC data lags:</strong> Cash-on-hand figures are quarterly snapshots. Massive D fundraising advantages could narrow in Q2 if Republican small-dollar donors activate post-primary.</p>
<p><strong>Confidence level: MEDIUM.</strong> Chamber-level prices are roughly right. State-level prices have serious platform divergence. FEC-to-market divergences are highest-signal but require 1-2 more quarters to confirm.</p>"""

analysis_6 = f"""<p>The 2026 cycle is pricing out to a <strong>divided government (D House, knife-edge Senate, R White House 2028)</strong> — and the most actionable information isn't in the topline numbers, it's in the {ts_spread:.0f}pp {ts_state} spread and the massive FEC divergences where campaign money is chasing races the market has already written off. <strong>For campaigns:</strong> {ts_state} is the single most mispriced race in the entire cycle — whoever is right between platforms, someone has real money beating the odds in a state where markets can't even agree on the baseline. That's where the next move happens. <strong>For media:</strong> Stop covering the House as if it's competitive — it's {hou_d:.0f}% priced and historically predetermined. The story is the Senate, specifically the handful of states where money and markets violently disagree.</p>"""

# ── Newsletter Hook (headline + lede + alpha bullets) ─────────────────────

# Determine the dominant narrative for headline
if ts_spread >= 20:
    hook_headline = f"Markets Price Historic Divided Government — Plus the {ts_spread:.0f}-Point {ts_state} Gap That Has Traders Fighting"
elif sen_d > 48 and sen_d < 55:
    hook_headline = f"Senate Control Is a Coin Flip at {sen_d:.0f}% — While House Landslide Looks Locked at {hou_d:.0f}%"
else:
    hook_headline = f"2026 Midterm Markets: {total_mkts:,} Contracts, {ts_spread:.0f}pp Spreads, and the Money Trail That Contradicts the Odds"

# Determine who leads funding
top_fund_d_states = [st for st, dc, rc in top_fund_states[:5] if dc > rc]
top_fund_r_divs = [m.get('state', '?') for m in money_vs_odds[:3]]

hook_lede = f"Democrats are heavy favorites for the House ({hou_d:.0f}%), the Senate is a {'coin-flip' if abs(sen_d - 50) < 5 else 'lean-D' if sen_d > 55 else 'toss-up'} ({sen_d:.0f}%), and Republicans are {pres_r:.0f}% to hold the White House in 2028. The real story? A {ts_spread:.0f}-point spread on {ts_state} {top_spread.get('race_category', 'Senate')} that has Polymarket and Kalshi in open disagreement — and massive Republican cash surges in races the market has written off."

# Build alpha bullets
alpha_bullets = []
alpha_bullets.append(f"<strong>{ts_state} {top_spread.get('race_category', 'race').title()} is the most mispriced race in 2026</strong> — {ts_spread:.0f}pp gap between Polymarket ({ts_poly_d:.0f}% D) and Kalshi ({ts_kalshi_d:.0f}% D)")

if top_fund_d_states:
    biggest_d = top_fund_states[0]
    alpha_bullets.append(f"<strong>Democrats are outfunding Republicans {len(top_fund_d_states)}-to-1</strong> in top races — {biggest_d[0]} alone is ${biggest_d[1]/1e6:.0f}M D vs ${biggest_d[2]/1e6:.0f}M R")

if money_vs_odds:
    alpha_bullets.append(f"<strong>GOP money is chasing {', '.join(top_fund_r_divs)}</strong> despite long market odds — internal polling showing something the market doesn't?")

if wiki_names:
    alpha_bullets.append(f"<strong>2028 positioning is live:</strong> {' and '.join(wiki_names[:2])} are spiking in Wikipedia attention, signaling early jockeying")
elif gb_margin > 0:
    alpha_bullets.append(f"<strong>Generic ballot at D+{gb_margin:.1f}</strong> from {gb_polls} polls — consistent with historical midterm backlash cycle")

# ── Composite score gauge ──────────────────────────────────────────────────
comp_score = summary.get('composite_score', 0)
if comp_score >= 75:
    comp_cls, comp_word = 'comp-hot', 'HOT'
elif comp_score >= 55:
    comp_cls, comp_word = 'comp-high', 'ELEVATED'
elif comp_score >= 35:
    comp_cls, comp_word = 'comp-med', 'MODERATE'
else:
    comp_cls, comp_word = 'comp-low', 'QUIET'
comp_gauge_html = f'<div class="comp-gauge {comp_cls}"><div class="comp-dot"></div>{comp_score:.0f} — {comp_word}</div>'

# ── Conditional card highlighting ─────────────────────────────────────────
spreads_card_cls = ' hot' if ts_spread >= 15 else ''
mvo_card_cls = ' hot' if div_pp >= 20 else ''
surges_card_cls = ' hot' if len(spending_surges) >= 3 else ''

# ── Final HTML ─────────────────────────────────────────────────────────────
HTML = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Election Intelligence Brief</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  @page {{ size: Letter; margin: 0.25in 0.4in; }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:#FAF8F5; color:#2D2D2D; font-family:'Inter',-apple-system,sans-serif; font-size:8.5px; line-height:1.35; -webkit-font-smoothing:antialiased; -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
  .page {{ width:7.5in; margin:0 auto; }}

  /* Header */
  .header {{ display:flex; justify-content:space-between; align-items:flex-start; border-bottom:2px solid #C75B39; padding-bottom:4px; margin-bottom:6px; }}
  .brand {{ font-family:'JetBrains Mono',monospace; font-weight:600; font-size:13px; color:#C75B39; text-transform:uppercase; letter-spacing:3px; }}
  .rtitle {{ font-size:17px; font-weight:700; margin-top:1px; }}
  .rsub {{ font-size:8px; color:#7A7568; margin-top:1px; }}
  .hright {{ text-align:right; display:flex; flex-direction:column; align-items:flex-end; gap:3px; }}
  .conf {{ font-family:'JetBrains Mono',monospace; font-size:7px; font-weight:600; color:#C75B39; border:1.5px solid #C75B39; padding:2px 7px; letter-spacing:2px; text-transform:uppercase; }}
  .ts {{ font-size:7.5px; color:#7A7568; }}

  /* Grid */
  .g2 {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:7px; }}
  .g3 {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:8px; margin-bottom:7px; }}
  .g23 {{ display:grid; grid-template-columns:2fr 1fr; gap:8px; margin-bottom:7px; }}
  .span2 {{ grid-column:span 2; }}

  /* Cards */
  .card {{ background:#FFF; border:1px solid #E2DDD6; border-radius:5px; padding:6px 8px; }}
  .ct {{ font-family:'JetBrains Mono',monospace; font-size:7px; font-weight:600; color:#7A7568; text-transform:uppercase; letter-spacing:1.2px; margin-bottom:5px; padding-bottom:3px; border-bottom:1px solid #EDE9E3; }}

  /* Control bars */
  .control-row {{ display:flex; align-items:center; margin-bottom:4px; }}
  .control-row:last-child {{ margin-bottom:0; }}
  .control-label {{ width:54px; font-size:8px; font-weight:600; }}
  .control-bar-container {{ flex:1; }}
  .control-bar-track {{ display:flex; height:17px; border-radius:3px; overflow:hidden; background:#F5F2EE; }}
  .control-bar {{ display:flex; align-items:center; justify-content:center; min-width:28px; }}
  .dem-bar {{ background:#2B6CB0; }}
  .rep-bar {{ background:#B83A2E; }}
  .bar-label {{ font-family:'JetBrains Mono',monospace; font-size:7.5px; font-weight:600; color:white; }}

  /* Frontrunners */
  .fr-row {{ display:flex; gap:8px; margin-bottom:6px; }}
  .fr {{ flex:1; display:flex; align-items:center; gap:5px; padding:4px 8px; border-radius:4px; font-size:8px; }}
  .fr.dem {{ background:rgba(43,108,176,0.08); border:1px solid rgba(43,108,176,0.2); }}
  .fr.rep {{ background:rgba(184,58,46,0.08); border:1px solid rgba(184,58,46,0.2); }}
  .fr-name {{ font-weight:600; }}
  .fr-price {{ font-family:'JetBrains Mono',monospace; font-weight:700; font-size:11px; margin-left:auto; }}
  .fr-party {{ font-family:'JetBrains Mono',monospace; font-size:6.5px; font-weight:600; opacity:0.6; }}

  /* Colors */
  .dem-text {{ color:#2B6CB0; }}
  .rep-text {{ color:#B83A2E; }}
  .mono {{ font-family:'JetBrains Mono',monospace; }}
  .accent {{ font-weight:700; color:#C75B39; }}

  /* Tables */
  table {{ width:100%; border-collapse:collapse; font-size:7.5px; }}
  th {{ font-size:6.5px; font-weight:600; color:#7A7568; text-transform:uppercase; letter-spacing:0.5px; text-align:left; padding:2px 3px; border-bottom:1px solid #E2DDD6; }}
  td {{ padding:2.5px 3px; border-bottom:1px solid #F5F2EE; }}
  .hl {{ background:rgba(199,91,57,0.06); }}
  .hl td {{ font-weight:600; }}
  .det {{ font-size:7px; color:#4A4A4A; max-width:180px; }}

  /* Money vs Odds */
  .mvo-row {{ padding:3px 0; border-bottom:1px solid #F5F2EE; }}
  .mvo-row:last-child {{ border-bottom:none; }}
  .mvo-head {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:2px; }}
  .mvo-state {{ font-size:8.5px; font-weight:700; color:#2D2D2D; }}
  .mvo-div {{ font-size:8px; font-weight:700; color:#C75B39; }}
  .mvo-bar-wrap {{ display:flex; height:6px; border-radius:2px; overflow:hidden; margin-bottom:2px; }}
  .mvo-bar {{ min-width:2px; }}
  .dem-bg {{ background:#2B6CB0; }}
  .rep-bg {{ background:#B83A2E; }}
  .mvo-detail {{ display:flex; align-items:center; gap:4px; font-size:6.5px; }}
  .mvo-vs {{ color:#9B9B9B; font-size:6px; }}

  /* Chart */
  .chart-wrap {{ height:110px; position:relative; }}

  /* Attention — wiki + trends */
  .att-section-label {{ font-family:'JetBrains Mono',monospace; font-size:6.5px; font-weight:600; color:#7A7568; text-transform:uppercase; letter-spacing:0.8px; margin-bottom:4px; }}
  .att-row {{ display:flex; align-items:center; gap:4px; padding:3px 0; border-bottom:1px solid #F5F2EE; }}
  .att-row:last-child {{ border-bottom:none; }}
  .att-name {{ font-size:7.5px; font-weight:600; width:55px; flex-shrink:0; }}
  .att-name.dem {{ color:#2B6CB0; }}
  .att-name.rep {{ color:#B83A2E; }}
  .att-name.neu {{ color:#2D2D2D; }}
  .att-bar-wrap {{ flex:1; height:10px; background:#F5F2EE; border-radius:2px; overflow:hidden; }}
  .att-bar {{ height:100%; border-radius:2px; min-width:4px; }}
  .att-bar-dem {{ background:rgba(43,108,176,0.5); }}
  .att-bar-rep {{ background:rgba(184,58,46,0.5); }}
  .att-bar-neu {{ background:rgba(122,117,104,0.4); }}
  .att-z {{ font-family:'JetBrains Mono',monospace; font-size:7px; font-weight:600; color:#2D2D2D; width:26px; flex-shrink:0; text-align:right; }}
  .att-meta {{ font-size:6.5px; color:#9B9B9B; width:80px; flex-shrink:0; text-align:right; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .att-trend {{ display:flex; align-items:center; gap:4px; padding:2px 0; font-size:7px; }}
  .att-trend-topic {{ color:#2D2D2D; font-weight:500; }}
  .att-trend-traffic {{ font-family:'JetBrains Mono',monospace; font-size:6.5px; color:#7A7568; margin-left:auto; }}

  /* Sentiment */
  /* Sentiment - news coverage intel */
  .sent-row {{ display:flex; align-items:center; gap:4px; padding:3px 0; border-bottom:1px solid #F5F2EE; }}
  .sent-row:last-child {{ border-bottom:none; }}
  .sent-label {{ font-size:7.5px; font-weight:600; width:28px; flex-shrink:0; }}
  .sent-reading {{ font-family:'JetBrains Mono',monospace; font-size:7px; font-weight:600; width:16px; text-align:center; padding:1px 0; border-radius:2px; flex-shrink:0; }}
  .sent-reading.neg {{ background:rgba(184,58,46,0.1); color:#B83A2E; }}
  .sent-reading.pos {{ background:rgba(91,154,107,0.1); color:#5B9A6B; }}
  .sent-reading.neu {{ background:#F5F2EE; color:#9B9B9B; }}
  .sent-desc {{ font-size:7px; color:#4A4A4A; flex:1; line-height:1.3; }}
  .sent-vol {{ font-family:'JetBrains Mono',monospace; font-size:6.5px; color:#9B9B9B; width:40px; text-align:right; flex-shrink:0; }}

  /* Tipping point */
  .tip-row {{ display:flex; align-items:center; gap:4px; padding:2.5px 0; border-bottom:1px solid #F5F2EE; font-size:8px; }}
  .tip-row:last-child {{ border-bottom:none; }}
  .tip-state {{ width:22px; font-weight:600; font-size:8px; }}
  .tip-bar-wrap {{ flex:1; height:10px; background:#F5F2EE; border-radius:2px; overflow:hidden; }}
  .tip-bar {{ height:100%; border-radius:2px; }}
  .dem-bg {{ background:#2B6CB0; }}
  .rep-bg {{ background:#B83A2E; }}
  .tip-margin {{ width:32px; font-size:7.5px; font-weight:600; }}
  .tip-prices {{ font-size:7px; width:70px; text-align:right; }}

  /* Ticket splits */
  .split-row {{ display:flex; align-items:center; gap:6px; padding:2.5px 0; border-bottom:1px solid #F5F2EE; font-size:7.5px; }}
  .split-row:last-child {{ border-bottom:none; }}
  .split-state {{ width:22px; font-weight:600; }}
  .split-detail {{ flex:1; font-size:7px; }}
  .split-margin {{ width:30px; text-align:right; font-size:7.5px; font-weight:600; color:#2B6CB0; }}

  /* Spending surges */
  .surge-summary {{ font-size:7.5px; color:#4A4A4A; margin-bottom:5px; padding-bottom:4px; border-bottom:1px solid #EDE9E3; }}
  .surge-summary .mono {{ font-size:9px; font-weight:700; color:#2D2D2D; }}
  .surge-row {{ display:flex; align-items:flex-start; gap:6px; padding:3px 0; border-bottom:1px solid #F5F2EE; }}
  .surge-row:last-child {{ border-bottom:none; }}
  .surge-amt {{ font-size:8px; font-weight:700; color:#2D2D2D; width:32px; flex-shrink:0; padding-top:1px; }}
  .surge-body {{ flex:1; min-width:0; }}
  .surge-top {{ display:flex; align-items:center; gap:3px; flex-wrap:wrap; }}
  .surge-action {{ font-family:'JetBrains Mono',monospace; font-size:6px; font-weight:700; padding:1px 4px; border-radius:2px; letter-spacing:0.5px; }}
  .surge-oppose {{ background:rgba(184,58,46,0.1); color:#B83A2E; }}
  .surge-support {{ background:rgba(91,154,107,0.1); color:#5B9A6B; }}
  .surge-candidate {{ font-size:7.5px; font-weight:600; }}
  .surge-office {{ font-size:6.5px; color:#7A7568; }}
  .surge-pac {{ font-size:6.5px; color:#9B9B9B; margin-top:1px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}

  /* Ballot */
  .ballot-row {{ display:flex; align-items:center; gap:6px; font-size:8px; padding:2px 0; }}
  .ballot-lbl {{ font-weight:600; width:14px; }}
  .ballot-bar {{ height:12px; border-radius:2px; display:flex; align-items:center; justify-content:center; }}
  .ballot-bar span {{ font-family:'JetBrains Mono',monospace; font-size:7px; font-weight:600; color:white; }}

  /* Scoreboard */
  .sb-row {{ display:flex; justify-content:space-between; align-items:center; padding:2px 0; font-size:7.5px; border-bottom:1px solid #F5F2EE; }}
  .sb-row:last-child {{ border-bottom:none; }}
  .sb-ch {{ font-weight:600; width:44px; }}
  .sb-seats {{ font-family:'JetBrains Mono',monospace; font-size:7.5px; }}
  .sb-seats .d {{ color:#2B6CB0; font-weight:600; }}
  .sb-seats .r {{ color:#B83A2E; font-weight:600; }}
  .sb-seats .t {{ color:#9B9B9B; }}

  .no-data {{ font-size:7.5px; color:#9B9B9B; font-style:italic; padding:6px 0; }}

  /* Conditional card highlighting */
  .card.hot {{ border-left:3px solid #C75B39; }}

  /* Composite gauge */
  .comp-gauge {{ display:inline-flex; align-items:center; gap:5px; padding:2px 8px; border-radius:10px; font-family:'JetBrains Mono',monospace; font-size:8px; font-weight:700; }}
  .comp-gauge .comp-dot {{ width:8px; height:8px; border-radius:50%; }}
  .comp-gauge.comp-low {{ background:rgba(155,155,155,0.12); color:#7A7568; }}
  .comp-gauge.comp-low .comp-dot {{ background:#9B9B9B; }}
  .comp-gauge.comp-med {{ background:rgba(212,168,67,0.12); color:#B8860B; }}
  .comp-gauge.comp-med .comp-dot {{ background:#D4A843; }}
  .comp-gauge.comp-high {{ background:rgba(199,91,57,0.12); color:#C75B39; }}
  .comp-gauge.comp-high .comp-dot {{ background:#C75B39; }}
  .comp-gauge.comp-hot {{ background:rgba(184,58,46,0.15); color:#B83A2E; }}
  .comp-gauge.comp-hot .comp-dot {{ background:#B83A2E; }}

  /* Footer */
  .footer {{ margin-top:auto; padding-top:5px; border-top:1px solid #E2DDD6; font-size:6.5px; color:#9B9B9B; display:flex; justify-content:space-between; }}
  .page-flex {{ display:flex; flex-direction:column; min-height:calc(100vh - 0.4in); }}

  /* Movers strip */
  .movers-strip {{ display:flex; gap:6px; margin-bottom:6px; }}
  .mover {{ flex:1; background:#FFF; border:1px solid #E2DDD6; border-radius:4px; padding:6px 8px; }}
  .mover-delta {{ font-family:'JetBrains Mono',monospace; font-size:11px; font-weight:700; line-height:1; margin-bottom:2px; }}
  .mover-delta.up {{ color:#5B9A6B; }}
  .mover-delta.down {{ color:#B83A2E; }}
  .mover-outcome {{ font-size:7px; font-weight:600; color:#2D2D2D; line-height:1.2; }}
  .mover-question {{ font-size:6.5px; color:#7A7568; line-height:1.2; margin-top:1px; }}

  /* Alpha box */
  .alpha-box {{ background:#FFF; border:1px solid #E2DDD6; border-radius:5px; padding:8px 12px; margin-bottom:8px; border-left:4px solid #D4A843; }}
  .alpha-header {{ display:flex; align-items:center; gap:6px; margin-bottom:6px; }}
  .alpha-badge {{ font-family:'JetBrains Mono',monospace; font-size:6.5px; font-weight:600; color:#D4A843; text-transform:uppercase; letter-spacing:1.2px; }}
  .alpha-item {{ padding:4px 0; border-bottom:1px solid #F5F2EE; }}
  .alpha-item:last-child {{ border-bottom:none; }}
  .alpha-title {{ font-size:8.5px; font-weight:700; color:#2D2D2D; margin-bottom:2px; }}
  .alpha-detail {{ font-size:7.5px; line-height:1.45; color:#4A4A4A; }}
  .alpha-edge {{ font-family:'JetBrains Mono',monospace; font-size:7px; font-weight:600; color:#D4A843; }}

  /* Newsletter Hook */
  .hook {{ background:#FFF; border:1px solid #E2DDD6; border-radius:5px; padding:8px 12px 6px; margin-bottom:6px; border-left:4px solid #C75B39; }}
  .hook-headline {{ font-size:11px; font-weight:700; color:#2D2D2D; line-height:1.2; margin-bottom:3px; }}
  .hook-lede {{ font-size:7.5px; line-height:1.4; color:#4A4A4A; margin-bottom:5px; }}
  .hook-alpha {{ display:grid; grid-template-columns:1fr 1fr; gap:2px 12px; }}
  .hook-label {{ font-family:'JetBrains Mono',monospace; font-size:6.5px; font-weight:600; color:#C75B39; text-transform:uppercase; letter-spacing:1.5px; margin-bottom:4px; grid-column:span 2; }}
  .hook-bullets {{ list-style:none; padding:0; display:contents; }}
  .hook-bullets li {{ font-size:7.5px; line-height:1.4; color:#2D2D2D; padding:2px 0 2px 10px; position:relative; }}
  .hook-bullets li::before {{ content:''; position:absolute; left:0; top:7px; width:4px; height:4px; background:#C75B39; border-radius:50%; }}

  /* Analysis */
  .analysis {{ margin-top:10px; }}
  .analysis-section {{ display:flex; gap:10px; padding:10px 0; border-bottom:1px solid #EDE9E3; }}
  .analysis-section.last {{ border-bottom:none; padding-bottom:0; }}
  .analysis-num {{ font-family:'JetBrains Mono',monospace; font-size:20px; font-weight:700; color:#E2DDD6; line-height:1; width:22px; flex-shrink:0; }}
  .analysis-content {{ flex:1; }}
  .analysis-title {{ font-size:10px; font-weight:700; color:#C75B39; margin-bottom:4px; text-transform:uppercase; letter-spacing:0.5px; }}
  .analysis-text {{ font-size:8.5px; line-height:1.5; color:#2D2D2D; }}
  .analysis-text p {{ margin-bottom:5px; }}
  .analysis-text p:last-child {{ margin-bottom:0; }}
  .analysis-text strong {{ font-weight:600; }}

  @media print {{ body {{ background:#FAF8F5; }} }}
</style>
</head>
<body>
<div class="page">
<div class="page-flex">

  <!-- HEADER -->
  <div class="header">
    <div>
      <div class="brand">POLYCLAWD</div>
      <div class="rtitle">Election Intelligence Brief</div>
      <div class="rsub">2026 Midterm &mdash; {summary.get('total_markets',0):,} markets across {summary.get('polymarket_count',0)} Polymarket + {summary.get('kalshi_count',0)} Kalshi + {summary.get('manifold_count',0)} Manifold</div>
    </div>
    <div class="hright">
      <div class="conf">CONFIDENTIAL</div>
      <div class="ts">{ts_str}</div>
      {comp_gauge_html}
    </div>
  </div>

  <!-- NEWSLETTER HOOK -->
  <div class="hook">
    <div class="hook-headline">{hook_headline}</div>
    <div class="hook-lede">{hook_lede}</div>
    <div class="hook-alpha">
      <div class="hook-label">This Week's Alpha</div>
      <ul class="hook-bullets">
        {''.join(f'<li>{b}</li>' for b in alpha_bullets)}
      </ul>
    </div>
  </div>

  <!-- BIGGEST MOVES -->
  {movers_strip_html}

  <!-- FRONTRUNNERS -->
  <div style="font-family:'JetBrains Mono',monospace;font-size:7px;font-weight:600;color:#7A7568;text-transform:uppercase;letter-spacing:1.2px;margin-bottom:4px;">2028 Presidential Frontrunners</div>
  <div class="fr-row">
    <div class="fr dem"><div class="fr-party">D</div><div class="fr-name dem-text">{esc(fr_d.get('name',''))}</div><div class="fr-price dem-text">{pct(fr_d.get('price'))}</div></div>
    <div class="fr rep"><div class="fr-party">R</div><div class="fr-name rep-text">{esc(fr_r.get('name',''))}</div><div class="fr-price rep-text">{pct(fr_r.get('price'))}</div></div>
  </div>

  <!-- ROW 1: Control + Scoreboard/Ballot -->
  <div class="g23">
    <div class="card">
      <div class="ct">Party Control Probability</div>
      {control_bars_html}
    </div>
    <div class="card">
      <div class="ct">Seat Projections</div>
      <div class="sb-row"><div class="sb-ch">Senate</div><div class="sb-seats"><span class="d">D {sen.get('d_total','')}</span> <span class="t">T {sen.get('tossup','')}</span> <span class="r">R {sen.get('r_total','')}</span></div></div>
      <div class="sb-row"><div class="sb-ch">House</div><div class="sb-seats"><span class="d">D {hou.get('d_total','')}</span> <span class="t">T {hou.get('tossup','')}</span> <span class="r">R {hou.get('r_total','')}</span></div></div>
      <div style="margin-top:4px;padding-top:3px;border-top:1px solid #EDE9E3">
        <div style="font-family:'JetBrains Mono',monospace;font-size:6.5px;font-weight:600;color:#7A7568;text-transform:uppercase;letter-spacing:1px;margin-bottom:3px">Generic Ballot (RCP)</div>
        <div class="ballot-row"><div class="ballot-lbl dem-text">D</div><div class="ballot-bar" style="width:{generic_ballot.get('dem_pct',45)*1.4}%;background:#2B6CB0"><span>{generic_ballot.get('dem_pct',''):.1f}%</span></div></div>
        <div class="ballot-row"><div class="ballot-lbl rep-text">R</div><div class="ballot-bar" style="width:{generic_ballot.get('rep_pct',42)*1.4}%;background:#B83A2E"><span>{generic_ballot.get('rep_pct',''):.1f}%</span></div></div>
        <div style="font-size:6.5px;color:#9B9B9B;margin-top:1px">D+{generic_ballot.get('margin',0):.1f} &mdash; {generic_ballot.get('poll_count',0)} polls</div>
      </div>
    </div>
  </div>

  <!-- ROW 2: Spreads + Money vs Odds -->
  <div class="g2">
    <div class="card{spreads_card_cls}">
      <div class="ct">Cross-Platform Spreads</div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:6px;font-weight:600;color:#7A7568;text-transform:uppercase;letter-spacing:1px;margin-bottom:2px">Polymarket vs Kalshi</div>
      <table><tr><th>St</th><th>Race</th><th>Poly D</th><th>Poly R</th><th>Kal D</th><th>Kal R</th><th>Spread</th></tr>{spreads_rows}</table>
      {f'<div style="font-family:JetBrains Mono,monospace;font-size:6px;font-weight:600;color:#7A7568;text-transform:uppercase;letter-spacing:1px;margin-top:5px;margin-bottom:2px;padding-top:4px;border-top:1px solid #EDE9E3">3-Way Spreads (incl. PredictIt)</div>' + pi_spreads_html if pi_spreads_html else ''}
    </div>
    <div class="card{mvo_card_cls}">
      <div class="ct">FEC Money vs Market Odds</div>
      {mvo_html}
    </div>
  </div>

  <!-- ROW 3: Fundraising + Tipping Point + Ticket Splits -->
  <div class="g3">
    <div class="card">
      <div class="ct">Top Fundraising (Cash on Hand, $M)</div>
      <div class="chart-wrap"><canvas id="fundChart"></canvas></div>
    </div>
    <div class="card">
      <div class="ct">Tipping Point Races</div>
      {tipping_html}
    </div>
    <div class="card">
      <div class="ct">Smart Money vs FEC</div>
      {smart_money_html}
    </div>
  </div>

  <!-- ROW 4: Attention + Sentiment + Super PAC Surges -->
  <div class="g3">
    <div class="card">
      <div class="ct">Public Attention Signals</div>
      {attention_html}
    </div>
    <div class="card">
      <div class="ct">Media Coverage Sentiment (7d)</div>
      {sentiment_html}
    </div>
    <div class="card{surges_card_cls}">
      <div class="ct">Super PAC Surges (eFiling)</div>
      {surges_html if surges_html else '<div class="no-data">No recent surges detected</div>'}
    </div>
  </div>

  <!-- PAGE 1 FOOTER -->
  <div class="footer">
    <div>Page 1 of 2 &mdash; 10-source pipeline</div>
    <div>Generated {ts_str}</div>
  </div>
</div><!-- /page-flex -->

  <!-- PAGE BREAK -->
  <div style="page-break-before:always"></div>

  <!-- ANALYSIS HEADER -->
  <div class="header" style="margin-top:0">
    <div>
      <div class="brand">POLYCLAWD</div>
      <div class="rtitle">Election Intel Analysis</div>
    </div>
    <div class="hright">
      <div class="conf">CONFIDENTIAL</div>
      <div class="ts">{ts_str}</div>
    </div>
  </div>

  <!-- ANALYSIS BODY -->
  <div class="analysis">
    <div class="analysis-section">
      <div class="analysis-num">1</div>
      <div class="analysis-content">
        <div class="analysis-title">Market Snapshot</div>
        <div class="analysis-text">{analysis_1}</div>
      </div>
    </div>
    <div class="analysis-section">
      <div class="analysis-num">2</div>
      <div class="analysis-content">
        <div class="analysis-title">What the Markets Are Actually Pricing</div>
        <div class="analysis-text">{analysis_2}</div>
      </div>
    </div>
    <div class="analysis-section">
      <div class="analysis-num">3</div>
      <div class="analysis-content">
        <div class="analysis-title">Strategic Implications</div>
        <div class="analysis-text">{analysis_3}</div>
      </div>
    </div>
    <div class="analysis-section">
      <div class="analysis-num">4</div>
      <div class="analysis-content">
        <div class="analysis-title">The Story Right Now</div>
        <div class="analysis-text">{analysis_4}</div>
      </div>
    </div>
    <div class="analysis-section">
      <div class="analysis-num">5</div>
      <div class="analysis-content">
        <div class="analysis-title">Risks &amp; Biases Check</div>
        <div class="analysis-text">{analysis_5}</div>
      </div>
    </div>
    <div class="analysis-section last">
      <div class="analysis-num">6</div>
      <div class="analysis-content">
        <div class="analysis-title">Bottom-Line Takeaway</div>
        <div class="analysis-text">{analysis_6}</div>
      </div>
    </div>
  </div>

  <!-- ALPHA OPPORTUNITIES -->
  {alpha_box_html}

  <!-- PAGE 2 FOOTER -->
  <div class="footer">
    <div>Page 2 of 2 &mdash; Polymarket, Kalshi, FEC, eFiling, PredictIt, Manifold, Wikipedia, Google Trends, GDELT, RealClearPolitics</div>
    <div>Generated {ts_str}</div>
  </div>

</div>

<script>
document.addEventListener('DOMContentLoaded', function() {{
  const ctx = document.getElementById('fundChart');
  if (ctx) {{
    new Chart(ctx, {{
      type: 'bar',
      data: {{
        labels: {fund_states},
        datasets: [
          {{ label: 'Democrat', data: {fund_dem}, backgroundColor: '#2B6CB0', borderRadius: 2 }},
          {{ label: 'Republican', data: {fund_rep}, backgroundColor: '#B83A2E', borderRadius: 2 }}
        ]
      }},
      options: {{
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{
          legend: {{ display: true, position: 'bottom', labels: {{ font: {{ size: 7, family: 'Inter' }}, boxWidth: 8, padding: 4 }} }}
        }},
        scales: {{
          x: {{ stacked: true, ticks: {{ font: {{ size: 6.5, family: 'JetBrains Mono' }}, callback: function(v) {{ return '$' + v + 'M'; }} }}, grid: {{ color: '#F5F2EE' }} }},
          y: {{ stacked: true, ticks: {{ font: {{ size: 7.5, family: 'JetBrains Mono' }}, padding: 2 }}, grid: {{ display: false }}, afterFit: function(axis) {{ axis.width = 28; }} }}
        }}
      }}
    }});
  }}
}});
</script>
</body>
</html>'''

with open(output_html, 'w') as f:
    f.write(HTML)
print(f'HTML written to {output_html}')

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f'file://{output_html}')
        page.wait_for_timeout(3000)
        page.pdf(
            path=output_pdf,
            format='Letter',
            print_background=True,
            margin={'top': '0.2in', 'bottom': '0.2in', 'left': '0.35in', 'right': '0.35in'}
        )
        browser.close()
    print(f'PDF saved to {output_pdf}')

    # ── Update archive index + latest symlink ──────────────────────────
    archive_path = os.path.join(_reports_dir, 'archive.json')
    archive = []
    if os.path.exists(archive_path):
        try:
            with open(archive_path) as af:
                archive = json.load(af)
        except (json.JSONDecodeError, IOError):
            archive = []

    # Key metrics for this report
    top_sp = cross_platform[0] if cross_platform else {}
    entry = {
        'timestamp': timestamp,
        'generated': _now.isoformat(),
        'filename': os.path.basename(output_pdf),
        'total_markets': summary.get('total_markets', 0),
        'composite_score': summary.get('composite_score', 0),
        'composite_label': summary.get('composite_label', ''),
        'presidency': {'d': party_control.get('presidency', {}).get('democrat', 0), 'r': party_control.get('presidency', {}).get('republican', 0)},
        'senate': {'d': party_control.get('senate', {}).get('democrat', 0), 'r': party_control.get('senate', {}).get('republican', 0)},
        'house': {'d': party_control.get('house', {}).get('democrat', 0), 'r': party_control.get('house', {}).get('republican', 0)},
        'top_spread': {'state': top_sp.get('state', ''), 'race': top_sp.get('race_category', ''), 'pp': top_sp.get('spread_pp', 0)},
        'top_divergence': {'state': money_vs_odds[0].get('state', '') if money_vs_odds else '', 'pp': money_vs_odds[0].get('divergence_pp', 0) if money_vs_odds else 0},
        'wiki_spikes': len(wiki_spikes),
        'gtrends_spikes': len(gtrends_spikes),
    }
    archive.append(entry)

    with open(archive_path, 'w') as af:
        json.dump(archive, af, indent=2)

    # Symlink latest
    latest_link = os.path.join(_reports_dir, 'latest.pdf')
    if os.path.islink(latest_link) or os.path.exists(latest_link):
        os.remove(latest_link)
    os.symlink(output_pdf, latest_link)

    print(f'Archive updated ({len(archive)} reports)')

except ImportError:
    print('Playwright not installed.')
except Exception as e:
    print(f'PDF error: {e}')
