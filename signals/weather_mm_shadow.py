#!/usr/bin/env python3
"""
weather_mm_shadow.py — adverse-selection shadow validation for weather
market-making (the first node of the reward-harvesting engine).

The question the reward-engine scope leaves open: on a weather reward market, does
the liquidity REWARD outweigh the PICK-OFF LOSS from forecast updates moving fair
value past your stale quote? This computes, per live weather reward market:

  reward $/day   — your est. share of the market's reward pool (you post the only
                   near-mid depth in a thin/uncontested book)
  pickoff $/day  — expected loss from getting lifted after a forecast revision:
                   forecasts update ~N/day; each revision shifts the model fair
                   value P by sensitivity(dP/°F) × revision_temp_std; you're picked
                   off when that move exceeds your half-spread (maxSpread/2).
  NET = reward − pickoff  → verdict SAFE / MARGINAL / PICKOFF-RISK

Reuses Polyclawd's weather ensemble (calibrated prob + forecast std) + scanner
parsers + the Polymarket rewards API. SHADOW ONLY — no quotes posted, no trading.

Writes static/weather_mm_shadow.json (served by nginx) for the weather dashboard.

Usage: python3 -m signals.weather_mm_shadow [--min-rate 25] [--share 0.5] [--size 50]
"""

from __future__ import annotations
from config.polymarket_urls import clob_url  # polyproxy: central URL config

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals import weather_ensemble as we  # calibrated prob + forecast std
from signals import weather_scanner as ws    # title parsers + CITY_COORDS

from config.polymarket_urls import GAMMA_API as GAMMA  # polyproxy: central URL config
MULTI = clob_url("/rewards/markets/multi")
UA = {"User-Agent": "Mozilla/5.0 polyclawd-weather-mm/1.0"}
OUT = Path(__file__).resolve().parent.parent / "static" / "weather_mm_shadow.json"

FORECAST_UPDATES_PER_DAY = 4          # open-meteo / NWS refresh cadence
REVISION_FRACTION = 0.30              # each update revises ~30% of forecast-error std
                                      # (ASSUMPTION — recalibrate from forecast history)


def _get(url, params=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _reward_rate(m):
    return sum(float(c.get("rate_per_day") or 0) for c in (m.get("rewards_config") or []))


def _eligible_share(token_id, max_spread_c, size_shares):
    """Observed reward-pool share = your size / (existing eligible-band depth +
    your size). Eligible band = within max_spread (cents) of the book mid, both
    sides. Grounds the share in REAL competition instead of a flat guess."""
    book = _get(clob_url("/book"), {"token_id": token_id})
    if not book or "bids" not in book:
        return None, None
    bids = sorted([(float(b["price"]), float(b["size"])) for b in book.get("bids", [])], key=lambda x: -x[0])
    asks = sorted([(float(a["price"]), float(a["size"])) for a in book.get("asks", [])], key=lambda x: x[0])
    if not bids or not asks:
        return None, None
    mid = (bids[0][0] + asks[0][0]) / 2
    band = (max_spread_c or 4.5) / 100.0
    elig = (sum(sz for pr, sz in bids if pr >= mid - band) +
            sum(sz for pr, sz in asks if pr <= mid + band))
    return size_shares / (elig + size_shares), elig


def _parse_threshold(title: str):
    """Return (threshold_f, direction) from a weather market title.
    direction in {'above','below'}; converts °C→°F if the title uses C."""
    t = title.lower()
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*°?\s*([cf])", t)
    if not m:
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:degrees?)?", t)
        if not m:
            return None, None
        val, unit = float(m.group(1)), "f"
    else:
        val, unit = float(m.group(1)), m.group(2)
    thr_f = we._c_to_f(val) if unit == "c" else val
    direction = "below" if any(w in t for w in ("or lower", "below", "less than", "under")) else "above"
    return thr_f, direction


def _model_prob(city: str, date: str, thr_f: float, direction: str):
    fn = we.prob_below if direction == "below" else we.prob_above
    return fn(city, date, thr_f)


def _sensitivity(city, date, thr_f, direction, p0):
    """dP per 1°F near the threshold (how fast fair value moves with temp)."""
    r = _model_prob(city, date, thr_f + 1.0, direction)
    if not r or "probability" not in r:
        return None
    return abs(r["probability"] - p0)


def analyze(min_rate: float, share: float, size_shares: float):
    # 1. weather reward markets (rate >= min_rate)
    rw = _get(MULTI, {"tag_slug": "weather", "order_by": "rate_per_day",
                      "position": "DESC", "page_size": 200}) or {}
    rewarded = {}
    for m in rw.get("data") or []:
        rt = _reward_rate(m)
        if rt >= min_rate:
            rewarded[(m.get("question") or "").strip().lower()] = rt

    # 2. live weather markets w/ prices, matched to reward rate
    rows = []
    evs = _get(f"{GAMMA}/events", {"closed": "false", "active": "true",
                                   "limit": 300, "tag_slug": "weather"}) or []
    seen = set()
    for ev in evs if isinstance(evs, list) else []:
        for mk in ev.get("markets", []) or []:
            q = (mk.get("question") or "").strip()
            rate = rewarded.get(q.lower())
            if rate is None or q.lower() in seen:
                continue
            city = ws._extract_city_from_market(q)
            date = ws._extract_date_from_market(q)
            thr_f, direction = _parse_threshold(q)
            if not (city and date and thr_f is not None):
                continue
            seen.add(q.lower())
            mp = _model_prob(city, date, thr_f, direction)
            if not mp or mp.get("probability") is None:
                continue
            p = mp["probability"]
            std_f = mp.get("forecast_std_f") or 3.0
            sens = _sensitivity(city, date, thr_f, direction, p)
            if sens is None:
                continue
            # poly price (mid from outcomePrices)
            try:
                prices = json.loads(mk.get("outcomePrices") or "[]")
                poly = float(prices[0]) if prices else None
            except Exception:
                poly = None

            # --- pick-off model ---
            revision_temp_std = REVISION_FRACTION * std_f          # °F moved per update
            p_move_std = sens * revision_temp_std                   # fair-value move per update
            half_spread = (mk.get("rewardsMaxSpread") or 4.5) / 2 / 100.0  # $ (e.g. 0.0225)
            # P(an update moves fair value past your half-spread, adverse side)
            if p_move_std <= 1e-9:
                pickoff_prob = 0.0
                exp_loss_per = 0.0
            else:
                z = half_spread / p_move_std
                pickoff_prob = max(0.0, 1 - we._norm_cdf(z))        # one adverse tail
                exp_loss_per = p_move_std                            # ~magnitude beyond spread
            pickoff_day = (FORECAST_UPDATES_PER_DAY * pickoff_prob
                           * exp_loss_per * size_shares)            # $ (shares × $move)
            # observed per-market share from EXISTING eligible-band depth (grounds
            # the 50% guess: bands are contested, real share ~10%, not 50%)
            try:
                _toks = json.loads(mk.get("clobTokenIds") or "[]")
            except Exception:
                _toks = []
            obs_share, elig_depth = (_eligible_share(_toks[0], mk.get("rewardsMaxSpread") or 4.5, size_shares)
                                     if _toks else (None, None))
            if obs_share is None:
                obs_share = share                                   # fallback to flat
            reward_day = rate * obs_share                            # OBSERVED share
            net = reward_day - pickoff_day
            verdict = ("SAFE" if net > 0.5 * reward_day else
                       "MARGINAL" if net > 0 else "PICKOFF-RISK")
            rows.append({
                "market": q, "city": city, "date": date,
                "threshold_f": round(thr_f, 1), "direction": direction,
                "model_p": round(p, 3), "poly_price": round(poly, 3) if poly else None,
                "edge": round(p - poly, 3) if poly else None,
                "forecast_std_f": round(std_f, 1),
                "sensitivity_dP_per_F": round(sens, 3),
                "observed_share": round(obs_share, 3),
                "eligible_depth_sh": round(elig_depth) if elig_depth is not None else None,
                "reward_day": round(reward_day, 2),
                "pickoff_day": round(pickoff_day, 2),
                "net_day": round(net, 2),
                "verdict": verdict,
            })
    rows.sort(key=lambda r: -r["net_day"])
    return rows


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-rate", type=float, default=25.0)
    ap.add_argument("--share", type=float, default=0.5, help="your est. reward-pool share")
    ap.add_argument("--size", type=float, default=50.0, help="posted size (shares)")
    args = ap.parse_args()

    rows = analyze(args.min_rate, args.share, args.size)
    n_safe = sum(1 for r in rows if r["verdict"] == "SAFE")
    tot_net = sum(r["net_day"] for r in rows)
    tot_reward = sum(r["reward_day"] for r in rows)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "assumptions": {"share": round(__import__("statistics").median([r["observed_share"] for r in rows]) if rows else args.share, 3),
                        "share_basis": "observed eligible-band depth (per-market)", "size_shares": args.size,
                        "updates_per_day": FORECAST_UPDATES_PER_DAY,
                        "revision_fraction": REVISION_FRACTION},
        "summary": {"markets": len(rows), "safe": n_safe,
                    "total_reward_day": round(tot_reward, 2),
                    "total_net_day": round(tot_net, 2)},
        "markets": rows,
    }
    OUT.write_text(json.dumps(payload, indent=2))

    print(f"weather MM shadow: {len(rows)} reward markets | SAFE {n_safe} | "
          f"reward ${tot_reward:.0f}/day  pickoff ${tot_reward-tot_net:.0f}/day  "
          f"NET ${tot_net:.0f}/day -> {OUT}")
    for r in rows[:12]:
        print(f"  [{r['verdict']:12}] ${r['net_day']:>6.2f}/day net "
              f"(rwd ${r['reward_day']:.1f} − pick ${r['pickoff_day']:.1f}) "
              f"p={r['model_p']} poly={r['poly_price']} | {r['market'][:46]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
