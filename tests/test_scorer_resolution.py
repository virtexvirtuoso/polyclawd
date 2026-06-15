"""Tests for the goalscorer two-source resolution core (signals/scorer_resolution.py).

Pure logic — synthetic API-Football + ESPN payloads only, NO network. Covers the
design note's load-bearing cases: legit goal, the OWN-GOAL trap, VAR-cancelled
goal, penalty scored, source disagreement, not-final, and name normalization.

Run: venv/bin/python -m pytest tests/test_scorer_resolution.py -q --noconftest
"""

from signals.scorer_resolution import (
    af_player_scored,
    espn_player_scored,
    resolve_scorer,
    name_match,
    match_fixture,
    ResolveState,
)


# ── Synthetic payload builders ────────────────────────────────────────────
def af_goal(player, detail="Normal Goal", elapsed=23):
    return {"type": "Goal", "detail": detail, "player": {"id": 1, "name": player},
            "time": {"elapsed": elapsed}}


def af_var_cancel(player, elapsed=23):
    return {"type": "Var", "detail": "Goal cancelled", "player": {"id": 1, "name": player},
            "time": {"elapsed": elapsed}}


def espn_goal(player, type_type="goal", scoring=True, shootout=False):
    return {"scoringPlay": scoring, "shootout": shootout,
            "type": {"type": type_type, "text": "Goal"},
            "participants": [{"athlete": {"displayName": player}}]}


# ── (1) Legit normal goal → YES on both → resolve YES ─────────────────────
def test_legit_normal_goal_resolves_yes():
    af = [af_goal("Harry Kane")]
    espn = [espn_goal("Harry Kane")]
    assert af_player_scored(af, "Harry Kane") is True
    assert espn_player_scored(espn, "Harry Kane") is True
    assert resolve_scorer("Harry Kane", af, espn, af_final=True, espn_completed=True) == ResolveState.YES


# ── (2) THE OWN-GOAL TRAP → both sources NO ───────────────────────────────
# An own goal must NOT credit the player who deflected it in. ESPN tags the
# conceding player on the own-goal event; API-Football marks detail="Own Goal".
def test_own_goal_does_not_credit_deflecting_player():
    # Malo Gusto (Chelsea) own-goal that counted FOR Sunderland.
    af = [{"type": "Goal", "detail": "Own Goal",
           "player": {"id": 9, "name": "Malo Gusto"}, "time": {"elapsed": 41}}]
    espn = [{"scoringPlay": True, "shootout": False,
             "type": {"type": "own-goal", "text": "Own Goal"},
             "team": {"displayName": "Sunderland"},
             "participants": [{"athlete": {"displayName": "Malo Gusto"}}]}]
    # Neither source may settle YES on Gusto's anytime-scorer prop.
    assert af_player_scored(af, "Malo Gusto") is False
    assert espn_player_scored(espn, "Malo Gusto") is False
    assert resolve_scorer("Malo Gusto", af, espn, af_final=True, espn_completed=True) == ResolveState.NO


# ── (3) VAR-cancelled goal → NO ───────────────────────────────────────────
def test_var_cancelled_goal_resolves_no():
    # API-Football: a goal row + a matching Var/Goal cancelled row (same player+time).
    af = [af_goal("Bukayo Saka", elapsed=67), af_var_cancel("Bukayo Saka", elapsed=67)]
    # ESPN gives no cancel row → relies on final-set-only; the disallowed goal is
    # simply absent from the final keyEvents, so ESPN also says NO.
    espn = []
    assert af_player_scored(af, "Bukayo Saka") is False
    assert espn_player_scored(espn, "Bukayo Saka") is False
    assert resolve_scorer("Bukayo Saka", af, espn, af_final=True, espn_completed=True) == ResolveState.NO


# ── (4) Penalty scored → YES ──────────────────────────────────────────────
def test_penalty_scored_resolves_yes():
    af = [af_goal("Bruno Fernandes", detail="Penalty", elapsed=55)]
    espn = [espn_goal("Bruno Fernandes", type_type="penalty---scored")]
    assert af_player_scored(af, "Bruno Fernandes") is True
    assert espn_player_scored(espn, "Bruno Fernandes") is True
    assert resolve_scorer("Bruno Fernandes", af, espn, af_final=True, espn_completed=True) == ResolveState.YES


def test_missed_penalty_is_not_a_goal():
    af = [af_goal("Bruno Fernandes", detail="Missed Penalty", elapsed=55)]
    assert af_player_scored(af, "Bruno Fernandes") is False


# ── (5) Sources disagree → DISPUTED ───────────────────────────────────────
def test_sources_disagree_resolves_disputed():
    af = [af_goal("Phil Foden")]      # API-Football: scored
    espn = []                          # ESPN: phantom-absent → not scored
    assert af_player_scored(af, "Phil Foden") is True
    assert espn_player_scored(espn, "Phil Foden") is False
    assert resolve_scorer("Phil Foden", af, espn, af_final=True, espn_completed=True) == ResolveState.DISPUTED


# ── (6) Not final → PENDING ───────────────────────────────────────────────
def test_not_final_resolves_pending():
    af = [af_goal("Harry Kane")]
    espn = [espn_goal("Harry Kane")]
    # AF final but ESPN not completed → PENDING (must be BOTH final).
    assert resolve_scorer("Harry Kane", af, espn, af_final=True, espn_completed=False) == ResolveState.PENDING
    assert resolve_scorer("Harry Kane", af, espn, af_final=False, espn_completed=True) == ResolveState.PENDING


# ── (7) Name normalization: "B. Fernandes" ↔ "Bruno Fernandes" ────────────
def test_name_normalization_initial_form():
    assert name_match("B. Fernandes", "Bruno Fernandes") is True
    assert name_match("Bruno Fernandes", "B. Fernandes") is True


def test_name_normalization_through_resolution():
    # ESPN abbreviates the scorer; the prop carries the full canonical name.
    af = [af_goal("Bruno Fernandes")]
    espn = [espn_goal("B. Fernandes")]
    assert resolve_scorer("Bruno Fernandes", af, espn, af_final=True, espn_completed=True) == ResolveState.YES


def test_name_normalization_diacritics():
    af = [af_goal("Nicolás Otamendi")]
    assert af_player_scored(af, "Nicolas Otamendi") is True


def test_name_mismatch_does_not_match():
    assert name_match("Harry Kane", "Harry Maguire") is False


# ── Extra coverage: shootout exclusion, multiple goals, fixture matching ──
def test_shootout_goal_excluded_espn():
    espn = [espn_goal("Lionel Messi", shootout=True)]
    assert espn_player_scored(espn, "Lionel Messi") is False


def test_multiple_goals_same_player_still_yes():
    af = [af_goal("Erling Haaland", elapsed=12), af_goal("Erling Haaland", elapsed=78)]
    assert af_player_scored(af, "Erling Haaland") is True


def test_var_cancel_only_removes_matching_goal():
    # Two goals; only the 67' one is cancelled — the 12' legit goal still stands.
    af = [af_goal("Erling Haaland", elapsed=12),
          af_goal("Erling Haaland", elapsed=67),
          af_var_cancel("Erling Haaland", elapsed=67)]
    assert af_player_scored(af, "Erling Haaland") is True


def test_fixture_match_both_teams_and_date():
    odds = {"home": "South Korea", "away": "Japan", "commence_time": "2026-06-20T18:00:00Z"}
    cands = [{"home": "Korea Republic", "away": "Japan", "date": "2026-06-20", "id": 555}]
    m = match_fixture(odds, cands, aliases={"South Korea": ["Korea Republic", "Korea"]})
    assert m is not None and m["id"] == 555


def test_fixture_match_ambiguous_returns_none():
    odds = {"home": "Brazil", "away": "Argentina", "commence_time": "2026-06-20T18:00:00Z"}
    cands = [
        {"home": "Brazil", "away": "Argentina", "date": "2026-06-20", "id": 1},
        {"home": "Brazil", "away": "Argentina", "date": "2026-06-21", "id": 2},
    ]
    assert match_fixture(odds, cands) is None  # >1 candidate → don't guess
