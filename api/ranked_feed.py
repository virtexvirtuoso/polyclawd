#!/usr/bin/env python3
"""Step 0b: Ranked alert feed — deploy as system service."""
import json, math, os, sys
from db import connect as db_connect
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META_DB = os.path.join(BASE, "storage/whale_meta.db")
HOST = "127.0.0.1"
PORT = 8421

WEIGHTS = {
    "flow_size": 0.30,
    "wallet_reputation": 0.25,
    "spread": 0.15,
    "urgency": 0.15,
    "archetype_bonus": 0.15,
}

def get_db():
    conn = db_connect(META_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def score_alert(row):
    score = 0.0
    components = {}
    
    fd = row["flow_dollars"] or 0
    if fd >= 100000:
        fs = 1.0
    elif fd >= 50000:
        fs = 0.8
    elif fd >= 25000:
        fs = 0.6
    elif fd >= 10000:
        fs = 0.4
    else:
        fs = 0.2
    score += fs * WEIGHTS["flow_size"]
    components["flow_size"] = round(fs, 2)
    
    wr = row["wallet_win_rate"]
    if wr is not None and wr >= 0.65:
        ws = 1.0
    elif wr is not None and wr >= 0.55:
        ws = 0.7
    elif wr is not None and wr >= 0.45:
        ws = 0.4
    elif wr is not None:
        ws = 0.2
    else:
        ws = 0.3
    score += ws * WEIGHTS["wallet_reputation"]
    components["wallet_rep"] = round(ws, 2)
    
    sp = row["spread_bps"]
    if sp is not None and sp > 0:
        if sp < 20:
            ss = 1.0
        elif sp < 50:
            ss = 0.7
        elif sp < 100:
            ss = 0.4
        elif sp < 200:
            ss = 0.2
        else:
            ss = 0.1
    else:
        ss = 0.5
    score += ss * WEIGHTS["spread"]
    components["spread"] = round(ss, 2)
    
    htr = row["hours_to_resolve"]
    if htr is not None and htr > 0:
        if htr < 2:
            us = 1.0
        elif htr < 6:
            us = 0.8
        elif htr < 24:
            us = 0.5
        elif htr < 72:
            us = 0.3
        else:
            us = 0.1
    else:
        us = 0.3
    score += us * WEIGHTS["urgency"]
    components["urgency"] = round(us, 2)
    
    arch = row["market_archetype"] or ""
    arch_bonus = {"weather": 1.0, "election": 0.8, "sports": 0.7, "deadline_binary": 0.6, "other": 0.5, "index": 0.3}
    ab = arch_bonus.get(arch, 0.3)
    score += ab * WEIGHTS["archetype_bonus"]
    components["archetype"] = round(ab, 2)
    
    return round(score, 3), components


def get_top_alerts(limit=10, min_score=0, severity_filter=None, platform_filter=None):
    conn = get_db()
    
    # Get recent open alerts with flow data, ordered by score components
    where = ["done = 0", "flow_dollars > 0", "severity IN ('CRITICAL', 'HIGH', 'LOW')"]
    params = []
    
    if severity_filter:
        where.append("severity = ?")
        params.append(severity_filter.upper())
    if platform_filter:
        where.append("platform = ?")
        params.append(platform_filter.lower())
    
    where_clause = " AND ".join(where)
    
    rows = conn.execute(
        "SELECT * FROM whale_outcomes WHERE %s ORDER BY alert_id DESC LIMIT 500" % where_clause
    ).fetchall()
    
    scored = []
    seen_markets = set()
    for row in rows:
        # Deduplicate by market (keep highest-scoring alert per market)
        market = row["market"]
        s, components = score_alert(row)
        if s < min_score:
            continue
        
        # Skip already-closed markets
        htr = row["hours_to_resolve"]
        if htr is not None and htr < 0:
            continue
        
        wallet_short = ""
        if row["top_wallet"]:
            w = row["top_wallet"]
            wallet_short = w[:10] + "..." if len(w) > 16 else w
        
        entry = {
            "rank": 0,
            "alert_id": row["alert_id"],
            "platform": row["platform"],
            "market": row["market"],
            "severity": row["severity"],
            "direction": row["direction"],
            "price": row["price_at_alert"],
            "flow_dollars": row["flow_dollars"],
            "wallet": wallet_short,
            "wallet_name": row["top_wallet_name"] or "",
            "wallet_win_rate": row["wallet_win_rate"],
            "spread_bps": row["spread_bps"],
            "hours_to_resolve": row["hours_to_resolve"],
            "market_archetype": row["market_archetype"],
            "composite_score": s,
            "components": components,
            "url": ("https://polymarket.com/market/" + row["market"]) if row["platform"] == "polymarket" else ("https://kalshi.com/markets/" + row["market"].split("-")[0]),
        }
        
        if market not in seen_markets:
            seen_markets.add(market)
            scored.append(entry)
        elif entry["composite_score"] > next((x["composite_score"] for x in scored if x["market"] == market), 0):
            # Replace with higher-scoring version
            scored = [x for x in scored if x["market"] != market]
            scored.append(entry)
    
    scored.sort(key=lambda x: x["composite_score"], reverse=True)
    for i, a in enumerate(scored[:limit]):
        a["rank"] = i + 1
    
    conn.close()
    return scored[:limit]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        
        self.send_header("Access-Control-Allow-Origin", "*")
        
        if parsed.path == "/api/whale-top":
            limit = min(int(params.get("limit", ["10"])[0]), 50)
            min_score = float(params.get("min_score", ["0"])[0])
            severity = params.get("severity", [None])[0]
            platform = params.get("platform", [None])[0]
            
            alerts = get_top_alerts(limit=limit, min_score=min_score,
                                    severity_filter=severity, platform_filter=platform)
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            
            response = {
                "count": len(alerts),
                "weights": WEIGHTS,
                "alerts": alerts,
                "note": "Composite score 0-1. Flat priors from literature. Auto-tuning starts after 200+ resolved alerts."
            }
            self.wfile.write(json.dumps(response, indent=2).encode())
        
        elif parsed.path == "/api/whale-precision":
            conn = get_db()
            results = {}
            
            for label, group_col, table in [
                ("by_severity", "severity", None),
                ("by_platform", "platform", None),
                ("by_archetype", "market_archetype", None),
            ]:
                rows = conn.execute("""
                    SELECT %s, COUNT(*),
                           SUM(CASE WHEN correct_res IS NOT NULL THEN 1 ELSE 0 END),
                           ROUND(AVG(CASE WHEN correct_res IS NOT NULL THEN correct_res END), 3)
                    FROM whale_outcomes WHERE direction IS NOT NULL AND %s IS NOT NULL
                    GROUP BY %s ORDER BY COUNT(*) DESC
                """ % (group_col, group_col, group_col)).fetchall()
                results[label] = [{"name": r[0], "total": r[1], "resolved": r[2], "precision": r[3]} for r in rows]
            
            # Flow size
            rows = conn.execute("""
                SELECT CASE 
                    WHEN flow_dollars < 5000 THEN 'under_5K'
                    WHEN flow_dollars < 25000 THEN '5K_25K'
                    WHEN flow_dollars < 100000 THEN '25K_100K'
                    ELSE 'over_100K' END,
                    COUNT(*),
                    SUM(CASE WHEN correct_res IS NOT NULL THEN 1 ELSE 0 END),
                    ROUND(AVG(CASE WHEN correct_res IS NOT NULL THEN correct_res END), 3)
                FROM whale_outcomes WHERE direction IS NOT NULL AND flow_dollars IS NOT NULL
                GROUP BY 1 ORDER BY MIN(flow_dollars)
            """).fetchall()
            results["by_flow_size"] = [{"bucket": r[0], "total": r[1], "resolved": r[2], "precision": r[3]} for r in rows]
            
            conn.close()
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(results, indent=2).encode())
        
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found. Try /api/whale-top or /api/whale-precision")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        alerts = get_top_alerts(limit=5)
        print("=== TOP 5 ALERTS ===")
        for a in alerts:
            print("  #%d %s %.3f flow=$%d wr=%s spread=%s" % (
                a["rank"], a["market"][:50], a["composite_score"],
                a["flow_dollars"], a["wallet_win_rate"], a["spread_bps"]
            ))
        print("\n=== PRECISION ===")
        conn = get_db()
        for label in ["by_severity", "by_platform"]:
            print("--- %s ---" % label)
        conn.close()
    else:
        server = HTTPServer((HOST, PORT), Handler)
        print("Ranked feed API: http://%s:%d" % (HOST, PORT))
        print("  GET /api/whale-top")
        print("  GET /api/whale-precision")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.server_close()