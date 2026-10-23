"""log_redact: strip credentials from URLs and messages before they reach a log (2026-10-23).

The scheduler journal carried weather.com, Visual Crossing and Tomorrow.io keys from
signals/weather_ensemble._fetch_json and an Odds API key prefix from odds/monitor_gate.
"""
import re
from urllib.parse import urlsplit, urlunsplit

_KEY_RE = re.compile(r"(?i)\b(api[_-]?key|apikey|key|token|auth_token|access_token|secret)=[^&\s'\"]+")


def redact(text) -> str:
    return _KEY_RE.sub(lambda mm: f"{mm.group(1)}=REDACTED", str(text))


def safe_url(url) -> str:
    """scheme://host/path only: the query string carries keys and is never needed to debug a failure."""
    try:
        p = urlsplit(str(url))
        if not p.scheme or not p.netloc:
            return redact(url)
        return urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    except Exception:
        return redact(url)
