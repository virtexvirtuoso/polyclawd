"""Tests for scripts/openclaw_alerts.py — send-layer hardening (Phase 1)."""

import io
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


def test_retries_transient_not_400(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("POLYCLAWD_LEDGER_PATH", str(tmp_path / "l.jsonl"))
    monkeypatch.setattr(oa.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def flaky(req, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, None)

    monkeypatch.setattr(oa.urllib.request, "urlopen", flaky)
    oa._telegram_http_send("hi")
    assert calls["n"] == 2  # 1 try + 1 retry on 5xx (D2: durable retry is the queue's job)

    calls["n"] = 0

    def bad(req, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)

    monkeypatch.setattr(oa.urllib.request, "urlopen", bad)
    oa._telegram_http_send("hi")
    assert calls["n"] == 1  # 400 = permanent, no retry


def test_long_message_split_into_chunks(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYCLAWD_LEDGER_PATH", str(tmp_path / "l.jsonl"))
    sent = []

    def fake_inner(message, channel="telegram", silent=False, parse_mode="Markdown"):
        sent.append(message)
        return True, ""

    monkeypatch.setattr(oa, "_alert_openclaw_inner", fake_inner)
    message = "\n".join("x" * 90 for _ in range(99))  # 9008 chars incl newlines
    assert len(message) > 9000 - 100
    ok = oa.alert_openclaw(message)
    assert ok is True
    assert len(sent) == 3
    assert all(len(chunk) <= 4000 for chunk in sent)
    # nothing lost: recombined content equals the original lines
    assert "\n".join(sent).split("\n") == message.split("\n")


def test_default_parse_mode_sends_plain_text(monkeypatch, tmp_path):
    """Arbitrary market titles/wallets (_ and *) must not 400 under the default mode."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("POLYCLAWD_LEDGER_PATH", str(tmp_path / "l.jsonl"))
    captured = {}

    class Resp:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def capture(req, timeout):
        captured["payload"] = req.data.decode()
        return Resp()

    monkeypatch.setattr(oa.urllib.request, "urlopen", capture)
    ok, err = oa._telegram_http_send("_underscore_wallet* text")
    assert ok is True and err == ""
    assert "parse_mode" not in captured["payload"]


def test_alert_openclaw_default_parse_mode_none(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYCLAWD_LEDGER_PATH", str(tmp_path / "l.jsonl"))
    seen = {}

    def fake_inner(message, channel="telegram", silent=False, parse_mode="SENTINEL"):
        seen["parse_mode"] = parse_mode
        return True, ""

    monkeypatch.setattr(oa, "_alert_openclaw_inner", fake_inner)
    assert oa.alert_openclaw("hi") is True
    assert seen["parse_mode"] is None


def test_no_token_records_err(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("POLYCLAWD_LEDGER_PATH", str(ledger))
    ok, err = oa._telegram_http_send("hi")
    assert ok is False and err == "no_token"


def _entity_400(url):
    """HTTPError whose body is Telegram's can't-parse-entities 400."""
    body = (
        b'{"ok":false,"error_code":400,"description":'
        b'"Bad Request: can\'t parse entities: Unsupported start tag \\"85\\" at byte offset 30"}'
    )
    return urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(body))


def test_entity_parse_400_falls_back_to_plain(monkeypatch, tmp_path):
    """A deterministic can't-parse-entities 400 must deliver the message plain
    instead of dropping it (plan §6 step 10 — was ~5% of deliveries)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("POLYCLAWD_LEDGER_PATH", str(tmp_path / "l.jsonl"))
    payloads = []

    class Resp:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def tg(req, timeout):
        payloads.append(req.data.decode())
        if "parse_mode" in payloads[-1]:
            raise _entity_400(req.full_url)
        return Resp()

    monkeypatch.setattr(oa.urllib.request, "urlopen", tg)
    ok, err = oa._telegram_http_send("Temp <85° tonight", parse_mode="HTML")
    assert ok is True
    assert err.startswith("degraded:")  # delivered, but formatting bug stays visible in ledger
    assert len(payloads) == 2
    assert "parse_mode" in payloads[0] and "parse_mode" not in payloads[1]


def test_other_400_does_not_fall_back(monkeypatch, tmp_path):
    """Non-entity 400s (wrong chat, oversized, ...) stay permanent failures — no blind resend."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("POLYCLAWD_LEDGER_PATH", str(tmp_path / "l.jsonl"))
    calls = {"n": 0}

    def tg(req, timeout):
        calls["n"] += 1
        body = b'{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}'
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(body))

    monkeypatch.setattr(oa.urllib.request, "urlopen", tg)
    ok, err = oa._telegram_http_send("hi", parse_mode="HTML")
    assert ok is False and err.startswith("http_400")
    assert calls["n"] == 1


def test_entity_400_without_parse_mode_no_fallback(monkeypatch, tmp_path):
    """If the send was already plain, there is nothing to strip — fail as before."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("POLYCLAWD_LEDGER_PATH", str(tmp_path / "l.jsonl"))
    calls = {"n": 0}

    def tg(req, timeout):
        calls["n"] += 1
        raise _entity_400(req.full_url)

    monkeypatch.setattr(oa.urllib.request, "urlopen", tg)
    ok, err = oa._telegram_http_send("hi")
    assert ok is False and err.startswith("http_400")
    assert calls["n"] == 1
