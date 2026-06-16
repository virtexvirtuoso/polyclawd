#!/usr/bin/env python3
"""
Virtuoso MCP Bridge — call Virtuoso MCP tools from Polyclawd.

Handles MCP streamable-http with session management.
Caches results for 2 minutes to avoid hammering.

Usage:
    python3 virtuoso_bridge.py ETH       # symbol analysis
    python3 virtuoso_bridge.py BTC
    python3 virtuoso_bridge.py fear      # fear & greed
    python3 virtuoso_bridge.py signals   # top signals

As module:
    from virtuoso_bridge import get_symbol_analysis, get_fear_greed
from loguru import logger
"""

import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

MCP_URL = "http://localhost:8091/mcp"
CACHE_DIR = Path("/tmp/virtuoso_cache")
CACHE_TTL = 120


def _cache_get(key: str) -> Optional[dict]:
    CACHE_DIR.mkdir(exist_ok=True)
    p = CACHE_DIR / f"{key}.json"
    if p.exists() and (time.time() - p.stat().st_mtime) < CACHE_TTL:
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return None


def _cache_set(key: str, data: dict):
    CACHE_DIR.mkdir(exist_ok=True)
    (CACHE_DIR / f"{key}.json").write_text(json.dumps(data))


def _mcp_request(payload: dict, session_id: str = None) -> tuple:
    """Send MCP request, return (parsed_response, session_id)."""
    data = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    req = urllib.request.Request(MCP_URL, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        sid = resp.headers.get("mcp-session-id") or session_id
        body = resp.read().decode()
        for line in body.split("\n"):
            if line.startswith("data: "):
                return json.loads(line[6:]), sid
        return json.loads(body), sid


def call_tool(tool_name: str, arguments: dict = None) -> dict:
    """Initialize session + call tool in one shot. Returns parsed result."""
    # Cache check
    ck = re.sub(r'[^a-zA-Z0-9]', '_', f"{tool_name}_{json.dumps(arguments or {})}")[:100]
    cached = _cache_get(ck)
    if cached:
        return cached

    try:
        # Step 1: Initialize
        init_resp, sid = _mcp_request({
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "polyclawd-bridge", "version": "1.0"},
            },
            "id": 1,
        })

        # Step 2: Call tool with session
        tool_resp, _ = _mcp_request({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
            "id": 2,
        }, session_id=sid)

        # Extract text content
        content = tool_resp.get("result", {}).get("content", [])
        if content and isinstance(content, list):
            text = content[0].get("text", "")
            result = {"raw_text": text}
            _cache_set(ck, result)
            return result

        _cache_set(ck, tool_resp)
        return tool_resp

    except Exception as e:
        return {"error": str(e)}


def get_symbol_analysis(symbol: str) -> dict:
    """Get Virtuoso 6D confluence analysis for a crypto symbol.
    
    Returns:
        {
            "symbol": str,
            "overall_score": float (0-100),
            "direction": str,
            "components": {technical, volume, orderflow, sentiment, orderbook, price_structure},
        }
    """
    result = call_tool("get_symbol_analysis", {"symbol": symbol})

    if "error" in result:
        return result

    text = result.get("raw_text", "")
    if not text:
        return {"error": "empty_response", "symbol": symbol}

    # Parse overall score
    m = re.search(r'Score:\*?\*?\s*(\d+\.?\d*)/100', text)
    score = float(m.group(1)) if m else None

    # Parse components
    components = {}
    for comp in ("technical", "volume", "orderflow", "sentiment", "orderbook", "price_structure"):
        m = re.search(rf'{comp}:\s*(\d+\.?\d*)', text)
        if m:
            components[comp] = float(m.group(1))

    # Parse direction from emoji/text
    if "Strong Bullish" in text or "🟢🟢" in text:
        direction = "strong_bullish"
    elif "Bullish" in text or ("🟢" in text and "🟢🟢" not in text):
        direction = "bullish"
    elif "Strong Bearish" in text or "🔴🔴" in text:
        direction = "strong_bearish"
    elif "Bearish" in text or ("🔴" in text and "🔴🔴" not in text):
        direction = "bearish"
    else:
        direction = "neutral"

    return {
        "symbol": symbol,
        "overall_score": score,
        "direction": direction,
        "components": components,
    }


def get_fear_greed() -> dict:
    """Get Fear & Greed index."""
    result = call_tool("get_fear_greed_index")
    text = result.get("raw_text", "")

    m_val = re.search(r'Value:\*?\*?\s*(\d+)', text)
    m_label = re.search(r'Label:\*?\*?\s*(.+)', text)
    m_regime = re.search(r'Market Regime:\*?\*?\s*(.+)', text)

    return {
        "value": int(m_val.group(1)) if m_val else None,
        "label": m_label.group(1).strip() if m_label else None,
        "regime": m_regime.group(1).strip() if m_regime else None,
    }


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "ETH"

    if arg == "fear":
        print(json.dumps(get_fear_greed(), indent=2))
    elif arg == "signals":
        print(json.dumps(call_tool("get_top_signals", {"limit": 10}), indent=2))
    else:
        print(json.dumps(get_symbol_analysis(arg.upper()), indent=2))
