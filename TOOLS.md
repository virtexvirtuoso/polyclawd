# Polyclawd — Operational Tools

## deploy.sh — atomic deploy + verify (added 2026-07-10)

Deploys files from this repo (the Mac canonical working tree) to the VPS and
restarts **all three** long-running services atomically, verifying each reaches
`active` within 10s and logged no tracebacks in its first 5s.

```bash
./deploy.sh scripts/foo.py signals/bar.py   # copy + restart + verify
./deploy.sh --no-restart static/index.html  # statics publish instantly via nginx
```

Why: partial deploys leave old code in service memory while cron paths pick up
the new file (2026-06-18 insider_detector incident). Never restart services by
hand after a code deploy — use this.

VPS-side half: `scripts/vps_restart_verify.sh` (safe to run standalone on the
VPS after in-place edits there — then pull the file back to this repo).

## Send ledger + watchdog (added 2026-07-10)

Every Telegram delivery attempt through `scripts/openclaw_alerts.py::alert_openclaw`
or `scripts/alert_formatter.py::send_telegram` appends one JSON line to
`logs/telegram_sent.jsonl` (`{ts, caller, channel, ok, parse_mode, len[, err]}`).

`scripts/send_ledger_watchdog.py` runs daily (cron 13:30 UTC), silent when
clean, alerts (plain text) when any delivery failed in the last 24h.

```bash
venv/bin/python3 scripts/send_ledger_watchdog.py --dry --hours 48   # inspect
```

Audit one-liner — deliveries per stream this week:

```bash
jq -r 'select(.ok) | .caller' logs/telegram_sent.jsonl | sort | uniq -c | sort -rn
```
