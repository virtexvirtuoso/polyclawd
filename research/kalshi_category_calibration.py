#!/usr/bin/env python3
"""
kalshi_category_calibration.py — Category-sweep calibration screen for Kalshi.

Purpose
-------
Replicate the calibration-audit methodology that surfaced Polyclawd's ONE
validated edge (favorite-longshot-bias fade in daily-temperature brackets)
across *non-weather* Kalshi categories, hunting for the next weather-shaped
edge: a price bin whose realized settlement rate diverges from its implied
probability by a weather-magnitude gap that survives a dual cluster bootstrap.

This is a SCREEN, not a validated edge. No holdout, no PnL claim. Candidates
that emerge require a dedicated pre-registered audit + candlestick re-check
before any capital.

Methodology
-----------
1. Enumerate series per category from /series?category=X (public, no auth).
   EXCLUDE the whole 'Climate and Weather' category (already validated) plus
   KXMVE* and short-cycle/intraday churn series (known noise).
2. Pull settled markets per series (window = last LOOKBACK_DAYS by close_time).
   Kalshi v2 NULLs legacy cent fields — we read *_dollars STRING fields and
   float() them. Settled markets carry `result` in {yes,no}.
3. Reference price = a pre-settlement snapshot, NOT the final close. The final
   `last_price_dollars` on settled markets is convergence-degenerate (empirically
   collapses to 0/1 for ~all markets), so we pull hourly candlesticks and take
   the price REF_OFFSET_H hours before settlement, excluding the last
   CONVERGENCE_GUARD_H hours. Prefer the traded price (price.close_dollars);
   fall back to yes_bid/yes_ask midpoint only when the spread is tight
   (<= MAX_SPREAD). Illiquid markets with no tradeable reference are dropped.
4. Bin by reference price, compute realized settlement rate per bin, and flag
   miscalibration with a DUAL cluster bootstrap: cluster by event_ticker AND by
   series independently (per-market independence is FALSE for same-event
   brackets and same-series repeats). A bin is flagged only if the (realized -
   implied) gap is weather-magnitude AND its 95% CI excludes 0 under BOTH
   clustering dimensions (the conservative intersection).

Outputs
-------
- JSON  : <SCRATCH>/kalshi_category_calibration.json
- MD     : <SCRATCH>/kalshi_category_calibration.md
- stdout : compact per-category table + flagged candidates.

Read-only. No edits to existing Polyclawd code.
"""

from __future__ import annotations
import json
import sys
import time
import math
import random
import datetime as dt
import urllib.request
import urllib.error
import urllib.parse
from collections import defaultdict

# --------------------------------------------------------------------------- #
# Config (CLI-overridable: key=value pairs)                                    #
# --------------------------------------------------------------------------- #
BASE = "https://api.elections.kalshi.com/trade-api/v2"
UA = "polyclawd-calibration-research/1.0 (read-only screen)"

CFG = {
    "lookback_days": 90,
    "categories": "Economics,Financials,Crypto,Politics,Sports",  # weather excluded
    "series_per_cat": 100,  # cap series enumerated per category
    "candle_budget_cat": 250,  # max markets we fetch candlesticks for, per category
    "ref_offset_h": 18,  # reference snapshot this many hours before close
    "convergence_guard_h": 6,  # never use candles inside the last N hours (anti-convergence)
    "search_back_h": 30,  # candle search window: [close - search_back_h, close - guard]
    "max_spread": 0.20,  # max yes_ask-yes_bid to trust a midpoint reference
    "min_cat_n": 100,  # only report categories with >= this many calibratable markets
    "min_bin_n": 20,  # min markets in a bin to attempt a bootstrap flag
    "min_flag_events": 6,  # min distinct event clusters to trust a flag
    "min_flag_series": 3,  # min distinct series clusters to trust a flag
    "flag_gap": 0.04,  # |realized-implied| threshold to consider "weather-magnitude"
    "n_boot": 2000,
    "seed": 17,
    "sleep": 0.25,  # base inter-request spacing (s)
    "scratch": "/private/tmp/claude-501/-Users-ffv-macmini/102cdff1-0486-4668-a5b2-4e1ca518bbb6/scratchpad",
}

BINS = [(0.0, 0.05), (0.05, 0.15), (0.15, 0.30), (0.30, 0.50), (0.50, 0.70), (0.70, 0.85), (0.85, 0.95), (0.95, 1.0)]

# series ticker keyword blocklist: weather (belt+suspenders) + intraday churn
BLOCK_KEYWORDS = ("KXMVE", "HIGH", "LOW", "TEMP", "RAIN", "SNOW", "HUR", "QUAKE", "MICHTEMP", "MEAD")
# tickers hinting hourly/intraday up-down churn (short-cycle noise)
BLOCK_INTRADAY = ("UPDOWN", "HOURLY", "INTRADAY", "NYD")


# --------------------------------------------------------------------------- #
# HTTP with backoff                                                            #
# --------------------------------------------------------------------------- #
def api_get(url: str, retries: int = 7):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(1.2 * (i + 1) + random.random())
                continue
            if e.code in (404, 400):
                return None
            if 500 <= e.code < 600:
                time.sleep(0.8 * (i + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.8 * (i + 1))
    return None


def parse_ts(iso: str) -> int:
    return int(dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp())


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Step 1: enumerate series per category                                        #
# --------------------------------------------------------------------------- #
def enumerate_series(categories, cap):
    out = []  # (category, series_ticker)
    for cat in categories:
        d = api_get(f"{BASE}/series?category={urllib.parse.quote(cat)}")
        time.sleep(CFG["sleep"])
        if not d:
            continue
        kept = 0
        for s in d.get("series", []):
            st = s.get("ticker", "")
            up = st.upper()
            if any(k in up for k in BLOCK_KEYWORDS):
                continue
            if any(k in up for k in BLOCK_INTRADAY):
                continue
            out.append((cat, st))
            kept += 1
            if kept >= cap:
                break
    return out


# --------------------------------------------------------------------------- #
# Step 2: pull settled markets per series (windowed)                           #
# --------------------------------------------------------------------------- #
def pull_settled(series_list, min_close_ts):
    """Return list of candidate dicts (no candlestick yet)."""
    cands = []
    for i, (cat, st) in enumerate(series_list):
        url = f"{BASE}/markets?series_ticker={st}&status=settled&limit=1000&min_close_ts={min_close_ts}"
        d = api_get(url)
        time.sleep(CFG["sleep"])
        if not d:
            continue
        for m in d.get("markets", []):
            res = m.get("result")
            if res not in ("yes", "no"):
                continue
            ct = m.get("close_time")
            if not ct:
                continue
            cands.append(
                {
                    "category": cat,
                    "series": st,
                    "event": m.get("event_ticker", ""),
                    "ticker": m.get("ticker", ""),
                    "close_ts": parse_ts(ct),
                    "y": 1 if res == "yes" else 0,
                    "volume": fnum(m.get("volume_fp")) or 0.0,
                    "last_price": fnum(m.get("last_price_dollars")),
                }
            )
        if (i + 1) % 50 == 0:
            print(f"  ...settled pull {i + 1}/{len(series_list)} series, {len(cands)} candidates", flush=True)
    return cands


# --------------------------------------------------------------------------- #
# Step 3: pre-settlement reference price from candlesticks                     #
# --------------------------------------------------------------------------- #
def reference_price(cand):
    """Fetch hourly candles, return implied prob REF_OFFSET_H before close.
    Prefer traded price; else tight-spread midpoint. None if not calibratable."""
    close_ts = cand["close_ts"]
    guard = close_ts - CFG["convergence_guard_h"] * 3600
    start = close_ts - CFG["search_back_h"] * 3600
    target = close_ts - CFG["ref_offset_h"] * 3600
    st = cand["ticker"].split("-")[0]
    url = (
        f"{BASE}/series/{st}/markets/{cand['ticker']}/candlesticks"
        f"?start_ts={start}&end_ts={close_ts}&period_interval=60"
    )
    d = api_get(url)
    time.sleep(CFG["sleep"])
    if not d:
        return None
    candles = d.get("candlesticks", []) or []
    best_traded = None  # (abs_dist, price)
    best_mid = None
    for c in candles:
        ts = c.get("end_period_ts")
        if ts is None or ts > guard or ts < start:
            continue
        dist = abs(ts - target)
        pr = c.get("price") or {}
        tp = fnum(pr.get("close_dollars"))
        vol = fnum(c.get("volume_fp")) or 0.0
        if tp is not None and 0.0 < tp < 1.0 and vol > 0:
            if best_traded is None or dist < best_traded[0]:
                best_traded = (dist, tp)
        yb = fnum((c.get("yes_bid") or {}).get("close_dollars"))
        ya = fnum((c.get("yes_ask") or {}).get("close_dollars"))
        if yb is not None and ya is not None and ya >= yb:
            if (ya - yb) <= CFG["max_spread"]:
                mid = (ya + yb) / 2.0
                if 0.0 < mid < 1.0:
                    if best_mid is None or dist < best_mid[0]:
                        best_mid = (dist, mid)
    if best_traded is not None:
        return best_traded[1]
    if best_mid is not None:
        return best_mid[1]
    return None


# --------------------------------------------------------------------------- #
# Step 4: binning + dual cluster bootstrap                                     #
# --------------------------------------------------------------------------- #
def bin_of(p):
    for lo, hi in BINS:
        if (lo <= p < hi) or (hi == 1.0 and p < 1.0) or (hi == 1.0 and p == 1.0):
            return (lo, hi)
    return None


def cluster_boot_ci(obs, cluster_key_idx, n_boot, rng):
    """obs: list of (implied, y, event, series). Resample whole clusters with
    replacement; return 95% CI of (realized - implied). cluster_key_idx: 2=event,3=series."""
    clusters = defaultdict(list)
    for o in obs:
        clusters[o[cluster_key_idx]].append(o)
    keys = list(clusters.keys())
    if len(keys) < 2:
        return None
    diffs = []
    for _ in range(n_boot):
        pool = []
        for _ in range(len(keys)):
            pool.extend(clusters[keys[rng.randrange(len(keys))]])
        if not pool:
            continue
        real = sum(o[1] for o in pool) / len(pool)
        impl = sum(o[0] for o in pool) / len(pool)
        diffs.append(real - impl)
    if not diffs:
        return None
    diffs.sort()
    lo = diffs[int(0.025 * len(diffs))]
    hi = diffs[int(0.975 * len(diffs)) - 1]
    return (lo, hi)


def analyze_category(cat, obs, rng):
    """obs: list of (implied, y, event, series). Return per-bin table + flags."""
    bybin = defaultdict(list)
    for o in obs:
        b = bin_of(o[0])
        if b:
            bybin[b].append(o)
    table, flags = [], []
    for lo, hi in BINS:
        rows = bybin.get((lo, hi), [])
        n = len(rows)
        if n == 0:
            continue
        impl = sum(r[0] for r in rows) / n
        real = sum(r[1] for r in rows) / n
        gap = real - impl
        n_events = len({r[2] for r in rows})
        n_series = len({r[3] for r in rows})
        row = {
            "bin": f"[{lo:.2f},{hi:.2f})",
            "n": n,
            "n_events": n_events,
            "n_series": n_series,
            "implied": round(impl, 4),
            "realized": round(real, 4),
            "gap": round(gap, 4),
            "ci_event": None,
            "ci_series": None,
            "survives": False,
        }
        enough_clusters = n_events >= CFG["min_flag_events"] and n_series >= CFG["min_flag_series"]
        if n >= CFG["min_bin_n"] and abs(gap) >= CFG["flag_gap"] and enough_clusters:
            ci_e = cluster_boot_ci(rows, 2, CFG["n_boot"], rng)
            ci_s = cluster_boot_ci(rows, 3, CFG["n_boot"], rng)
            row["ci_event"] = [round(x, 4) for x in ci_e] if ci_e else None
            row["ci_series"] = [round(x, 4) for x in ci_s] if ci_s else None
            surv_e = ci_e is not None and (ci_e[0] > 0 or ci_e[1] < 0)
            surv_s = ci_s is not None and (ci_s[0] > 0 or ci_s[1] < 0)
            row["survives"] = bool(surv_e and surv_s)
            if row["survives"]:
                flags.append({"category": cat, **row})
        table.append(row)
    return table, flags


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    for a in sys.argv[1:]:
        if "=" in a:
            k, v = a.split("=", 1)
            if k in CFG:
                CFG[k] = type(CFG[k])(v) if not isinstance(CFG[k], str) else v
    rng = random.Random(CFG["seed"])
    cats = [c.strip() for c in CFG["categories"].split(",") if c.strip()]
    now = dt.datetime.now(dt.timezone.utc)
    min_close_ts = int((now - dt.timedelta(days=CFG["lookback_days"])).timestamp())

    print(f"[1/4] Enumerating series across {cats} (weather excluded)...", flush=True)
    series_list = enumerate_series(cats, CFG["series_per_cat"])
    print(f"      {len(series_list)} non-weather/non-churn series", flush=True)

    print(f"[2/4] Pulling settled markets (last {CFG['lookback_days']}d)...", flush=True)
    cands = pull_settled(series_list, min_close_ts)
    all_close = [c["close_ts"] for c in cands]
    print(f"      {len(cands)} settled markets with result in {{yes,no}}", flush=True)

    # per-category candlestick budget: prioritize higher-volume (calibratable) markets
    bycat = defaultdict(list)
    for c in cands:
        bycat[c["category"]].append(c)
    sampled = []
    for cat, lst in bycat.items():
        lst.sort(key=lambda x: x["volume"], reverse=True)
        sampled.extend(lst[: CFG["candle_budget_cat"]])
    print(
        f"[3/4] Fetching pre-settlement reference for {len(sampled)} markets (~{CFG['ref_offset_h']}h pre-close)...",
        flush=True,
    )

    obs_by_cat = defaultdict(list)  # (implied, y, event, series)
    calibrated = 0
    for i, c in enumerate(sampled):
        ref = reference_price(c)
        if ref is None:
            continue
        obs_by_cat[c["category"]].append((ref, c["y"], c["event"], c["series"]))
        calibrated += 1
        if (i + 1) % 100 == 0:
            print(f"  ...ref {i + 1}/{len(sampled)}, {calibrated} calibratable", flush=True)
    print(f"      {calibrated} markets with a valid pre-settlement reference", flush=True)

    print("[4/4] Per-category calibration + dual cluster bootstrap...", flush=True)
    results, all_flags = {}, []
    for cat in cats:
        obs = obs_by_cat.get(cat, [])
        if len(obs) < CFG["min_cat_n"]:
            results[cat] = {"n": len(obs), "reported": False, "table": []}
            continue
        table, flags = analyze_category(cat, obs, rng)
        results[cat] = {"n": len(obs), "reported": True, "table": table}
        all_flags.extend(flags)

    date_range = None
    if all_close:
        date_range = [
            dt.datetime.utcfromtimestamp(min(all_close)).strftime("%Y-%m-%d"),
            dt.datetime.utcfromtimestamp(max(all_close)).strftime("%Y-%m-%d"),
        ]

    out = {
        "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": CFG,
        "n_settled_pulled": len(cands),
        "n_calibratable": calibrated,
        "settlement_date_range": date_range,
        "categories": results,
        "flags": sorted(all_flags, key=lambda f: -abs(f["gap"])),
    }
    scratch = CFG["scratch"]
    jpath = f"{scratch}/kalshi_category_calibration.json"
    mpath = f"{scratch}/kalshi_category_calibration.md"
    with open(jpath, "w") as f:
        json.dump(out, f, indent=2)
    write_md(out, mpath)

    # stdout summary
    print("\n" + "=" * 70)
    print(f"SETTLED PULLED: {len(cands)} | CALIBRATABLE: {calibrated} | RANGE: {date_range}")
    for cat in cats:
        r = results[cat]
        if not r["reported"]:
            print(f"\n[{cat}] n={r['n']} (< {CFG['min_cat_n']}, not reported)")
            continue
        print(f"\n[{cat}] calibratable n={r['n']}")
        print(f"  {'bin':13s} {'n':>4s} {'ev':>4s} {'ser':>4s} {'impl':>6s} {'real':>6s} {'gap':>7s} surv")
        for row in r["table"]:
            print(
                f"  {row['bin']:13s} {row['n']:4d} {row['n_events']:4d} "
                f"{row['n_series']:4d} {row['implied']:6.3f} {row['realized']:6.3f} "
                f"{row['gap']:+7.3f} {'YES' if row['survives'] else ''}"
            )
    print("\n--- FLAGGED (weather-magnitude gap, survives dual cluster bootstrap) ---")
    if not all_flags:
        print("  none")
    for f in out["flags"]:
        print(
            f"  {f['category']:11s} {f['bin']:13s} n={f['n']:4d} "
            f"impl={f['implied']:.3f} real={f['realized']:.3f} "
            f"gap={f['gap']:+.3f} ci_ev={f['ci_event']} ci_ser={f['ci_series']}"
        )
    print(f"\nJSON: {jpath}\nMD:   {mpath}")


def write_md(out, path):
    L = []
    L.append("# Kalshi Category Calibration Screen\n")
    L.append(f"- Generated: {out['generated']}")
    L.append(f"- Settled markets pulled: {out['n_settled_pulled']}")
    L.append(f"- Calibratable (valid pre-settlement reference): {out['n_calibratable']}")
    L.append(f"- Settlement date range: {out['settlement_date_range']}")
    L.append(
        f"- Reference: candlestick ~{out['config']['ref_offset_h']}h before close, "
        f"excluding last {out['config']['convergence_guard_h']}h "
        f"(traded price preferred; tight-spread midpoint fallback)\n"
    )
    L.append("## Caveats\n")
    L.append(
        "- SCREEN ONLY — no holdout, no PnL claim. Candidates need a dedicated pre-registered audit before capital."
    )
    L.append(
        "- Reference-price choice: a pre-settlement candlestick snapshot. Final "
        "`last_price_dollars` was rejected (convergence-degenerate: collapses to 0/1)."
    )
    L.append(
        "- Survivorship: only *settled* markets in-window; voided markets and "
        "never-traded illiquid brackets are dropped (drops calibratable N)."
    )
    L.append(
        "- Fees: Kalshi charges ~0.07*p*(1-p) per contract; a gap must clear this "
        "(~1.75c at p=0.5, ~0.9c at p=0.85) to be tradeable."
    )
    L.append(
        "- Per-market independence is FALSE for same-event brackets; flags require "
        "the gap's 95% CI to exclude 0 under BOTH event- and series-clustered bootstraps.\n"
    )
    for cat, r in out["categories"].items():
        if not r["reported"]:
            L.append(f"## {cat} — n={r['n']} (below reporting threshold)\n")
            continue
        L.append(f"## {cat} — calibratable n={r['n']}\n")
        L.append("| bin | n | events | series | implied | realized | gap | survives |")
        L.append("|---|---|---|---|---|---|---|---|")
        for row in r["table"]:
            L.append(
                f"| {row['bin']} | {row['n']} | {row['n_events']} | "
                f"{row['n_series']} | {row['implied']:.3f} | {row['realized']:.3f} | "
                f"{row['gap']:+.3f} | {'YES' if row['survives'] else ''} |"
            )
        L.append("")
    L.append("## Flagged candidates\n")
    if not out["flags"]:
        L.append("None survived the dual cluster bootstrap at the configured gap threshold.\n")
    else:
        L.append("| category | bin | n | implied | realized | gap | CI(event) | CI(series) |")
        L.append("|---|---|---|---|---|---|---|---|")
        for f in out["flags"]:
            L.append(
                f"| {f['category']} | {f['bin']} | {f['n']} | {f['implied']:.3f} | "
                f"{f['realized']:.3f} | {f['gap']:+.3f} | {f['ci_event']} | {f['ci_series']} |"
            )
    with open(path, "w") as fh:
        fh.write("\n".join(L))


if __name__ == "__main__":
    main()
