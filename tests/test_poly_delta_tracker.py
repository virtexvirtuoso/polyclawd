"""poly_delta_tracker._get_mid_price endpoint correctness.

Bug (2026-06-20): used GAMMA /markets/{condition_id} (path) -> HTTP 422, so
poly_delta_60/300 never populated (0/331 fills). The working modules use
GAMMA /markets?condition_ids={id} (query param), which returns a LIST.
"""


def _fake_fetch_factory(calls):
    def fake_fetch(url, timeout=8):
        calls.append(url)
        if "/markets?condition_ids=" in url:
            return [{"clobTokenIds": '["tokA","tokB"]', "outcomes": '["Yes","No"]'}]
        if "/book" in url:
            return {"bids": [{"price": "0.40"}], "asks": [{"price": "0.44"}]}
        return None
    return fake_fetch


def test_get_mid_uses_condition_ids_query_and_unwraps_list(monkeypatch):
    from services import poly_delta_tracker as pdt
    calls = []
    monkeypatch.setattr(pdt, "_fetch_json", _fake_fetch_factory(calls))

    mid = pdt._get_mid_price("0xabc", "YES")

    assert mid == 0.42  # (0.40 + 0.44) / 2
    assert any("/markets?condition_ids=0xabc" in u for u in calls)
    # must NOT use the broken path-style endpoint
    assert not any(u.rstrip("/").endswith("/markets/0xabc") for u in calls)


def test_get_mid_picks_token_by_side(monkeypatch):
    from services import poly_delta_tracker as pdt
    monkeypatch.setattr(pdt, "_fetch_json", _fake_fetch_factory([]))
    # NO side -> still returns a valid mid (token resolution must not crash)
    assert pdt._get_mid_price("0xabc", "NO") == 0.42
