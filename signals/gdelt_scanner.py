#!/usr/bin/env python3
"""
GDELT multi-domain news scanner — structured sentiment + volume signals
for every prediction market archetype we track.

Usage:
    python3 signals/gdelt_scanner.py --all          # all domains (takes ~3 min)
    python3 signals/gdelt_scanner.py --geopolitics  # Iran, Hormuz, Ukraine, NATO
    python3 signals/gdelt_scanner.py --econ         # Fed, CPI, inflation, rates
    python3 signals/gdelt_scanner.py --crypto       # BTC, ETH, SEC, ETF
    python3 signals/gdelt_scanner.py --sports       # World Cup, Super Bowl, UFC
    python3 signals/gdelt_scanner.py --weather      # hurricanes, earthquakes
    python3 signals/gdelt_scanner.py --entertainment # Oscars, Grammys
    python3 signals/gdelt_scanner.py --elections    # original election queries
    python3 signals/gdelt_scanner.py --query "iran nuclear"  # custom query
    python3 signals/gdelt_scanner.py --report      # full report with shifts
"""

import json, time, sys, argparse
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlencode, quote
import urllib.request

from loguru import logger

GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"
CACHE_DIR = Path(__file__).parent.parent / "storage" / "gdelt_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 7200  # 2 hours

# ── Domain query sets ──────────────────────────────────────────────────────
# Each maps to active prediction markets. Queries are GDELT DOC 2.0 syntax.
# Rate limit: ~12 req/min total. Each query = 2 calls (tone + volume).

GEOPOLITICS_QUERIES = {
    "iran_nuclear":     'iran nuclear enrichment uranium 2026',
    "strait_hormuz":    '"strait of hormuz" iran 2026',
    "iran_israel":      'iran israel conflict 2026',
    "ukraine_russia":   'ukraine russia war 2026',
    "nato_europe":      'nato europe defense 2026',
    "middle_east":      'middle east conflict 2026',
}

ECON_QUERIES = {
    "fed_rate":         'federal reserve interest rate cut 2026',
    "cpi_inflation":    'cpi inflation 2026',
    "gdp_growth":       'gdp economic growth 2026',
    "recession":        'recession economic outlook 2026',
    "oil_prices":       'crude oil price 2026',
    "gas_prices":       'gas prices 2026',
}

CRYPTO_QUERIES = {
    "bitcoin":          'bitcoin btc price regulation 2026',
    "ethereum":         'ethereum eth 2026',
    "sec_crypto":       'sec cryptocurrency regulation 2026',
    "crypto_etf":       'bitcoin etf crypto etf 2026',
    "defi":             'defi decentralized finance 2026',
}

SPORTS_QUERIES = {
    "world_cup":        'fifa world cup 2026',
    "super_bowl":       'super bowl 2026',
    "ufc":              'ufc fight 2026',
    "nba_finals":       'nba finals 2026',
    "mlb":              'mlb baseball 2026',
}

WEATHER_QUERIES = {
    "hurricane":        'hurricane atlantic 2026',
    "earthquake":       'earthquake 2026',
    "extreme_weather":  'extreme weather climate 2026',
}

ENTERTAINMENT_QUERIES = {
    "oscars":           'oscars academy awards 2026',
    "grammys":          'grammy awards 2026',
    "box_office":       'box office movie 2026',
}

# ── Core GDELT client ──────────────────────────────────────────────────────

def _gdelt_get(query: str, mode: str = "timelinetone", timespan: str = "7d",
               timeout: int = 20) -> dict | list | None:
    """GET request to GDELT DOC 2.0 API with file-based caching."""
    params = {
        "query": query,
        "mode": mode,
        "format": "json",
        "timespan": timespan,
    }
    url = f"{GDELT_API}?{urlencode(params, quote_via=quote)}"
    cache_key = f"gdelt_{mode}_{query[:60]}_{timespan}".replace(" ", "_").replace("/", "_").replace('"', "")[:120]
    cache_path = CACHE_DIR / f"{cache_key}.json"
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            with open(cache_path) as f:
                return json.load(f)

    req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                content_type = resp.headers.get("Content-Type", "")
                if "text/html" in content_type:
                    logger.warning("GDELT returned HTML for: {}", query[:50])
                    if cache_path.exists():
                        with open(cache_path) as f:
                            return json.load(f)
                    return None
                data = json.loads(raw)
            with open(cache_path, "w") as f:
                json.dump(data, f)
            return data
        except Exception as e:
            is_429 = "429" in str(e) or "Too Many" in str(e)
            if is_429 and attempt < max_retries - 1:
                backoff = (attempt + 1) * 8
                time.sleep(backoff)
                continue
            logger.warning("GDELT error for '{}': {}", query[:50], e)
            if cache_path.exists():
                with open(cache_path) as f:
                    return json.load(f)
            return None


def _extract_tone_series(data):
    if not data or "timeline" not in data:
        return []
    for series in data["timeline"]:
        if "data" in series:
            return series["data"]
    return []


def _extract_volume_series(data):
    if not data or "timeline" not in data:
        return []
    for series in data["timeline"]:
        name = series.get("series", "")
        if name.lower() == "all articles":
            continue
        if "data" in series:
            return series["data"]
    return []


def _avg_tone(series):
    if not series:
        return 0.0
    vals = [pt.get("value", 0) for pt in series]
    return sum(vals) / len(vals) if vals else 0.0


def _tone_trend(series):
    """Positive = sentiment improving, Negative = deteriorating."""
    if len(series) < 4:
        return 0.0
    mid = len(series) // 2
    older = [pt.get("value", 0) for pt in series[:mid]]
    recent = [pt.get("value", 0) for pt in series[mid:]]
    if not older or not recent:
        return 0.0
    return (sum(recent) / len(recent)) - (sum(older) / len(older))


def _volume_spike(series):
    """Detect if the most recent data point is a significant volume spike.
    Returns (spike_ratio, is_spiking) where spike_ratio > 2.0 = significant."""
    if not series or len(series) < 3:
        return 0.0, False
    vals = [pt.get("value", 0) for pt in series]
    recent = vals[-1]
    prior = vals[:-1]
    mean = sum(prior) / len(prior) if prior else 1
    if mean < 1:
        return 0.0, False
    ratio = recent / mean
    return round(ratio, 2), ratio > 2.0


# ── Domain scanners ────────────────────────────────────────────────────────

def scan_domain(name: str, queries: dict, timespan: str = "7d",
                sleep_s: float = 15) -> list[dict]:
    """Scan a domain's queries and return structured results."""
    results = []
    for label, query in queries.items():
        tone_data = _gdelt_get(query, mode="timelinetone", timespan=timespan)
        time.sleep(8)  # pace between tone and volume calls
        vol_data = _gdelt_get(query, mode="timelinevolraw", timespan=timespan)

        tone_series = _extract_tone_series(tone_data)
        vol_series = _extract_volume_series(vol_data)

        avg = _avg_tone(tone_series)
        trend = _tone_trend(tone_series)
        total_articles = sum(pt.get("value", 0) for pt in vol_series)
        spike_ratio, is_spiking = _volume_spike(vol_series)

        results.append({
            "domain": name,
            "label": label,
            "query": query,
            "avg_tone": round(avg, 3),
            "tone_trend": round(trend, 3),
            "total_articles": int(total_articles),
            "volume_spike_ratio": spike_ratio,
            "is_volume_spiking": is_spiking,
            "data_points": len(tone_series),
            "timespan": timespan,
        })
        time.sleep(sleep_s)
    return results


# ── Narrative shift detection ─────────────────────────────────────────────

def detect_shifts(all_results: list[dict]) -> list[dict]:
    """Detect significant narrative shifts across all domains."""
    shifts = []
    for r in all_results:
        # Volume spike = breaking news
        if r["is_volume_spiking"]:
            shifts.append({
                "type": "volume_spike",
                "domain": r["domain"],
                "label": r["label"],
                "magnitude": r["volume_spike_ratio"],
                "articles": r["total_articles"],
                "detail": (
                    f"Volume spike ({r['volume_spike_ratio']}x normal) for "
                    f"'{r['label']}' — {r['total_articles']} articles in 7d"
                ),
            })
        # Tone trend shift
        trend = r["tone_trend"]
        if abs(trend) >= 0.8:
            direction = "improving" if trend > 0 else "deteriorating"
            shifts.append({
                "type": "tone_shift",
                "domain": r["domain"],
                "label": r["label"],
                "direction": direction,
                "magnitude": abs(trend),
                "avg_tone": r["avg_tone"],
                "articles": r["total_articles"],
                "detail": (
                    f"Sentiment {direction} for '{r['label']}' "
                    f"(trend: {trend:+.2f}, avg tone: {r['avg_tone']:.2f})"
                ),
            })
    shifts.sort(key=lambda x: x["magnitude"], reverse=True)
    return shifts


# ── CLI ────────────────────────────────────────────────────────────────────

DOMAIN_MAP = {
    "geopolitics": ("Geopolitics", GEOPOLITICS_QUERIES),
    "econ": ("Economics", ECON_QUERIES),
    "crypto": ("Crypto", CRYPTO_QUERIES),
    "sports": ("Sports", SPORTS_QUERIES),
    "weather": ("Weather/Disasters", WEATHER_QUERIES),
    "entertainment": ("Entertainment", ENTERTAINMENT_QUERIES),
    "elections": ("Elections", {
        "senate_dem": '("democrat" OR "democratic") senate 2026 sourcecountry:US',
        "senate_gop": '("republican" OR "GOP") senate 2026 sourcecountry:US',
        "midterms": '"2026 election" OR "2026 midterm" sourcecountry:US',
    }),
}

ALL_DOMAINS = list(DOMAIN_MAP.keys())


def print_results(results, shifts=None):
    """Pretty-print scan results grouped by domain."""
    by_domain = {}
    for r in results:
        by_domain.setdefault(r["domain"], []).append(r)

    for domain, items in by_domain.items():
        print(f"\n{'='*60}")
        print(f"  {domain.upper()}")
        print(f"{'='*60}")
        for r in items:
            spike = " 🔥 SPIKE" if r["is_volume_spiking"] else ""
            trend_str = f"trend={r['tone_trend']:+.2f}" if abs(r['tone_trend']) >= 0.3 else ""
            print(f"  {r['label']:20s} | tone={r['avg_tone']:+.2f} | {trend_str:20s} | {r['total_articles']:>4} articles{spike}")

    if shifts:
        print(f"\n{'='*60}")
        print("  NARRATIVE SHIFTS")
        print(f"{'='*60}")
        for s in shifts[:10]:
            icon = "🔥" if s["type"] == "volume_spike" else "📊"
            print(f"  {icon} {s['detail']}")


def main():
    parser = argparse.ArgumentParser(description="GDELT multi-domain news scanner")
    parser.add_argument("--all", action="store_true", help="Scan all domains")
    parser.add_argument("--geopolitics", action="store_true")
    parser.add_argument("--econ", action="store_true")
    parser.add_argument("--crypto", action="store_true")
    parser.add_argument("--sports", action="store_true")
    parser.add_argument("--weather", action="store_true")
    parser.add_argument("--entertainment", action="store_true")
    parser.add_argument("--elections", action="store_true")
    parser.add_argument("--query", help="Custom GDELT query")
    parser.add_argument("--report", action="store_true", help="Full report with shifts")
    parser.add_argument("--timespan", default="7d", help="GDELT timespan (7d, 1m, etc)")
    parser.add_argument("--json", help="Export results to JSON file")

    args = parser.parse_args()

    # Determine which domains to scan
    domains_to_scan = []
    if args.all:
        domains_to_scan = ALL_DOMAINS
    else:
        for d in ALL_DOMAINS:
            if getattr(args, d, False):
                domains_to_scan.append(d)

    # Custom query
    if args.query:
        print(f"\n{'='*60}")
        print(f"  CUSTOM QUERY: {args.query}")
        print(f"{'='*60}")
        r = scan_domain("custom", {"custom": args.query}, args.timespan)
        print_results(r)
        if args.json:
            with open(args.json, "w") as f:
                json.dump(r, f, indent=2)
            print(f"\nWrote to {args.json}")
        return

    if not domains_to_scan:
        parser.print_help()
        return

    # Scan
    all_results = []
    for d in domains_to_scan:
        name, queries = DOMAIN_MAP[d]
        print(f"\nScanning {name}...")
        results = scan_domain(d, queries, args.timespan)
        all_results.extend(results)

    # Detect shifts
    shifts = detect_shifts(all_results) if args.report else None

    # Print
    print_results(all_results, shifts)

    # Export
    if args.json:
        out = {"results": all_results}
        if shifts:
            out["shifts"] = shifts
        with open(args.json, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nWrote {len(all_results)} results to {args.json}")

    # Summary
    spiking = [r for r in all_results if r["is_volume_spiking"]]
    if spiking:
        print(f"\n{'='*60}")
        print(f"  🔥 VOLUME SPIKES DETECTED ({len(spiking)})")
        print(f"{'='*60}")
        for r in spiking:
            print(f"  {r['domain']:15s} | {r['label']:20s} | {r['volume_spike_ratio']}x normal | {r['total_articles']} articles")


if __name__ == "__main__":
    main()
