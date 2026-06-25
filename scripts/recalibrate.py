# scripts/recalibrate.py
"""Weekly champion/challenger calibration bake-off (design Phase 1).

Pure function `run_bakeoff(rows, champion)` is unit-tested; `main()` wires it to
the live DB, the champion store, the ledger, and the alert hook. Never fits
in-sample; promotion is gated on held-out Brier-skill-vs-market with a sample floor.
"""

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signals.calibration_core import (  # noqa: E402
    time_forward_split,
    fit_isotonic,
    apply_isotonic,
    brier_skill_score,
    bootstrap_ece_ci,
)
from signals.empirical_confidence import calibrated_confidence_oos, classify_archetype  # noqa: E402
from signals.calibration_champion import (  # noqa: E402
    load_champion,
    save_champion,
    append_ledger,
    _alert,
    LEDGER_PATH,
)

DB = Path(__file__).resolve().parent.parent / "storage" / "shadow_trades.db"

MIN_TEST = 30  # sample-size floor for any promotion (design §9B)
PROMOTE_MARGIN = 0.02  # challenger must beat champion skill by >= this
COOLDOWN_RUNS = 2  # hysteresis: runs to wait after a promotion (§9D)


def _skill_of(maps, test, train):
    """Brier-skill-vs-market of a champion's maps over the test slice."""
    preds, outs, base = [], [], []
    for r in test:
        arch = classify_archetype(r["title"])
        raw = calibrated_confidence_oos(r["title"], r["side"], float(r["price"]), train) / 100.0
        model = maps.get(arch)
        preds.append(apply_isotonic(model, raw) if model else raw)
        outs.append(int(r["won"]))
        base.append(float(r["price"]))  # market-implied prob = baseline
    return brier_skill_score(preds, outs, base), preds, outs


def _fit_challenger_maps(train):
    """Fit one isotonic map per archetype that has enough own train samples."""
    by_arch = defaultdict(list)
    for r in train:
        by_arch[classify_archetype(r["title"])].append(r)
    maps = {}
    for arch, rows in by_arch.items():
        if len(rows) < 20:
            continue
        raw = [calibrated_confidence_oos(r["title"], r["side"], float(r["price"]), train) / 100.0 for r in rows]
        won = [int(r["won"]) for r in rows]
        maps[arch] = fit_isotonic(raw, won)
    return maps


def run_bakeoff(rows: list, champion: dict, runs_since_promo: int = COOLDOWN_RUNS) -> dict:
    """Pure decision function. Returns a ledger-ready record. Never writes."""
    train, test = time_forward_split(rows, key="timestamp", train_frac=0.7)
    if len(test) < MIN_TEST:
        return {
            "decision": "skip_insufficient",
            "promoted": False,
            "n_test": len(test),
            "champion_skill": None,
            "challenger_skill": None,
            "ece_ci": None,
            "margin": None,
            "new_champion": champion,
        }

    champ_skill, _, _ = _skill_of(champion.get("maps", {}), test, train)
    chal_maps = _fit_challenger_maps(train)
    chal_skill, chal_preds, chal_outs = _skill_of(chal_maps, test, train)
    lo, hi = bootstrap_ece_ci(chal_preds, chal_outs, n_boot=2000, seed=0)
    margin = round(chal_skill - champ_skill, 4)

    # Gate on the point skill estimate. The bootstrap ECE CI (lo,hi) is stored as a
    # DIAGNOSTIC only (ledger/meta) — it is a CI of ECE, not of skill, so it can't gate
    # promotion. A true skill-CI lower-bound gate is deferred to Phase 2.
    promote = (
        margin >= PROMOTE_MARGIN and len(test) >= MIN_TEST and chal_skill > 0 and runs_since_promo >= COOLDOWN_RUNS
    )
    new_champion = champion
    if promote:
        new_champion = {
            "version": champion.get("version", 0) + 1,
            "maps": chal_maps,
            "meta": {"n_test": len(test), "skill": chal_skill, "ece_ci": [lo, hi]},
        }
    return {
        "decision": "promote" if promote else "keep",
        "promoted": promote,
        "n_test": len(test),
        "champion_skill": champ_skill,
        "challenger_skill": chal_skill,
        "ece_ci": [lo, hi],
        "margin": margin,
        "new_champion": new_champion,
    }


def _load_rows():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = [
        dict(r)
        for r in con.execute(
            "SELECT market AS title, side, entry_price AS price, "
            "CASE WHEN pnl>0 THEN 1 ELSE 0 END AS won, timestamp "
            "FROM shadow_trades WHERE resolved=1 AND entry_price IS NOT NULL ORDER BY timestamp"
        )
    ]
    con.close()
    return rows


def _runs_since_promo():
    if not LEDGER_PATH.exists():
        return COOLDOWN_RUNS
    runs = 0
    for line in reversed(LEDGER_PATH.read_text().splitlines()):
        try:
            if json.loads(line).get("promoted"):
                break
        except Exception:
            continue
        runs += 1
    return runs


def main():
    try:
        rows = _load_rows()
        champion = load_champion()
        res = run_bakeoff(rows, champion, runs_since_promo=_runs_since_promo())
        ledger_entry = {k: v for k, v in res.items() if k != "new_champion"}
        append_ledger(ledger_entry)
        if res["promoted"]:
            save_champion(res["new_champion"])
            _alert(
                f"PROMOTED champion v{res['new_champion']['version']} "
                f"skill={res['challenger_skill']} margin={res['margin']} n={res['n_test']}"
            )
        print(
            f"recalibrate: {res['decision']} | champ_skill={res['champion_skill']} "
            f"chal_skill={res['challenger_skill']} margin={res['margin']} n_test={res['n_test']}"
        )
    except Exception as e:
        _alert(f"BAKEOFF FAILED: {e}")
        raise


if __name__ == "__main__":
    main()
