#!/usr/bin/env python3
"""
AI Model Market Edge Tracker

Specialized signal source for AI/LLM prediction markets.
Monitors Arena leaderboard scores, model releases, and vote velocity
to find edges in markets like "Which company will have #1 model?"

Edge Sources:
1. Live Arena leaderboard scraping — detect score gaps & trends
2. Release cycle detection — new model submissions before prices move
3. Vote velocity — flag unstable rankings (<500 votes)
4. Score delta tracking — daily snapshots to spot momentum shifts

Feeds into mispriced_category as a sub-signal for tech/AI markets.
"""

import json
import logging
import re
import time
import urllib.request
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# Configuration
# ============================================================================

ARENA_URL = "https://lmarena.ai/leaderboard"
ARENA_API_URL = "https://lmarena.ai/api/v1/leaderboard"  # If API exists

# Company → search patterns for Arena model names
COMPANY_PATTERNS = {
    "Anthropic": [r"claude", r"anthropic"],
    "Google": [r"gemini", r"google"],
    "OpenAI": [r"gpt-\d", r"o\d+", r"chatgpt", r"openai"],
    "xAI": [r"grok"],
    "DeepSeek": [r"deepseek"],
    "Meta": [r"llama", r"meta"],
    "Mistral": [r"mistral"],
    "Moonshot": [r"kimi", r"moonshot"],
    "Alibaba": [r"qwen"],
    "Zhipu": [r"glm"],
    "Baidu": [r"ernie"],
}

# Storage
STORAGE_DIR = Path(__file__).parent.parent / "storage"
DB_PATH = STORAGE_DIR / "ai_model_tracker.db"
SNAPSHOT_DIR = STORAGE_DIR / "arena_snapshots"

# Staleness thresholds
SNAPSHOT_MAX_AGE_HOURS = 24
MIN_VOTES_STABLE = 500


# ============================================================================
# Database
# ============================================================================

def init_db():
    """Initialize SQLite database for Arena score tracking."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arena_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            model_name TEXT NOT NULL,
            company TEXT,
            overall_rank INTEGER,
            arena_score REAL,
            ci_lower REAL,
            ci_upper REAL,
            vote_count INTEGER,
            style_control_off_rank INTEGER,
            raw_data TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS score_deltas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            company TEXT NOT NULL,
            best_rank INTEGER,
            best_score REAL,
            prev_best_rank INTEGER,
            prev_best_score REAL,
            rank_change INTEGER,
            score_change REAL,
            new_models TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_releases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT NOT NULL,
            model_name TEXT NOT NULL,
            company TEXT,
            source TEXT,
            initial_rank INTEGER,
            initial_score REAL,
            vote_count INTEGER,
            is_stable INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_snapshots_ts
        ON arena_snapshots(timestamp)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_snapshots_company
        ON arena_snapshots(company)
    """)
    conn.commit()
    conn.close()


# ============================================================================
# Arena Scraper
# ============================================================================

def classify_company(model_name: str) -> str:
    """Map a model name to its parent company."""
    name_lower = model_name.lower()
    for company, patterns in COMPANY_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, name_lower):
                return company
    return "Unknown"


def fetch_arena_leaderboard() -> List[Dict[str, Any]]:
    """
    Fetch current Arena leaderboard data from arena.ai.
    Uses browser-like UA and parses the Next.js SSR HTML.
    """
    models = []

    try:
        req = urllib.request.Request(
            "https://arena.ai/leaderboard",
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/131.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        models = _parse_arena_html(html)

        if not models:
            logger.warning("No models parsed from Arena HTML — layout may have changed")

    except Exception as e:
        logger.error(f"Failed to fetch Arena leaderboard: {e}")

    return models


# Known model name patterns to filter signal from noise
_MODEL_KEYWORDS = [
    "claude", "gpt", "gemini", "grok", "llama", "qwen", "deepseek",
    "glm", "ernie", "kimi", "mistral", "o1-", "o3-", "o4-",
    "chatgpt", "moonshot",
]


def _parse_arena_html(html: str) -> List[Dict[str, Any]]:
    """
    Parse Arena leaderboard from SSR HTML.
    
    Arena renders model names in title attributes of anchor tags,
    with rank numbers in preceding table cells.
    Extracts ordered list of (rank, model_name, company).
    """
    models = []
    seen = set()

    # Arena renders: <a title="model-name"> with rank in a preceding cell
    # Model names appear in title attributes, ordered by rank
    rank_counter = 0
    for match in re.finditer(r'title="([\w\-\.]+(?:-\d[\w\-\.]*)?)"', html):
        name = match.group(1)

        # Filter to actual model names
        name_lower = name.lower()
        if not any(kw in name_lower for kw in _MODEL_KEYWORDS):
            continue

        if name in seen:
            continue
        seen.add(name)

        rank_counter += 1
        company = classify_company(name)

        models.append({
            "model_name": name,
            "company": company,
            "overall_rank": rank_counter,
            "arena_score": None,  # Not easily extractable from HTML
            "vote_count": None,
        })

    # If title-based extraction failed, try text-based fallback
    if not models:
        rank_pattern = r'(\d+)\s+([\w\-\.]+(?:[\w\-\./ ]+)?)\s+(\d{3,4}(?:\.\d+)?)\s*±?\s*(\d+)?'
        for match in re.finditer(rank_pattern, html):
            rank, name, score, ci = match.groups()
            name = name.strip()
            if len(name) > 3 and name not in seen:
                seen.add(name)
                models.append({
                    "model_name": name,
                    "company": classify_company(name),
                    "overall_rank": int(rank),
                    "arena_score": float(score),
                    "ci_margin": int(ci) if ci else None,
                    "vote_count": None,
                })

    return models


def snapshot_leaderboard(models: List[Dict[str, Any]]) -> str:
    """Save a timestamped snapshot of the leaderboard to SQLite."""
    if not models:
        return "No models to snapshot"

    init_db()
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(DB_PATH))

    for m in models:
        conn.execute("""
            INSERT INTO arena_snapshots
            (timestamp, model_name, company, overall_rank, arena_score, vote_count, raw_data)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            now, m["model_name"], m.get("company"), m.get("overall_rank"),
            m.get("arena_score"), m.get("vote_count"), json.dumps(m)
        ))

    conn.commit()

    # Also save a JSON snapshot file
    snapshot_file = SNAPSHOT_DIR / f"arena_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    with open(snapshot_file, "w") as f:
        json.dump({"timestamp": now, "models": models}, f, indent=2)

    conn.close()
    return f"Snapshot saved: {len(models)} models at {now}"


# ============================================================================
# Score Delta Analysis
# ============================================================================

def compute_company_rankings(models: List[Dict[str, Any]]) -> Dict[str, Dict]:
    """Get best model per company from current leaderboard."""
    rankings = {}
    for m in models:
        company = m.get("company", "Unknown")
        rank = m.get("overall_rank", 999)
        score = m.get("arena_score", 0)

        if company not in rankings or rank < rankings[company]["best_rank"]:
            rankings[company] = {
                "company": company,
                "best_model": m["model_name"],
                "best_rank": rank,
                "best_score": score,
                "model_count": rankings.get(company, {}).get("model_count", 0) + 1,
            }
        else:
            rankings[company]["model_count"] += 1

    return rankings


def detect_new_models(models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Detect models not seen in previous snapshots."""
    init_db()
    conn = sqlite3.connect(str(DB_PATH))

    known = set()
    cursor = conn.execute("SELECT DISTINCT model_name FROM arena_snapshots")
    for row in cursor:
        known.add(row[0])
    conn.close()

    new_models = []
    for m in models:
        if m["model_name"] not in known:
            new_models.append(m)

    return new_models


def get_score_history(company: str, days: int = 30) -> List[Dict]:
    """Get historical best scores for a company."""
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    cursor = conn.execute("""
        SELECT timestamp, model_name, overall_rank, arena_score
        FROM arena_snapshots
        WHERE company = ? AND timestamp > ?
        ORDER BY timestamp ASC
    """, (company, cutoff))

    history = []
    for row in cursor:
        history.append({
            "timestamp": row[0],
            "model": row[1],
            "rank": row[2],
            "score": row[3],
        })

    conn.close()
    return history


# ============================================================================
# Signal Generation
# ============================================================================

def generate_ai_model_signals(polymarket_markets: List[Dict] = None) -> List[Dict[str, Any]]:
    """
    Generate trading signals for AI model prediction markets.
    
    Combines:
    1. Arena leaderboard position
    2. Score gap analysis (how far behind is each company?)
    3. Vote stability (is the ranking settled?)
    4. New model detection (surprise releases)
    
    Returns list of signal dicts compatible with aggregate_all_signals().
    """
    signals = []

    # Fetch current leaderboard
    models = fetch_arena_leaderboard()
    if not models:
        logger.warning("Could not fetch Arena leaderboard — skipping AI signals")
        return signals

    # Snapshot for historical tracking
    snapshot_leaderboard(models)

    # Detect new models
    new_models = detect_new_models(models)
    if new_models:
        logger.info(f"🆕 {len(new_models)} new models detected on Arena")
        for nm in new_models:
            init_db()
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute("""
                INSERT INTO model_releases (detected_at, model_name, company, source, initial_rank, initial_score, vote_count)
                VALUES (?, ?, ?, 'arena', ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                nm["model_name"], nm.get("company"),
                nm.get("overall_rank"), nm.get("arena_score"),
                nm.get("vote_count")
            ))
            conn.commit()
            conn.close()

    # Company rankings
    rankings = compute_company_rankings(models)

    # If we have Polymarket markets about AI models, generate signals
    if polymarket_markets:
        for market in polymarket_markets:
            signal = _evaluate_ai_market(market, rankings, models, new_models)
            if signal:
                signals.append(signal)

    # Also generate standalone insights
    leader = min(rankings.values(), key=lambda x: x["best_rank"])
    gap_signals = _generate_gap_signals(rankings, leader)
    signals.extend(gap_signals)

    return signals


def _evaluate_ai_market(
    market: Dict,
    rankings: Dict[str, Dict],
    models: List[Dict],
    new_models: List[Dict]
) -> Optional[Dict[str, Any]]:
    """
    Evaluate a specific prediction market about AI models.
    
    Looks for markets like:
    - "Which company will have #1 model on Arena?"
    - "Will GPT-5 beat Claude?"
    - "Google #1 on LLM leaderboard?"
    """
    title = market.get("title", "").lower()
    question = market.get("question", "").lower()
    text = f"{title} {question}"

    # Detect market type
    if any(kw in text for kw in ["arena", "leaderboard", "chatbot arena", "#1 model", "best model"]):
        return _evaluate_arena_winner_market(market, rankings, new_models)

    if any(kw in text for kw in ["beat", "vs", "versus", "better than"]):
        return _evaluate_head_to_head_market(market, rankings)

    return None


def _evaluate_arena_winner_market(
    market: Dict,
    rankings: Dict[str, Dict],
    new_models: List[Dict]
) -> Optional[Dict[str, Any]]:
    """
    Evaluate "Which company will be #1 on Arena?" style markets.
    
    Edge logic:
    - Current #1 has structural advantage (momentum + votes)
    - Gap size matters: >20 points is very hard to close
    - New model releases could disrupt but need time for votes
    - Check each outcome's price vs Arena-implied probability
    """
    leader = min(rankings.values(), key=lambda x: x["best_rank"])
    leader_company = leader["company"]
    leader_score = leader.get("best_score", 0)

    outcomes = market.get("outcomes", [])
    if not outcomes:
        return None

    best_edge = None
    best_edge_pct = 0

    for outcome in outcomes:
        company_name = outcome.get("title", outcome.get("name", ""))
        market_price = outcome.get("price", 0)

        if market_price <= 0 or market_price >= 1:
            continue

        # Estimate fair probability from Arena data
        fair_prob = _estimate_fair_probability(
            company_name, leader_company, leader_score, rankings, new_models
        )

        if fair_prob is None:
            continue

        edge = fair_prob - market_price  # Negative = overpriced YES

        if abs(edge) > abs(best_edge_pct):
            best_edge_pct = edge
            best_edge = {
                "company": company_name,
                "market_price": market_price,
                "fair_value": fair_prob,
                "edge_pct": abs(edge),
                "side": "YES" if edge > 0 else "NO",
            }

    if not best_edge or best_edge["edge_pct"] < 0.05:  # Min 5% edge
        return None

    confidence = min(0.85, 0.50 + best_edge["edge_pct"])

    return {
        "source": "ai_model_tracker",
        "platform": market.get("platform", "polymarket"),
        "market_id": market.get("id", ""),
        "title": market.get("title", "AI Model Market"),
        "signal": best_edge["side"],
        "target": best_edge["company"],
        "confidence": round(confidence, 3),
        "edge_pct": round(best_edge["edge_pct"] * 100, 1),
        "market_price": best_edge["market_price"],
        "fair_value": round(best_edge["fair_value"], 3),
        "arena_leader": leader_company,
        "arena_leader_rank": leader["best_rank"],
        "arena_leader_score": leader_score,
        "new_models_detected": len(new_models),
        "reasoning": (
            f"Arena shows {leader_company} at #{leader['best_rank']} "
            f"(score {leader_score}). {best_edge['company']} market price "
            f"${best_edge['market_price']:.2f} vs fair value "
            f"${best_edge['fair_value']:.3f}. "
            f"Edge: {best_edge['edge_pct']*100:.1f}% on {best_edge['side']}."
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _estimate_fair_probability(
    company: str,
    leader_company: str,
    leader_score: float,
    rankings: Dict[str, Dict],
    new_models: List[Dict]
) -> Optional[float]:
    """
    Estimate fair probability that a company will be #1 on Arena.
    
    Factors:
    - Current rank position
    - Score gap from #1
    - Historical volatility of rankings
    - Pending model releases
    - Time to resolution
    """
    company_norm = company.strip()

    # Find in rankings (fuzzy match)
    matched = None
    for co, data in rankings.items():
        if co.lower() in company_norm.lower() or company_norm.lower() in co.lower():
            matched = data
            break

    if not matched:
        return 0.02  # Unknown company → very unlikely

    rank = matched["best_rank"]
    score = matched.get("best_score", 0)

    if rank == 1:
        # Current leader — base 65-80% depending on gap to #2
        base = 0.70
        # Find second place
        sorted_companies = sorted(rankings.values(), key=lambda x: x["best_rank"])
        if len(sorted_companies) > 1:
            gap = score - sorted_companies[1].get("best_score", 0) if score else 0
            if gap > 20:
                base = 0.80  # Large gap = very stable
            elif gap > 10:
                base = 0.75
            elif gap < 5:
                base = 0.60  # Tight race
        return base

    elif rank <= 3:
        # Contender — 10-25%
        gap = (leader_score - score) if leader_score and score else 30
        if gap < 10:
            return 0.25
        elif gap < 20:
            return 0.15
        else:
            return 0.10

    elif rank <= 5:
        return 0.05

    elif rank <= 10:
        return 0.02

    else:
        return 0.01


def _evaluate_head_to_head_market(
    market: Dict, rankings: Dict[str, Dict]
) -> Optional[Dict[str, Any]]:
    """Evaluate 'Will Model A beat Model B?' markets."""
    # TODO: implement head-to-head model comparison
    return None


def _generate_gap_signals(
    rankings: Dict[str, Dict], leader: Dict
) -> List[Dict[str, Any]]:
    """
    Generate informational signals about Arena dynamics.
    Useful for dashboards even without specific markets.
    """
    signals = []

    sorted_companies = sorted(rankings.values(), key=lambda x: x["best_rank"])

    if len(sorted_companies) >= 2:
        gap = (leader.get("best_score", 0) - sorted_companies[1].get("best_score", 0))
        signals.append({
            "source": "ai_model_tracker",
            "type": "arena_gap",
            "leader": leader["company"],
            "leader_model": leader["best_model"],
            "leader_rank": leader["best_rank"],
            "runner_up": sorted_companies[1]["company"],
            "runner_up_model": sorted_companies[1]["best_model"],
            "score_gap": round(gap, 1) if gap else None,
            "total_companies_tracked": len(sorted_companies),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    return signals


# ============================================================================
# API Integration Points
# ============================================================================

def get_arena_summary() -> Dict[str, Any]:
    """Get a summary of current Arena state for API endpoints."""
    models = fetch_arena_leaderboard()
    if not models:
        return {"error": "Could not fetch leaderboard", "models": []}

    rankings = compute_company_rankings(models)
    new_models = detect_new_models(models)

    # Snapshot
    snapshot_leaderboard(models)

    sorted_rankings = sorted(rankings.values(), key=lambda x: x["best_rank"])

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_models": len(models),
        "company_rankings": sorted_rankings[:10],
        "new_models": [
            {"name": m["model_name"], "company": m.get("company"), "rank": m.get("overall_rank")}
            for m in new_models
        ],
        "leader": sorted_rankings[0] if sorted_rankings else None,
    }


def get_score_trends(days: int = 7) -> Dict[str, Any]:
    """Get score trends over recent days."""
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Get distinct timestamps
    cursor = conn.execute("""
        SELECT DISTINCT date(timestamp) as day, company, MIN(overall_rank) as best_rank
        FROM arena_snapshots
        WHERE timestamp > ?
        GROUP BY day, company
        ORDER BY day ASC, best_rank ASC
    """, (cutoff,))

    trends = {}
    for row in cursor:
        day, company, rank = row
        if company not in trends:
            trends[company] = []
        trends[company].append({"date": day, "best_rank": rank})

    conn.close()
    return {"period_days": days, "trends": trends}


# ============================================================================
# CLI
# ============================================================================

def main():
    """CLI for testing the AI model tracker."""
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"

    if cmd == "summary":
        summary = get_arena_summary()
        print(json.dumps(summary, indent=2))

    elif cmd == "snapshot":
        models = fetch_arena_leaderboard()
        result = snapshot_leaderboard(models)
        print(result)

    elif cmd == "trends":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        trends = get_score_trends(days)
        print(json.dumps(trends, indent=2))

    elif cmd == "signals":
        signals = generate_ai_model_signals()
        print(json.dumps(signals, indent=2))

    elif cmd == "history":
        company = sys.argv[2] if len(sys.argv) > 2 else "Anthropic"
        history = get_score_history(company)
        print(json.dumps(history, indent=2))

    else:
        print(f"Usage: {sys.argv[0]} [summary|snapshot|trends|signals|history <company>]")


if __name__ == "__main__":
    main()
