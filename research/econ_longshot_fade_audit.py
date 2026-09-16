#!/usr/bin/env python3
"""
econ_longshot_fade_audit.py — Pre-registered KILL/KEEP/INSUFFICIENT audit of the
Economics YES-longshot fade candidate flagged by kalshi_category_calibration.py
(screen dated 2026-07-09).

===========================================================================
PRE-REGISTRATION  (written BEFORE any statistic is computed — do not edit
                   the hypothesis/splits below after seeing results)
===========================================================================

CANDIDATE (from the screen)
  Kalshi Economics-category markets whose reference price ~18h before close sits
  in the [0.15, 0.30) YES bin settled YES only 5% of the time vs 20% implied
  (gap -15pt, n=20, survived event- AND series-clustered bootstrap in-screen).
  Interpretation: YES-longshots in Economics are systematically overpriced ->
  the NO side is underpriced -> fade by buying NO.

H1 (directional, one primary bin):
  Buying the NO side of Economics markets referenced at [0.15,0.30) YES, ~18h
  before close, is +EV AFTER Kalshi fees (fee = 0.07 * p * (1-p) per contract,
  p = contract price). Null H0: fee-adjusted per-contract EV <= 0.

  Success test (pre-registered, must clear ALL to KEEP):
    (a) Depth >= ~6 months of settled Economics markets with usable candlesticks.
    (b) Estimation-set bin N >= 50 (independent-ish events).
    (c) Estimation gap 95% CI excludes 0 under BOTH event- and series-clustered
        bootstrap (dual, as in the screen).
    (d) The gap is NOT driven by 1-2 series (per-series decomposition: no single
        series contributes > ~50% of the aggregate gap*n mass; >=4 series with
        same-sign gap).
    (e) Holdout (never-optimized) bin reproduces a same-sign, fee-positive gap on
        the ONE look we are permitted.
  If (a) or (b) fails -> verdict INSUFFICIENT (depth/N), and the deliverable is a
  forward paper-shadow protocol, not a KEEP/KILL on the effect itself.
  If (a)+(b) pass but (c)/(d)/(e) fail -> KILL (noise or series-specific quirk).

SPLIT (pre-registered):
  Chronological halves by settlement (close) timestamp. Estimation = OLDER half,
  Holdout = NEWER half. (Odd/even settlement-month split was the fallback if depth
  >= 6 months; depth probe shows it does not, so chronological halves stand.)
  The dual cluster bootstrap and ALL tuning happen on estimation only. Holdout is
  looked at EXACTLY ONCE.

REFERENCE-PRICE PROTOCOL (identical to the screen — no re-tuning):
  Hourly candlestick ~REF_OFFSET_H (18h) before close_time; never use candles
  inside the last CONVERGENCE_GUARD_H (6h) (final last_price is convergence-
  degenerate, collapses to 0/1 -> forbidden). Prefer traded price
  (price.close_dollars, vol>0); else tight-spread yes_bid/yes_ask midpoint
  (spread <= MAX_SPREAD). No tradeable reference -> market dropped.

FEE / PnL MODEL (pre-registered):
  Buy 1 NO contract at cost_no = (1 - yes_ref). Kalshi fee = 0.07*p*(1-p),
  p = yes_ref (symmetric in p vs 1-p). Realized PnL per contract:
     y==0 (NO wins):  +(1 - cost_no) - fee
     y==1 (YES wins): -cost_no        - fee
  Correlated same-event brackets are capped: PnL is aggregated to EVENT level
  (mean PnL of that event's qualifying markets = one exposure unit) before
  computing EV, win rate, and the drawdown curve (ordered by close time).

DATA / EXCLUSIONS:
  Kalshi v2 NULLs legacy cent fields -> read *_dollars STRING fields, float().
  Settled markets carry result in {yes,no}. Exclude weather (belt+suspenders
  keyword block) and KXMVE / short-cycle intraday churn series. Only Economics
  category. Read-only public API, no auth. Bounded request budget.

Outputs: JSON + MD to SCRATCH. Read-only; creates no edits to Polyclawd code.
"""

from __future__ import annotations
import json, sys, time, random, datetime as dt
import urllib.request, urllib.error, urllib.parse
from collections import defaultdict

BASE = "https://api.elections.kalshi.com/trade-api/v2"
UA = "polyclawd-econ-longshot-audit/1.0 (read-only research)"

CFG = {
    "lookback_days": 400,       # ask for deep history; depth is measured, not assumed
    "series_cap": 700,          # cap Economics series enumerated
    "candle_budget": 1400,      # max candlestick fetches (bounds runtime)
    "ref_offset_h": 18,
    "convergence_guard_h": 6,
    "search_back_h": 30,
    "max_spread": 0.20,
    "primary_bin": (0.15, 0.30),
    "min_estimation_bin_n": 50,   # pre-registered N gate
    "depth_months_gate": 6.0,     # pre-registered depth gate
    "min_flag_events": 6,
    "min_flag_series": 3,
    "n_boot": 4000,
    "seed": 17,
    "sleep": 0.18,
    "fee_rate": 0.07,
    "scratch": "/private/tmp/claude-501/-Users-ffv-macmini/102cdff1-0486-4668-a5b2-4e1ca518bbb6/scratchpad",
}

BINS = [(0.0,0.05),(0.05,0.15),(0.15,0.30),(0.30,0.50),(0.50,0.70),(0.70,0.85),(0.85,0.95),(0.95,1.0)]
BLOCK_KEYWORDS = ("KXMVE","HIGH","LOW","TEMP","RAIN","SNOW","HUR","QUAKE","MICHTEMP","MEAD")
BLOCK_INTRADAY = ("UPDOWN","HOURLY","INTRADAY","NYD")


def api_get(url, retries=6):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(1.3*(i+1) + random.random()); continue
            if e.code in (404, 400):
                return None
            if 500 <= e.code < 600:
                time.sleep(0.8*(i+1)); continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.8*(i+1))
    return None


def pts(iso): return int(dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp())
def fnum(x):
    try: return float(x)
    except (TypeError, ValueError): return None
def bin_of(p):
    for lo, hi in BINS:
        if lo <= p < hi or (hi == 1.0 and p <= 1.0 and p >= lo): return (lo, hi)
    return None


def enumerate_series(cap):
    d = api_get(f"{BASE}/series?category={urllib.parse.quote('Economics')}")
    time.sleep(CFG["sleep"])
    out = []
    for s in (d.get("series", []) if d else []):
        st = s.get("ticker", ""); up = st.upper()
        if any(k in up for k in BLOCK_KEYWORDS): continue
        if any(k in up for k in BLOCK_INTRADAY): continue
        out.append(st)
        if len(out) >= cap: break
    return out


def pull_settled(series_list, min_close_ts):
    cands = []
    for i, st in enumerate(series_list):
        url = f"{BASE}/markets?series_ticker={st}&status=settled&limit=1000&min_close_ts={min_close_ts}"
        d = api_get(url); time.sleep(CFG["sleep"])
        if not d: continue
        for m in d.get("markets", []):
            if m.get("result") not in ("yes", "no"): continue
            ct = m.get("close_time")
            if not ct: continue
            cands.append({
                "series": st, "event": m.get("event_ticker", ""), "ticker": m.get("ticker", ""),
                "close_ts": pts(ct), "close_iso": ct,
                "y": 1 if m.get("result") == "yes" else 0,
                "volume": fnum(m.get("volume_fp")) or 0.0,
            })
        if (i+1) % 100 == 0:
            print(f"  ...settled {i+1}/{len(series_list)} series, {len(cands)} cands", flush=True)
    return cands


def reference_price(c):
    close_ts = c["close_ts"]
    guard = close_ts - CFG["convergence_guard_h"]*3600
    start = close_ts - CFG["search_back_h"]*3600
    target = close_ts - CFG["ref_offset_h"]*3600
    base = c["ticker"].split("-")[0]
    url = (f"{BASE}/series/{base}/markets/{c['ticker']}/candlesticks"
           f"?start_ts={start}&end_ts={close_ts}&period_interval=60")
    d = api_get(url); time.sleep(CFG["sleep"])
    if not d: return None
    best_t = best_m = None
    for cd in (d.get("candlesticks", []) or []):
        ts = cd.get("end_period_ts")
        if ts is None or ts > guard or ts < start: continue
        dist = abs(ts - target)
        pr = cd.get("price") or {}
        tp = fnum(pr.get("close_dollars")); vol = fnum(cd.get("volume_fp")) or 0.0
        if tp is not None and 0.0 < tp < 1.0 and vol > 0:
            if best_t is None or dist < best_t[0]: best_t = (dist, tp)
        yb = fnum((cd.get("yes_bid") or {}).get("close_dollars"))
        ya = fnum((cd.get("yes_ask") or {}).get("close_dollars"))
        if yb is not None and ya is not None and ya >= yb and (ya - yb) <= CFG["max_spread"]:
            mid = (ya + yb)/2.0
            if 0.0 < mid < 1.0 and (best_m is None or dist < best_m[0]): best_m = (dist, mid)
    if best_t is not None: return best_t[1]
    if best_m is not None: return best_m[1]
    return None


def cluster_boot_ci(obs, key_idx, n_boot, rng):
    """obs rows: (implied, y, event, series). Resample whole clusters; 95% CI of realized-implied."""
    clusters = defaultdict(list)
    for o in obs: clusters[o[key_idx]].append(o)
    keys = list(clusters.keys())
    if len(keys) < 2: return None
    diffs = []
    for _ in range(n_boot):
        pool = []
        for _ in range(len(keys)): pool.extend(clusters[keys[rng.randrange(len(keys))]])
        if not pool: continue
        diffs.append(sum(o[1] for o in pool)/len(pool) - sum(o[0] for o in pool)/len(pool))
    if not diffs: return None
    diffs.sort()
    return (diffs[int(0.025*len(diffs))], diffs[int(0.975*len(diffs))-1])


def calib_table(obs):
    bybin = defaultdict(list)
    for o in obs:
        b = bin_of(o[0])
        if b: bybin[b].append(o)
    tbl = []
    for lo, hi in BINS:
        rows = bybin.get((lo, hi), [])
        if not rows: continue
        n = len(rows)
        impl = sum(r[0] for r in rows)/n; real = sum(r[1] for r in rows)/n
        tbl_row = {"bin": f"[{lo:.2f},{hi:.2f})", "n": n,
                   "events": len({r[2] for r in rows}), "series": len({r[3] for r in rows}),
                   "implied": round(impl,4), "realized": round(real,4), "gap": round(real-impl,4)}
        tbl.append(tbl_row)
    return tbl


def pnl_sim(rows, fee_rate):
    """rows: (implied yes_ref, y, event, series). Buy 1 NO. Aggregate to event level."""
    by_ev = defaultdict(list)
    for imp, y, ev, ser in rows:
        cost_no = 1.0 - imp
        fee = fee_rate * imp * (1.0 - imp)
        pnl = (1.0 - cost_no - fee) if y == 0 else (-cost_no - fee)
        by_ev[ev].append((c_close_lookup.get((imp,y,ev,ser), 0), pnl))
    # event-level mean pnl, ordered by close time
    ev_units = []
    for ev, lst in by_ev.items():
        mean_pnl = sum(p for _, p in lst)/len(lst)
        ct = max(t for t, _ in lst)
        ev_units.append((ct, mean_pnl))
    ev_units.sort(key=lambda x: x[0])
    pnls = [p for _, p in ev_units]
    n = len(pnls)
    if n == 0: return None
    ev_mean = sum(pnls)/n
    wins = sum(1 for p in pnls if p > 0)
    # drawdown on cumulative event-level pnl
    cum = 0.0; peak = 0.0; mdd = 0.0
    for p in pnls:
        cum += p; peak = max(peak, cum); mdd = min(mdd, cum - peak)
    return {"n_events": n, "ev_per_contract": round(ev_mean,4),
            "win_rate": round(wins/n,4), "total_pnl": round(sum(pnls),4),
            "max_drawdown": round(mdd,4)}


c_close_lookup = {}  # (imp,y,event,series) -> close_ts, filled in main


def main():
    for a in sys.argv[1:]:
        if "=" in a:
            k, v = a.split("=", 1)
            if k in CFG and not isinstance(CFG[k], (tuple, dict)):
                CFG[k] = type(CFG[k])(v) if not isinstance(CFG[k], str) else v
    rng = random.Random(CFG["seed"])
    now = dt.datetime.now(dt.timezone.utc)
    min_close_ts = int((now - dt.timedelta(days=CFG["lookback_days"])).timestamp())

    print("[1/5] Enumerating Economics series...", flush=True)
    series = enumerate_series(CFG["series_cap"])
    print(f"      {len(series)} econ series (weather/intraday excluded)", flush=True)

    print(f"[2/5] Pulling settled markets (asked back {CFG['lookback_days']}d)...", flush=True)
    cands = pull_settled(series, min_close_ts)
    print(f"      {len(cands)} settled Economics markets with result", flush=True)
    if not cands:
        print("No candidates. Abort."); return

    # DEPTH measured from settled markets actually returned
    closes = sorted(c["close_ts"] for c in cands)
    depth_days = (closes[-1] - closes[0]) / 86400.0
    depth_months = depth_days / 30.4375
    d_lo = dt.datetime.utcfromtimestamp(closes[0]).strftime("%Y-%m-%d")
    d_hi = dt.datetime.utcfromtimestamp(closes[-1]).strftime("%Y-%m-%d")
    print(f"      DEPTH: {d_lo} -> {d_hi}  ({depth_months:.2f} months)", flush=True)

    # candle budget: prioritise volume (liquidity -> tradeable reference)
    cands.sort(key=lambda x: x["volume"], reverse=True)
    sampled = cands[: CFG["candle_budget"]]
    print(f"[3/5] Reference price (~{CFG['ref_offset_h']}h pre-close) for {len(sampled)} mkts...", flush=True)

    obs = []  # (implied, y, event, series)
    calibrated = 0
    for i, c in enumerate(sampled):
        ref = reference_price(c)
        if ref is None: continue
        row = (ref, c["y"], c["event"], c["series"])
        obs.append(row)
        c_close_lookup[row] = c["close_ts"]
        calibrated += 1
        if (i+1) % 150 == 0:
            print(f"  ...ref {i+1}/{len(sampled)}, {calibrated} calibratable", flush=True)
    print(f"      {calibrated} calibratable markets", flush=True)

    # Full calibration table (context)
    full_table = calib_table(obs)

    # Primary bin
    plo, phi = CFG["primary_bin"]
    bin_rows = [o for o in obs if plo <= o[0] < phi]
    bin_rows.sort(key=lambda o: c_close_lookup[o])
    nbin = len(bin_rows)

    # Chronological split (older=estimation, newer=holdout)
    mid = nbin // 2
    est = bin_rows[:mid]; hold = bin_rows[mid:]

    def summ(rows):
        if not rows: return {"n":0}
        impl = sum(r[0] for r in rows)/len(rows); real = sum(r[1] for r in rows)/len(rows)
        return {"n":len(rows), "events":len({r[2] for r in rows}), "series":len({r[3] for r in rows}),
                "implied":round(impl,4), "realized":round(real,4), "gap":round(real-impl,4)}

    est_s, hold_s = summ(est), summ(hold)

    # Dual cluster bootstrap on ESTIMATION only
    est_ci_ev = cluster_boot_ci(est, 2, CFG["n_boot"], rng) if len(est) >= 4 else None
    est_ci_ser = cluster_boot_ci(est, 3, CFG["n_boot"], rng) if len(est) >= 4 else None

    # Per-series decomposition of FULL bin (gap*n mass concentration)
    by_ser = defaultdict(list)
    for r in bin_rows: by_ser[r[3]].append(r)
    ser_decomp = []
    for ser, rows in by_ser.items():
        n = len(rows); impl = sum(x[0] for x in rows)/n; real = sum(x[1] for x in rows)/n
        ser_decomp.append({"series": ser, "n": n, "events": len({x[2] for x in rows}),
                           "implied": round(impl,4), "realized": round(real,4),
                           "gap": round(real-impl,4), "gap_mass": round((real-impl)*n,4)})
    ser_decomp.sort(key=lambda d: d["gap_mass"])  # most-negative (biggest fade) first
    total_neg_mass = sum(d["gap_mass"] for d in ser_decomp if d["gap_mass"] < 0)
    top_share = (ser_decomp[0]["gap_mass"]/total_neg_mass) if (ser_decomp and total_neg_mass) else None
    same_sign_series = sum(1 for d in ser_decomp if d["gap"] < 0)

    # Fee-adjusted PnL sim (full bin + estimation + holdout)
    pnl_full = pnl_sim(bin_rows, CFG["fee_rate"])
    pnl_est = pnl_sim(est, CFG["fee_rate"])
    pnl_hold = pnl_sim(hold, CFG["fee_rate"])

    # VERDICT logic (pre-registered gates)
    depth_ok = depth_months >= CFG["depth_months_gate"]
    n_ok = est_s.get("n", 0) >= CFG["min_estimation_bin_n"]
    if not depth_ok or not n_ok:
        verdict = "INSUFFICIENT"
    else:
        dual_ok = (est_ci_ev and (est_ci_ev[0] > 0 or est_ci_ev[1] < 0) and
                   est_ci_ser and (est_ci_ser[0] > 0 or est_ci_ser[1] < 0))
        not_quirk = (top_share is not None and top_share < 0.5 and same_sign_series >= 4)
        holdout_ok = (hold_s.get("gap", 0) < 0 and pnl_hold and pnl_hold["ev_per_contract"] > 0)
        verdict = "KEEP" if (dual_ok and not_quirk and holdout_ok) else "KILL"

    out = {
        "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": {k: v for k, v in CFG.items() if k != "scratch"},
        "depth": {"from": d_lo, "to": d_hi, "months": round(depth_months,2),
                  "gate_months": CFG["depth_months_gate"], "pass": depth_ok},
        "n_settled": len(cands), "n_calibratable": calibrated,
        "full_calibration": full_table,
        "primary_bin": f"[{plo},{phi})",
        "bin_full": summ(bin_rows),
        "estimation": est_s, "holdout": hold_s,
        "estimation_n_gate": CFG["min_estimation_bin_n"], "estimation_n_pass": n_ok,
        "est_ci_event": [round(x,4) for x in est_ci_ev] if est_ci_ev else None,
        "est_ci_series": [round(x,4) for x in est_ci_ser] if est_ci_ser else None,
        "series_decomposition": ser_decomp,
        "top_series_neg_mass_share": round(top_share,3) if top_share is not None else None,
        "same_sign_series": same_sign_series,
        "pnl_full": pnl_full, "pnl_estimation": pnl_est, "pnl_holdout": pnl_hold,
        "verdict": verdict,
    }
    jp = f"{CFG['scratch']}/econ_longshot_fade_audit.json"
    with open(jp, "w") as f: json.dump(out, f, indent=2)

    # stdout
    print("\n" + "="*72)
    print(f"DEPTH {d_lo}->{d_hi} ({depth_months:.2f}mo, gate {CFG['depth_months_gate']}mo) "
          f"pass={depth_ok}")
    print(f"settled={len(cands)} calibratable={calibrated}")
    print(f"\nFULL Economics calibration:")
    print(f"  {'bin':13s}{'n':>5s}{'ev':>5s}{'ser':>5s}{'impl':>7s}{'real':>7s}{'gap':>8s}")
    for r in full_table:
        print(f"  {r['bin']:13s}{r['n']:5d}{r['events']:5d}{r['series']:5d}"
              f"{r['implied']:7.3f}{r['realized']:7.3f}{r['gap']:+8.3f}")
    print(f"\nPRIMARY BIN [{plo},{phi}) — full n={bin_full_n(out)}  "
          f"est n={est_s.get('n')} (gate {CFG['min_estimation_bin_n']} pass={n_ok})  "
          f"hold n={hold_s.get('n')}")
    print(f"  estimation: {est_s}")
    print(f"  holdout:    {hold_s}")
    print(f"  est CI(event)={out['est_ci_event']} CI(series)={out['est_ci_series']}")
    print(f"\nPER-SERIES decomposition (most-negative gap_mass first):")
    print(f"  {'series':22s}{'n':>4s}{'ev':>4s}{'impl':>7s}{'real':>7s}{'gap':>8s}{'mass':>8s}")
    for d in ser_decomp:
        print(f"  {d['series']:22s}{d['n']:4d}{d['events']:4d}{d['implied']:7.3f}"
              f"{d['realized']:7.3f}{d['gap']:+8.3f}{d['gap_mass']:+8.3f}")
    print(f"  top-series share of negative mass: {out['top_series_neg_mass_share']}, "
          f"same-sign(neg) series: {same_sign_series}")
    print(f"\nFEE-ADJUSTED NO-BUY PnL (event-capped):")
    print(f"  full:       {pnl_full}")
    print(f"  estimation: {pnl_est}")
    print(f"  holdout:    {pnl_hold}")
    print(f"\n>>> VERDICT: {verdict}")
    print(f"JSON: {jp}")


def bin_full_n(out): return out["bin_full"].get("n", 0)


if __name__ == "__main__":
    main()
