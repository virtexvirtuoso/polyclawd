"""Cross-poll trade dedup for the smart-wallet fast poll.

Context: `_LOOKBACK_SECS` was widened 180s -> 600s because the PM trades feed's
head lags ~3.5 min, so the old window fetched 0 trades on every poll and the
scanner was structurally blind. A wider window overlaps successive polls, and the
downstream `_accumulate` has no per-trade dedup (trade identity is lost before it
gets there), so the same fill would inflate `total_usd` against the $1000
cumulative THRESHOLD. Measured pre-fix: 7.8% of accumulator entries were repeats,
dominant gap 90s == the poll interval.

No network, no real DB — an in-memory sqlite connection stands in for the ledger.
"""

import ast
import sqlite3

import pytest

import scripts.smart_wallet_fast_poll as sw


def _trade(tx="0xaa", asset="tok1", wallet="0xw1", side="BUY", size=10, price=0.5):
    return {"transactionHash": tx, "asset": asset, "proxyWallet": wallet,
            "side": side, "size": size, "price": price}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


def test_first_poll_passes_everything(conn):
    trades = [_trade(tx=f"0x{i}") for i in range(5)]
    assert len(sw._dedupe_unseen_trades(conn, trades, 1000)) == 5


def test_second_poll_filters_repeats(conn):
    trades = [_trade(tx=f"0x{i}") for i in range(5)]
    sw._dedupe_unseen_trades(conn, trades, 1000)
    assert sw._dedupe_unseen_trades(conn, trades, 1090) == []


def test_only_new_trades_survive_an_overlapping_poll(conn):
    first = [_trade(tx=f"0x{i}") for i in range(5)]
    sw._dedupe_unseen_trades(conn, first, 1000)
    overlap = first[3:] + [_trade(tx="0x99"), _trade(tx="0x98")]
    out = sw._dedupe_unseen_trades(conn, overlap, 1090)
    assert {t["transactionHash"] for t in out} == {"0x99", "0x98"}


def test_same_tx_hash_different_fills_are_distinct(conn):
    """One transaction can carry several fills — 7500 trades / 4856 hashes measured.

    Shaped as TWO polls deliberately. A first-poll batch returns N for any key
    function, because `seen` is empty — a single-poll assert here is true by
    construction and survived a mutant that replaced _trade_key with a constant.
    Only the second poll can observe key granularity.
    """
    a = _trade(tx="0xsame", asset="tokA", size=10)
    b = _trade(tx="0xsame", asset="tokB", size=10)
    c = _trade(tx="0xsame", asset="tokA", size=25)
    assert len(sw._dedupe_unseen_trades(conn, [a, b, c], 1000)) == 3, (
        "distinct fills sharing a tx hash were wrongly collapsed"
    )
    d = _trade(tx="0xsame", asset="tokC", size=10)
    out = sw._dedupe_unseen_trades(conn, [a, b, c, d], 1090)
    assert [t["asset"] for t in out] == ["tokC"], (
        "second poll did not suppress exactly the true repeats"
    )


@pytest.mark.parametrize("field,other", [
    ("side", "SELL"),
    ("proxyWallet", "0xOTHERWALLET"),
    ("asset", "tokZZ"),
])
def test_every_key_component_survives_a_second_poll(conn, field, other):
    """Pins side/proxyWallet/asset in the key. Dropping proxyWallet would make a
    SECOND wallet's identical-looking fill be suppressed as a repeat — a silent
    drop of a real, distinct signal. Mutants removing each of these survived the
    old single-poll test."""
    base = _trade(tx="0xg", asset="tokA", wallet="0xw1", side="BUY")
    assert len(sw._dedupe_unseen_trades(conn, [base], 1000)) == 1
    variant = dict(base)
    variant[field] = other
    out = sw._dedupe_unseen_trades(conn, [base, variant], 1090)
    assert out == [variant], f"{field} is not contributing to trade identity"


def test_repeat_of_one_fill_within_a_shared_tx_is_caught(conn):
    a = _trade(tx="0xsame", asset="tokA", size=10)
    b = _trade(tx="0xsame", asset="tokB", size=10)
    sw._dedupe_unseen_trades(conn, [a, b], 1000)
    assert sw._dedupe_unseen_trades(conn, [a], 1090) == []


def test_old_keys_are_pruned_so_the_table_cannot_grow_unbounded(conn):
    sw._dedupe_unseen_trades(conn, [_trade(tx="0xold")], 1000)
    # far beyond the 4x-window retention
    sw._dedupe_unseen_trades(conn, [_trade(tx="0xnew")], 1000 + 10 * sw._LOOKBACK_SECS)
    rows = conn.execute(f"SELECT key FROM {sw._SEEN_TABLE}").fetchall()
    assert len(rows) == 1 and "0xnew" in rows[0][0]


def test_pruned_trade_may_reappear_but_only_after_the_retention_window(conn):
    t = _trade(tx="0xold")
    sw._dedupe_unseen_trades(conn, [t], 1000)
    assert sw._dedupe_unseen_trades(conn, [t], 1000 + 10 * sw._LOOKBACK_SECS) == [t]


def test_empty_input_is_a_noop(conn):
    assert sw._dedupe_unseen_trades(conn, [], 1000) == []


def test_dedup_failure_never_drops_trades(monkeypatch):
    """A broken ledger must degrade to passing everything, not to silence."""
    class Broken:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

    sw._mem_seen.clear()  # shares module state with the in-process LRU fallback
    trades = [_trade(tx="0x1"), _trade(tx="0x2")]
    assert sw._dedupe_unseen_trades(Broken(), trades, 1000) == trades


def test_lookback_exceeds_the_measured_feed_lag():
    """The feed head lagged ~3.5 min (210s); 180s sat inside it and saw nothing."""
    assert sw._LOOKBACK_SECS > 210, "lookback is back inside the feed lag — scanner goes blind"


# --------------------------------------------------------------------------- #
# /qa round 2 — regressions for two defects a fresh critic falsified
# --------------------------------------------------------------------------- #

def test_json_type_drift_does_not_fork_the_key():
    """DEFECT 1: str() made 100, 100.0 and "100" three keys for ONE fill.

    The PM feed is JSON, so a numeric can arrive as int, float or string across
    polls — which defeated dedup on exactly the repeats it exists to catch.
    """
    base = dict(transactionHash="0xa", asset="t1", proxyWallet="0xw", side="BUY")
    k_int = sw._trade_key({**base, "size": 100, "price": 0.5})
    k_flt = sw._trade_key({**base, "size": 100.0, "price": 0.50})
    k_str = sw._trade_key({**base, "size": "100", "price": "0.5"})
    assert k_int == k_flt == k_str


def test_genuinely_different_sizes_stay_distinct():
    base = dict(transactionHash="0xa", asset="t1", proxyWallet="0xw", side="BUY", price=0.5)
    assert sw._trade_key({**base, "size": 100}) != sw._trade_key({**base, "size": 101})
    assert sw._trade_key({**base, "size": 100}) != sw._trade_key({**base, "size": 100.5})


def test_type_drift_repeat_is_actually_suppressed_end_to_end(conn):
    """The key fix is worthless if the filter still lets the repeat through."""
    t1 = _trade(tx="0xdrift", size=100, price=0.5)
    t2 = _trade(tx="0xdrift", size=100.0, price=0.50)
    assert len(sw._dedupe_unseen_trades(conn, [t1], 1000)) == 1
    assert sw._dedupe_unseen_trades(conn, [t2], 1090) == []


def test_db_failure_still_dedups_in_process():
    """DEFECT 2: the except path returned ALL trades, double-counting into the
    $1000 cumulative threshold. It now falls back to a bounded in-process LRU."""
    class Broken:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

    sw._mem_seen.clear()
    trades = [_trade(tx="0xf1"), _trade(tx="0xf2")]
    first = sw._dedupe_unseen_trades(Broken(), trades, 1000)
    assert first == trades, "first sighting must pass — failing closed would drop signals"
    second = sw._dedupe_unseen_trades(Broken(), trades, 1090)
    assert second == [], "repeat during DB failure must NOT be passed through again"


def test_db_failure_still_admits_genuinely_new_trades():
    class Broken:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

    sw._mem_seen.clear()
    sw._dedupe_unseen_trades(Broken(), [_trade(tx="0xg1")], 1000)
    out = sw._dedupe_unseen_trades(Broken(), [_trade(tx="0xg1"), _trade(tx="0xg2")], 1090)
    assert [t["transactionHash"] for t in out] == ["0xg2"]


def test_in_process_lru_is_bounded():
    sw._mem_seen.clear()
    sw._mem_filter(*(lambda ts: ([sw._trade_key(t) for t in ts], ts))(
        [_trade(tx=f"0x{i}") for i in range(sw._MEM_SEEN_MAX + 500)]), 1000)
    assert len(sw._mem_seen) == sw._MEM_SEEN_MAX


# --------------------------------------------------------------------------- #
# /qa rounds 3-5 — the three CONDITIONAL-PASS conditions.
#
# Round 3 and round 4 critics each found NEW defects in the alarm's interacting
# parts (streak x window x cooldown x re-arm x delivery) and none in the dedup
# itself. Round 5 replaces that machine with ONE signal, a fixed cooldown and a
# bounded retry, so the defect classes below are gone by construction rather
# than patched. These tests pin that they stay gone.
# --------------------------------------------------------------------------- #

_CLOCK_T0 = 10 * 86400.0  # must exceed the longest cooldown, or the FIRST page
                          # is blocked by the 0.0 baseline and every assertion
                          # below it is silently vacuous.
POLL_SECS = 90


class _Broken:
    def execute(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")


@pytest.fixture(autouse=True)
def _reset_module_state():
    sw._dedup_recent.clear()
    sw._dedup_alert_last = 0.0
    sw._dedup_retry_attempts = 0
    sw._dedup_retry_next = 0.0
    sw._mem_seen.clear()
    yield


def _page_sink(monkeypatch, delivered=True, sink=None):
    """Capture pages. Pass `sink` to keep recording into the SAME list after a
    re-patch — otherwise a retry lands in a fresh list and reads as 'no retry'."""
    sent = [] if sink is None else sink

    def fake(msg):
        sent.append(msg)
        return delivered

    monkeypatch.setattr("scripts.alert_formatter.send_telegram", fake)
    return sent


def _clock(monkeypatch):
    c = [_CLOCK_T0]
    monkeypatch.setattr("time.time", lambda: c[0])
    return c


def _degraded(n, t0=1000, tag="d"):
    for i in range(n):
        sw._dedupe_unseen_trades(_Broken(), [_trade(tx=f"0x{tag}{i}")], t0 + i)


# --- (a) the two dedup paths must agree, and both must suppress ------------- #

def test_the_two_dedup_paths_agree_on_intra_batch_repeats(conn):
    """CONDITION (a). The fallback returned 1 where the primary returned 2 — a
    silent drop occurring only while the ledger was already broken."""
    batch = [_trade(tx="0xdup"), _trade(tx="0xdup")]
    mem = sw._mem_filter([sw._trade_key(x) for x in batch], batch, 1000)
    sw._mem_seen.clear()
    db = sw._dedupe_unseen_trades(conn, batch, 1000)
    assert len(mem) == len(db), f"fallback kept {len(mem)}, primary kept {len(db)}"
    assert len(db) == 1, "an intra-batch pagination repeat reached the accumulator"


def test_primary_path_collapses_intra_batch_pagination_repeats(conn):
    """Measured on the production feed 2026-08-26: `fetch_pm_trades_since` pages
    by offset over a live newest-first stream, so rows shift across the page
    boundary and are re-served. 4 pages -> 0 extra rows; 14 pages -> 1,878 extra
    of 6,500 kept. Independently re-measured: 22,960 rows, ZERO intra-page
    duplicates, every duplicate cross-page and byte-identical on all 19 fields.
    """
    batch = [_trade(tx="0xa"), _trade(tx="0xa"), _trade(tx="0xa"), _trade(tx="0xb")]
    out = sw._dedupe_unseen_trades(conn, batch, 1000)
    assert [x["transactionHash"] for x in out] == ["0xa", "0xb"]


def test_intra_batch_collapse_keeps_the_first_occurrence():
    """The copies must differ on an UNKEYED field, or first-vs-last is
    unobservable — a mutant keeping the LAST copy survived the identical-rows
    version of this test."""
    first = dict(_trade(tx="0xa"), timestamp=111)
    later = dict(_trade(tx="0xa"), timestamp=999)
    assert sw._trade_key(first) == sw._trade_key(later), "fixture must share a key"
    keys, trades = sw._collapse_intra_batch(
        [sw._trade_key(x) for x in (first, later)], [first, later])
    assert len(trades) == 1
    assert trades[0]["timestamp"] == 111, "collapse kept the last copy, not the first"
    assert keys == [sw._trade_key(first)]


def test_mem_filter_still_suppresses_cross_batch_repeats():
    """The measured cross-poll population (7.8%, dominant gap 90s == the poll
    interval) must stay suppressed.

    Uses epoch-scale timestamps deliberately: at t=1000 the TTL cutoff is
    negative, so a mutant stamping a CONSTANT sighting time is indistinguishable
    from a correct one and survives.
    """
    t0 = 1_700_000_000
    batch = [_trade(tx="0xr1")]
    keys = [sw._trade_key(x) for x in batch]
    assert len(sw._mem_filter(keys, batch, t0)) == 1
    assert sw._mem_filter(keys, batch, t0 + POLL_SECS) == []


def test_lru_records_the_actual_sighting_time():
    """A constant stamp makes every entry instantly expired (or immortal), which
    silently converts the fallback into a no-op."""
    t0 = 1_700_000_000
    k = sw._trade_key(_trade(tx="0xstamp"))
    sw._mem_record([k], t0)
    assert sw._mem_seen[k] == t0
    sw._mem_prune(t0 + POLL_SECS)
    assert k in sw._mem_seen, "a freshly-seen key was pruned"


def test_prune_expires_old_keys_hiding_behind_a_refreshed_one():
    """_mem_prune walks from the front and STOPS at the first live entry, so
    recording must move a re-seen key to the back. Without that, one refreshed
    key parks at the front and shields every stale entry behind it forever."""
    t0 = 1_700_000_000
    old_k = sw._trade_key(_trade(tx="0xold"))
    other = sw._trade_key(_trade(tx="0xother"))
    sw._mem_record([old_k, other], t0)
    sw._mem_record([old_k], t0 + 10 * sw._LOOKBACK_SECS)   # re-seen, still live
    sw._mem_prune(t0 + 10 * sw._LOOKBACK_SECS)
    assert old_k in sw._mem_seen, "the refreshed key was wrongly expired"
    assert other not in sw._mem_seen, "a stale key survived behind a refreshed one"


def test_success_path_warms_the_lru_so_the_fallback_is_not_empty(conn):
    """W1. The success path never wrote the LRU, so it was EMPTY at every
    fallback onset and the first DB failure re-admitted the whole overlapping
    600s window — not just 'after a restart', as the old comment claimed."""
    x = _trade(tx="0xwarm")
    assert len(sw._dedupe_unseen_trades(conn, [x], 1000)) == 1
    assert len(sw._mem_seen) == 1, "success path did not warm the LRU"
    assert sw._dedupe_unseen_trades(_Broken(), [x], 1090) == [], (
        "fallback onset re-admitted an already-alerted fill"
    )


def test_lru_expires_on_the_same_retention_as_the_ledger(conn):
    """W1 (round 4). With no TTL the fallback becomes STRICTER than the primary
    once the DB prunes at 4x the window — an under-count, the wrong side of the
    fail-open contract."""
    x = _trade(tx="0xttl")
    later = 1000 + 10 * sw._LOOKBACK_SECS
    assert len(sw._dedupe_unseen_trades(conn, [x], 1000)) == 1
    assert sw._dedupe_unseen_trades(conn, [x], later) == [x], "primary must re-admit"
    sw._mem_seen.clear()
    sw._mem_record([sw._trade_key(x)], 1000)
    assert sw._mem_filter([sw._trade_key(x)], [x], later) == [x], (
        "fallback is stricter than the primary after retention"
    )


def test_in_process_lru_is_still_bounded():
    sw._mem_filter(*(lambda ts: ([sw._trade_key(x) for x in ts], ts))(
        [_trade(tx=f"0x{i}") for i in range(sw._MEM_SEEN_MAX + 500)]), 1000)
    assert len(sw._mem_seen) == sw._MEM_SEEN_MAX


# --- (b) a degraded ledger must alarm, and the alarm must stay proportionate - #

def test_a_single_fallback_does_not_page(monkeypatch):
    sent = _page_sink(monkeypatch)
    _clock(monkeypatch)
    _degraded(1)
    assert sent == [], "a transient DB blip must not page"


def test_a_sustained_fault_pages(monkeypatch):
    sent = _page_sink(monkeypatch)
    _clock(monkeypatch)
    _degraded(sw._DEDUP_FALLBACK_ALARM_AT)
    assert len(sent) == 1 and "DEDUP DEGRADED" in sent[0]


def test_the_page_is_rate_limited_not_repeated_every_poll(monkeypatch):
    sent = _page_sink(monkeypatch)
    c = _clock(monkeypatch)
    for i in range(sw._DEDUP_FALLBACK_ALARM_AT + 50):
        c[0] += POLL_SECS
        sw._dedupe_unseen_trades(_Broken(), [_trade(tx=f"0xq{i}")], 1000 + i)
    assert len(sent) == 1, "the alarm must not spam every 90s poll"


def test_an_intermittent_ledger_fault_still_pages(conn, monkeypatch):
    """A consecutive-only counter is reset by every lucky poll: a 2-fail/1-ok
    cycle ran 400 degraded polls and paged ZERO times. SQLITE_BUSY is
    intermittent by construction and is this repo's documented lock symptom."""
    sent = _page_sink(monkeypatch)
    c = _clock(monkeypatch)
    for i in range(60):
        c[0] += POLL_SECS
        conn_or_broken = conn if i % 3 == 2 else _Broken()
        sw._dedupe_unseen_trades(conn_or_broken, [_trade(tx=f"0xi{i}")], 1000 + i)
    assert sent, "a chronic intermittent fault never paged"


def test_a_flapping_ledger_cannot_drown_the_alert_channel(conn, monkeypatch):
    """Round-4 defect 1: the re-arm made a 3-fail/1-ok flap page 48x/day — 12x a
    TOTAL outage — on the channel that also carries live-fill alerts."""
    sent = _page_sink(monkeypatch)
    c = _clock(monkeypatch)
    for i in range(960):  # 24h at a 90s poll
        c[0] += POLL_SECS
        conn_or_broken = conn if i % 4 == 3 else _Broken()
        sw._dedupe_unseen_trades(conn_or_broken, [_trade(tx=f"0xf{i}")], 1000 + i)
    assert sent, "a chronic flap must still page"
    ceiling = 86400 / sw._DEDUP_FALLBACK_ALERT_COOLDOWN + 1
    assert len(sent) <= ceiling, f"flapping ledger paged {len(sent)}x in 24h"


def test_a_total_outage_is_never_quieter_than_a_flap(conn, monkeypatch):
    """The severity inversion round 4 found: the MILDER fault paged 12x louder."""
    c = _clock(monkeypatch)
    flap = _page_sink(monkeypatch)
    for i in range(960):
        c[0] += POLL_SECS
        conn_or_broken = conn if i % 4 == 3 else _Broken()
        sw._dedupe_unseen_trades(conn_or_broken, [_trade(tx=f"0xf{i}")], 1000 + i)

    sw._dedup_recent.clear()
    sw._dedup_alert_last = 0.0
    c[0] = _CLOCK_T0
    outage = _page_sink(monkeypatch)
    for i in range(960):
        c[0] += POLL_SECS
        sw._dedupe_unseen_trades(_Broken(), [_trade(tx=f"0xo{i}")], 1000 + i)
    assert len(flap) <= len(outage), (
        f"intermittent fault paged {len(flap)}x vs {len(outage)}x for a total outage"
    )


def test_a_single_blip_after_a_drained_window_does_not_page(conn, monkeypatch):
    """Round-4 defect 3: the window stayed hot after an outage, so one isolated
    blip paged — against the module's own 'one failure is transient noise'."""
    sent = _page_sink(monkeypatch)
    c = _clock(monkeypatch)
    _degraded(sw._DEDUP_FALLBACK_ALARM_AT)
    assert len(sent) == 1
    for i in range(sw._DEDUP_FALLBACK_WINDOW):
        c[0] += POLL_SECS
        sw._dedupe_unseen_trades(conn, [_trade(tx=f"0xh{i}")], 2000 + i)
    c[0] += sw._DEDUP_FALLBACK_ALERT_COOLDOWN + 60  # cooldown fully expired
    sw._dedupe_unseen_trades(_Broken(), [_trade(tx="0xblip")], 3000)
    assert len(sent) == 1, "a single blip paged after the window had drained"


def test_successes_drain_the_window(conn):
    """Without recording successes the window fills with failures and never
    recovers — the alarm latches on forever."""
    _degraded(sw._DEDUP_FALLBACK_ALARM_AT)
    assert sw._dedup_alarm_due()
    for i in range(sw._DEDUP_FALLBACK_WINDOW):
        sw._dedupe_unseen_trades(conn, [_trade(tx=f"0xg{i}")], 2000 + i)
    assert not sw._dedup_alarm_due(), "healthy polls never drained the window"


def test_a_failed_page_is_retried_and_never_stamps_the_cooldown(monkeypatch):
    """Round-3 defect 1: the cooldown was stamped BEFORE the send, so one flaky
    Telegram call bought 6h of silence while the fault ran on."""
    attempts = _page_sink(monkeypatch, delivered=False)
    c = _clock(monkeypatch)
    _degraded(sw._DEDUP_FALLBACK_ALARM_AT)
    assert len(attempts) == 1
    assert sw._dedup_alert_last == 0.0, "a failed send stamped the cooldown"
    _page_sink(monkeypatch, delivered=True, sink=attempts)  # channel recovers
    c[0] += sw._DEDUP_RETRY_BACKOFF + 1
    sw._dedupe_unseen_trades(_Broken(), [_trade(tx="0xfx")], 2000)
    assert len(attempts) == 2, "the undelivered page was never retried"
    assert sw._dedup_alert_last > 0, "a delivered page did not stamp the cooldown"


def test_alert_retries_are_bounded_so_a_dead_channel_cannot_stall_the_scan(monkeypatch):
    """Round-4 defect 2: retrying at poll cadence meant 238 blocking sends in 240
    polls. Each send is up to 2 subprocesses with a 30s timeout, against a
    measured 20-48s feed fetch inside a 90s poll budget."""
    attempts = _page_sink(monkeypatch, delivered=False)
    c = _clock(monkeypatch)
    for i in range(240):  # 6h of failing polls
        c[0] += POLL_SECS
        sw._dedupe_unseen_trades(_Broken(), [_trade(tx=f"0xr{i}")], 1000 + i)
    assert attempts, "must attempt delivery at least once"
    assert len(attempts) <= 30, f"{len(attempts)} blocking sends in 6h of polls"


def test_exhausted_retries_pause_but_never_buy_six_hours_of_silence(monkeypatch):
    """The page is still OWED. Stamping the 6h cooldown when delivery failed is
    the round-3 defect in a new place: the operator hears nothing while the
    ledger stays broken."""
    attempts = _page_sink(monkeypatch, delivered=False)
    c = _clock(monkeypatch)
    _degraded(sw._DEDUP_FALLBACK_ALARM_AT)
    for _ in range(sw._DEDUP_RETRY_MAX):
        c[0] = max(c[0], sw._dedup_retry_next) + 1
        sw._dedupe_unseen_trades(_Broken(), [_trade(tx="0xex")], 2000)
    assert len(attempts) >= sw._DEDUP_RETRY_MAX
    assert sw._dedup_alert_last == 0.0, "an undelivered page stamped the cooldown"

    _page_sink(monkeypatch, delivered=True, sink=attempts)
    c[0] += sw._DEDUP_RETRY_PAUSE + 1
    before = len(attempts)
    sw._dedupe_unseen_trades(_Broken(), [_trade(tx="0xex2")], 2100)
    assert len(attempts) == before + 1, (
        "after the retry pause the still-owed page was never sent"
    )
    assert sw._dedup_alert_last > 0
    assert c[0] - _CLOCK_T0 < sw._DEDUP_FALLBACK_ALERT_COOLDOWN, (
        "delivery took longer than the cooldown — this test proves nothing"
    )


def test_retry_backoff_grows(monkeypatch):
    _page_sink(monkeypatch, delivered=False)
    c = _clock(monkeypatch)
    _degraded(sw._DEDUP_FALLBACK_ALARM_AT)
    first_gap = sw._dedup_retry_next - c[0]
    c[0] += first_gap
    sw._dedupe_unseen_trades(_Broken(), [_trade(tx="0xb2")], 2000)
    assert sw._dedup_retry_next - c[0] > first_gap, "backoff did not grow"


def test_an_empty_poll_is_not_recorded_at_all(conn):
    """An empty poll returns before any DB work — it is evidence of neither
    health nor degradation."""
    _degraded(1)
    assert list(sw._dedup_recent) == [1]
    sw._dedupe_unseen_trades(conn, [], 1090)
    assert list(sw._dedup_recent) == [1]


def test_a_commit_failure_is_not_counted_as_recovery():
    """_note_dedup_ok() must sit AFTER conn.commit(), or a ledger that reads fine
    but cannot write reads as healthy forever."""
    class CommitBroken:
        def execute(self, *a, **k):
            return []

        def executemany(self, *a, **k):
            return None

        def commit(self):
            raise sqlite3.OperationalError("disk I/O error")

    _degraded(1)
    sw._dedupe_unseen_trades(CommitBroken(), [_trade(tx="0xc2")], 1090)
    assert list(sw._dedup_recent) == [1, 1], "a commit failure read as recovery"


def test_a_broken_alert_channel_cannot_break_the_scan(monkeypatch):
    def boom(_):
        raise RuntimeError("telegram down")

    monkeypatch.setattr("scripts.alert_formatter.send_telegram", boom)
    _clock(monkeypatch)
    out = None
    for i in range(sw._DEDUP_FALLBACK_ALARM_AT):
        out = sw._dedupe_unseen_trades(_Broken(), [_trade(tx="0xb1")], 1000 + i)
    assert out == [], "the scan must still return, and still dedup, if paging fails"


def test_alarm_constants_cannot_silently_disable_the_alarm():
    """Both alarm tests loop over `_DEDUP_FALLBACK_ALARM_AT`, so they are
    construction-relative and a config change to 100 polls would keep them green."""
    assert sw._DEDUP_FALLBACK_ALARM_AT >= 2, "one blip must not page"
    assert sw._DEDUP_FALLBACK_ALARM_AT * POLL_SECS < sw._LOOKBACK_SECS, (
        "must page before a full lookback window has gone unsuppressed"
    )
    assert sw._DEDUP_FALLBACK_ALARM_AT <= sw._DEDUP_FALLBACK_WINDOW
    assert sw._dedup_recent.maxlen == sw._DEDUP_FALLBACK_WINDOW
    assert sw._DEDUP_FALLBACK_ALERT_COOLDOWN <= 86400, "a page a day is not a monitor"
    assert 1 <= sw._DEDUP_RETRY_MAX <= 5
    assert sw._DEDUP_RETRY_PAUSE < sw._DEDUP_FALLBACK_ALERT_COOLDOWN


def test_run_still_wires_the_dedup_in():
    """No test exercises run() — it routes to the live executor — so the wiring
    is guarded statically. A mutant deleting the call site left the suite green."""
    import inspect

    tree = ast.parse(inspect.getsource(sw.run))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_dedupe_unseen_trades" in called, (
        "run() no longer dedups — the overlapping lookback window now reaches "
        "the accumulator raw"
    )


# --- (c) price normalisation — mutation proved it was untested theatre ------ #

def _price_base(**kw):
    return dict(transactionHash="0xp", asset="t1", proxyWallet="0xw",
                side="BUY", size=10, **kw)


def test_price_type_drift_does_not_fork_the_key():
    """CONDITION (c). The old drift test used 0.5 / 0.50 / "0.5" — str() maps all
    three to "0.5", so it passed identically with and without _norm on price."""
    k = sw._trade_key(_price_base(price=0.5))
    for variant in ("0.500", "0.50", "0.5000000", " 0.5", 5e-1, 0.50):
        assert k == sw._trade_key(_price_base(price=variant)), (
            f"price {variant!r} forked the key — _norm is not applied to price"
        )


def test_genuinely_different_prices_stay_distinct():
    k = sw._trade_key(_price_base(price=0.5))
    for variant in (0.51, 0.4999, "0.501", 0.9):
        assert k != sw._trade_key(_price_base(price=variant)), (
            f"price {variant!r} collapsed onto 0.5 — normalisation is too lossy"
        )


def test_price_is_actually_part_of_the_key():
    """Guards the other direction: dropping price from the key entirely would
    also make the drift test above pass."""
    assert sw._trade_key(_price_base(price=0.10)) != sw._trade_key(_price_base(price=0.90))


def test_price_drift_repeat_is_suppressed_end_to_end(conn):
    assert len(sw._dedupe_unseen_trades(conn, [_trade(tx="0xpd", price=0.5)], 1000)) == 1
    assert sw._dedupe_unseen_trades(conn, [_trade(tx="0xpd", price="0.500")], 1090) == [], (
        "a trailing-zero price string re-admitted an already-seen fill"
    )


def test_price_drift_is_suppressed_on_the_fallback_path_too():
    """Both paths must normalise, or dedup quality changes when sqlite hiccups."""
    assert sw._dedupe_unseen_trades(_Broken(), [_trade(tx="0xpf", price=0.5)], 1000)
    assert sw._dedupe_unseen_trades(_Broken(), [_trade(tx="0xpf", price="0.500")], 1090) == []
