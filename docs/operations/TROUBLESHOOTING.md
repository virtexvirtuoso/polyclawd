# Polyclawd Troubleshooting Guide

Common issues and their solutions.

---

## Quick Diagnostics

```bash
# Check API health
curl https://virtuosocrypto.com/polyclawd/api/health

# Check service status (VPS)
sudo systemctl status polyclawd

# View recent logs
sudo journalctl -u polyclawd -n 100

# Check if port is listening
lsof -i :8000
```

---

## Common Issues

### API Won't Start

**Symptom:** Service fails to start, port 8000 not responding

**Check 1: Port in use**
```bash
lsof -i :8000
# If something is using it:
kill -9 <PID>
```

**Check 2: Virtual environment**
```bash
cd ~/polyclawd
source venv/bin/activate
pip install -r requirements.txt
python -c "import fastapi; print('OK')"
```

**Check 3: Python version**
```bash
python3 --version  # Should be 3.11+
```

**Check 4: Permissions**
```bash
ls -la ~/polyclawd
# Ensure owned by correct user
sudo chown -R polyclawd:polyclawd ~/polyclawd
```

**Check 5: Log errors**
```bash
sudo journalctl -u polyclawd -n 50
# Look for ImportError, PermissionError, etc.
```

---

### API Quota Exceeded

**Symptom:** Empty responses, 429 errors, "rate limit" in logs

**Affected services:**
- The Odds API (Vegas/Betfair)
- External prediction markets

**Solutions:**

1. **Check Odds API usage:**
   ```bash
   # Log in to the-odds-api.com and check quota
   # Free tier: 500 requests/month
   ```

2. **Increase cache TTL:**
   Edit `api/edge_cache.py`:
   ```python
   CACHE_TTL = 120  # Increase from 60 to 120 seconds
   ```

3. **Reduce scan frequency:**
   Update cron jobs to run less often:
   ```bash
   openclaw cron update <job-id> --schedule "0 */4 * * *"
   ```

4. **Check for runaway requests:**
   ```bash
   grep "Odds API" logs/polyclawd.log | wc -l
   ```

---

### Missing Data Sources

**Symptom:** Signals endpoint returns fewer sources than expected

**Check which sources are failing:**
```bash
curl https://virtuosocrypto.com/polyclawd/api/signals | jq '.signals | group_by(.source) | map({source: .[0].source, count: length})'
```

**Source-specific fixes:**

| Source | Common Issue | Fix |
|--------|--------------|-----|
| Vegas | Odds API quota | Reduce frequency or upgrade plan |
| ESPN | API changed | Check `odds/espn_odds.py` for 404s |
| Kalshi | Auth expired | Re-enter credentials in `.env` |
| PredictIt | Rate limited | Wait 15 minutes |
| Manifold | API timeout | Increase timeout in `odds/manifold.py` |
| News | Google blocking | Rotate User-Agent |

**Manual test each source:**
```bash
# Test Vegas
curl https://virtuosocrypto.com/polyclawd/api/vegas/nfl

# Test ESPN
curl https://virtuosocrypto.com/polyclawd/api/espn/odds

# Test Kalshi
curl https://virtuosocrypto.com/polyclawd/api/kalshi/markets
```

---

### Service Won't Start (Systemd)

**Symptom:** `systemctl start polyclawd` fails

**Check 1: Service file syntax**
```bash
sudo systemd-analyze verify /etc/systemd/system/polyclawd.service
```

**Check 2: ExecStart path**
```bash
# Verify path exists
ls -la /home/polyclawd/polyclawd/venv/bin/uvicorn
```

**Check 3: User permissions**
```bash
# Ensure user exists
id polyclawd

# Ensure user can access directory
sudo -u polyclawd ls -la /home/polyclawd/polyclawd
```

**Check 4: Full error log**
```bash
sudo journalctl -u polyclawd -e --no-pager
```

---

### Stale Data / Old Prices

**Symptom:** Market prices not updating, showing old data

**Clear caches:**
```bash
# Clear Vegas cache
rm ~/polyclawd/odds/vegas_cache.json

# Clear news cache
rm ~/.openclaw/polyclawd/news_cache.json

# Restart service
sudo systemctl restart polyclawd
```

**Check cache age in API:**
```bash
curl https://virtuosocrypto.com/polyclawd/api/metrics | jq '.cache_stats'
```

---

### Trading Engine Not Running

**Symptom:** Engine status shows "stopped", no auto-trades

**Check status:**
```bash
curl https://virtuosocrypto.com/polyclawd/api/engine/status
```

**Start engine:**
```bash
curl -X POST https://virtuosocrypto.com/polyclawd/api/engine/start
```

**Check if daily limit hit:**
```bash
curl https://virtuosocrypto.com/polyclawd/api/phase/limits
```

**Reset daily counters:**
```bash
curl -X POST https://virtuosocrypto.com/polyclawd/api/engine/reset-daily
```

---

### MCP Server Issues

**Symptom:** Claude can't connect to Polyclawd MCP tools

**Check 1: Server running**
```bash
curl https://virtuosocrypto.com/polyclawd/api/health
# Must return {"status": "healthy"}
```

**Check 2: MCP config**
```bash
cat ~/.claude/claude_desktop_config.json
# Verify polyclawd entry exists
```

**Check 3: Test locally**
```bash
cd ~/polyclawd
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python mcp/server.py
```

**Check 4: Network from Claude**
MCP server calls `https://virtuosocrypto.com/polyclawd` - ensure this is accessible.

---

### Paper Trading Balance Wrong

**Symptom:** Balance shows unexpected value, missing trades

**Check storage files:**
```bash
cat ~/polyclawd/storage/trades.json | jq '.'
```

**Reset paper trading:**
```bash
curl -X POST "https://virtuosocrypto.com/polyclawd/api/reset" \
  -H "X-API-Key: your-key" \
  -d "starting_balance=1000"
```

---

### Signals Have Low Confidence

**Symptom:** All signals showing confidence < 40

**Check Bayesian priors:**
```bash
cat ~/polyclawd/data/source_outcomes.json | jq '.'
```

If win rates are very low, either:
1. Wait for more data (priors will correct)
2. Reset priors:
   ```bash
   rm ~/polyclawd/data/source_outcomes.json
   # Restart service to regenerate defaults
   sudo systemctl restart polyclawd
   ```

---

## Log Locations

| Log | Location | Command |
|-----|----------|---------|
| Application | `logs/polyclawd.log` | `tail -f logs/polyclawd.log` |
| Systemd | journald | `sudo journalctl -u polyclawd -f` |
| Nginx | `/var/log/nginx/` | `tail -f /var/log/nginx/error.log` |
| Cron jobs | OpenClaw | `openclaw cron logs <job-id>` |

---

## Log Analysis

### Find errors in last hour

```bash
sudo journalctl -u polyclawd --since "1 hour ago" | grep -i error
```

### Count requests by endpoint

```bash
grep "GET\|POST" logs/polyclawd.log | awk '{print $7}' | sort | uniq -c | sort -rn
```

### Find slow requests

```bash
grep "took" logs/polyclawd.log | awk '$NF > 5 {print}'
```

---

## Health Checks

### Full System Check

```bash
#!/bin/bash
echo "=== Polyclawd Health Check ==="

# API Health
echo -n "API: "
curl -s https://virtuosocrypto.com/polyclawd/api/health | jq -r '.status'

# Service Status
echo -n "Service: "
sudo systemctl is-active polyclawd

# Disk Space
echo -n "Disk: "
df -h /home/polyclawd | tail -1 | awk '{print $5 " used"}'

# Memory
echo -n "Memory: "
free -h | grep Mem | awk '{print $3 "/" $2}'

# Last signal scan
echo -n "Last Scan: "
curl -s https://virtuosocrypto.com/polyclawd/api/engine/status | jq -r '.last_scan'
```

### Automated Monitoring

Use `polyclawd-monitor` cron job (see [CRON_JOBS.md](CRON_JOBS.md)).

---

## Getting Help

1. **Check logs first** - Most issues are visible in logs
2. **Test endpoints manually** - Use curl to isolate the problem
3. **Check external APIs** - Many issues are upstream
4. **Restart cleanly** - `sudo systemctl restart polyclawd`
5. **Check GitHub issues** - Someone may have hit the same problem
