#!/usr/bin/env python3
"""
sum_to_one_arb_screen.py — Point-in-time opportunity-sizing screen for
sum-to-one mispricings on Polymarket, per Saguillo et al. (2025),
"Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets".

Taxonomy adjudicated (paper §Two forms of arbitrage):
  1. Market Rebalancing Arbitrage (intra-market): within a single binary
     condition, YES ask + NO ask < $1  => buy both, guaranteed $1 payout.
  2. Combinatorial Arbitrage (inter-market): a negRisk event whose outcomes
     are mutually exclusive + exhaustive.
       - LONG-ALL:  sum(YES ask over outcomes) < $1  => buy every YES, exactly
                    one resolves to $1.
       - SHORT-ALL: sum(NO ask over outcomes) < (n-1) => buy every NO, exactly
                    (n-1) resolve to $1.

House rule: price EVERYTHING off the executable CLOB order book
(best ask + depth at it). Never Gamma midpoints. Read-only. No keys. No orders.

Usage: python3 sum_to_one_arb_screen.py [--top 400] [--persist-wait 60]
"""
import argparse, json, time, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
SESS = requests.Session()
SESS.headers.update({"User-Agent": "polyclawd-arb-screen/1.0"})

# Friction assumptions (qualitative, applied to the guesstimate).
# Polymarket CLOB trading is gas-free (off-chain matching, on-chain settle via
# relayer); real friction is the maker/taker spread already in the book, the
# 2bps-ish effective tick granularity, and race/latency risk. We net a flat
# haircut to acknowledge partial fills + adverse selection.
CAPTURE_HAIRCUT = 0.5   # assume we capture 50% of theoretical edge after races
MIN_EDGE_BPS = 20       # ignore sub-20bps "violations" (tick noise / not worth wiring)
MIN_FILL_USD = 20       # ignore dust-depth violations


def get(url, **params):
    for _ in range(3):
        try:
            r = SESS.get(url, params=params or None, timeout=20)
            if r.status_code == 200:
                return r.json()
        except Exception:
            time.sleep(0.4)
    return None


def best_ask(token_id):
    """Return (best_ask_price, size_at_that_price, usd_depth) or None."""
    b = get(f"{CLOB}/book", token_id=token_id)
    if not b or not b.get("asks"):
        return None
    # asks come sorted descending by price; best (lowest) ask is the min level.
    lvl = min(b["asks"], key=lambda a: float(a["price"]))
    p = float(lvl["price"])
    sz = float(lvl["size"])
    return (p, sz, p * sz)


def fetch_top_markets(n):
    """Top-n active markets by liquidity, paginated."""
    out, offset = [], 0
    while len(out) < n:
        page = get(f"{GAMMA}/markets", closed=False, active=True,
                   order="liquidityNum", ascending=False, limit=100, offset=offset)
        if not page:
            break
        rows = page if isinstance(page, list) else page.get("data", [])
        if not rows:
            break
        out.extend(rows)
        offset += 100
        if len(rows) < 100:
            break
    return out[:n]


def fetch_negrisk_events(n):
    """Top negRisk (multi-outcome, mutually-exclusive) events by liquidity."""
    out, offset = [], 0
    while len(out) < n * 3 and offset < 900:
        page = get(f"{GAMMA}/events", closed=False, active=True,
                   order="liquidity", ascending=False, limit=100, offset=offset)
        if not page:
            break
        rows = page if isinstance(page, list) else page.get("data", [])
        if not rows:
            break
        out.extend([e for e in rows if e.get("negRisk")])
        offset += 100
        if len(rows) < 100:
            break
    return out[:n]


def parse_tokens(m):
    try:
        t = json.loads(m.get("clobTokenIds") or "[]")
        return t if len(t) == 2 else None
    except Exception:
        return None


# ---------- Scan 1: intra-market (Market Rebalancing) ----------
def scan_intra(markets):
    violations = []
    def check(m):
        toks = parse_tokens(m)
        if not toks:
            return None
        ya, na = best_ask(toks[0]), best_ask(toks[1])
        if not ya or not na:
            return None
        cost = ya[0] + na[0]
        if cost >= 1.0:
            return None
        edge_bps = (1.0 - cost) * 10000
        pairs = min(ya[1], na[1])          # shares fillable on the thinner side
        fill_usd = pairs * cost            # capital deployed
        return {
            "type": "intra_rebalance", "id": m.get("id"),
            "name": (m.get("question") or "")[:70],
            "yes_ask": round(ya[0], 4), "no_ask": round(na[0], 4),
            "sum": round(cost, 4), "edge_bps": round(edge_bps, 1),
            "fill_usd": round(fill_usd, 2), "tokens": toks,
        }
    with ThreadPoolExecutor(max_workers=16) as ex:
        for r in ex.map(check, markets):
            if r and r["edge_bps"] >= MIN_EDGE_BPS and r["fill_usd"] >= MIN_FILL_USD:
                violations.append(r)
    return violations


# ---------- Scan 2: combinatorial (negRisk long-all / short-all) ----------
def scan_combinatorial(events):
    violations = []
    for e in events:
        mkts = [m for m in e.get("markets", []) if m.get("active") and not m.get("closed")]
        toks = [(m, parse_tokens(m)) for m in mkts]
        toks = [(m, t) for m, t in toks if t]
        n = len(toks)
        if n < 2:
            continue
        # fetch YES + NO best asks for every outcome
        def yn(mt):
            m, t = mt
            return (best_ask(t[0]), best_ask(t[1]))
        with ThreadPoolExecutor(max_workers=16) as ex:
            books = list(ex.map(yn, toks))
        yes = [b[0] for b in books]
        no = [b[1] for b in books]
        if any(y is None for y in yes) or any(x is None for x in no):
            continue  # incomplete book -> not cleanly executable, skip
        # LONG-ALL: buy every YES, one pays $1
        sum_yes = sum(y[0] for y in yes)
        if sum_yes < 1.0:
            edge_bps = (1.0 - sum_yes) * 10000
            fill_usd = min(y[1] for y in yes) * sum_yes
            if edge_bps >= MIN_EDGE_BPS and fill_usd >= MIN_FILL_USD:
                violations.append({
                    "type": "combo_long_all", "id": e.get("id"),
                    "name": (e.get("title") or "")[:70], "n_outcomes": n,
                    "sum": round(sum_yes, 4), "edge_bps": round(edge_bps, 1),
                    "fill_usd": round(fill_usd, 2),
                    "tokens": [t[0] for _, t in toks],
                })
        # SHORT-ALL: buy every NO, (n-1) pay $1
        sum_no = sum(x[0] for x in no)
        if sum_no < (n - 1):
            edge_bps = ((n - 1) - sum_no) / (n - 1) * 10000
            fill_usd = min(x[1] for x in no) * sum_no
            if edge_bps >= MIN_EDGE_BPS and fill_usd >= MIN_FILL_USD:
                violations.append({
                    "type": "combo_short_all", "id": e.get("id"),
                    "name": (e.get("title") or "")[:70], "n_outcomes": n,
                    "sum_no": round(sum_no, 4), "edge_bps": round(edge_bps, 1),
                    "fill_usd": round(fill_usd, 2),
                    "tokens": [t[1] for _, t in toks],
                })
    return violations


def recheck_persist(v):
    """Re-price the same violating legs; return True if still an arb."""
    toks = v["tokens"]
    if v["type"] == "intra_rebalance":
        ya, na = best_ask(toks[0]), best_ask(toks[1])
        if not ya or not na:
            return False
        return (ya[0] + na[0]) < 1.0
    if v["type"] == "combo_long_all":
        asks = [best_ask(t) for t in toks]
        if any(a is None for a in asks):
            return False
        return sum(a[0] for a in asks) < 1.0
    if v["type"] == "combo_short_all":
        asks = [best_ask(t) for t in toks]
        if any(a is None for a in asks):
            return False
        n = len(toks)
        return sum(a[0] for a in asks) < (n - 1)
    return False


def median(xs):
    xs = sorted(xs)
    if not xs:
        return 0.0
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=400)
    ap.add_argument("--neg-events", type=int, default=120)
    ap.add_argument("--persist-wait", type=int, default=60)
    args = ap.parse_args()

    t0 = time.time()
    print(f"[{datetime.now(timezone.utc).isoformat()}] fetching universe...", file=sys.stderr)
    markets = fetch_top_markets(args.top)
    events = fetch_negrisk_events(args.neg_events)
    n_combo_sets = sum(1 for e in events
                       if len([m for m in e.get("markets", [])
                               if m.get("active") and not m.get("closed")]) >= 2)
    print(f"  {len(markets)} markets, {len(events)} negRisk events "
          f"({n_combo_sets} usable combo sets)", file=sys.stderr)

    print("scanning intra-market...", file=sys.stderr)
    v_intra = scan_intra(markets)
    print(f"  {len(v_intra)} intra violations", file=sys.stderr)

    print("scanning combinatorial...", file=sys.stderr)
    v_combo = scan_combinatorial(events)
    print(f"  {len(v_combo)} combo violations", file=sys.stderr)

    all_v = v_intra + v_combo
    # persistence re-check after wait
    persist = {}
    if all_v:
        print(f"waiting {args.persist_wait}s for persistence re-check...", file=sys.stderr)
        time.sleep(args.persist_wait)
        for v in all_v:
            persist[id(v)] = recheck_persist(v)

    # ---- report ----
    scanned_sets = len(markets) + n_combo_sets
    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": round(time.time() - t0, 1),
        "universe": {"markets": len(markets), "negrisk_events": len(events),
                     "combo_sets": n_combo_sets, "total_sets_scanned": scanned_sets},
        "violations": all_v,
        "by_type": {},
        "persistence": {},
    }
    for typ in ("intra_rebalance", "combo_long_all", "combo_short_all"):
        vs = [v for v in all_v if v["type"] == typ]
        surv = [v for v in vs if persist.get(id(v))]
        out["by_type"][typ] = {
            "count": len(vs),
            "median_bps": round(median([v["edge_bps"] for v in vs]), 1),
            "median_fill_usd": round(median([v["fill_usd"] for v in vs]), 2),
            "max_fill_usd": round(max([v["fill_usd"] for v in vs], default=0), 2),
            "persist_rate": round(len(surv) / len(vs), 2) if vs else None,
        }
    n_persist = sum(1 for v in all_v if persist.get(id(v)))
    out["persistence"] = {"survived": n_persist, "total": len(all_v),
                          "rate": round(n_persist / len(all_v), 2) if all_v else None}

    # crude annualized guesstimate from surviving violations
    surv_v = [v for v in all_v if persist.get(id(v))]
    per_hit_profit = median([v["fill_usd"] * (v["edge_bps"] / 10000) for v in surv_v]) if surv_v else 0.0
    out["guesstimate"] = {
        "surviving_violations": len(surv_v),
        "median_theoretical_profit_per_hit_usd": round(per_hit_profit, 2),
        "capture_haircut": CAPTURE_HAIRCUT,
        "note": "annualized = surviving_hits_per_snapshot * refresh_rate * "
                "median_profit * capture_haircut; extrapolate with caution "
                "(single snapshot, no gas but latency/race risk).",
    }

    ts = datetime.now().strftime("%Y-%m-%d")
    outpath = f"/Users/ffv_macmini/Desktop/polyclawd/research/sum_to_one_arb_screen_results_{ts}.json"
    with open(outpath, "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps({k: out[k] for k in
                      ("universe", "by_type", "persistence", "guesstimate", "elapsed_s")},
                     indent=2))
    print(f"\nfull results -> {outpath}", file=sys.stderr)


if __name__ == "__main__":
    main()
