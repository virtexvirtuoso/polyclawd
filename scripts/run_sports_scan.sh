#!/usr/bin/env bash
# Cron wrapper for the soccer/UFC/World-Cup edge scanner.
#
# Sources the app env (so ODDS_API_KEY is available — cron does NOT inherit the
# systemd manager environment), then runs the scanner from the app root.
#
# Install on the VPS (crontab -e) — staggered cadence, credit-conscious:
#   0 */6 * * *  /var/www/virtuosocrypto.com/polyclawd/scripts/run_sports_scan.sh soccer_match   >> /var/log/polyclawd-sports-scan.log 2>&1
#   0 7   * * *  /var/www/virtuosocrypto.com/polyclawd/scripts/run_sports_scan.sh soccer_futures  >> /var/log/polyclawd-sports-scan.log 2>&1
#   0 8   * * *  /var/www/virtuosocrypto.com/polyclawd/scripts/run_sports_scan.sh ufc             >> /var/log/polyclawd-sports-scan.log 2>&1
#   0 */3 * * 6,0 /var/www/virtuosocrypto.com/polyclawd/scripts/run_sports_scan.sh ufc            >> /var/log/polyclawd-sports-scan.log 2>&1   # fight nights (Sat/Sun)
#
# PREREQ: ODDS_API_KEY must be readable by cron. It currently lives only in the
# systemd manager env. Add it to config/polymarket.env (NOT committed) on the VPS:
#   echo "ODDS_API_KEY=<key>" >> config/polymarket.env
set -euo pipefail
APP_DIR="${POLYCLAWD_DIR:-/var/www/virtuosocrypto.com/polyclawd}"
cd "$APP_DIR"
if [ -f config/polymarket.env ]; then set -a; . config/polymarket.env; set +a; fi
exec venv/bin/python3 scripts/sports_edge_scan.py "$@"
