#!/usr/bin/env python3
"""
Polymarket Trading Bot - FastAPI Backend
Paper trading + Simmer SDK live trading integration.
"""

import json
import os
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
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

# Polymarket paper trading (separate from Simmer)
POLY_STORAGE_DIR = Path.home() / ".openclaw" / "paper-trading-polymarket"
POLY_BALANCE_FILE = POLY_STORAGE_DIR / "balance.json"
POLY_POSITIONS_FILE = POLY_STORAGE_DIR / "positions.json"
POLY_TRADES_FILE = POLY_STORAGE_DIR / "trades.json"

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

@app.get("/api/markets/new")
async def get_new_markets():
    """Detect newly created markets on Polymarket"""
    return scan_new_markets()

@app.get("/api/markets/opportunities")
async def get_market_opportunities(min_liquidity: float = Query(1000, description="Minimum liquidity USD")):
    """Get new markets with enough liquidity to trade"""
    result = scan_new_markets()
    
    # Filter by minimum liquidity
    tradeable = [m for m in result.get("new_markets", []) 
                 if m.get("liquidity", 0) >= min_liquidity]
    
    return {
        "opportunities": tradeable,
        "count": len(tradeable),
        "scan_time": result.get("scan_time"),
        "note": "New markets with liquidity - early mover opportunities"
    }

@app.get("/api/markets/{market_id}")
async def get_market_details(market_id: str):
    market = get_market(market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    yes_price, no_price = get_market_prices(market)
    return {"id": market["id"], "question": market.get("question"), "description": market.get("description"), "slug": market.get("slug"), "yes_price": yes_price, "no_price": no_price, "volume_24h": market.get("volume24hr", 0), "liquidity": market.get("liquidityNum", 0), "created_at": market.get("createdAt"), "end_date": market.get("endDate"), "closed": market.get("closed", False)}

# ============================================================================
# Volume Spike Detection
# ============================================================================

VOLUME_STATE_FILE = Path(__file__).parent.parent / "data" / "volume_state.json"

def load_volume_state() -> dict:
    """Load historical volume data"""
    VOLUME_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if VOLUME_STATE_FILE.exists():
        with open(VOLUME_STATE_FILE) as f:
            return json.load(f)
    return {"markets": {}, "last_scan": None}

def save_volume_state(state: dict):
    """Save volume state"""
    with open(VOLUME_STATE_FILE, "w") as f:
        json.dump(state, f)

import math

def scan_volume_spikes(spike_threshold: float = 2.0, use_zscore: bool = True) -> dict:
    """Detect markets with unusual volume activity using z-score or ratio"""
    old_state = load_volume_state()
    old_volumes = old_state.get("markets", {})
    
    # Fetch active markets
    try:
        url = f"{GAMMA_API}/markets?limit=200&active=true&closed=false"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            markets = json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e), "spikes": []}
    
    new_state = {"markets": {}, "last_scan": datetime.now().isoformat()}
    spikes = []
    
    for m in markets:
        market_id = m.get("id", "")
        if not market_id:
            continue
        
        current_volume = m.get("volume24hr", 0) or 0
        liquidity = m.get("liquidityNum", 0) or 0
        
        # Get historical data
        old_data = old_volumes.get(market_id, {})
        avg_volume = old_data.get("avg_volume", current_volume)
        variance = old_data.get("variance", 0)
        samples = old_data.get("samples", 0)
        
        # Update running statistics (Welford's algorithm for online variance)
        new_samples = samples + 1
        delta = current_volume - avg_volume
        new_avg = avg_volume + delta / new_samples
        delta2 = current_volume - new_avg
        new_variance = ((variance * samples) + delta * delta2) / new_samples if new_samples > 1 else 0
        
        # Calculate standard deviation
        std_dev = math.sqrt(new_variance) if new_variance > 0 else 0
        
        new_state["markets"][market_id] = {
            "title": m.get("question", "")[:100],
            "volume_24h": current_volume,
            "avg_volume": new_avg,
            "variance": new_variance,
            "std_dev": std_dev,
            "samples": new_samples
        }
        
        # Check for spike (need at least 5 samples for reliable std dev)
        if samples >= 5 and avg_volume > 1000:  # Min $1k avg volume
            # Calculate z-score
            z_score = (current_volume - avg_volume) / std_dev if std_dev > 0 else 0
            volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
            
            # Use z-score or ratio based on parameter
            is_spike = z_score >= spike_threshold if use_zscore else volume_ratio >= spike_threshold
            
            if is_spike and current_volume > avg_volume:  # Only positive spikes
                yes_price = 0.5
                if m.get("outcomePrices"):
                    try:
                        yes_price = float(m["outcomePrices"][0])
                    except:
                        pass
                
                spikes.append({
                    "market_id": market_id,
                    "title": m.get("question", "Unknown"),
                    "current_volume": round(current_volume, 2),
                    "avg_volume": round(avg_volume, 2),
                    "std_dev": round(std_dev, 2),
                    "z_score": round(z_score, 2),
                    "spike_ratio": round(volume_ratio, 1),
                    "liquidity": round(liquidity, 2),
                    "yes_price": yes_price,
                    "url": f"https://polymarket.com/event/{m.get('slug', market_id)}",
                    "detected_at": datetime.now().isoformat()
                })
    
    save_volume_state(new_state)
    
    # Sort by z-score
    spikes.sort(key=lambda x: x["z_score"], reverse=True)
    
    return {
        "spikes": spikes[:20],
        "count": len(spikes),
        "method": "z-score" if use_zscore else "ratio",
        "threshold": spike_threshold,
        "markets_scanned": len(markets),
        "scan_time": new_state["last_scan"],
        "previous_scan": old_state.get("last_scan"),
        "note": f"Volume {'>' if use_zscore else ''}{spike_threshold}{'σ above mean' if use_zscore else 'x normal'} - potential information edge"
    }

@app.get("/api/volume/spikes")
async def get_volume_spikes(
    threshold: float = Query(2.0, ge=1.0, le=5, description="Z-score threshold (2.0 = 2 std devs above mean)"),
    method: str = Query("zscore", description="Detection method: 'zscore' or 'ratio'")
):
    """Detect markets with unusual volume spikes using statistical analysis"""
    use_zscore = method.lower() == "zscore"
    return scan_volume_spikes(threshold, use_zscore)


# ============================================================================
# Resolution Timing - Markets Near Expiry
# ============================================================================

def scan_resolution_timing(hours_until: int = 48) -> dict:
    """Find markets approaching resolution - volatility opportunities"""
    try:
        url = f"{GAMMA_API}/markets?limit=300&active=true&closed=false"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            markets = json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e), "markets": []}
    
    now = datetime.now()
    approaching = []
    
    for m in markets:
        end_date_str = m.get("endDate")
        if not end_date_str:
            continue
        
        try:
            # Parse ISO date
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00").replace("+00:00", ""))
            hours_left = (end_date - now).total_seconds() / 3600
            
            if 0 < hours_left <= hours_until:
                yes_price = 0.5
                if m.get("outcomePrices"):
                    try:
                        yes_price = float(m["outcomePrices"][0])
                    except:
                        pass
                
                # Calculate "edge potential" - markets near 50% have most uncertainty
                uncertainty = 1 - abs(yes_price - 0.5) * 2  # 1.0 at 50%, 0.0 at 0% or 100%
                
                approaching.append({
                    "market_id": m.get("id"),
                    "title": m.get("question", "Unknown"),
                    "yes_price": yes_price,
                    "hours_until_resolution": round(hours_left, 1),
                    "end_date": end_date_str,
                    "volume_24h": m.get("volume24hr", 0),
                    "liquidity": m.get("liquidityNum", 0),
                    "uncertainty_score": round(uncertainty, 2),
                    "url": f"https://polymarket.com/event/{m.get('slug', m.get('id'))}",
                    "opportunity": "HIGH" if uncertainty > 0.7 and hours_left < 24 else "MEDIUM" if uncertainty > 0.5 else "LOW"
                })
        except:
            continue
    
    # Sort by hours until resolution
    approaching.sort(key=lambda x: x["hours_until_resolution"])
    
    return {
        "markets": approaching[:30],
        "count": len(approaching),
        "hours_threshold": hours_until,
        "scan_time": datetime.now().isoformat(),
        "note": "Markets near resolution often see volatility spikes as outcomes become clearer"
    }

@app.get("/api/resolution/approaching")
async def get_approaching_resolution(
    hours: int = Query(48, ge=1, le=168, description="Hours until resolution threshold")
):
    """Find markets approaching resolution - volatility opportunities"""
    return scan_resolution_timing(hours)

@app.get("/api/resolution/imminent")
async def get_imminent_resolution():
    """Markets resolving within 24 hours - highest volatility potential"""
    result = scan_resolution_timing(24)
    # Filter to high opportunity only
    high_opp = [m for m in result.get("markets", []) if m.get("opportunity") == "HIGH"]
    return {
        "markets": high_opp,
        "count": len(high_opp),
        "note": "HIGH uncertainty markets resolving within 24h - prime volatility plays"
    }


# ============================================================================
# Price Alerts System
# ============================================================================

PRICE_ALERTS_FILE = Path(__file__).parent.parent / "data" / "price_alerts.json"

def load_price_alerts() -> list:
    """Load configured price alerts"""
    PRICE_ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if PRICE_ALERTS_FILE.exists():
        with open(PRICE_ALERTS_FILE) as f:
            return json.load(f)
    return []

def save_price_alerts(alerts: list):
    """Save price alerts"""
    with open(PRICE_ALERTS_FILE, "w") as f:
        json.dump(alerts, f, indent=2)

def check_price_alerts() -> dict:
    """Check all price alerts against current prices"""
    alerts = load_price_alerts()
    if not alerts:
        return {"triggered": [], "active": [], "count": 0}
    
    # Fetch current prices for all monitored markets
    triggered = []
    still_active = []
    
    for alert in alerts:
        market_id = alert.get("market_id")
        if not market_id:
            continue
        
        try:
            url = f"{GAMMA_API}/markets/{market_id}"
            req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                market = json.loads(resp.read().decode())
            
            current_price = 0.5
            if market.get("outcomePrices"):
                current_price = float(market["outcomePrices"][0])
            
            target = alert.get("target_price", 0)
            direction = alert.get("direction", "above")
            
            is_triggered = (
                (direction == "above" and current_price >= target) or
                (direction == "below" and current_price <= target)
            )
            
            if is_triggered:
                triggered.append({
                    **alert,
                    "current_price": current_price,
                    "triggered_at": datetime.now().isoformat()
                })
            else:
                alert["current_price"] = current_price
                alert["last_checked"] = datetime.now().isoformat()
                still_active.append(alert)
        except:
            still_active.append(alert)  # Keep alert if check failed
    
    # Remove triggered alerts
    save_price_alerts(still_active)
    
    return {
        "triggered": triggered,
        "active": still_active,
        "triggered_count": len(triggered),
        "active_count": len(still_active)
    }

@app.get("/api/alerts")
async def get_price_alerts():
    """List all active price alerts"""
    alerts = load_price_alerts()
    return {"alerts": alerts, "count": len(alerts)}

@app.post("/api/alerts")
async def create_price_alert(
    market_id: str = Query(..., description="Market ID to monitor"),
    target_price: float = Query(..., ge=0.01, le=0.99, description="Target price (0.01-0.99)"),
    direction: str = Query("above", description="Trigger when price goes 'above' or 'below' target"),
    note: str = Query(None, description="Optional note for this alert")
):
    """Create a new price alert"""
    alerts = load_price_alerts()
    
    # Verify market exists
    try:
        url = f"{GAMMA_API}/markets/{market_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            market = json.loads(resp.read().decode())
        
        current_price = 0.5
        if market.get("outcomePrices"):
            current_price = float(market["outcomePrices"][0])
    except:
        raise HTTPException(status_code=404, detail="Market not found")
    
    new_alert = {
        "id": f"alert_{len(alerts)+1}_{int(datetime.now().timestamp())}",
        "market_id": market_id,
        "title": market.get("question", "Unknown")[:100],
        "target_price": target_price,
        "direction": direction,
        "current_price": current_price,
        "note": note,
        "created_at": datetime.now().isoformat()
    }
    
    alerts.append(new_alert)
    save_price_alerts(alerts)
    
    return {"created": new_alert, "total_alerts": len(alerts)}

@app.delete("/api/alerts/{alert_id}")
async def delete_price_alert(alert_id: str):
    """Delete a price alert"""
    alerts = load_price_alerts()
    original_count = len(alerts)
    alerts = [a for a in alerts if a.get("id") != alert_id]
    
    if len(alerts) == original_count:
        raise HTTPException(status_code=404, detail="Alert not found")
    
    save_price_alerts(alerts)
    return {"deleted": alert_id, "remaining": len(alerts)}

@app.get("/api/alerts/check")
async def check_alerts():
    """Check all alerts and return triggered ones"""
    return check_price_alerts()


# ============================================================================
# Predictor Accuracy Tracking
# ============================================================================

PREDICTOR_STATS_FILE = Path(__file__).parent.parent / "data" / "predictor_stats.json"

def load_predictor_stats() -> dict:
    """Load predictor accuracy statistics"""
    PREDICTOR_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if PREDICTOR_STATS_FILE.exists():
        with open(PREDICTOR_STATS_FILE) as f:
            return json.load(f)
    return {"predictors": {}, "last_updated": None}

def save_predictor_stats(stats: dict):
    """Save predictor statistics"""
    with open(PREDICTOR_STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2)

def update_predictor_stats() -> dict:
    """Update predictor accuracy based on resolved positions"""
    config = load_whale_config()
    stats = load_predictor_stats()
    predictors = stats.get("predictors", {})
    
    for whale in config.get("whales", []):
        address = whale["address"]
        name = whale.get("name", "Unknown")
        
        # Fetch positions including resolved ones
        positions = fetch_polymarket_positions(address, limit=100)
        if isinstance(positions, dict) and positions.get("error"):
            continue
        
        # Initialize predictor if new
        if address not in predictors:
            predictors[address] = {
                "name": name,
                "total_predictions": 0,
                "correct_predictions": 0,
                "total_profit": 0,
                "positions_tracked": [],
                "accuracy": 0
            }
        
        pred = predictors[address]
        tracked_ids = set(pred.get("positions_tracked", []))
        
        for p in (positions if isinstance(positions, list) else []):
            pos_id = p.get("asset", "")
            if not pos_id or pos_id in tracked_ids:
                continue
            
            # Check if position is resolved (redeemable or has final PnL)
            is_resolved = p.get("redeemable", False) or p.get("resolved", False)
            pnl = p.get("cashPnl", 0)
            
            if is_resolved and pnl != 0:
                pred["total_predictions"] += 1
                pred["total_profit"] += pnl
                
                if pnl > 0:
                    pred["correct_predictions"] += 1
                
                pred["positions_tracked"].append(pos_id)
                
                # Keep only last 200 tracked positions
                if len(pred["positions_tracked"]) > 200:
                    pred["positions_tracked"] = pred["positions_tracked"][-200:]
        
        # Calculate accuracy
        if pred["total_predictions"] > 0:
            pred["accuracy"] = round(pred["correct_predictions"] / pred["total_predictions"] * 100, 1)
        
        pred["name"] = name  # Update name if changed
    
    stats["predictors"] = predictors
    stats["last_updated"] = datetime.now().isoformat()
    save_predictor_stats(stats)
    
    return stats

@app.get("/api/predictors")
async def get_predictor_stats():
    """Get accuracy statistics for all tracked predictors (whales)"""
    stats = load_predictor_stats()
    predictors = stats.get("predictors", {})
    
    # Format for response
    leaderboard = []
    for address, data in predictors.items():
        if data.get("total_predictions", 0) > 0:
            leaderboard.append({
                "address": address,
                "name": data.get("name", "Unknown"),
                "accuracy": data.get("accuracy", 0),
                "total_predictions": data.get("total_predictions", 0),
                "correct_predictions": data.get("correct_predictions", 0),
                "total_profit": round(data.get("total_profit", 0), 2),
                "avg_profit_per_trade": round(data.get("total_profit", 0) / data.get("total_predictions", 1), 2)
            })
    
    # Sort by accuracy (with min predictions threshold)
    leaderboard.sort(key=lambda x: (x["total_predictions"] >= 10, x["accuracy"]), reverse=True)
    
    return {
        "leaderboard": leaderboard,
        "count": len(leaderboard),
        "last_updated": stats.get("last_updated"),
        "note": "Accuracy based on resolved positions only"
    }

@app.post("/api/predictors/update")
async def refresh_predictor_stats():
    """Refresh predictor accuracy statistics"""
    stats = update_predictor_stats()
    return {
        "updated": True,
        "predictors_tracked": len(stats.get("predictors", {})),
        "last_updated": stats.get("last_updated")
    }


# ============================================================================
# Inverse Whale Strategy - Fade the Losers
# ============================================================================

def get_inverse_whale_signals() -> dict:
    """Find positions where losing whales are heavily invested - fade them"""
    stats = load_predictor_stats()
    predictors = stats.get("predictors", {})
    config = load_whale_config()
    
    # Identify losing whales (accuracy < 50% with 10+ trades)
    losing_whales = []
    for address, data in predictors.items():
        if data.get("total_predictions", 0) >= 10 and data.get("accuracy", 50) < 50:
            losing_whales.append({
                "address": address,
                "name": data.get("name", "Unknown"),
                "accuracy": data.get("accuracy", 0),
                "total_profit": data.get("total_profit", 0)
            })
    
    if not losing_whales:
        return {"signals": [], "losing_whales": [], "note": "No losing whales identified yet (need more data)"}
    
    # Get current positions of losing whales
    inverse_signals = []
    market_aggregates = {}  # Aggregate positions per market
    
    for whale in losing_whales:
        positions = fetch_polymarket_positions(whale["address"], limit=30)
        if isinstance(positions, dict) and positions.get("error"):
            continue
        
        for p in (positions if isinstance(positions, list) else []):
            if p.get("currentValue", 0) < 100:  # Skip small positions
                continue
            
            market_title = p.get("title", "Unknown")
            outcome = p.get("outcome", "").upper()
            value = p.get("currentValue", 0)
            
            # Determine inverse side
            inverse_side = "NO" if outcome == "YES" else "YES"
            
            market_key = market_title[:50]
            if market_key not in market_aggregates:
                market_aggregates[market_key] = {
                    "title": market_title,
                    "whale_side": outcome,
                    "inverse_side": inverse_side,
                    "total_whale_value": 0,
                    "whale_count": 0,
                    "whales": [],
                    "avg_entry": p.get("avgPrice", 0.5),
                    "current_price": p.get("curPrice", 0.5)
                }
            
            market_aggregates[market_key]["total_whale_value"] += value
            market_aggregates[market_key]["whale_count"] += 1
            market_aggregates[market_key]["whales"].append({
                "name": whale["name"],
                "accuracy": whale["accuracy"],
                "value": value
            })
    
    # Convert to list and calculate confidence
    for market_key, data in market_aggregates.items():
        # Higher value + more whales + lower accuracy = stronger inverse signal
        avg_accuracy = sum(w["accuracy"] for w in data["whales"]) / len(data["whales"])
        confidence = min(100, (data["total_whale_value"] / 1000) * (50 - avg_accuracy))
        
        inverse_signals.append({
            "market": data["title"],
            "whale_side": data["whale_side"],
            "inverse_side": data["inverse_side"],
            "whale_value": round(data["total_whale_value"], 2),
            "whale_count": data["whale_count"],
            "avg_whale_accuracy": round(avg_accuracy, 1),
            "current_price": data["current_price"],
            "confidence_score": round(confidence, 1),
            "action": f"BET {data['inverse_side']} (fade {data['whale_count']} losing whale{'s' if data['whale_count'] > 1 else ''})"
        })
    
    # Sort by confidence
    inverse_signals.sort(key=lambda x: x["confidence_score"], reverse=True)
    
    return {
        "signals": inverse_signals[:15],
        "count": len(inverse_signals),
        "losing_whales": losing_whales,
        "strategy": "Fade positions where losing whales (accuracy <50%) are heavily invested"
    }

@app.get("/api/inverse-whale")
async def inverse_whale_signals():
    """Get signals to fade losing whale positions"""
    return get_inverse_whale_signals()


# ============================================================================
# Smart Money Flow - Net Position by Market
# ============================================================================

def get_smart_money_flow() -> dict:
    """Calculate net whale buying/selling per market"""
    config = load_whale_config()
    stats = load_predictor_stats()
    predictors = stats.get("predictors", {})
    
    market_flows = {}
    
    for whale in config.get("whales", []):
        address = whale["address"]
        name = whale.get("name", "Unknown")
        
        # Get whale accuracy for weighting
        whale_data = predictors.get(address, {})
        accuracy = whale_data.get("accuracy", 50)
        weight = accuracy / 50  # >1 if good, <1 if bad
        
        positions = fetch_polymarket_positions(address, limit=50)
        if isinstance(positions, dict) and positions.get("error"):
            continue
        
        for p in (positions if isinstance(positions, list) else []):
            value = p.get("currentValue", 0)
            if value < 50:  # Skip tiny positions
                continue
            
            market_title = p.get("title", "Unknown")[:80]
            outcome = p.get("outcome", "").upper()
            
            if market_title not in market_flows:
                market_flows[market_title] = {
                    "title": market_title,
                    "yes_value": 0,
                    "no_value": 0,
                    "yes_weighted": 0,
                    "no_weighted": 0,
                    "whales_yes": [],
                    "whales_no": [],
                    "current_price": p.get("curPrice", 0.5)
                }
            
            weighted_value = value * weight
            
            if outcome == "YES":
                market_flows[market_title]["yes_value"] += value
                market_flows[market_title]["yes_weighted"] += weighted_value
                market_flows[market_title]["whales_yes"].append({"name": name, "value": value, "accuracy": accuracy})
            else:
                market_flows[market_title]["no_value"] += value
                market_flows[market_title]["no_weighted"] += weighted_value
                market_flows[market_title]["whales_no"].append({"name": name, "value": value, "accuracy": accuracy})
    
    # Calculate net flow and signal
    flow_signals = []
    for market, data in market_flows.items():
        net_raw = data["yes_value"] - data["no_value"]
        net_weighted = data["yes_weighted"] - data["no_weighted"]
        total_value = data["yes_value"] + data["no_value"]
        
        if total_value < 200:  # Skip low-value markets
            continue
        
        # Determine signal
        if abs(net_weighted) > 500:  # Significant weighted flow
            signal_side = "YES" if net_weighted > 0 else "NO"
            conviction = "STRONG" if abs(net_weighted) > 2000 else "MODERATE"
        else:
            signal_side = "NEUTRAL"
            conviction = "WEAK"
        
        flow_signals.append({
            "market": data["title"],
            "net_flow_raw": round(net_raw, 2),
            "net_flow_weighted": round(net_weighted, 2),
            "yes_total": round(data["yes_value"], 2),
            "no_total": round(data["no_value"], 2),
            "whales_on_yes": len(data["whales_yes"]),
            "whales_on_no": len(data["whales_no"]),
            "current_price": data["current_price"],
            "signal": signal_side,
            "conviction": conviction,
            "action": f"{conviction} {signal_side}" if signal_side != "NEUTRAL" else "No clear signal"
        })
    
    # Sort by absolute weighted flow
    flow_signals.sort(key=lambda x: abs(x["net_flow_weighted"]), reverse=True)
    
    return {
        "flows": flow_signals[:20],
        "count": len(flow_signals),
        "note": "Weighted by whale accuracy. Positive = bullish YES, Negative = bullish NO"
    }

@app.get("/api/smart-money")
async def smart_money_flow():
    """Get net whale flow per market (weighted by accuracy)"""
    return get_smart_money_flow()


# ============================================================================
# Auto-Execute Pipeline
# ============================================================================

AUTO_TRADE_LOG = Path(__file__).parent.parent / "data" / "auto_trades.json"

def load_auto_trade_log() -> list:
    """Load auto trade history"""
    AUTO_TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)
    if AUTO_TRADE_LOG.exists():
        with open(AUTO_TRADE_LOG) as f:
            return json.load(f)
    return []

def save_auto_trade_log(trades: list):
    """Save auto trade log"""
    with open(AUTO_TRADE_LOG, "w") as f:
        json.dump(trades, f, indent=2)

def find_simmer_market(title: str) -> Optional[dict]:
    """Try to find matching market on Simmer by title keywords"""
    # Extract key terms from title
    keywords = [w for w in title.lower().split() if len(w) > 3][:5]
    query = " ".join(keywords[:3])
    
    result = simmer_request(f"/markets?q={urllib.parse.quote(query)}&status=active&limit=10")
    if not result or result.get("error"):
        return None
    
    markets = result.get("markets", result) if isinstance(result, dict) else result
    if not markets:
        return None
    
    # Find best match
    for m in (markets if isinstance(markets, list) else []):
        m_title = m.get("title", "").lower()
        if any(kw in m_title for kw in keywords[:2]):
            return m
    
    return markets[0] if markets else None

def execute_auto_strategy(
    strategy: str = "inverse_whale",
    max_trades: int = 3,
    max_per_trade: float = 50.0,
    bankroll: float = 10000.0,
    dry_run: bool = True
) -> dict:
    """Execute automated trading strategy"""
    
    results = {
        "strategy": strategy,
        "dry_run": dry_run,
        "trades_attempted": 0,
        "trades_executed": [],
        "trades_failed": [],
        "total_deployed": 0
    }
    
    # Get signals based on strategy
    if strategy == "inverse_whale":
        signals_data = get_inverse_whale_signals()
        signals = signals_data.get("signals", [])
    elif strategy == "smart_money":
        signals_data = get_smart_money_flow()
        raw_signals = signals_data.get("flows", [])
        # Convert to actionable signals
        signals = [
            {
                "market": s["market"],
                "inverse_side": s["signal"],
                "confidence_score": abs(s["net_flow_weighted"]) / 100,
                "current_price": s["current_price"]
            }
            for s in raw_signals 
            if s["conviction"] in ["STRONG", "MODERATE"] and s["signal"] != "NEUTRAL"
        ]
    else:
        return {"error": f"Unknown strategy: {strategy}"}
    
    if not signals:
        results["note"] = "No actionable signals found"
        return results
    
    # Execute trades
    trades_log = load_auto_trade_log()
    
    for signal in signals[:max_trades]:
        results["trades_attempted"] += 1
        
        market_title = signal.get("market", "")
        side = signal.get("inverse_side", "YES")
        confidence = signal.get("confidence_score", 50)
        current_price = signal.get("current_price", 0.5)
        
        # Calculate position size (simplified Kelly)
        edge_estimate = min(0.15, confidence / 200)  # Conservative edge estimate
        kelly_fraction = 0.25
        position_size = min(max_per_trade, bankroll * edge_estimate * kelly_fraction)
        
        if position_size < 10:
            results["trades_failed"].append({
                "market": market_title[:50],
                "reason": "Position size too small"
            })
            continue
        
        # Find market on Simmer
        simmer_market = find_simmer_market(market_title)
        if not simmer_market:
            results["trades_failed"].append({
                "market": market_title[:50],
                "reason": "Market not found on Simmer"
            })
            continue
        
        trade_record = {
            "market": market_title[:60],
            "market_id": simmer_market.get("id"),
            "side": side,
            "amount": round(position_size, 2),
            "strategy": strategy,
            "confidence": confidence,
            "timestamp": datetime.now().isoformat()
        }
        
        if dry_run:
            trade_record["status"] = "DRY_RUN"
            results["trades_executed"].append(trade_record)
        else:
            # Execute real trade via Simmer
            trade_data = {
                "market_id": simmer_market.get("id"),
                "side": side.lower(),
                "amount": round(position_size, 2),
                "reasoning": f"Auto-{strategy}: confidence {confidence}",
                "source": f"polyclawd:auto:{strategy}"
            }
            
            trade_result = simmer_request("/trade", method="POST", data=trade_data)
            
            if trade_result and not trade_result.get("error"):
                trade_record["status"] = "EXECUTED"
                trade_record["result"] = trade_result
                results["trades_executed"].append(trade_record)
                results["total_deployed"] += position_size
            else:
                trade_record["status"] = "FAILED"
                trade_record["error"] = trade_result.get("error") if trade_result else "API unavailable"
                results["trades_failed"].append(trade_record)
        
        trades_log.append(trade_record)
    
    save_auto_trade_log(trades_log)
    results["total_deployed"] = round(results["total_deployed"], 2)
    
    return results

def analyze_simmer_markets() -> dict:
    """Analyze Simmer markets for trading opportunities
    
    Enhanced with:
    1. Volume confirmation - high Polymarket volume in same direction = stronger
    2. Momentum alignment - Polymarket price moving toward our bet = stronger
    3. Divergence persistence - gaps that persist get boosted
    """
    result = simmer_request("/markets?status=active&limit=50")
    if not result or result.get("error"):
        return {"error": "Simmer API unavailable", "opportunities": []}
    
    markets = result.get("markets", result) if isinstance(result, dict) else result
    if not isinstance(markets, list):
        return {"error": "Invalid response", "opportunities": []}
    
    # Load divergence history for persistence tracking
    divergence_file = Path(__file__).parent.parent / "data" / "divergence_history.json"
    try:
        divergence_history = json.loads(divergence_file.read_text()) if divergence_file.exists() else {}
    except:
        divergence_history = {}
    
    # Get Polymarket volume data for confirmation
    try:
        poly_volume_data = {}
        volume_result = scan_volume_spikes(1.5, False)  # Get recent volume
        for spike in volume_result.get("spikes", []):
            poly_volume_data[spike.get("market_id", "")] = {
                "volume": spike.get("current_volume", 0),
                "z_score": spike.get("z_score", 0),
                "price": spike.get("yes_price", 0.5)
            }
    except:
        poly_volume_data = {}
    
    opportunities = []
    now = datetime.now().isoformat()
    
    for m in markets:
        # Simmer API uses 'question' and 'current_probability'
        title = m.get("question", m.get("title", "Unknown"))
        yes_price = m.get("current_probability", m.get("yes_price", 0.5))
        external_price = m.get("external_price_yes", yes_price)
        
        # Look for edge opportunities
        # Markets near 50% have highest uncertainty/opportunity
        uncertainty = 1 - abs(yes_price - 0.5) * 2
        
        # Estimate edge based on various factors
        edge_score = 0
        recommendation = "HOLD"
        reasoning = ""
        
        # Price divergence from external (Polymarket) price
        divergence = abs(yes_price - external_price) if external_price else 0
        volume_boost = 0
        momentum_boost = 0
        persistence_boost = 0
        boosts = []
        
        if divergence > 0.1:
            edge_score = divergence * 50
            if yes_price > external_price:
                recommendation = "NO"  # Simmer overpriced vs Polymarket
                reasoning = f"Simmer {yes_price*100:.0f}¢ vs Polymarket {external_price*100:.0f}¢ - overpriced"
            else:
                recommendation = "YES"  # Simmer underpriced
                reasoning = f"Simmer {yes_price*100:.0f}¢ vs Polymarket {external_price*100:.0f}¢ - underpriced"
            
            market_id = m.get("id", "")
            
            # BOOST 1: Volume confirmation
            # If Polymarket has high volume, it confirms price discovery
            if market_id in poly_volume_data:
                vol_data = poly_volume_data[market_id]
                if vol_data.get("z_score", 0) > 1.5:  # Above average volume
                    volume_boost = min(15, vol_data["z_score"] * 5)
                    boosts.append(f"+{volume_boost:.0f} volume")
            
            # BOOST 2: Momentum alignment
            # Check if Polymarket price movement supports our direction
            # If we're betting YES and Poly price is rising, that's confirmation
            poly_price = external_price
            if poly_price and poly_price != yes_price:
                if recommendation == "YES" and poly_price < 0.5:
                    # Poly thinks NO but price is low - momentum could flip
                    momentum_boost = 5
                    boosts.append("+5 momentum")
                elif recommendation == "NO" and poly_price > 0.5:
                    # Poly thinks YES but price is high - momentum could flip
                    momentum_boost = 5
                    boosts.append("+5 momentum")
            
            # BOOST 3: Divergence persistence
            # Track how long this divergence has existed
            div_key = f"{market_id}:{recommendation}"
            if div_key in divergence_history:
                first_seen = divergence_history[div_key].get("first_seen", now)
                try:
                    first_dt = datetime.fromisoformat(first_seen)
                    hours_persistent = (datetime.now() - first_dt).total_seconds() / 3600
                    if hours_persistent > 1:
                        persistence_boost = min(20, hours_persistent * 5)
                        boosts.append(f"+{persistence_boost:.0f} persistent({hours_persistent:.1f}h)")
                except:
                    pass
            else:
                # First time seeing this divergence
                divergence_history[div_key] = {"first_seen": now, "divergence": divergence}
            
            # BOOST 4: Category accuracy
            # Some market categories have better hit rates than others
            category_boost = 0
            title_lower = title.lower()
            
            # Detect category
            category = "other"
            if any(kw in title_lower for kw in ["bitcoin", "btc", "ethereum", "eth", "crypto", "xrp", "solana"]):
                category = "crypto"
            elif any(kw in title_lower for kw in ["trump", "biden", "election", "congress", "senate", "president", "governor"]):
                category = "politics"
            elif any(kw in title_lower for kw in ["nba", "nfl", "mlb", "soccer", "football", "basketball", "tennis", "ufc"]):
                category = "sports"
            elif any(kw in title_lower for kw in ["netflix", "spotify", "movie", "album", "billboard", "grammy", "oscar"]):
                category = "entertainment"
            elif any(kw in title_lower for kw in ["temperature", "weather", "rain", "snow"]):
                category = "weather"
            elif any(kw in title_lower for kw in ["fed", "interest rate", "gdp", "inflation", "unemployment"]):
                category = "economics"
            
            # Load category performance (or use priors)
            category_file = Path(__file__).parent.parent / "data" / "category_performance.json"
            try:
                cat_perf = json.loads(category_file.read_text()) if category_file.exists() else {}
            except:
                cat_perf = {}
            
            # Default priors based on predictability hypothesis
            category_priors = {
                "crypto": 0.52,      # Slightly predictable (momentum)
                "politics": 0.48,    # Polls often wrong
                "sports": 0.50,      # Efficient
                "entertainment": 0.50,
                "weather": 0.45,     # Forecasts usually right, hard to beat
                "economics": 0.55,   # Fed signals often readable
                "other": 0.50
            }
            
            cat_data = cat_perf.get(category, {"wins": 1, "losses": 1})
            cat_win_rate = cat_data.get("wins", 1) / max(1, cat_data.get("wins", 1) + cat_data.get("losses", 1))
            # Use prior if not enough data
            if cat_data.get("wins", 0) + cat_data.get("losses", 0) < 5:
                cat_win_rate = category_priors.get(category, 0.5)
            
            if cat_win_rate > 0.55:
                category_boost = (cat_win_rate - 0.5) * 40  # 60% = +4
                boosts.append(f"+{category_boost:.0f} {category}({cat_win_rate:.0%})")
            elif cat_win_rate < 0.45:
                category_boost = (cat_win_rate - 0.5) * 40  # 40% = -4
                boosts.append(f"{category_boost:.0f} {category}({cat_win_rate:.0%})")
            
            # BOOST 5: Time-of-day patterns
            # Some hours have better signal accuracy (market maker activity, news cycles)
            time_boost = 0
            current_hour = datetime.now().hour
            
            # Hypothesis: Early morning (pre-market) and late afternoon have more predictable moves
            # Avoid midday chop (11-14 EST)
            favorable_hours = {9: 5, 10: 3, 15: 3, 16: 5, 17: 3}  # Hour -> boost
            unfavorable_hours = {11: -3, 12: -5, 13: -5, 14: -3, 2: -5, 3: -5}  # Overnight chop
            
            if current_hour in favorable_hours:
                time_boost = favorable_hours[current_hour]
                boosts.append(f"+{time_boost} goodHour({current_hour}:00)")
            elif current_hour in unfavorable_hours:
                time_boost = unfavorable_hours[current_hour]
                boosts.append(f"{time_boost} badHour({current_hour}:00)")
            
            # Apply all boosts
            edge_score += volume_boost + momentum_boost + persistence_boost + category_boost + time_boost
            if boosts:
                reasoning += f" [{', '.join(boosts)}]"
        
        # Contrarian: Extreme prices often overcorrect
        # BUT skip truly extreme prices (< 2¢ or > 98¢) - likely resolved/garbage
        elif yes_price < 0.12 and yes_price >= 0.02:
            edge_score = (0.15 - yes_price) * 100
            recommendation = "YES"  # Potential undervalued
            reasoning = f"Extreme low price ({yes_price*100:.0f}¢)"
        elif yes_price > 0.88 and yes_price <= 0.98:
            edge_score = (yes_price - 0.85) * 100
            recommendation = "NO"  # Potential overvalued
            reasoning = f"Extreme high price ({yes_price*100:.0f}¢)"
        elif yes_price < 0.02 or yes_price > 0.98:
            # Skip garbage prices - likely resolved or data errors
            recommendation = "SKIP"
            reasoning = f"Price too extreme ({yes_price*100:.0f}¢) - likely resolved"
            edge_score = 0
        
        # Include all markets for visibility
        opportunities.append({
            "market_id": m.get("id"),
            "title": title,
            "simmer_price": round(yes_price, 3),
            "external_price": round(external_price, 3) if external_price else None,
            "divergence": round(divergence, 3),
            "uncertainty": round(uncertainty, 2),
            "edge_score": round(edge_score, 1),
            "recommendation": recommendation,
            "reasoning": reasoning or "No clear edge",
            "url": m.get("url"),
            "resolves_at": m.get("resolves_at")
        })
    
    # Sort by edge score
    opportunities.sort(key=lambda x: x["edge_score"], reverse=True)
    
    # Split into actionable vs monitoring (exclude HOLD and SKIP)
    actionable = [o for o in opportunities if o["recommendation"] not in ["HOLD", "SKIP"]]
    
    # Save divergence history for persistence tracking
    try:
        # Clean old entries (>24h)
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
        divergence_history = {k: v for k, v in divergence_history.items() 
                             if v.get("first_seen", "") > cutoff}
        divergence_file.write_text(json.dumps(divergence_history, indent=2))
    except:
        pass
    
    return {
        "actionable": actionable[:10],
        "all_markets": opportunities[:20],
        "actionable_count": len(actionable),
        "total_markets": len(markets),
        "note": "Edge from price divergence (Simmer vs Polymarket) and extreme prices + boosts"
    }

@app.get("/api/simmer/opportunities")
async def get_simmer_opportunities():
    """Find trading opportunities on Simmer markets"""
    return analyze_simmer_markets()

def execute_paper_trade(market_id: str, market_title: str, side: str, amount: float, price: float, reasoning: str, source: str = None) -> dict:
    """Execute a paper trade and update local storage"""
    ensure_storage()
    
    # Load current state
    balance_data = load_json(BALANCE_FILE)
    positions = load_json(POSITIONS_FILE)
    trades = load_json(TRADES_FILE)
    
    current_balance = balance_data.get("usdc", DEFAULT_BALANCE)
    
    # Check balance
    if amount > current_balance:
        return {"success": False, "error": f"Insufficient balance: ${current_balance:.2f}"}
    
    # Calculate shares
    shares = amount / price if price > 0 else 0
    
    # Extract source from reasoning if not provided
    if not source and reasoning:
        # Parse [SOURCE:xxx] or [ENGINE:xxx] from reasoning
        import re
        match = re.search(r'\[(ENGINE:)?(\w+)\]', reasoning)
        if match:
            source = match.group(2)
    
    # Deduct from balance
    balance_data["usdc"] = current_balance - amount
    balance_data["last_trade"] = datetime.now().isoformat()
    
    # Add position with source tracking
    position = {
        "id": f"pos_{int(datetime.now().timestamp())}_{len(positions)}",
        "market_id": market_id,
        "market": market_title,
        "side": side.upper(),
        "shares": shares,
        "entry_price": price,
        "cost_basis": amount,
        "opened_at": datetime.now().isoformat(),
        "strategy": "auto",
        "source": source,  # Track which signal source generated this
        "status": "open",
        "resolved_at": None,
        "outcome": None,
        "pnl": None
    }
    positions.append(position)
    
    # Log trade
    trade = {
        "type": "BUY",
        "mode": "PAPER",
        "market_id": market_id,
        "market": market_title,
        "side": side.upper(),
        "amount": amount,
        "shares": shares,
        "price": price,
        "reasoning": reasoning,
        "source": source,
        "timestamp": datetime.now().isoformat()
    }
    trades.append(trade)
    
    # Save
    save_json(BALANCE_FILE, balance_data)
    save_json(POSITIONS_FILE, positions)
    save_json(TRADES_FILE, trades)
    
    return {
        "success": True,
        "shares": shares,
        "price": price,
        "new_balance": balance_data["usdc"],
        "position_id": position["id"]
    }


def check_and_resolve_positions() -> dict:
    """Check all open positions for resolution and update outcomes"""
    ensure_storage()
    positions = load_json(POSITIONS_FILE)
    balance_data = load_json(BALANCE_FILE)
    
    resolved = []
    still_open = []
    
    for pos in positions:
        if pos.get("status") == "resolved":
            still_open.append(pos)  # Keep resolved positions in history
            continue
        
        market_id = pos.get("market_id", "")
        
        # Try to get current market status from Simmer
        try:
            result = simmer_request(f"/markets/{market_id}")
            if result and not result.get("error"):
                market_status = result.get("status", "active")
                outcome = result.get("outcome")  # YES, NO, or None
                
                if market_status in ["resolved", "closed"] and outcome:
                    # Market has resolved!
                    our_side = pos.get("side", "").upper()
                    won = (outcome.upper() == our_side)
                    
                    # Calculate P&L
                    cost_basis = pos.get("cost_basis", 0)
                    shares = pos.get("shares", 0)
                    
                    if won:
                        # We win: shares pay out at $1 each
                        payout = shares * 1.0
                        pnl = payout - cost_basis
                    else:
                        # We lose: shares worth $0
                        pnl = -cost_basis
                    
                    # Update position
                    pos["status"] = "resolved"
                    pos["resolved_at"] = datetime.now().isoformat()
                    pos["outcome"] = "win" if won else "loss"
                    pos["market_outcome"] = outcome
                    pos["pnl"] = round(pnl, 2)
                    
                    # Credit/debit balance
                    if won:
                        balance_data["usdc"] = balance_data.get("usdc", 0) + payout
                    
                    # Record outcome for Bayesian update
                    source = pos.get("source")
                    market_title = pos.get("market", "")
                    if source:
                        record_outcome(source, won, market_title)
                    
                    # Update conflict meta-learning
                    market_id = pos.get("market_id", "")
                    if market_id:
                        resolve_conflict_outcome(market_id, outcome.upper())
                    
                    resolved.append({
                        "position_id": pos.get("id"),
                        "market": market_title[:50],
                        "side": our_side,
                        "outcome": "WIN" if won else "LOSS",
                        "pnl": pnl,
                        "source": source
                    })
                else:
                    still_open.append(pos)
            else:
                still_open.append(pos)
        except:
            still_open.append(pos)
    
    # Save updated positions and balance
    # still_open already contains both open AND previously-resolved positions
    save_json(POSITIONS_FILE, still_open)
    save_json(BALANCE_FILE, balance_data)
    
    return {
        "resolved_count": len(resolved),
        "resolved_positions": resolved,
        "still_open": len([p for p in still_open if p.get("status") != "resolved"]),
        "new_balance": balance_data.get("usdc", 0),
        "checked_at": datetime.now().isoformat()
    }


def simulate_resolution(position_id: str, won: bool) -> dict:
    """Manually simulate a position resolution (for testing)"""
    ensure_storage()
    positions = load_json(POSITIONS_FILE)
    balance_data = load_json(BALANCE_FILE)
    
    for pos in positions:
        if pos.get("id") == position_id and pos.get("status") != "resolved":
            cost_basis = pos.get("cost_basis", 0)
            shares = pos.get("shares", 0)
            
            if won:
                payout = shares * 1.0
                pnl = payout - cost_basis
                balance_data["usdc"] = balance_data.get("usdc", 0) + payout
            else:
                pnl = -cost_basis
            
            pos["status"] = "resolved"
            pos["resolved_at"] = datetime.now().isoformat()
            pos["outcome"] = "win" if won else "loss"
            pos["pnl"] = round(pnl, 2)
            
            # Record outcome for Bayesian update
            source = pos.get("source")
            market_title = pos.get("market", "")
            if source:
                record_outcome(source, won, market_title)
            
            save_json(POSITIONS_FILE, positions)
            save_json(BALANCE_FILE, balance_data)
            
            return {
                "resolved": True,
                "position_id": position_id,
                "outcome": "win" if won else "loss",
                "pnl": pnl,
                "source": source,
                "new_win_rate": round(get_source_win_rate(source) * 100, 1) if source else None,
                "new_balance": balance_data.get("usdc", 0)
            }
    
    return {"error": "Position not found or already resolved"}


@app.get("/api/positions/check")
async def check_positions():
    """Check all positions for resolution and auto-update outcomes"""
    return check_and_resolve_positions()

@app.post("/api/positions/{position_id}/resolve")
async def manually_resolve_position(
    position_id: str,
    won: bool = Query(..., description="Did this position win?")
):
    """Manually resolve a position (for testing or manual markets)"""
    return simulate_resolution(position_id, won)

@app.post("/api/simmer/auto-trade")
async def simmer_auto_trade(
    max_trades: int = Query(3, ge=1, le=5, description="Max trades"),
    max_per_trade: float = Query(50.0, ge=10, le=200, description="Max $ per trade"),
    min_edge: float = Query(1.0, ge=0, description="Minimum edge score to trade"),
    mode: str = Query("dry_run", description="Mode: dry_run, paper, or live")
):
    """Auto-trade Simmer opportunities based on price divergence"""
    opps = analyze_simmer_markets()
    
    if opps.get("error"):
        return opps
    
    results = {
        "mode": mode,
        "trades_attempted": 0,
        "trades_executed": [],
        "trades_skipped": [],
        "total_deployed": 0,
        "balance_before": None,
        "balance_after": None
    }
    
    # Get starting balance for paper mode
    if mode == "paper":
        ensure_storage()
        balance_data = load_json(BALANCE_FILE)
        results["balance_before"] = balance_data.get("usdc", DEFAULT_BALANCE)
    
    for opp in opps.get("actionable", [])[:max_trades]:
        if opp["edge_score"] < min_edge:
            results["trades_skipped"].append({
                "market": opp["title"][:50],
                "reason": f"Edge {opp['edge_score']:.1f} below minimum {min_edge}"
            })
            continue
        
        results["trades_attempted"] += 1
        amount = min(max_per_trade, 50)  # Conservative sizing
        price = opp["simmer_price"]
        
        trade_record = {
            "market_id": opp["market_id"],
            "market": opp["title"][:60],
            "side": opp["recommendation"],
            "amount": amount,
            "price": price,
            "edge_score": opp["edge_score"],
            "divergence": opp["divergence"],
            "reasoning": opp["reasoning"],
            "timestamp": datetime.now().isoformat()
        }
        
        if mode == "dry_run":
            trade_record["status"] = "DRY_RUN"
            results["trades_executed"].append(trade_record)
            results["total_deployed"] += amount
            
        elif mode == "paper":
            # Execute paper trade
            paper_result = execute_paper_trade(
                opp["market_id"],
                opp["title"][:60],
                opp["recommendation"],
                amount,
                price,
                opp["reasoning"]
            )
            
            if paper_result.get("success"):
                trade_record["status"] = "PAPER_EXECUTED"
                trade_record["shares"] = paper_result["shares"]
                trade_record["new_balance"] = paper_result["new_balance"]
                results["trades_executed"].append(trade_record)
                results["total_deployed"] += amount
            else:
                trade_record["status"] = "FAILED"
                trade_record["error"] = paper_result.get("error")
                results["trades_skipped"].append(trade_record)
                
        elif mode == "live":
            # Execute real trade via Simmer
            trade_data = {
                "market_id": opp["market_id"],
                "side": opp["recommendation"].lower(),
                "amount": amount,
                "reasoning": f"Auto: {opp['reasoning']}",
                "source": "polyclawd:auto:simmer"
            }
            
            trade_result = simmer_request("/trade", method="POST", data=trade_data)
            
            if trade_result and not trade_result.get("error"):
                trade_record["status"] = "LIVE_EXECUTED"
                trade_record["result"] = trade_result
                results["trades_executed"].append(trade_record)
                results["total_deployed"] += amount
            else:
                trade_record["status"] = "FAILED"
                trade_record["error"] = trade_result.get("error") if trade_result else "API error"
                results["trades_skipped"].append(trade_record)
    
    # Get ending balance for paper mode
    if mode == "paper":
        balance_data = load_json(BALANCE_FILE)
        results["balance_after"] = balance_data.get("usdc", DEFAULT_BALANCE)
    
    results["total_deployed"] = round(results["total_deployed"], 2)
    
    # Log to auto trade history
    trades_log = load_auto_trade_log()
    for t in results["trades_executed"]:
        trades_log.append(t)
    save_auto_trade_log(trades_log)
    
    return results

# ============================================================================
# Bayesian Confidence Scoring System
# ============================================================================

SOURCE_OUTCOMES_FILE = Path(__file__).parent.parent / "data" / "source_outcomes.json"

def load_source_outcomes() -> dict:
    """Load historical win/loss data per signal source"""
    SOURCE_OUTCOMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SOURCE_OUTCOMES_FILE.exists():
        with open(SOURCE_OUTCOMES_FILE) as f:
            return json.load(f)
    # Initialize with prior: 50% win rate assumption
    return {
        "inverse_whale": {"wins": 1, "losses": 1, "total": 2},
        "smart_money": {"wins": 1, "losses": 1, "total": 2},
        "simmer_divergence": {"wins": 1, "losses": 1, "total": 2},
        "volume_spike": {"wins": 1, "losses": 1, "total": 2},
        "new_market": {"wins": 1, "losses": 1, "total": 2},
        "resolution_timing": {"wins": 1, "losses": 1, "total": 2},
        "price_alert": {"wins": 1, "losses": 1, "total": 2},
        "cross_arb": {"wins": 1, "losses": 1, "total": 2},
        "whale_new_position": {"wins": 1, "losses": 1, "total": 2},
        "momentum_confirm": {"wins": 1, "losses": 1, "total": 2},
        "high_divergence": {"wins": 1, "losses": 1, "total": 2},
        "resolution_edge": {"wins": 1, "losses": 1, "total": 2}
    }

def save_source_outcomes(outcomes: dict):
    """Save source outcomes"""
    with open(SOURCE_OUTCOMES_FILE, "w") as f:
        json.dump(outcomes, f, indent=2)

def record_outcome(source: str, won: bool, market_title: str = ""):
    """Record a trade outcome to update Bayesian priors"""
    outcomes = load_source_outcomes()
    if source not in outcomes:
        outcomes[source] = {"wins": 1, "losses": 1, "total": 2}
    
    outcomes[source]["total"] += 1
    if won:
        outcomes[source]["wins"] += 1
    else:
        outcomes[source]["losses"] += 1
    
    save_source_outcomes(outcomes)
    
    # Also track category performance
    if market_title:
        record_category_outcome(market_title, won)

def record_category_outcome(market_title: str, won: bool):
    """Track win rates by market category"""
    category_file = Path(__file__).parent.parent / "data" / "category_performance.json"
    try:
        cat_perf = json.loads(category_file.read_text()) if category_file.exists() else {}
    except:
        cat_perf = {}
    
    # Detect category
    title_lower = market_title.lower()
    category = "other"
    if any(kw in title_lower for kw in ["bitcoin", "btc", "ethereum", "eth", "crypto", "xrp", "solana"]):
        category = "crypto"
    elif any(kw in title_lower for kw in ["trump", "biden", "election", "congress", "senate", "president"]):
        category = "politics"
    elif any(kw in title_lower for kw in ["nba", "nfl", "mlb", "soccer", "football", "basketball"]):
        category = "sports"
    elif any(kw in title_lower for kw in ["netflix", "spotify", "movie", "album", "billboard"]):
        category = "entertainment"
    elif any(kw in title_lower for kw in ["temperature", "weather", "rain", "snow"]):
        category = "weather"
    elif any(kw in title_lower for kw in ["fed", "interest rate", "gdp", "inflation"]):
        category = "economics"
    
    if category not in cat_perf:
        cat_perf[category] = {"wins": 0, "losses": 0}
    
    if won:
        cat_perf[category]["wins"] += 1
    else:
        cat_perf[category]["losses"] += 1
    
    try:
        category_file.write_text(json.dumps(cat_perf, indent=2))
    except:
        pass

# ==================== CONFLICT META-LEARNING ====================

CONFLICT_HISTORY_FILE = Path(__file__).parent.parent / "data" / "conflict_history.json"

def load_conflict_history() -> dict:
    """Load conflict tracking history"""
    try:
        if CONFLICT_HISTORY_FILE.exists():
            return json.loads(CONFLICT_HISTORY_FILE.read_text())
    except:
        pass
    return {"conflicts": [], "source_vs_source": {}}

def save_conflict_history(history: dict):
    """Save conflict history"""
    try:
        CONFLICT_HISTORY_FILE.write_text(json.dumps(history, indent=2))
    except:
        pass

def track_conflict(market_id: str, signals: list, net_confidence: float):
    """Track a conflict for later learning when market resolves"""
    history = load_conflict_history()
    
    # Record the conflict
    conflict_record = {
        "market_id": market_id,
        "timestamp": datetime.now().isoformat(),
        "signals": [
            {"source": s["source"], "side": s["side"], "confidence": s["confidence"]}
            for s in signals
        ],
        "net_confidence": net_confidence,
        "traded_side": "YES" if net_confidence >= 30 else ("NO" if net_confidence <= -30 else None),
        "resolved": False,
        "outcome": None
    }
    
    # Keep only last 200 conflicts
    history["conflicts"] = history.get("conflicts", [])[-199:] + [conflict_record]
    save_conflict_history(history)

def resolve_conflict_outcome(market_id: str, winning_side: str):
    """When a conflicted market resolves, update source-vs-source stats"""
    history = load_conflict_history()
    
    for conflict in history.get("conflicts", []):
        if conflict.get("market_id") == market_id and not conflict.get("resolved"):
            conflict["resolved"] = True
            conflict["outcome"] = winning_side
            
            # Update source-vs-source stats
            yes_sources = [s["source"] for s in conflict["signals"] if s["side"] == "YES"]
            no_sources = [s["source"] for s in conflict["signals"] if s["side"] == "NO"]
            
            winners = yes_sources if winning_side == "YES" else no_sources
            losers = no_sources if winning_side == "YES" else yes_sources
            
            svs = history.get("source_vs_source", {})
            
            for winner in winners:
                for loser in losers:
                    key = f"{winner}_vs_{loser}"
                    if key not in svs:
                        svs[key] = {"wins": 0, "losses": 0}
                    svs[key]["wins"] += 1
                    
                    # Also track reverse
                    reverse_key = f"{loser}_vs_{winner}"
                    if reverse_key not in svs:
                        svs[reverse_key] = {"wins": 0, "losses": 0}
                    svs[reverse_key]["losses"] += 1
            
            history["source_vs_source"] = svs
            break
    
    save_conflict_history(history)

def get_source_conflict_edge(source_a: str, source_b: str) -> float:
    """Get how often source_a beats source_b in conflicts (0.5 = even)"""
    history = load_conflict_history()
    svs = history.get("source_vs_source", {})
    
    key = f"{source_a}_vs_{source_b}"
    if key in svs:
        data = svs[key]
        total = data["wins"] + data["losses"]
        if total >= 3:  # Need at least 3 data points
            return data["wins"] / total
    
    return 0.5  # Default: no edge

def get_source_win_rate(source: str) -> float:
    """Get Bayesian win rate for a source (with Laplace smoothing)"""
    outcomes = load_source_outcomes()
    if source not in outcomes:
        return 0.5  # Prior assumption
    
    data = outcomes[source]
    # Bayesian estimate with Beta prior (Laplace smoothing)
    return data["wins"] / data["total"] if data["total"] > 0 else 0.5

def normalize_confidence(raw_score: float, source: str) -> float:
    """Normalize raw signal scores to 0-100 scale based on source type"""
    # Define expected ranges per source
    ranges = {
        "inverse_whale": (0, 50),      # confidence_score typically 0-50
        "smart_money": (0, 40),        # flow/50, typically 0-40
        "simmer_divergence": (0, 50),  # edge_score typically 0-50
        "volume_spike": (0, 50),       # z_score * 10, typically 0-50
        "new_market": (0, 30),         # capped at 30
        "resolution_timing": (0, 30),  # uncertainty * 30
        "price_alert": (50, 50),       # fixed at 50
        "whale_new_position": (0, 40), # capped at 40
        "cross_arb": (0, 50)           # match_score * 50
    }
    
    min_val, max_val = ranges.get(source, (0, 50))
    if max_val == min_val:
        return min(100, max(0, raw_score * 2))  # Fixed sources
    
    # Normalize to 0-100
    normalized = ((raw_score - min_val) / (max_val - min_val)) * 100
    return min(100, max(0, normalized))

def calculate_bayesian_confidence(raw_score: float, source: str, market: str, side: str, all_signals: list) -> dict:
    """
    Calculate Bayesian-adjusted confidence score.
    
    Components:
    1. Base: Normalized raw score (0-100)
    2. Bayesian: Adjusted by source historical win rate
    3. Composite: Boosted if multiple sources agree
    """
    # 1. Normalize base score
    base_confidence = normalize_confidence(raw_score, source)
    
    # 2. Bayesian adjustment based on source win rate
    win_rate = get_source_win_rate(source)
    bayesian_multiplier = win_rate / 0.5  # 60% win rate = 1.2x, 40% = 0.8x
    bayesian_confidence = base_confidence * bayesian_multiplier
    
    # 3. Composite boost - check if other sources agree
    agreement_count = 0
    agreeing_sources = []
    market_key = market[:30].lower()
    
    for sig in all_signals:
        if sig.get("source") == source:
            continue  # Don't count self
        sig_market = sig.get("market", "")[:30].lower()
        sig_side = sig.get("side", "")
        
        # Check for agreement (same market direction)
        if market_key in sig_market or sig_market in market_key:
            if sig_side == side:
                agreement_count += 1
                agreeing_sources.append(sig.get("source"))
    
    # +20% per agreeing source, max 2x
    composite_multiplier = min(2.0, 1 + agreement_count * 0.2)
    
    final_confidence = bayesian_confidence * composite_multiplier
    
    return {
        "raw_score": raw_score,
        "base_confidence": round(base_confidence, 1),
        "win_rate": round(win_rate, 3),
        "bayesian_multiplier": round(bayesian_multiplier, 2),
        "bayesian_confidence": round(bayesian_confidence, 1),
        "agreement_count": agreement_count,
        "agreeing_sources": agreeing_sources,
        "composite_multiplier": round(composite_multiplier, 2),
        "final_confidence": round(min(100, final_confidence), 1)
    }

@app.get("/api/confidence/sources")
async def get_source_statistics():
    """Get win rate statistics for all signal sources"""
    outcomes = load_source_outcomes()
    stats = []
    
    for source, data in outcomes.items():
        win_rate = data["wins"] / data["total"] if data["total"] > 0 else 0.5
        stats.append({
            "source": source,
            "wins": data["wins"],
            "losses": data["losses"],
            "total": data["total"],
            "win_rate": round(win_rate * 100, 1),
            "bayesian_multiplier": round(win_rate / 0.5, 2)
        })
    
    stats.sort(key=lambda x: x["win_rate"], reverse=True)
    return {"sources": stats}

@app.post("/api/confidence/record")
async def record_trade_outcome(
    source: str = Query(..., description="Signal source"),
    won: bool = Query(..., description="Did the trade win?")
):
    """Record a trade outcome to update source reliability"""
    record_outcome(source, won)
    return {
        "recorded": True,
        "source": source,
        "outcome": "win" if won else "loss",
        "new_win_rate": round(get_source_win_rate(source) * 100, 1)
    }

@app.get("/api/conflicts/stats")
async def get_conflict_stats():
    """Get conflict resolution statistics and source-vs-source performance"""
    history = load_conflict_history()
    
    conflicts = history.get("conflicts", [])
    svs = history.get("source_vs_source", {})
    
    # Recent conflicts
    recent = conflicts[-10:] if conflicts else []
    
    # Source matchups sorted by sample size
    matchups = []
    for key, data in svs.items():
        total = data["wins"] + data["losses"]
        if total >= 1:
            matchups.append({
                "matchup": key,
                "wins": data["wins"],
                "losses": data["losses"],
                "total": total,
                "win_rate": round(data["wins"] / total * 100, 1)
            })
    
    matchups.sort(key=lambda x: x["total"], reverse=True)
    
    # Summary
    resolved_conflicts = [c for c in conflicts if c.get("resolved")]
    traded_conflicts = [c for c in conflicts if c.get("traded_side")]
    
    return {
        "total_conflicts": len(conflicts),
        "resolved_conflicts": len(resolved_conflicts),
        "traded_conflicts": len(traded_conflicts),
        "skipped_conflicts": len(conflicts) - len(traded_conflicts),
        "source_matchups": matchups[:20],
        "recent_conflicts": recent
    }


# ============================================================================
# Unified Signal Aggregator + Auto Paper Trader
# ============================================================================

def aggregate_all_signals() -> dict:
    """Gather and score all trading signals from EVERY source"""
    all_signals = []
    
    # 1. Inverse Whale Signals (Polymarket)
    try:
        inverse_data = get_inverse_whale_signals()
        for sig in inverse_data.get("signals", [])[:5]:
            all_signals.append({
                "source": "inverse_whale",
                "platform": "polymarket",
                "market": sig.get("market", ""),
                "side": sig.get("inverse_side", ""),
                "confidence": sig.get("confidence_score", 0),
                "value": sig.get("whale_value", 0),
                "reasoning": f"Fade {sig.get('whale_count', 0)} losing whale(s) with {sig.get('avg_whale_accuracy', 0):.0f}% accuracy",
                "price": sig.get("current_price", 0.5)
            })
    except: pass
    
    # 2. Smart Money Flow
    try:
        flow_data = get_smart_money_flow()
        for flow in flow_data.get("flows", [])[:5]:
            if flow.get("conviction") in ["STRONG", "MODERATE"] and flow.get("signal") != "NEUTRAL":
                all_signals.append({
                    "source": "smart_money",
                    "platform": "polymarket", 
                    "market": flow.get("market", ""),
                    "side": flow.get("signal", ""),
                    "confidence": abs(flow.get("net_flow_weighted", 0)) / 50,
                    "value": abs(flow.get("net_flow_weighted", 0)),
                    "reasoning": f"{flow.get('conviction')} flow: ${flow.get('net_flow_weighted', 0):+,.0f} weighted",
                    "price": flow.get("current_price", 0.5)
                })
    except: pass
    
    # 3. Simmer Price Divergence
    try:
        simmer_data = analyze_simmer_markets()
        for opp in simmer_data.get("actionable", [])[:10]:
            if opp.get("recommendation") not in ["HOLD", "SKIP"]:
                all_signals.append({
                    "source": "simmer_divergence",
                    "platform": "simmer",
                    "market": opp.get("title", ""),
                    "market_id": opp.get("market_id"),
                    "side": opp.get("recommendation", ""),
                    "confidence": opp.get("edge_score", 0),
                    "value": opp.get("divergence", 0) * 100,
                    "reasoning": opp.get("reasoning", ""),
                    "price": opp.get("simmer_price", 0.5),
                    "url": opp.get("url")
                })
    except: pass
    
    # 4. Resolution Timing (HIGH opportunity only)
    try:
        resolution_data = scan_resolution_timing(24)
        for mkt in resolution_data.get("markets", [])[:5]:
            if mkt.get("opportunity") == "HIGH":
                all_signals.append({
                    "source": "resolution_timing",
                    "platform": "polymarket",
                    "market": mkt.get("title", ""),
                    "side": "RESEARCH",
                    "confidence": mkt.get("uncertainty_score", 0) * 30,
                    "value": mkt.get("hours_until_resolution", 0),
                    "reasoning": f"HIGH uncertainty, resolves in {mkt.get('hours_until_resolution', 0):.1f}h",
                    "price": mkt.get("yes_price", 0.5)
                })
    except: pass
    
    # 5. Volume Spikes (unusual activity)
    try:
        volume_data = scan_volume_spikes(2.0, True)
        for spike in volume_data.get("spikes", [])[:5]:
            # Volume spike suggests information edge - bet direction based on price movement
            price = spike.get("yes_price", 0.5)
            # If price moved up with volume, momentum says YES; if down, NO
            side = "YES" if price > 0.5 else "NO"
            all_signals.append({
                "source": "volume_spike",
                "platform": "polymarket",
                "market": spike.get("title", ""),
                "market_id": spike.get("market_id"),
                "side": side,
                "confidence": spike.get("z_score", 0) * 10,  # 2σ = 20 confidence
                "value": spike.get("current_volume", 0),
                "reasoning": f"{spike.get('z_score', 0):.1f}σ volume spike ({spike.get('spike_ratio', 0):.1f}x normal)",
                "price": price
            })
    except: pass
    
    # 6. New Markets (early mover)
    try:
        new_markets = scan_new_markets()
        for mkt in new_markets.get("new_markets", [])[:3]:
            if mkt.get("liquidity", 0) > 5000:  # Only liquid new markets
                all_signals.append({
                    "source": "new_market",
                    "platform": "polymarket",
                    "market": mkt.get("title", ""),
                    "market_id": mkt.get("id"),
                    "side": "RESEARCH",  # New markets need analysis
                    "confidence": min(30, mkt.get("liquidity", 0) / 1000),
                    "value": mkt.get("liquidity", 0),
                    "reasoning": f"New market with ${mkt.get('liquidity', 0):,.0f} liquidity",
                    "price": mkt.get("yes_price", 0.5)
                })
    except: pass
    
    # 7. Price Alerts (triggered)
    try:
        alerts = check_price_alerts()
        for alert in alerts.get("triggered", [])[:5]:
            all_signals.append({
                "source": "price_alert",
                "platform": "polymarket",
                "market": alert.get("title", ""),
                "market_id": alert.get("market_id"),
                "side": "YES" if alert.get("direction") == "above" else "NO",
                "confidence": 50,  # Alerts are user-defined, high confidence
                "value": alert.get("target_price", 0),
                "reasoning": f"Price alert triggered: {alert.get('direction')} {alert.get('target_price')}",
                "price": alert.get("current_price", 0.5)
            })
    except: pass
    
    # 8. Cross-Platform Arbitrage (strict)
    try:
        arb_data = {"matches": []}  # Would call find_cross_arb but it's slow
        # For strict arb, any match is high confidence
        for match in arb_data.get("matches", [])[:3]:
            if match.get("match_score", 0) > 0.7:
                all_signals.append({
                    "source": "cross_arb",
                    "platform": "polymarket",
                    "market": match.get("polymarket", {}).get("title", ""),
                    "side": "ARB",  # Special handling needed
                    "confidence": match.get("match_score", 0) * 50,
                    "value": match.get("price_diff_pct", 0),
                    "reasoning": f"Cross-platform arb: {match.get('price_diff_pct', 0):.1f}% spread",
                    "price": match.get("polymarket", {}).get("yes", 0.5)
                })
    except: pass
    
    # 9. Whale Activity (new positions)
    try:
        whale_activity = scan_whale_activity()
        for sig in whale_activity.get("signals", [])[:3]:
            if sig.get("type") == "NEW_POSITION" and sig.get("value_usd", 0) > 1000:
                # Follow whale if they have good track record
                all_signals.append({
                    "source": "whale_new_position",
                    "platform": "polymarket",
                    "market": sig.get("market", ""),
                    "side": sig.get("outcome", "YES"),
                    "confidence": min(40, sig.get("value_usd", 0) / 100),
                    "value": sig.get("value_usd", 0),
                    "reasoning": f"{sig.get('whale', 'Whale')} opened ${sig.get('value_usd', 0):,.0f} {sig.get('outcome', '')} position",
                    "price": sig.get("entry_price", 0.5) or 0.5
                })
    except: pass
    
    # 10. Polymarket Momentum Confirmation for Simmer signals
    # If Simmer divergence exists AND Polymarket shows same direction momentum, boost
    try:
        simmer_signals = [s for s in all_signals if s.get("source") == "simmer_divergence"]
        poly_signals = [s for s in all_signals if s.get("platform") == "polymarket"]
        
        for sim in simmer_signals:
            sim_market = sim.get("market", "").lower()
            sim_side = sim.get("side", "").upper()
            
            # Look for confirming Polymarket signals
            for poly in poly_signals:
                poly_market = poly.get("market", "").lower()
                poly_side = poly.get("side", "").upper()
                
                # Check if same market category (crypto keywords)
                crypto_keywords = ["bitcoin", "btc", "ethereum", "eth", "xrp", "solana", "crypto"]
                sim_is_crypto = any(kw in sim_market for kw in crypto_keywords)
                poly_is_crypto = any(kw in poly_market for kw in crypto_keywords)
                
                if sim_is_crypto and poly_is_crypto and sim_side == poly_side:
                    all_signals.append({
                        "source": "momentum_confirm",
                        "platform": "simmer",
                        "market": sim.get("market", ""),
                        "market_id": sim.get("market_id"),
                        "side": sim_side,
                        "confidence": 15,  # Confirmation bonus
                        "value": 0,
                        "reasoning": f"Polymarket {poly.get('source', '')} confirms {sim_side} direction",
                        "price": sim.get("price", 0.5),
                        "url": sim.get("url")
                    })
                    break
    except: pass
    
    # 11. High Liquidity Boost - Simmer markets with good trading depth
    try:
        simmer_data = analyze_simmer_markets()
        for opp in simmer_data.get("all_markets", []):
            # Check for high-confidence divergence with good uncertainty (not settled)
            if opp.get("divergence", 0) > 0.15 and opp.get("uncertainty", 0) > 0.3:
                all_signals.append({
                    "source": "high_divergence",
                    "platform": "simmer",
                    "market": opp.get("title", ""),
                    "market_id": opp.get("market_id"),
                    "side": opp.get("recommendation", ""),
                    "confidence": opp.get("divergence", 0) * 80,  # 20% div = 16 conf
                    "value": opp.get("divergence", 0),
                    "reasoning": f"High divergence: {opp.get('divergence', 0)*100:.0f}% gap from Polymarket",
                    "price": opp.get("simmer_price", 0.5),
                    "url": opp.get("url")
                })
    except: pass
    
    # 12. Resolution Edge - Markets resolving soon with clear direction
    try:
        for opp in simmer_data.get("all_markets", []):
            resolves_at = opp.get("resolves_at")
            if resolves_at:
                from datetime import datetime
                try:
                    resolve_time = datetime.fromisoformat(resolves_at.replace("Z", "+00:00"))
                    hours_left = (resolve_time - datetime.now(resolve_time.tzinfo)).total_seconds() / 3600
                    
                    # If resolving within 24h and price is decisive (not 50/50)
                    price = opp.get("simmer_price", 0.5)
                    if 0 < hours_left < 24 and (price < 0.3 or price > 0.7):
                        # Strong conviction near resolution
                        side = "NO" if price > 0.7 else "YES"
                        all_signals.append({
                            "source": "resolution_edge",
                            "platform": "simmer",
                            "market": opp.get("title", ""),
                            "market_id": opp.get("market_id"),
                            "side": side,
                            "confidence": 20 + (24 - hours_left),  # More confidence closer to resolution
                            "value": hours_left,
                            "reasoning": f"Resolves in {hours_left:.1f}h at {price*100:.0f}¢ - likely {side}",
                            "price": price,
                            "url": opp.get("url")
                        })
                except: pass
    except: pass
    
    # Apply Bayesian confidence scoring to all signals
    for sig in all_signals:
        raw_conf = sig.get("confidence", 0)
        bayesian_result = calculate_bayesian_confidence(
            raw_conf,
            sig.get("source", "unknown"),
            sig.get("market", ""),
            sig.get("side", ""),
            all_signals
        )
        
        # Store both raw and Bayesian confidence
        sig["raw_confidence"] = raw_conf
        sig["confidence"] = bayesian_result["final_confidence"]
        sig["confidence_breakdown"] = {
            "base": bayesian_result["base_confidence"],
            "source_win_rate": bayesian_result["win_rate"],
            "bayesian_mult": bayesian_result["bayesian_multiplier"],
            "agreement": bayesian_result["agreement_count"],
            "composite_mult": bayesian_result["composite_multiplier"]
        }
    
    # Sort by Bayesian-adjusted confidence
    all_signals.sort(key=lambda x: x.get("confidence", 0), reverse=True)
    
    # Separate by actionability
    actionable = [s for s in all_signals if s.get("side") not in ["NEUTRAL", "RESEARCH", "ARB", ""]]
    research = [s for s in all_signals if s.get("side") in ["RESEARCH"]]
    arb = [s for s in all_signals if s.get("side") == "ARB"]
    
    # Count by source
    source_counts = {}
    for s in all_signals:
        src = s.get("source", "unknown")
        source_counts[src] = source_counts.get(src, 0) + 1
    
    return {
        "actionable_signals": actionable,
        "research_signals": research,
        "arb_signals": arb,
        "total_signals": len(all_signals),
        "actionable_count": len(actionable),
        "sources": source_counts,
        "scoring_method": "bayesian_composite",
        "generated_at": datetime.now().isoformat()
    }

def auto_paper_trade_signals(max_trades: int = 5, max_per_trade: float = 100, min_confidence: float = 10) -> dict:
    """Automatically paper trade based on aggregated signals"""
    ensure_storage()
    
    signals = aggregate_all_signals()
    actionable = signals.get("actionable_signals", [])
    
    balance_data = load_json(BALANCE_FILE)
    starting_balance = balance_data.get("usdc", DEFAULT_BALANCE)
    
    results = {
        "starting_balance": starting_balance,
        "signals_found": len(actionable),
        "trades_executed": [],
        "trades_skipped": [],
        "total_deployed": 0
    }
    
    trades_made = 0
    
    for sig in actionable:
        if trades_made >= max_trades:
            break
            
        if sig.get("confidence", 0) < min_confidence:
            results["trades_skipped"].append({
                "market": sig.get("market", "")[:40],
                "reason": f"Confidence {sig.get('confidence', 0):.1f} below minimum {min_confidence}"
            })
            continue
        
        # Skip non-Simmer for now (can't execute on Polymarket directly)
        # But still log as "SIGNAL_ONLY"
        if sig.get("platform") != "simmer":
            results["trades_skipped"].append({
                "market": sig.get("market", "")[:40],
                "source": sig.get("source"),
                "side": sig.get("side"),
                "confidence": sig.get("confidence"),
                "reason": "Polymarket signal - manual execution required",
                "action": f"Consider {sig.get('side')} on Polymarket"
            })
            continue
        
        # Calculate position size based on confidence
        confidence = sig.get("confidence", 0)
        size_pct = min(0.05, confidence / 500)  # Max 5% of bankroll per trade
        amount = min(max_per_trade, starting_balance * size_pct)
        
        if amount < 10:
            continue
        
        price = sig.get("price", 0.5)
        
        # Execute paper trade with source tracking
        trade_result = execute_paper_trade(
            sig.get("market_id", sig.get("market", "")[:20]),
            sig.get("market", "")[:60],
            sig.get("side", "YES"),
            amount,
            price,
            f"[{sig.get('source')}] {sig.get('reasoning', '')}",
            source=sig.get("source")  # Track source for Bayesian learning
        )
        
        if trade_result.get("success"):
            results["trades_executed"].append({
                "source": sig.get("source"),
                "market": sig.get("market", "")[:50],
                "side": sig.get("side"),
                "amount": amount,
                "price": price,
                "shares": trade_result.get("shares", 0),
                "confidence": confidence,
                "reasoning": sig.get("reasoning", "")
            })
            results["total_deployed"] += amount
            trades_made += 1
        else:
            results["trades_skipped"].append({
                "market": sig.get("market", "")[:40],
                "reason": trade_result.get("error", "Unknown error")
            })
    
    # Get final balance
    balance_data = load_json(BALANCE_FILE)
    results["ending_balance"] = balance_data.get("usdc", DEFAULT_BALANCE)
    results["total_deployed"] = round(results["total_deployed"], 2)
    
    return results

@app.get("/api/signals")
async def get_all_signals():
    """Get aggregated signals from all sources"""
    return aggregate_all_signals()

@app.post("/api/signals/auto-trade")
async def auto_trade_on_signals(
    max_trades: int = Query(5, ge=1, le=10, description="Max trades to execute"),
    max_per_trade: float = Query(100, ge=10, le=500, description="Max $ per trade"),
    min_confidence: float = Query(10, ge=0, le=100, description="Minimum confidence score")
):
    """Automatically paper trade based on all aggregated signals"""
    return auto_paper_trade_signals(max_trades, max_per_trade, min_confidence)


# ============================================================================
# Real-Time Trading Engine
# ============================================================================

import threading
import time

TRADING_ENGINE_STATE = Path(__file__).parent.parent / "data" / "engine_state.json"
TRADED_SIGNALS_FILE = Path(__file__).parent.parent / "data" / "traded_signals.json"

# Global engine state
_engine_running = False
_engine_thread = None

def load_engine_state() -> dict:
    """Load trading engine state"""
    TRADING_ENGINE_STATE.parent.mkdir(parents=True, exist_ok=True)
    if TRADING_ENGINE_STATE.exists():
        with open(TRADING_ENGINE_STATE) as f:
            return json.load(f)
    return {
        "enabled": False,
        "min_confidence": 35,
        "max_per_trade": 100,
        "max_daily_trades": 20,
        "max_position_pct": 0.05,
        "cooldown_minutes": 5,
        "trades_today": 0,
        "last_trade_time": None,
        "last_scan_time": None,
        "total_trades": 0
    }

def save_engine_state(state: dict):
    """Save engine state"""
    with open(TRADING_ENGINE_STATE, "w") as f:
        json.dump(state, f, indent=2)

def load_traded_signals() -> set:
    """Load signals we've already traded to avoid duplicates"""
    if TRADED_SIGNALS_FILE.exists():
        with open(TRADED_SIGNALS_FILE) as f:
            data = json.load(f)
            return set(data.get("traded", []))
    return set()

def save_traded_signal(signal_key: str):
    """Mark a signal as traded"""
    traded = load_traded_signals()
    traded.add(signal_key)
    # Keep only last 500 to prevent unbounded growth
    traded_list = list(traded)[-500:]
    with open(TRADED_SIGNALS_FILE, "w") as f:
        json.dump({"traded": traded_list, "updated": datetime.now().isoformat()}, f)

def generate_signal_key(sig: dict) -> str:
    """Generate unique key for a signal to prevent duplicate trades
    Uses market_id only - prevents trading both sides of same market"""
    market_id = sig.get('market_id') or sig.get('market', '')[:30]
    return f"{market_id}"

def engine_should_trade(state: dict) -> tuple:
    """Check if engine should execute a trade right now"""
    if not state.get("enabled", False):
        return False, "Engine disabled"
    
    # Check daily limit
    if state.get("trades_today", 0) >= state.get("max_daily_trades", 20):
        return False, "Daily trade limit reached"
    
    # Check cooldown
    last_trade = state.get("last_trade_time")
    if last_trade:
        last_trade_dt = datetime.fromisoformat(last_trade)
        cooldown_min = state.get("cooldown_minutes", 5)
        if (datetime.now() - last_trade_dt).total_seconds() < cooldown_min * 60:
            return False, f"Cooldown active ({cooldown_min}min)"
    
    return True, "Ready"

def engine_evaluate_and_trade() -> dict:
    """Core trading logic - evaluate signals and execute if conditions met"""
    state = load_engine_state()
    
    can_trade, reason = engine_should_trade(state)
    if not can_trade:
        return {"action": "skip", "reason": reason}
    
    # Get fresh signals
    signals_data = aggregate_all_signals()
    actionable = signals_data.get("actionable_signals", [])
    
    if not actionable:
        return {"action": "skip", "reason": "No actionable signals"}
    
    # Analyze conflicting signals with weighted net confidence
    from collections import defaultdict
    market_signals = defaultdict(list)
    for sig in actionable:
        market_id = sig.get('market_id') or sig.get('market', '')[:30]
        side = sig.get('side', '').upper()
        if side in ['YES', 'NO']:
            market_signals[market_id].append({
                'side': side,
                'confidence': sig.get('confidence', 0),
                'source': sig.get('source', 'unknown'),
                'signal': sig
            })
    
    # Calculate weighted net confidence for each market
    conflict_resolutions = {}
    CONFLICT_NET_THRESHOLD = 30  # Need this much edge to trade a conflict
    
    for market_id, signals in market_signals.items():
        sides = set(s['side'] for s in signals)
        if len(sides) > 1:  # This is a conflict
            # Weight by source win rate
            yes_total = 0
            no_total = 0
            for s in signals:
                win_rate = get_source_win_rate(s['source'])
                weight = win_rate / 0.5  # 60% win rate = 1.2x weight
                weighted_conf = s['confidence'] * weight
                if s['side'] == 'YES':
                    yes_total += weighted_conf
                else:
                    no_total += weighted_conf
            
            net = yes_total - no_total
            
            if abs(net) >= CONFLICT_NET_THRESHOLD:
                # Strong enough edge - trade the dominant side
                winning_side = 'YES' if net > 0 else 'NO'
                winning_signal = next((s['signal'] for s in signals if s['side'] == winning_side), None)
                conflict_resolutions[market_id] = {
                    'action': 'trade',
                    'side': winning_side,
                    'net': net,
                    'signal': winning_signal,
                    'yes_total': yes_total,
                    'no_total': no_total
                }
            else:
                # Too close - skip
                conflict_resolutions[market_id] = {
                    'action': 'skip',
                    'reason': f'Net {net:.1f} below threshold {CONFLICT_NET_THRESHOLD}',
                    'yes_total': yes_total,
                    'no_total': no_total
                }
            
            # Track conflict for meta-learning
            track_conflict(market_id, signals, net)
    
    # Get already traded signals
    traded = load_traded_signals()
    
    # Get paper balance
    ensure_storage()
    balance_data = load_json(BALANCE_FILE)
    current_balance = balance_data.get("usdc", DEFAULT_BALANCE)
    
    min_conf = state.get("min_confidence", 35)
    max_per_trade = state.get("max_per_trade", 100)
    max_position_pct = state.get("max_position_pct", 0.05)
    
    result = {"action": "evaluated", "signals_checked": len(actionable), "trades": []}
    
    for sig in actionable:
        # Check confidence threshold
        if sig.get("confidence", 0) < min_conf:
            continue
        
        # Check if already traded
        sig_key = generate_signal_key(sig)
        if sig_key in traded:
            continue
        
        # Handle conflicting signals with weighted net confidence
        if sig_key in conflict_resolutions:
            resolution = conflict_resolutions[sig_key]
            if resolution['action'] == 'skip':
                continue  # Too close, skip
            elif resolution['action'] == 'trade':
                # Use the winning signal from conflict resolution
                if sig != resolution['signal']:
                    continue  # This isn't the winning signal, skip it
        
        # Filter garbage markets (low-alpha noise)
        market_title = sig.get("market", "").lower()
        garbage_keywords = ["temperature", "weather"]
        if any(kw in market_title for kw in garbage_keywords):
            continue
        
        platform = sig.get("platform", "")
        
        # Get appropriate balance for platform
        if platform == "simmer":
            trade_balance = current_balance
        elif platform == "polymarket":
            ensure_poly_storage()
            poly_balance_data = load_json(POLY_BALANCE_FILE)
            trade_balance = poly_balance_data.get("usdc", DEFAULT_BALANCE)
        else:
            continue  # Unknown platform
        
        # Calculate position size
        amount = min(
            max_per_trade,
            trade_balance * max_position_pct,
            trade_balance * (sig.get("confidence", 0) / 500)
        )
        
        if amount < 10:
            continue
        
        price = sig.get("price", 0.5)
        
        # EXECUTE TRADE based on platform
        if platform == "simmer":
            trade_result = execute_paper_trade(
                sig.get("market_id", sig.get("market", "")[:20]),
                sig.get("market", "")[:60],
                sig.get("side", "YES"),
                amount,
                price,
                f"[ENGINE:{sig.get('source')}] {sig.get('reasoning', '')}",
                source=sig.get("source")
            )
        elif platform == "polymarket":
            trade_result = execute_poly_paper_trade(
                sig.get("market_id", sig.get("market", "")[:20]),
                sig.get("market", "")[:60],
                sig.get("side", "YES"),
                amount,
                price,
                f"[ENGINE:{sig.get('source')}] {sig.get('reasoning', '')}",
                source=sig.get("source")
            )
        else:
            continue
        
        if trade_result.get("success"):
            # Mark as traded
            save_traded_signal(sig_key)
            
            # Update state
            state["trades_today"] = state.get("trades_today", 0) + 1
            state["total_trades"] = state.get("total_trades", 0) + 1
            state["last_trade_time"] = datetime.now().isoformat()
            save_engine_state(state)
            
            # Update balance for next iteration (track by platform)
            if platform == "simmer":
                current_balance -= amount
            # Polymarket balance is tracked in its own file
            
            result["trades"].append({
                "platform": platform,
                "market": sig.get("market", "")[:50],
                "side": sig.get("side"),
                "amount": round(amount, 2),
                "confidence": sig.get("confidence"),
                "source": sig.get("source"),
                "executed_at": datetime.now().isoformat()
            })
            
            result["action"] = "traded"
            
            # Only one trade per evaluation to be conservative
            break
    
    state["last_scan_time"] = datetime.now().isoformat()
    save_engine_state(state)
    
    return result

def engine_loop():
    """Background loop that continuously monitors and trades"""
    global _engine_running
    
    scan_count = 0
    
    while _engine_running:
        try:
            state = load_engine_state()
            if state.get("enabled", False):
                result = engine_evaluate_and_trade()
                
                # Log significant events
                if result.get("action") == "traded":
                    # Could trigger webhook here
                    pass
                
                # Every 10 scans (~5 min), check for resolved positions
                scan_count += 1
                if scan_count % 10 == 0:
                    check_and_resolve_positions()  # Simmer positions
                    check_poly_positions()  # Polymarket positions
            
            # Sleep between scans (30 seconds)
            time.sleep(30)
            
        except Exception as e:
            # Log error but keep running
            time.sleep(60)  # Longer sleep on error

def start_engine():
    """Start the trading engine background thread"""
    global _engine_running, _engine_thread
    
    if _engine_running:
        return {"status": "already_running"}
    
    _engine_running = True
    _engine_thread = threading.Thread(target=engine_loop, daemon=True)
    _engine_thread.start()
    
    state = load_engine_state()
    state["enabled"] = True
    state["started_at"] = datetime.now().isoformat()
    save_engine_state(state)
    
    return {"status": "started", "state": state}

def stop_engine():
    """Stop the trading engine"""
    global _engine_running
    
    _engine_running = False
    
    state = load_engine_state()
    state["enabled"] = False
    state["stopped_at"] = datetime.now().isoformat()
    save_engine_state(state)
    
    return {"status": "stopped", "state": state}

@app.get("/api/engine/status")
async def get_engine_status():
    """Get trading engine status"""
    state = load_engine_state()
    
    # Get paper account info
    ensure_storage()
    balance_data = load_json(BALANCE_FILE)
    positions = load_json(POSITIONS_FILE)
    
    return {
        "running": _engine_running,
        "enabled": state.get("enabled", False),
        "config": {
            "min_confidence": state.get("min_confidence", 35),
            "max_per_trade": state.get("max_per_trade", 100),
            "max_daily_trades": state.get("max_daily_trades", 20),
            "cooldown_minutes": state.get("cooldown_minutes", 5)
        },
        "stats": {
            "trades_today": state.get("trades_today", 0),
            "total_trades": state.get("total_trades", 0),
            "last_trade": state.get("last_trade_time"),
            "last_scan": state.get("last_scan_time")
        },
        "paper_account": {
            "balance": balance_data.get("usdc", DEFAULT_BALANCE),
            "positions": len(positions)
        }
    }

@app.post("/api/engine/start")
async def api_start_engine():
    """Start the real-time trading engine"""
    return start_engine()

@app.post("/api/engine/stop")
async def api_stop_engine():
    """Stop the trading engine"""
    return stop_engine()

@app.post("/api/engine/config")
async def configure_engine(
    min_confidence: float = Query(None, ge=5, le=100),
    max_per_trade: float = Query(None, ge=10, le=1000),
    max_daily_trades: int = Query(None, ge=1, le=100),
    cooldown_minutes: int = Query(None, ge=1, le=60),
    max_position_pct: float = Query(None, ge=0.01, le=0.2)
):
    """Update trading engine configuration"""
    state = load_engine_state()
    
    if min_confidence is not None:
        state["min_confidence"] = min_confidence
    if max_per_trade is not None:
        state["max_per_trade"] = max_per_trade
    if max_daily_trades is not None:
        state["max_daily_trades"] = max_daily_trades
    if cooldown_minutes is not None:
        state["cooldown_minutes"] = cooldown_minutes
    if max_position_pct is not None:
        state["max_position_pct"] = max_position_pct
    
    save_engine_state(state)
    return {"updated": True, "config": state}

@app.post("/api/engine/trigger")
async def trigger_engine_evaluation():
    """Manually trigger one evaluation cycle"""
    return engine_evaluate_and_trade()

@app.post("/api/engine/reset-daily")
async def reset_daily_counter():
    """Reset daily trade counter"""
    state = load_engine_state()
    state["trades_today"] = 0
    save_engine_state(state)
    return {"reset": True, "trades_today": 0}


@app.get("/api/paper/status")
async def paper_trading_status():
    """Get paper trading account status"""
    ensure_storage()
    balance_data = load_json(BALANCE_FILE)
    positions = load_json(POSITIONS_FILE)
    trades = load_json(TRADES_FILE)
    
    # Calculate P&L for positions
    total_invested = sum(p.get("cost_basis", 0) for p in positions)
    
    return {
        "balance": balance_data.get("usdc", DEFAULT_BALANCE),
        "total_invested": round(total_invested, 2),
        "open_positions": len(positions),
        "total_trades": len(trades),
        "last_trade": balance_data.get("last_trade"),
        "positions": positions[-10:],  # Last 10 positions
        "recent_trades": trades[-10:]  # Last 10 trades
    }

@app.post("/api/paper/reset")
async def reset_paper_trading():
    """Reset paper trading account to $10,000"""
    ensure_storage()
    save_json(BALANCE_FILE, {"usdc": DEFAULT_BALANCE, "created_at": datetime.now().isoformat(), "reset_at": datetime.now().isoformat()})
    save_json(POSITIONS_FILE, [])
    save_json(TRADES_FILE, [])
    return {"success": True, "balance": DEFAULT_BALANCE, "message": "Paper trading reset to $10,000"}

# ==================== POLYMARKET PAPER TRADING ====================

def ensure_poly_storage():
    """Ensure Polymarket paper trading storage exists"""
    POLY_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    if not POLY_BALANCE_FILE.exists():
        save_json(POLY_BALANCE_FILE, {"usdc": DEFAULT_BALANCE, "created_at": datetime.now().isoformat()})
    if not POLY_POSITIONS_FILE.exists():
        save_json(POLY_POSITIONS_FILE, [])
    if not POLY_TRADES_FILE.exists():
        save_json(POLY_TRADES_FILE, [])

def execute_poly_paper_trade(market_id: str, market_title: str, side: str, amount: float, price: float, reasoning: str, source: str = None) -> dict:
    """Execute a paper trade on Polymarket (tracking only)"""
    ensure_poly_storage()
    
    balance_data = load_json(POLY_BALANCE_FILE)
    positions = load_json(POLY_POSITIONS_FILE)
    trades = load_json(POLY_TRADES_FILE)
    
    current_balance = balance_data.get("usdc", DEFAULT_BALANCE)
    
    if amount > current_balance:
        return {"success": False, "error": "Insufficient balance", "balance": current_balance}
    
    # Calculate shares
    shares = amount / price if price > 0 else 0
    
    # Deduct from balance
    balance_data["usdc"] = current_balance - amount
    
    # Create position
    position = {
        "id": f"poly_{int(datetime.now().timestamp())}_{len(positions)}",
        "market_id": market_id,
        "market": market_title,
        "side": side.upper(),
        "shares": shares,
        "entry_price": price,
        "cost_basis": amount,
        "opened_at": datetime.now().isoformat(),
        "strategy": "auto",
        "source": source,
        "status": "open",
        "resolved_at": None,
        "outcome": None,
        "pnl": None
    }
    positions.append(position)
    
    # Log trade
    trade = {
        "type": "BUY",
        "mode": "PAPER_POLY",
        "market_id": market_id,
        "market": market_title,
        "side": side.upper(),
        "amount": amount,
        "shares": shares,
        "price": price,
        "reasoning": reasoning,
        "source": source,
        "timestamp": datetime.now().isoformat()
    }
    trades.append(trade)
    
    # Save
    save_json(POLY_BALANCE_FILE, balance_data)
    save_json(POLY_POSITIONS_FILE, positions)
    save_json(POLY_TRADES_FILE, trades)
    
    return {
        "success": True,
        "platform": "polymarket",
        "shares": shares,
        "price": price,
        "new_balance": balance_data["usdc"],
        "position_id": position["id"]
    }

def check_poly_positions() -> dict:
    """Check Polymarket paper positions for resolution"""
    ensure_poly_storage()
    positions = load_json(POLY_POSITIONS_FILE)
    balance_data = load_json(POLY_BALANCE_FILE)
    
    resolved = []
    
    for pos in positions:
        if pos.get("status") == "resolved":
            continue
        
        market_id = pos.get("market_id", "")
        
        # Try to get market status from Polymarket
        try:
            result = gamma_request(f"/markets/{market_id}")
            if result and not result.get("error"):
                # Check if market is resolved
                closed = result.get("closed", False)
                outcome = result.get("outcome")
                
                if closed and outcome:
                    our_side = pos.get("side", "").upper()
                    won = (outcome.upper() == our_side)
                    
                    cost_basis = pos.get("cost_basis", 0)
                    shares = pos.get("shares", 0)
                    
                    if won:
                        payout = shares * 1.0
                        pnl = payout - cost_basis
                        balance_data["usdc"] = balance_data.get("usdc", 0) + payout
                    else:
                        pnl = -cost_basis
                    
                    pos["status"] = "resolved"
                    pos["resolved_at"] = datetime.now().isoformat()
                    pos["outcome"] = "win" if won else "loss"
                    pos["pnl"] = round(pnl, 2)
                    
                    # Record for Bayesian learning
                    source = pos.get("source")
                    market_title = pos.get("market", "")
                    if source:
                        record_outcome(source, won, market_title)
                    
                    resolved.append({
                        "position_id": pos.get("id"),
                        "market": market_title[:50],
                        "outcome": "WIN" if won else "LOSS",
                        "pnl": pnl
                    })
        except:
            pass
    
    save_json(POLY_POSITIONS_FILE, positions)
    save_json(POLY_BALANCE_FILE, balance_data)
    
    return {
        "platform": "polymarket",
        "resolved_count": len(resolved),
        "resolved_positions": resolved,
        "balance": balance_data.get("usdc", 0)
    }

@app.get("/api/paper/polymarket/status")
async def get_poly_paper_status():
    """Get Polymarket paper trading account status"""
    ensure_poly_storage()
    balance_data = load_json(POLY_BALANCE_FILE)
    positions = load_json(POLY_POSITIONS_FILE)
    trades = load_json(POLY_TRADES_FILE)
    
    open_positions = [p for p in positions if p.get("status") != "resolved"]
    resolved_positions = [p for p in positions if p.get("status") == "resolved"]
    wins = len([p for p in resolved_positions if p.get("outcome") == "win"])
    losses = len([p for p in resolved_positions if p.get("outcome") == "loss"])
    
    total_pnl = sum(p.get("pnl", 0) or 0 for p in resolved_positions)
    
    return {
        "platform": "polymarket",
        "balance": balance_data.get("usdc", DEFAULT_BALANCE),
        "total_invested": sum(p.get("cost_basis", 0) for p in open_positions),
        "open_positions": len(open_positions),
        "resolved": len(resolved_positions),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / max(1, wins + losses) * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "total_trades": len(trades),
        "positions": positions[-10:]
    }

@app.post("/api/paper/polymarket/reset")
async def reset_poly_paper_trading():
    """Reset Polymarket paper trading account"""
    ensure_poly_storage()
    save_json(POLY_BALANCE_FILE, {"usdc": DEFAULT_BALANCE, "created_at": datetime.now().isoformat()})
    save_json(POLY_POSITIONS_FILE, [])
    save_json(POLY_TRADES_FILE, [])
    return {"success": True, "platform": "polymarket", "balance": DEFAULT_BALANCE}

@app.get("/api/paper/polymarket/check")
async def check_poly_paper_positions():
    """Check and resolve Polymarket paper positions"""
    return check_poly_positions()

@app.get("/api/auto/inverse-whale")
async def auto_inverse_whale_preview():
    """Preview inverse whale signals (for POLYMARKET manual trading)"""
    signals = get_inverse_whale_signals()
    signals["platform"] = "polymarket"
    signals["note"] = "These signals are for MANUAL trading on Polymarket. Use inverse_side to fade losing whales."
    return signals

@app.get("/api/auto/smart-money")
async def auto_smart_money_preview():
    """Preview smart money auto-trades (dry run)"""
    return execute_auto_strategy("smart_money", max_trades=5, dry_run=True)

@app.post("/api/auto/execute")
async def execute_auto_trades(
    strategy: str = Query("inverse_whale", description="Strategy: inverse_whale or smart_money"),
    max_trades: int = Query(3, ge=1, le=10, description="Max trades to execute"),
    max_per_trade: float = Query(50.0, ge=10, le=500, description="Max $ per trade"),
    bankroll: float = Query(10000.0, ge=100, description="Total bankroll for sizing"),
    dry_run: bool = Query(True, description="If true, simulate only")
):
    """Execute automated trading strategy"""
    return execute_auto_strategy(strategy, max_trades, max_per_trade, bankroll, dry_run)

@app.get("/api/auto/history")
async def get_auto_trade_history(limit: int = Query(50, le=200)):
    """Get auto-trade execution history"""
    trades = load_auto_trade_log()
    return {
        "trades": trades[-limit:] if len(trades) > limit else trades,
        "total_trades": len(trades)
    }


# ============================================================================
# Webhook System - Push Alerts
# ============================================================================

WEBHOOKS_FILE = Path(__file__).parent.parent / "data" / "webhooks.json"

def load_webhooks() -> list:
    """Load configured webhooks"""
    WEBHOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if WEBHOOKS_FILE.exists():
        with open(WEBHOOKS_FILE) as f:
            return json.load(f)
    return []

def save_webhooks(webhooks: list):
    """Save webhooks"""
    with open(WEBHOOKS_FILE, "w") as f:
        json.dump(webhooks, f, indent=2)

def trigger_webhooks(event_type: str, payload: dict) -> list:
    """Send payload to all webhooks subscribed to event type"""
    webhooks = load_webhooks()
    results = []
    
    for wh in webhooks:
        if event_type not in wh.get("events", []):
            continue
        
        url = wh.get("url")
        if not url:
            continue
        
        try:
            data = json.dumps({
                "event": event_type,
                "timestamp": datetime.now().isoformat(),
                "payload": payload
            }).encode()
            
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json", "User-Agent": "Polyclawd/2.0"},
                method="POST"
            )
            
            with urllib.request.urlopen(req, timeout=5) as resp:
                results.append({"webhook_id": wh.get("id"), "status": resp.status, "success": True})
        except Exception as e:
            results.append({"webhook_id": wh.get("id"), "error": str(e), "success": False})
    
    return results

@app.get("/api/webhooks")
async def list_webhooks():
    """List all configured webhooks"""
    webhooks = load_webhooks()
    # Hide full URLs for security
    safe_webhooks = []
    for wh in webhooks:
        safe_wh = {**wh}
        if "url" in safe_wh:
            safe_wh["url"] = safe_wh["url"][:30] + "..." if len(safe_wh["url"]) > 30 else safe_wh["url"]
        safe_webhooks.append(safe_wh)
    
    return {"webhooks": safe_webhooks, "count": len(webhooks)}

@app.post("/api/webhooks")
async def create_webhook(
    url: str = Query(..., description="Webhook URL to POST to"),
    events: str = Query("all", description="Comma-separated events: whale_signal,new_market,volume_spike,price_alert,arb_found,all")
):
    """Create a new webhook subscription"""
    webhooks = load_webhooks()
    
    # Parse events
    event_list = ["whale_signal", "new_market", "volume_spike", "price_alert", "arb_found"] if events == "all" else [e.strip() for e in events.split(",")]
    
    new_webhook = {
        "id": f"wh_{len(webhooks)+1}_{int(datetime.now().timestamp())}",
        "url": url,
        "events": event_list,
        "created_at": datetime.now().isoformat(),
        "last_triggered": None
    }
    
    webhooks.append(new_webhook)
    save_webhooks(webhooks)
    
    return {"created": {**new_webhook, "url": url[:30] + "..."}, "total_webhooks": len(webhooks)}

@app.delete("/api/webhooks/{webhook_id}")
async def delete_webhook(webhook_id: str):
    """Delete a webhook"""
    webhooks = load_webhooks()
    original_count = len(webhooks)
    webhooks = [w for w in webhooks if w.get("id") != webhook_id]
    
    if len(webhooks) == original_count:
        raise HTTPException(status_code=404, detail="Webhook not found")
    
    save_webhooks(webhooks)
    return {"deleted": webhook_id, "remaining": len(webhooks)}

@app.post("/api/webhooks/test")
async def test_webhooks(event_type: str = Query("test", description="Event type to simulate")):
    """Test all webhooks with a sample payload"""
    test_payload = {
        "test": True,
        "message": "This is a test webhook from Polyclawd",
        "event_type": event_type
    }
    
    results = trigger_webhooks(event_type, test_payload)
    
    # Also trigger "all" subscribers
    if event_type != "all":
        results.extend(trigger_webhooks("all", test_payload))
    
    return {
        "tested": True,
        "event_type": event_type,
        "results": results
    }


# ============================================================================
# Kelly Criterion Position Sizing
# ============================================================================

def calculate_kelly(
    probability: float,  # Your estimated probability of YES (0-1)
    yes_price: float,    # Current market YES price (0-1)
    bankroll: float,     # Your total bankroll
    fractional: float = 0.25  # Kelly fraction (0.25 = quarter Kelly, safer)
) -> dict:
    """
    Calculate optimal position size using Kelly Criterion.
    
    Kelly formula: f* = (bp - q) / b
    Where:
      b = net odds (payout - 1)
      p = probability of winning
      q = probability of losing (1 - p)
    """
    if probability <= 0 or probability >= 1:
        return {"error": "Probability must be between 0 and 1"}
    if yes_price <= 0 or yes_price >= 1:
        return {"error": "Price must be between 0 and 1"}
    
    # Determine if we should bet YES or NO
    if probability > yes_price:
        # Bet YES: we think it's underpriced
        bet_side = "YES"
        price = yes_price
        win_prob = probability
    else:
        # Bet NO: we think YES is overpriced
        bet_side = "NO"
        price = 1 - yes_price  # NO price
        win_prob = 1 - probability
    
    # Calculate odds and Kelly
    # If we buy at price p, we get 1/p shares, payout is 1 if we win
    # Net profit = (1 - price) / price = (1/price) - 1
    payout_ratio = 1 / price  # Total return per dollar if win
    net_odds = payout_ratio - 1  # Net profit per dollar
    
    lose_prob = 1 - win_prob
    
    # Kelly formula
    kelly_fraction = (net_odds * win_prob - lose_prob) / net_odds
    
    # Apply fractional Kelly for safety
    adj_kelly = kelly_fraction * fractional
    
    # Cap at reasonable maximum
    adj_kelly = max(0, min(adj_kelly, 0.20))  # Cap at 20% of bankroll
    
    optimal_bet = bankroll * adj_kelly
    
    # Calculate expected value
    ev_per_dollar = (win_prob * net_odds) - lose_prob
    expected_profit = optimal_bet * ev_per_dollar
    
    # Calculate edge
    edge = probability - yes_price if bet_side == "YES" else (1 - probability) - (1 - yes_price)
    
    return {
        "recommendation": bet_side,
        "your_probability": probability,
        "market_price": yes_price,
        "edge": round(edge * 100, 2),  # Edge in percentage points
        "kelly_fraction": round(kelly_fraction * 100, 2),  # Full Kelly %
        "adjusted_kelly": round(adj_kelly * 100, 2),  # After fractional adjustment
        "optimal_bet": round(optimal_bet, 2),
        "expected_value_pct": round(ev_per_dollar * 100, 2),
        "expected_profit": round(expected_profit, 2),
        "risk_of_ruin": "Low" if adj_kelly < 0.05 else "Medium" if adj_kelly < 0.10 else "Higher",
        "note": f"Bet {bet_side} with ${optimal_bet:.2f} ({adj_kelly*100:.1f}% of bankroll)"
    }

@app.get("/api/kelly")
async def kelly_sizing(
    probability: float = Query(..., ge=0.01, le=0.99, description="Your estimated probability (0.01-0.99)"),
    yes_price: float = Query(..., ge=0.01, le=0.99, description="Current YES price (0.01-0.99)"),
    bankroll: float = Query(10000, ge=100, description="Your total bankroll"),
    kelly_fraction: float = Query(0.25, ge=0.1, le=1.0, description="Kelly fraction (0.25 = quarter Kelly)")
):
    """Calculate optimal bet size using Kelly Criterion"""
    return calculate_kelly(probability, yes_price, bankroll, kelly_fraction)

@app.get("/api/kelly/batch")
async def kelly_batch(
    bankroll: float = Query(10000, ge=100, description="Your total bankroll"),
    kelly_fraction: float = Query(0.25, ge=0.1, le=1.0, description="Kelly fraction")
):
    """Calculate Kelly sizing for whale copy trades (auto-estimate probability from whale conviction)"""
    # Get whale signals
    activity = scan_whale_activity()
    signals = activity.get("signals", [])
    
    recommendations = []
    total_allocation = 0
    
    for signal in signals[:10]:  # Top 10 signals
        if signal["type"] not in ["NEW_POSITION", "INCREASED_POSITION"]:
            continue
        
        # Estimate probability based on whale conviction
        # Whale putting $1k+ suggests ~60% confidence, scale up with size
        value = signal.get("value_usd", 0)
        base_prob = 0.55
        size_bonus = min(0.15, value / 50000)  # Up to +15% for $50k+ positions
        estimated_prob = min(0.80, base_prob + size_bonus)
        
        entry_price = signal.get("entry_price", 0.5)
        if not entry_price or entry_price <= 0:
            entry_price = 0.5
        
        kelly_result = calculate_kelly(estimated_prob, entry_price, bankroll, kelly_fraction)
        
        if kelly_result.get("optimal_bet", 0) > 0:
            recommendations.append({
                "market": signal.get("market", "Unknown")[:60],
                "whale": signal.get("whale"),
                "whale_position": value,
                "estimated_probability": estimated_prob,
                "market_price": entry_price,
                **kelly_result
            })
            total_allocation += kelly_result.get("optimal_bet", 0)
    
    return {
        "recommendations": recommendations,
        "count": len(recommendations),
        "total_allocation": round(total_allocation, 2),
        "remaining_bankroll": round(bankroll - total_allocation, 2),
        "bankroll": bankroll,
        "note": "Probability estimated from whale conviction level"
    }


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

@app.get("/api/whales/activity")
async def get_whale_activity():
    """Scan for whale position changes - copy trading signals"""
    return scan_whale_activity()

@app.get("/api/whales/signals")
async def get_whale_signals(min_value: float = Query(500, description="Minimum position value USD")):
    """Get only actionable whale signals (filtered)"""
    activity = scan_whale_activity()
    
    # Filter by minimum value
    filtered = [s for s in activity["signals"] 
                if s.get("value_usd", s.get("was_value_usd", 0)) >= min_value]
    
    # Only NEW and INCREASED positions (not closed)
    buy_signals = [s for s in filtered if s["type"] in ["NEW_POSITION", "INCREASED_POSITION"]]
    
    return {
        "buy_signals": buy_signals,
        "all_signals": filtered,
        "count": len(buy_signals),
        "scan_time": activity["scan_time"]
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
# Whale Activity Scanner - Copy Trading Signals
# ============================================================================

WHALE_STATE_FILE = Path(__file__).parent.parent / "data" / "whale_state.json"

def load_whale_state() -> dict:
    """Load previous whale positions state"""
    WHALE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if WHALE_STATE_FILE.exists():
        with open(WHALE_STATE_FILE) as f:
            return json.load(f)
    return {"positions": {}, "last_scan": None}

def save_whale_state(state: dict):
    """Save whale positions state"""
    WHALE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(WHALE_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def scan_whale_activity() -> dict:
    """Scan all whales for position changes - returns new/changed positions"""
    config = load_whale_config()
    old_state = load_whale_state()
    old_positions = old_state.get("positions", {})
    
    new_state = {"positions": {}, "last_scan": datetime.now().isoformat()}
    signals = []
    
    for whale in config.get("whales", []):
        address = whale["address"]
        name = whale.get("name", "Unknown")
        
        positions = fetch_polymarket_positions(address, limit=30)
        if isinstance(positions, dict) and positions.get("error"):
            continue
        
        # Build current position map
        current = {}
        for p in (positions if isinstance(positions, list) else []):
            market_id = p.get("asset", p.get("title", ""))[:100]
            if not market_id:
                continue
            current[market_id] = {
                "title": p.get("title", "Unknown"),
                "outcome": p.get("outcome"),
                "size": round(p.get("size", 0), 2),
                "value": round(p.get("currentValue", 0), 2),
                "entry_price": p.get("avgPrice"),
                "current_price": p.get("curPrice")
            }
        
        new_state["positions"][address] = current
        
        # Compare with old state
        old_whale_positions = old_positions.get(address, {})
        
        for market_id, pos in current.items():
            old_pos = old_whale_positions.get(market_id)
            
            if old_pos is None:
                # NEW POSITION
                if pos["value"] >= 100:  # Min $100 to signal
                    signals.append({
                        "type": "NEW_POSITION",
                        "whale": name,
                        "address": address,
                        "market": pos["title"],
                        "outcome": pos["outcome"],
                        "size": pos["size"],
                        "value_usd": pos["value"],
                        "entry_price": pos["entry_price"],
                        "timestamp": datetime.now().isoformat()
                    })
            elif pos["size"] > old_pos["size"] * 1.2:
                # INCREASED POSITION (>20% increase)
                if pos["value"] >= 100:
                    signals.append({
                        "type": "INCREASED_POSITION",
                        "whale": name,
                        "address": address,
                        "market": pos["title"],
                        "outcome": pos["outcome"],
                        "old_size": old_pos["size"],
                        "new_size": pos["size"],
                        "value_usd": pos["value"],
                        "timestamp": datetime.now().isoformat()
                    })
        
        # Check for closed positions
        for market_id, old_pos in old_whale_positions.items():
            if market_id not in current and old_pos["value"] >= 100:
                signals.append({
                    "type": "CLOSED_POSITION",
                    "whale": name,
                    "address": address,
                    "market": old_pos["title"],
                    "outcome": old_pos["outcome"],
                    "was_size": old_pos["size"],
                    "was_value_usd": old_pos["value"],
                    "timestamp": datetime.now().isoformat()
                })
    
    # Save new state
    save_whale_state(new_state)
    
    # Sort signals by value
    signals.sort(key=lambda x: x.get("value_usd", x.get("was_value_usd", 0)), reverse=True)
    
    return {
        "signals": signals,
        "whales_scanned": len(config.get("whales", [])),
        "scan_time": new_state["last_scan"],
        "previous_scan": old_state.get("last_scan")
    }

# Whale activity endpoints moved before {address} routes - see above


# ============================================================================
# New Market Scanner - Early Mover Detection
# ============================================================================

SEEN_MARKETS_FILE = Path(__file__).parent.parent / "data" / "seen_markets.json"

def load_seen_markets() -> dict:
    """Load previously seen market IDs"""
    SEEN_MARKETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SEEN_MARKETS_FILE.exists():
        with open(SEEN_MARKETS_FILE) as f:
            return json.load(f)
    return {"markets": {}, "last_scan": None}

def save_seen_markets(state: dict):
    """Save seen markets state"""
    with open(SEEN_MARKETS_FILE, "w") as f:
        json.dump(state, f, indent=2)

def fetch_recent_markets(limit: int = 100) -> list:
    """Fetch recent/active markets from Polymarket"""
    try:
        # Use events endpoint sorted by created
        url = f"{GAMMA_API}/events?limit={limit}&active=true&closed=false"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            events = json.loads(resp.read().decode())
        
        markets = []
        for event in events:
            event_id = event.get("id", "")
            event_title = event.get("title", "")
            
            for market in event.get("markets", []):
                markets.append({
                    "id": market.get("id", ""),
                    "event_id": event_id,
                    "title": event_title,
                    "question": market.get("question", event_title),
                    "volume": market.get("volume", 0),
                    "liquidity": market.get("liquidity", 0),
                    "yes_price": market.get("outcomePrices", [0.5])[0] if market.get("outcomePrices") else 0.5,
                    "end_date": market.get("endDate"),
                    "url": f"https://polymarket.com/event/{event.get('slug', event_id)}"
                })
        
        return markets
    except Exception as e:
        return [{"error": str(e)}]

def scan_new_markets() -> dict:
    """Scan for newly created markets"""
    old_state = load_seen_markets()
    old_markets = set(old_state.get("markets", {}).keys())
    
    current_markets = fetch_recent_markets(limit=150)
    if current_markets and isinstance(current_markets[0], dict) and current_markets[0].get("error"):
        return {"error": current_markets[0]["error"], "new_markets": []}
    
    new_state = {"markets": {}, "last_scan": datetime.now().isoformat()}
    new_markets = []
    
    for m in current_markets:
        market_id = m.get("id", "")
        if not market_id:
            continue
        
        new_state["markets"][market_id] = {
            "title": m.get("question", m.get("title", "")),
            "first_seen": old_state.get("markets", {}).get(market_id, {}).get("first_seen", datetime.now().isoformat()),
            "volume": m.get("volume", 0)
        }
        
        # Check if truly new
        if market_id not in old_markets:
            new_markets.append({
                "id": market_id,
                "title": m.get("question", m.get("title", "")),
                "volume": m.get("volume", 0),
                "liquidity": m.get("liquidity", 0),
                "yes_price": m.get("yes_price"),
                "end_date": m.get("end_date"),
                "url": m.get("url"),
                "discovered_at": datetime.now().isoformat()
            })
    
    save_seen_markets(new_state)
    
    # Sort by liquidity (higher liquidity = more tradeable)
    new_markets.sort(key=lambda x: x.get("liquidity", 0), reverse=True)
    
    return {
        "new_markets": new_markets,
        "count": len(new_markets),
        "total_tracked": len(new_state["markets"]),
        "scan_time": new_state["last_scan"],
        "previous_scan": old_state.get("last_scan")
    }

# New market endpoints moved before {market_id} route - see above


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

# Entity extraction patterns for smart matching
PERSON_PATTERNS = [
    r"\b([A-Z][a-z]+ [A-Z][a-z]+)\b",  # First Last
    r"\b([A-Z][a-z]+ [A-Z]\. [A-Z][a-z]+)\b",  # First M. Last
]

YEAR_PATTERN = r"\b(20\d{2})\b"

EVENT_CATEGORIES = {
    "us_president": ["president", "presidential", "white house", "oval office"],
    "us_election": ["election", "primary", "nominee", "nomination", "electoral"],
    "fed": ["fed", "federal reserve", "interest rate", "fomc", "powell"],
    "crypto": ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol"],
    "sports_nba": ["nba", "basketball", "lakers", "celtics", "warriors", "finals"],
    "sports_nfl": ["nfl", "super bowl", "football", "touchdown", "quarterback"],
    "sports_soccer": ["fifa", "world cup", "soccer", "premier league", "champions league"],
    "tech": ["apple", "google", "microsoft", "tesla", "nvidia", "ai", "openai"],
    "geopolitics": ["war", "invasion", "military", "nato", "russia", "ukraine", "china", "taiwan"],
    "iran": ["iran", "khamenei", "tehran", "ayatollah"],
    "uk": ["uk", "britain", "british", "parliament", "prime minister"],
    "eu": ["eu", "european union", "brexit", "eurozone"],
}

COUNTRY_KEYWORDS = {
    "us": ["us", "usa", "united states", "america", "american"],
    "uk": ["uk", "britain", "british", "england"],
    "germany": ["germany", "german", "deutschland"],
    "france": ["france", "french"],
    "russia": ["russia", "russian", "moscow"],
    "china": ["china", "chinese", "beijing"],
    "iran": ["iran", "iranian", "tehran"],
    "brazil": ["brazil", "brazilian"],
    "mexico": ["mexico", "mexican"],
    "portugal": ["portugal", "portuguese"],
    "romania": ["romania", "romanian"],
}

def extract_entities(text: str) -> dict:
    """Extract key entities from market title"""
    entities = {
        "persons": [],
        "years": [],
        "categories": [],
        "countries": [],
        "core_subject": "",
        "outcome_type": "",
        "keywords": set()
    }
    
    text_lower = text.lower()
    
    # Extract years
    entities["years"] = re.findall(YEAR_PATTERN, text)
    
    # Extract persons (proper nouns)
    for pattern in PERSON_PATTERNS:
        matches = re.findall(pattern, text)
        entities["persons"].extend(matches)
    
    # Detect categories
    for cat, keywords in EVENT_CATEGORIES.items():
        if any(kw in text_lower for kw in keywords):
            entities["categories"].append(cat)
    
    # Detect countries
    for country, keywords in COUNTRY_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            entities["countries"].append(country)
    
    # Extract outcome type
    if any(w in text_lower for w in ["win", "wins", "winner", "elected"]):
        entities["outcome_type"] = "win"
    elif any(w in text_lower for w in ["above", "over", "exceed", "reach", "hit"]):
        entities["outcome_type"] = "above"
    elif any(w in text_lower for w in ["below", "under", "drop"]):
        entities["outcome_type"] = "below"
    elif any(w in text_lower for w in ["leave", "out", "resign", "removed", "fired"]):
        entities["outcome_type"] = "exit"
    elif any(w in text_lower for w in ["next", "succeed", "replace", "become"]):
        entities["outcome_type"] = "succession"
    
    # Extract core subject (first proper noun or key entity)
    if entities["persons"]:
        entities["core_subject"] = entities["persons"][0].lower()
    
    # Extract significant words (4+ chars, not common words)
    stopwords = {"will", "the", "and", "for", "this", "that", "with", "from", "have", 
                 "been", "before", "after", "next", "win", "won", "lose", "lost",
                 "first", "round", "election", "presidential", "president"}
    words = re.findall(r"\b[a-z]{4,}\b", text_lower)
    entities["keywords"] = set(w for w in words if w not in stopwords)
    
    return entities

def calculate_match_score(title1: str, title2: str) -> float:
    """Calculate STRICT similarity - only match truly equivalent markets (same bet)"""
    e1 = extract_entities(title1)
    e2 = extract_entities(title2)
    
    norm1 = normalize_text(title1)
    norm2 = normalize_text(title2)
    
    # First check: raw text similarity must be high
    text_sim = SequenceMatcher(None, norm1, norm2).ratio()
    if text_sim < 0.5:
        return 0.0  # Not similar enough to even consider
    
    # HARD REQUIREMENTS (all must pass for true arb)
    
    # 1. Must share at least one category
    cat_overlap = set(e1["categories"]) & set(e2["categories"])
    if not cat_overlap:
        return 0.0
    
    # 2. Countries must match (if present)
    if e1["countries"] and e2["countries"]:
        if not (set(e1["countries"]) & set(e2["countries"])):
            return 0.0
    
    # 3. Years must match exactly (if present)
    if e1["years"] and e2["years"]:
        if not (set(e1["years"]) & set(e2["years"])):
            return 0.0
    
    # 4. Must share a person/entity name
    persons1 = set(p.lower() for p in e1["persons"])
    persons2 = set(p.lower() for p in e2["persons"])
    
    # Check full name match
    full_name_match = bool(persons1 & persons2)
    
    # Check last name match
    lastname_match = False
    if not full_name_match and persons1 and persons2:
        lastnames1 = set(p.split()[-1] for p in persons1)
        lastnames2 = set(p.split()[-1] for p in persons2)
        lastname_match = bool(lastnames1 & lastnames2)
    
    # 5. Keyword overlap must be very high
    kw1, kw2 = e1["keywords"], e2["keywords"]
    kw_jaccard = len(kw1 & kw2) / len(kw1 | kw2) if (kw1 and kw2) else 0
    
    # 6. Outcome type must match
    outcome_match = (e1["outcome_type"] == e2["outcome_type"]) if (e1["outcome_type"] and e2["outcome_type"]) else True
    
    # TRUE ARB DETECTION
    # For true arb, we need: same person + same outcome + high text similarity
    
    if not (full_name_match or lastname_match):
        # No person match - check if it's a non-person market with very high text sim
        if text_sim < 0.7 or kw_jaccard < 0.6:
            return 0.0
    
    if not outcome_match:
        return 0.0  # Different outcomes = not the same bet
    
    # SCORING (strict)
    score = 0.0
    
    # Text similarity is king for true arb
    if text_sim >= 0.85:
        score = 0.9
    elif text_sim >= 0.75:
        score = 0.8
    elif text_sim >= 0.65:
        score = 0.7
    elif text_sim >= 0.55:
        score = 0.6
    else:
        score = 0.5
    
    # Bonuses
    if full_name_match:
        score += 0.05
    if kw_jaccard >= 0.7:
        score += 0.05
    
    return min(score, 1.0)

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

def calculate_related_score(title1: str, title2: str) -> float:
    """Calculate RELATED market similarity - for correlated pairs trading"""
    e1 = extract_entities(title1)
    e2 = extract_entities(title2)
    
    score = 0.0
    
    # Category match (must share at least one category)
    cat_overlap = set(e1["categories"]) & set(e2["categories"])
    if not cat_overlap:
        return 0.0
    score += 0.3
    
    # Country match
    country_overlap = set(e1["countries"]) & set(e2["countries"])
    if country_overlap:
        score += 0.2
    elif e1["countries"] and e2["countries"]:
        return 0.0  # Different countries = no match
    
    # Year match (allow 1 year diff for related)
    if e1["years"] and e2["years"]:
        years1 = set(int(y) for y in e1["years"])
        years2 = set(int(y) for y in e2["years"])
        if years1 & years2:
            score += 0.15
        elif any(abs(y1 - y2) <= 1 for y1 in years1 for y2 in years2):
            score += 0.05
        else:
            return 0.0  # Years too far apart
    
    # Person name match
    persons1 = set(p.lower() for p in e1["persons"])
    persons2 = set(p.lower() for p in e2["persons"])
    if persons1 & persons2:
        score += 0.25
    else:
        # Check last names
        lastnames1 = set(p.split()[-1] for p in persons1 if p)
        lastnames2 = set(p.split()[-1] for p in persons2 if p)
        if lastnames1 & lastnames2:
            score += 0.15
    
    # Keyword overlap
    kw1, kw2 = e1["keywords"], e2["keywords"]
    if kw1 and kw2:
        jaccard = len(kw1 & kw2) / len(kw1 | kw2)
        score += jaccard * 0.2
    
    # Text similarity
    norm1 = normalize_text(title1)
    norm2 = normalize_text(title2)
    text_sim = SequenceMatcher(None, norm1, norm2).ratio()
    score += text_sim * 0.1
    
    return min(score, 1.0)


@app.get("/api/cross-arb")
async def cross_platform_arb_scan(
    min_spread: float = Query(3.0, ge=0.5, le=20, description="Minimum spread % to report"),
    min_match: float = Query(0.6, ge=0.3, le=1.0, description="Minimum title match score")
):
    """Scan for cross-platform arbitrage between Polymarket and Kalshi"""
    return find_cross_arb_opportunities(min_spread, min_match)

@app.get("/api/cross-arb/strict")
async def strict_arb_matches(
    min_match: float = Query(0.6, ge=0.5, le=1.0)
):
    """TRUE ARB ONLY - Find identical markets with different prices"""
    poly_markets = fetch_polymarket_for_arb()
    kalshi_markets = fetch_kalshi_markets()
    
    matches = []
    for poly in poly_markets[:150]:
        for kalshi in kalshi_markets:
            score = calculate_match_score(poly["title"], kalshi["title"])
            if score >= min_match:
                price_diff = abs(poly["yes_price"] - kalshi["yes_price"]) * 100
                # Calculate potential profit
                cost = poly["yes_price"] + kalshi["no_price"]
                spread = (1 - cost) * 100 if cost < 1 else 0
                matches.append({
                    "polymarket": {"title": poly["title"][:80], "yes": poly["yes_price"], "no": poly["no_price"], "url": poly["url"]},
                    "kalshi": {"title": kalshi["title"][:80], "yes": kalshi["yes_price"], "no": kalshi["no_price"], "url": kalshi["url"]},
                    "match_score": round(score, 2),
                    "price_diff_pct": round(price_diff, 1),
                    "arb_spread_pct": round(spread, 2)
                })
    
    matches.sort(key=lambda x: x["match_score"], reverse=True)
    return {
        "mode": "strict",
        "description": "True arbitrage - identical markets only",
        "count": len(matches), 
        "matches": matches[:20], 
        "scanned_at": datetime.now().isoformat()
    }

@app.get("/api/cross-arb/related")
async def related_market_matches(
    min_match: float = Query(0.5, ge=0.3, le=1.0)
):
    """RELATED MARKETS - For pairs trading on correlated events"""
    poly_markets = fetch_polymarket_for_arb()
    kalshi_markets = fetch_kalshi_markets()
    
    matches = []
    for poly in poly_markets[:150]:
        for kalshi in kalshi_markets:
            score = calculate_related_score(poly["title"], kalshi["title"])
            if score >= min_match:
                price_diff = abs(poly["yes_price"] - kalshi["yes_price"]) * 100
                matches.append({
                    "polymarket": {"title": poly["title"][:80], "yes": poly["yes_price"], "url": poly["url"]},
                    "kalshi": {"title": kalshi["title"][:80], "yes": kalshi["yes_price"], "url": kalshi["url"]},
                    "match_score": round(score, 2),
                    "price_diff_pct": round(price_diff, 1),
                    "correlation": "likely" if score > 0.7 else "possible"
                })
    
    matches.sort(key=lambda x: x["match_score"], reverse=True)
    return {
        "mode": "related",
        "description": "Correlated markets for pairs trading",
        "count": len(matches), 
        "matches": matches[:30], 
        "scanned_at": datetime.now().isoformat()
    }

@app.get("/api/cross-arb/matches")
async def get_cross_platform_matches(
    min_match: float = Query(0.5, ge=0.3, le=1.0)
):
    """DEPRECATED - Use /api/cross-arb/strict or /api/cross-arb/related instead"""
    return await related_market_matches(min_match)


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
