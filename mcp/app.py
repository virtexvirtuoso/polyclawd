# Polyclawd MCP Server - FastMCP Application Instance
from fastmcp import FastMCP

mcp = FastMCP(
    "polyclawd",
    instructions="""
    Polyclawd - Prediction market intelligence platform.
    163 tools covering signals, arbitrage, Vegas odds, ESPN, paper trading, and more.
    All data is real-time from live market feeds across Polymarket, Kalshi, Manifold, PredictIt, Metaculus, and Betfair.
    """,
)
