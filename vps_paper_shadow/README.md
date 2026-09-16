# Theta-Inversion Paper-Shadow Harness

> Sidecar that logs three hypothesis confidences (H0 original / H1 drop-theta / H2 invert-theta) for every `mispriced_category` signal emission. Read-only on existing tables; writes only to new `theta_shadow_log`. **Trading behavior is unchanged.**

---

## What this does (and doesn't)

| | Yes | No |
|---|---|---|
| Read `signal_snapshots` | ✅ | |
| Read `signal_predictions` (for analysis only) | ✅ | |
| Write `theta_shadow_log` (new table) | ✅ | |
| Modify `source_weights` | | ❌ |
| Modify producer code | | ❌ |
| Modify `calibrator.py` | | ❌ |
| Place or alter paper trades | | ❌ |

**Rollback in one command:** `DROP TABLE theta_shadow_log;` — nothing else touched.

---

## Files

```
vps_paper_shadow/
├── sql/
│   └── 001_create_theta_shadow_log.sql        # idempotent migration
├── shadow_runner.py                            # cron entry point
├── analyze_shadow.py                           # post-window IC analysis
├── polyclawd-theta-shadow.service              # systemd one-shot unit
├── polyclawd-theta-shadow.timer                # systemd timer (every 5min)
├── tests/
│   └── test_shadow_runner.py                   # 11 tests, all green locally
└── README.md                                   # this file
```

---

## Local verification (already done)

```bash
cd ~/Desktop/polyclawd-calibration
source .venv/bin/activate
python -m pytest vps_paper_shadow/tests/ -v
# → 11 passed

# Smoke test against 500-row real-data fixture
python vps_paper_shadow/shadow_runner.py \
  --db fixtures/snaps_test.db \
  --source mispriced_category \
  --sql-dir vps_paper_shadow/sql
# → 500 rows inserted; rerun = 0 new (idempotent)
```

---

## Deploy to VPS

> [!warning] Confirm with Mr. V before each step. Each is reversible but additive.

### Step 1 — copy harness to VPS

```bash
rsync -avz --exclude tests/ --exclude __pycache__ \
  ~/Desktop/polyclawd-calibration/vps_paper_shadow/ \
  vps:/var/www/virtuosocrypto.com/polyclawd/vps_paper_shadow/

ssh vps "ls -la /var/www/virtuosocrypto.com/polyclawd/vps_paper_shadow/"
```

### Step 2 — dry-run on live DB

```bash
ssh vps "cd /var/www/virtuosocrypto.com/polyclawd && \
  ./venv/bin/python vps_paper_shadow/shadow_runner.py \
    --db storage/shadow_trades.db \
    --source mispriced_category \
    --limit 100 \
    --dry-run --verbose"
```

Expected: prints how many snapshots would be processed and the first 3 records. No writes yet.

### Step 3 — first real run (no timer yet)

```bash
ssh vps "cd /var/www/virtuosocrypto.com/polyclawd && \
  ./venv/bin/python vps_paper_shadow/shadow_runner.py \
    --db storage/shadow_trades.db \
    --source mispriced_category \
    --limit 1000"
```

Validate:

```bash
ssh vps "sqlite3 /var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db \
  'SELECT COUNT(*) n, MIN(snapshot_ts), MAX(snapshot_ts) FROM theta_shadow_log'"
```

Should see ≈1000 rows on first run. Re-run the runner immediately; should report `0 new snapshots`.

### Step 4 — install systemd timer

```bash
scp vps_paper_shadow/polyclawd-theta-shadow.service \
    vps_paper_shadow/polyclawd-theta-shadow.timer \
    vps:/tmp/

ssh vps "sudo mv /tmp/polyclawd-theta-shadow.* /etc/systemd/system/ && \
         sudo systemctl daemon-reload && \
         sudo systemctl enable --now polyclawd-theta-shadow.timer"
```

Validate:

```bash
ssh vps "systemctl list-timers polyclawd-theta-shadow.timer && \
         systemctl status polyclawd-theta-shadow.timer"
```

### Step 5 — monitor for 7-14 days

```bash
# Row growth (should rise ~50-200/day matching emission rate)
ssh vps "sqlite3 /var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db \
  'SELECT DATE(snapshot_ts) d, COUNT(*) n,
   ROUND(AVG(h0_confidence),1) h0,
   ROUND(AVG(h2_confidence),1) h2,
   ROUND(AVG(h0_confidence - h2_confidence),1) delta
   FROM theta_shadow_log GROUP BY d ORDER BY d DESC LIMIT 14'"

# Service health
ssh vps "journalctl -u polyclawd-theta-shadow.service --since=24h --no-pager | tail -30"
```

### Step 6 — post-window analysis

After 7-14 days, run the IC analyzer:

```bash
ssh vps "cd /var/www/virtuosocrypto.com/polyclawd && \
  ./venv/bin/python vps_paper_shadow/analyze_shadow.py \
    --db storage/shadow_trades.db --min-n 200"
```

Sample output:
```json
{
  "n_paired_rows": 1842,
  "n_distinct_markets": 47,
  "hypotheses": {
    "H0_original":     {"n": 1842, "ic": -0.18},
    "H1_drop_theta":   {"n": 1842, "ic":  0.22},
    "H2_invert_theta": {"n": 1842, "ic":  0.41}
  },
  "verdict": "H2 wins by +0.59 — deploy candidate"
}
```

**Decision gate:** If verdict says "deploy candidate" AND `n_distinct_markets ≥ 30`, proceed to the live H2 deploy (step 4 of the main deploy path). Otherwise extend shadow window or escalate.

---

## Rollback

| Concern | Rollback |
|---|---|
| Timer too noisy / DB pressure | `sudo systemctl disable --now polyclawd-theta-shadow.timer` |
| Schema regret | `sqlite3 .../shadow_trades.db "DROP TABLE theta_shadow_log"` |
| Full removal | Both above + `rm -rf /var/www/virtuosocrypto.com/polyclawd/vps_paper_shadow` + `sudo rm /etc/systemd/system/polyclawd-theta-shadow.*` |

No producer or trading code is touched. No production behavior changes during shadow.

---

## Known limitations

- **Snapshot ≠ trade.** This logs every emission, not just the ones that became trades. The actual deploy decision should also confirm post-H2 emissions still meet the `MispricedCategoryWhale` threshold (separate check).
- **5-min cadence.** The producer emits roughly every few minutes; 5-min cadence is fine but may briefly lag during burst periods. Backlog clears on next run.
- **Migration is forward-only.** The `001_create_theta_shadow_log.sql` is `CREATE IF NOT EXISTS` — safe to re-apply but not auto-migrating if columns change. For column additions later, add `002_*.sql` files.

---

## See also

- [[Mispriced-Walkforward-Theta-Fix-2026-05-15]] — walk-forward justifying H2
- [[Mispriced-Subscore-IC-Audit-2026-05-15]] — per-sub-score IC decomposition
- [[Mispriced-Category-DB-Review-2026-05-15]] — semantic errors in original audit
- `vps:/var/www/virtuosocrypto.com/polyclawd/signals/mispriced_category_signal.py:265-268,464-468` — producer composite (after step 4 of deploy path)
