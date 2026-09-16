#!/usr/bin/env python3
"""
btc_threshold_options_rv.py  —  Point-in-time replication of Portnaya (2026),
"Do Prediction Markets Match Option Prices? Bitcoin Threshold Evidence from
Binance and Polymarket" (arXiv:2606.19517).

QUESTION: Does the paper's persistent ~6pp wedge (Polymarket Yes price above
the option-implied risk-neutral binary value) still exist mid-July 2026?

PAPER METHOD (Sec 2-3):
  P_fair,t = e^{-r*tau} * Phi(d2)   (discounted cash-or-nothing call)
  where sigma is inverted from a listed vanilla call on the SAME strike/maturity.
  Gap  D_t = P_poly,t - P_fair,t.   Positive D = Polymarket RICH.
  Paper: main mkt +5.6pp (N=214); pooled Binance +6.3pp (N=287); Deribit +11pp.

THIS REPLICATION (read-only, no keys, no orders):
  - Venues: Polymarket (Gamma discovery + CLOB executable book) vs DERIBIT options.
    Binance eapi (eapi.binance.com) is GEO-BLOCKED from this location
    ("Service unavailable from a restricted location"), so we fall back to
    Deribit — same fallback the paper itself runs (its Deribit extension: +11pp).
  - Estimator: interpolate the Deribit call IV smile to the exact threshold K
    (linear in strike), then Black-76 d2 with the per-expiry forward F
    (Deribit underlying_price), r=0 (short horizon, USD-quoted). This is the
    paper's Phi(d2) estimator; it uses the smile IV at K but omits the skew-slope
    digital correction (call-spread term) -- same simplification as the paper.
  - Expiry mismatch: PM markets resolve 16:00 UTC on date D; Deribit expires
    08:00 UTC. When the resolution time falls between two listed expiries we
    compute P_fair at each bracketing expiry and linearly interpolate in
    calendar time. Flagged per row.
  - Executable-price house rule: we record the CLOB top-of-book bid/ask, not the
    midpoint. Rich Yes is monetised by SELLING at the BID; cheap Yes by BUYING at
    the ASK. The executable gap crosses the PM spread.

Scope: single point-in-time snapshot, NOT a time series.
"""

import math
import time
import datetime as dt
import statistics
import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"
DERIBIT = "https://www.deribit.com/api/v2/public/"
R = 0.0            # risk-free (crypto USD, sub-week horizon) -> discount ~1
TIMEOUT = 25
NDCDF = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ----------------------------------------------------------------------------- #
# 1. Polymarket discovery
# ----------------------------------------------------------------------------- #
def fetch_pm_threshold_markets():
    """Return list of dicts: BTC 'above $K on <date>' markets with executable book."""
    seen, out = set(), []
    for q in ["bitcoin above", "bitcoin above july"]:
        r = requests.get(f"{GAMMA}/public-search",
                         params={"q": q, "limit_per_type": 40,
                                 "events_status": "active"}, timeout=TIMEOUT)
        for ev in r.json().get("events", []):
            title = ev.get("title", "")
            if "bitcoin above" not in title.lower():
                continue
            end = ev.get("endDate")  # resolution datetime (UTC)
            for m in ev.get("markets", []):
                cid = m.get("conditionId")
                if cid in seen:
                    continue
                seen.add(cid)
                git = (m.get("groupItemTitle") or "").replace(",", "").replace("$", "")
                try:
                    K = float(git)
                except (TypeError, ValueError):
                    continue
                import json
                try:
                    toks = json.loads(m.get("clobTokenIds") or "[]")
                except json.JSONDecodeError:
                    toks = []
                if len(toks) != 2:
                    continue
                out.append({
                    "event": title,
                    "K": K,
                    "resolve": m.get("endDate") or end,
                    "yes_token": toks[0],
                    "volume": float(m.get("volume") or 0.0),
                })
    return out


def clob_top_of_book(token_id):
    """Executable top-of-book for a CLOB token. Returns (best_bid, best_ask)."""
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=TIMEOUT)
        b = r.json()
        bids = b.get("bids") or []
        asks = b.get("asks") or []
        # CLOB returns price-ascending; best bid = highest, best ask = lowest.
        best_bid = max((float(x["price"]) for x in bids), default=None)
        best_ask = min((float(x["price"]) for x in asks), default=None)
        return best_bid, best_ask
    except Exception:
        return None, None


# ----------------------------------------------------------------------------- #
# 2. Deribit option chain -> IV smile per expiry
# ----------------------------------------------------------------------------- #
def fetch_deribit_smiles():
    """
    Return {expiry_ts_ms: {'F': forward, 'calls': [(K, iv_frac), ...]}}
    using book_summary (mark_iv, underlying_price) for all live BTC calls.
    """
    ins = requests.get(DERIBIT + "get_instruments",
                       params={"currency": "BTC", "kind": "option",
                               "expired": "false"}, timeout=TIMEOUT).json()["result"]
    call_meta = {i["instrument_name"]: (i["expiration_timestamp"], i["strike"])
                 for i in ins if i["option_type"] == "call"}
    summ = requests.get(DERIBIT + "get_book_summary_by_currency",
                        params={"currency": "BTC", "kind": "option"},
                        timeout=TIMEOUT).json()["result"]
    smiles = {}
    for s in summ:
        name = s["instrument_name"]
        if name not in call_meta:
            continue
        exp, K = call_meta[name]
        iv = s.get("mark_iv")
        F = s.get("underlying_price")
        if iv is None or F is None or iv <= 0:
            continue
        smiles.setdefault(exp, {"F": F, "calls": []})
        smiles[exp]["calls"].append((K, iv / 100.0))
        smiles[exp]["F"] = F  # per-expiry forward
    for e in smiles:
        smiles[e]["calls"].sort()
    return smiles


def interp_iv(calls, K):
    """Linear-in-strike IV interpolation; clamp/flat-extrapolate at wings."""
    ks = [c[0] for c in calls]
    vs = [c[1] for c in calls]
    if K <= ks[0]:
        return vs[0]
    if K >= ks[-1]:
        return vs[-1]
    for i in range(1, len(ks)):
        if ks[i] >= K:
            w = (K - ks[i - 1]) / (ks[i] - ks[i - 1])
            return vs[i - 1] + w * (vs[i] - vs[i - 1])
    return vs[-1]


def digital_call(F, K, sigma, tau):
    """Black-76 cash-or-nothing call = e^{-r tau} Phi(d2).  P(S_T > K) risk-neutral."""
    if tau <= 0 or sigma <= 0:
        return 1.0 if F > K else 0.0
    d2 = (math.log(F / K) - 0.5 * sigma * sigma * tau) / (sigma * math.sqrt(tau))
    return math.exp(-R * tau) * NDCDF(d2)


def p_fair_at_expiry(smile, K, tau):
    sigma = interp_iv(smile["calls"], K)
    return digital_call(smile["F"], K, sigma, tau), sigma


def option_implied_prob(smiles, K, resolve_ts_ms, now_ms):
    """
    P_fair for threshold K at resolution time. Interpolate in calendar time
    between the two Deribit expiries bracketing the resolution timestamp.
    Returns (p_fair, tag) or (None, reason).
    """
    exps = sorted(smiles)
    yr = 365.0 * 86400_000.0
    below = [e for e in exps if e <= resolve_ts_ms]
    above = [e for e in exps if e >= resolve_ts_ms]
    if above and below and below[-1] != above[0]:
        e1, e2 = below[-1], above[0]
        p1, s1 = p_fair_at_expiry(smiles[e1], K, (e1 - now_ms) / yr)
        p2, s2 = p_fair_at_expiry(smiles[e2], K, (e2 - now_ms) / yr)
        w = (resolve_ts_ms - e1) / (e2 - e1)
        p = p1 + w * (p2 - p1)
        return p, f"time-interp {_d(e1)}<->{_d(e2)} (iv~{s1*100:.0f}/{s2*100:.0f}%)"
    e = min(exps, key=lambda x: abs(x - resolve_ts_ms))
    tau = (e - now_ms) / yr
    if tau <= 0:
        return None, "nearest Deribit expiry already passed"
    p, s = p_fair_at_expiry(smiles[e], K, tau)
    dd = abs(e - resolve_ts_ms) / 3600_000.0
    return p, f"nearest {_d(e)} (dt={dd:.0f}h, iv~{s*100:.0f}%)"


def _d(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000, dt.timezone.utc).strftime("%d%b %H:%M")


# ----------------------------------------------------------------------------- #
# 3. Main
# ----------------------------------------------------------------------------- #
def main():
    now_ms = int(time.time() * 1000)
    print("=" * 100)
    print("BTC THRESHOLD:  Polymarket  vs  Deribit option-implied  (Binance eapi geo-blocked)")
    print("Snapshot:", dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    print("=" * 100)

    smiles = fetch_deribit_smiles()
    print(f"Deribit live BTC option expiries: {', '.join(_d(e) for e in sorted(smiles))}")
    mkts = fetch_pm_threshold_markets()
    print(f"Polymarket 'Bitcoin above ___' markets discovered: {len(mkts)}\n")

    hdr = (f"{'event':<26}{'K':>8}{'PMbid':>7}{'PMask':>7}{'PMmid':>7}"
           f"{'Pfair':>7}{'gap_mid':>8}{'exec_gap':>9}{'side':>6}  match")
    print(hdr); print("-" * len(hdr))

    rows = []
    for m in sorted(mkts, key=lambda x: (x["resolve"] or "", x["K"])):
        if not m["resolve"]:
            continue
        rt = dt.datetime.fromisoformat(m["resolve"].replace("Z", "+00:00"))
        resolve_ts = int(rt.timestamp() * 1000)
        if resolve_ts <= now_ms:
            continue  # already resolving/expired
        bid, ask = clob_top_of_book(m["yes_token"])
        if bid is None or ask is None:
            continue
        mid = 0.5 * (bid + ask)
        pf, tag = option_implied_prob(smiles, m["K"], resolve_ts, now_ms)
        if pf is None:
            continue
        # Informative sample only: two-sided book AND fair prob off the 0/1 bounds.
        if not (0.03 < pf < 0.97):
            continue
        if (ask - bid) > 0.20:  # book too wide to be meaningful
            continue
        gap_mid = mid - pf                      # paper convention D = Ppoly - Pfair
        # executable edge on the rich/cheap side (cross the PM spread):
        if gap_mid >= 0:                        # PM rich -> SELL Yes at bid
            exec_gap, side = bid - pf, "SELL"
        else:                                   # PM cheap -> BUY Yes at ask
            exec_gap, side = pf - ask, "BUY"
        ev = m["event"].replace("Bitcoin above ___ on ", "").replace("?", "")
        row = dict(event=ev, K=m["K"], bid=bid, ask=ask, mid=mid, pf=pf,
                   gap_mid=gap_mid, exec_gap=exec_gap, side=side, tag=tag,
                   spread=ask - bid)
        rows.append(row)
        print(f"{ev:<26}{m['K']:>8.0f}{bid:>7.3f}{ask:>7.3f}{mid:>7.3f}"
              f"{pf:>7.3f}{gap_mid*100:>+7.1f}p{exec_gap*100:>+8.1f}p{side:>6}  {tag}")

    if not rows:
        print("\nNo informative matched pairs (all markets at 0/1 bounds or unmatched).")
        print("VERDICT: INCONCLUSIVE — too few near-the-money threshold/expiry pairs.")
        return

    g = [r["gap_mid"] * 100 for r in rows]
    eg = [r["exec_gap"] * 100 for r in rows]
    sp = [r["spread"] * 100 for r in rows]
    g_sorted = sorted(g)
    q1 = g_sorted[len(g)//4]; q3 = g_sorted[(3*len(g))//4]
    print("\n" + "=" * 100)
    print(f"N matched informative pairs: {len(rows)}")
    print(f"GAP vs MID (D = P_poly - P_fair):  mean {statistics.mean(g):+.1f}pp   "
          f"median {statistics.median(g):+.1f}pp   IQR [{q1:+.1f}, {q3:+.1f}]pp")
    print(f"EXECUTABLE gap (after crossing PM spread):  mean {statistics.mean(eg):+.1f}pp   "
          f"median {statistics.median(eg):+.1f}pp")
    print(f"PM spread cost:  mean {statistics.mean(sp):.1f}pp   median {statistics.median(sp):.1f}pp")
    n_rich = sum(1 for r in rows if r['gap_mid'] > 0)
    print(f"Direction: {n_rich}/{len(rows)} markets have Polymarket RICH (Yes above fair)")
    print(f"Paper benchmark: +6.3pp pooled (Binance), +11pp (Deribit). "
          f"Positive here => same sign (PM rich).")
    n_exec = sum(1 for r in rows if r['exec_gap'] > 0)
    print(f"Executable after spread: {n_exec}/{len(rows)} pairs keep a positive edge "
          f"crossing the PM book.")
    print("=" * 100)


if __name__ == "__main__":
    main()
