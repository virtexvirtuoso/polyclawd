"""
Cross-Market Correlation Analysis

Detects related markets and enforces probability constraints.
Flags violations as arbitrage opportunities.

Examples:
- P(Chiefs win Super Bowl) <= P(Chiefs win AFC)
- P(Player wins MVP) <= P(Team makes playoffs)
- P(Candidate wins) <= P(Candidate wins primary)
"""

import re
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from datetime import datetime


@dataclass
class MarketPair:
    """A pair of related markets with a constraint."""
    parent_market: str  # The broader outcome (AFC win)
    parent_price: float
    child_market: str   # The narrower outcome (SB win)
    child_price: float
    constraint: str     # "child <= parent"
    violation: float    # How much constraint is violated (0 = no violation)
    arb_opportunity: bool
    entity: str         # Common entity (team name, person name)


# Entity extraction patterns
TEAM_PATTERNS = [
    # NFL
    r'\b(Chiefs|Eagles|49ers|Ravens|Bills|Cowboys|Dolphins|Lions|Packers|'
    r'Jets|Patriots|Bengals|Browns|Steelers|Commanders|Giants|Saints|'
    r'Buccaneers|Falcons|Panthers|Bears|Vikings|Seahawks|Rams|Cardinals|'
    r'Chargers|Raiders|Broncos|Texans|Colts|Titans|Jaguars)\b',
    # NBA
    r'\b(Lakers|Celtics|Warriors|Bucks|Nuggets|Heat|Suns|76ers|Nets|'
    r'Clippers|Mavericks|Grizzlies|Pelicans|Kings|Cavaliers|Knicks|'
    r'Bulls|Hawks|Raptors|Thunder|Timberwolves|Rockets|Spurs|Magic|'
    r'Pacers|Hornets|Wizards|Pistons|Trail Blazers|Jazz)\b',
    # MLB
    r'\b(Yankees|Dodgers|Astros|Braves|Mets|Phillies|Padres|Cardinals|'
    r'Mariners|Blue Jays|Guardians|Orioles|Rangers|Twins|Rays|'
    r'Red Sox|Cubs|Brewers|Giants|Diamondbacks|Marlins|Reds|Pirates|'
    r'Rockies|Tigers|Royals|White Sox|Angels|Athletics|Nationals)\b',
]

PERSON_PATTERNS = [
    # Politicians
    r'\b(Trump|Biden|Harris|DeSantis|Haley|Newsom|Obama|Clinton)\b',
    # Athletes (common MVP candidates)
    r'\b(Mahomes|Hurts|Jackson|Allen|Burrow|Herbert|Kelce|Hill|'
    r'LeBron|Curry|Jokic|Embiid|Giannis|Doncic|Tatum|Durant|'
    r'Ohtani|Judge|Acuna|Soto|Betts|Trout)\b',
]

# Constraint templates: (parent_pattern, child_pattern, constraint_type)
CONSTRAINT_TEMPLATES = [
    # Sports championships
    (r'win (AFC|NFC)', r'win Super Bowl', 'subset'),
    (r'win (AL|NL)', r'win World Series', 'subset'),
    (r'win (East|West)ern Conference', r'win (NBA )?Championship', 'subset'),
    (r'make playoffs', r'win (Championship|Super Bowl|World Series)', 'subset'),
    (r'win division', r'win (Championship|Super Bowl|World Series)', 'subset'),
    # Politics
    (r'win (primary|nomination)', r'win (election|presidency)', 'subset'),
    (r'win (Iowa|New Hampshire|South Carolina)', r'win nomination', 'subset'),
    # Awards
    (r'(nominated|make.*final)', r'win.*award', 'subset'),
]


def extract_entities(text: str) -> List[str]:
    """Extract team names, person names from market title."""
    entities = []
    text_lower = text.lower()
    
    for pattern in TEAM_PATTERNS + PERSON_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        entities.extend(matches)
    
    return list(set(entities))


def find_constraint_type(parent_title: str, child_title: str) -> Optional[str]:
    """Determine if there's a logical constraint between markets."""
    parent_lower = parent_title.lower()
    child_lower = child_title.lower()
    
    for parent_pattern, child_pattern, constraint in CONSTRAINT_TEMPLATES:
        if re.search(parent_pattern, parent_lower) and re.search(child_pattern, child_lower):
            return constraint
        # Also check reverse (in case markets are swapped)
        if re.search(child_pattern, parent_lower) and re.search(parent_pattern, child_lower):
            return constraint
    
    return None


def group_markets_by_entity(markets: List[Dict]) -> Dict[str, List[Dict]]:
    """Group markets by common entities (teams, people)."""
    entity_markets = {}
    
    for market in markets:
        title = market.get('title', '') or market.get('question', '')
        entities = extract_entities(title)
        
        for entity in entities:
            entity_key = entity.lower()
            if entity_key not in entity_markets:
                entity_markets[entity_key] = []
            entity_markets[entity_key].append({
                **market,
                'extracted_entity': entity
            })
    
    return entity_markets


def detect_constraint_violations(markets: List[Dict]) -> List[MarketPair]:
    """
    Find related markets and check for constraint violations.
    
    Returns list of market pairs with violations (arb opportunities).
    """
    violations = []
    entity_groups = group_markets_by_entity(markets)
    
    for entity, entity_markets in entity_groups.items():
        if len(entity_markets) < 2:
            continue
        
        # Compare all pairs within this entity
        for i, m1 in enumerate(entity_markets):
            for m2 in entity_markets[i+1:]:
                title1 = m1.get('title', '') or m1.get('question', '')
                title2 = m2.get('title', '') or m2.get('question', '')
                
                # Get prices
                price1 = _get_price(m1)
                price2 = _get_price(m2)
                
                if price1 is None or price2 is None:
                    continue
                
                # Determine which is parent (broader) and child (narrower)
                constraint = find_constraint_type(title1, title2)
                
                if constraint == 'subset':
                    # Determine parent/child based on title keywords
                    if _is_broader_outcome(title1, title2):
                        parent, parent_price = title1, price1
                        child, child_price = title2, price2
                    else:
                        parent, parent_price = title2, price2
                        child, child_price = title1, price1
                    
                    # Check constraint: child <= parent
                    violation = max(0, child_price - parent_price)
                    
                    if violation > 0.01:  # 1% minimum violation
                        violations.append(MarketPair(
                            parent_market=parent,
                            parent_price=parent_price,
                            child_market=child,
                            child_price=child_price,
                            constraint="P(child) <= P(parent)",
                            violation=round(violation * 100, 2),
                            arb_opportunity=violation > 0.03,  # 3% = actionable
                            entity=entity.title()
                        ))
    
    return violations


def _get_price(market: Dict) -> Optional[float]:
    """Extract YES price from market data."""
    import json as _json
    
    # Try various field names
    for field in ['yes_price', 'outcomePrices', 'probability', 'lastTradePrice']:
        if field in market:
            val = market[field]
            
            # Handle JSON string (Polymarket returns outcomePrices as JSON string)
            if isinstance(val, str) and val.startswith('['):
                try:
                    val = _json.loads(val)
                except:
                    pass
            
            if isinstance(val, list) and len(val) > 0:
                val = val[0]
            if isinstance(val, (int, float)):
                return float(val) if val <= 1 else float(val) / 100
            if isinstance(val, str):
                try:
                    v = float(val)
                    return v if v <= 1 else v / 100
                except:
                    pass
    return None


def _is_broader_outcome(title1: str, title2: str) -> bool:
    """Determine if title1 represents a broader outcome than title2."""
    broader_keywords = ['conference', 'division', 'playoff', 'primary', 'nomination', 'nominated']
    narrower_keywords = ['super bowl', 'championship', 'world series', 'election', 'presidency', 'win award']
    
    t1_lower = title1.lower()
    t2_lower = title2.lower()
    
    t1_broader = any(kw in t1_lower for kw in broader_keywords)
    t1_narrower = any(kw in t1_lower for kw in narrower_keywords)
    t2_broader = any(kw in t2_lower for kw in broader_keywords)
    t2_narrower = any(kw in t2_lower for kw in narrower_keywords)
    
    if t1_broader and t2_narrower:
        return True
    if t2_broader and t1_narrower:
        return False
    
    # Default: longer title is usually more specific (child)
    return len(title1) < len(title2)


def scan_correlation_arb(markets: List[Dict], min_violation_pct: float = 3.0) -> Dict:
    """
    Main entry point: scan markets for correlation-based arbitrage.
    
    Args:
        markets: List of market dicts with title and price
        min_violation_pct: Minimum constraint violation to report (default 3%)
    
    Returns:
        Dict with violations and summary stats
    """
    all_violations = detect_constraint_violations(markets)
    
    actionable = [v for v in all_violations if v.violation >= min_violation_pct]
    
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "total_markets_scanned": len(markets),
        "entity_groups_found": len(group_markets_by_entity(markets)),
        "total_violations": len(all_violations),
        "actionable_violations": len(actionable),
        "violations": [
            {
                "entity": v.entity,
                "parent_market": v.parent_market,
                "parent_price": f"{v.parent_price:.1%}",
                "child_market": v.child_market,
                "child_price": f"{v.child_price:.1%}",
                "constraint": v.constraint,
                "violation_pct": v.violation,
                "arb_opportunity": v.arb_opportunity,
                "action": f"Buy NO on child ({v.child_market[:50]}...) at {v.child_price:.1%}"
                         if v.arb_opportunity else "Monitor"
            }
            for v in sorted(actionable, key=lambda x: -x.violation)
        ]
    }


# Quick test
if __name__ == "__main__":
    test_markets = [
        {"title": "Will the Chiefs win the AFC Championship?", "yes_price": 0.45},
        {"title": "Will the Chiefs win Super Bowl LX?", "yes_price": 0.52},  # Violation!
        {"title": "Will the Eagles win the NFC Championship?", "yes_price": 0.30},
        {"title": "Will the Eagles win Super Bowl LX?", "yes_price": 0.22},  # OK
        {"title": "Will Trump win the Republican nomination?", "yes_price": 0.85},
        {"title": "Will Trump win the 2024 election?", "yes_price": 0.90},  # Violation!
    ]
    
    result = scan_correlation_arb(test_markets)
    import json
    print(json.dumps(result, indent=2))
