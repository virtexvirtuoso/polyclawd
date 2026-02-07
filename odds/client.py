"""
The Odds API Client
Real-time odds from 40+ sportsbooks
"""

import os
import httpx
from typing import Optional
from datetime import datetime


class OddsAPIClient:
    """Client for The Odds API"""
    
    BASE_URL = "https://api.the-odds-api.com/v4"
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or self._get_api_key()
        self.requests_remaining = None
        self.requests_used = None
    
    def _get_api_key(self) -> str:
        """Get API key from keychain or environment"""
        # Try environment first
        key = os.getenv("ODDS_API_KEY")
        if key:
            return key
        
        # Try macOS keychain
        try:
            import subprocess
            result = subprocess.run(
                ["security", "find-generic-password", "-a", "the-odds-api", "-s", "the-odds-api", "-w"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except:
            pass
        
        raise ValueError("No API key found. Set ODDS_API_KEY or store in keychain.")
    
    def _update_quota(self, headers: dict):
        """Track API quota from response headers"""
        self.requests_remaining = headers.get("x-requests-remaining")
        self.requests_used = headers.get("x-requests-used")
    
    async def get_sports(self) -> list[dict]:
        """Get list of available sports"""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.BASE_URL}/sports",
                params={"apiKey": self.api_key}
            )
            self._update_quota(response.headers)
            return response.json()
    
    async def get_odds(
        self,
        sport: str,
        regions: str = "us",
        markets: str = "h2h",
        odds_format: str = "american",
        bookmakers: Optional[str] = None
    ) -> list[dict]:
        """
        Get odds for a sport.
        
        Args:
            sport: Sport key (e.g., 'americanfootball_nfl')
            regions: Comma-separated regions (us, uk, eu, au)
            markets: Comma-separated markets (h2h, spreads, totals)
            odds_format: american or decimal
            bookmakers: Optional comma-separated bookmaker keys
        """
        params = {
            "apiKey": self.api_key,
            "regions": regions,
            "markets": markets,
            "oddsFormat": odds_format
        }
        if bookmakers:
            params["bookmakers"] = bookmakers
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.BASE_URL}/sports/{sport}/odds",
                params=params
            )
            self._update_quota(response.headers)
            return response.json()
    
    async def get_scores(self, sport: str, days_from: int = 1) -> list[dict]:
        """Get scores/results for a sport"""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.BASE_URL}/sports/{sport}/scores",
                params={
                    "apiKey": self.api_key,
                    "daysFrom": days_from
                }
            )
            self._update_quota(response.headers)
            return response.json()
    
    def get_quota(self) -> dict:
        """Get current API quota status"""
        return {
            "requests_remaining": self.requests_remaining,
            "requests_used": self.requests_used
        }


# Convenience functions
def american_to_prob(odds: int) -> float:
    """Convert American odds to implied probability"""
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)


def remove_vig(prob_a: float, prob_b: float) -> tuple[float, float]:
    """Remove vig to get true probabilities (two-way markets)"""
    total = prob_a + prob_b
    return prob_a / total, prob_b / total


def devig_multiway(probs: list[float]) -> list[float]:
    """
    Remove vig from multi-way market (futures, outrights)
    
    Args:
        probs: List of implied probabilities (with vig baked in)
    
    Returns:
        List of true probabilities (summing to 1.0)
    """
    total = sum(probs)
    if total == 0:
        return probs
    return [p / total for p in probs]


def calculate_consensus(bookmakers: list[dict], team: str) -> dict:
    """Calculate consensus odds across bookmakers"""
    odds_list = []
    
    for book in bookmakers:
        for market in book.get("markets", []):
            if market["key"] == "h2h":
                for outcome in market["outcomes"]:
                    if outcome["name"] == team:
                        odds_list.append({
                            "book": book["key"],
                            "odds": outcome["price"]
                        })
    
    if not odds_list:
        return {"error": "No odds found"}
    
    avg_odds = sum(o["odds"] for o in odds_list) / len(odds_list)
    best_odds = max(odds_list, key=lambda x: x["odds"])
    worst_odds = min(odds_list, key=lambda x: x["odds"])
    
    avg_prob = american_to_prob(int(avg_odds))
    
    return {
        "team": team,
        "average_odds": round(avg_odds),
        "average_prob": round(avg_prob, 4),
        "best_odds": best_odds,
        "worst_odds": worst_odds,
        "books_count": len(odds_list)
    }
