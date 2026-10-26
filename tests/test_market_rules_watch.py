import sqlite3
import urllib.error
import urllib.parse

import pytest

from scripts import market_rules_watch as w

TOK_Z = "1" * 70  # CLOB token id that title_cache maps "Will Z happen?" to (latest ts)
TOK_Z_OLD = "9" * 70  # an older title_cache row for the same title
TOK_LIVE = "3" * 70  # live position that carries only a token id


@pytest.fixture
def dbs(tmp_path):
    shadow = tmp_path / "shadow.db"
    con = sqlite3.connect(shadow)
    con.executescript(f"""
      CREATE TABLE shadow_trades (market_id TEXT NOT NULL, market TEXT, platform TEXT, resolved INTEGER DEFAULT 0);
      CREATE TABLE live_positions (market_id TEXT, market_slug TEXT, token_id TEXT, status TEXT);
      CREATE TABLE title_cache (market_id TEXT PRIMARY KEY, title TEXT NOT NULL, ts INTEGER NOT NULL);
      INSERT INTO shadow_trades VALUES
        ('KXTEST-1', 'Kalshi test', 'kalshi', 0),
        ('KXOLD-1', 'Kalshi resolved', 'kalshi', 1),
        ('0xabc', 'Will X happen?', 'polymarket', 0),
        ('0xold', 'Resolved PM', 'polymarket', 1),
        ('1640919', 'Gamma id market', 'polymarket', 0),
        ('', 'Will Z happen?', 'polymarket', 0),
        ('', 'Title nobody cached', 'polymarket', 0),
        ('', '', 'polymarket', 0);
      INSERT INTO live_positions VALUES
        ('0xdef', 'will-y-happen', '222', 'open'),
        ('{TOK_LIVE}', '{TOK_LIVE}', '{TOK_LIVE}', 'open'),
        ('0xclosed', 'closed-one', '444', 'closed');
      INSERT INTO title_cache VALUES ('{TOK_Z}', 'Will Z happen?', 2), ('{TOK_Z_OLD}', 'Will Z happen?', 1);""")
    con.commit()
    con.close()
    return str(shadow), str(tmp_path / "rules.db")


def pm(cid, slug, desc="Resolves YES if X.", statuses="[]"):
    return {
        "conditionId": cid,
        "slug": slug,
        "question": slug,
        "description": desc,
        "resolutionSource": "https://src",
        "endDate": "2026-12-31T00:00:00Z",
        "umaResolutionStatus": None,
        "umaResolutionStatuses": statuses,
        "closed": False,
    }


class Fake:
    def __init__(self):
        self.pm = {
            "0xabc": pm("0xabc", "will-x-happen"),
            "0xdef": pm("0xdef", "will-y-happen"),
            "1640919": pm("0x164", "us-forces-enter-iran"),
            TOK_Z: pm("0x111", "will-z-happen"),
            TOK_LIVE: pm("0x333", "strait-of-hormuz"),
        }
        self.k = {
            "KXTEST-1": {
                "ticker": "KXTEST-1",
                "title": "Kalshi test",
                "rules_primary": "If A then Yes.",
                "rules_secondary": "",
                "settlement_sources": [{"name": "AP"}],
                "close_time": "2026-12-31T00:00:00Z",
                "status": "active",
            }
        }
        self.sent = []
        self.pm_errors = []

    def fetch_pm(self, cids, ids, tokens, errors):
        errors.extend(self.pm_errors)
        return [
            m for key, m in self.pm.items() if key in set(cids) | set(ids) | set(tokens)
        ]

    def fetch_k(self, ticker):
        return self.k.get(ticker)

    def notify(self, text):
        self.sent.append(text)
        return True


def go(dbs, f, now):
    shadow, rules = dbs
    return w.run(
        shadow_db=shadow,
        db_path=rules,
        fetch_pm=f.fetch_pm,
        fetch_k=f.fetch_k,
        notify=f.notify,
        now_ms=now,
    )


def q(dbs, sql):
    con = sqlite3.connect(dbs[1])
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_tracked_set_covers_every_id_kind_and_counts_the_rest(dbs):
    k, cids, ids, tokens, n_untracked = w.tracked(dbs[0])
    assert k == {"KXTEST-1"}
    assert cids == {"0xabc", "0xdef"}
    assert ids == {"1640919"}
    assert tokens == {TOK_Z, TOK_LIVE}
    assert n_untracked == 2


def test_first_run_seeds_silently(dbs):
    f = Fake()
    assert go(dbs, f, 1000) == 0
    assert f.sent == []
    assert q(dbs, "select count(*) from rules")[0][0] == 6
    assert q(
        dbs,
        "select n_tracked, n_untracked, n_fetched, n_missing, n_events, error from runs",
    ) == [(6, 2, 6, 0, 0, None)]


def test_polymarket_docs_are_keyed_by_condition_id(dbs):
    f = Fake()
    go(dbs, f, 1000)
    assert {
        r[0] for r in q(dbs, "select market from rules where platform='polymarket'")
    } == {"0xabc", "0xdef", "0x164", "0x111", "0x333"}
    f.pm["0xabc"] = dict(f.pm["0xabc"], slug="will-x-happen-renamed")
    go(dbs, f, 2000)
    assert f.sent == [] and q(dbs, "select count(*) from rules")[0][0] == 6


def test_a_description_edit_pages_with_a_diff(dbs):
    f = Fake()
    go(dbs, f, 1000)
    f.pm["0xabc"] = pm(
        "0xabc",
        "will-x-happen",
        desc="Resolves YES if X. Additional context: Y counts too.",
    )
    go(dbs, f, 2000)
    assert len(f.sent) == 1
    assert (
        "description" in f.sent[0]
        and "Additional context" in f.sent[0]
        and "will-x-happen" in f.sent[0]
    )
    assert q(dbs, "select platform, market, field from rule_events") == [
        ("polymarket", "0xabc", "description")
    ]


def test_a_new_dispute_pages_and_an_existing_one_at_seed_is_reported_once(dbs):
    f = Fake()
    f.pm["0xdef"] = pm("0xdef", "will-y-happen", statuses='["proposed", "disputed"]')
    go(dbs, f, 1000)
    assert (
        len(f.sent) == 1
        and "already disputed" in f.sent[0]
        and "will-y-happen" in f.sent[0]
    )
    go(dbs, f, 1500)
    assert len(f.sent) == 1
    f.pm["0xabc"] = pm("0xabc", "will-x-happen", statuses='["proposed", "disputed"]')
    go(dbs, f, 2000)
    assert (
        len(f.sent) == 2 and "DISPUTED" in f.sent[-1] and "will-x-happen" in f.sent[-1]
    )


def test_a_second_dispute_pages_but_a_plain_proposal_is_recorded_silently(dbs):
    f = Fake()
    go(dbs, f, 1000)
    f.pm["0xabc"] = pm("0xabc", "will-x-happen", statuses='["proposed"]')
    go(dbs, f, 2000)
    assert f.sent == []
    assert q(dbs, "select field from rule_events") == [("umaResolutionStatuses",)]
    f.pm["0xabc"] = pm("0xabc", "will-x-happen", statuses='["proposed", "disputed"]')
    go(dbs, f, 3000)
    f.pm["0xabc"] = pm(
        "0xabc",
        "will-x-happen",
        statuses='["proposed", "disputed", "proposed", "disputed"]',
    )
    go(dbs, f, 4000)
    assert len(f.sent) == 2 and all("DISPUTED" in s for s in f.sent)


def test_a_kalshi_rules_change_pages(dbs):
    f = Fake()
    go(dbs, f, 1000)
    f.k["KXTEST-1"]["rules_secondary"] = "Settles on the AP call."
    go(dbs, f, 2000)
    assert (
        len(f.sent) == 1
        and "rules_secondary" in f.sent[-1]
        and "KXTEST-1" in f.sent[-1]
    )


def test_a_kalshi_market_that_404s_is_skipped_and_counted_not_an_error(dbs):
    f = Fake()
    f.k = {}
    assert go(dbs, f, 1000) == 0
    assert q(dbs, "select n_fetched, n_missing, error from runs") == [(5, 1, None)]


def test_a_failed_chunk_keeps_and_diffs_what_was_fetched_and_exits_zero(dbs):
    f = Fake()
    go(dbs, f, 1000)
    f.pm_errors = ["condition_ids chunk 0 closed=true: URLError: timed out"]
    f.pm["0xabc"] = pm("0xabc", "will-x-happen", desc="Resolves YES if X or W.")
    assert go(dbs, f, 2000) == 0
    assert len(f.sent) == 1 and "will-x-happen" in f.sent[0]
    assert (
        "timed out"
        in q(dbs, "select error from runs order by ts_ms desc limit 1")[0][0]
    )


def test_a_fetch_that_raises_is_recorded_and_kalshi_still_runs(dbs):
    f = Fake()
    go(dbs, f, 1000)

    def boom(*a):
        raise TimeoutError("gamma down")

    f.k["KXTEST-1"]["rules_primary"] = "If B then Yes."
    assert (
        w.run(
            shadow_db=dbs[0],
            db_path=dbs[1],
            fetch_pm=boom,
            fetch_k=f.fetch_k,
            notify=f.notify,
            now_ms=2000,
        )
        == 0
    )
    assert len(f.sent) == 1 and "rules_primary" in f.sent[0]
    assert (
        "gamma down"
        in q(dbs, "select error from runs order by ts_ms desc limit 1")[0][0]
    )


def test_fetching_nothing_while_tracking_something_is_an_error(dbs):
    f = Fake()
    f.pm, f.k = {}, {}
    go(dbs, f, 1000)
    assert "fetched 0" in q(dbs, "select error from runs")[0][0]


def test_three_failing_runs_page_once_and_recovery_pages_once(dbs):
    f = Fake()
    f.pm_errors = ["boom"]
    for t in (1000, 2000):
        assert go(dbs, f, t) == 0
    assert f.sent == []
    go(dbs, f, 3000)
    assert len(f.sent) == 1 and "3 runs in a row" in f.sent[0] and "boom" in f.sent[0]
    go(dbs, f, 4000)
    assert len(f.sent) == 1
    f.pm_errors = []
    go(dbs, f, 5000)
    assert len(f.sent) == 2 and "recovered" in f.sent[1]
    go(dbs, f, 6000)
    assert len(f.sent) == 2


def test_a_failed_streak_page_is_retried_next_run(dbs):
    f = Fake()
    f.pm_errors = ["boom"]
    calls = []
    for t in (1000, 2000, 3000, 4000):
        w.run(
            shadow_db=dbs[0],
            db_path=dbs[1],
            fetch_pm=f.fetch_pm,
            fetch_k=f.fetch_k,
            notify=lambda text: calls.append(text) and False,
            now_ms=t,
        )
    assert len(calls) == 2


def test_every_change_is_sent(dbs):
    f = Fake()
    go(dbs, f, 1000)
    for key, m in f.pm.items():
        f.pm[key] = dict(
            m,
            description="z " * 2000,
            resolutionSource="https://other",
            endDate="2027-01-01",
        )
    go(dbs, f, 2000)
    assert sum(s.count(" changed") for s in f.sent) == 15 and all(
        len(s) <= w.MAX_MSG for s in f.sent
    )


def test_many_messages_are_split_under_the_telegram_limit():
    sent = []
    w._send(
        lambda t: sent.append(t) or True,
        [f"m{i} " + "x" * 1000 for i in range(10)] + ["y" * 9000],
    )
    assert len(sent) >= 4 and all(len(s) <= w.MAX_MSG for s in sent)
    assert all(any(f"m{i} " in s for s in sent) for i in range(10))


def test_fetch_pm_asks_open_and_closed_in_chunks_of_20_and_dedupes(monkeypatch):
    calls = []

    def fake_get(url, timeout=20):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        calls.append(qs)
        if "id" in qs and qs["closed"] == ["true"]:
            return [{"conditionId": "0xclosed", "slug": "closed-market"}]
        if "condition_ids" in qs and qs["closed"] == ["false"]:
            return [{"conditionId": c} for c in qs["condition_ids"]]
        if "clob_token_ids" in qs and qs["closed"] == ["false"]:
            return [
                {"conditionId": "0x00"}
            ]  # the same market as a condition id already returned
        return []

    monkeypatch.setattr(w, "_get", fake_get)
    errors = []
    cids = {f"0x{i:02d}" for i in range(25)}
    out = w.fetch_pm(cids, {"1640919"}, {TOK_Z}, errors)
    assert errors == []
    assert sorted(len(c["condition_ids"]) for c in calls if "condition_ids" in c) == [
        5,
        5,
        20,
        20,
    ]
    assert sorted(c["closed"][0] for c in calls if "id" in c) == ["false", "true"]
    got = [m["conditionId"] for m in out]
    assert "0xclosed" in got and len(got) == len(set(got)) == 26


def test_fetch_pm_keeps_other_chunks_when_one_fails(monkeypatch):
    def fake_get(url, timeout=20):
        if "closed=true" in url:
            raise urllib.error.URLError("timed out")
        return [{"conditionId": "0xabc"}]

    monkeypatch.setattr(w, "_get", fake_get)
    errors = []
    out = w.fetch_pm({"0xabc"}, set(), set(), errors)
    assert [m["conditionId"] for m in out] == ["0xabc"]
    assert len(errors) == 1 and "timed out" in errors[0]


def test_fetch_k_returns_none_on_404_and_raises_otherwise(monkeypatch):
    def gone(url, timeout=20):
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)

    monkeypatch.setattr(w, "_get", gone)
    assert w.fetch_k("KXGONE-1") is None

    def busy(url, timeout=20):
        raise urllib.error.HTTPError(url, 429, "slow down", {}, None)

    monkeypatch.setattr(w, "_get", busy)
    with pytest.raises(urllib.error.HTTPError):
        w.fetch_k("KXBUSY-1")


def test_the_real_notifier_refuses_under_pytest(monkeypatch):
    called = []
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: called.append(a))
    assert w.infra_notify("should never be sent") is False
    assert called == []
