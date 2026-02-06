# Polyclawd

Polymarket prediction market trading bot combining intelligent scanning with safe execution via Simmer SDK.

## Quick Reference

| Item | Value |
|------|-------|
| Dashboard | `https://virtuosocrypto.com/polyclawd` |
| Simmer Helper | `~/clawd/scripts/simmer.sh` |
| Env Config | `config/polymarket.env` |
| Credentials | `~/.config/simmer/credentials.json` |

## APIs Used

| API | Purpose |
|-----|---------|
| Simmer SDK | Trade execution, portfolio management |
| Polymarket | Market data, positions |
| Kalshi | Cross-platform arbitrage comparison |
| Chainstack | Polygon RPC endpoint |
| OpenRouter | AI/LLM inference |

**Simmer is the primary execution layer - all trades go through Simmer SDK.**

## Architecture

```
Scanners (Intel)  →  Executor (Filter)  →  Simmer (Trade)
├── Whale Tracker       Score >= 10         $100/trade max
├── Arb Scanner         Top 5 signals       $500/day limit
├── Weather Markets     Confidence %        Managed custody
└── Liquidity Rewards   Reasoning           Safety rails
```

## Development Workflow

| Directory | Purpose |
|-----------|---------|
| `whale-tracker/` | Whale wallet monitoring |
| `cross-platform-arb/` | Polymarket vs Kalshi spreads |
| `liquidity-rewards/` | LP incentive scanner |
| `integrations/` | Simmer executor bridge |
| `frontend/` | Web dashboard (FastAPI + HTML) |
| `skills/` | Claude Code skills (polyclaw, quantish) |

## Project Notes

- Default mode is **dry run** - use `--execute` flag for live trades
- Cron job `polyclawd-monitor` runs every 2 hours
- All trades include reasoning for transparency
- Rate limit: 1 trade/2min/market in sandbox mode

## Simmer Commands

```bash
simmer.sh status       # Agent status + balance
simmer.sh markets 20   # Active markets
simmer.sh weather 20   # Weather markets
simmer.sh portfolio    # Portfolio summary
simmer.sh positions    # Current positions
simmer.sh trades 20    # Trade history
simmer.sh context <id> # Pre-trade context
simmer.sh trade <id> <yes|no> <amount> "reason"
```

## Skills (Invoke Proactively)

| Skill | Location | When to Use |
|-------|----------|-------------|
| `polyclaw` | `skills/polyclaw/` | Polymarket trading, market analysis |
| `quantish` | `skills/quantish/` | Cross-platform MCP operations |
| `/trading-analysis` | Global | Signal evaluation, position sizing |

### Proactive Skill Usage Rules

1. **Market analysis** → Use `polyclaw` skill for Polymarket-specific logic
2. **Cross-platform arb** → Use `quantish` skill for Kalshi comparison
3. **Signal evaluation** → Use `/trading-analysis` for sentiment scoring
4. **Trade execution** → Always verify with `simmer.sh context <id>` first

## Safety Rails

| Protection | Value |
|------------|-------|
| Per-trade limit | $100 |
| Daily limit | $500 |
| Dry run default | Yes |
| Managed custody | Simmer holds keys |

## Links

- **Claim Agent:** https://simmer.markets/claim/deep-7SNQ
- **Simmer:** https://simmer.markets
- **Polymarket:** https://polymarket.com
