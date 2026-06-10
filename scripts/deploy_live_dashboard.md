# Phase H Live Dashboard — Deploy Runbook

> **WARNING: Requires Mr. V go-ahead — touches production. MODE stays PAPER.**
>
> This is a READ-ONLY dashboard. No trading logic is modified. The execution/
> module is imported by the API route for DB access only.

---

## Context

This repo (`polyclawd-live-dev`, branch `feature/live-weather-execution`) is
**non-canonical** per `NONCANONICAL.md`. Canonical source lives at
`~/Desktop/polyclawd`, deployed via `~/bin/polyclawd-deploy`.

The VPS serves the Polyclawd dashboard via nginx under `/polyclawd/`. Static
HTML files are served directly by nginx from
`/var/www/virtuosocrypto.com/polyclawd/static/` — `scp` publishes them
instantly without any service restart. The backend API is
`polyclawd-api.service` (NOT `polyclawd.service` — that unit does not exist).

---

## Pre-flight checks

Before executing any step, verify:

```bash
# On Mac Mini — confirm feature branch and clean status
cd ~/Desktop/polyclawd
git status
git log --oneline -5

# On VPS — check service health
ssh vps "sudo systemctl status polyclawd-api.service"
ssh vps "curl -s localhost:8420/health"
```

---

## Step 1 — Port execution/ module to canonical repo

The `execution/` directory in this sandbox is the authoritative implementation
(live_db.py, live_config.py, live_position_tracker.py, live_executor.py,
risk_governor.py, clob_client.py, fee_model.py). Copy it wholesale:

```bash
# From Mac Mini
rsync -av --checksum \
  ~/Projects/polyclawd-live-dev/execution/ \
  ~/Desktop/polyclawd/execution/

# Verify no accidental deletions
diff -rq ~/Projects/polyclawd-live-dev/execution/ ~/Desktop/polyclawd/execution/
```

---

## Step 2 — Port api/routes/live.py

```bash
cp ~/Projects/polyclawd-live-dev/api/routes/live.py \
   ~/Desktop/polyclawd/api/routes/live.py
```

Then update `~/Desktop/polyclawd/api/routes/__init__.py` to export
`live_router` (match the sandbox version exactly):

```python
from .live import router as live_router
# add to __all__ list
```

And update `~/Desktop/polyclawd/api/main.py` to register the router:

```python
from api.routes import live_router
# ...
app.include_router(live_router, prefix="/api", tags=["Live"])
```

Diff canonical main.py against sandbox version to avoid clobbering any VPS-
specific additions:

```bash
diff ~/Projects/polyclawd-live-dev/api/main.py ~/Desktop/polyclawd/api/main.py
```

Apply only the live_router lines — do NOT replace the whole file.

---

## Step 3 — Port static/live.html (two locations)

Nginx serves static dashboards from two paths:
1. `/var/www/virtuosocrypto.com/polyclawd/static/` — for `/polyclawd/static/live.html`
2. `/var/www/virtuosocrypto.com/polyclawd/` — nginx root for direct `/polyclawd/live.html`

Copy to canonical static dir locally:

```bash
cp ~/Projects/polyclawd-live-dev/static/live.html \
   ~/Desktop/polyclawd/static/live.html
```

Also copy to the polyclawd ROOT (so `https://virtuosocrypto.com/polyclawd/live.html` works):

```bash
cp ~/Projects/polyclawd-live-dev/static/live.html \
   ~/Desktop/polyclawd/live.html
```

---

## Step 4 — Run polyclawd-deploy

This script syncs canonical `~/Desktop/polyclawd` to the VPS:

```bash
~/bin/polyclawd-deploy
```

Watch for errors. If it completes, the Python files and static assets are on
the VPS.

---

## Step 4.5 — Dependencies (READ CAREFULLY — do NOT blanket-upgrade)

The execution layer adds ONE genuinely-new runtime dependency: **`py-clob-client==0.34.6`** (+ its transitive deps: `web3`, `eth-account`, etc.). Install ONLY that into the VPS venv:

```bash
ssh vps "cd /var/www/virtuosocrypto.com/polyclawd && ./venv/bin/pip install py-clob-client==0.34.6"
# Verify it imports cleanly without disturbing the running app:
ssh vps "cd /var/www/virtuosocrypto.com/polyclawd && ./venv/bin/python -c 'import py_clob_client; print(py_clob_client.__version__)'"
```

> ⚠️ **Do NOT run `pip install -r requirements.txt --upgrade` on the VPS.** The local dev venv was bumped to `starlette>=0.40` ONLY so the FastAPI TestClient works with the local `httpx>=0.27`. The VPS already runs a self-consistent `fastapi 0.109 / starlette 0.35 / httpx` set, and the `/live` code uses only plain `APIRouter` (no version-specific APIs), so it runs fine on the VPS's existing versions. Force-upgrading fastapi/starlette on the running production app is an unnecessary risk. The `requirements.txt` DEPLOY NOTE documents this.

After installing py-clob-client, do a dry import of the new modules against the VPS interpreter BEFORE restarting the service:

```bash
ssh vps "cd /var/www/virtuosocrypto.com/polyclawd && ./venv/bin/python -c 'from api.routes.live import router; from execution import live_executor, risk_governor, live_db; print(\"imports OK\")'"
```

If that errors, STOP — fix before restarting (a bad import will crash `polyclawd-api` on restart).

---

## Step 5 — Restart polyclawd-api.service

The new API route requires a Python process restart:

```bash
ssh vps "sudo systemctl restart polyclawd-api.service"
# Verify it came up cleanly
ssh vps "sudo systemctl status polyclawd-api.service"
ssh vps "journalctl -u polyclawd-api.service -n 30 --no-pager"
```

---

## Step 6 — Publish static files (scp — no restart needed)

Static files are served by nginx directly; no service restart required after
the initial deploy above. If you need to update only the HTML afterward:

```bash
# SCP directly to both nginx paths
scp ~/Desktop/polyclawd/static/live.html \
    vps:/var/www/virtuosocrypto.com/polyclawd/static/live.html

scp ~/Desktop/polyclawd/static/live.html \
    vps:/var/www/virtuosocrypto.com/polyclawd/live.html
```

---

## Step 7 — Verify

```bash
# Backend API responds
ssh vps "curl -s localhost:8420/api/live/portfolio" | python3 -m json.tool | head -20

# Public HTTPS endpoint (200 response)
curl -s -o /dev/null -w "%{http_code}" \
  https://virtuosocrypto.com/polyclawd/live.html
# Expected: 200

# Page contains "Account" text (basic smoke test)
curl -s https://virtuosocrypto.com/polyclawd/live.html | grep -c "Account"
# Expected: at least 1
```

---

## Rollback

If the restart breaks the API service:

```bash
ssh vps "sudo systemctl restart polyclawd-api.service"
# If that fails, check logs:
ssh vps "journalctl -u polyclawd-api.service -n 50 --no-pager"
```

The dashboard HTML changes are static-only and require no rollback — simply
delete the file from nginx if needed:

```bash
ssh vps "sudo rm /var/www/virtuosocrypto.com/polyclawd/live.html"
ssh vps "sudo rm /var/www/virtuosocrypto.com/polyclawd/static/live.html"
```

---

## Notes

- `POLYCLAWD_MODE` defaults to `PAPER` — the dashboard will show the paper
  banner until explicitly set to `LIVE` in `config/polymarket.env`.
- The live_db module creates `storage/shadow_trades.db` tables on first
  connect (IF NOT EXISTS). No separate migration step needed.
- The `/api/live/*` endpoints are read-only. No trading logic is touched.
