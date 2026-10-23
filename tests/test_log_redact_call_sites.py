"""Call sites must never write a credential to a log or to source_health (2026-10-23)."""
import io
import logging
import urllib.error

import pytest
from loguru import logger

SENTINEL = "SENTINELKEY123456"
URL = f"https://api.weather.com/v3/wx/forecast/daily/5day?icaoCode=KATL&units=e&apiKey={SENTINEL}"


@pytest.fixture
def captured():
    buf = io.StringIO()
    sink = logger.add(buf, level="DEBUG", format="{message}")
    yield buf
    logger.remove(sink)


def _raise_http(*a, **k):
    raise urllib.error.HTTPError(URL, 401, "Unauthorized", {}, io.BytesIO(b'{"apiKey":"x"}'))


@pytest.mark.parametrize("modname", ["signals.weather_ensemble", "odds.weather_ensemble"])
def test_weather_fetch_json_failure_logs_no_key(monkeypatch, captured, modname):
    mod = __import__(modname, fromlist=["_fetch_json"])
    monkeypatch.setattr(mod.urllib.request, "urlopen", _raise_http)
    assert mod._fetch_json(URL) is None
    out = captured.getvalue()
    assert "fetch failed" in out and SENTINEL not in out


def test_monitor_gate_failure_prints_no_key(monkeypatch, capsys):
    import odds.monitor_gate as mg
    from odds import rate_limiter as rl
    # never touch the real credit gate / auth-breaker state files
    monkeypatch.setattr(rl, "can_make_call", lambda *a, **k: (True, ""))
    monkeypatch.setattr(rl, "note_auth_failure", lambda *a, **k: False)
    monkeypatch.setattr(mg, "_CACHE", {})
    monkeypatch.setattr(mg.urllib.request, "urlopen", _raise_http)
    url = f"https://api.the-odds-api.com/v4/sports/soccer_epl/odds/?apiKey={SENTINEL}&regions=us"
    mg.gated_fetch_json(url)
    out = capsys.readouterr().out
    assert "[monitor_gate] GET" in out and SENTINEL not in out


def test_source_health_never_stores_or_logs_a_key(monkeypatch, tmp_path, caplog):
    import api.services.source_health as sh
    monkeypatch.setattr(sh, "DB_PATH", tmp_path / "scratch_source_health.db")
    caplog.set_level(logging.DEBUG)
    sh.record_failure("twc_actuals", f"InvalidURL: {URL}")
    sh.record_failure("twc_actuals", f"InvalidURL again: {URL}")  # UPDATE branch too
    h = sh.get_source_health("twc_actuals")
    assert h is not None and h["total_failures"] == 2
    assert "REDACTED" in h["last_error_msg"]
    assert SENTINEL not in h["last_error_msg"]
    assert SENTINEL not in caplog.text
