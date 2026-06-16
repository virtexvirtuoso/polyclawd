#!/usr/bin/env python3
"""
Super Bowl archive scraper — pulls closing lines for all 60 Super Bowls
from Covers Sports Odds History.

Usage:
    python3 scripts/super_bowl_scraper.py                        # print table
    python3 scripts/super_bowl_scraper.py --csv super_bowl.csv    # export CSV
    python3 scripts/super_bowl_scraper.py --json super_bowl.json  # export JSON
    python3 scripts/super_bowl_scraper.py --sqlite                # insert into db
"""

import re, sys, json, csv, argparse
from datetime import datetime
import requests
from bs4 import BeautifulSoup

URL = "https://www.covers.com/sportsoddshistory/nfl-super-bowl/"


def parse_super_bowls() -> list[dict]:
    resp = requests.get(URL, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # The table has multiple <tbody> sections with <tr class="table-row">
    # Each row: Season/SB, Date, Location, Favorite, Spread, Underdog, O/U, Winner, Score, ATS Result
    rows = soup.select("tr.table-row")
    if not rows:
        # fallback: try all trs inside a table
        tables = soup.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            if len(rows) > 5:
                break

    bowls = []
    for tr in rows:
        cols = tr.find_all("td")
        if len(cols) < 10:
            continue

        # Extract text, cleaning up
        cells = []
        for c in cols:
            txt = c.get_text(strip=True)
            # Merge multi-line cells (ATS Result often spans 2 lines)
            txt = re.sub(r'\s+', ' ', txt).strip()
            cells.append(txt)

        season_raw = cells[0]  # e.g. "2025LX" or "2024LIX"
        # Parse season and SB number
        m = re.match(r'(\d{4})\s*([A-Z]+|\d+)', season_raw)
        if not m:
            continue
        season = int(m.group(1))
        sb = m.group(2)

        date_str = cells[1]
        location = cells[2]
        favorite = cells[3].replace(" (", " (").strip()
        spread = cells[4]
        underdog = cells[5].replace(" (", " (").strip()
        over_under = cells[6]
        winner = cells[7]
        score = cells[8]
        ats = cells[9]  # "Favorite-\nUnder" or "Underdog-\nOver"

        # Parse ATS result into winner_type (fav/underdog/push) and ou_type
        ats_parts = ats.split("-")
        winner_type = ats_parts[0].strip() if len(ats_parts) > 0 else ""
        ou_type = ats_parts[1].strip() if len(ats_parts) > 1 else ""

        # Parse score into winner_score, loser_score
        score_parts = score.split("-")
        winner_score = int(score_parts[0].strip()) if score_parts[0].strip().isdigit() else None
        loser_score = int(score_parts[1].strip()) if len(score_parts) > 1 and score_parts[1].strip().isdigit() else None

        # Parse spread
        spread_val = float(spread) if spread else None

        # Parse O/U
        ou_val = float(over_under) if over_under else None

        # Determiner winner: favorite or underdog won
        if winner_type.lower() == "favorite":
            fav_won = True
        elif winner_type.lower() == "underdog":
            fav_won = False
        else:
            fav_won = None  # push

        bowl = {
            "season": season,
            "super_bowl": sb,
            "date": date_str,
            "location": location,
            "favorite": favorite,
            "spread": spread_val,
            "underdog": underdog,
            "over_under": ou_val,
            "winner": winner,
            "winner_score": winner_score,
            "loser_score": loser_score,
            "total_score": (winner_score + loser_score) if winner_score is not None and loser_score is not None else None,
            "fav_won_ats": fav_won,
            "over_under_result": ou_type,
        }
        bowls.append(bowl)

    return bowls


def print_table(bowls):
    """Pretty-print the archive."""
    print(f"{'Season':<8} {'SB':<6} {'Date':<14} {'Favorite':<30} {'Spread':<8} {'Underdog':<30} {'O/U':<6} {'Result':<8} {'ATS':<10}")
    print("-" * 140)
    for b in bowls:
        seen = b["season"]
        print(
            f"{seen:<8} "
            f"{b['super_bowl']:<6} "
            f"{b['date']:<14} "
            f"{b['favorite'][:28]:<30} "
            f"{b['spread'] if b['spread'] else '':<8} "
            f"{b['underdog'][:28]:<30} "
            f"{b['over_under'] if b['over_under'] else '':<6} "
            f"{b['winner_score']}-{b['loser_score']:<4} "
            f"{b['fav_won_ats'] if b['fav_won_ats'] is not None else 'push':<10}"
        )
    print(f"\nTotal: {len(bowls)} Super Bowls")


def to_csv(bowls, path):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=bowls[0].keys())
        w.writeheader()
        w.writerows(bowls)
    print(f"Wrote {len(bowls)} rows to {path}")


def to_json(bowls, path):
    with open(path, "w") as f:
        json.dump(bowls, f, indent=2)
    print(f"Wrote {len(bowls)} rows to {path}")


def to_sqlite(bowls):
    import sqlite3
    DB = "storage/shadow_trades.db"
    conn = sqlite3.connect(DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS super_bowl_archive (
            season INTEGER PRIMARY KEY,
            super_bowl TEXT,
            date TEXT,
            location TEXT,
            favorite TEXT,
            spread REAL,
            underdog TEXT,
            over_under REAL,
            winner TEXT,
            winner_score INTEGER,
            loser_score INTEGER,
            total_score INTEGER,
            fav_won_ats TEXT,
            over_under_result TEXT,
            fetched_at TEXT
        )
    """)
    now = datetime.utcnow().isoformat()
    for b in bowls:
        b["fav_won_ats"] = str(b.get("fav_won_ats")) if b.get("fav_won_ats") is not None else "push"
        b["fetched_at"] = now
        conn.execute("""
            INSERT OR REPLACE INTO super_bowl_archive
            (season, super_bowl, date, location, favorite, spread, underdog,
             over_under, winner, winner_score, loser_score, total_score,
             fav_won_ats, over_under_result, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            b["season"], b["super_bowl"], b["date"], b["location"],
            b["favorite"], b["spread"], b["underdog"], b["over_under"],
            b["winner"], b["winner_score"], b["loser_score"], b["total_score"],
            b["fav_won_ats"], b["over_under_result"], b["fetched_at"]
        ))
    conn.commit()
    conn.close()
    print(f"Inserted {len(bowls)} rows into {DB}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape Super Bowl archive from Covers")
    parser.add_argument("--csv", help="Export to CSV file")
    parser.add_argument("--json", help="Export to JSON file")
    parser.add_argument("--sqlite", action="store_true", help="Insert into shadow_trades.db")
    parser.add_argument("--no-print", action="store_true", help="Skip printing (quiet mode)")
    args = parser.parse_args()

    bowls = parse_super_bowls()
    if not bowls:
        print("ERROR: No Super Bowls parsed. The page structure may have changed.", file=sys.stderr)
        sys.exit(1)

    if not args.no_print:
        print_table(bowls)

    if args.csv:
        to_csv(bowls, args.csv)
    if args.json:
        to_json(bowls, args.json)
    if args.sqlite:
        to_sqlite(bowls)