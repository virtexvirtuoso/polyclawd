# Polyclawd - Polymarket Trading Bot

**Virtuoso Crypto's AI-powered prediction market trading system.**

Combines intelligent scanning with safe execution for Polymarket trading.

## Features

| Feature | Description |
|---------|-------------|
| **Dashboard** | Portfolio overview, P&L tracking, open positions |
| **Arb Scanner** | Find same-platform arbitrage (Yes + No ≠ $1.00) |
| **Cross-Platform Arb** | Polymarket vs Kalshi price gaps |
| **Whale Tracker** | Monitor top traders with live balances |
| **Liquidity Rewards** | Optimal market making opportunities |
| **Paper Trading** | Practice with virtual $10,000 |

## Quick Start

### Local Development

```bash
cd ~/Desktop/polyclawd
python3 -m venv venv
source venv/bin/activate
pip install -r api/requirements.txt
uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

Open: **http://127.0.0.1:8000**

### Production (VPS)

```bash
# Service runs on port 8420
sudo systemctl status polyclawd-api
```

## Pages

| Page | Path | Description |
|------|------|-------------|
| Dashboard | `/` | Portfolio summary, positions, P&L |
| Arb Scanner | `/arb.html` | Same-platform arbitrage finder |
| Cross-Platform | `/cross-arb.html` | Polymarket vs Kalshi arb |
| Whales | `/whales.html` | Whale tracker with live balances |
| Rewards | `/rewards.html` | Liquidity reward opportunities |
| Trade | `/trade.html` | Paper trading interface |
| Markets | `/markets.html` | Market browser & search |

## API Endpoints

### Paper Trading

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/balance` | Portfolio balance with position values |
| GET | `/api/positions` | Open positions with live P&L |
| GET | `/api/trades?limit=N` | Trade history |
| POST | `/api/trade` | Execute paper trade |
| POST | `/api/reset` | Reset to $10,000 |

### Markets

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/markets/trending` | Trending markets by volume |
| GET | `/api/markets/search?q=query` | Search markets |
| GET | `/api/markets/{id}` | Market details |
| GET | `/api/arb-scan` | Same-platform arbitrage scan |
| GET | `/api/rewards` | Liquidity reward opportunities |

### Whale Tracker

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/whales` | List tracked whales with metadata |
| GET | `/api/whales/balances` | Live USDC/POL balances (Polygon RPC) |
| GET | `/api/whales/positions` | All whale positions (Polymarket Data API) |
| GET | `/api/whales/{address}` | Single whale details |
| GET | `/api/whales/{address}/positions` | Single whale positions |

### Cross-Platform Arbitrage

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/cross-arb` | Fuzzy match scan (Polymarket vs Kalshi) |
| GET | `/api/cross-arb/curated` | Curated pairs scan (more accurate) |
| GET | `/api/cross-arb/pairs` | List configured market pairs |
| GET | `/api/cross-arb/matches` | All matched markets |

### Simmer SDK (Trade Execution)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/simmer/status` | Agent status and balance |
| GET | `/api/simmer/markets` | Active Simmer markets |
| GET | `/api/simmer/positions` | Current positions |
| POST | `/api/simmer/trade` | Execute live trade |

## File Structure

```
polyclawd/
├── api/
│   ├── main.py              # FastAPI backend (all endpoints)
│   └── requirements.txt     # Python dependencies
├── config/
│   ├── whale_config.json    # Tracked whale addresses
│   └── cross-arb-pairs.json # Curated Polymarket-Kalshi pairs
├── css/
│   └── virtuoso.css         # Dark cyberpunk theme
├── js/
│   └── app.js               # Frontend logic
├── index.html               # Dashboard
├── arb.html                 # Same-platform arb
├── cross-arb.html           # Cross-platform arb
├── whales.html              # Whale tracker
├── rewards.html             # Liquidity rewards
├── trade.html               # Paper trading
├── markets.html             # Market browser
├── start.sh                 # Startup script
└── README.md
```

## Configuration

### Whale Config (`config/whale_config.json`)

```json
{
  "whales": [
    {
      "address": "0x...",
      "name": "Theo",
      "profit_estimate": "$8M+",
      "win_rate": "72%"
    }
  ]
}
```

### Cross-Arb Pairs (`config/cross-arb-pairs.json`)

```json
{
  "pairs": [
    {
      "id": "next-pope",
      "name": "Next Pope",
      "polymarket_keywords": ["next pope", "papal"],
      "kalshi_tickers": ["KXNEWPOPE"]
    }
  ]
}
```

## Design System

| Element | Value |
|---------|-------|
| Theme | Dark cyberpunk terminal |
| Primary | `#fbbf24` (neon amber) |
| Background | `#0a0a0a` (dark) |
| Cards | `#111111` |
| Font (UI) | Inter |
| Font (Data) | IBM Plex Mono |

## Data Sources

| Source | Purpose |
|--------|---------|
| Polymarket Gamma API | Market data, prices, volume |
| Polymarket Data API | User positions, P&L |
| Kalshi API | Cross-platform price comparison |
| Polygon RPC | On-chain wallet balances |
| Simmer SDK | Trade execution (optional) |

## Notes

- **Paper Trading** - Uses virtual money with real market data
- **No API Keys Required** - Read-only access to public APIs
- **Simmer Optional** - Only needed for live trade execution
- **Data Persisted** - Trades saved in `~/.polyclawd/paper-trading/`

## Links

- **Dashboard:** https://virtuosocrypto.com/polyclawd
- **Simmer SDK:** https://simmer.markets
- **Polymarket:** https://polymarket.com
- **Kalshi:** https://kalshi.com

---

*Built by Virt for Virtuoso Crypto*
