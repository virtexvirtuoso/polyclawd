from log_redact import redact, safe_url


def test_safe_url_drops_the_query_string():
    u = "https://api.weather.com/v3/wx/forecast/daily/5day?icaoCode=KATL&units=e&apiKey=e1f10a1e78da46f5"
    assert safe_url(u) == "https://api.weather.com/v3/wx/forecast/daily/5day"


def test_redact_masks_every_key_style():
    s = "GET /x?apiKey=AAA&key=BBB&apikey=CCC&auth_token=DDD&token=EEE&access_token=FFF ok"
    out = redact(s)
    for secret in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"):
        assert secret not in out
    assert out.count("REDACTED") == 6


def test_redact_leaves_ordinary_text_alone():
    assert redact("keyboard monkey=1 hotkey") == "keyboard monkey=1 hotkey"


def test_safe_url_on_garbage_falls_back_to_redaction():
    assert "SECRET" not in safe_url("not a url ?apiKey=SECRET")
