"""Tests for scripts/alert_formatter.py send path (Task 1.6).

The formatter's Telegram delivery must go through ONE hardened send path
(ledger + err detail + transient-only retry) and must never 400-drop an
alert because a caller interpolated raw `<`/`>`/`&` into HTML mode.
"""

import io
import json
import subprocess
import urllib.error
import urllib.parse

import scripts.alert_formatter as af


class _Resp:
    def read(self):
        return b'{"ok": true}'

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch, tmp_path, urlopen):
    """Route delivery through the real HTTP fallback with a fake Telegram."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("SMART_WALLET_ALERT_SEND", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("POLYCLAWD_LEDGER_PATH", str(ledger))

    def no_cli(*a, **k):
        raise FileNotFoundError("openclaw not installed")

    monkeypatch.setattr(subprocess, "run", no_cli)

    import scripts.openclaw_alerts as oa

    monkeypatch.setattr(oa.time, "sleep", lambda s: None)
    monkeypatch.setattr(oa.urllib.request, "urlopen", urlopen)
    return ledger


def _fake_telegram(calls):
    """Rejects HTML payloads containing a raw '< ' (real Telegram behavior:
    can't parse entities); accepts everything else."""

    def urlopen(req, timeout):
        payload = urllib.parse.parse_qs(req.data.decode())
        calls.append(payload)
        text = payload["text"][0]
        pm = payload.get("parse_mode", [None])[0]
        if pm == "HTML" and "< 2" in text:
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"ok":false,"description":"can\'t parse entities"}')
            )
        return _Resp()

    return urlopen


def test_raw_angle_bracket_message_still_delivers(monkeypatch, tmp_path):
    calls = []
    ledger = _wire(monkeypatch, tmp_path, _fake_telegram(calls))
    ok = af.send_telegram("⚡ HF spread alert\n< 2 min left")
    assert ok is True
    # last attempt was accepted, in plain-text mode, content preserved
    assert "2 min left" in calls[-1]["text"][0]
    assert calls[-1].get("parse_mode", [None])[0] is None
    lines = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert lines[-1]["ok"] is True


def test_permanent_400_lands_err_in_ledger_no_retry(monkeypatch, tmp_path):
    calls = []

    def always_400(req, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"ok":false,"description":"chat not found"}')
        )

    ledger = _wire(monkeypatch, tmp_path, always_400)
    ok = af.send_telegram("hello <b>world</b>")
    assert ok is False
    # 400 is permanent: one HTML attempt + one plain-text fallback, NO retries
    assert len(calls) == 2
    lines = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert lines, "failure must land in the ledger, never a silent swallow"
    assert all(l["ok"] is False and l["err"].startswith("http_400") for l in lines)


def test_format_alert_escapes_dynamic_fields(monkeypatch, tmp_path):
    """Raw </>/& in caller data must be escaped so the HTML send succeeds
    FIRST try with formatting intact — not rescued plain by the send-path
    fallback (step-10 polish)."""
    calls = []
    _wire(monkeypatch, tmp_path, _fake_telegram(calls))
    msg = af.format_alert(
        alert_type="whale",
        rank=2,
        emoji="🐋",
        title="High temp < 2°C in NYC?",
        direction="YES",
        price_cents=41,
        action="Bought $5K < 2 min after open",
        data_line="Depth < 2x & thin",
        links=["<a href='https://x'>Polymarket</a>"],
        tags=["🔴 <soon>"],
    )
    # dynamic text escaped; template markup and links untouched
    assert "&lt; 2°C" in msg and "&lt; 2 min" in msg and "&lt; 2x &amp; thin" in msg
    assert "&lt;soon&gt;" in msg
    assert "<b>#2</b>" in msg and "<a href=" in msg
    ok = af.send_telegram(msg)
    assert ok is True
    assert len(calls) == 1  # clean escaped HTML passes on the first try
    assert calls[0]["parse_mode"][0] == "HTML"


def test_intentional_html_tags_still_render(monkeypatch, tmp_path):
    calls = []
    _wire(monkeypatch, tmp_path, _fake_telegram(calls))
    msg = af.format_alert(alert_type="arb", rank=1, emoji="\U0001f4ca", title="Spread")
    assert "<b>" in msg
    ok = af.send_telegram(msg)
    assert ok is True
    assert len(calls) == 1  # clean HTML goes through first try
    assert calls[0]["parse_mode"][0] == "HTML"
    assert "<b>#1</b>" in calls[0]["text"][0]
