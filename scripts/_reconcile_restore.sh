#!/usr/bin/env bash
# Restore the 11 risk files to their original local (pre-merge) state from backup,
# then run the 3-way merge for all of them.
set -uo pipefail
cd ~/Desktop/polyclawd
BK=/tmp/polyclawd-backup-2026-08-24

echo "=== Restoring risk files from backup ==="
while IFS= read -r f; do
    [ -z "$f" ] && continue
    if [ -f "$BK/dirty/$f" ]; then
        cp "$BK/dirty/$f" "$f"
        echo "  restored: $f"
    else
        echo "  (no backup for $f - skipping restore)"
    fi
done < /tmp/risk.txt
echo "restore done"
