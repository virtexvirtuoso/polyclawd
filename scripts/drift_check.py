#!/usr/bin/env python3
"""
drift_check.py — alert when the local canonical tree drifts from the VPS deployed tree.

Canonical = what's deployed on the VPS. This guards the unified state established
2026-06-16: it md5-compares *.py under the core source dirs on local vs VPS and
alerts (Telegram, via the canonical alert_openclaw) on any NEW drift —
DIFFER, VPS-only, or LOCAL-only — beyond a small allowlist of intentional
local-only files.

Run by launchd daily. Exit 0 = in sync (or only allowlisted deltas); exit 3 = drift.

Usage:
  python3 scripts/drift_check.py            # check + alert on drift
  python3 scripts/drift_check.py --dry      # check + print, never alert
"""

import os
import sys
import subprocess
import hashlib
from datetime import datetime, timezone

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts"))

DIRS = ["odds", "signals", "services", "api"]
VPS_DIR = "/var/www/virtuosocrypto.com/polyclawd"
LOG = os.path.expanduser("~/Library/Logs/polyclawd-drift-check.log")
ALERT_ENV = os.path.expanduser("~/.config/polyclawd/alerts.env")
DRY = "--dry" in sys.argv


def _load_alert_env() -> None:
    """Populate TELEGRAM_* from the alerts env file so the direct Bot API fallback
    works under launchd, where the openclaw CLI is not on PATH (zsh's Homebrew PATH
    is not loaded by /bin/bash -lc)."""
    if not os.path.exists(ALERT_ENV):
        return
    with open(ALERT_ENV) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


# Intentional local-only files (held orphans, not deployed) — not drift.
ALLOWLIST_LOCAL_ONLY = {
    "odds/mlb_enrichment.py",
    "odds/pitcher_profile.py",
}


def _log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}Z  {msg}"
    print(line)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _local_manifest() -> dict:
    out = {}
    for d in DIRS:
        root = os.path.join(PROJECT_DIR, d)
        for dirpath, _, files in os.walk(root):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, PROJECT_DIR)
                with open(full, "rb") as fh:
                    out[rel] = hashlib.md5(fh.read()).hexdigest()
    return out


def _vps_manifest() -> dict:
    cmd = (
        f"cd {VPS_DIR} && for d in {' '.join(DIRS)}; do "
        f"find $d -name '*.py' -not -path '*/__pycache__/*' -exec md5sum {{}} \\;; done"
    )
    res = subprocess.run(
        ["ssh", "-n", "-o", "ConnectTimeout=20", "vps", cmd],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if res.returncode != 0:
        raise RuntimeError(f"ssh/vps manifest failed: {res.stderr.strip()[:200]}")
    out = {}
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        md5, path = line.split(None, 1)
        out[path] = md5
    return out


def main() -> int:
    try:
        loc = _local_manifest()
        vps = _vps_manifest()
    except Exception as e:
        _log(f"ERROR drift-check could not run: {e}")
        # Don't alert on transient ssh failures; just log.
        return 0

    differ = sorted(p for p in (set(loc) & set(vps)) if loc[p] != vps[p])
    vps_only = sorted(set(vps) - set(loc))
    local_only = sorted((set(loc) - set(vps)) - ALLOWLIST_LOCAL_ONLY)

    total = len(differ) + len(vps_only) + len(local_only)
    if total == 0:
        _log(f"OK in sync (local={len(loc)} vps={len(vps)}, allowlisted local-only ignored)")
        return 0

    def sample(lst, n=5):
        return ", ".join(lst[:n]) + (f" (+{len(lst) - n} more)" if len(lst) > n else "")

    lines = [
        f"DRIFT polyclawd local<->VPS: {total} files",
        f"  DIFFER ({len(differ)}): {sample(differ)}" if differ else "",
        f"  VPS-only ({len(vps_only)}): {sample(vps_only)}" if vps_only else "",
        f"  LOCAL-only ({len(local_only)}): {sample(local_only)}" if local_only else "",
    ]
    body = "\n".join(l for l in lines if l)
    _log(body.replace("\n", " | "))

    if not DRY:
        try:
            _load_alert_env()
            from openclaw_alerts import alert_openclaw

            ok = alert_openclaw("⚠️ " + body, channel="telegram", parse_mode=None)
            _log("alert sent" if ok else "WARN alert_openclaw returned False")
        except Exception as e:
            _log(f"WARN alert send failed: {e}")
    return 3


if __name__ == "__main__":
    sys.exit(main())
