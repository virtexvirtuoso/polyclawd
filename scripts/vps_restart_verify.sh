#!/usr/bin/env bash
# Runs ON THE VPS: atomically restart all long-running Polyclawd services and
# verify each reaches `active` within 10s, then tail journals 5s for tracebacks.
# Invoked by deploy.sh; safe to run standalone after manual edits.
set -uo pipefail

SERVICES=(polyclawd-api polyclawd-hf polyclawd-scheduler)

sudo systemctl restart "${SERVICES[@]}"
START_TS=$(date -u '+%Y-%m-%d %H:%M:%S')

fail=0
for s in "${SERVICES[@]}"; do
  ok=""
  for _ in $(seq 1 10); do
    if [[ "$(systemctl is-active "$s")" == "active" ]]; then ok=1; break; fi
    sleep 1
  done
  if [[ -n "$ok" ]]; then
    echo "OK      $s active"
  else
    echo "FAIL    $s did not reach active within 10s"
    fail=1
  fi
done

sleep 5
for s in "${SERVICES[@]}"; do
  tb=$(sudo journalctl -u "$s" --since "$START_TS" --no-pager 2>/dev/null | grep -c 'Traceback (most recent call last)')
  if [[ "$tb" -gt 0 ]]; then
    echo "WARN    $s logged $tb traceback(s) in first seconds after start:"
    sudo journalctl -u "$s" --since "$START_TS" --no-pager | grep -A3 'Traceback' | head -8
    fail=1
  else
    echo "CLEAN   $s journal (first 5s)"
  fi
done

exit $fail
