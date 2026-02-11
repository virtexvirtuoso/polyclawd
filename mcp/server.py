#!/usr/bin/env python3
"""
Polyclawd MCP Server

Exposes Polyclawd API as MCP tools for Claude integration.
Run: python server.py (stdio mode)
"""

import json
import sys
import urllib.request
from typing import Any

# Base URL for Polyclawd API
BASE_URL = "https://virtuosocrypto.com/polyclawd"

# MCP Protocol version
PROTOCOL_VERSION = "2024-11-05"

def api_get(path: str) -> dict:
    """Make GET request to Polyclawd API."""
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd-MCP/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def api_post(path: str, params: dict = None) -> dict:
    """Make POST request to Polyclawd API."""
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, method="POST", headers={"User-Agent": "Polyclawd-MCP/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

# Tool definitions organized by category
TOOLS = [
    # === CORE SIGNALS ===
    {
        "name": "polyclawd_signals",
        "description": "Get all aggregated trading signals from all sources",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_news",
        "description": "Get news-based signals from Google News and Reddit",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_volume_spikes",
        "description": "Get volume spike signals (unusual trading activity)",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_smart_money",
        "description": "Get smart money whale signals",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_inverse_whale",
        "description": "Get inverse whale signals (fade the whales)",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    
    # === ARBITRAGE & EDGE ===
    {
        "name": "polyclawd_arb_scan",
        "description": "Scan for cross-platform arbitrage opportunities",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_kalshi_edge",
        "description": "Get Kalshi vs Polymarket edge opportunities",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_kalshi_entertainment",
        "description": "Get Kalshi entertainment/sports prop markets (Super Bowl halftime, Grammy, Oscar, celebrity props)",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_kalshi_all",
        "description": "Get ALL Kalshi markets with comprehensive pagination",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_manifold_edge",
        "description": "Get Manifold vs Polymarket edge opportunities",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_metaculus_edge",
        "description": "Get Metaculus vs Polymarket edge opportunities",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_predictit_edge",
        "description": "Get PredictIt vs Polymarket edge opportunities",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_betfair_edge",
        "description": "Get Betfair exchange edge opportunities",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_polyrouter_edge",
        "description": "Get PolyRouter cross-platform edge (7 platforms)",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    
    # === VEGAS ODDS ===
    {
        "name": "polyclawd_vegas_nfl",
        "description": "Get NFL Vegas odds (Super Bowl, AFC, NFC futures)",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_vegas_superbowl",
        "description": "Get Super Bowl winner odds only",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_vegas_soccer",
        "description": "Get all soccer futures odds",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_vegas_epl",
        "description": "Get English Premier League futures odds",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_vegas_ucl",
        "description": "Get UEFA Champions League futures odds",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_vegas_edge",
        "description": "Get Vegas vs Polymarket edge for sports",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    
    # === ESPN ODDS ===
    {
        "name": "polyclawd_espn_moneyline",
        "description": "Get ESPN moneyline odds with true probabilities",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "Sport: nfl, nba, nhl, mlb", "default": "nfl"}
            },
            "required": []
        }
    },
    {
        "name": "polyclawd_espn_moneylines",
        "description": "Get all ESPN moneylines across all sports",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_espn_edge",
        "description": "Get ESPN vs Polymarket edge opportunities",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    
    # === MARKETS ===
    {
        "name": "polyclawd_markets_trending",
        "description": "Get trending Polymarket markets",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_markets_opportunities",
        "description": "Get market opportunities (mispriced, high volume)",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_markets_search",
        "description": "Search markets by query",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "polyclawd_markets_new",
        "description": "Get newly created markets",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_kalshi_markets",
        "description": "Get Kalshi markets",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_manifold_markets",
        "description": "Get Manifold markets",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_predictit_markets",
        "description": "Get PredictIt markets",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_metaculus_questions",
        "description": "Get Metaculus questions",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    
    # === POLYROUTER ===
    {
        "name": "polyclawd_polyrouter_markets",
        "description": "Get markets from PolyRouter (7 platforms unified)",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_polyrouter_search",
        "description": "Search PolyRouter markets",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "polyclawd_polyrouter_sports",
        "description": "Get sports markets from PolyRouter",
        "inputSchema": {
            "type": "object",
            "properties": {
                "league": {"type": "string", "description": "League: nfl, nba, mlb, nhl, soccer"}
            },
            "required": ["league"]
        }
    },
    
    # === ENGINE & TRADING ===
    {
        "name": "polyclawd_engine",
        "description": "Get trading engine status",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_engine_start",
        "description": "Start the automated trading engine",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_engine_stop",
        "description": "Stop the automated trading engine",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_engine_trigger",
        "description": "Manually trigger a trading scan",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_trades",
        "description": "Get recent trades",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_positions",
        "description": "Get current positions",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    
    # === PAPER TRADING ===
    {
        "name": "polyclawd_phase",
        "description": "Get current scaling phase, balance, and position limits",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_balance",
        "description": "Get paper trading balance",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_simulate",
        "description": "Simulate position sizing for given parameters",
        "inputSchema": {
            "type": "object",
            "properties": {
                "balance": {"type": "number", "description": "Account balance in USD"},
                "confidence": {"type": "number", "description": "Signal confidence 0-100"}
            },
            "required": ["balance", "confidence"]
        }
    },
    {
        "name": "polyclawd_simmer_portfolio",
        "description": "Get Simmer paper trading portfolio",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_simmer_status",
        "description": "Get Simmer account status",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    
    # === CONFIDENCE & LEARNING ===
    {
        "name": "polyclawd_keywords",
        "description": "Get learned keyword performance statistics",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_learn",
        "description": "Teach keyword learner from a market title",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Market title to learn from"},
                "outcome": {"type": "string", "enum": ["win", "loss"], "description": "Trade outcome"}
            },
            "required": ["title"]
        }
    },
    {
        "name": "polyclawd_confidence_sources",
        "description": "Get confidence scores by signal source",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_confidence_calibration",
        "description": "Get confidence calibration stats",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    
    # === RESOLUTION & ROTATION ===
    {
        "name": "polyclawd_resolution_approaching",
        "description": "Get markets approaching resolution",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_resolution_imminent",
        "description": "Get markets with imminent resolution (<24h)",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_rotation_candidates",
        "description": "Get position rotation candidates (weak positions to exit)",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    
    # === NEW: POLYMARKET CLOB ===
    {
        "name": "polyclawd_polymarket_orderbook",
        "description": "Get Polymarket orderbook depth for a market",
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Market slug (e.g. 'will-trump-win-2024')"},
                "outcome": {"type": "string", "description": "Outcome: Yes or No", "default": "Yes"}
            },
            "required": ["slug"]
        }
    },
    {
        "name": "polyclawd_polymarket_microstructure",
        "description": "Get market microstructure analysis (spread, depth, liquidity)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Market slug"}
            },
            "required": ["slug"]
        }
    },
    
    # === NEW: MANIFOLD ===
    {
        "name": "polyclawd_manifold_bets",
        "description": "Get recent bets on Manifold (track betting flow)",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_manifold_top_traders",
        "description": "Get top Manifold traders (smart money)",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    
    # === NEW: METACULUS ===
    {
        "name": "polyclawd_metaculus_divergence",
        "description": "Get Metaculus vs community prediction divergence (expert signal)",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    
    # === NEW: Cross-Market Correlation ===
    {
        "name": "polyclawd_correlation_violations",
        "description": "Find probability constraint violations between related markets (arb opportunities). E.g., P(Chiefs win SB) should be <= P(Chiefs win AFC)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_violation": {"type": "number", "description": "Minimum violation % to report (default 3)", "default": 3}
            },
            "required": []
        }
    },
    {
        "name": "polyclawd_correlation_entities",
        "description": "Get all entities (teams, people) with multiple related markets for manual correlation analysis",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    
    # === NEW: ESPN ===
    {
        "name": "polyclawd_espn_injuries",
        "description": "Get injury report for a sport (predict line movements)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "Sport: nfl, nba, mlb, nhl", "default": "nfl"}
            },
            "required": []
        }
    },
    {
        "name": "polyclawd_espn_standings",
        "description": "Get team standings for a sport",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "Sport: nfl, nba, mlb, nhl", "default": "nfl"}
            },
            "required": []
        }
    },
    
    # === NEW: VEGAS (more sports) ===
    {
        "name": "polyclawd_vegas_nba",
        "description": "Get NBA championship futures odds",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_vegas_mlb",
        "description": "Get MLB World Series futures odds",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_vegas_nhl",
        "description": "Get NHL Stanley Cup futures odds",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    
    # === NEW: POLYROUTER ===
    {
        "name": "polyclawd_polyrouter_arbitrage",
        "description": "Find cross-platform arbitrage opportunities",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_polyrouter_props",
        "description": "Get player props from multiple platforms",
        "inputSchema": {
            "type": "object",
            "properties": {
                "league": {"type": "string", "description": "League: nfl, nba, mlb, nhl", "default": "nfl"}
            },
            "required": []
        }
    },
    
    # === NEW: KALSHI ENTERTAINMENT ===
    {
        "name": "polyclawd_kalshi_entertainment",
        "description": "Get Kalshi entertainment/sports props (Super Bowl, Grammys, Oscars)",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    
    # === SYSTEM ===
    {
        "name": "polyclawd_health",
        "description": "Get API health status",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_metrics",
        "description": "Get system metrics",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    }
]

def handle_tool_call(name: str, arguments: dict) -> Any:
    """Execute a tool and return the result."""
    
    # Core signals
    if name == "polyclawd_signals":
        return api_get("/api/signals")
    elif name == "polyclawd_news":
        return api_get("/api/signals/news")
    elif name == "polyclawd_volume_spikes":
        return api_get("/api/volume/spikes")
    elif name == "polyclawd_smart_money":
        return api_get("/api/smart-money")
    elif name == "polyclawd_inverse_whale":
        return api_get("/api/inverse-whale")
    
    # Arbitrage & edge
    elif name == "polyclawd_arb_scan":
        return api_get("/api/arb-scan")
    elif name == "polyclawd_kalshi_edge":
        return api_get("/api/kalshi/markets")
    elif name == "polyclawd_kalshi_entertainment":
        return api_get("/api/kalshi/entertainment")
    elif name == "polyclawd_kalshi_all":
        return api_get("/api/kalshi/all")
    elif name == "polyclawd_manifold_edge":
        return api_get("/api/manifold/edge")
    elif name == "polyclawd_metaculus_edge":
        return api_get("/api/metaculus/edge")
    elif name == "polyclawd_predictit_edge":
        return api_get("/api/predictit/edge")
    elif name == "polyclawd_betfair_edge":
        return api_get("/api/betfair/edge")
    elif name == "polyclawd_polyrouter_edge":
        return api_get("/api/polyrouter/edge")
    
    # Vegas odds
    elif name == "polyclawd_vegas_nfl":
        return api_get("/api/vegas/nfl")
    elif name == "polyclawd_vegas_superbowl":
        return api_get("/api/vegas/nfl/superbowl")
    elif name == "polyclawd_vegas_soccer":
        return api_get("/api/vegas/soccer")
    elif name == "polyclawd_vegas_epl":
        return api_get("/api/vegas/epl")
    elif name == "polyclawd_vegas_ucl":
        return api_get("/api/vegas/ucl")
    elif name == "polyclawd_vegas_edge":
        return api_get("/api/vegas/edge")
    
    # ESPN odds
    elif name == "polyclawd_espn_moneyline":
        sport = arguments.get("sport", "nfl")
        return api_get(f"/api/espn/moneyline/{sport}")
    elif name == "polyclawd_espn_moneylines":
        return api_get("/api/espn/moneylines")
    elif name == "polyclawd_espn_edge":
        return api_get("/api/espn/edge")
    
    # Markets
    elif name == "polyclawd_markets_trending":
        return api_get("/api/markets/trending")
    elif name == "polyclawd_markets_opportunities":
        return api_get("/api/markets/opportunities")
    elif name == "polyclawd_markets_search":
        query = arguments.get("query", "")
        return api_get(f"/api/markets/search?q={query}")
    elif name == "polyclawd_markets_new":
        return api_get("/api/markets/new")
    elif name == "polyclawd_kalshi_markets":
        return api_get("/api/kalshi/markets")
    elif name == "polyclawd_manifold_markets":
        return api_get("/api/manifold/markets")
    elif name == "polyclawd_predictit_markets":
        return api_get("/api/predictit/markets")
    elif name == "polyclawd_metaculus_questions":
        return api_get("/api/metaculus/questions")
    
    # PolyRouter
    elif name == "polyclawd_polyrouter_markets":
        return api_get("/api/polyrouter/markets")
    elif name == "polyclawd_polyrouter_search":
        query = arguments.get("query", "")
        return api_get(f"/api/polyrouter/search?q={query}")
    elif name == "polyclawd_polyrouter_sports":
        league = arguments.get("league", "nfl")
        return api_get(f"/api/polyrouter/sports/{league}")
    
    # Engine & trading
    elif name == "polyclawd_engine":
        return api_get("/api/engine/status")
    elif name == "polyclawd_engine_start":
        return api_post("/api/engine/start")
    elif name == "polyclawd_engine_stop":
        return api_post("/api/engine/stop")
    elif name == "polyclawd_engine_trigger":
        return api_post("/api/engine/trigger")
    elif name == "polyclawd_trades":
        return api_get("/api/trades")
    elif name == "polyclawd_positions":
        return api_get("/api/positions")
    
    # Paper trading
    elif name == "polyclawd_phase":
        return api_get("/api/phase/current")
    elif name == "polyclawd_balance":
        return api_get("/api/paper/balance")
    elif name == "polyclawd_simulate":
        params = {
            "balance": arguments.get("balance", 1000),
            "confidence": arguments.get("confidence", 50),
            "win_rate": arguments.get("win_rate", 0.55),
            "win_streak": arguments.get("win_streak", 0),
            "source_agreement": arguments.get("source_agreement", 1)
        }
        return api_post("/api/phase/simulate", params)
    elif name == "polyclawd_simmer_portfolio":
        return api_get("/api/simmer/portfolio")
    elif name == "polyclawd_simmer_status":
        return api_get("/api/simmer/status")
    
    # Confidence & learning
    elif name == "polyclawd_keywords":
        return api_get("/api/keywords/stats")
    elif name == "polyclawd_learn":
        params = {"title": arguments.get("title", "")}
        if arguments.get("outcome"):
            params["outcome"] = arguments["outcome"]
        params["market_id"] = f"mcp-{hash(params['title']) % 10000}"
        return api_post("/api/keywords/learn", params)
    elif name == "polyclawd_confidence_sources":
        return api_get("/api/confidence/sources")
    elif name == "polyclawd_confidence_calibration":
        return api_get("/api/confidence/calibration")
    
    # Resolution & rotation
    elif name == "polyclawd_resolution_approaching":
        return api_get("/api/resolution/approaching")
    elif name == "polyclawd_resolution_imminent":
        return api_get("/api/resolution/imminent")
    elif name == "polyclawd_rotation_candidates":
        return api_get("/api/rotation/candidates")
    
    # New: Polymarket CLOB
    elif name == "polyclawd_polymarket_orderbook":
        slug = arguments.get("slug", "")
        outcome = arguments.get("outcome", "Yes")
        return api_get(f"/api/polymarket/orderbook/{slug}?outcome={outcome}")
    elif name == "polyclawd_polymarket_microstructure":
        slug = arguments.get("slug", "")
        return api_get(f"/api/polymarket/microstructure/{slug}")
    
    # New: Manifold
    elif name == "polyclawd_manifold_bets":
        return api_get("/api/manifold/bets")
    elif name == "polyclawd_manifold_top_traders":
        return api_get("/api/manifold/top-traders")
    
    # New: Metaculus
    elif name == "polyclawd_metaculus_divergence":
        return api_get("/api/metaculus/divergence")
    
    # New: Cross-Market Correlation
    elif name == "polyclawd_correlation_violations":
        min_violation = arguments.get("min_violation", 3)
        return api_get(f"/api/correlation/violations?min_violation={min_violation}")
    elif name == "polyclawd_correlation_entities":
        return api_get("/api/correlation/entities")
    
    # New: ESPN
    elif name == "polyclawd_espn_injuries":
        sport = arguments.get("sport", "nfl")
        return api_get(f"/api/espn/injuries/{sport}")
    elif name == "polyclawd_espn_standings":
        sport = arguments.get("sport", "nfl")
        return api_get(f"/api/espn/standings/{sport}")
    
    # New: Vegas (more sports)
    elif name == "polyclawd_vegas_nba":
        return api_get("/api/vegas/nba")
    elif name == "polyclawd_vegas_mlb":
        return api_get("/api/vegas/mlb")
    elif name == "polyclawd_vegas_nhl":
        return api_get("/api/vegas/nhl")
    
    # New: PolyRouter
    elif name == "polyclawd_polyrouter_arbitrage":
        return api_get("/api/polyrouter/arbitrage")
    elif name == "polyclawd_polyrouter_props":
        league = arguments.get("league", "nfl")
        return api_get(f"/api/polyrouter/props/{league}")
    
    # New: Kalshi entertainment
    elif name == "polyclawd_kalshi_entertainment":
        return api_get("/api/kalshi/entertainment")
    
    # System
    elif name == "polyclawd_health":
        return api_get("/api/health")
    elif name == "polyclawd_metrics":
        return api_get("/api/metrics")
    
    else:
        return {"error": f"Unknown tool: {name}"}

def send_response(id: Any, result: Any):
    """Send JSON-RPC response."""
    response = {
        "jsonrpc": "2.0",
        "id": id,
        "result": result
    }
    print(json.dumps(response), flush=True)

def send_error(id: Any, code: int, message: str):
    """Send JSON-RPC error."""
    response = {
        "jsonrpc": "2.0",
        "id": id,
        "error": {"code": code, "message": message}
    }
    print(json.dumps(response), flush=True)

def main():
    """Main stdio loop."""
    for line in sys.stdin:
        try:
            request = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        
        method = request.get("method")
        id = request.get("id")
        params = request.get("params", {})
        
        if method == "initialize":
            send_response(id, {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {
                    "name": "polyclawd",
                    "version": "2.0.0"
                },
                "capabilities": {
                    "tools": {}
                }
            })
        
        elif method == "notifications/initialized":
            pass  # No response needed
        
        elif method == "tools/list":
            send_response(id, {"tools": TOOLS})
        
        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            result = handle_tool_call(tool_name, arguments)
            send_response(id, {
                "content": [
                    {"type": "text", "text": json.dumps(result, indent=2)}
                ]
            })
        
        else:
            send_error(id, -32601, f"Method not found: {method}")

if __name__ == "__main__":
    main()
