"""Phase R characterization test for baseball_edge sharp-devig flag.

Locks BOTH behaviors so the live engine's default is provably unchanged and the
new sharp-book path is provably correct. Run:
    venv/bin/python -m pytest tests/test_baseball_phase_r.py -v --noconftest

Phase R deploys the BASEBALL_SHARP_DEVIG flag flip only AFTER soccer/UFC have a
shadow track record (per plan v2 gate). Default OFF = current production behavior.
"""
from odds import baseball_edge as be


def _two_book_game():
    # DraftKings (soft) vs Pinnacle (sharp). Best-of-all cherry-picks the most
    # favorable price per team across books, deflating the overround.
    return {"bookmakers": [
        {"key": "draftkings", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Yankees", "price": -150}, {"name": "Red Sox", "price": 140}]}]},
        {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Yankees", "price": -170}, {"name": "Red Sox", "price": 150}]}]},
    ]}


def test_default_off_is_legacy_best_of_all(monkeypatch):
    monkeypatch.delenv("BASEBALL_SHARP_DEVIG", raising=False)
    best = be._best_odds_per_team(_two_book_game())
    # Yankees: -150 (DK) has LOWER implied prob than -170 (Pinn) -> best-of-all picks -150
    # Red Sox: +150 (Pinn) higher payout than +140 (DK) -> picks +150
    assert best == {"Yankees": -150, "Red Sox": 150}


def test_flag_on_uses_single_sharp_book(monkeypatch):
    monkeypatch.setenv("BASEBALL_SHARP_DEVIG", "1")
    best = be._best_odds_per_team(_two_book_game())
    # Pinnacle only — coherent single-book line, no overround deflation
    assert best == {"Yankees": -170, "Red Sox": 150}


def test_flag_on_falls_back_when_no_sharp_book(monkeypatch):
    monkeypatch.setenv("BASEBALL_SHARP_DEVIG", "1")
    soft_only = {"bookmakers": [
        {"key": "betonline", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Yankees", "price": -150}, {"name": "Red Sox", "price": 140}]}]},
    ]}
    best = be._best_odds_per_team(soft_only)
    assert best == {"Yankees": -150, "Red Sox": 140}  # legacy fallback, no 500
