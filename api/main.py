#!/usr/bin/env python3
"""
Polymarket Trading Bot - FastAPI Backend
Paper trading + Simmer SDK live trading integration.
"""

import json
import os
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ============================================================================
# Configuration
# ============================================================================

app = FastAPI(title="Polyclawd Trading API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Paper trading storage
STORAGE_DIR = Path.home() / ".openclaw" / "paper-trading"
BALANCE_FILE = STORAGE_DIR / "balance.json"
POSITIONS_FILE = STORAGE_DIR / "positions.json"
TRADES_FILE = STORAGE_DIR / "trades.json"
DEFAULT_BALANCE = 10000.0

# APIs
GAMMA_API = "https://gamma-api.polymarket.com"
SIMMER_API = "https://api.simmer.markets/api/sdk"

# Simmer config
SIMMER_API_KEY = None
SIMMER_MAX_TRADE = 100.0  # $100 per trade limit
SIMMER_DAILY_LIMIT = 500.0  # $500 daily limit

def load_simmer_credentials():
    global SIMMER_API_KEY
    creds_path = Path.home() / ".config" / "simmer" / "credentials.json"
    if creds_path.exists():
        with open(creds_path) as f:
            SIMMER_API_KEY = json.load(f).get("api_key")
    else:
        SIMMER_API_KEY = os.environ.get("SIMMER_API_KEY")
    return SIMMER_API_KEY is not None

# ============================================================================
# Models
# ============================================================================

class TradeRequest(BaseModel):
    market_id: str
    side: str
    amount: float
    type: str = "BUY"

class SimmerTradeRequest(BaseModel):
    market_id: str
    side: str  # yes or no
    amount: float
    reasoning: str = "Polyclawd trade"

class TradeResponse(BaseModel):
    success: bool
    message: str
    shares: float = 0
    price: float = 0
    pnl: Optional[float] = None

# ============================================================================
# Storage Helpers
# ============================================================================

def ensure_storage():
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    if not BALANCE_FILE.exists():
        save_json(BALANCE_FILE, {"usdc": DEFAULT_BALANCE, "created_at": datetime.now().isoformat()})
    if not POSITIONS_FILE.exists():
        save_json(POSITIONS_FILE, [])
    if not TRADES_FILE.exists():
        save_json(TRADES_FILE, [])

def load_json(path: Path):
    with open(path, "r") as f:
        return json.load(f)

def save_json(path: Path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# ============================================================================
# Polymarket (Gamma) API Helpers
# ============================================================================

def api_get(endpoint: str, params: dict = None) -> Any:
    url = f"{GAMMA_API}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Polymarket API error: {str(e)}")

def get_market(market_id: str) -> Optional[dict]:
    for param in ["id", "slug", "conditionId"]:
        try:
            markets = api_get("/markets", {param: market_id})
            if markets:
                return markets[0]
        except:
            pass
    return None

def get_market_prices(market: dict) -> tuple:
    try:
        prices = json.loads(market.get("outcomePrices", "[0, 0]"))
        return float(prices[0]) if prices[0] else 0.0, float(prices[1]) if prices[1] else 0.0
    except:
        return 0.0, 0.0

# ============================================================================
# Simmer API Helpers
# ============================================================================

def simmer_request(endpoint: str, method: str = "GET", data: dict = None) -> Optional[dict]:
    if not SIMMER_API_KEY:
        if not load_simmer_credentials():
            return None
    
    url = f"{SIMMER_API}{endpoint}"
    headers = {
        "Authorization": f"Bearer {SIMMER_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "Polyclawd/2.0"
    }
    
    req = urllib.request.Request(url, headers=headers, method=method)
    if data:
        req.data = json.dumps(data).encode()
    
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        return {"error": f"API Error {e.code}: {error_body}", "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}

# ============================================================================
# Paper Trading Endpoints (unchanged)
# ============================================================================

@app.get("/api/balance")
async def get_balance():
    ensure_storage()
    balance = load_json(BALANCE_FILE)
    positions = load_json(POSITIONS_FILE)
    usdc = balance.get("usdc", DEFAULT_BALANCE)
    
    position_value = 0.0
    for pos in positions:
        market = get_market(pos["market_id"])
        if market:
            yes_price, no_price = get_market_prices(market)
            current_price = yes_price if pos["side"] == "YES" else no_price
            position_value += pos["shares"] * current_price
    
    total = usdc + position_value
    return {
        "cash": usdc,
        "positions_value": position_value,
        "total": total,
        "pnl": total - DEFAULT_BALANCE,
        "pnl_percent": ((total - DEFAULT_BALANCE) / DEFAULT_BALANCE) * 100
    }

@app.get("/api/positions")
async def get_positions():
    ensure_storage()
    positions = load_json(POSITIONS_FILE)
    result = []
    for pos in positions:
        market = get_market(pos["market_id"])
        if not market:
            continue
        yes_price, no_price = get_market_prices(market)
        current_price = yes_price if pos["side"] == "YES" else no_price
        current_value = pos["shares"] * current_price
        cost_basis = pos.get("cost_basis", pos["shares"] * pos["entry_price"])
        pnl = current_value - cost_basis
        result.append({
            "market_id": pos["market_id"],
            "market_question": pos.get("market_question", market.get("question", "Unknown")),
            "side": pos["side"],
            "shares": pos["shares"],
            "entry_price": pos["entry_price"],
            "current_price": current_price,
            "cost_basis": cost_basis,
            "current_value": current_value,
            "pnl": pnl,
            "pnl_percent": (pnl / cost_basis * 100) if cost_basis > 0 else 0,
            "opened_at": pos.get("opened_at")
        })
    return result

@app.get("/api/trades")
async def get_trades(limit: int = Query(default=20, le=100)):
    ensure_storage()
    trades = load_json(TRADES_FILE)
    return list(reversed(trades[-limit:]))

@app.post("/api/trade")
async def execute_paper_trade(request: TradeRequest):
    """Execute a PAPER trade (simulated)."""
    ensure_storage()
    side = request.side.upper()
    if side not in ("YES", "NO"):
        raise HTTPException(status_code=400, detail="Side must be YES or NO")
    if request.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    
    market = get_market(request.market_id)
    if not market:
        raise HTTPException(status_code=404, detail=f"Market not found: {request.market_id}")
    if market.get("closed"):
        raise HTTPException(status_code=400, detail="Market is closed")
    
    yes_price, no_price = get_market_prices(market)
    price = yes_price if side == "YES" else no_price
    if price <= 0:
        raise HTTPException(status_code=400, detail=f"Invalid price for {side}")
    
    if request.type.upper() == "BUY":
        return await _execute_buy(market, side, request.amount, price)
    else:
        return await _execute_sell(market, side, request.amount, price)

async def _execute_buy(market: dict, side: str, amount: float, price: float):
    balance = load_json(BALANCE_FILE)
    usdc = balance.get("usdc", DEFAULT_BALANCE)
    if amount > usdc:
        raise HTTPException(status_code=400, detail=f"Insufficient balance: ${usdc:.2f}")
    
    shares = amount / price
    balance["usdc"] = usdc - amount
    save_json(BALANCE_FILE, balance)
    
    positions = load_json(POSITIONS_FILE)
    existing = next((p for p in positions if p["market_id"] == market["id"] and p["side"] == side), None)
    
    if existing:
        old_cost = existing["shares"] * existing["entry_price"]
        total_shares = existing["shares"] + shares
        existing["shares"] = total_shares
        existing["entry_price"] = (old_cost + amount) / total_shares
        existing["cost_basis"] = old_cost + amount
    else:
        positions.append({
            "market_id": market["id"],
            "market_question": market.get("question", "Unknown")[:80],
            "side": side,
            "shares": shares,
            "entry_price": price,
            "cost_basis": amount,
            "opened_at": datetime.now().isoformat(),
        })
    save_json(POSITIONS_FILE, positions)
    
    trades = load_json(TRADES_FILE)
    trades.append({
        "type": "BUY", "mode": "paper",
        "market_id": market["id"],
        "market_question": market.get("question", "Unknown")[:80],
        "side": side, "amount": amount, "price": price, "shares": shares,
        "timestamp": datetime.now().isoformat(),
    })
    save_json(TRADES_FILE, trades)
    
    return TradeResponse(success=True, message=f"[PAPER] Bought {shares:.2f} {side} shares", shares=shares, price=price)

async def _execute_sell(market: dict, side: str, amount: float, price: float):
    positions = load_json(POSITIONS_FILE)
    pos_idx = next((i for i, p in enumerate(positions) if p["market_id"] == market["id"] and p["side"] == side), None)
    
    if pos_idx is None:
        raise HTTPException(status_code=400, detail=f"No {side} position")
    
    pos = positions[pos_idx]
    shares_to_sell = amount / price
    if shares_to_sell > pos["shares"]:
        raise HTTPException(status_code=400, detail=f"Not enough shares: {pos['shares']:.2f}")
    
    proceeds = shares_to_sell * price
    cost_per_share = pos["cost_basis"] / pos["shares"]
    pnl = proceeds - (shares_to_sell * cost_per_share)
    
    pos["shares"] -= shares_to_sell
    pos["cost_basis"] -= shares_to_sell * cost_per_share
    if pos["shares"] < 0.001:
        positions.pop(pos_idx)
    save_json(POSITIONS_FILE, positions)
    
    balance = load_json(BALANCE_FILE)
    balance["usdc"] = balance.get("usdc", 0) + proceeds
    save_json(BALANCE_FILE, balance)
    
    trades = load_json(TRADES_FILE)
    trades.append({
        "type": "SELL", "mode": "paper",
        "market_id": market["id"],
        "market_question": market.get("question", "Unknown")[:80],
        "side": side, "amount": proceeds, "price": price, "shares": shares_to_sell, "pnl": pnl,
        "timestamp": datetime.now().isoformat(),
    })
    save_json(TRADES_FILE, trades)
    
    return TradeResponse(success=True, message=f"[PAPER] Sold {shares_to_sell:.2f} {side} shares", shares=shares_to_sell, price=price, pnl=pnl)

@app.post("/api/reset")
async def reset_paper_trading():
    ensure_storage()
    save_json(BALANCE_FILE, {"usdc": DEFAULT_BALANCE, "created_at": datetime.now().isoformat()})
    save_json(POSITIONS_FILE, [])
    trades = load_json(TRADES_FILE)
    trades.append({"type": "RESET", "timestamp": datetime.now().isoformat()})
    save_json(TRADES_FILE, trades)
    return {"success": True, "message": f"Reset to ${DEFAULT_BALANCE:,.2f}"}

# ============================================================================
# Simmer SDK Live Trading Endpoints
# ============================================================================

@app.get("/api/simmer/status")
async def get_simmer_status():
    """Get Simmer agent status and balance."""
    result = simmer_request("/agents/me")
    if not result or result.get("error"):
        raise HTTPException(status_code=502, detail=result.get("error", "Simmer API unavailable"))
    return result

@app.get("/api/simmer/markets")
async def get_simmer_markets(
    q: Optional[str] = None,
    tags: Optional[str] = None,
    limit: int = Query(default=30, le=100)
):
    """Get active markets from Simmer."""
    params = {"status": "active", "limit": limit}
    if q:
        params["q"] = q
    if tags:
        params["tags"] = tags
    
    endpoint = "/markets?" + urllib.parse.urlencode(params)
    result = simmer_request(endpoint)
    if not result or result.get("error"):
        raise HTTPException(status_code=502, detail=result.get("error", "Simmer API unavailable"))
    return result

@app.get("/api/simmer/positions")
async def get_simmer_positions():
    """Get current positions from Simmer."""
    result = simmer_request("/positions")
    if not result or result.get("error"):
        raise HTTPException(status_code=502, detail=result.get("error", "Simmer API unavailable"))
    return result

@app.get("/api/simmer/trades")
async def get_simmer_trades(limit: int = Query(default=20, le=100)):
    """Get trade history from Simmer."""
    result = simmer_request(f"/trades?limit={limit}")
    if not result or result.get("error"):
        raise HTTPException(status_code=502, detail=result.get("error", "Simmer API unavailable"))
    return result

@app.post("/api/simmer/trade")
async def execute_simmer_trade(request: SimmerTradeRequest):
    """Execute a LIVE trade via Simmer SDK."""
    # Validate
    side = request.side.lower()
    if side not in ("yes", "no"):
        raise HTTPException(status_code=400, detail="Side must be yes or no")
    if request.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    if request.amount > SIMMER_MAX_TRADE:
        raise HTTPException(status_code=400, detail=f"Amount exceeds max trade limit: ${SIMMER_MAX_TRADE}")
    
    # Execute via Simmer
    data = {
        "market_id": request.market_id,
        "side": side,
        "amount": request.amount,
        "reasoning": request.reasoning,
        "source": "polyclawd:api"
    }
    
    result = simmer_request("/trade", method="POST", data=data)
    if not result:
        raise HTTPException(status_code=502, detail="Simmer API unavailable")
    
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    
    # Log to local trades file
    ensure_storage()
    trades = load_json(TRADES_FILE)
    trades.append({
        "type": "BUY", "mode": "LIVE",
        "market_id": request.market_id,
        "side": side.upper(),
        "amount": request.amount,
        "reasoning": request.reasoning,
        "simmer_result": result,
        "timestamp": datetime.now().isoformat(),
    })
    save_json(TRADES_FILE, trades)
    
    return {
        "success": True,
        "mode": "LIVE",
        "message": f"Live trade executed via Simmer",
        "result": result
    }

@app.get("/api/simmer/context/{market_id}")
async def get_simmer_context(market_id: str):
    """Get pre-trade context for a market from Simmer."""
    result = simmer_request(f"/context/{market_id}")
    if not result or result.get("error"):
        raise HTTPException(status_code=502, detail=result.get("error", "Simmer API unavailable"))
    return result

# ============================================================================
# Scanner Endpoints (unchanged)
# ============================================================================

@app.get("/api/arb-scan")
async def arb_scan(limit: int = Query(default=50, le=100)):
    markets = api_get("/markets", {"closed": "false", "active": "true", "limit": str(limit), "order": "volume24hr", "ascending": "false"})
    opportunities = []
    for market in markets:
        yes_price, no_price = get_market_prices(market)
        if yes_price <= 0 or no_price <= 0:
            continue
        total = yes_price + no_price
        if total < 0.99 or total > 1.01:
            opportunities.append({
                "market_id": market["id"],
                "question": market.get("question", "Unknown"),
                "yes_price": yes_price, "no_price": no_price,
                "total": total, "spread": abs(1.0 - total),
                "type": "underpriced" if total < 0.99 else "overpriced",
            })
    opportunities.sort(key=lambda x: x["spread"], reverse=True)
    return {"count": len(opportunities), "opportunities": opportunities[:20], "scanned_at": datetime.now().isoformat()}

@app.get("/api/rewards")
async def get_rewards():
    markets = api_get("/markets", {"closed": "false", "active": "true", "limit": "500"})
    opportunities = []
    for market in markets:
        min_size = market.get("rewardsMinSize", 0)
        max_spread = market.get("rewardsMaxSpread", 0)
        clob_rewards = market.get("clobRewards", [])
        
        if not (min_size > 0 and max_spread > 0):
            if not clob_rewards or not any(r.get("rewardsDailyRate", 0) > 0 for r in clob_rewards):
                continue
        
        daily_rate = sum(r.get("rewardsDailyRate", 0) for r in clob_rewards) if clob_rewards else (1.0 if max_spread > 0 else 0)
        yes_price, no_price = get_market_prices(market)
        midpoint = (yes_price + no_price) / 2 if yes_price > 0 and no_price > 0 else 0.5
        liquidity = float(market.get("liquidityNum", 0))
        competitive = float(market.get("competitive", 0.5))
        
        score = min(daily_rate / 5.0, 2.0) * 30 + (1 - competitive) * 25 + min(max_spread / 3.0, 2.0) * 15
        score += 15 if liquidity > 100000 else (10 if liquidity > 10000 else 5)
        score += 15 if 0.2 <= midpoint <= 0.8 else (10 if 0.1 <= midpoint <= 0.9 else 5)
        
        opportunities.append({
            "market_id": market["id"], "question": market.get("question", "Unknown"),
            "rewards_min_size": min_size, "rewards_max_spread": max_spread,
            "daily_reward_rate": daily_rate, "midpoint": midpoint,
            "liquidity": liquidity, "opportunity_score": round(score, 2)
        })
    opportunities.sort(key=lambda x: x["opportunity_score"], reverse=True)
    return {"count": len(opportunities), "opportunities": opportunities[:30], "scanned_at": datetime.now().isoformat()}

@app.get("/api/markets/trending")
async def get_trending_markets(limit: int = Query(default=20, le=50)):
    markets = api_get("/markets", {"closed": "false", "active": "true", "limit": str(limit), "order": "volume24hr", "ascending": "false"})
    result = []
    for market in markets:
        yes_price, no_price = get_market_prices(market)
        result.append({"id": market["id"], "question": market.get("question", "Unknown"), "slug": market.get("slug", ""), "yes_price": yes_price, "no_price": no_price, "volume_24h": market.get("volume24hr", 0), "liquidity": market.get("liquidityNum", 0)})
    return {"markets": result}

@app.get("/api/markets/search")
async def search_markets(q: str = Query(..., min_length=2), limit: int = Query(default=15, le=30)):
    markets = api_get("/markets", {"closed": "false", "active": "true", "_q": q, "limit": str(limit)})
    result = []
    for market in markets:
        yes_price, no_price = get_market_prices(market)
        result.append({"id": market["id"], "question": market.get("question", "Unknown"), "slug": market.get("slug", ""), "yes_price": yes_price, "no_price": no_price, "volume_24h": market.get("volume24hr", 0)})
    return {"markets": result, "query": q}

@app.get("/api/markets/{market_id}")
async def get_market_details(market_id: str):
    market = get_market(market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    yes_price, no_price = get_market_prices(market)
    return {"id": market["id"], "question": market.get("question"), "description": market.get("description"), "slug": market.get("slug"), "yes_price": yes_price, "no_price": no_price, "volume_24h": market.get("volume24hr", 0), "liquidity": market.get("liquidityNum", 0), "created_at": market.get("createdAt"), "end_date": market.get("endDate"), "closed": market.get("closed", False)}

# ============================================================================
# Health Check
# ============================================================================

@app.get("/api/health")
async def health_check():
    simmer_ok = load_simmer_credentials()
    return {
        "status": "healthy",
        "version": "2.0.0",
        "paper_trading": True,
        "simmer_sdk": simmer_ok,
        "timestamp": datetime.now().isoformat()
    }

# ============================================================================
# Static Files
# ============================================================================

frontend_dir = Path(__file__).parent.parent
app.mount("/css", StaticFiles(directory=str(frontend_dir / "css")), name="css")
app.mount("/js", StaticFiles(directory=str(frontend_dir / "js")), name="js")

@app.get("/")
async def serve_index():
    return FileResponse(str(frontend_dir / "index.html"))

@app.get("/{page}.html")
async def serve_page(page: str):
    file_path = frontend_dir / f"{page}.html"
    if file_path.exists():
        return FileResponse(str(file_path))
    raise HTTPException(status_code=404, detail="Page not found")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

# ============================================================================
# Whale Tracker Endpoints
# ============================================================================

WHALE_CONFIG_PATH = Path(__file__).parent.parent / "config" / "whale_config.json"
POLYGONSCAN_API = "https://api.polygonscan.com/api"

def load_whale_config():
    """Load whale configuration"""
    if WHALE_CONFIG_PATH.exists():
        with open(WHALE_CONFIG_PATH) as f:
            return json.load(f)
    return {"whales": [], "settings": {}}

def fetch_wallet_balance(address: str) -> dict:
    """Fetch wallet balance via public Polygon RPC"""
    RPC_URL = "https://polygon-rpc.com"
    try:
        # POL balance via eth_getBalance
        payload = {"jsonrpc": "2.0", "method": "eth_getBalance", "params": [address, "latest"], "id": 1}
        req = urllib.request.Request(RPC_URL, data=json.dumps(payload).encode(), 
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            pol_balance = int(data.get("result", "0x0"), 16) / 1e18
        
        # USDC balance via eth_call (balanceOf)
        usdc_addr = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        # balanceOf(address) selector = 0x70a08231 + padded address
        data_hex = "0x70a08231" + address[2:].lower().zfill(64)
        payload = {"jsonrpc": "2.0", "method": "eth_call", 
                  "params": [{"to": usdc_addr, "data": data_hex}, "latest"], "id": 2}
        req = urllib.request.Request(RPC_URL, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            usdc_balance = int(data.get("result", "0x0"), 16) / 1e6
        
        return {"pol": round(pol_balance, 4), "usdc": round(usdc_balance, 2)}
    except Exception as e:
        return {"pol": 0, "usdc": 0, "error": str(e)}

@app.get("/api/whales")
async def list_whales(include_balances: bool = Query(False, description="Fetch live balances (slower)")):
    """List all tracked whale addresses with profiles"""
    config = load_whale_config()
    whales = []
    
    for whale in config.get("whales", []):
        whale_data = {
            "address": whale["address"],
            "name": whale.get("name", "Unknown"),
            "notes": whale.get("notes"),
            "profit_estimate": whale.get("profit_estimate"),
            "win_rate": whale.get("win_rate"),
            "polygonscan_url": f"https://polygonscan.com/address/{whale['address']}"
        }
        
        if include_balances:
            balances = fetch_wallet_balance(whale["address"])
            whale_data["balances"] = balances
        
        whales.append(whale_data)
    
    return {
        "count": len(whales),
        "whales": whales,
        "settings": config.get("settings", {})
    }

@app.get("/api/whales/balances")
async def get_whale_balances():
    """Get live balances for all tracked whales"""
    config = load_whale_config()
    results = []
    
    for whale in config.get("whales", []):
        balances = fetch_wallet_balance(whale["address"])
        results.append({
            "address": whale["address"],
            "name": whale.get("name", "Unknown"),
            "usdc": balances.get("usdc", 0),
            "pol": balances.get("pol", 0),
            "profit_estimate": whale.get("profit_estimate"),
            "win_rate": whale.get("win_rate")
        })
    
    # Sort by USDC balance
    results.sort(key=lambda x: x["usdc"], reverse=True)
    
    return {
        "count": len(results),
        "whales": results,
        "fetched_at": datetime.now().isoformat()
    }
@app.get("/api/whales/positions")
async def get_all_whale_positions():
    """Get positions for all tracked whales"""
    config = load_whale_config()
    results = []
    
    for whale in config.get("whales", []):
        positions = fetch_polymarket_positions(whale["address"], limit=20)
        value = fetch_polymarket_value(whale["address"])
        
        # Calculate totals
        total_pnl = 0
        position_count = 0
        if isinstance(positions, list):
            position_count = len(positions)
            total_pnl = sum(p.get("cashPnl", 0) for p in positions)
        
        results.append({
            "address": whale["address"],
            "name": whale.get("name", "Unknown"),
            "portfolio_value": value,
            "position_count": position_count,
            "total_pnl": round(total_pnl, 2),
            "win_rate": whale.get("win_rate"),
            "profit_estimate": whale.get("profit_estimate")
        })
    
    # Sort by portfolio value
    results.sort(key=lambda x: x["portfolio_value"], reverse=True)
    
    return {
        "count": len(results),
        "whales": results,
        "fetched_at": datetime.now().isoformat()
    }


@app.get("/api/whales/{address}")
async def get_whale_profile(address: str):
    """Get detailed profile for a specific whale"""
    config = load_whale_config()
    
    whale_info = None
    for whale in config.get("whales", []):
        if whale["address"].lower() == address.lower():
            whale_info = whale
            break
    
    if not whale_info:
        raise HTTPException(status_code=404, detail="Whale not in tracking list")
    
    balances = fetch_wallet_balance(whale_info["address"])
    
    return {
        "address": whale_info["address"],
        "name": whale_info.get("name", "Unknown"),
        "notes": whale_info.get("notes"),
        "profit_estimate": whale_info.get("profit_estimate"),
        "win_rate": whale_info.get("win_rate"),
        "balances": balances,
        "polygonscan_url": f"https://polygonscan.com/address/{whale_info['address']}"
    }



# ============================================================================
# Whale Positions via Polymarket Data API
# ============================================================================

POLYMARKET_DATA_API = "https://data-api.polymarket.com"

def fetch_polymarket_positions(address: str, limit: int = 50) -> dict:
    """Fetch positions from Polymarket Data API"""
    try:
        url = f"{POLYMARKET_DATA_API}/positions?user={address}&limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def fetch_polymarket_value(address: str) -> float:
    """Fetch total portfolio value from Polymarket Data API"""
    try:
        url = f"{POLYMARKET_DATA_API}/value?user={address}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data[0].get("value", 0) if data else 0
    except:
        return 0

@app.get("/api/whales/{address}/positions")
async def get_whale_positions(address: str, limit: int = Query(50, le=100)):
    """Get detailed positions for a specific whale"""
    config = load_whale_config()
    
    # Find whale info
    whale_info = None
    for whale in config.get("whales", []):
        if whale["address"].lower() == address.lower():
            whale_info = whale
            break
    
    # Fetch positions from Polymarket
    positions = fetch_polymarket_positions(address, limit)
    value = fetch_polymarket_value(address)
    
    if isinstance(positions, dict) and positions.get("error"):
        raise HTTPException(status_code=502, detail=positions["error"])
    
    # Format positions
    formatted = []
    total_pnl = 0
    total_invested = 0
    
    for p in (positions if isinstance(positions, list) else []):
        total_pnl += p.get("cashPnl", 0)
        total_invested += p.get("initialValue", 0)
        
        formatted.append({
            "market": p.get("title", "Unknown"),
            "outcome": p.get("outcome"),
            "shares": round(p.get("size", 0), 2),
            "entry_price": p.get("avgPrice"),
            "current_price": p.get("curPrice"),
            "invested": round(p.get("initialValue", 0), 2),
            "current_value": round(p.get("currentValue", 0), 2),
            "pnl": round(p.get("cashPnl", 0), 2),
            "pnl_percent": round(p.get("percentPnl", 0), 2),
            "redeemable": p.get("redeemable", False),
            "end_date": p.get("endDate")
        })
    
    return {
        "address": address,
        "name": whale_info.get("name") if whale_info else "Unknown",
        "notes": whale_info.get("notes") if whale_info else None,
        "portfolio_value": value,
        "total_invested": round(total_invested, 2),
        "total_pnl": round(total_pnl, 2),
        "position_count": len(formatted),
        "positions": formatted,
        "fetched_at": datetime.now().isoformat()
    }

# ============================================================================
# Cross-Platform Arbitrage Scanner (Polymarket vs Kalshi)
# ============================================================================

import re
from difflib import SequenceMatcher

KALSHI_API = "https://api.elections.kalshi.com/v1/events"

def fetch_kalshi_markets() -> list:
    """Fetch markets from Kalshi"""
    try:
        req = urllib.request.Request(KALSHI_API + "?limit=200", headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            
        markets = []
        for event in data.get("events", []):
            for m in event.get("markets", []):
                yes_bid = float(m.get("yes_bid", 0)) / 100
                yes_ask = float(m.get("yes_ask", 0)) / 100
                yes_price = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else float(m.get("last_price", 0)) / 100
                
                if yes_price > 0:
                    markets.append({
                        "platform": "kalshi",
                        "title": m.get("title", event.get("title", "")),
                        "yes_price": yes_price,
                        "no_price": 1 - yes_price,
                        "volume": float(m.get("dollar_volume", 0) or 0),
                        "ticker": m.get("ticker_name", ""),
                        "url": f"https://kalshi.com/markets/{m.get('ticker_name', '')}"
                    })
        return markets
    except Exception as e:
        return [{"error": str(e)}]

def fetch_polymarket_for_arb() -> list:
    """Fetch Polymarket markets for arb comparison"""
    try:
        params = {"active": "true", "closed": "false", "limit": "300", "order": "volume24hr", "ascending": "false"}
        url = GAMMA_API + "/markets?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        
        markets = []
        for m in data:
            try:
                prices = json.loads(m.get("outcomePrices", "[0,0]"))
                yes_price = float(prices[0]) if prices else 0
                no_price = float(prices[1]) if len(prices) > 1 else 1 - yes_price
                
                if yes_price > 0:
                    markets.append({
                        "platform": "polymarket",
                        "title": m.get("question", ""),
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "volume": float(m.get("volumeNum", 0) or 0),
                        "slug": m.get("slug", ""),
                        "url": f"https://polymarket.com/event/{m.get('slug', '')}"
                    })
            except:
                continue
        return markets
    except Exception as e:
        return [{"error": str(e)}]

def normalize_text(text: str) -> str:
    """Normalize text for comparison"""
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def calculate_match_score(title1: str, title2: str) -> float:
    """Calculate similarity between two market titles"""
    norm1 = normalize_text(title1)
    norm2 = normalize_text(title2)
    return SequenceMatcher(None, norm1, norm2).ratio()

def find_cross_arb_opportunities(min_spread: float = 3.0, min_match: float = 0.6) -> dict:
    """Find cross-platform arbitrage opportunities"""
    poly_markets = fetch_polymarket_for_arb()
    kalshi_markets = fetch_kalshi_markets()
    
    if isinstance(poly_markets, list) and poly_markets and "error" in poly_markets[0]:
        return {"error": "Failed to fetch Polymarket", "opportunities": []}
    if isinstance(kalshi_markets, list) and kalshi_markets and "error" in kalshi_markets[0]:
        return {"error": "Failed to fetch Kalshi", "opportunities": []}
    
    opportunities = []
    matches_found = []
    
    for poly in poly_markets:
        best_match = None
        best_score = 0
        
        for kalshi in kalshi_markets:
            score = calculate_match_score(poly["title"], kalshi["title"])
            if score > best_score:
                best_score = score
                best_match = kalshi
        
        if best_match and best_score >= min_match:
            # Calculate arb: Buy YES on one, NO on other
            # Scenario 1: Poly YES + Kalshi NO
            cost1 = poly["yes_price"] + best_match["no_price"]
            spread1 = (1 - cost1) * 100
            
            # Scenario 2: Kalshi YES + Poly NO
            cost2 = best_match["yes_price"] + poly["no_price"]
            spread2 = (1 - cost2) * 100
            
            if spread1 >= min_spread or spread2 >= min_spread:
                best_spread = max(spread1, spread2)
                if spread1 >= spread2:
                    action = f"BUY YES on Polymarket @ {poly['yes_price']:.2f}, BUY NO on Kalshi @ {best_match['no_price']:.2f}"
                else:
                    action = f"BUY YES on Kalshi @ {best_match['yes_price']:.2f}, BUY NO on Polymarket @ {poly['no_price']:.2f}"
                
                opportunities.append({
                    "event": poly["title"][:100],
                    "polymarket": {
                        "title": poly["title"],
                        "yes_price": poly["yes_price"],
                        "no_price": poly["no_price"],
                        "url": poly["url"]
                    },
                    "kalshi": {
                        "title": best_match["title"],
                        "yes_price": best_match["yes_price"],
                        "no_price": best_match["no_price"],
                        "url": best_match["url"]
                    },
                    "spread_pct": round(best_spread, 2),
                    "profit_per_100": round(best_spread, 2),
                    "action": action,
                    "match_score": round(best_score, 2)
                })
            
            matches_found.append({
                "poly": poly["title"][:60],
                "kalshi": best_match["title"][:60],
                "score": round(best_score, 2),
                "poly_yes": poly["yes_price"],
                "kalshi_yes": best_match["yes_price"]
            })
    
    # Sort by spread
    opportunities.sort(key=lambda x: x["spread_pct"], reverse=True)
    matches_found.sort(key=lambda x: x["score"], reverse=True)
    
    return {
        "polymarket_count": len(poly_markets),
        "kalshi_count": len(kalshi_markets),
        "matches_found": len(matches_found),
        "opportunities_count": len(opportunities),
        "opportunities": opportunities[:20],
        "top_matches": matches_found[:15],
        "config": {"min_spread_pct": min_spread, "min_match_score": min_match},
        "scanned_at": datetime.now().isoformat()
    }

@app.get("/api/cross-arb")
async def cross_platform_arb_scan(
    min_spread: float = Query(3.0, ge=0.5, le=20, description="Minimum spread % to report"),
    min_match: float = Query(0.6, ge=0.3, le=1.0, description="Minimum title match score")
):
    """Scan for cross-platform arbitrage between Polymarket and Kalshi"""
    return find_cross_arb_opportunities(min_spread, min_match)

@app.get("/api/cross-arb/matches")
async def get_cross_platform_matches(
    min_match: float = Query(0.5, ge=0.3, le=1.0)
):
    """Get matched markets between Polymarket and Kalshi (without arb filter)"""
    poly_markets = fetch_polymarket_for_arb()
    kalshi_markets = fetch_kalshi_markets()
    
    matches = []
    for poly in poly_markets[:100]:  # Limit for performance
        for kalshi in kalshi_markets:
            score = calculate_match_score(poly["title"], kalshi["title"])
            if score >= min_match:
                price_diff = abs(poly["yes_price"] - kalshi["yes_price"]) * 100
                matches.append({
                    "polymarket": {"title": poly["title"][:80], "yes": poly["yes_price"], "url": poly["url"]},
                    "kalshi": {"title": kalshi["title"][:80], "yes": kalshi["yes_price"], "url": kalshi["url"]},
                    "match_score": round(score, 2),
                    "price_diff_pct": round(price_diff, 1)
                })
    
    matches.sort(key=lambda x: x["match_score"], reverse=True)
    return {"count": len(matches), "matches": matches[:30], "scanned_at": datetime.now().isoformat()}


# =============================================================================
# Curated Cross-Platform Arbitrage
# =============================================================================

def load_curated_pairs():
    """Load curated market pairs config"""
    config_path = Path(__file__).parent.parent / "config/cross-arb-pairs.json"
    if config_path.exists():
        with open(config_path) as f:
            return json.load(f)
    return {"pairs": []}

def find_curated_arb_opportunities():
    """Find arb opportunities using curated market pairs"""
    config = load_curated_pairs()
    poly_markets = fetch_polymarket_for_arb()
    kalshi_markets = fetch_kalshi_markets()
    
    opportunities = []
    
    for pair in config.get("pairs", []):
        # Find matching Polymarket markets
        poly_matches = []
        for pm in poly_markets:
            title_lower = pm["title"].lower()
            if any(kw.lower() in title_lower for kw in pair.get("polymarket_keywords", [])):
                poly_matches.append(pm)
        
        # Find matching Kalshi markets
        kalshi_matches = []
        for km in kalshi_markets:
            title_lower = km["title"].lower()
            ticker = km.get("ticker", "")
            # Match by keywords OR ticker prefix
            if any(kw.lower() in title_lower for kw in pair.get("kalshi_keywords", [])):
                kalshi_matches.append(km)
            elif any(ticker.startswith(t) for t in pair.get("kalshi_tickers", [])):
                kalshi_matches.append(km)
        
        # Find best price match for arb
        for pm in poly_matches:
            for km in kalshi_matches:
                poly_yes = pm["yes_price"]
                kalshi_yes = km["yes_price"]
                spread = abs(poly_yes - kalshi_yes) * 100
                
                if spread >= 1.0:  # At least 1% spread
                    opportunities.append({
                        "pair_id": pair["id"],
                        "pair_name": pair["name"],
                        "category": pair.get("category", "unknown"),
                        "polymarket": {
                            "title": pm["title"][:80],
                            "yes_price": poly_yes,
                            "url": pm["url"]
                        },
                        "kalshi": {
                            "title": km["title"][:80],
                            "yes_price": kalshi_yes,
                            "url": km["url"]
                        },
                        "spread_pct": round(spread, 2),
                        "direction": "buy_poly" if poly_yes < kalshi_yes else "buy_kalshi",
                        "notes": pair.get("notes", "")
                    })
    
    # Sort by spread
    opportunities.sort(key=lambda x: x["spread_pct"], reverse=True)
    return opportunities


@app.get("/api/cross-arb/curated")
async def curated_cross_arb_scan():
    """Scan for arb using curated market pairs (more accurate than fuzzy matching)"""
    config = load_curated_pairs()
    opportunities = find_curated_arb_opportunities()
    
    return {
        "description": "Curated cross-platform arbitrage opportunities",
        "pairs_configured": len(config.get("pairs", [])),
        "opportunities_found": len(opportunities),
        "opportunities": opportunities,
        "scanned_at": datetime.now().isoformat()
    }


@app.get("/api/cross-arb/pairs")
async def list_curated_pairs():
    """List all configured curated market pairs"""
    config = load_curated_pairs()
    return {
        "last_updated": config.get("last_updated", "unknown"),
        "count": len(config.get("pairs", [])),
        "pairs": config.get("pairs", [])
    }
