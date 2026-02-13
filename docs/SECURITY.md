# Security Architecture — Polyclawd Infrastructure

> Last audit: 2026-02-13 | Status: **Hardened**

## Network Architecture

```
Internet
  │
  ├── 22/tcp ──→ SSH (key-only, no root, no passwords, fail2ban)
  ├── 80/tcp ──→ nginx → redirect to 443
  ├── 443/tcp ─→ nginx (TLS) → rate limited → backend services
  └── 51820/udp → WireGuard VPN → full internal access
                  (10.66.66.0/24 subnet)

All other ports: CLOSED to public, accessible only via WireGuard.
```

## Access Control

### SSH
| Setting | Value |
|---------|-------|
| Root login | **Disabled** |
| Password auth | **Disabled** |
| Key auth | **Enabled** (1 authorized key) |
| Max auth tries | 3 |
| Client keepalive | 300s interval, 2 max |
| fail2ban | Active, auto-bans brute force |
| Config | `/etc/ssh/sshd_config.d/hardened.conf` |

### WireGuard VPN
- Admin-only tunnel: `10.66.66.0/24`
- Full access to all internal ports via VPN
- Acts as emergency backdoor if nginx or SSH config breaks
- Port 51820/udp (cryptographically authenticated, silent drop on unknown peers)

### UFW Firewall
```
22/tcp      ALLOW   Anywhere         # SSH
80/tcp      ALLOW   Anywhere         # HTTP → HTTPS redirect
443/tcp     ALLOW   Anywhere         # HTTPS (nginx)
51820/udp   ALLOW   Anywhere         # WireGuard VPN
Anywhere    ALLOW   10.66.66.0/24    # Admin VPN full access
```

**Everything else is denied.** 13 previously public service ports closed on 2026-02-13.

### Backup Rules
- Pre-hardening UFW: `/etc/ufw/user.rules.backup.20260213`
- Pre-hardening SSH: `/etc/ssh/sshd_config.backup.20260213`
- Pre-hardening nginx: `/etc/nginx/sites-enabled/virtuoso-website.backup.20260213`

## Web Application Firewall

### Nginx Rate Limiting
| Zone | Rate | Burst | Scope |
|------|------|-------|-------|
| `general` | 30 req/s | 50 | All routes |
| `api` | 10 req/s | 20 | `/polyclawd/api/*` |
| `login` | 3 req/min | — | Auth endpoints |

Config: `/etc/nginx/conf.d/rate-limit.conf`

### Scanner Blocking
Instant silent drop (HTTP 444) for common exploit probes:
- `wp-admin`, `wp-login`, `wp-content`
- `.php`, `.asp`, `.env`, `.git`
- `phpmyadmin`, `xmlrpc`, `administrator`

### Fail2ban Jails (4 active)
| Jail | Trigger | Ban Duration |
|------|---------|-------------|
| `sshd` | SSH brute force | Default |
| `nginx-botsearch` | 3 scanner hits / 60s | **24 hours** |
| `nginx-http-auth` | 3 auth failures / 5min | 1 hour |
| `nginx-limit-req` | 5 rate limit hits / 10s | 1 hour |

Config: `/etc/fail2ban/jail.d/nginx.conf`

## Service Isolation

All backend services bind to `127.0.0.1` (localhost) and are only accessible through nginx reverse proxy:

| Service | Internal Port | Public Path |
|---------|--------------|-------------|
| Polyclawd API | 127.0.0.1:8420 | `/polyclawd/api/*` |
| Virtuoso Website | 127.0.0.1:8080 | `/` |
| Virtuoso Dashboard | 127.0.0.1:8002 | `/api/dashboard/*` |
| Virtuoso MCP | 127.0.0.1:8091 | `/mcp` |
| Derivatives API | 127.0.0.1:8004 | via subdomain proxy |
| Redis | 127.0.0.1:6379 | Not exposed |
| PostgreSQL | localhost | Not exposed |

## Local Machine (Mac Mini)

| Check | Status |
|-------|--------|
| FileVault disk encryption | ✅ Enabled |
| OpenClaw credentials dir | ✅ `chmod 700` |
| Time Machine backups | ✅ Configured |
| Auto security updates | ✅ Enabled |
| macOS firewall | ⚠️ Disabled (home network, low risk) |

## Automatic Protection

| System | What It Does | Frequency |
|--------|-------------|-----------|
| Unattended upgrades | Auto-installs security patches | Daily |
| fail2ban | Auto-bans malicious IPs | Real-time |
| nginx rate limiting | Throttles excessive requests | Real-time |
| Scanner blocking | Silently drops exploit probes | Real-time |
| Polyclawd watchdog | Restarts unhealthy services | Every 5 min |

## Rollback Procedures

### Revert UFW (if locked out via VPN)
```bash
sudo cp /etc/ufw/user.rules.backup.20260213 /etc/ufw/user.rules
sudo ufw reload
```

### Revert SSH
```bash
sudo rm /etc/ssh/sshd_config.d/hardened.conf
sudo systemctl restart ssh
```

### Revert nginx
```bash
sudo cp /etc/nginx/sites-enabled/virtuoso-website.backup.20260213 \
        /etc/nginx/sites-enabled/virtuoso-website
sudo rm /etc/nginx/conf.d/rate-limit.conf
sudo nginx -t && sudo systemctl reload nginx
```

### Revert fail2ban
```bash
sudo rm /etc/fail2ban/jail.d/nginx.conf
sudo systemctl restart fail2ban
```

## Remaining Considerations

- [ ] 2FA on VPS hosting provider account
- [ ] Periodic dependency audit (`pip audit`, `npm audit`)
- [ ] nginx access log monitoring / anomaly alerting
- [ ] API authentication for Polyclawd endpoints (currently open)
- [ ] Haiku model in OpenClaw fallback config (lower injection resistance)
- [ ] macOS firewall (low priority, home network)

## Threat Model

| Vector | Risk | Mitigation |
|--------|------|-----------|
| SSH brute force | ❌ Blocked | Key-only + fail2ban |
| Port scanning | ❌ Blocked | Only 4 ports open |
| Direct service exploit | ❌ Blocked | All services behind nginx |
| Web app vulnerability | ⚠️ Medium | Rate limiting + fail2ban |
| Dependency CVE | ⚠️ Medium | Auto-updates, needs manual pip/npm audit |
| API key leak | ⚠️ Medium | Keys in env vars, not in git |
| Social engineering | ⚠️ Low | 2FA on provider recommended |
| DDoS | ⚠️ Low | Rate limiting, no CDN |
