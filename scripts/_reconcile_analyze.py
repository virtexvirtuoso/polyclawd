#!/usr/bin/env python3
"""Analyze risk files: does local uncommitted diff touch polyproxy lines?
Reads risk list from /tmp/risk.txt. Prints per-file verdict."""
import subprocess, os, sys

os.chdir("/Users/ffv_macmini/Desktop/polyclawd")

risk = [l.strip() for l in open("/tmp/risk.txt") if l.strip()]

def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout

for f in risk:
    diff = run(f"git diff -- {f}")
    stat = run(f"git diff --stat -- {f}").strip().splitlines()
    statline = stat[-1] if stat else ""
    marker_lines = [l for l in diff.splitlines()
                    if l[:1] in "+-" and any(k in l for k in
                    ("polymarket_urls","GAMMA_API","POLYPROXY","_CENTRAL","gamma-api","gamma.polymarket"))]
    print(f"=== {f}")
    print(f"  {statline}")
    if marker_lines:
        print(f"  LOCAL DIFF TOUCHES POLYPROXY LINES ({len(marker_lines)}):")
        for l in marker_lines[:6]:
            print(f"    {l}")
    else:
        print(f"  local diff does NOT touch polyproxy lines -> SAFE to take VPS polyproxy version and re-apply local edits")
