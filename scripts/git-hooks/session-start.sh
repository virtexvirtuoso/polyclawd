#!/usr/bin/env bash
# session-start.sh — Claude Code SessionStart hook (wired in .claude/settings.json).
# Every session on any clone opens KNOWING where it stands, without being told to check:
#   1. bootstrap the clone (hooks path, ff-only pulls, prune) — idempotent, first run only
#   2. fetch, and fast-forward a CLEAN tree to its upstream
#   3. print one status line: branch, labelled ahead/behind, dirty count, role
# Never merges, never touches a dirty tree, never blocks a session on network failure.
set -u
cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}" || exit 0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# --- 1. bootstrap (only writes keys that are unset) ---------------------------
setdef() { git config --get "$1" >/dev/null 2>&1 || git config "$1" "$2"; }
setdef core.hooksPath scripts/git-hooks
setdef pull.ff only
setdef fetch.prune true
setdef push.autoSetupRemote true
chmod +x scripts/git-hooks/post-commit scripts/git-hooks/pre-commit 2>/dev/null

role=$(git config --get virtuoso.role || echo replica)
branch=$(git symbolic-ref --short -q HEAD || echo detached)

# --- 2. fetch + fast-forward -------------------------------------------------
fetch_note=""
if ! GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=10" git fetch -q --prune origin 2>/dev/null; then
    fetch_note=" (fetch FAILED — offline? status below is against the last fetch)"
fi

dirty=$(git status --porcelain --untracked-files=no | wc -l | tr -d ' ')
# upstream configured but GONE counts as none — `rev-parse @{u}` would echo the literal
# '@{u}' on stdout while failing, which then poisons every count below.
upstream=$(git for-each-ref --format='%(upstream:short)' "refs/heads/$branch" 2>/dev/null | head -1)
{ [ -n "$upstream" ] && git rev-parse -q --verify "$upstream^{commit}" >/dev/null 2>&1; } || upstream=""
ff_note=""
if [ -n "$upstream" ]; then
    behind=$(git rev-list --count "HEAD..$upstream")
    ahead=$(git rev-list --count "$upstream..HEAD")
    if [ "$dirty" = 0 ] && [ "$behind" -gt 0 ] && [ "$ahead" = 0 ]; then
        if git merge -q --ff-only "$upstream" 2>/dev/null; then
            ff_note=" — fast-forwarded $behind commit(s)"; behind=0
        fi
    fi
else
    behind="?"; ahead="?"
fi

# --- 3. one line the model can act on ---------------------------------------
repo=$(basename "$(git rev-parse --show-toplevel)")
printf 'git[%s]: %s' "$repo" "$branch"
[ -n "$upstream" ] && printf ' → %s | behind %s | ahead %s' "$upstream" "$behind" "$ahead" \
                   || printf ' | NO UPSTREAM'
printf ' | dirty tracked %s | role %s%s%s\n' "$dirty" "$role" "$ff_note" "$fetch_note"

if [ "$role" != writer ] && { [ "$branch" = main ] || [ "$branch" = master ]; }; then
    echo "  replica on $branch: read-only here. To change code: git checkout -b feat/<slug> → PR."
fi
if [ "$upstream" ] && [ "$behind" != 0 ] && [ "$behind" != "?" ]; then
    echo "  BEHIND upstream and not fast-forwarded (dirty tree or diverged) — reconcile BEFORE editing."
fi
exit 0
