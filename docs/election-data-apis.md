# Election Prediction Market Data Sources — API Research

> Research conducted 2026-04-07. Focus: FREE or low-cost APIs for building trading signals against Polymarket/Kalshi election markets.

## Table of Contents

1. [Polling Data](#1-polling-data)
2. [Race Ratings](#2-race-ratings)
3. [Money & Spending](#3-money--spending)
4. [Structured Race Data](#4-structured-race-data)
5. [Additional Prediction Markets](#5-additional-prediction-markets)
6. [Alternative Data](#6-alternative-data)
7. [Signal Integration Matrix](#7-signal-integration-matrix)

---

## 1. Polling Data

### 1A. RealClearPolitics (RCP)

| Field | Detail |
|-------|--------|
| **Access Method** | Web scraping + undocumented JSON endpoints |
| **Base URL** | `https://www.realclearpolling.com/` (rebranded from realclearpolitics.com/epolls) |
| **Auth** | None (public pages) |
| **Rate Limits** | No published limits; be respectful (1 req/sec) |
| **Update Frequency** | Averages updated as new polls are added (typically daily during election season) |
| **Cost** | Free |
| **Status** | Active but no official API. Site was rebranded to RealClearPolling in 2024. |

**Data Provided:**
- Polling averages by race (President, Senate, House, Governor)
- Individual poll results with sample size, margin of error, date range
- Head-to-head matchups and favorability ratings
- Generic ballot averages

**JSON Endpoint Pattern** (undocumented, reverse-engineered):
Each poll page has a numeric ID in its URL. The JSON data can be accessed via the page's embedded JavaScript data objects. The Python library [`realclearpolitics`](https://pypi.org/project/realclearpolitics/) wraps this scraping.

**Signal Value:** Polling average vs. market price divergence. When RCP average implies 55% win probability but Polymarket prices a candidate at 48 cents, that's a 7-point signal.

```python
# pip install realclearpolitics
from realclearpolitics import get_polls

# Pass the RCP poll page URL
url = "https://www.realclearpolling.com/polls/president/general/2028/..."
polls = get_polls(url)

for poll in polls:
    print(f"{poll['Poll']}: {poll['Date']} | Spread: {poll['Spread']}")
    # Fields: Poll, Date, Sample, MoE, candidate names with %
```

**Limitations:**
- Library not actively maintained (last PyPI release stale)
- Scraping may break with site redesigns
- No historical average time series via API — only current snapshot


### 1B. FiveThirtyEight / Silver Bulletin

| Field | Detail |
|-------|--------|
| **Access Method** | GitHub CSV downloads (538 archive) + Silver Bulletin Substack downloads |
| **538 Archive** | `https://github.com/fivethirtyeight/data/tree/master/polls` |
| **Silver Bulletin** | `https://www.natesilver.net/` (Substack, partially paywalled) |
| **Auth** | None for GitHub CSVs; Substack subscription for model outputs |
| **Rate Limits** | GitHub raw file rate limits (~60 req/hr unauthenticated) |
| **Update Frequency** | 538 archive is frozen (site shut down March 2025). Silver Bulletin updates during election cycles. |
| **Cost** | Free (raw polls) / $8/mo (Silver Bulletin premium for model outputs) |

**Data Provided:**
- **538 GitHub (archived):** 12,000+ historical polls across president, senate, house, governor, approval, generic ballot. CSV fields include `question_id`, `poll_id`, `pollster`, `methodology`, `sample_size`, `population`, `pct` per candidate, `created_at`
- **Silver Bulletin:** Pollster ratings database (public), polling database of 12,000+ polls (public), model forecasts (paid subscribers only)

**Signal Value:** Pollster-quality-weighted averages are more predictive than simple RCP averages. 538's pollster ratings let you weight polls by historical accuracy.

```python
import pandas as pd

# FiveThirtyEight archived polls (still accessible on GitHub)
POLLS_URL = "https://raw.githubusercontent.com/fivethirtyeight/data/master/polls/president_polls.csv"
df = pd.read_csv(POLLS_URL)

# Filter to recent cycle
df['end_date'] = pd.to_datetime(df['end_date'])
recent = df[df['cycle'] == 2024].sort_values('end_date', ascending=False)

# Pollster ratings for quality weighting
RATINGS_URL = "https://raw.githubusercontent.com/fivethirtyeight/data/master/pollster-ratings/2023/pollster-ratings.csv"
ratings = pd.read_csv(RATINGS_URL)
# Fields: Pollster, Races Polled, Predictive Plus-Minus, Mean-Reverted Bias, etc.
```

**Limitations:**
- 538 data frozen at March 2025 shutdown — no new polls being added
- Silver Bulletin model outputs are paywalled
- NYT is building a successor poll tracker but no public API yet


### 1C. Wikipedia Polling Tables

| Field | Detail |
|-------|--------|
| **Access Method** | HTML table scraping via `pandas.read_html()` or `wikipedia` API |
| **Auth** | None |
| **Rate Limits** | Wikipedia API: 200 req/sec for good-faith bots |
| **Update Frequency** | Community-edited, often within hours of poll release |
| **Cost** | Free |

**Data Provided:**
- Comprehensive polling tables for every major race
- Typically includes: pollster, date, sample size, margin of error, candidate percentages
- Historical polling tables for past elections

**Signal Value:** Broad coverage — Wikipedia often has polls not yet on RCP. Good for cross-referencing.

```python
import pandas as pd

# Scrape polling table directly from Wikipedia
url = "https://en.wikipedia.org/wiki/Nationwide_opinion_polling_for_the_2028_United_States_presidential_election"
tables = pd.read_html(url)

# The main polling table is usually the largest
polling_table = max(tables, key=len)
print(f"Found {len(polling_table)} polls")
print(polling_table.head())
```

**Limitations:**
- Table structure varies by page and changes over time
- Data quality depends on volunteer editors
- Need to handle merged cells, footnotes, and formatting inconsistencies

---

## 2. Race Ratings

### 2A. Cook Political Report

| Field | Detail |
|-------|--------|
| **Access Method** | REST API (JSON) |
| **Base URL** | `https://www.cookpolitical.com/api/race/` |
| **Auth** | HTTP Basic Auth (base64-encoded `email:password`) |
| **Rate Limits** | Once per day (ratings change at most daily) |
| **Update Frequency** | Irregular — updated when analysts change ratings |
| **Cost** | Paid subscription required for API access (contact for pricing) |

**Endpoints:**
- `GET /house` — House race ratings + Cook PVI scores
- `GET /senate` — Senate race ratings
- `GET /governor` — Governor race ratings
- `GET /presidential` — Presidential/Electoral College ratings

**Response Fields:**
- House: `Title`, `State`, `District`, `Incumbent`, `Cook_PVI`, `Rating`, `Cycle`, `Rating_date`
- Senate: `Title`, `State`, `Incumbent`, `Rating`
- Presidential: `Title`, `State`, `Electoral_votes`, `Rating`, `Cycle`, `Rating_date`

**Rating Scale:** Solid D/R, Likely D/R, Lean D/R, Toss Up

**Signal Value:** Rating changes are high-impact events. When Cook shifts a race from "Lean R" to "Toss Up," that's a signal the market should reprice. Map ratings to implied probabilities: Solid=95%, Likely=80%, Lean=65%, Toss Up=50%.

```python
import requests
import base64

email = "your@email.com"
password = "your_password"
auth_string = base64.b64encode(f"{email}:{password}".encode()).decode()

headers = {"Authorization": f"Basic {auth_string}"}

# Get Senate ratings
resp = requests.get("https://www.cookpolitical.com/api/race/senate", headers=headers)
races = resp.json()

for race in races:
    print(f"{race['State']}: {race['Rating']} (Incumbent: {race['Incumbent']})")
```

**Limitations:**
- Paid access only — pricing not public
- Once-per-day rate limit
- No historical ratings via API (archives available separately)


### 2B. Sabato's Crystal Ball (UVA Center for Politics)

| Field | Detail |
|-------|--------|
| **Access Method** | Web scraping only — no public API |
| **URL** | `https://centerforpolitics.org/crystalball/` |
| **Auth** | None |
| **Rate Limits** | Standard web scraping courtesy (1 req/sec) |
| **Update Frequency** | Published as articles; rating change pages updated irregularly |
| **Cost** | Free (public website) |

**Data Provided:**
- Race ratings for House, Senate, Governor, Presidential
- Rating scale: Safe, Likely, Leans, Toss Up
- 2026 rating changes tracked at `centerforpolitics.org/crystalball/2026-rating-changes/`

**Signal Value:** Second-opinion on Cook ratings. When Crystal Ball and Cook disagree, the divergence itself is informative. Crystal Ball shifting a race before Cook often front-runs the market.

```python
import requests
from bs4 import BeautifulSoup

url = "https://centerforpolitics.org/crystalball/2026-rating-changes/"
resp = requests.get(url)
soup = BeautifulSoup(resp.text, 'html.parser')

# Parse the rating changes table
tables = soup.find_all('table')
for table in tables:
    rows = table.find_all('tr')
    for row in rows[1:]:  # skip header
        cols = [td.text.strip() for td in row.find_all('td')]
        if cols:
            print(f"Race: {cols[0]} | Old: {cols[1]} | New: {cols[2]}")
```

**Limitations:**
- Scraping-only, fragile to layout changes
- No structured API
- Updates are editorial, not data-driven


### 2C. Inside Elections

| Field | Detail |
|-------|--------|
| **Access Method** | REST API (documented) |
| **Base URL** | `https://www.insideelections.com/developer/ratings` |
| **Auth** | Likely API key or subscription (details require contacting them) |
| **Rate Limits** | Not publicly documented |
| **Update Frequency** | Irregular — analyst-driven |
| **Cost** | Subscription required |

**Data Provided:**
- Race ratings for House, Senate, Governor, President
- Rating scale similar to Cook: Safe, Likely, Lean, Tilt, Toss Up

**Signal Value:** Third rating service — consensus across Cook, Crystal Ball, and Inside Elections creates a "ratings consensus" signal. Three-way agreement is much stronger than any single rater.

```python
# Access details require subscription — placeholder for API pattern
import requests

# Endpoint pattern (requires auth)
resp = requests.get(
    "https://www.insideelections.com/api/ratings/senate",
    headers={"Authorization": "Bearer YOUR_API_KEY"}
)
# Response structure TBD based on subscription access
```

**Limitations:**
- Paid subscription required
- Limited public documentation

---

## 3. Money & Spending

### 3A. OpenSecrets (Center for Responsive Politics)

| Field | Detail |
|-------|--------|
| **Access Method** | Bulk data downloads only (API discontinued April 2025) |
| **Data URL** | `https://www.opensecrets.org/open-data/bulk-data` |
| **Auth** | Account required for bulk downloads |
| **Rate Limits** | N/A (download-based) |
| **Update Frequency** | Periodic (FEC filing cycles) |
| **Cost** | Free for educational/non-commercial use |
| **Status** | API DISCONTINUED as of April 15, 2025 after 17 years |

**Data Provided (via bulk download):**
- Campaign contributions by donor, candidate, PAC
- Independent expenditure spending
- Lobbying data
- Personal financial disclosures
- 30+ years of historical data

**Signal Value:** PAC spending surges on a race signal that sophisticated money sees an opportunity. Sudden IE spending against a candidate can precede polling drops.

```python
import pandas as pd

# Download bulk data from OpenSecrets (manual download required first)
# Files are in pipe-delimited format
# Example: Independent Expenditures
ie_data = pd.read_csv(
    "path/to/indivs_2026.txt",
    sep="|",
    header=None,
    names=["cycle", "fecTransId", "contribId", "contrib", "recipId",
           "orgName", "ultOrg", "realCode", "date", "amount",
           "street", "city", "state", "zip", "recipCode", "type",
           "cmbdCode", "otherID", "gender", "microfilm", "occupation",
           "employer", "source"]
)
```

**Limitations:**
- No more API access — bulk download only
- Data processing required (pipe-delimited files)
- Updates lag behind FEC filings


### 3B. FEC OpenFEC API — Independent Expenditures & PAC Spending

| Field | Detail |
|-------|--------|
| **Access Method** | REST API |
| **Base URL** | `https://api.open.fec.gov/v1/` |
| **Auth** | API key from [api.data.gov/signup](https://api.data.gov/signup/) |
| **Rate Limits** | 1,000 requests/hour (default key) |
| **Update Frequency** | Near real-time for e-filed reports (24-48h reporting requirement for IEs) |
| **Cost** | Free |

**Key Endpoints for IE/PAC Spending:**

| Endpoint | Purpose |
|----------|---------|
| `/schedules/schedule_e/` | Itemized independent expenditures |
| `/schedules/schedule_e/by_candidate/` | IE spending aggregated by candidate |
| `/schedules/schedule_e/totals/` | IE totals by candidate and committee |
| `/committee/{committee_id}/` | Committee (PAC/Super PAC) details |
| `/elections/` | Candidates and spending by election |

**Key Parameters:**
- `candidate_id` — FEC candidate ID (e.g., P00000001)
- `committee_id` — PAC/Super PAC committee ID
- `cycle` — Election cycle (2026)
- `support_oppose_indicator` — S (support) or O (oppose)
- `is_notice` — True for 24/48-hour IE reports (most time-sensitive)
- `sort` — Sort by `-expenditure_date` for newest first

**Signal Value:** 24-hour IE notice filings are the fastest money signal. When a Super PAC drops $5M in a race via 24-hour notice, markets often haven't priced it in yet. Track `is_notice=true` filings for real-time signals.

```python
import requests

FEC_API_KEY = "your_api_key_from_data_gov"
BASE = "https://api.open.fec.gov/v1"

# Get recent 24/48-hour independent expenditure notices
resp = requests.get(f"{BASE}/schedules/schedule_e/", params={
    "api_key": FEC_API_KEY,
    "cycle": 2026,
    "is_notice": True,
    "sort": "-expenditure_date",
    "per_page": 20,
})
data = resp.json()

for ie in data["results"]:
    print(f"{ie['committee']['name']} spent ${ie['expenditure_amount']:,.0f} "
          f"{'supporting' if ie['support_oppose_indicator'] == 'S' else 'opposing'} "
          f"{ie['candidate_name']} on {ie['expenditure_date']}")

# Aggregate IE spending by candidate
resp2 = requests.get(f"{BASE}/schedules/schedule_e/by_candidate/", params={
    "api_key": FEC_API_KEY,
    "cycle": 2026,
    "candidate_id": "S2AZ00561",  # example Senate candidate
})
totals = resp2.json()
for t in totals["results"]:
    print(f"  {t['support_oppose_indicator']}: ${t['total']:,.0f} from {t['committee_name']}")
```

**Limitations:**
- 1,000 req/hr limit (sufficient for periodic polling, tight for real-time)
- Pagination required for large result sets (use `last_indexes` keyset pagination)
- Paper-filed reports have significant delays

---

## 4. Structured Race Data

### 4A. Ballotpedia API

| Field | Detail |
|-------|--------|
| **Access Method** | REST API |
| **Base URL** | `https://api.ballotpedia.org/v1/` |
| **Docs** | `https://developer.ballotpedia.org/` |
| **Auth** | API key (subscription required) |
| **Rate Limits** | Not publicly documented |
| **Update Frequency** | Weekly data refreshes |
| **Cost** | Paid subscription (contact `data@ballotpedia.org` for pricing) |

**Data Provided:**
- Candidate information: names, party, incumbency, bio, contact, campaign website
- District boundary maps
- Election dates and filing deadlines
- Coverage: all state legislative, executive, congressional elections + top 100 cities

**Signal Value:** Structural data enrichment — know filing deadlines, candidate withdrawals, and district-level context. A candidate dropping out of a primary is a tradeable event.

```python
import requests

# Ballotpedia API (requires paid subscription key)
headers = {"Authorization": "Bearer YOUR_BALLOTPEDIA_KEY"}

# Get candidates for a specific election
resp = requests.get("https://api.ballotpedia.org/v1/elections", params={
    "year": 2026,
    "type": "general",
    "state": "AZ",
    "office": "senate"
}, headers=headers)

candidates = resp.json()
for c in candidates.get("data", []):
    print(f"{c['name']} ({c['party']}) - Incumbent: {c.get('is_incumbent', False)}")
```

**Limitations:**
- Paid access only — likely expensive for full API
- Pricing not transparent (must contact sales)


### 4B. AP Elections API

| Field | Detail |
|-------|--------|
| **Access Method** | REST API |
| **Base URL** | Enterprise access (contact `elections_api_info@ap.org`) |
| **Auth** | API key (enterprise agreement) |
| **Rate Limits** | Per-agreement |
| **Update Frequency** | Real-time on election night; periodic pre-election |
| **Cost** | Enterprise pricing (used by major news orgs — likely $$$) |

**Data Provided:**
- Real-time election results (precinct-level on election night)
- Race calls (AP race caller decisions)
- Delegate counts
- Candidate information
- Historical results

**Signal Value:** The gold standard for election night — AP race calls move markets instantly. Pre-election, less useful (data is the same as what you'd find elsewhere).

```python
# AP Elections API — enterprise access only
# Python wrapper: pip install elex (NYT's open-source wrapper)
# https://github.com/newsdev/elex

# NOTE: Requires AP API credentials
from elex.api import Elections

e = Elections(api_key="YOUR_AP_KEY")

# Get results for a specific race
results = e.get_results(race_id="12345")
for result in results:
    print(f"{result.candidate_name}: {result.vote_count} ({result.vote_pct}%)")
```

**Limitations:**
- Enterprise pricing — prohibitively expensive for individual use
- Overkill for pre-election signals (value is election-night real-time)
- `elex` wrapper may be outdated

---

## 5. Additional Prediction Markets

### 5A. Manifold Markets

| Field | Detail |
|-------|--------|
| **Access Method** | REST API + WebSocket |
| **Base URL** | `https://api.manifold.markets/v0` |
| **Docs** | [docs.manifold.markets/api](https://docs.manifold.markets/api) |
| **Auth** | `Authorization: Key {key}` (generated from profile) |
| **Rate Limits** | 500 requests/minute per IP |
| **Update Frequency** | Real-time |
| **Cost** | Free (play money + sweepstakes markets) |

**Key Endpoints:**

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v0/search-markets` | GET | Search markets by query, sort, topic |
| `/v0/market/{id}` | GET | Single market with probability |
| `/v0/market/{id}/positions` | GET | User positions in market |
| `/v0/market-probs` | GET | Batch probabilities (up to 100) |
| `/v0/bet` | POST | Place bet |

**Response Fields:** `id`, `question`, `url`, `probability`, `pool`, `totalLiquidity`, `volume`, `outcomeType`, `isResolved`, `resolution`, `closeTime`

**WebSocket:** `wss://api.manifold.markets/ws` — subscribe to `contract/{marketId}` for real-time updates

**Signal Value:** Manifold markets are play-money / low-stakes sweepstakes. They tend to be less efficient than Polymarket/Kalshi. Divergence between Manifold and real-money markets can indicate either (a) a market inefficiency on Manifold or (b) information not yet priced into real-money markets. Use as a sentiment/crowd indicator rather than a price signal.

```python
import requests

BASE = "https://api.manifold.markets/v0"

# Search for election markets
resp = requests.get(f"{BASE}/search-markets", params={
    "term": "2026 senate",
    "sort": "liquidity",
    "limit": 20,
})
markets = resp.json()

for m in markets:
    prob = m.get("probability", 0)
    print(f"{m['question'][:80]}")
    print(f"  Prob: {prob:.1%} | Volume: ${m.get('volume', 0):,.0f} | Liquidity: ${m.get('totalLiquidity', 0):,.0f}")

# Batch probability check for multiple markets
market_ids = [m["id"] for m in markets[:10]]
resp2 = requests.get(f"{BASE}/market-probs", params={"ids": ",".join(market_ids)})
probs = resp2.json()
```

**Limitations:**
- Play money / sweepstakes — lower signal quality than real-money markets
- Market creation is open — many low-quality/thinly-traded markets
- Liquidity much lower than Polymarket/Kalshi


### 5B. Metaculus

| Field | Detail |
|-------|--------|
| **Access Method** | REST API |
| **Base URL** | `https://www.metaculus.com/api2/` |
| **Docs** | `https://www.metaculus.com/api2/schema/redoc/` (OpenAPI) |
| **Auth** | API token (from account settings) |
| **Rate Limits** | Not publicly documented; be courteous |
| **Update Frequency** | Community predictions update continuously |
| **Cost** | Free |

**Key Endpoints:**

| Endpoint | Purpose |
|----------|---------|
| `GET /api2/questions/` | List/search questions |
| `GET /api2/questions/{id}/` | Single question details |
| `GET /api2/questions/{id}/predictions/` | Community prediction distribution |

**Query Parameters:** `search=`, `status=` (open, closed, resolved), `project=`, `order_by=`

**Signal Value:** Metaculus has a strong forecasting community with calibrated predictions. Their community median is often more accurate than prediction markets for long-horizon questions. Divergence between Metaculus community forecast and market price is a quality signal.

```python
import requests

BASE = "https://www.metaculus.com/api2"

# Search for election-related questions
resp = requests.get(f"{BASE}/questions/", params={
    "search": "2026 US Senate",
    "status": "open",
    "order_by": "-activity",
    "limit": 20,
})
data = resp.json()

for q in data.get("results", []):
    community_pred = q.get("community_prediction", {})
    median = community_pred.get("full", {}).get("q2")  # median
    print(f"{q['title'][:80]}")
    print(f"  Community median: {median}")
    print(f"  Forecasters: {q.get('number_of_forecasters', 0)}")
```

**Limitations:**
- Fewer political questions than pure election markets
- Predictions are distributions, not simple probabilities (more complex to compare)
- No real-money stakes — but community is highly calibrated


### 5C. PredictIt

| Field | Detail |
|-------|--------|
| **Access Method** | REST API (read-only, no trading API) |
| **Base URL** | `https://www.predictit.org/api/marketdata/` |
| **Auth** | None required for market data |
| **Rate Limits** | Not published; data refreshes every 60 seconds |
| **Update Frequency** | Prices updated every 60 seconds |
| **Cost** | Free (data); 10% profit fee + 5% withdrawal fee on trades |
| **Status** | Operational — survived CFTC shutdown attempt; $3,500 contract cap (raised from $850) |

**Key Endpoints:**

| Endpoint | Purpose |
|----------|---------|
| `GET /all/` | All active markets with contracts and prices |
| `GET /markets/{id}` | Single market details |

**Response Fields per Contract:**
- `lastTradePrice` — Last traded price
- `bestBuyYesCost` / `bestBuyNoCost` — Current bid/ask
- `bestSellYesCost` / `bestSellNoCost` — Current sell prices
- `lastClosePrice` — Previous day close

**Signal Value:** PredictIt has high fees (10% profit + 5% withdrawal) that distort prices. A candidate at 55c on PredictIt is more like 50c fee-adjusted. Cross-market arbitrage: compare PredictIt fee-adjusted prices to Polymarket/Kalshi for true divergences.

```python
import requests

# All markets — no auth needed
resp = requests.get("https://www.predictit.org/api/marketdata/all/")
data = resp.json()

for market in data["markets"]:
    print(f"\n{market['name']} (ID: {market['id']})")
    for contract in market["contracts"]:
        print(f"  {contract['name']}: "
              f"Last={contract['lastTradePrice']} "
              f"BuyYes={contract['bestBuyYesCost']} "
              f"BuyNo={contract['bestBuyNoCost']}")

# Fee-adjusted probability calculation
def fee_adjusted_prob(predictit_price, profit_fee=0.10, withdrawal_fee=0.05):
    """Convert PredictIt price to true implied probability accounting for fees."""
    if predictit_price is None:
        return None
    # PredictIt takes 10% of profits and 5% of withdrawals
    # True prob = price / (1 - profit_fee * (1 - price) - withdrawal_fee * price)
    effective = predictit_price / (1 - profit_fee * (1 - predictit_price))
    return round(effective, 3)
```

**Limitations:**
- No trading API — manual trade execution only
- No historical price API (CSV download for past data)
- High fees distort raw prices
- Lower liquidity than Polymarket/Kalshi
- Non-commercial data license

---

## 6. Alternative Data

### 6A. Google Trends

| Field | Detail |
|-------|--------|
| **Access Method** | Official API (alpha, limited access) + PyTrends (unofficial) |
| **Official API** | `https://trends.googleapis.com/v1alpha/trends:query` |
| **PyTrends** | `pip install pytrends` |
| **Auth** | Official: OAuth 2.0 (alpha invite only). PyTrends: None (uses browser cookies) |
| **Rate Limits** | Official: ~5 queries/day at daily resolution. PyTrends: ~10-20 req/min before throttling |
| **Update Frequency** | Hourly for real-time, daily for historical |
| **Cost** | Free |

**Data Provided:**
- Normalized search interest (0-100) over time
- Interest by region / sub-region
- Related queries and topics
- Real-time trending searches

**Signal Value:** Search volume spikes for candidate names often precede polling movements. A sudden 3x spike in "[Candidate] scandal" searches can predict a polling drop before polls are released. Compare search volume ratios between candidates to market prices.

```python
from pytrends.request import TrendReq

pytrends = TrendReq(hl='en-US', tz=360)

# Compare candidate search interest
candidates = ["DeSantis", "Whitmer", "Shapiro"]
pytrends.build_payload(candidates, timeframe='now 7-d', geo='US')

interest = pytrends.interest_over_time()
print(interest.tail())

# Interest by state — compare to state-level markets
regional = pytrends.interest_by_region(resolution='REGION')
print(regional.sort_values(candidates[0], ascending=False).head(10))

# Related queries — detect emerging narratives
related = pytrends.related_queries()
for candidate in candidates:
    rising = related[candidate].get('rising')
    if rising is not None:
        print(f"\n{candidate} — Rising searches:")
        print(rising.head(5))
```

**Limitations:**
- PyTrends can break without warning (Google blocks/changes endpoints)
- Official API is alpha with extremely limited quota (apply at developers.google.com)
- Data is normalized (relative, not absolute volume) — hard to compare across time periods
- 429 rate limiting is aggressive with PyTrends


### 6B. ActBlue / WinRed Donation Velocity (via FEC)

| Field | Detail |
|-------|--------|
| **Access Method** | OpenFEC API (Schedule A individual contributions) |
| **Base URL** | `https://api.open.fec.gov/v1/schedules/schedule_a/` |
| **Auth** | data.gov API key |
| **Rate Limits** | 1,000 req/hr |
| **Update Frequency** | Quarterly filings + periodic (daily e-filings for large committees) |
| **Cost** | Free |

**ActBlue Committee ID:** `C00401224`
**WinRed Committee ID:** `C00694323`

**Data Provided:**
- Itemized individual contributions (>$200)
- Contribution date, amount, donor name, employer, occupation
- Earmarked contributions through to candidate committees

**Signal Value:** Donation velocity (contributions/day) is a leading indicator. A candidate whose small-dollar fundraising doubles in a week often sees polling improvement 2-4 weeks later. Compare D vs R donation velocity ratios to market-implied probabilities.

```python
import requests
from datetime import datetime, timedelta

FEC_API_KEY = "your_key"
BASE = "https://api.open.fec.gov/v1"

# ActBlue recent contributions (earmarked to specific candidates)
resp = requests.get(f"{BASE}/schedules/schedule_a/", params={
    "api_key": FEC_API_KEY,
    "committee_id": "C00401224",  # ActBlue
    "two_year_transaction_period": 2026,
    "sort": "-contribution_receipt_date",
    "per_page": 20,
    "min_date": (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"),
})
data = resp.json()

for contrib in data["results"]:
    print(f"${contrib['contribution_receipt_amount']:,.0f} from {contrib['contributor_name']} "
          f"earmarked to {contrib.get('memo_text', 'N/A')[:50]} "
          f"on {contrib['contribution_receipt_date']}")
```

**Limitations:**
- Only itemized contributions (>$200) in near-real-time
- Sub-$200 donations aggregated in quarterly reports — significant lag
- Earmarking memo text requires parsing to identify destination candidate
- FEC data can be 24-48 hours behind for e-filed reports


### 6C. Congressional Trading Disclosures

| Field | Detail |
|-------|--------|
| **Access Method** | Third-party APIs (multiple providers) |
| **Options** | Capitol Trades (no API), FMP, Finnhub, Apify actors |
| **Auth** | Varies by provider |
| **Rate Limits** | Varies |
| **Update Frequency** | Within 45 days of trade (STOCK Act requirement) |
| **Cost** | Free tier available on several providers |

**Best Free Options:**

| Provider | Endpoint | Free Tier |
|----------|----------|-----------|
| **Finnhub** | `GET /api/v1/stock/congressional-trading` | 60 calls/min |
| **FMP** | `GET /api/v4/senate-trading` + `/house-trading` | 250 calls/day |
| **Apify** | Actor-based scraping | 30 sec/month free |

**Signal Value:** Limited for election prediction directly. More useful for sector/stock signals. However, unusual trading patterns by members in competitive races could indicate insider knowledge about policy or race outcomes.

```python
import requests

# Finnhub congressional trading (free tier)
FINNHUB_KEY = "your_finnhub_key"

resp = requests.get("https://finnhub.io/api/v1/stock/congressional-trading", params={
    "symbol": "",  # empty for all trades
    "from": "2026-01-01",
    "to": "2026-04-07",
    "token": FINNHUB_KEY,
})
trades = resp.json()

for trade in trades.get("data", [])[:10]:
    print(f"{trade['name']} ({trade['chamber']}): "
          f"{trade['transactionType']} {trade['assetDescription'][:40]} "
          f"${trade.get('amountFrom', 'N/A')}-${trade.get('amountTo', 'N/A')}")
```

**Limitations:**
- 45-day disclosure lag makes this a slow signal
- House Stock Watcher (previously best free source) S3 bucket went offline mid-2025
- Paper filings add additional delays
- Limited direct election signal — more of a stock-trading alpha source

---

## 7. Signal Integration Matrix

### Priority Ranking for Election Market Trading

| Priority | Source | Signal Type | Cost | Latency | Integration Effort |
|----------|--------|-------------|------|---------|-------------------|
| **P0** | OpenFEC IE (Schedule E) | 24h IE spending surges | Free | Hours | Low |
| **P0** | PredictIt API | Cross-market arb | Free | 60s | Low |
| **P0** | Manifold Markets API | Cross-market divergence | Free | Real-time | Low |
| **P1** | RCP / 538 Polls | Poll-vs-market divergence | Free | Daily | Medium |
| **P1** | Google Trends | Search momentum | Free | Hourly | Medium |
| **P1** | Cook Political Report | Rating change events | Paid | Daily | Low |
| **P2** | Metaculus | Calibrated forecaster divergence | Free | Daily | Medium |
| **P2** | Wikipedia Polls | Broad poll coverage | Free | Daily | Medium |
| **P2** | FEC ActBlue/WinRed | Donation velocity | Free | Days | High |
| **P3** | Crystal Ball / Inside Elections | Rating consensus | Free/Paid | Weekly | Medium |
| **P3** | Ballotpedia | Structural race data | Paid | Weekly | Low |
| **P3** | Congressional Trading | Insider behavior | Free | 45 days | Medium |
| **P4** | AP Elections API | Election night results | $$$ | Real-time | High |

### Recommended Implementation Order

**Phase 1 — Free Cross-Market Signals (immediate value):**
1. PredictIt `/api/marketdata/all/` — compare to Polymarket/Kalshi prices
2. Manifold Markets `/v0/search-markets` — play-money divergence detector
3. OpenFEC Schedule E `is_notice=true` — 24h IE spending alerts

**Phase 2 — Polling Divergence (requires matching logic):**
4. RCP scraping or 538 GitHub polls — build polling average
5. Google Trends candidate search volume ratios
6. Wikipedia polling table scraper

**Phase 3 — Expert Ratings (event-driven):**
7. Cook Political Report API (if budget allows)
8. Crystal Ball scraper (free alternative)
9. Rating consensus calculator across all three services

**Phase 4 — Deep Data (research advantage):**
10. FEC ActBlue/WinRed donation velocity analysis
11. Metaculus calibrated forecast comparison
12. Ballotpedia structural data enrichment

### Composite Signal Formula (Proposed)

```
election_edge = (
    w1 * poll_market_divergence      # polling avg - market price
  + w2 * cross_market_arb            # PredictIt/Manifold vs Polymarket
  + w3 * money_momentum              # IE spending velocity signal
  + w4 * search_momentum             # Google Trends candidate ratio
  + w5 * rating_shift                # Cook/Crystal Ball rating changes
  + w6 * forecaster_divergence       # Metaculus community vs market
)
```

Where weights are calibrated via backtesting against 2024 election market outcomes.
