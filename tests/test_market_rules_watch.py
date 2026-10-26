import json
import sqlite3
import urllib.error
import urllib.parse

import pytest

from scripts import market_rules_watch as w

TOK_Z = "1" * 70  # CLOB token id that title_cache maps "Will Z happen?" to (latest ts)
TOK_Z_OLD = "9" * 70  # an older title_cache row for the same title, inserted FIRST so scan order would pick it
TOK_LIVE = "3" * 70  # live position that carries only a token id
DAY_MS = 86_400_000


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
        ('KXTEST-2', 'Kalshi test two', 'kalshi', 0),
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
      INSERT INTO title_cache VALUES ('{TOK_Z_OLD}', 'Will Z happen?', 1), ('{TOK_Z}', 'Will Z happen?', 2);""")
    con.commit()
    con.close()
    return str(shadow), str(tmp_path / "rules.db")


def pm(cid, slug, desc="Resolves YES if X.", statuses="[]", status=None):
    """Live Gamma shape: umaResolutionStatus is the latest step (or 'resolved'), alongside the history."""
    if status is None:
        history = json.loads(statuses)
        status = history[-1] if history else None
    return {"conditionId": cid, "slug": slug, "question": slug, "description": desc,
            "resolutionSource": "https://src", "endDate": "2026-12-31T00:00:00Z",
            "umaResolutionStatus": status, "umaResolutionStatuses": statuses, "closed": False}


def km(ticker, title, event="KXTEST", rules="If A then Yes.", close="2026-12-31T00:00:00Z", status="active"):
    """Live Kalshi /markets shape: no settlement_sources (that lives on the event)."""
    return {"ticker": ticker, "event_ticker": event, "title": title, "rules_primary": rules,
            "rules_secondary": "", "close_time": close, "status": status}


class Fake:
    def __init__(self):
        self.pm = {"0xabc": pm("0xabc", "will-x-happen"), "0xdef": pm("0xdef", "will-y-happen"),
                   "1640919": pm("0x164", "us-forces-enter-iran"), TOK_Z: pm("0x111", "will-z-happen"),
                   TOK_LIVE: pm("0x333", "strait-of-hormuz")}
        self.k = {"KXTEST-1": km("KXTEST-1", "Kalshi test"), "KXTEST-2": km("KXTEST-2", "Kalshi test two")}
        self.ev = {"KXTEST": {"event_ticker": "KXTEST", "settlement_sources": [{"name": "AP", "url": "https://ap.org"}]}}
        self.ev_calls = []
        self.ev_error = None
        self.sent = []
        self.pm_errors = []
        self.notify_ok = True

    def fetch_pm(self, cids, ids, tokens, errors):
        errors.extend(self.pm_errors)
        return [m for key, m in self.pm.items() if key in set(cids) | set(ids) | set(tokens)]

    def fetch_k(self, ticker):
        return self.k.get(ticker)

    def fetch_ev(self, event_ticker):
        self.ev_calls.append(event_ticker)
        if self.ev_error:
            raise self.ev_error
        return self.ev.get(event_ticker)

    def notify(self, text):
        self.sent.append(text)
        return self.notify_ok


def go(dbs, f, now):
    shadow, rules = dbs
    return w.run(shadow_db=shadow, db_path=rules, fetch_pm=f.fetch_pm, fetch_k=f.fetch_k, fetch_ev=f.fetch_ev,
                 notify=f.notify, now_ms=now)


def q(dbs, sql):
    con = sqlite3.connect(dbs[1])
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def doc(dbs, market):
    return json.loads(q(dbs, f"select doc_json from rules where market='{market}'")[0][0])


def outage(f):
    f.pm, f.k = {}, {}


# --- tracked set ---------------------------------------------------------------------------------------------

def test_tracked_set_covers_every_id_kind_and_counts_the_rest(dbs):
    k, cids, ids, tokens, n_untracked = w.tracked(dbs[0])
    assert k == {"KXTEST-1", "KXTEST-2"}
    assert cids == {"0xabc", "0xdef"}
    assert ids == {"1640919"}
    assert tokens == {TOK_Z, TOK_LIVE}
    assert n_untracked == 2


# --- seeding and diffs ---------------------------------------------------------------------------------------

def test_first_run_seeds_silently(dbs):
    f = Fake()
    assert go(dbs, f, 1000) == 0
    assert f.sent == []
    assert q(dbs, "select count(*) from rules")[0][0] == 7
    assert q(dbs, "select n_tracked, n_untracked, n_fetched, n_missing, n_ev_fetched, n_ev_missing, n_events, "
                  "failing, error from runs") == [(7, 2, 7, 0, 1, 0, 0, 0, None)]


def test_polymarket_docs_are_keyed_by_condition_id(dbs):
    f = Fake()
    go(dbs, f, 1000)
    assert {r[0] for r in q(dbs, "select market from rules where platform='polymarket'")} == \
        {"0xabc", "0xdef", "0x164", "0x111", "0x333"}
    f.pm["0xabc"] = dict(f.pm["0xabc"], slug="will-x-happen-renamed")
    go(dbs, f, 2000)
    assert f.sent == [] and q(dbs, "select count(*) from rules")[0][0] == 7


def test_a_description_edit_pages_with_a_diff(dbs):
    f = Fake()
    go(dbs, f, 1000)
    f.pm["0xabc"] = pm("0xabc", "will-x-happen", desc="Resolves YES if X. Additional context: Y counts too.")
    go(dbs, f, 2000)
    assert len(f.sent) == 1
    assert "description" in f.sent[0] and "Additional context" in f.sent[0] and "will-x-happen" in f.sent[0]
    assert q(dbs, "select platform, market, field, reseed from rule_events") == \
        [("polymarket", "0xabc", "description", 0)]


def test_a_new_dispute_pages_and_an_existing_one_at_seed_is_reported_once(dbs):
    f = Fake()
    f.pm["0xdef"] = pm("0xdef", "will-y-happen", statuses='["proposed", "disputed"]')
    go(dbs, f, 1000)
    assert len(f.sent) == 1 and "already disputed" in f.sent[0] and "will-y-happen" in f.sent[0]
    go(dbs, f, 1500)
    assert len(f.sent) == 1
    f.pm["0xabc"] = pm("0xabc", "will-x-happen", statuses='["proposed", "disputed"]')
    go(dbs, f, 2000)
    assert len(f.sent) == 2 and "DISPUTED" in f.sent[-1] and "will-x-happen" in f.sent[-1]


def test_a_second_dispute_pages_but_a_plain_proposal_is_recorded_silently(dbs):
    f = Fake()
    go(dbs, f, 1000)
    f.pm["0xabc"] = pm("0xabc", "will-x-happen", statuses='["proposed"]')
    go(dbs, f, 2000)
    assert f.sent == []
    assert q(dbs, "select field from rule_events order by field") == \
        [("umaResolutionStatus",), ("umaResolutionStatuses",)]
    f.pm["0xabc"] = pm("0xabc", "will-x-happen", statuses='["proposed", "disputed"]')
    go(dbs, f, 3000)
    f.pm["0xabc"] = pm("0xabc", "will-x-happen", statuses='["proposed", "disputed", "proposed", "disputed"]')
    go(dbs, f, 4000)
    assert len(f.sent) == 2 and all("DISPUTED" in s for s in f.sent)


def test_a_uma_resolution_status_change_alone_is_recorded_silently(dbs):
    f = Fake()
    hist = '["proposed", "disputed", "proposed", "disputed"]'
    f.pm["0xabc"] = pm("0xabc", "will-x-happen", statuses=hist)
    go(dbs, f, 1000)
    n = len(f.sent)   # the seed's "already disputed"
    f.pm["0xabc"] = pm("0xabc", "will-x-happen", statuses=hist, status="resolved")
    go(dbs, f, 2000)
    assert len(f.sent) == n
    assert q(dbs, "select field, after from rule_events") == [("umaResolutionStatus", '"resolved"')]


def test_a_kalshi_rules_change_pages(dbs):
    f = Fake()
    go(dbs, f, 1000)
    f.k["KXTEST-1"]["rules_secondary"] = "Settles on the AP call."
    go(dbs, f, 2000)
    assert len(f.sent) == 1 and "rules_secondary" in f.sent[-1] and "KXTEST-1" in f.sent[-1]


def test_a_kalshi_close_time_change_pages_while_active_and_is_silent_once_it_is_not(dbs):
    f = Fake()
    go(dbs, f, 1000)
    f.k["KXTEST-1"]["close_time"] = "2027-03-31T00:00:00Z"   # a pushed deadline on an open market
    go(dbs, f, 2000)
    assert len(f.sent) == 1 and "close_time changed" in f.sent[0] and "KXTEST-1" in f.sent[0]
    f.k["KXTEST-1"].update(close_time="2026-11-01T00:00:00Z", status="determined")   # settlement rewrites it
    go(dbs, f, 3000)
    assert len(f.sent) == 1
    assert q(dbs, "select field from rule_events where ts_ms=3000 order by field") == [("close_time",), ("status",)]


# --- Kalshi settlement sources come from the event --------------------------------------------------------

def test_kalshi_settlement_sources_come_from_the_event_fetched_once_per_run(dbs):
    f = Fake()
    go(dbs, f, 1000)
    assert f.ev_calls == ["KXTEST"]
    assert doc(dbs, "KXTEST-1")["settlement_sources"] == [{"name": "AP", "url": "https://ap.org"}]
    assert doc(dbs, "KXTEST-2")["settlement_sources"] == [{"name": "AP", "url": "https://ap.org"}]
    f.ev["KXTEST"]["settlement_sources"] = [{"name": "ESPN", "url": "https://espn.com"}]
    go(dbs, f, 2000)
    assert f.ev_calls == ["KXTEST", "KXTEST"]
    assert len(f.sent) == 1 and f.sent[0].count("settlement_sources changed") == 2 and "ESPN" in f.sent[0]


def test_a_reseed_silences_only_the_added_field_and_a_field_in_both_docs_still_pages(dbs):
    f = Fake()
    f.ev_error = TimeoutError("events timed out")   # seeded without settlement_sources
    go(dbs, f, 1000)
    assert "settlement_sources" not in doc(dbs, "KXTEST-1")
    f.ev_error = None
    f.k["KXTEST-1"]["rules_primary"] = "If B then Yes."
    go(dbs, f, 2000)
    assert len(f.sent) == 1 and "rules_primary changed" in f.sent[0] and "settlement_sources" not in f.sent[0]
    assert set(q(dbs, "select market, field, reseed from rule_events")) == {
        ("KXTEST-1", "rules_primary", 0), ("KXTEST-1", "settlement_sources", 1),
        ("KXTEST-2", "settlement_sources", 1)}


def test_a_missing_event_is_counted_not_an_error_and_its_later_arrival_reseeds_silently(dbs):
    f = Fake()
    f.ev = {}
    go(dbs, f, 1000)
    assert q(dbs, "select n_fetched, n_ev_fetched, n_ev_missing, failing, error from runs") == [(7, 0, 1, 0, None)]
    assert "settlement_sources" not in doc(dbs, "KXTEST-1")
    f.ev = {"KXTEST": {"settlement_sources": [{"name": "AP"}]}}
    go(dbs, f, 2000)
    assert f.sent == []
    assert doc(dbs, "KXTEST-1")["settlement_sources"] == [{"name": "AP"}]
    assert set(q(dbs, "select market, field, reseed from rule_events")) == \
        {("KXTEST-1", "settlement_sources", 1), ("KXTEST-2", "settlement_sources", 1)}


def test_a_failed_event_fetch_keeps_the_stored_sources_and_is_a_partial_error(dbs):
    f = Fake()
    go(dbs, f, 1000)
    f.ev_error = TimeoutError("kalshi slow")
    go(dbs, f, 2000)
    assert f.sent == [] and q(dbs, "select count(*) from rule_events")[0][0] == 0
    assert doc(dbs, "KXTEST-1")["settlement_sources"] == [{"name": "AP", "url": "https://ap.org"}]
    assert q(dbs, "select failing from runs order by id desc limit 1") == [(0,)]
    assert "kalshi slow" in q(dbs, "select error from runs order by id desc limit 1")[0][0]


def test_a_new_field_in_the_doc_is_recorded_silently_and_pages_on_its_next_change(dbs, monkeypatch):
    f = Fake()
    go(dbs, f, 1000)
    monkeypatch.setattr(w, "PM_FIELDS", w.PM_FIELDS + ("negRisk",))
    for m in f.pm.values():
        m["negRisk"] = False
    f.pm["0xabc"]["description"] = "Edited in the same run as the deploy."
    go(dbs, f, 2000)
    assert len(f.sent) == 1 and "description changed" in f.sent[0] and "negRisk" not in f.sent[0]
    assert q(dbs, "select count(*) from rule_events where reseed=1 and field='negRisk'")[0][0] == 5
    assert q(dbs, "select reseed from rule_events where field='description'") == [(0,)]
    f.pm["0xabc"]["negRisk"] = True
    go(dbs, f, 3000)
    assert len(f.sent) == 2 and "negRisk changed" in f.sent[1]


def test_a_dispute_still_pages_during_a_reseed(dbs, monkeypatch):
    f = Fake()
    go(dbs, f, 1000)
    monkeypatch.setattr(w, "PM_FIELDS", w.PM_FIELDS + ("negRisk",))
    f.pm["0xabc"] = pm("0xabc", "will-x-happen", statuses='["proposed", "disputed"]')
    go(dbs, f, 2000)
    assert len(f.sent) == 1 and "DISPUTED" in f.sent[0]


def test_a_kalshi_market_that_404s_is_skipped_and_counted_not_an_error(dbs):
    f = Fake()
    del f.k["KXTEST-2"]
    assert go(dbs, f, 1000) == 0
    assert q(dbs, "select n_fetched, n_missing, failing, error from runs") == [(6, 1, 0, None)]


# --- outbox ----------------------------------------------------------------------------------------------

def test_a_failed_send_stays_in_the_outbox_and_the_next_run_sends_it_once(dbs):
    f = Fake()
    go(dbs, f, 1000)
    f.pm["0xabc"] = pm("0xabc", "will-x-happen", desc="Resolves YES if X or W.")
    f.notify_ok = False
    go(dbs, f, 2000)
    assert len(f.sent) == 1 and q(dbs, "select count(*) from outbox")[0][0] == 1
    assert doc(dbs, "0xabc")["description"] == "Resolves YES if X or W."   # advanced with the outbox row
    f.notify_ok = True
    go(dbs, f, 3000)
    assert len(f.sent) == 2 and f.sent[1] == f.sent[0]
    assert q(dbs, "select count(*) from outbox")[0][0] == 0
    go(dbs, f, 4000)
    assert len(f.sent) == 2


def test_a_successful_send_leaves_the_outbox_empty(dbs):
    f = Fake()
    go(dbs, f, 1000)
    f.k["KXTEST-1"]["rules_primary"] = "If B then Yes."
    go(dbs, f, 2000)
    assert len(f.sent) == 1 and q(dbs, "select count(*) from outbox")[0][0] == 0


def test_an_outbox_row_older_than_a_day_is_printed_and_dropped_not_sent(dbs, capsys):
    f = Fake()
    go(dbs, f, 1000)
    f.k["KXTEST-1"]["rules_primary"] = "If B then Yes."
    f.notify_ok = False
    go(dbs, f, 2000)
    f.notify_ok = True
    capsys.readouterr()
    go(dbs, f, 2000 + DAY_MS + 1)
    assert len(f.sent) == 1
    assert q(dbs, "select count(*) from outbox")[0][0] == 0
    out = capsys.readouterr().out
    assert "DROPPED" in out and "rules_primary" in out


# --- failure handling ------------------------------------------------------------------------------------

def test_a_failed_chunk_keeps_and_diffs_what_was_fetched_and_exits_zero(dbs):
    f = Fake()
    go(dbs, f, 1000)
    f.pm_errors = ["condition_ids chunk 0 closed=true: URLError: timed out"]
    f.pm["0xabc"] = pm("0xabc", "will-x-happen", desc="Resolves YES if X or W.")
    assert go(dbs, f, 2000) == 0
    assert len(f.sent) == 1 and "will-x-happen" in f.sent[0]
    assert "timed out" in q(dbs, "select error from runs order by id desc limit 1")[0][0]


def test_a_fetch_that_raises_is_recorded_and_kalshi_still_runs(dbs):
    f = Fake()
    go(dbs, f, 1000)

    def boom(*a):
        raise TimeoutError("gamma down")
    f.k["KXTEST-1"]["rules_primary"] = "If B then Yes."
    assert w.run(shadow_db=dbs[0], db_path=dbs[1], fetch_pm=boom, fetch_k=f.fetch_k, fetch_ev=f.fetch_ev,
                 notify=f.notify, now_ms=2000) == 0
    assert len(f.sent) == 1 and "rules_primary" in f.sent[0]
    assert "gamma down" in q(dbs, "select error from runs order by id desc limit 1")[0][0]


def test_fetching_nothing_while_tracking_something_is_a_failing_run(dbs):
    f = Fake()
    outage(f)
    go(dbs, f, 1000)
    assert q(dbs, "select failing from runs") == [(1,)]
    assert "fetched 0 of 7" in q(dbs, "select error from runs")[0][0]


def test_fetching_fewer_than_half_is_failing_and_half_is_not(dbs):
    f = Fake()
    for key in ("0xabc", "0xdef", "1640919"):
        del f.pm[key]
    go(dbs, f, 1000)            # 4 of 7
    for key in (TOK_Z,):
        del f.pm[key]
    go(dbs, f, 2000)            # 3 of 7
    assert q(dbs, "select n_fetched, failing from runs order by id") == [(4, 0), (3, 1)]


def test_three_failing_runs_page_once_and_recovery_pages_once(dbs):
    f = Fake()
    outage(f)
    for t in (1000, 2000):
        assert go(dbs, f, t) == 0
    assert f.sent == []
    go(dbs, f, 3000)
    assert len(f.sent) == 1 and "3 runs in a row" in f.sent[0] and "fetched 0 of 7" in f.sent[0]
    go(dbs, f, 4000)
    assert len(f.sent) == 1
    f = Fake()
    go(dbs, f, 5000)
    assert len(f.sent) == 1 and "recovered" in f.sent[0]
    go(dbs, f, 6000)
    assert len(f.sent) == 1


def test_partial_errors_are_recorded_and_printed_but_never_page(dbs, capsys):
    f = Fake()
    for t, err in ((1000, ["chunk boom"]), (2000, []), (3000, ["chunk boom"]), (4000, ["chunk boom"]),
                   (5000, ["chunk boom"]), (6000, ["chunk boom"])):
        f.pm_errors = err
        go(dbs, f, t)
    assert f.sent == []
    assert [r[0] for r in q(dbs, "select failing from runs order by id")] == [0] * 6
    assert q(dbs, "select count(*) from runs where error like '%chunk boom%'")[0][0] == 5
    assert "ERROR chunk boom" in capsys.readouterr().out


def test_an_outage_streak_must_be_consecutive(dbs):
    f, healthy = Fake(), Fake()
    outage(f)
    for t, fk in ((1000, f), (2000, healthy), (3000, f), (4000, f)):
        go(dbs, fk, t)
    assert f.sent == [] and healthy.sent == []
    go(dbs, f, 5000)
    assert len(f.sent) == 1 and "FAILING" in f.sent[0]


def test_a_permanent_partial_error_does_not_mask_a_later_full_outage(dbs):
    f = Fake()
    f.pm_errors = ["condition_ids chunk 1 closed=true: HTTPError 500"]
    for t in (1000, 2000, 3000, 4000):
        go(dbs, f, t)
    assert f.sent == []
    outage(f)
    for t in (5000, 6000):
        go(dbs, f, t)
    assert f.sent == []
    go(dbs, f, 7000)
    assert len(f.sent) == 1 and "FAILING" in f.sent[0] and "fetched 0 of 7" in f.sent[0]


def test_a_failed_streak_page_is_retried_next_run(dbs):
    f = Fake()
    outage(f)
    calls = []
    for t in (1000, 2000, 3000, 4000):
        w.run(shadow_db=dbs[0], db_path=dbs[1], fetch_pm=f.fetch_pm, fetch_k=f.fetch_k, fetch_ev=f.fetch_ev,
              notify=lambda text: calls.append(text) and False, now_ms=t)
    assert len(calls) == 2


# --- message packing -------------------------------------------------------------------------------------

def test_every_change_is_sent(dbs):
    f = Fake()
    go(dbs, f, 1000)
    for key, m in f.pm.items():
        f.pm[key] = dict(m, description="z " * 2000, resolutionSource="https://other", endDate="2027-01-01")
    go(dbs, f, 2000)
    assert sum(s.count(" changed") for s in f.sent) == 15 and all(len(s) <= w.MAX_MSG for s in f.sent)


def test_many_messages_are_split_under_the_telegram_limit():
    texts = w._pack([f"m{i} " + "x" * 1000 for i in range(10)] + ["y" * 9000])
    assert len(texts) >= 4 and all(len(s) <= w.MAX_MSG for s in texts)
    assert all(any(f"m{i} " in s for s in texts) for i in range(10))


# --- real fetchers and notifier --------------------------------------------------------------------------

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
            return [{"conditionId": "0x00"}]   # the same market as a condition id already returned
        return []
    monkeypatch.setattr(w, "_get", fake_get)
    errors = []
    cids = {f"0x{i:02d}" for i in range(25)}
    out = w.fetch_pm(cids, {"1640919"}, {TOK_Z}, errors)
    assert errors == []
    assert sorted(len(c["condition_ids"]) for c in calls if "condition_ids" in c) == [5, 5, 20, 20]
    assert sorted(c["closed"][0] for c in calls if "id" in c) == ["false", "true"]
    assert all(c.get("limit") == ["20"] for c in calls) and len(calls) == 8
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


@pytest.mark.parametrize("name,arg,path", [("fetch_k", "KXGONE-1", "/markets/KXGONE-1"),
                                           ("fetch_event", "KXGONE", "/events/KXGONE")])
def test_kalshi_fetchers_return_none_on_404_and_raise_otherwise(monkeypatch, name, arg, path):
    fn = getattr(w, name)
    urls = []

    def gone(url, timeout=20):
        urls.append(url)
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)
    monkeypatch.setattr(w, "_get", gone)
    assert fn(arg) is None and urls[0].endswith(path)

    def busy(url, timeout=20):
        raise urllib.error.HTTPError(url, 429, "slow down", {}, None)
    monkeypatch.setattr(w, "_get", busy)
    with pytest.raises(urllib.error.HTTPError):
        fn(arg)


def test_the_real_notifier_refuses_under_pytest(monkeypatch):
    called = []
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: called.append(a))
    assert w.infra_notify("should never be sent") is False
    assert called == []
