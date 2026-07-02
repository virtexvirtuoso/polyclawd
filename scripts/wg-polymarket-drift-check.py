#!/usr/bin/env python3
"""wg-polymarket-drift-check — WireGuard split-tunnel drift DETECTOR (no auto-fix).

Verifies that every IPv4 address Polymarket's hostnames currently resolve to is
covered by the proton-ie tunnel's runtime AllowedIPs. Cloudflare rotates IPs;
an uncovered IP means that traffic silently bypasses the VPN (geo-block risk).
Detection + Telegram alert only — a human applies any config change.

Also guards the IPv6 story: v6 is intentionally excluded from the tunnel and
blackholed via `unreachable` routes (PostUp in proton-ie.conf). If a v6 entry
is present in runtime AllowedIPs WITHOUT a matching unreachable route, v6
traffic could enter the tunnel and blackhole -> alert.

Runs as linuxuser from cron every 30 min. Needs one narrow sudoers grant:
  linuxuser ALL=(root) NOPASSWD: /usr/bin/wg show proton-ie allowed-ips
(/etc/sudoers.d/wg-drift-check)

Alert policy (30-min monitor; "logs always, pings only on change"):
  - in sync   -> log line only, exit 0
  - drift     -> Telegram alert (deduped: same fingerprint re-alerts every 6h), exit 1
  - crash     -> Telegram alert with traceback tail (bookend contract), exit 2

Flags: --test-host HOST (append a hostname; for forced-drift verification)
       --force (bypass dedup suppression)
"""

import hashlib
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
import traceback

ENV_FILE = "/home/linuxuser/.config/polyclawd/alerts.env"
POLYCLAWD_TREE = "/var/www/virtuosocrypto.com/polyclawd"
STATE_FILE = "/home/linuxuser/logs/wg-polymarket-drift.state.json"
WG_IFACE = "proton-ie"
WG_CMD = ["sudo", "-n", "/usr/bin/wg", "show", WG_IFACE, "allowed-ips"]
REALERT_SECONDS = 6 * 3600  # identical drift re-pings at most every 6h

# Polymarket hosts the tunnel must cover (from grep of the polyclawd tree).
HOSTNAMES = [
    "clob.polymarket.com",
    "gamma-api.polymarket.com",
    "data-api.polymarket.com",
    "polymarket.com",
    "ws-subscriptions-clob.polymarket.com",
    "ws-live-data.polymarket.com",
    "sports-api.polymarket.com",
]


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S%z')} {msg}", flush=True)


def load_env(path: str) -> None:
    """Load KEY=VAL lines (absolute path only — cron-safe) into os.environ."""
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def send_telegram(message: str) -> bool:
    """Prefer the shared helper in the polyclawd tree; inline fallback so the
    alerter itself can't fail silently if the tree layout changes. Loads the
    env itself so crash-path alerts work even if main() died before load_env."""
    try:
        load_env(ENV_FILE)  # setdefault — idempotent
    except Exception as e:
        log(f"env load failed in alerter: {e}")
    try:
        sys.path.insert(0, POLYCLAWD_TREE)
        from scripts.openclaw_alerts import alert_openclaw  # type: ignore

        if alert_openclaw(message, parse_mode=""):
            return True
        log("helper returned False; using inline telegram fallback")
    except Exception as e:
        log(f"helper import/send failed ({e}); using inline telegram fallback")
    try:
        import urllib.parse
        import urllib.request

        token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "468298295")
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=payload
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return bool(json.loads(resp.read().decode()).get("ok", False))
    except Exception as e:
        log(f"ALERT DELIVERY FAILED: {e}")
        return False


def crash_hook(exc_type, exc, tb):
    tail = "".join(traceback.format_exception(exc_type, exc, tb))[-600:]
    send_telegram(f"[wg-drift-check] CRASH on VPS\n{tail}")
    sys.__excepthook__(exc_type, exc, tb)
    sys.exit(2)


def get_allowed_ips():
    """Return (v4_networks, v6_networks) from the RUNTIME wg state — runtime,
    not the conf file, is what actually routes traffic."""
    out = subprocess.run(WG_CMD, capture_output=True, text=True, timeout=20)
    if out.returncode != 0:
        raise RuntimeError(f"wg show failed rc={out.returncode}: {out.stderr.strip()}")
    v4, v6 = [], []
    for line in out.stdout.strip().splitlines():
        # format: <peer-pubkey>\t<cidr> <cidr> ...
        for cidr in line.split("\t", 1)[-1].split():
            if cidr == "(none)":
                continue
            net = ipaddress.ip_network(cidr, strict=False)
            (v4 if net.version == 4 else v6).append(net)
    return v4, v6


def resolve_v4(host):
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        return sorted({i[4][0] for i in infos})
    except socket.gaierror as e:
        log(f"WARN: DNS failure for {host}: {e} (skipping — not drift)")
        return []


def v6_unreachable_routes():
    out = subprocess.run(
        ["ip", "-6", "route", "show"], capture_output=True, text=True, timeout=10
    )
    return {
        line.split()[1]
        for line in out.stdout.splitlines()
        if line.startswith("unreachable ")
    }


def main() -> int:
    force = "--force" in sys.argv
    hosts = list(HOSTNAMES)
    if "--test-host" in sys.argv:
        hosts.append(sys.argv[sys.argv.index("--test-host") + 1])

    load_env(ENV_FILE)
    log(f"start: checking {len(hosts)} hosts against {WG_IFACE} AllowedIPs")

    v4_nets, v6_nets = get_allowed_ips()
    if not v4_nets:
        raise RuntimeError(
            f"{WG_IFACE} has NO IPv4 AllowedIPs — tunnel misconfigured or down"
        )

    problems = []
    for host in hosts:
        for ip in resolve_v4(host):
            addr = ipaddress.ip_address(ip)
            if not any(addr in net for net in v4_nets):
                problems.append(f"{host} -> {ip} NOT covered by AllowedIPs")

    # v6 guard: any v6 AllowedIPs entry must have a matching unreachable route,
    # otherwise v6 can enter the tunnel and blackhole (the 2026-07-02 bug).
    if v6_nets:
        unreach = v6_unreachable_routes()
        for net in v6_nets:
            candidates = {str(net), str(net.network_address)}
            if not (candidates & unreach):
                problems.append(
                    f"v6 {net} in AllowedIPs WITHOUT unreachable blackhole route"
                )

    if not problems:
        log(
            f"in sync: all resolved IPv4s covered by {[str(n) for n in v4_nets]}; "
            f"v6 entries={len(v6_nets)} (all blackholed)"
        )
        return 0

    fingerprint = hashlib.sha256("\n".join(sorted(problems)).encode()).hexdigest()
    state = {}
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    suppressed = (
        not force
        and state.get("fingerprint") == fingerprint
        and time.time() - state.get("alerted_at", 0) < REALERT_SECONDS
    )

    for p in problems:
        log(f"DRIFT: {p}")
    if suppressed:
        log("drift unchanged since last alert; Telegram ping suppressed (<6h)")
    else:
        msg = (
            "[wg-drift-check] Polymarket VPN split-tunnel DRIFT on VPS\n"
            + "\n".join(problems[:15])
            + f"\n\nAllowedIPs(v4): {', '.join(str(n) for n in v4_nets)}"
            + "\nAction: human review /etc/wireguard/proton-ie.conf (no auto-fix)."
        )
        sent = send_telegram(msg)
        log(f"telegram alert sent={sent}")
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"fingerprint": fingerprint, "alerted_at": time.time()}, f)
        os.replace(tmp, STATE_FILE)
    return 1


if __name__ == "__main__":
    sys.excepthook = crash_hook
    sys.exit(main())
