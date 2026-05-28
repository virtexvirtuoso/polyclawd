"""Daily fetch + parse of Atlanta Fed Market Probability Tracker.

Idempotent. Writes a sha256-tagged daily CSV. Alerts on schema break or HTTP non-200.

Schedule via launchd at ~18:00 ET. See vault:
  ~/virtuoso-vault/02-Projects/Polyclawd/Infrastructure/AtlantaFed-Tracker-Ingestion.md
"""
from __future__ import annotations

import hashlib
import pathlib
import sys
from datetime import date

import pandas as pd
import requests

URL = "https://www.atlantafed.org/-/media/Project/Atlanta/FRBA/Documents/cenfis/market-probability-tracker/mpt_histdata.xlsx"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"
OUT_DIR = pathlib.Path.home() / "Projects" / "polyclawd" / "data" / "atlfed"
EXPECTED_COLS = {"date", "reference_start", "target_range", "field", "value"}


class SchemaError(Exception):
    pass


def fetch() -> bytes:
    r = requests.get(URL, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    return r.content


def parse(data: bytes) -> pd.DataFrame:
    df = pd.read_excel(data, sheet_name="DATA")
    missing = EXPECTED_COLS - set(df.columns)
    if missing:
        raise SchemaError(f"missing columns: {missing}")
    if len(df) == 0:
        raise SchemaError("zero rows in DATA sheet")
    df["date"] = pd.to_datetime(df["date"])
    df["reference_start"] = pd.to_datetime(df["reference_start"])
    return df


def save(df: pd.DataFrame, raw: bytes) -> pathlib.Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    sha = hashlib.sha256(raw).hexdigest()[:12]
    out = OUT_DIR / f"{today}__sha{sha}.csv.gz"
    # Skip if same hash already exists today
    existing_today = list(OUT_DIR.glob(f"{today}__*.csv.gz"))
    for f in existing_today:
        if sha in f.name:
            print(f"SKIP: identical snapshot already saved as {f.name}", file=sys.stderr)
            return f
    df.to_csv(out, index=False, compression="gzip")
    return out


def main() -> int:
    try:
        raw = fetch()
        df = parse(raw)
        out = save(df, raw)
        print(f"OK rows={len(df):,} last_date={df.date.max().date()} -> {out}")
        return 0
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
