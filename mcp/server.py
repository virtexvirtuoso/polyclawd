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

# Tool definitions
TOOLS = [
    {
        "name": "polyclawd_phase",
        "description": "Get current scaling phase, balance, and position limits",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_signals",
        "description": "Get all aggregated trading signals from 13 sources",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_news",
        "description": "Get news-based signals from Google News and Reddit",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_keywords",
        "description": "Get learned keyword performance statistics",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_positions",
        "description": "Get current paper trading positions",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_balance",
        "description": "Get paper trading balance for Simmer and Polymarket",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_engine",
        "description": "Get trading engine status (running, trades today, etc)",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_simulate",
        "description": "Simulate position sizing for given parameters",
        "inputSchema": {
            "type": "object",
            "properties": {
                "balance": {"type": "number", "description": "Account balance in USD"},
                "confidence": {"type": "number", "description": "Signal confidence 0-100"},
                "win_rate": {"type": "number", "description": "Recent win rate 0-1", "default": 0.55},
                "win_streak": {"type": "integer", "description": "Current win streak", "default": 0}
            },
            "required": ["balance", "confidence"]
        }
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
        "name": "polyclawd_engine_start",
        "description": "Start the automated trading engine",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "polyclawd_engine_stop",
        "description": "Stop the automated trading engine",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    }
]

def handle_tool_call(name: str, arguments: dict) -> Any:
    """Execute a tool and return the result."""
    
    if name == "polyclawd_phase":
        return api_get("/api/phase/current")
    
    elif name == "polyclawd_signals":
        return api_get("/api/signals")
    
    elif name == "polyclawd_news":
        return api_get("/api/signals/news")
    
    elif name == "polyclawd_keywords":
        return api_get("/api/keywords/stats")
    
    elif name == "polyclawd_positions":
        return api_get("/api/paper/positions")
    
    elif name == "polyclawd_balance":
        return api_get("/api/paper/balance")
    
    elif name == "polyclawd_engine":
        return api_get("/api/engine/status")
    
    elif name == "polyclawd_simulate":
        params = {
            "balance": arguments.get("balance", 1000),
            "confidence": arguments.get("confidence", 50),
            "win_rate": arguments.get("win_rate", 0.55),
            "win_streak": arguments.get("win_streak", 0),
            "source_agreement": arguments.get("source_agreement", 1)
        }
        return api_post("/api/phase/simulate", params)
    
    elif name == "polyclawd_learn":
        params = {"title": arguments.get("title", "")}
        if arguments.get("outcome"):
            params["outcome"] = arguments["outcome"]
        params["market_id"] = f"mcp-{hash(params['title']) % 10000}"
        return api_post("/api/keywords/learn", params)
    
    elif name == "polyclawd_engine_start":
        return api_post("/api/engine/start")
    
    elif name == "polyclawd_engine_stop":
        return api_post("/api/engine/stop")
    
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
                    "version": "1.0.0"
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
