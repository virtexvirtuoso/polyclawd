"""Tests for scripts/backfill_market_titles.py — dry-run default, --apply to write."""
import sqlite3

import pytest

import scripts.backfill_market_titles as bf

HEX_ID = "0x" + "cd" * 32
TOKEN_ID = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
LONG_HEXISH = "0x" + "ef" * 40  # >60 chars, no spaces


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "shadow_trades.db"
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE live_positions ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " market_id TEXT, market_title TEXT, status TEXT)"
    )
    rows = [
        (HEX_ID, "", "open"),                      # 1: empty title, resolvable
        (HEX_ID, HEX_ID, "open"),                  # 2: title == market_id
        (LONG_HEXISH, LONG_HEXISH, "closed"),      # 3: long spaceless title
        ("0x" + "11" * 32, "Will X happen?", "open"),  # 4: healthy — untouched
        (TOKEN_ID, "", "open"),                    # 5: token id — unresolvable
    ]
    con.executemany(
        "INSERT INTO live_positions(market_id, market_title, status) VALUES (?,?,?)",
        rows,
    )
    con.commit()
    con.close()
    return path


def _titles(path):
    con = sqlite3.connect(str(path))
    out = [r[0] for r in con.execute("SELECT market_title FROM live_positions ORDER BY id")]
    con.close()
    return out


def test_dry_run_selects_bad_rows_but_writes_nothing(db, monkeypatch):
    monkeypatch.setattr(
        bf, "resolve_title",
        lambda mid, db_path=None: "Resolved Q?" if mid.startswith("0x") else None,
    )
    before = _titles(db)
    stats = bf.backfill(db, apply=False)
    assert _titles(db) == before  # dry run: untouched
    assert stats["candidates"] == 4      # rows 1,2,3,5
    assert stats["resolved"] == 3        # rows 1,2,3
    assert stats["unresolved"] == 1      # row 5 (token id)
    assert stats["updated"] == 0


def test_apply_updates_only_resolved_rows(db, monkeypatch):
    monkeypatch.setattr(
        bf, "resolve_title",
        lambda mid, db_path=None: "Resolved Q?" if mid.startswith("0x") else None,
    )
    stats = bf.backfill(db, apply=True)
    assert stats["updated"] == 3
    titles = _titles(db)
    assert titles[0] == titles[1] == titles[2] == "Resolved Q?"
    assert titles[3] == "Will X happen?"   # healthy row untouched
    assert titles[4] == ""                 # unresolvable left as-is
