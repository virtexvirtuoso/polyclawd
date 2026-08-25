#!/usr/bin/env bash
# Backup current canonical working state before polyproxy reconcile
set -e
cd ~/Desktop/polyclawd
BK=/tmp/polyclawd-backup-2026-08-24
mkdir -p "$BK/dirty"
git diff > "$BK/local-dirty.diff"
echo "backed up $(wc -l < "$BK/local-dirty.diff") diff lines"

# Snapshot all dirty files by content
while IFS= read -r f; do
    [ -z "$f" ] && continue
    mkdir -p "$BK/dirty/$(dirname "$f")"
    cp "$f" "$BK/dirty/$f" 2>/dev/null || echo "  (skip missing: $f)"
done < /tmp/local_dirty.txt
echo "snapshot done. count:"
find "$BK/dirty" -name '*.py' | wc -l
