
> ## ⛔ DEPRECATED (Feb 2026)
> 
> The Odds API is no longer used by Polyclawd. No API key was configured on VPS.
> **Replaced by:** ActionNetwork API (`odds/sports_odds.py`) — free, no key, 18+ books.
> This document is kept for historical reference only.

# The Odds API - Pricing & Upgrade Research

**Research Date:** February 8, 2026  
**URL:** https://the-odds-api.com

## Current Tier

**Tier:** Starter (FREE)  
**Credits:** 500/month  
**Cost:** $0/month

## Available Tiers

| Tier | Monthly Cost | Credits/Month | Notes |
|------|-------------|---------------|-------|
| **Starter** | FREE | 500 | Current tier |
| **20K** | $30 | 20,000 | 40x increase |
| **100K** | $59 | 100,000 | 200x increase |
| **5M** | $119 | 5,000,000 | Best value for heavy usage |
| **15M** | $249 | 15,000,000 | Enterprise-level |

## What's Included (All Tiers)

- ✅ All sports (NFL, NBA, MLB, NHL, Soccer, etc.)
- ✅ All bookmakers (DraftKings, FanDuel, BetMGM, Betfair, etc.)
- ✅ All betting markets (moneyline, spreads, totals, futures)
- ✅ Historical odds data
- ✅ JSON format
- ✅ American & Decimal odds formats
- ✅ Google Sheets / Excel add-ons

## Coverage

### Sports
- **Football:** NFL, College Football (NCAA), CFL, AFL
- **Soccer:** EPL, Bundesliga, La Liga, Serie A, Champions League, World Cup
- **Basketball:** NBA, NCAA, WNBA, Euroleague
- **Baseball:** MLB, MiLB, KBO, NPB
- **Hockey:** NHL, AHL, SHL, Liiga
- **Other:** Cricket, Rugby, Golf, Tennis, MMA, Politics

### Bookmakers
- **US:** DraftKings, FanDuel, BetMGM, Caesars, Bovada, MyBookie
- **UK:** Unibet, William Hill, Ladbrokes, Betfair, Bet Victor, Paddy Power
- **EU:** 1xBet, Pinnacle, Betfair, Unibet
- **AU:** Sportsbet, TAB, Neds, Ladbrokes

## Credit Usage

- Each API request costs credits based on complexity
- Standard odds request: ~1 credit per sport-region combination
- Historical data: Higher credit cost

## Recommendation for Polyclawd

### Current Usage Analysis
- Polling Vegas odds every ~12 hours
- 6 sports x 2 regions = ~12 credits/day = 360 credits/month
- **Current tier is sufficient for basic polling**

### If Scaling Up
For real-time odds (e.g., every 5 minutes):
- 12 requests/hour × 24 hours × 30 days = 8,640 requests/month
- **20K tier ($30/month)** would be adequate

For heavy usage (multiple sports, high frequency):
- **100K tier ($59/month)** recommended

### Cost-Benefit
| Scenario | Tier | Monthly Cost | ROI Threshold |
|----------|------|-------------|---------------|
| Casual monitoring | Starter | $0 | N/A |
| Active trading | 20K | $30 | Need 1-2 winning edges |
| Algo trading | 100K | $59 | Need 3-4 winning edges |

## Action Items

1. **Short term:** Continue with free tier + ESPN fallback (unlimited, free)
2. **If expanding:** Upgrade to 20K tier ($30/mo)
3. **For production:** Consider 100K tier for reliability

## API Key Management

Current key location:
- macOS Keychain: `the-odds-api` service
- Environment: `ODDS_API_KEY`

## Related Resources

- API Docs: https://the-odds-api.com/liveapi/guides/v4/
- Historical Odds: https://the-odds-api.com/historical-odds-data/
- Betting Markets: https://the-odds-api.com/sports-odds-data/betting-markets.html
