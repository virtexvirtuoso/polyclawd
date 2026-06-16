"""Insider Detection API endpoints."""

from fastapi import APIRouter, Query
from loguru import logger

router = APIRouter(prefix="/api/insider", tags=["Insider Detection"])


@router.get("/recent")
async def get_recent_insiders(
    limit: int = Query(default=20, ge=1, le=100),
    min_score: float = Query(default=40, ge=0, le=100)
):
    """Get recent insider detections."""
    try:
        import sys
        from pathlib import Path
        signals_path = str(Path(__file__).parent.parent.parent / "signals")
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from insider_detector import get_recent_insiders
        return {"detections": get_recent_insiders(limit, min_score)}
    except Exception as e:
        logger.exception("Insider recent failed: {}", e)
        return {"error": str(e), "detections": []}


@router.get("/leaderboard")
async def get_insider_leaderboard(
    min_bets: int = Query(default=2, ge=1)
):
    """Get top insider wallets by score and win rate."""
    try:
        import sys
        from pathlib import Path
        signals_path = str(Path(__file__).parent.parent.parent / "signals")
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from insider_detector import get_insider_leaderboard
        return {"wallets": get_insider_leaderboard(min_bets)}
    except Exception as e:
        logger.exception("Insider leaderboard failed: {}", e)
        return {"error": str(e), "wallets": []}


@router.post("/scan")
async def trigger_insider_scan():
    """Manually trigger an insider scan."""
    try:
        import sys
        from pathlib import Path
        signals_path = str(Path(__file__).parent.parent.parent / "signals")
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from insider_detector import scan_for_insiders, send_alerts
        results = scan_for_insiders()
        if results:
            send_alerts(results)
        return {
            "detections": len(results),
            "results": [
                {
                    "wallet": r["wallet"][:12] + "...",
                    "score": r["scores"]["insider_score"],
                    "size": r["size_usd"],
                    "title": r["title"][:80],
                }
                for r in results
            ]
        }
    except Exception as e:
        logger.exception("Insider scan failed: {}", e)
        return {"error": str(e), "detections": 0}
