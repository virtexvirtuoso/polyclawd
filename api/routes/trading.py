"""Paper trading, Simmer integration, and Paper Polymarket endpoints.

This router consolidates all trading-related endpoints:
- /balance, /positions, /trades - Paper trading state
- /trade, /reset - Paper trading actions
- /positions/check, /positions/{id}/resolve - Position management
- /simmer/* - Simmer SDK live trading
- /paper/* - Paper Polymarket trading
"""
import json
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from api.deps import get_settings, get_storage_service
from api.middleware import verify_api_key
from api.models import TradeRequest, TradeResponse
from api.services.storage import StorageService

router = APIRouter()
logger = logging.getLogger(__name__)

# Rate limiter (will use app.state.limiter at runtime)
limiter = Limiter(key_func=get_remote_address)

# Constants
GAMMA_API = "https://gamma-api.polymarket.com"
SIMMER_API = "https://api.simmer.markets/api/sdk"
SIMMER_MAX_TRADE = 100.0

# Simmer credentials (loaded lazily)
_simmer_api_key: Optional[str] = None


def _load_simmer_credentials() -> bool:
    """Load Simmer API key from credentials file."""
    global _simmer_api_key
    creds_path = Path.home() / ".config" / "simmer" / "credentials.json"
    if creds_path.exists():
        try:
            with open(creds_path) as f:
                creds = json.load(f)
                _simmer_api_key = creds.get("api_key")
                return bool(_simmer_api_key)
        except Exception as e:
            logger.error(f"Failed to load Simmer credentials: {e}")
    return False


def _simmer_request(endpoint: str, method: str = "GET", data: dict = None) -> Optional[dict]:
    """Make request to Simmer API."""
    global _simmer_api_key
    if not _simmer_api_key:
        if not _load_simmer_credentials():
            return None

    url = f"{SIMMER_API}{endpoint}"
    headers = {
        "Authorization": f"Bearer {_simmer_api_key}",
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
        logger.warning(f"Simmer HTTP {e.code}: {error_body[:200]}")
        return {"error": f"Simmer API error: {e.code}"}
    except Exception as e:
        logger.error(f"Simmer request failed: {e}")
        return {"error": str(e)}


def _api_get(endpoint: str, params: dict = None) -> list:
    """GET request to Polymarket Gamma API."""
    url = f"{GAMMA_API}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Polymarket API error: {str(e)}")


def _get_market(market_id: str) -> Optional[dict]:
    """Get market by ID, slug, or conditionId."""
    for param in ["id", "slug", "conditionId"]:
        try:
            markets = _api_get("/markets", {param: market_id})
            if markets:
                return markets[0]
        except Exception:
            pass
    return None


def _get_market_prices(market: dict) -> tuple:
    """Extract YES/NO prices from market."""
    try:
        prices = json.loads(market.get("outcomePrices", "[0, 0]"))
        return float(prices[0]) if prices[0] else 0.0, float(prices[1]) if prices[1] else 0.0
    except Exception:
        return 0.0, 0.0


# ============================================================================
# Paper Trading Core Endpoints
# ============================================================================

@router.get("/balance")
async def get_balance():
    """Get paper trading account balance with P&L."""
    storage = get_storage_service()
    settings = get_settings()

    balance_data = await storage.load("balance.json", {"usdc": settings.DEFAULT_BALANCE})
    positions = await storage.load("positions.json", [])

    usdc = balance_data.get("usdc", settings.DEFAULT_BALANCE)
    position_value = 0.0

    for pos in positions:
        market = _get_market(pos.get("market_id", ""))
        if market:
            yes_price, no_price = _get_market_prices(market)
            current_price = yes_price if pos.get("side") == "YES" else no_price
            position_value += pos.get("shares", 0) * current_price

    total = usdc + position_value
    return {
        "cash": usdc,
        "positions_value": round(position_value, 2),
        "total": round(total, 2),
        "pnl": round(total - settings.DEFAULT_BALANCE, 2),
        "pnl_percent": round(((total - settings.DEFAULT_BALANCE) / settings.DEFAULT_BALANCE) * 100, 2)
    }


@router.get("/positions")
async def get_positions():
    """Get all open paper trading positions with current prices."""
    storage = get_storage_service()
    positions = await storage.load("positions.json", [])

    result = []
    for pos in positions:
        market = _get_market(pos.get("market_id", ""))
        if not market:
            continue

        yes_price, no_price = _get_market_prices(market)
        current_price = yes_price if pos.get("side") == "YES" else no_price
        shares = pos.get("shares", 0)
        entry_price = pos.get("entry_price", 0)
        cost_basis = pos.get("cost_basis", shares * entry_price)
        current_value = shares * current_price
        pnl = current_value - cost_basis

        result.append({
            "market_id": pos.get("market_id"),
            "market_question": pos.get("market_question", market.get("question", "Unknown")),
            "side": pos.get("side"),
            "shares": shares,
            "entry_price": entry_price,
            "current_price": current_price,
            "cost_basis": cost_basis,
            "current_value": current_value,
            "pnl": round(pnl, 2),
            "pnl_percent": round((pnl / cost_basis * 100) if cost_basis > 0 else 0, 2),
            "opened_at": pos.get("opened_at")
        })

    return result


@router.get("/trades")
async def get_trades(limit: int = Query(default=20, ge=1, le=100)):
    """Get recent trade history."""
    storage = get_storage_service()
    trades_data = await storage.load("trades.json", [])

    # Handle both list and dict formats
    if isinstance(trades_data, dict):
        trades = trades_data.get("trades", [])
    else:
        trades = trades_data

    return list(reversed(trades[-limit:]))


@router.post("/trade", response_model=TradeResponse, dependencies=[Depends(verify_api_key)])
@limiter.limit("5/minute")
async def execute_trade(request: Request, trade_request: TradeRequest):
    """Execute a paper trade - requires API key authentication."""
    storage = get_storage_service()
    settings = get_settings()

    side = trade_request.side.upper()
    amount = float(trade_request.amount)

    # Validate market
    market = _get_market(trade_request.market_id)
    if not market:
        raise HTTPException(status_code=404, detail=f"Market not found: {trade_request.market_id}")
    if market.get("closed"):
        raise HTTPException(status_code=400, detail="Market is closed")

    # Get price
    yes_price, no_price = _get_market_prices(market)
    price = yes_price if side == "YES" else no_price
    if price <= 0:
        raise HTTPException(status_code=400, detail=f"Invalid price for {side}")

    # Check balance
    balance_data = await storage.load("balance.json", {"usdc": settings.DEFAULT_BALANCE})
    usdc = balance_data.get("usdc", settings.DEFAULT_BALANCE)

    if amount > usdc:
        raise HTTPException(status_code=400, detail=f"Insufficient balance: ${usdc:.2f}")

    # Calculate shares and execute
    shares = amount / price
    balance_data["usdc"] = usdc - amount
    balance_data["last_trade"] = datetime.now().isoformat()
    await storage.save("balance.json", balance_data)

    # Update positions
    positions = await storage.load("positions.json", [])
    existing = next(
        (p for p in positions if p.get("market_id") == market["id"] and p.get("side") == side),
        None
    )

    trade_id = f"paper-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    if existing:
        old_cost = existing["shares"] * existing["entry_price"]
        total_shares = existing["shares"] + shares
        existing["shares"] = total_shares
        existing["entry_price"] = (old_cost + amount) / total_shares
        existing["cost_basis"] = old_cost + amount
    else:
        positions.append({
            "id": trade_id,
            "market_id": market["id"],
            "market_question": market.get("question", "Unknown")[:80],
            "side": side,
            "shares": shares,
            "entry_price": price,
            "cost_basis": amount,
            "opened_at": datetime.now().isoformat()
        })

    await storage.save("positions.json", positions)

    # Log trade
    trade_record = {
        "id": trade_id,
        "type": "BUY",
        "market_id": market["id"],
        "market_question": market.get("question", "Unknown")[:80],
        "side": side,
        "amount": amount,
        "shares": shares,
        "price": price,
        "reasoning": trade_request.reasoning,
        "timestamp": datetime.now().isoformat()
    }
    await storage.append_to_list("trades.json", trade_record)

    logger.info(f"Trade executed: {trade_request.market_id} {side} ${amount:.2f} @ {price:.4f}")

    return TradeResponse(
        success=True,
        trade_id=trade_id,
        message=f"Bought {shares:.4f} shares of {side} @ ${price:.4f}",
        balance=balance_data["usdc"]
    )


@router.post("/reset", dependencies=[Depends(verify_api_key)])
@limiter.limit("2/minute")
async def reset_paper_trading(request: Request):
    """Reset paper trading account - requires API key."""
    storage = get_storage_service()
    settings = get_settings()

    await storage.save("balance.json", {
        "usdc": settings.DEFAULT_BALANCE,
        "created_at": datetime.now().isoformat()
    })
    await storage.save("positions.json", [])

    # Log reset to trades
    trades = await storage.load("trades.json", [])
    if isinstance(trades, dict):
        trades = trades.get("trades", [])
    trades.append({"type": "RESET", "timestamp": datetime.now().isoformat()})
    await storage.save("trades.json", trades)

    logger.info(f"Paper trading reset to ${settings.DEFAULT_BALANCE:,.2f}")

    return {"success": True, "message": f"Reset to ${settings.DEFAULT_BALANCE:,.2f}"}


@router.get("/positions/check")
async def check_positions():
    """Check all positions for resolution status."""
    storage = get_storage_service()
    positions = await storage.load("positions.json", [])

    results = {
        "checked": len(positions),
        "resolved": 0,
        "pending": 0,
        "positions": []
    }

    for pos in positions:
        market = _get_market(pos.get("market_id", ""))
        status = "unknown"

        if market:
            if market.get("closed"):
                status = "resolved"
                results["resolved"] += 1
            else:
                status = "active"
                results["pending"] += 1
        else:
            status = "market_not_found"

        results["positions"].append({
            "market_id": pos.get("market_id"),
            "side": pos.get("side"),
            "status": status
        })

    return results


@router.post("/positions/{position_id}/resolve", dependencies=[Depends(verify_api_key)])
async def resolve_position(
    position_id: str,
    won: bool = Query(..., description="Did this position win?")
):
    """Manually resolve a position."""
    storage = get_storage_service()
    settings = get_settings()

    positions = await storage.load("positions.json", [])
    balance_data = await storage.load("balance.json", {"usdc": settings.DEFAULT_BALANCE})

    # Find position
    pos_index = next(
        (i for i, p in enumerate(positions) if p.get("id") == position_id or p.get("market_id") == position_id),
        None
    )

    if pos_index is None:
        raise HTTPException(status_code=404, detail=f"Position not found: {position_id}")

    pos = positions[pos_index]
    payout = 0.0

    if won:
        payout = pos.get("shares", 0) * 1.0  # Winning positions pay $1 per share
        balance_data["usdc"] = balance_data.get("usdc", 0) + payout

    # Remove position
    positions.pop(pos_index)

    await storage.save("positions.json", positions)
    await storage.save("balance.json", balance_data)

    # Log resolution
    resolution_record = {
        "type": "RESOLUTION",
        "position_id": position_id,
        "market_id": pos.get("market_id"),
        "won": won,
        "payout": payout,
        "timestamp": datetime.now().isoformat()
    }
    await storage.append_to_list("trades.json", resolution_record)

    logger.info(f"Position resolved: {position_id} won={won} payout=${payout:.2f}")

    return {
        "success": True,
        "position_id": position_id,
        "won": won,
        "payout": payout,
        "new_balance": balance_data["usdc"]
    }


# ============================================================================
# Simmer SDK Live Trading Endpoints
# ============================================================================

@router.get("/simmer/status")
async def get_simmer_status():
    """Get Simmer agent status and balance."""
    result = _simmer_request("/agents/me")
    if not result or result.get("error"):
        raise HTTPException(status_code=502, detail=result.get("error", "Simmer API unavailable"))
    return result


@router.get("/simmer/portfolio")
async def get_simmer_portfolio():
    """Get Simmer portfolio summary."""
    result = _simmer_request("/portfolio")
    if not result or result.get("error"):
        raise HTTPException(status_code=502, detail=result.get("error", "Simmer API unavailable"))
    return result


@router.get("/simmer/positions")
async def get_simmer_positions():
    """Get current positions from Simmer."""
    result = _simmer_request("/positions")
    if not result or result.get("error"):
        raise HTTPException(status_code=502, detail=result.get("error", "Simmer API unavailable"))
    return result


@router.get("/simmer/trades")
async def get_simmer_trades(limit: int = Query(default=20, ge=1, le=100)):
    """Get trade history from Simmer."""
    result = _simmer_request(f"/trades?limit={limit}")
    if not result or result.get("error"):
        raise HTTPException(status_code=502, detail=result.get("error", "Simmer API unavailable"))
    return result


@router.post("/simmer/trade", dependencies=[Depends(verify_api_key)])
@limiter.limit("5/minute")
async def execute_simmer_trade(
    request: Request,
    market_id: str = Query(..., description="Market ID"),
    side: str = Query(..., description="YES or NO"),
    amount: float = Query(..., gt=0, le=SIMMER_MAX_TRADE, description="Amount in USD"),
    reasoning: str = Query("", description="Trade reasoning")
):
    """Execute a LIVE trade via Simmer SDK - requires API key."""
    side_lower = side.lower()
    if side_lower not in ("yes", "no"):
        raise HTTPException(status_code=400, detail="Side must be YES or NO")

    data = {
        "market_id": market_id,
        "side": side_lower,
        "amount": amount,
        "reasoning": reasoning,
        "source": "polyclawd:api"
    }

    result = _simmer_request("/trade", method="POST", data=data)
    if not result:
        raise HTTPException(status_code=502, detail="Simmer API unavailable")

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("error"))

    # Log to local trades file
    storage = get_storage_service()
    trade_record = {
        "type": "SIMMER_LIVE",
        "market_id": market_id,
        "side": side.upper(),
        "amount": amount,
        "reasoning": reasoning,
        "result": result,
        "timestamp": datetime.now().isoformat()
    }
    await storage.append_to_list("trades.json", trade_record)

    logger.info(f"Simmer trade executed: {market_id} {side} ${amount:.2f}")

    return result


@router.get("/simmer/context/{market_id}")
async def get_simmer_context(market_id: str):
    """Get pre-trade context for a market from Simmer."""
    result = _simmer_request(f"/context/{market_id}")
    if not result or result.get("error"):
        raise HTTPException(status_code=502, detail=result.get("error", "Simmer API unavailable"))
    return result


# ============================================================================
# Paper Polymarket Trading Endpoints
# ============================================================================

def _get_poly_storage() -> StorageService:
    """Get StorageService for Polymarket paper trading."""
    settings = get_settings()
    from api.services.storage import StorageService
    return StorageService(settings.POLY_STORAGE_DIR)


@router.get("/paper/status")
async def get_paper_status():
    """Get paper trading status."""
    storage = get_storage_service()
    settings = get_settings()

    balance_data = await storage.load("balance.json", {"usdc": settings.DEFAULT_BALANCE})
    positions = await storage.load("positions.json", [])
    trades = await storage.load("trades.json", [])

    if isinstance(trades, dict):
        trades = trades.get("trades", [])

    total_invested = sum(p.get("cost_basis", 0) for p in positions)

    return {
        "balance": balance_data.get("usdc", settings.DEFAULT_BALANCE),
        "total_invested": round(total_invested, 2),
        "open_positions": len(positions),
        "total_trades": len(trades),
        "last_trade": balance_data.get("last_trade"),
        "positions": positions[-10:],
        "recent_trades": trades[-10:]
    }


@router.get("/paper/positions")
async def get_paper_positions():
    """Get paper trading positions."""
    storage = get_storage_service()
    positions = await storage.load("positions.json", [])
    return {"positions": positions, "count": len(positions)}


@router.post("/paper/trade", dependencies=[Depends(verify_api_key)])
@limiter.limit("5/minute")
async def execute_paper_trade_manual(
    request: Request,
    market_id: str = Query(..., description="Market ID or slug"),
    market_title: str = Query(..., description="Market title"),
    side: str = Query(..., description="YES or NO"),
    amount: float = Query(..., ge=5, description="Amount in USD"),
    price: float = Query(..., ge=0.01, le=0.99, description="Entry price"),
    reasoning: str = Query("Manual trade", description="Trade reasoning")
):
    """Execute a manual paper trade with explicit parameters."""
    storage = get_storage_service()
    settings = get_settings()

    side = side.upper()
    if side not in ("YES", "NO"):
        raise HTTPException(status_code=400, detail="Side must be YES or NO")

    # Check balance
    balance_data = await storage.load("balance.json", {"usdc": settings.DEFAULT_BALANCE})
    usdc = balance_data.get("usdc", settings.DEFAULT_BALANCE)

    if amount > usdc:
        raise HTTPException(status_code=400, detail=f"Insufficient balance: ${usdc:.2f}")

    # Calculate shares
    shares = amount / price
    trade_id = f"paper-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    # Update balance
    balance_data["usdc"] = usdc - amount
    balance_data["last_trade"] = datetime.now().isoformat()
    await storage.save("balance.json", balance_data)

    # Add position
    positions = await storage.load("positions.json", [])
    positions.append({
        "id": trade_id,
        "market_id": market_id,
        "market_question": market_title[:80],
        "side": side,
        "shares": shares,
        "entry_price": price,
        "cost_basis": amount,
        "opened_at": datetime.now().isoformat(),
        "source": "manual"
    })
    await storage.save("positions.json", positions)

    # Log trade
    trade_record = {
        "id": trade_id,
        "type": "BUY",
        "market_id": market_id,
        "market_question": market_title[:80],
        "side": side,
        "amount": amount,
        "shares": shares,
        "price": price,
        "reasoning": reasoning,
        "source": "manual",
        "timestamp": datetime.now().isoformat()
    }
    await storage.append_to_list("trades.json", trade_record)

    logger.info(f"Manual paper trade: {market_id} {side} ${amount:.2f} @ {price:.4f}")

    return {
        "success": True,
        "trade_id": trade_id,
        "market_id": market_id,
        "side": side,
        "amount": amount,
        "shares": round(shares, 4),
        "price": price,
        "new_balance": balance_data["usdc"]
    }
