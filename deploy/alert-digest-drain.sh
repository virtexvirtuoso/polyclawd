#!/bin/bash
# Alert-router Tier-3 digest drain (Gate 2, 2026-08-19).
# Wrapper exists because the previous inline `python3 -c "... % ..."` crontab
# line was silently truncated at the first % (crontab treats % as newline).
set -a; . /home/linuxuser/.config/polyclawd/alerts.env; set +a
cd /var/www/virtuosocrypto.com/polyclawd || { echo "$(date -u +%FT%TZ) ERROR: cd failed"; exit 1; }
echo -n "$(date -u +%FT%TZ) "
exec venv/bin/python3 -c "from signals.alert_dispatch import drain_digest; print(f'sent_batches={drain_digest()}', flush=True)"
