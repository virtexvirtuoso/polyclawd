#!/usr/bin/env python3
"""
Goalscorer prop RESOLUTION — pure, two-source ("did player X score in match Y?").

Implements §5 of [[prop-edge-system-spec]] per the full design at
[[Goalscorer-Resolution-Design]] (designed 2026-06-15). Resolution logic ONLY —
fetch is injectable so the network is never touched in tests; the paper logger /
scanner wires the real API-Football + ESPN fetchers in.

Authority chain (from the design note):
  - Grade strictly off each source's FINAL post-match event set.
  - Attribute by the scorer TYPE/DETAIL flag, never the beneficiary team or the
    player field — the own-goal trap: ESPN tags an own goal to the player who
    deflected it in (conceding side), so "credit participants[0]" would settle a
    FALSE YES on that player's anytime-scorer prop. The discriminator is the
    own-goal type flag.
  - Drop own-goals and VAR-disallowed goals; penalties scored DO count.
  - Settle YES only when API-Football AND ESPN agree. Disagreement → DISPUTED
    (excluded from metrics). UNMATCHED / VOID handled upstream at fixture-match.

Two API shapes consumed (synthetic in tests, real upstream):
  - API-Football /fixtures/events rows: {type, detail, player:{id,name}, time:{elapsed}}
  - ESPN summary keyEvents rows: {scoringPlay, type:{type,text}, participants:[{athlete:{displayName}}], shootout}
"""
from __future__ import annotations

import difflib
from typing import Dict, List, Optional

# Reuse the canonical normalizer (NFKD + lowercase) so "Türkiye"/diacritics match.
try:
    from odds.sports_edge_common import _norm  # type: ignore
except Exception:  # pragma: no cover - import-path fallback for standalone runs
    try:
        from sports_edge_common import _norm  # type: ignore
    except Exception:  # pragma: no cover
        import unicodedata

        def _norm(s: str) -> str:
            return "".join(
                c for c in unicodedata.normalize("NFKD", s or "")
                if not unicodedata.combining(c)
            ).lower().strip()


# ── Resolution states ────────────────────────────────────────────────────
# Two-source core emits YES/NO/DISPUTED/PENDING. UNMATCHED/VOID are set upstream
# at fixture-match / TTL time but live here as the canonical constant set.
class ResolveState:
    YES = "YES"
    NO = "NO"
    DISPUTED = "DISPUTED"        # sources disagree → exclude from metrics
    PENDING = "PENDING"          # not both final yet
    UNMATCHED = "UNMATCHED"      # no shared fixture across sources (upstream)
    VOID = "VOID"               # abandoned/postponed past 24h TTL (upstream)


VALID_STATES = (
    ResolveState.YES,
    ResolveState.NO,
    ResolveState.DISPUTED,
    ResolveState.PENDING,
    ResolveState.UNMATCHED,
    ResolveState.VOID,
)

# States excluded from realized hit-rate (CLV is the primary metric until n large).
EXCLUDED_FROM_METRICS = (ResolveState.DISPUTED, ResolveState.UNMATCHED, ResolveState.VOID, ResolveState.PENDING)

# API-Football final statuses (caller passes af_final precomputed, but exported
# for the upstream poller to gate on status.short).
AF_FINAL_STATUSES = ("FT", "AET", "PEN")

NAME_MATCH_THRESHOLD = 0.90


# ── Player-name matching (§6: layered, strongest first) ───────────────────
def _last_token(norm_name: str) -> str:
    parts = norm_name.split()
    return parts[-1] if parts else ""


def _first_initial(norm_name: str) -> str:
    parts = norm_name.split()
    return parts[0][0] if parts and parts[0] else ""


def name_match(candidate: str, player: str) -> bool:
    """True if the source scorer `candidate` is the prop's `player`.

    Layered, strongest first (design §6):
      1. exact normalized equality
      2. last-token + first-initial  ("B. Fernandes" ↔ "Bruno Fernandes")
      3. difflib SequenceMatcher ratio >= 0.90
    """
    a, b = _norm(candidate), _norm(player)
    if not a or not b:
        return False
    if a == b:
        return True

    # Initial-form match: same surname AND compatible first initial. Handles both
    # "B. Fernandes" and a bare "Fernandes" against "Bruno Fernandes".
    la, lb = _last_token(a), _last_token(b)
    if la and la == lb:
        ia, ib = _first_initial(a), _first_initial(b)
        # If either side has only the surname (1 token), the surname match carries.
        if len(a.split()) == 1 or len(b.split()) == 1 or ia == ib:
            return True

    return difflib.SequenceMatcher(None, a, b).ratio() >= NAME_MATCH_THRESHOLD


# ── Source A: API-Football /fixtures/events (§2) ──────────────────────────
_AF_GOAL_DETAILS = ("Normal Goal", "Penalty")           # count these
_AF_EXCLUDE_DETAILS = ("Own Goal", "Missed Penalty")    # never a scorer YES


def _af_player_of(ev: dict) -> str:
    p = ev.get("player") or {}
    if isinstance(p, dict):
        return p.get("name") or ""
    return str(p or "")


def af_player_scored(af_events: List[dict], player) -> bool:
    """Did `player` score a legit (own-goal/VAR-safe) goal in the API-Football
    final event set?

    Counts a goal ONLY if type=="Goal" and detail in ("Normal Goal","Penalty")
    and the scorer matches `player`. EXCLUDES detail=="Own Goal" and
    "Missed Penalty" (keyed on the TYPE/DETAIL flag, NOT the player/team field —
    the load-bearing own-goal trap). Subtracts any goal cancelled by a matching
    Var/"Goal cancelled" row (same player + time.elapsed).
    """
    events = af_events or []

    # Build the VAR-retraction index: (norm_player, elapsed) of cancelled goals.
    cancelled = set()
    for ev in events:
        if (ev.get("type") == "Var") and (ev.get("detail") == "Goal cancelled"):
            elapsed = (ev.get("time") or {}).get("elapsed")
            cancelled.add((_norm(_af_player_of(ev)), elapsed))

    for ev in events:
        if ev.get("type") != "Goal":
            continue
        detail = ev.get("detail")
        # Own-goal trap + missed penalty: excluded by the DETAIL flag.
        if detail in _AF_EXCLUDE_DETAILS:
            continue
        if detail not in _AF_GOAL_DETAILS:
            continue
        scorer = _af_player_of(ev)
        if not name_match(scorer, player):
            continue
        # Belt-and-suspenders VAR subtraction: skip if this exact goal was cancelled.
        elapsed = (ev.get("time") or {}).get("elapsed")
        if (_norm(scorer), elapsed) in cancelled:
            continue
        return True  # anytime = >=1 legit goal; short-circuit YES
    return False


# ── Source B: ESPN summary keyEvents (§3) ─────────────────────────────────
_ESPN_SCORER_PREFIXES = ("goal", "penalty---scored")


def _espn_first_athlete(ev: dict) -> str:
    parts = ev.get("participants") or []
    if not parts:
        return ""
    ath = (parts[0] or {}).get("athlete") or {}
    if isinstance(ath, dict):
        return ath.get("displayName") or ath.get("fullName") or ath.get("name") or ""
    return str(ath or "")


def espn_player_scored(key_events: List[dict], player) -> bool:
    """Did `player` score per the ESPN summary keyEvents (soccer has no
    scoringPlays — keyEvents only)?

    YES only if scoringPlay is True AND type.type starts with
    ("goal","penalty---scored") AND NOT startswith "own-goal" AND
    participants[0].athlete matches `player`. Shootout goals are excluded
    (`shootout` True) — not a 90+ET anytime-scorer YES. Graded off the final set;
    completed-gate is applied in resolve_scorer.
    """
    for ev in (key_events or []):
        if not ev.get("scoringPlay"):
            continue
        if ev.get("shootout") is True:
            continue
        tt = ((ev.get("type") or {}).get("type") or "")
        if tt.startswith("own-goal"):
            continue
        if not tt.startswith(_ESPN_SCORER_PREFIXES):
            continue
        if name_match(_espn_first_athlete(ev), player):
            return True
    return False


# ── Resolution algorithm (§4) ─────────────────────────────────────────────
def resolve_scorer(
    player,
    af_events: List[dict],
    espn_key_events: List[dict],
    af_final: bool,
    espn_completed: bool,
) -> str:
    """Two-source resolution of an anytime-goalscorer prop.

    Returns one of YES | NO | DISPUTED | PENDING.
      - PENDING   if not both sources are final.
      - YES       if both sources agree the player scored.
      - NO        if both sources agree the player did NOT score.
      - DISPUTED  if the two sources disagree (excluded from metrics).
    (UNMATCHED / VOID are decided upstream at fixture-match / TTL time.)
    """
    if not (af_final and espn_completed):
        return ResolveState.PENDING
    af = af_player_scored(af_events, player)
    espn = espn_player_scored(espn_key_events, player)
    if af and espn:
        return ResolveState.YES
    if not af and not espn:
        return ResolveState.NO
    return ResolveState.DISPUTED


# ── Fixture-matching helper skeleton (§5) ─────────────────────────────────
# Resolution core is the priority; this is the documented skeleton the upstream
# logger fleshes out with real candidate lists. Provider athlete IDs are NOT
# shared across sources → join verdicts per-source, never map events across them.
def _team_match(a: str, b: str, aliases: Optional[Dict[str, List[str]]] = None,
                threshold: float = 0.85) -> bool:
    """Team-name match: normalized exact / alias containment / SequenceMatcher
    >= 0.85 ("Korea Republic" ↔ "South Korea"). Looser than the player threshold
    by design (team aliases are noisier)."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    for canon, names in (aliases or {}).items():
        forms = {_norm(canon)} | {_norm(x) for x in names}
        if na in forms and nb in forms:
            return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


def _date_within(iso_a: str, iso_b: str, days: int = 1) -> bool:
    """commence_date ± `days` gate (UTC/midnight slop). True if within window or
    unparseable-but-equal."""
    from datetime import datetime

    def _d(s: str):
        try:
            return datetime.fromisoformat((s or "").replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            return None

    da, db = _d(iso_a), _d(iso_b)
    if da is None or db is None:
        return False
    return abs((da - db).days) <= days


def match_fixture(
    odds_event: dict,
    candidates: List[dict],
    aliases: Optional[Dict[str, List[str]]] = None,
) -> Optional[dict]:
    """Resolve an Odds API event to a single source fixture candidate.

    odds_event: {"home": str, "away": str, "commence_time": isostr}
    candidates: [{"home": str, "away": str, "date": isostr, "id": ...}, ...]

    Require BOTH teams to match (alias/SequenceMatcher) AND the date within ±1d.
    >1 surviving candidate → return None (UNMATCHED; don't guess), per §5.
    """
    home = odds_event.get("home", "")
    away = odds_event.get("away", "")
    commence = odds_event.get("commence_time", "")
    hits = []
    for c in candidates or []:
        if not _date_within(commence, c.get("date", ""), days=1):
            continue
        # BOTH teams must match (allow home/away swap across providers).
        straight = _team_match(home, c.get("home", ""), aliases) and _team_match(away, c.get("away", ""), aliases)
        swapped = _team_match(home, c.get("away", ""), aliases) and _team_match(away, c.get("home", ""), aliases)
        if straight or swapped:
            hits.append(c)
    return hits[0] if len(hits) == 1 else None


if __name__ == "__main__":  # pragma: no cover
    print("scorer_resolution: pure two-source resolver (no network at import).")
