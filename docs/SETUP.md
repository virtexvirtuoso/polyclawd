# Polyclawd Setup Guide

Complete installation and deployment guide.

---

## Table of Contents

- [Requirements](#requirements)
- [Local Development](#local-development)
- [Environment Variables](#environment-variables)
- [VPS Deployment](#vps-deployment)
- [MCP Server Setup](#mcp-server-setup)
- [Quick Start Commands](#quick-start-commands)

---

## Requirements

| Component | Version | Notes |
|-----------|---------|-------|
| Python | 3.11+ | 3.14 recommended |
| pip | Latest | For dependency management |
| git | Latest | For version control |

### Python Dependencies

```
fastapi==0.109.0
uvicorn[standard]==0.27.0
pydantic==2.5.3
aiofiles
slowapi
httpx
pytest
pytest-asyncio
locust
```

---

## Local Development

### 1. Clone Repository

```bash
git clone https://github.com/virtuosocrypto/polyclawd.git
cd polyclawd
```

### 2. Create Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate  # macOS/Linux
# or
.\venv\Scripts\activate   # Windows
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment

```bash
cp .env.example .env
# Edit .env with your settings
```

### 5. Run Development Server

```bash
# Option 1: Use start script
./start.sh

# Option 2: Run directly
uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

Access at: `http://127.0.0.1:8000`

---

## Environment Variables

Create a `.env` file in the project root:

```bash
# API Authentication
POLYCLAWD_API_KEYS=dev-test-key,admin-key

# Optional: Odds API (for Vegas lines)
ODDS_API_KEY=your-odds-api-key

# Optional: Polymarket credentials (for live trading)
POLY_API_KEY=your-polymarket-api-key
POLY_API_SECRET=your-polymarket-secret
POLY_WALLET_ADDRESS=0x...

# Optional: External services
BETFAIR_API_KEY=your-betfair-key
KALSHI_EMAIL=your@email.com
KALSHI_PASSWORD=your-password
```

### Required vs Optional

| Variable | Required | Description |
|----------|----------|-------------|
| `POLYCLAWD_API_KEYS` | Yes | Comma-separated API keys |
| `ODDS_API_KEY` | No | The Odds API for Vegas lines |
| `POLY_*` | No | Only for live Polymarket trading |
| `BETFAIR_*` | No | Only for Betfair edge detection |
| `KALSHI_*` | No | Only for Kalshi integration |

---

## VPS Deployment

### Recommended Specs

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 1 vCPU | 2 vCPU |
| RAM | 1 GB | 2 GB |
| Storage | 10 GB | 20 GB |
| OS | Ubuntu 22.04 | Ubuntu 24.04 |

### 1. Initial Server Setup

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python
sudo apt install python3.11 python3.11-venv python3-pip git -y

# Create app user (optional but recommended)
sudo useradd -m -s /bin/bash polyclawd
sudo su - polyclawd
```

### 2. Deploy Application

```bash
# Clone repo
cd ~
git clone https://github.com/virtuosocrypto/polyclawd.git
cd polyclawd

# Setup venv
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
nano .env  # Add your API keys
```

### 3. Create Systemd Service

Create `/etc/systemd/system/polyclawd.service`:

```ini
[Unit]
Description=Polyclawd Prediction Market Bot
After=network.target

[Service]
Type=simple
User=polyclawd
Group=polyclawd
WorkingDirectory=/home/polyclawd/polyclawd
Environment=PATH=/home/polyclawd/polyclawd/venv/bin
ExecStart=/home/polyclawd/polyclawd/venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/polyclawd/polyclawd/data /home/polyclawd/polyclawd/storage /home/polyclawd/polyclawd/logs

[Install]
WantedBy=multi-user.target
```

### 4. Enable and Start Service

```bash
sudo systemctl daemon-reload
sudo systemctl enable polyclawd
sudo systemctl start polyclawd

# Check status
sudo systemctl status polyclawd

# View logs
sudo journalctl -u polyclawd -f
```

### 5. Nginx Reverse Proxy (Optional)

```nginx
server {
    listen 80;
    server_name yourdomain.com;

    location /polyclawd/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## MCP Server Setup

Polyclawd includes an MCP (Model Context Protocol) server for Claude integration.

### 1. MCP Server Location

```
polyclawd/mcp/server.py
```

### 2. Configure Claude Desktop

Add to `~/.claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "polyclawd": {
      "command": "python3",
      "args": ["/path/to/polyclawd/mcp/server.py"],
      "env": {}
    }
  }
}
```

### 3. Available MCP Tools (52 total)

| Category | Tools |
|----------|-------|
| **Core Signals** | polyclawd_signals, polyclawd_news, polyclawd_volume_spikes, polyclawd_smart_money, polyclawd_inverse_whale |
| **Arbitrage** | polyclawd_arb_scan, polyclawd_kalshi_edge, polyclawd_manifold_edge, polyclawd_metaculus_edge, polyclawd_predictit_edge, polyclawd_betfair_edge, polyclawd_polyrouter_edge |
| **Vegas Odds** | polyclawd_vegas_nfl, polyclawd_vegas_superbowl, polyclawd_vegas_soccer, polyclawd_vegas_epl, polyclawd_vegas_ucl, polyclawd_vegas_edge |
| **ESPN** | polyclawd_espn_moneyline, polyclawd_espn_moneylines, polyclawd_espn_edge |
| **Markets** | polyclawd_markets_trending, polyclawd_markets_search, polyclawd_markets_new, polyclawd_markets_opportunities |
| **PolyRouter** | polyclawd_polyrouter_markets, polyclawd_polyrouter_search, polyclawd_polyrouter_sports |
| **Engine** | polyclawd_engine, polyclawd_engine_start, polyclawd_engine_stop, polyclawd_engine_trigger |
| **Trading** | polyclawd_trades, polyclawd_positions, polyclawd_balance |
| **Paper** | polyclawd_phase, polyclawd_simulate, polyclawd_simmer_portfolio, polyclawd_simmer_status |
| **Learning** | polyclawd_keywords, polyclawd_learn, polyclawd_confidence_sources, polyclawd_confidence_calibration |
| **Resolution** | polyclawd_resolution_approaching, polyclawd_resolution_imminent, polyclawd_rotation_candidates |
| **System** | polyclawd_health, polyclawd_metrics |

### 4. Test MCP Server

```bash
cd polyclawd
python mcp/server.py
# Then send JSON-RPC requests via stdin
```

---

## Quick Start Commands

### Development

```bash
# Start server
./start.sh

# Run with reload
uvicorn api.main:app --reload --host 127.0.0.1 --port 8000

# Run tests
pytest

# Load testing
locust -f tests/load/locustfile.py
```

### Production

```bash
# Service management
sudo systemctl start polyclawd
sudo systemctl stop polyclawd
sudo systemctl restart polyclawd
sudo systemctl status polyclawd

# View logs
sudo journalctl -u polyclawd -f
sudo journalctl -u polyclawd --since "1 hour ago"

# Update deployment
cd ~/polyclawd
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart polyclawd
```

### Health Checks

```bash
# Local
curl http://127.0.0.1:8000/api/health

# Production
curl https://virtuosocrypto.com/polyclawd/api/health
curl https://virtuosocrypto.com/polyclawd/api/metrics
```

### Quick API Tests

```bash
# Get signals
curl https://virtuosocrypto.com/polyclawd/api/signals

# Search markets
curl "https://virtuosocrypto.com/polyclawd/api/markets/search?q=bitcoin"

# Get Vegas edge
curl https://virtuosocrypto.com/polyclawd/api/vegas/edge

# Get NFL odds
curl https://virtuosocrypto.com/polyclawd/api/vegas/nfl
```

---

## Troubleshooting

### Common Issues

| Issue | Solution |
|-------|----------|
| Port 8000 in use | `lsof -i :8000` then kill process |
| Permission denied | Check file permissions, run as correct user |
| Module not found | Activate venv: `source venv/bin/activate` |
| API key rejected | Check `.env` format (comma-separated, no spaces) |

### Log Locations

| Log | Location |
|-----|----------|
| Application | `logs/polyclawd.log` |
| Systemd | `journalctl -u polyclawd` |
| Nginx | `/var/log/nginx/access.log` |

See [operations/TROUBLESHOOTING.md](operations/TROUBLESHOOTING.md) for detailed troubleshooting.
