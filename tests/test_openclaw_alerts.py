"""Tests for scripts/openclaw_alerts.py — send-layer hardening (Phase 1)."""
import json
import urllib.error

import scripts.openclaw_alerts as oa


def test_http_send_returns_err_detail_on_400(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("POLYCLAWD_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))

    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)

    monkeypatch.setattr(oa.urllib.request, "urlopen", boom)
    ok, err = oa._telegram_http_send("hi", parse_mode="Markdown")
    assert ok is False and "400" in err


def test_no_token_records_err(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("POLYCLAWD_LEDGER_PATH", str(ledger))
    ok, err = oa._telegram_http_send("hi")
    assert ok is False and err == "no_token"
