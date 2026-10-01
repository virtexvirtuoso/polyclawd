"""Telegram alert formatting tests (2026-10-01 format rework).

Pins two things:
1. The plain-text entry/refire block — entry/refire route through dispatch
   tier-2/3, which strip HTML and force parse_mode=None, so the block must be
   authored plain-text-first (the old single line truncated the title
   mid-word, buried the market behind a dense bracket tag, no link).
2. Batch/digest overflow chunking — a group whose packed text exceeds the
   Telegram 4096-char limit used to fail as ONE send (http_400) and linger
   queued until the 6h/15h expiry silently dropped it.
"""
import sqlite3
import time

import pytest

from scripts import smart_wallet_alert as swa
import signals.alert_dispatch as ad


@pytest.fixture()
def conns():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    swa.init_accum(c)
    swa.init_shadows(c)
    return c, c


# --- entry/refire plain-text block ---

def _rec(**over):
    base = dict(
        wallet="0xabc", name="21372137", market="0xcid",
        title="LoL: MAGAZA vs The Otter Side (BO3) - EMEA Masters Swiss Stage",
        direction="BUY", outcome="The Otter Side", outcome_index=1,
        price_at_alert=0.5565, cumulative_usd=1241.52, num_fills=6,
        wallet_wr=0.28, wallet_pnl=18742,
        alert_type="entry", ts_alert=1000, market_slug="lol-mgz-tos",
    )
    base.update(over)
    return base


def test_one_liner_block_structure():
    text = swa._format_wallet_one_liner(_rec())
    lines = text.split("\n")
    assert lines[0] == "🧠 ENTRY — The Otter Side @ 56¢"
    assert lines[1].startswith("LoL: MAGAZA")
    assert "$1,242 in 6 fills" in lines[2]
    assert "28% WR" in lines[2] and "+$18,742" in lines[2] and "21372137" in lines[2]
    assert lines[3] == "polymarket.com/event/lol-mgz-tos"
    assert "<" not in text  # plain-text-first: batch/digest strips HTML


def test_one_liner_refire_icon_and_truncation():
    text = swa._format_wallet_one_liner(_rec(alert_type="refire", title="x" * 100))
    assert text.startswith("🔁 ADD")
    title_line = text.split("\n")[1]
    assert title_line.endswith("…")
    assert len(title_line) <= 70


def test_one_liner_handles_missing_optional_fields():
    # outcome name empty -> generic side by token index (codebase convention,
    # same mapping _format_alert uses: 0=YES token, 1=NO token)
    text = swa._format_wallet_one_liner(
        _rec(wallet_wr=None, wallet_pnl=None, market_slug="", outcome="",
             outcome_index=0))
    assert "YES @ 56¢" in text.split("\n")[0]
    assert "NO @ 56¢" in swa._format_wallet_one_liner(
        _rec(outcome="", outcome_index=1)).split("\n")[0]
    assert "polymarket.com" not in text


def test_entry_dispatch_uses_block(conns, monkeypatch):
    meta, shadow = conns
    captured = {}
    monkeypatch.setattr(
        swa, "dispatch",
        lambda pipe, msg, tier: captured.update(pipeline=pipe, message=msg, tier=tier))
    monkeypatch.setattr(swa, "_executable_snapshot", lambda *a, **k: {})
    monkeypatch.setattr(swa, "_price_band_gate_suppress", lambda rec: False)
    monkeypatch.setattr(swa, "_clv_gate_suppress", lambda conn, w: False)
    T = swa.THRESHOLD
    fills = [dict(wallet="0xabc", market="m1", direction="BUY", usd=T * 0.4,
                  price=0.40, outcome="Yes", outcome_index=0, name="W",
                  title="T Market")] * 3
    fired = swa.check_and_fire(
        meta, shadow, fills,
        lambda cid: {"volume": 1e6, "price": 0.4, "title": "T Market",
                     "close_time": ""},
        now=1000)
    assert len(fired) == 1
    assert captured["pipeline"] == "wallet_moves"
    assert captured["message"].startswith("🧠 ENTRY")
    assert "\n" in captured["message"]


# --- batch/digest overflow chunking ---

def test_pack_chunks_respects_limit_and_covers_all_rows():
    rows = [{"id": i, "message": "x" * 900} for i in range(6)]
    chunks = ad._pack_chunks(rows, header="H")
    assert all(len(t) <= ad.TG_MSG_MAX for t, _ in chunks)
    all_ids = [i for _, ids in chunks for i in ids]
    assert all_ids == [r["id"] for r in rows]
    assert chunks[0][0].startswith("H\n")


def test_pack_chunks_truncates_poison_row():
    chunks = ad._pack_chunks([{"id": 1, "message": "y" * 5000}], header="")
    assert len(chunks) == 1
    assert len(chunks[0][0]) <= ad.TG_MSG_MAX


def _tmp_db(tmp_path):
    p = tmp_path / "q.db"
    con = sqlite3.connect(str(p))
    ad._ensure_tables(con)
    con.commit()
    con.close()
    return str(p)


def test_drain_splits_oversized_batch(tmp_path, monkeypatch):
    db = _tmp_db(tmp_path)
    sent = []
    monkeypatch.setattr(
        ad, "alert_openclaw",
        lambda msg, parse_mode=None, **k: sent.append(msg) or True)
    for i in range(3):
        ad.dispatch("wallet_moves", "m" * 1800, ad.TIER_BATCH,
                    dedup_key=str(i), db_path=db)
    # now must stay near the rows' enqueue ts (int(time.time())): a far-future
    # now (int(2e9)) makes the 6h stale sweep eat fresh rows before the due
    # query — the drain silently sent nothing.
    n = ad.drain(db_path=db, now=int(time.time()) + 1000, force=True)
    assert n == 2  # 3 x 1800-char rows exceed one message -> two chunks
    assert all(len(m) <= ad.TG_MSG_MAX for m in sent)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM alert_queue").fetchone()[0] == 0


def test_small_batch_keeps_single_message_format(tmp_path, monkeypatch):
    db = _tmp_db(tmp_path)
    sent = []
    monkeypatch.setattr(
        ad, "alert_openclaw",
        lambda msg, parse_mode=None, **k: sent.append(msg) or True)
    ad.dispatch("rising_wallets", "event one", ad.TIER_BATCH,
                dedup_key="a", db_path=db)
    ad.dispatch("rising_wallets", "event two", ad.TIER_BATCH,
                dedup_key="b", db_path=db)
    ad.drain(db_path=db, now=int(time.time()) + 1000, force=True)
    assert len(sent) == 1
    assert sent[0].startswith("📨 rising_wallets — 2 events (")


def test_digest_splits_and_drops_noise_rows(tmp_path, monkeypatch):
    db = _tmp_db(tmp_path)
    sent = []
    monkeypatch.setattr(
        ad, "alert_openclaw",
        lambda msg, parse_mode=None, **k: sent.append(msg) or True)
    for i in range(3):
        ad.dispatch("wallet_moves", "w" * 1800, ad.TIER_DIGEST,
                    dedup_key=str(i), db_path=db)
    ad.dispatch("wallet_moves", "no PM gap heartbeat", ad.TIER_DIGEST,
                dedup_key="n", db_path=db)
    n = ad.drain_digest(db_path=db, now=int(2e9))
    assert n == 2  # 3 x 1800-char signal rows pack into 2 chunks; noise row never sent
    assert len(sent) == 2
    assert all("no PM gap" not in m for m in sent)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM alert_queue").fetchone()[0] == 0