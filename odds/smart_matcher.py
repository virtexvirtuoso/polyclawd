"""
Smart Market Matcher
Entity-based matching for cross-platform market comparison
"""

import re
from dataclasses import dataclass, field
from typing import Optional, List, Set, Tuple
from datetime import datetime

@dataclass
class MarketSignature:
    """Extracted signature of a prediction market"""
    raw_text: str
    entities: Set[str] = field(default_factory=set)      # People, orgs, places
    event_type: Optional[str] = None                      # win, reach, pass, announce, etc.
    target: Optional[str] = None                          # What they need to win/reach
    timeframe: Optional[str] = None                       # Date or period
    numeric_threshold: Optional[float] = None             # e.g., "$100k", "50%"
    is_yes_market: bool = True                           # YES or NO phrasing

# Key entities to extract (case-insensitive)
PERSON_PATTERNS = [
    r'\btrump\b', r'\bbiden\b', r'\bharris\b', r'\bnewsom\b', r'\bdesantis\b',
    r'\bvance\b', r'\baoc\b', r'\bocasio-?cortez\b', r'\bmusk\b', r'\bbezos\b',
    r'\bnetanyahu\b', r'\bzelensky\b', r'\bputin\b', r'\bxi\s?jinping\b', r'\bkhamenei\b',
    r'\bmaduro\b', r'\bwarsh\b', r'\bpowell\b', r'\byellen\b', r'\bsam\s?altman\b',
]

ORG_PATTERNS = [
    r'\bfed\b', r'\bfederal reserve\b', r'\bscotus\b', r'\bsupreme court\b',
    r'\bopenai\b', r'\banthropic\b', r'\btesla\b', r'\btiktok\b', r'\bmeta\b',
    r'\bgoogle\b', r'\bapple\b', r'\bmicrosoft\b', r'\bnvidia\b',
    r'\bchampions league\b', r'\bpremier league\b', r'\bla liga\b', r'\bnba\b',
    r'\bnfl\b', r'\bmlb\b', r'\bnhl\b', r'\bsuper bowl\b', r'\bworld cup\b',
    # Crypto
    r'\bbitcoin\b', r'\bbtc\b', r'\bethereum\b', r'\beth\b', r'\bsolana\b', r'\bsol\b',
    r'\bdogecoin\b', r'\bdoge\b', r'\bxrp\b', r'\bcardano\b', r'\bada\b',
]

PLACE_PATTERNS = [
    r'\bukraine\b', r'\brussia\b', r'\bisrael\b', r'\bgaza\b', r'\biran\b',
    r'\bchina\b', r'\btaiwan\b', r'\bvenezuela\b', r'\bnorth korea\b',
]

SPORTS_TEAMS = [
    # NFL
    r'\bchiefs\b', r'\beagles\b', r'\bbills\b', r'\blions\b', r'\bravens\b',
    r'\b49ers\b', r'\bpackers\b', r'\bcowboys\b', r'\bseahawks\b', r'\bvikings\b',
    # NBA
    r'\bceltics\b', r'\blakers\b', r'\bwarriors\b', r'\bbucks\b', r'\bnuggets\b',
    r'\bheat\b', r'\bsuns\b', r'\bmavericks\b', r'\bthunder\b', r'\bcavaliers\b',
    # Soccer
    r'\bman(?:chester)?\s*city\b', r'\bman(?:chester)?\s*united\b', r'\barsenal\b',
    r'\bliverpool\b', r'\bchelsea\b', r'\btottenham\b', r'\breal madrid\b',
    r'\bbarcelona\b', r'\bbayern\b', r'\bpsg\b', r'\binter milan\b', r'\bjuventus\b',
]

# Entity aliases (map variations to canonical form)
ENTITY_ALIASES = {
    # Crypto
    'btc': 'bitcoin',
    'eth': 'ethereum',
    'sol': 'solana',
    'doge': 'dogecoin',
    'ada': 'cardano',
    # Sports
    'man city': 'manchester city',
    'man united': 'manchester united',
    'spurs': 'tottenham',
    'barca': 'barcelona',
    # People
    'xi': 'xi jinping',
    'ocasio-cortez': 'aoc',
    'ocasio cortez': 'aoc',
}

EVENT_TYPE_PATTERNS = {
    'win_election': r'\bwin\b.*\b(election|presidency|presidential)\b|\bpresident(?:ial)?\s+election\b.*\bwin\b',
    'win_nomination': r'\b(nomination|nominate[d]?|nominee)\b.*\bwin\b|\bwin\b.*\b(nomination|nominee)\b',
    'win': r'\bwin(?:s|ning)?\b|\bchampion\b|\bvictory\b',
    'nominate': r'\bnominate[ds]?\b|\bnomination\b|\bnominee\b',
    'leave_office': r'\bleave[s]?\s+(the\s+)?office\b|\bstep(?:s)?\s+down\b|\bresign[s]?\b|\bdepart\b',
    'reach': r'\breach(?:es|ing)?\b|\bhit(?:s|ting)?\b|\bexceed\b',
    'pass': r'\bpass(?:es|ed)?\b|\bapprove[ds]?\b|\benact\b',
    'announce': r'\bannounce[ds]?\b|\breveal\b|\bconfirm\b',
    'ban': r'\bban(?:s|ned)?\b|\bblock\b|\bprohibit\b',
    'launch': r'\blaunch(?:es)?\b|\brelease[ds]?\b|\broll out\b',
    'default': r'\bdefault\b|\bbankrupt\b',
    'rate_cut': r'\brate cut\b|\bcut(?:s)? rate\b|\blower(?:s)? rate\b',
    'rate_hike': r'\brate hike\b|\braise(?:s)? rate\b|\bhigher rate\b',
}

TARGET_PATTERNS = {
    'presidential_election': r'\bpresidential\s+election\b|\bwin\s+the\s+\d{4}\b.*\bpresident\b',
    'presidential_nomination': r'\bpresidential\s+nomination\b|\brepublican\s+nomin\b|\bdemocratic\s+nomin\b|\bgop\s+nomin\b',
    'presidency': r'\bpresident\b|\bpresidency\b|\bwhite house\b',
    'championship': r'\bchampion(?:ship)?\b|\btitle\b|\btrophy\b',
    'super_bowl': r'\bsuper bowl\b',
    'world_series': r'\bworld series\b',
    'stanley_cup': r'\bstanley cup\b',
    'ucl': r'\bchampions league\b|\bucl\b',
    'epl': r'\bpremier league\b|\bepl\b',
    'senate': r'\bsenate\b',
    'house': r'\bhouse\b|\bcongress\b',
    'fed_chair': r'\bfed chair\b|\bfederal reserve chair\b',
    'btc_price': r'\bbitcoin\b.*\$|\bbtc\b.*\$',
    'eth_price': r'\bethereum\b.*\$|\beth\b.*\$',
}

def extract_entities(text: str) -> Set[str]:
    """Extract named entities from text, normalized via aliases"""
    entities = set()
    text_lower = text.lower()
    
    for pattern in PERSON_PATTERNS + ORG_PATTERNS + PLACE_PATTERNS + SPORTS_TEAMS:
        match = re.search(pattern, text_lower)
        if match:
            entity = match.group().strip()
            entity = re.sub(r'\s+', ' ', entity)
            
            # Apply aliases to canonical form
            entity = ENTITY_ALIASES.get(entity, entity)
            entities.add(entity)
    
    return entities

def extract_event_type(text: str) -> Optional[str]:
    """Extract event type from market question"""
    text_lower = text.lower()
    for event_type, pattern in EVENT_TYPE_PATTERNS.items():
        if re.search(pattern, text_lower):
            return event_type
    return None

def extract_target(text: str) -> Optional[str]:
    """Extract the target/goal of the prediction"""
    text_lower = text.lower()
    for target, pattern in TARGET_PATTERNS.items():
        if re.search(pattern, text_lower):
            return target
    return None

def extract_timeframe(text: str) -> Optional[str]:
    """Extract date/timeframe from text, returns end year for seasons"""
    text_lower = text.lower()
    
    # Season notation: 2024-25, 2024/25 -> use end year (2025)
    season_match = re.search(r'\b(202[4-9])[-/](\d{2})\b', text)
    if season_match:
        start_year = int(season_match.group(1))
        end_suffix = int(season_match.group(2))
        # 2024-25 -> 2025, 2024-26 -> 2026
        end_year = 2000 + end_suffix if end_suffix < 50 else 1900 + end_suffix
        return str(end_year)
    
    # Month + year
    month_match = re.search(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*(202[4-9])\b', text_lower)
    if month_match:
        return month_match.group(2)  # Just return year for simpler matching
    
    # Specific dates - extract year
    date_match = re.search(r'\b(\d{1,2})[/-](\d{1,2})[/-](202[4-9])\b', text)
    if date_match:
        return date_match.group(3)
    
    # Quarter notation
    quarter_match = re.search(r'\bq([1-4])\s*(202[4-9])?\b', text_lower)
    if quarter_match:
        return quarter_match.group(2) or "2025"
    
    # Standalone year
    year_match = re.search(r'\b(202[4-9]|203[0-9])\b', text)
    if year_match:
        return year_match.group(1)
    
    return None

def extract_numeric_threshold(text: str) -> Optional[float]:
    """Extract numeric thresholds like $100k, 50%, etc."""
    # Dollar amounts
    dollar_match = re.search(r'\$\s*([\d,.]+)\s*([kmbt](?:illion|rillion)?)?', text.lower())
    if dollar_match:
        value = float(dollar_match.group(1).replace(',', ''))
        multiplier = dollar_match.group(2) or ''
        if multiplier.startswith('k'):
            value *= 1_000
        elif multiplier.startswith('m'):
            value *= 1_000_000
        elif multiplier.startswith('b'):
            value *= 1_000_000_000
        elif multiplier.startswith('t'):
            value *= 1_000_000_000_000
        return value
    
    # Percentages
    pct_match = re.search(r'(\d+(?:\.\d+)?)\s*%', text)
    if pct_match:
        return float(pct_match.group(1))
    
    return None

def create_signature(text: str) -> MarketSignature:
    """Create a market signature for matching"""
    return MarketSignature(
        raw_text=text,
        entities=extract_entities(text),
        event_type=extract_event_type(text),
        target=extract_target(text),
        timeframe=extract_timeframe(text),
        numeric_threshold=extract_numeric_threshold(text),
        is_yes_market='not' not in text.lower()[:50]  # Check for negation early in text
    )

def extract_subject(text: str, entities: Set[str]) -> Optional[str]:
    """Extract who/what is the subject of the action (entity immediately before verb)"""
    text_lower = text.lower()
    
    # Key action verbs
    verbs = ['win', 'reach', 'hit', 'pass', 'resign', 'announce', 'ban', 'launch', 
             'visit', 'meet', 'invade', 'attack', 'approve', 'cut', 'raise', 'fire']
    
    # Find first verb in text
    verb_pos = len(text_lower)
    found_verb = None
    for verb in verbs:
        # Look for verb with word boundary
        match = re.search(rf'\b{verb}(?:s|ed|ing)?\b', text_lower)
        if match and match.start() < verb_pos:
            verb_pos = match.start()
            found_verb = verb
    
    if not found_verb:
        return None
    
    # Extract text before the verb
    before_verb = text_lower[:verb_pos]
    
    # Find which entity appears closest to (and before) the verb
    best_entity = None
    best_pos = -1
    
    for entity in entities:
        # Find last occurrence of entity before verb
        for match in re.finditer(rf'\b{re.escape(entity)}\b', before_verb):
            if match.end() > best_pos:
                best_pos = match.end()
                best_entity = entity
    
    return best_entity

def signatures_match(sig1: MarketSignature, sig2: MarketSignature, min_entity_overlap: int = 1) -> Tuple[bool, float, str]:
    """
    Check if two market signatures match.
    Returns (is_match, confidence, reason)
    """
    reasons = []
    confidence = 0.0
    
    # Entity overlap (required)
    entity_overlap = sig1.entities & sig2.entities
    if len(entity_overlap) < min_entity_overlap:
        return False, 0.0, f"No entity overlap (need {min_entity_overlap})"
    
    confidence += 0.3 * len(entity_overlap)
    reasons.append(f"entities: {entity_overlap}")
    
    # Subject matching (who is doing the action)
    subj1 = extract_subject(sig1.raw_text, sig1.entities)
    subj2 = extract_subject(sig2.raw_text, sig2.entities)
    
    if subj1 and subj2:
        if subj1 == subj2:
            confidence += 0.2
            reasons.append(f"subject: {subj1}")
        else:
            # Different subjects doing same action = different market
            return False, 0.0, f"Subject mismatch: {subj1} vs {subj2}"
    
    # Event type match (strong signal)
    if sig1.event_type and sig2.event_type:
        if sig1.event_type == sig2.event_type:
            confidence += 0.3
            reasons.append(f"event: {sig1.event_type}")
        else:
            # Different event types = likely different markets
            return False, 0.0, f"Event type mismatch: {sig1.event_type} vs {sig2.event_type}"
    
    # Target match (strong signal)
    if sig1.target and sig2.target:
        if sig1.target == sig2.target:
            confidence += 0.25
            reasons.append(f"target: {sig1.target}")
        else:
            # Different targets = different markets
            return False, 0.0, f"Target mismatch: {sig1.target} vs {sig2.target}"
    elif sig1.target or sig2.target:
        # Only one has a target - mild penalty
        confidence -= 0.1
    
    # Timeframe compatibility
    if sig1.timeframe and sig2.timeframe:
        # Extract year for comparison
        year1 = re.search(r'202[4-9]', sig1.timeframe)
        year2 = re.search(r'202[4-9]', sig2.timeframe)
        if year1 and year2:
            if year1.group() == year2.group():
                confidence += 0.15
                reasons.append(f"year: {year1.group()}")
            else:
                # Different years = different markets
                return False, 0.0, f"Year mismatch: {sig1.timeframe} vs {sig2.timeframe}"
    
    # Numeric threshold similarity
    if sig1.numeric_threshold and sig2.numeric_threshold:
        # Allow 20% tolerance
        ratio = min(sig1.numeric_threshold, sig2.numeric_threshold) / max(sig1.numeric_threshold, sig2.numeric_threshold)
        if ratio >= 0.8:
            confidence += 0.15
            reasons.append(f"threshold: ~{sig1.numeric_threshold}")
        else:
            return False, 0.0, f"Threshold mismatch: {sig1.numeric_threshold} vs {sig2.numeric_threshold}"
    
    # Cap confidence at 1.0
    confidence = min(confidence, 1.0)
    
    # Require minimum confidence
    if confidence < 0.4:
        return False, confidence, f"Low confidence ({confidence:.2f})"
    
    return True, confidence, " | ".join(reasons)

def match_markets(
    source_title: str,
    candidates: List[dict],
    title_key: str = "title",
    min_entity_overlap: int = 1,
    min_confidence: float = 0.4,
    max_matches: int = 3
) -> List[dict]:
    """
    Find matching markets from candidates.
    
    Args:
        source_title: The market title to match
        candidates: List of candidate markets (dicts with title_key)
        title_key: Key for title in candidate dicts
        min_entity_overlap: Minimum entities that must overlap
        min_confidence: Minimum confidence score
        max_matches: Maximum matches to return
    
    Returns:
        List of matches with confidence scores
    """
    source_sig = create_signature(source_title)
    
    if not source_sig.entities:
        return []  # Can't match without entities
    
    matches = []
    seen_titles = set()
    
    for candidate in candidates:
        title = candidate.get(title_key, "")
        if not title or title in seen_titles:
            continue
        
        cand_sig = create_signature(title)
        is_match, confidence, reason = signatures_match(source_sig, cand_sig, min_entity_overlap)
        
        if is_match and confidence >= min_confidence:
            seen_titles.add(title)
            matches.append({
                **candidate,
                "_match_confidence": round(confidence, 3),
                "_match_reason": reason,
                "_source_entities": list(source_sig.entities),
                "_matched_entities": list(source_sig.entities & cand_sig.entities)
            })
    
    # Sort by confidence
    matches.sort(key=lambda x: x["_match_confidence"], reverse=True)
    
    return matches[:max_matches]


if __name__ == "__main__":
    # Test cases
    test_pairs = [
        # Should match
        ("Will Trump win the 2024 presidential election?", "Trump to win 2024 presidency"),
        ("Chiefs to win Super Bowl 2025", "Will the Kansas City Chiefs win Super Bowl LIX?"),
        ("Bitcoin to reach $100k by end of 2024", "Will BTC hit $100,000 in 2024?"),
        ("Man City to win Premier League 2024-25", "Manchester City EPL champions 2025"),
        
        # Should NOT match
        ("Trump approval rating above 50%", "Will Trump win 2024 election?"),
        ("Fed to cut rates in March 2025", "Fed Chair to be replaced in 2025"),
        ("Bitcoin to $100k", "Ethereum to $10k"),
    ]
    
    print("Testing market matching:\n")
    for text1, text2 in test_pairs:
        sig1 = create_signature(text1)
        sig2 = create_signature(text2)
        match, conf, reason = signatures_match(sig1, sig2)
        
        status = "✅ MATCH" if match else "❌ NO MATCH"
        print(f"{status} ({conf:.2f})")
        print(f"  1: {text1}")
        print(f"  2: {text2}")
        print(f"  Reason: {reason}")
        print()
