#!/usr/bin/env bash
# 3-way merge for the 11 risk files: base=HEAD, ours=local(dirty), theirs=VPS(polyproxy)
set -uo pipefail
cd ~/Desktop/polyclawd
VPS_BASE=/var/www/virtuosocrypto.com/polyclawd
TMP=/tmp/reconcile-merge
mkdir -p "$TMP"

conflicts=0
while IFS= read -r f; do
    [ -z "$f" ] && continue
    echo "=== MERGE: $f"
    # ours = current local (has local edits), base = HEAD, theirs = VPS
    git show "HEAD:$f" > "$TMP/base" 2>/dev/null || { echo "  SKIP: no HEAD base for $f (untracked)"; continue; }
    cp "$f" "$TMP/ours"
    ssh vps "cat '$VPS_BASE/$f'" < /dev/null > "$TMP/theirs" 2>/dev/null || { echo "  SKIP: no VPS version"; continue; }

    if git merge-file "$TMP/ours" "$TMP/base" "$TMP/theirs" 2>/dev/null; then
        # clean merge -> take merged result
        cp "$TMP/ours" "$f"
        echo "  CLEAN merge -> applied"
    else
        conflicts=$((conflicts+1))
        echo "  CONFLICT -> $TMP/$f.conflicted (manual review needed)"
        cp "$TMP/ours" "$TMP/$f.conflicted"
    fi
done < /tmp/risk.txt

echo ""
echo "DONE. conflicts needing manual review: $conflicts"
ls -la "$TMP"/*.conflicted 2>/dev/null || echo "no conflicted files"
