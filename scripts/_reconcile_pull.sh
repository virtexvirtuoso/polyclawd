#!/usr/bin/env bash
# Pull polyproxy layer VPS -> local: 51 safe files + config/polymarket_urls.py + tier1_whale_alert.py
set -euo pipefail
cd ~/Desktop/polyclawd
BK=/tmp/polyclawd-backup-2026-08-24
TAR=/tmp/polyproxy-pull.tar

# Build file list (safe overlap + config + vps-only)
cat /tmp/safe_overlap.txt > /tmp/pull_list.txt
echo "config/polymarket_urls.py" >> /tmp/pull_list.txt
echo "signals/tier1_whale_alert.py" >> /tmp/pull_list.txt
echo "pulling $(wc -l < /tmp/pull_list.txt) files"

# Create tar ON VPS from the list (tar -T reads list; paths relative to VPS_DIR)
# Pass list via ssh stdin using tar --files-from=-
ssh vps 'cd /var/www/virtuosocrypto.com/polyclawd && tar cf - --files-from=- 2>/dev/null' < /tmp/pull_list.txt > "$TAR"
echo "tar size: $(stat -f%z "$TAR" 2>/dev/null || stat -c%s "$TAR")"

# Extract locally (overwrite canonical)
tar xf "$TAR" -C ~/Desktop/polyclawd
echo "extracted. verifying a few:"
head -3 api/deps.py
echo "---"
grep -c 'polymarket_urls' config/polymarket_urls.py 2>/dev/null && echo "config OK" || echo "config MISSING"
