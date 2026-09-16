#!/usr/bin/env python3
"""
polyclawd-odds-credit-check.py — daily Odds API credit heartbeat + threshold alert.

Deployed to /home/linuxuser/bin/ on the VPS, run by polyclawd-odds-credit.timer
at 13:00 UTC (09:00 ET). Kept in-repo so it is reviewable and versioned.

Reads the LIVE x-requests headers from The Odds API's FREE /v4/sports endpoint
(costs 0 credits — verified 2026-06-25) for the production key, appends a history
line, and sends a Telegram message every day (heartbeat) with an escalated ⚠️
alert when remaining drops below a floor.

Why live header, not the cache JSON: the self-reported odds_api_credit.json once
masked which key prod actually uses (a sibling project's near-dead key looked like
prod). Always meter the real key. See memory:
  feedback-verify-api-utilization-and-key-before-credit-audit

2026-08-29 — failure handling rewritten. Two real incidents drove this:
  * The prod key started returning 401 DEACTIVATED_KEY ("cancelation or a failed
    payment"). Every polyclawd task retried it for ~a day. Nothing alerted.
  * This very script died on an uncaught `HTTPError: 502 Bad Gateway` from a
    transient upstream blip, so the day's heartbeat was simply missing — an
    absent message reads exactly like a healthy silent day.
Now: transient errors are retried and reported softly; auth errors trip the
shared rate_limiter breaker (halting every odds fetch fleet-wide) and page hard;
every outcome, success or failure, appends a history record so gaps are visible.

Env (supplied by the systemd unit's EnvironmentFile):
  ODDS_API_KEY        required — prod key (expect prefix 51ef, ~5M plan)
  TELEGRAM_BOT_TOKEN  required — reuse Polyclawd's bot
  TELEGRAM_CHAT_ID    required — target chat
  ODDS_CREDIT_FLOOR_PCT   optional, default 10  (alert if remaining/total < this %)
  ODDS_CREDIT_FLOOR_ABS   optional, default 50000 (alert if remaining < this)
  POLYCLAWD_ROOT          optional, default /var/www/virtuosocrypto.com/polyclawd
                          (on sys.path so odds.rate_limiter's breaker is shared
                          with the running services)
"""
import os
import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import traceback
from datetime import datetime, timezone

API = "https://api.the-odds-api.com/v4/sports"
HIST_LOG = "/var/log/polyclawd-odds-credit.log"
JOB = "polyclawd-odds-credit"

BOT = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
KEY = os.environ.get("ODDS_API_KEY", "")
FLOOR_PCT = float(os.environ.get("ODDS_CREDIT_FLOOR_PCT", "10"))
FLOOR_ABS = int(os.environ.get("ODDS_CREDIT_FLOOR_ABS", "50000"))
ROOT = os.environ.get("POLYCLAWD_ROOT", "/var/www/virtuosocrypto.com/polyclawd")

# Retry budget for TRANSIENT failures only (5xx, timeouts, DNS). Auth failures
# are never retried — a cancelled subscription does not recover in 20 seconds.
ATTEMPTS = 3
BACKOFF_S = (5, 20)
# A heartbeat older than this means the daily check has been failing silently.
STALE_HOURS = 36


def telegram(msg: str) -> None:
    """Best-effort Telegram send; never raises (alerter must not fail silently-hard)."""
    if not (BOT and CHAT):
        print(f"[warn] TELEGRAM creds missing; would send: {msg}", file=sys.stderr)
        return
    try:
        data = urllib.parse.urlencode({
            "chat_id": CHAT, "text": msg, "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except Exception as e:  # log, don't crash the job over a notify failure
        print(f"[warn] telegram send failed: {e}", file=sys.stderr)


def _rate_limiter():
    """Import the SHARED breaker from the deployed tree, so tripping it here
    actually halts the running services (they read the same state file).
    Returns None when the tree is unavailable — this script must still report."""
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from odds import rate_limiter

        return rate_limiter
    except Exception as e:
        print(f"[warn] rate_limiter unavailable ({e}) — breaker not wired", file=sys.stderr)
        return None


def append_history(rec: dict) -> None:
    """Append one record. Failures get a record too: a missing line is
    indistinguishable from a quiet day, which is how the 2026-08-29 outage hid."""
    try:
        with open(HIST_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print(f"[warn] could not append history: {e}", file=sys.stderr)


def last_success(now: datetime):
    """(age_hours, record) of the most recent OK history line, or (None, None).

    Records written before 2026-08-29 have no "ok" key; those are all successes
    (the old script only ever wrote on the success path), so absent == True.
    """
    try:
        with open(HIST_LOG) as f:
            lines = f.readlines()[-400:]
    except Exception:
        return None, None
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if not rec.get("ok", True):
            continue
        try:
            ts = datetime.fromisoformat(rec["ts"])
        except Exception:
            continue
        return (now - ts).total_seconds() / 3600.0, rec
    return None, None


def days_to_month_reset(now: datetime) -> int:
    """The Odds API quota resets on the 1st of each month (UTC)."""
    if now.month == 12:
        nxt = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        nxt = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return max(1, (nxt - now).days)


def fetch_credit():
    """Read live credit headers.

    Returns (ok, data, failure):
      ok=True  -> data = {"remaining","used","last"}
      ok=False -> failure = {"kind": "auth"|"transient", "status", "error_code",
                             "detail"}; "auth" is terminal, "transient" was
                             already retried ATTEMPTS times.
    """
    url = f"{API}?{urllib.parse.urlencode({'apiKey': KEY})}"
    last_failure = None

    for attempt in range(ATTEMPTS):
        req = urllib.request.Request(url, headers={"User-Agent": "polyclawd-credit-check/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return True, {
                    "remaining": int(resp.headers.get("x-requests-remaining", -1)),
                    "used": int(resp.headers.get("x-requests-used", -1)),
                    "last": resp.headers.get("x-requests-last", "?"),
                }, None
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read()[:400].decode("utf-8", "replace")
            except Exception:
                pass
            if e.code in (401, 403):  # terminal — do not retry, do not sleep
                error_code = ""
                try:
                    error_code = str(json.loads(body).get("error_code", ""))
                except Exception:
                    pass
                return False, None, {"kind": "auth", "status": e.code,
                                     "error_code": error_code, "detail": body[:300]}
            last_failure = {"kind": "transient", "status": e.code,
                            "error_code": "", "detail": f"HTTP {e.code}: {body[:200]}"}
        except Exception as e:
            last_failure = {"kind": "transient", "status": None,
                            "error_code": "", "detail": f"{type(e).__name__}: {e}"}

        if attempt < ATTEMPTS - 1:
            time.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])

    return False, None, last_failure


def report_auth_failure(now: datetime, failure: dict) -> int:
    """Terminal key failure: trip the fleet-wide breaker, then page."""
    status = failure.get("status")
    error_code = failure.get("error_code") or ""
    append_history({"ts": now.isoformat(), "ok": False, "kind": "auth",
                    "status": status, "error_code": error_code,
                    "detail": failure.get("detail", "")[:200], "key_prefix": KEY[:4]})

    tripped_now = False
    rl = _rate_limiter()
    if rl is not None:
        try:
            tripped_now = rl.note_auth_failure(status, failure.get("detail", ""), key_prefix=KEY[:4])
        except Exception as e:
            print(f"[warn] could not trip breaker: {e}", file=sys.stderr)

    meaning = {
        "DEACTIVATED_KEY": "Key deactivated — cancellation or a <b>failed payment</b>. "
                           "Check the card on file at the-odds-api.com.",
        "MISSING_KEY": "No key was sent — ODDS_API_KEY is empty in the unit's EnvironmentFile.",
        "INVALID_KEY": "Key rejected as invalid — it may have been rotated.",
    }.get(error_code, "Key rejected by the API.")

    age_h, rec = last_success(now)
    since = f"\nLast good reading: {age_h:.0f}h ago ({rec['remaining']:,} credits left)." if rec else ""
    breaker = ("\n\n🛑 Auth breaker <b>tripped now</b> — all odds fetches halted."
               if tripped_now else
               "\n\n🛑 Auth breaker already tripped — odds fetches remain halted.")

    telegram(
        f"❌ <b>{JOB}: KEY DEAD</b> · {KEY[:4]}…\n"
        f"HTTP {status} {error_code}\n{meaning}{since}{breaker}\n"
        f"Every odds-derived signal is down until this is fixed. "
        f"Recovery is automatic once the key answers."
    )
    print(f"[error] auth failure {status} {error_code}", file=sys.stderr)
    return 4


def report_transient_failure(now: datetime, failure: dict) -> int:
    """Upstream blip: report softly, never trip the breaker, never crash."""
    detail = (failure or {}).get("detail", "unknown error")
    append_history({"ts": now.isoformat(), "ok": False, "kind": "transient",
                    "status": (failure or {}).get("status"),
                    "detail": detail[:200], "key_prefix": KEY[:4]})

    age_h, rec = last_success(now)
    stale = ""
    if age_h is not None and age_h > STALE_HOURS:
        stale = (f"\n\n‼️ No successful reading in <b>{age_h:.0f}h</b> — this is no longer "
                 f"a blip. Check the key and the upstream API.")
    elif rec:
        stale = f"\nLast good reading {age_h:.0f}h ago: {rec['remaining']:,} credits left."

    telegram(
        f"⚠️ <b>{JOB}: check failed</b> (transient)\n"
        f"{ATTEMPTS} attempts over ~{sum(BACKOFF_S)}s all failed.\n"
        f"<pre>{detail[:200]}</pre>"
        f"Key was NOT declared dead and the breaker was NOT tripped — "
        f"odds fetches continue normally.{stale}"
    )
    print(f"[error] transient failure: {detail}", file=sys.stderr)
    return 5


def report_success(now: datetime, data: dict) -> int:
    remaining, used, last = data["remaining"], data["used"], data["last"]
    if remaining < 0 or used < 0:
        append_history({"ts": now.isoformat(), "ok": False, "kind": "no_headers",
                        "detail": f"remaining={remaining} used={used}", "key_prefix": KEY[:4]})
        telegram(f"❌ {JOB}: API 200 but no x-requests headers "
                 f"(remaining={remaining}, used={used}).")
        return 3

    # A good read means the key works — clear any tripped breaker and re-open the fleet.
    recovered = False
    rl = _rate_limiter()
    if rl is not None:
        try:
            recovered = bool(rl.read_breaker().get("tripped"))
            rl.note_auth_success()
        except Exception as e:
            print(f"[warn] breaker clear failed: {e}", file=sys.stderr)

    total = remaining + used
    pct_used = (used / total * 100) if total else 0.0
    pct_remaining = 100 - pct_used
    dtr = days_to_month_reset(now)
    avg_daily = (used / now.day) if now.day else used
    projected_eom = used + avg_daily * dtr

    append_history({
        "ts": now.isoformat(), "ok": True, "remaining": remaining, "used": used,
        "total": total, "pct_used": round(pct_used, 4),
        "avg_daily": round(avg_daily, 1), "projected_eom": round(projected_eom),
        "key_prefix": KEY[:4], "last_call_cost": last,
    })

    breach = (pct_remaining < FLOOR_PCT) or (remaining < FLOOR_ABS)
    icon = "⚠️" if breach else "🟢"
    lines = [f"{icon} <b>Odds API credit</b> · key {KEY[:4]}…"]
    if total:
        lines += [
            f"remaining: <b>{remaining:,}</b> / {total:,} ({pct_remaining:.2f}% left)",
            f"used this month: {used:,}  ·  avg {avg_daily:,.0f}/day",
            f"resets in {dtr}d  ·  projected EOM use ~{projected_eom:,.0f} "
            f"({projected_eom / total * 100:.1f}% of plan)",
        ]
    if breach:
        lines += ["", f"‼️ <b>Below floor</b> (&lt;{FLOOR_PCT:.0f}% or &lt;{FLOOR_ABS:,}). "
                      f"Investigate burn or top up the plan."]
    if recovered:
        lines += ["", "✅ Auth breaker cleared — the key is answering again, odds fetches resumed."]

    age_h, _ = last_success(now)
    telegram("\n".join(lines))
    print(f"[ok] remaining={remaining} used={used} pct_left={pct_remaining:.2f} "
          f"breach={breach} recovered={recovered}")
    return 0


def main() -> int:
    now = datetime.now(timezone.utc)
    if not KEY:
        append_history({"ts": now.isoformat(), "ok": False, "kind": "config",
                        "detail": "ODDS_API_KEY not set"})
        telegram(f"❌ {JOB}: ODDS_API_KEY not set in environment — cannot check credit.")
        print("[error] ODDS_API_KEY missing", file=sys.stderr)
        return 2

    ok, data, failure = fetch_credit()
    if ok:
        return report_success(now, data)
    if (failure or {}).get("kind") == "auth":
        return report_auth_failure(now, failure)
    return report_transient_failure(now, failure)


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:
        tb = traceback.format_exc()
        telegram(f"❌ {JOB} CRASH:\n<pre>{tb[-600:]}</pre>")
        print(tb, file=sys.stderr)
        rc = 1
    sys.exit(rc)
