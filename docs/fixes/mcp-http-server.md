# Polyclawd MCP HTTP Server

**Deployed:** 2026-02-24  
**Port:** 8421  
**Public:** `https://virtuosocrypto.com/polyclawd/mcp`  
**Transport:** streamable-http (FastMCP)

---

## Architecture

```
MCP Client (Claude, mcporter, etc.)
    ↓ streamable-http
nginx (/polyclawd/mcp → localhost:8421)
    ↓
FastMCP Server (polyclawd-mcp.service)
    ↓ internal HTTP
Polyclawd API (localhost:8420)
```

## Files

| File | Purpose |
|---|---|
| `mcp/app.py` | FastMCP instance ("polyclawd" v3.0.2) |
| `mcp/http_server.py` | HTTP entrypoint, 83+ tools registered |
| `mcp/server.py` | Original stdio server (unchanged, still works) |

## Systemd Service

```
/etc/systemd/system/polyclawd-mcp.service
```

```bash
# Status
sudo systemctl status polyclawd-mcp

# Restart
sudo systemctl restart polyclawd-mcp

# Logs
journalctl -u polyclawd-mcp -f
```

## Nginx Config

Added to `/etc/nginx/sites-enabled/virtuoso-website`:

```nginx
location /polyclawd/mcp {
    proxy_pass http://127.0.0.1:8421/mcp;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection 'upgrade';
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

## Connecting

### mcporter
```bash
mcporter config add polyclawd --url https://virtuosocrypto.com/polyclawd/mcp --transport streamable-http
mcporter list polyclawd --schema
mcporter call polyclawd.polyclawd_phase
```

### Claude Desktop / OpenClaw
```json
{
  "polyclawd": {
    "url": "https://virtuosocrypto.com/polyclawd/mcp",
    "transport": "streamable-http"
  }
}
```

## Tools (83+)

Key tools:
- `polyclawd_signals` — all aggregated signals
- `polyclawd_phase` — system status
- `polyclawd_arb_scan` — cross-platform arbitrage
- `polyclawd_portfolio_status` — paper portfolio
- `polyclawd_risk_guards` — Kelly + correlation status
- `polyclawd_strike_scanner` — Price-to-Strike signals
- `polyclawd_source_health` — API source health
- `polyclawd_espn_moneyline` — ESPN odds with devigged probs
- `polyclawd_kalshi_entertainment` — entertainment props
- `polyclawd_polymarket_orderbook` — orderbook depth

## Internal Design

- `BASE_URL = "http://localhost:8420"` — calls API internally, not through nginx
- Reuses `handle_tool_call()` dispatcher from `server.py`
- No-param tools auto-generated, parameterized tools have typed signatures
- Original stdio server (`mcp/server.py`) untouched for backward compatibility
