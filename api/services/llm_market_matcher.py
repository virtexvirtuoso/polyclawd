"""
LLM-based Market Matcher
Uses local Ollama (gemma3:4b) to verify if two prediction market questions
are semantically equivalent across platforms.

Handles: same question, inverted phrasing (NOT/negation), different timeframes.
Results cached in SQLite to avoid redundant LLM calls.
"""

import json
from db import connect as db_connect
import sqlite3
import hashlib
import os
import urllib.request
from dataclasses import dataclass
from typing import Optional, Tuple
from loguru import logger

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:cloud")
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "storage", "shadow_trades.db")

SYSTEM_PROMPT = (
    "You compare prediction market questions. Two markets are \"same\" ONLY if:\n"
    "1. They ask about the EXACT same outcome (not just related topics)\n"
    "2. Similar resolution timeframe (within ~3 months)\n"
    "3. Same resolution criteria — both resolve YES/NO under identical conditions\n\n"
    "CRITICAL: These are DIFFERENT markets (answer same=false):\n"
    "- \"Will X happen in [specific place]\" vs \"Will X happen\" (location constraint makes them different!)\n"
    "- \"Will X reach [threshold A]\" vs \"Will X reach [threshold B]\"\n"
    "- Different actions (\"resign\" vs \"announce a run\")\n"
    "- Subset questions: \"meet in Turkey\" could be YES while \"meet\" is also YES (they can both be true), but \"meet in Turkey\" can be NO while \"meet\" is YES — so they are NOT the same bet!\n\n"
    "\"inverted\" = true ONLY if same question but opposite polarity (YES on A ≈ NO on B).\n\n"
    "Respond ONLY with JSON: {\"same\": bool, \"inverted\": bool, \"confidence\": float, \"reason\": \"brief\"}"
)


@dataclass
class MatchResult:
    same: bool
    inverted: bool
    confidence: float
    reason: str
    from_cache: bool = False


def _cache_key(title_a: str, title_b: str) -> str:
    """Deterministic cache key regardless of order."""
    pair = sorted([title_a.strip().lower(), title_b.strip().lower()])
    return hashlib.sha256(f"{pair[0]}|||{pair[1]}".encode()).hexdigest()[:16]


def _ensure_cache_table():
    """Create cache table if not exists."""
    try:
        conn = db_connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_match_cache (
                cache_key TEXT PRIMARY KEY,
                title_a TEXT,
                title_b TEXT,
                same INTEGER,
                inverted INTEGER,
                confidence REAL,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("LLM match cache table creation failed: %s", e)


def _check_cache(key: str) -> Optional[MatchResult]:
    """Check SQLite cache for previous result."""
    try:
        conn = db_connect(DB_PATH)
        row = conn.execute(
            "SELECT same, inverted, confidence, reason FROM llm_match_cache WHERE cache_key=?",
            (key,)
        ).fetchone()
        conn.close()
        if row:
            return MatchResult(
                same=bool(row[0]),
                inverted=bool(row[1]),
                confidence=row[2],
                reason=row[3],
                from_cache=True,
            )
    except Exception:
        pass
    return None


def _save_cache(key: str, title_a: str, title_b: str, result: MatchResult):
    """Persist result to SQLite cache."""
    try:
        conn = db_connect(DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO llm_match_cache (cache_key, title_a, title_b, same, inverted, confidence, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, title_a[:200], title_b[:200], int(result.same), int(result.inverted), result.confidence, result.reason),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("LLM match cache save failed: %s", e)


def _call_ollama(title_a: str, title_b: str) -> Optional[MatchResult]:
    """Call local Ollama for market comparison."""
    prompt = f"{SYSTEM_PROMPT}\n\nA: {title_a}\nB: {title_b}"

    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 1000,
        },
    }).encode()

    try:
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())

        text = data.get("response", "").strip()
        duration_s = data.get("total_duration", 0) / 1e9

        # Extract JSON from response (may be wrapped in ```json blocks)
        text = text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(text)

        result = MatchResult(
            same=bool(parsed.get("same", False)),
            inverted=bool(parsed.get("inverted", False)),
            confidence=float(parsed.get("confidence", 0)),
            reason=parsed.get("reason", ""),
        )
        logger.debug("LLM match (%.1fs): same=%s inv=%s conf=%.2f | %s vs %s",
                      duration_s, result.same, result.inverted, result.confidence,
                      title_a[:40], title_b[:40])
        return result

    except json.JSONDecodeError as e:
        logger.warning("LLM response not valid JSON: %s | raw: %s", e, text[:200])
        return None
    except Exception as e:
        logger.warning("Ollama call failed: %s", e)
        return None


def _is_subset_question(title_a: str, title_b: str) -> bool:
    """Detect when one market is a location/method-specific subset of the other.
    e.g., 'meet in Italy before 2027' is a subset of 'meet before 2027'
    """
    import re
    a_lower = title_a.lower()
    b_lower = title_b.lower()

    # Detect "X in [place]" or "next in [place]" patterns
    # Match both multi-word places and short ones (US, UK, EU)
    location_pattern = r'\b(?:next )?in\s+(?:the\s+)?(?:[A-Z][a-z]+(?:\s*/\s*[A-Z][a-z]+)*|[A-Z]{2,3})\b'
    a_locations = re.findall(location_pattern, title_a)
    b_locations = re.findall(location_pattern, title_b)

    def _check_subset(with_loc, without_loc, loc_title, gen_title):
        stripped = re.sub(location_pattern, '', loc_title).strip()
        loc_words = set(re.findall(r'\w+', stripped.lower()))
        gen_words = set(re.findall(r'\w+', gen_title.lower()))
        overlap = len(loc_words & gen_words) / max(len(loc_words), 1)
        return overlap > 0.4

    if a_locations and not b_locations:
        if _check_subset(a_locations, b_locations, title_a, title_b):
            return True

    if b_locations and not a_locations:
        if _check_subset(b_locations, a_locations, title_b, title_a):
            return True

    # Both have locations — check if DIFFERENT locations on same question
    if a_locations and b_locations:
        a_loc_text = set(l.lower() for l in a_locations)
        b_loc_text = set(l.lower() for l in b_locations)
        if a_loc_text != b_loc_text:
            # Different locations — could still be same question if locations match
            # But if core question is similar with different locations, it's different markets
            a_stripped = re.sub(location_pattern, '', title_a).strip().lower()
            b_stripped = re.sub(location_pattern, '', title_b).strip().lower()
            a_words = set(re.findall(r'\w+', a_stripped))
            b_words = set(re.findall(r'\w+', b_stripped))
            overlap = len(a_words & b_words) / max(min(len(a_words), len(b_words)), 1)
            if overlap > 0.6:
                return True  # Same question, different locations = different markets

    return False


def _different_subjects(title_a: str, title_b: str) -> bool:
    """Detect when markets reference different key people.
    e.g., 'Trump and Putin meet in Ukraine' vs 'Putin and Zelenskyy meet in Ukraine'
    """
    import re
    # Extract proper nouns (capitalized words not at start of sentence)
    def _extract_people(text):
        # Common prediction market people
        people = set()
        patterns = [
            r'\btrump\b', r'\bbiden\b', r'\bharris\b', r'\bmusk\b',
            r'\bzelenskyy?\b', r'\bputin\b', r'\bxi\b', r'\bnetanyahu\b',
            r'\bmacron\b', r'\bstarmer\b', r'\bjokic\b', r'\bluka\b',
        ]
        text_lower = text.lower()
        for p in patterns:
            if re.search(p, text_lower):
                people.add(re.search(p, text_lower).group())
        return people

    people_a = _extract_people(title_a)
    people_b = _extract_people(title_b)

    # If both mention people but the sets differ, these are different markets
    if people_a and people_b and people_a != people_b:
        # Allow if one is a subset (e.g., "Putin" in both but one also mentions Trump)
        if not people_a.issubset(people_b) and not people_b.issubset(people_a):
            return True

    return False


def _fix_negation(title_a: str, title_b: str, result: MatchResult) -> MatchResult:
    """Post-LLM fix: detect negation mismatch that small models miss.
    If LLM says same=True but one title has 'not'/'won't' and the other doesn't,
    force inverted=True.
    """
    import re
    neg_pattern = r'\bnot\b|\bwon\'?t\b|\bnever\b|\bno\b(?=\s+\w)'

    a_neg = bool(re.search(neg_pattern, title_a.lower()))
    b_neg = bool(re.search(neg_pattern, title_b.lower()))

    if a_neg != b_neg and not result.inverted:
        return MatchResult(
            same=result.same,
            inverted=True,
            confidence=result.confidence,
            reason=f"{result.reason} [negation fix: one title has NOT/WON'T]",
            from_cache=result.from_cache,
        )
    return result


def verify_match(title_a: str, title_b: str) -> Optional[MatchResult]:
    """
    Verify if two market titles represent the same binary question.
    Uses heuristic pre-filters + SQLite cache + local LLM.
    
    Returns MatchResult or None if LLM is unavailable.
    """
    _ensure_cache_table()

    # Heuristic pre-filters: reject obvious non-matches before hitting LLM
    if _is_subset_question(title_a, title_b):
        result = MatchResult(
            same=False, inverted=False, confidence=0.9,
            reason="Subset/location-specific question detected"
        )
        key = _cache_key(title_a, title_b)
        _save_cache(key, title_a, title_b, result)
        return result

    if _different_subjects(title_a, title_b):
        result = MatchResult(
            same=False, inverted=False, confidence=0.9,
            reason="Different key subjects/people detected"
        )
        key = _cache_key(title_a, title_b)
        _save_cache(key, title_a, title_b, result)
        return result

    key = _cache_key(title_a, title_b)

    # Check cache first
    cached = _check_cache(key)
    if cached:
        return cached

    # Call LLM
    result = _call_ollama(title_a, title_b)
    if result and result.same:
        # Post-LLM negation check: if one title has "not" / "won't" that the other doesn't,
        # force inverted=True (small models often miss negation)
        result = _fix_negation(title_a, title_b, result)
        _save_cache(key, title_a, title_b, result)
        return result
    elif result:
        _save_cache(key, title_a, title_b, result)
        return result

    return None


def verify_batch(pairs: list) -> list:
    """
    Verify a batch of (title_a, title_b) pairs.
    Returns list of (title_a, title_b, MatchResult|None).
    Cache hits are instant; LLM calls are sequential.
    """
    _ensure_cache_table()
    results = []

    for title_a, title_b in pairs:
        result = verify_match(title_a, title_b)
        results.append((title_a, title_b, result))

    return results


def is_ollama_available() -> bool:
    """Quick health check for Ollama."""
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            models = [m.get("name", "") for m in data.get("models", [])]
            return any(OLLAMA_MODEL in m for m in models)
    except Exception:
        return False
