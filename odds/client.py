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


# Extended Odds API Client with more markets
class ExtendedOddsClient(OddsAPIClient):
    """Extended client with spreads, totals, outrights, and player props"""
    
    async def get_all_markets(
        self,
        sport: str,
        regions: str = "us",
        odds_format: str = "american"
    ) -> list[dict]:
        """
        Get all market types for a sport: h2h, spreads, totals.
        
        More efficient than separate calls.
        """
        return await self.get_odds(
            sport=sport,
            regions=regions,
            markets="h2h,spreads,totals",
            odds_format=odds_format
        )
    
    async def get_outrights(
        self,
        sport: str,
        regions: str = "us",
        odds_format: str = "american"
    ) -> list[dict]:
        """
        Get futures/outrights for a sport (e.g., Super Bowl winner).
        """
        params = {
            "apiKey": self.api_key,
            "regions": regions,
            "markets": "outrights",
            "oddsFormat": odds_format
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.BASE_URL}/sports/{sport}/odds",
                params=params
            )
            self._update_quota(response.headers)
            return response.json()
    
    async def get_event_odds(
        self,
        sport: str,
        event_id: str,
        markets: str = "player_pass_tds,player_rush_yds,player_receptions",
        regions: str = "us"
    ) -> dict:
        """
        Get odds for a specific event including player props.
        
        Args:
            sport: Sport key
            event_id: Event ID from get_events or get_odds
            markets: Comma-separated market keys (player props, alternates, etc.)
            regions: Bookmaker regions
        
        Available player prop markets:
        - player_pass_tds, player_pass_yds, player_pass_completions
        - player_rush_yds, player_rush_attempts
        - player_receptions, player_reception_yds
        - player_anytime_td (anytime touchdown scorer)
        - player_first_td (first touchdown scorer)
        - player_points, player_rebounds, player_assists (NBA)
        - player_threes (NBA 3-pointers)
        """
        params = {
            "apiKey": self.api_key,
            "regions": regions,
            "markets": markets,
            "oddsFormat": "american"
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.BASE_URL}/sports/{sport}/events/{event_id}/odds",
                params=params
            )
            self._update_quota(response.headers)
            return response.json()
    
    async def get_events(self, sport: str) -> list[dict]:
        """Get list of events for a sport (free endpoint)"""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.BASE_URL}/sports/{sport}/events",
                params={"apiKey": self.api_key}
            )
            self._update_quota(response.headers)
            return response.json()
    
    async def get_player_props(
        self,
        sport: str,
        event_id: str,
        prop_type: str = "all"
    ) -> list[dict]:
        """
        Get player props for an event.
        
        Args:
            sport: Sport key (americanfootball_nfl, basketball_nba)
            event_id: Event ID
            prop_type: "passing", "rushing", "receiving", "scoring", "all"
        
        Returns:
            List of player props with odds from multiple books
        """
        prop_markets = {
            "passing": "player_pass_tds,player_pass_yds,player_pass_completions,player_interceptions",
            "rushing": "player_rush_yds,player_rush_attempts,player_longest_rush",
            "receiving": "player_receptions,player_reception_yds,player_longest_reception",
            "scoring": "player_anytime_td,player_first_td,player_last_td",
            "nba_points": "player_points,player_points_rebounds_assists",
            "nba_other": "player_rebounds,player_assists,player_threes,player_steals,player_blocks",
            "all": "player_pass_tds,player_rush_yds,player_receptions,player_anytime_td"
        }
        
        markets = prop_markets.get(prop_type, prop_markets["all"])
        return await self.get_event_odds(sport, event_id, markets)
    
    async def find_prop_edges(
        self,
        sport: str,
        min_edge_pct: float = 10.0
    ) -> list[dict]:
        """
        Find player props with significant odds differences across books.
        
        Edges exist when bookmakers disagree on a player's line.
        """
        events = await self.get_events(sport)
        edges = []
        
        for event in events[:5]:  # Limit to first 5 events to save quota
            event_id = event.get("id")
            if not event_id:
                continue
            
            try:
                props = await self.get_player_props(sport, event_id)
                
                if not props or "bookmakers" not in props:
                    continue
                
                # Analyze each market for edge
                market_odds = {}
                
                for book in props.get("bookmakers", []):
                    for market in book.get("markets", []):
                        market_key = market.get("key")
                        
                        for outcome in market.get("outcomes", []):
                            player = outcome.get("description") or outcome.get("name")
                            line = outcome.get("point", "")
                            side = outcome.get("name")
                            price = outcome.get("price")
                            
                            key = f"{market_key}|{player}|{line}|{side}"
                            
                            if key not in market_odds:
                                market_odds[key] = []
                            
                            market_odds[key].append({
                                "book": book.get("key"),
                                "price": price
                            })
                
                # Find edges (large price differences)
                for key, odds_list in market_odds.items():
                    if len(odds_list) < 2:
                        continue
                    
                    prices = [o["price"] for o in odds_list]
                    max_price = max(prices)
                    min_price = min(prices)
                    
                    # Convert to probability and compare
                    max_prob = american_to_prob(max_price)
                    min_prob = american_to_prob(min_price)
                    
                    edge = (max_prob - min_prob) * 100
                    
                    if edge >= min_edge_pct:
                        parts = key.split("|")
                        edges.append({
                            "event": event.get("home_team", "") + " vs " + event.get("away_team", ""),
                            "market": parts[0],
                            "player": parts[1],
                            "line": parts[2],
                            "side": parts[3],
                            "edge_pct": round(edge, 1),
                            "best_book": max(odds_list, key=lambda x: x["price"])["book"],
                            "best_price": max_price,
                            "worst_price": min_price,
                        })
                        
            except Exception as e:
                print(f"Error processing event {event_id}: {e}")
                continue
        
        return sorted(edges, key=lambda x: x["edge_pct"], reverse=True)


# Sports with good prop coverage
PROP_SPORTS = [
    "americanfootball_nfl",
    "americanfootball_ncaaf",
    "basketball_nba",
    "basketball_ncaab",
    "icehockey_nhl",
    "baseball_mlb"
]

# Available futures/outright markets
FUTURES_SPORTS = [
    "americanfootball_nfl_super_bowl_winner",
    "basketball_nba_championship_winner",
    "baseball_mlb_world_series_winner",
    "icehockey_nhl_stanley_cup_winner",
    "soccer_epl_winner",
    "soccer_spain_la_liga_winner",
    "soccer_uefa_champs_league_winner",
]
