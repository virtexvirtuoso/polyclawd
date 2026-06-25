# signals/calibration_champion.py
"""Champion calibration store + ledger + alert hook for the self-improvement loop.

Resilience contract (design §9C): a missing or corrupt champion NEVER raises and
NEVER changes live behavior — it degrades to the identity map (apply no correction).
"""

import copy
import json
import logging
from pathlib import Path
from typing import Dict, Optional

from signals.calibration_core import apply_isotonic

logger = logging.getLogger("calibration_champion")

_STORAGE = Path(__file__).resolve().parent.parent / "storage"
CHAMPION_PATH = _STORAGE / "calibration_champion.json"
LEDGER_PATH = _STORAGE / "calibration_ledger.jsonl"

# version 0, no per-archetype maps -> apply_champion is a pure passthrough.
IDENTITY_CHAMPION: Dict = {"version": 0, "maps": {}, "meta": {"identity": True}}


def load_champion(path: Path = CHAMPION_PATH) -> Dict:
    """Load the champion; return IDENTITY_CHAMPION on any failure (missing/corrupt)."""
    try:
        if not Path(path).exists():
            return copy.deepcopy(IDENTITY_CHAMPION)
        champ = json.loads(Path(path).read_text())
        if not isinstance(champ, dict) or "maps" not in champ or "version" not in champ:
            logger.warning("champion file malformed -> identity fallback")
            return copy.deepcopy(IDENTITY_CHAMPION)
        return champ
    except Exception as e:
        logger.warning("champion load failed (%s) -> identity fallback", e)
        return copy.deepcopy(IDENTITY_CHAMPION)


def save_champion(champion: Dict, path: Path = CHAMPION_PATH) -> None:
    """Atomically write the champion (tmp + replace to survive crashes mid-write). Single-writer assumed; concurrent callers race on the shared tmp file."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(path).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(champion, indent=1))
    tmp.replace(path)


def apply_champion(raw_prob: float, archetype: str, champion: Optional[Dict] = None) -> float:
    """Map a raw [0,1] probability through the champion's per-archetype isotonic map.
    Unknown archetype, identity champion, or a corrupt sub-model -> passthrough (§9C)."""
    if champion is None:
        champion = load_champion()
    model = champion.get("maps", {}).get(archetype)
    if not model or not isinstance(model, dict):
        return raw_prob
    try:
        return apply_isotonic(model, raw_prob)
    except Exception as e:
        logger.warning("apply_isotonic failed for archetype=%s (%s) -> passthrough", archetype, e)
        return raw_prob


def append_ledger(entry: Dict, path: Path = LEDGER_PATH) -> None:
    """Append one decision record to the append-only ledger (never raises)."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning("ledger append failed: %s", e)


def _alert(msg: str) -> None:
    """Best-effort alert; falls back to logger if no channel is wired (§9C)."""
    try:
        from signals.discord_alerts import send_discord  # type: ignore

        send_discord(f"[calibration-loop] {msg}")
    except Exception:
        logger.warning("[calibration-loop alert] %s", msg)
