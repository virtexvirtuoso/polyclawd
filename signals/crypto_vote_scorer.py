#!/usr/bin/env python3
"""Crypto-alignment scoring for members of Congress.

Joins three roll-call votes — FIT21 (House roll 226), CBDC Anti-Surveillance
Act (House roll 230), SAB 121 override (Senate vote 169) — into a single
crypto-alignment score per member. The score is the share of "pro-crypto"
votes cast on bills where the member was present.

"Pro-crypto" direction per vote:
    FIT21 (HR 4763)       — YES = pro-crypto (industry supported passage)
    CBDC Anti-Surv (HR 5403) — YES = pro-crypto (blocks Fed-issued digital dollar)
    SAB 121 (HJR 109)     — YES = pro-crypto (overrides SEC custody guidance)

The result is merged into FEC top_recipients by last-name + state + party, so
the dashboard can render a "Money vs. Votes" card showing whether the dollars
actually bought aligned votes.
"""

import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from loguru import logger

CACHE_DIR = Path(__file__).parent.parent / "storage" / "crypto_votes_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 30 * 24 * 3600  # 30 days — historical roll calls are immutable

TRACKED_VOTES = [
    {
        "key": "fit21",
        "label": "FIT21",
        "bill": "H.R. 4763",
        "url": "https://clerk.house.gov/evs/2024/roll226.xml",
        "chamber": "house",
        "pro_crypto_vote": "Aye",
        "date": "2024-05-22",
    },
    {
        "key": "cbdc_anti_surv",
        "label": "CBDC Anti-Surv",
        "bill": "H.R. 5403",
        "url": "https://clerk.house.gov/evs/2024/roll230.xml",
        "chamber": "house",
        "pro_crypto_vote": "Aye",
        "date": "2024-05-23",
    },
    {
        "key": "sab121_override",
        "label": "SAB 121 Override",
        "bill": "H.J. Res. 109",
        "url": "https://www.senate.gov/legislative/LIS/roll_call_votes/vote1182/vote_118_2_00169.xml",
        "chamber": "senate",
        "pro_crypto_vote": "Yea",
        "date": "2024-05-16",
    },
]


def _fetch_cached(url: str, filename: str) -> bytes | None:
    """Fetch URL bytes with 30d file cache."""
    cache_path = CACHE_DIR / filename
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            return cache_path.read_bytes()

    req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
        cache_path.write_bytes(data)
        return data
    except Exception as e:
        logger.warning("crypto vote fetch failed for {}: {}", url, e)
        if cache_path.exists():
            return cache_path.read_bytes()
        return None


def parse_house_roll(xml_bytes: bytes) -> list[dict]:
    """Parse a House Clerk roll-call XML into per-member vote rows.

    Note: when multiple members share a last name, Clerk appends " (XX)" to
    the sort-field (e.g. "Moore (AL)" vs "Moore (UT)"). Strip that suffix so
    composite keys match the bare last-name form used for FEC joining.
    """
    import re
    strip_suffix = re.compile(r"\s*\([A-Z]{2}\)\s*$")
    root = ET.fromstring(xml_bytes)
    out = []
    for rv in root.findall(".//recorded-vote"):
        leg = rv.find("legislator")
        vote = rv.find("vote")
        if leg is None or vote is None:
            continue
        last_raw = (leg.get("sort-field") or leg.get("unaccented-name") or leg.text or "").strip()
        last_clean = strip_suffix.sub("", last_raw).strip()
        out.append({
            "last_name": last_clean,
            "state": (leg.get("state") or "").strip(),
            "party": (leg.get("party") or "").strip(),
            "bioguide": (leg.get("name-id") or "").strip(),
            "vote": (vote.text or "").strip(),
        })
    return out


def parse_senate_vote(xml_bytes: bytes) -> list[dict]:
    """Parse a Senate roll-call XML into per-member vote rows."""
    root = ET.fromstring(xml_bytes)
    out = []
    for m in root.findall(".//member"):
        def txt(tag):
            el = m.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        out.append({
            "last_name": txt("last_name"),
            "first_name": txt("first_name"),
            "state": txt("state"),
            "party": txt("party"),
            "lis_id": txt("lis_member_id"),
            "vote": txt("vote_cast"),
        })
    return out


def fetch_vote_rows(spec: dict) -> list[dict]:
    """Fetch + parse a single tracked vote spec, returning member rows."""
    filename = f"{spec['chamber']}_{spec['key']}.xml"
    raw = _fetch_cached(spec["url"], filename)
    if not raw:
        return []
    if spec["chamber"] == "house":
        return parse_house_roll(raw)
    return parse_senate_vote(raw)


def _member_key(last_name: str, state: str, party: str) -> str:
    """Composite join key used to match roll-call members to FEC recipients."""
    return f"{last_name.strip().upper()}|{state.strip().upper()}|{party.strip().upper()[:1]}"


def build_vote_index() -> dict:
    """Build a lookup: member_key → {vote_key → {vote, pro_crypto: bool|None}}.

    Also returns per-vote totals for dashboard context.
    """
    member_votes: dict[str, dict] = {}
    vote_meta: dict[str, dict] = {}

    for spec in TRACKED_VOTES:
        rows = fetch_vote_rows(spec)
        yeas = sum(1 for r in rows if r["vote"] in ("Aye", "Yea"))
        nays = sum(1 for r in rows if r["vote"] in ("No", "Nay"))
        vote_meta[spec["key"]] = {
            "label": spec["label"],
            "bill": spec["bill"],
            "date": spec["date"],
            "chamber": spec["chamber"],
            "yeas": yeas,
            "nays": nays,
            "pro_crypto_vote": spec["pro_crypto_vote"],
        }

        pro = spec["pro_crypto_vote"]
        for r in rows:
            key = _member_key(r["last_name"], r["state"], r["party"])
            slot = member_votes.setdefault(key, {
                "last_name": r["last_name"],
                "state": r["state"],
                "party": r["party"],
                "chamber": spec["chamber"],
                "votes": {},
            })
            vt = r["vote"]
            if vt in ("Aye", "Yea", "No", "Nay"):
                slot["votes"][spec["key"]] = {
                    "cast": vt,
                    "pro_crypto": (vt == pro),
                }

    return {
        "member_votes": member_votes,
        "vote_meta": vote_meta,
    }


def score_member(member: dict) -> dict | None:
    """Compute a crypto alignment score from a member's vote record.

    Returns None if the member has no tracked votes (e.g. elected post-2024).
    """
    votes = member.get("votes") or {}
    if not votes:
        return None
    total = len(votes)
    pro = sum(1 for v in votes.values() if v["pro_crypto"])
    return {
        "pct": round(100.0 * pro / total, 1) if total else 0.0,
        "pro": pro,
        "total": total,
        "casts": {k: v["cast"] for k, v in votes.items()},
    }


def canonical_recipient_key(full_name: str, state: str, party: str) -> str:
    """Build the member-lookup key from an FEC recipient record.

    FEC gives 'First Middle Last' already cleaned by _clean_name. We take the
    last whitespace-separated token as the last name. Party is already 3-letter
    ('DEM', 'REP', 'IND') — we slice to one char to match roll-call format.
    """
    if not full_name:
        return ""
    last = full_name.strip().split()[-1] if full_name.strip() else ""
    return _member_key(last, state, party)


def enrich_recipients_with_votes(committees: list[dict]) -> dict:
    """Walk the FEC committees -> top_recipients list and attach vote alignment.

    Mutates each recipient dict in place with a `crypto_alignment` field if the
    recipient matched a tracked roll call. Returns summary stats.
    """
    idx = build_vote_index()
    members = idx["member_votes"]
    matched = 0
    unmatched = 0
    seen_matches: set[str] = set()

    for c in committees:
        for r in (c.get("top_recipients") or []):
            key = canonical_recipient_key(r.get("candidate") or "", r.get("state") or "", r.get("party") or "")
            if not key or key not in members:
                unmatched += 1
                r["crypto_alignment"] = None
                continue
            score = score_member(members[key])
            if score is None:
                unmatched += 1
                r["crypto_alignment"] = None
                continue
            r["crypto_alignment"] = score
            matched += 1
            seen_matches.add(key)

    return {
        "matched_recipients": matched,
        "unmatched_recipients": unmatched,
        "unique_members_matched": len(seen_matches),
        "vote_meta": idx["vote_meta"],
    }


def build_vote_alignment_overlay(committees: list[dict]) -> dict:
    """Top-level entry: enrich FEC recipients with crypto vote alignment.

    Called from election_tracker._fetch_crypto_money_overlay() after FEC data
    is already populated. Safe to call — all I/O cached for 30 days.
    """
    stats = enrich_recipients_with_votes(committees)
    logger.info(
        "Crypto vote alignment: {} recipients matched / {} unmatched ({} unique members)",
        stats["matched_recipients"], stats["unmatched_recipients"], stats["unique_members_matched"],
    )
    return stats


if __name__ == "__main__":
    import sys
    idx = build_vote_index()
    print(f"Tracked votes: {len(idx['vote_meta'])}")
    for k, v in idx["vote_meta"].items():
        print(f"  {k}: {v['label']} ({v['bill']}) {v['date']} — Y:{v['yeas']} N:{v['nays']}")
    print(f"Total members indexed: {len(idx['member_votes'])}")
    # Show a few sample members
    sample = list(idx["member_votes"].items())[:5]
    for k, m in sample:
        score = score_member(m)
        print(f"  {k}: {m['last_name']} ({m['party']}-{m['state']}) → {score}")
