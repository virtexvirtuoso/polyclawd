"""Task 1.2c-2c: the paper stop alert helpers describe paper closes only.

_send_discord_alert and _send_stop_close_telegram used to prefer an "outcome" label that
only a live-close result carried (so they could announce a live result as a paper close).
Paper loops no longer produce live results; these helpers read the paper row's side."""

import services.stop_evaluator as se

PAPER_RESULT = {
    "position_id": 7, "market_title": "Paper market on July 20?", "side": "NO",
    "entry_price": 0.5, "current_price": 0.73, "pnl": -4.6, "bet_size": 10.0,
    "reason": "UNIVERSAL STOP loss 46% >= 40% threshold (all strategies)", "strategy": "",
}


def test_telegram_stop_close_names_the_paper_side(monkeypatch):
    import scripts.openclaw_alerts as oa
    sent = []
    monkeypatch.setattr(oa, "alert_openclaw", lambda msg, **kw: sent.append(msg) or True)
    se._send_stop_close_telegram(dict(PAPER_RESULT, outcome="Yes"))   # a stray live-style key
    assert len(sent) == 1
    assert "Side: NO |" in sent[0] and "STOP-LOSS CLOSED" in sent[0]


def test_discord_stop_alert_names_the_paper_side(monkeypatch):
    import signals.discord_alerts as da
    embeds = []
    monkeypatch.setattr(da, "_portfolio_context",
                        lambda: {"bankroll": 1000.0, "record": "0W/0L"})
    monkeypatch.setattr(da, "_send", lambda e, **kw: embeds.append(e) or True)
    se._send_discord_alert(dict(PAPER_RESULT, outcome="Yes"))
    side = [f for f in embeds[0][0]["fields"] if f["name"] == "Side"][0]
    assert side["value"] == "**NO**"
