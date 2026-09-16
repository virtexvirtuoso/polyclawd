# Git hooks — repo sync by construction

One clone of this repo is the **writer** (the VPS, where the code runs). GitHub is a
mirror it pushes to automatically. Every other clone is a **replica** that fast-forwards
itself when a Claude Code session opens and refuses to commit on `main`.

Activated per clone by `session-start.sh` (self-installs on first session), or by hand:

```bash
git config core.hooksPath scripts/git-hooks
git config virtuoso.role writer      # ONLY on the host that runs the code; default = replica
git config virtuoso.notify ~/bin/infra-notify   # optional: host-local Telegram sender
```

| Hook | Runs on | Does |
|---|---|---|
| `post-commit` | writer | `git push origin <branch>` in the background; logs to `.git/hooks-push.log`; notifies on failure |
| `pre-commit` | all | replica: refuses commits on `main` (feature branch → PR is the only path). all: refuses `*.bak`, `*.pre-*`, `*_old*` files — git is the backup |
| `session-start.sh` | all (Claude Code `SessionStart`) | bootstraps git config, `fetch`, fast-forwards a clean tree, prints `git status -sb` so every session opens knowing where it stands |

No host paths or secrets live here — anything host-specific is read from `git config`.
Contract: vault `08-AI/Repo-Sync-Contract.md`.
