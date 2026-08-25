#!/usr/bin/env bash
# Deploy files to the Polyclawd VPS and atomically restart + verify all services.
#
# The failure class this kills: partial deploys where a cron path picks up new
# code but a long-running service (scheduler/api/hf) keeps the old module in
# memory (2026-06-18 insider_detector incident; ~8 manual restarts in the
# 2026-07 alert-fleet work). One command, all-or-nothing.
#
# Usage:
#   ./deploy.sh <file> [file...]          # paths relative to repo root
#   ./deploy.sh --no-restart <file...>    # static/dashboard-only changes
#
# Exit non-zero if any file fails to copy or any service fails to reach
# `active` within 10s of restart.
set -euo pipefail

VPS_HOST="vps"
VPS_ROOT="/var/www/virtuosocrypto.com/polyclawd"
RESTART=1
if [[ "${1:-}" == "--no-restart" ]]; then RESTART=0; shift; fi
[[ $# -ge 1 ]] || { echo "usage: ./deploy.sh [--no-restart] <file> [file...]"; exit 2; }

cd "$(dirname "$0")"

for f in "$@"; do
  [[ -f "$f" ]] || { echo "FAIL: $f not found in repo"; exit 1; }
  python3 -m py_compile "$f" 2>/dev/null || { [[ "$f" == *.py ]] && { echo "FAIL: $f does not compile"; exit 1; } || true; }
  scp -q "$f" "${VPS_HOST}:${VPS_ROOT}/$f"
  echo "deployed $f"
done

if [[ $RESTART -eq 1 ]]; then
  ssh "$VPS_HOST" "bash ${VPS_ROOT}/scripts/vps_restart_verify.sh"
else
  echo "no-restart mode: files copied only (nginx-served statics publish instantly)"
fi
