#!/usr/bin/env python3
"""Codemod: rewrite ``sqlite3.connect(`` -> ``db_connect(`` and add the import.

Usage:
    python scripts/migrate_db_connect.py <file-list.txt> [--apply]

Reads newline-separated file paths (relative to repo root). Without --apply it
dry-runs (reports what it would change). With --apply it edits in place.

Safety:
- Skips files that already contain ``db_connect`` (idempotent / name in use).
- Requires a bare ``import sqlite3`` line as the anchor for inserting the import.
- Only rewrites the literal token ``sqlite3.connect(``.

WARNING: only safe for modules imported with the repo root on sys.path
(the ``api`` package). Modules run standalone (scripts/, some signals/) need
the packaging fix first or they will fail ``from db import``.
"""

import pathlib
import re
import sys

IMPORT_LINE = "from db import connect as db_connect"


def _insert_import(src: str):
    """Insert IMPORT_LINE after the first TOP-LEVEL import statement (column 0).

    Avoids landing inside a function body when sqlite3 is imported there.
    Returns the new source, or None if no top-level import anchor is found.
    """
    m = re.search(r"(?m)^(?:import \S|from \S.* import )", src)
    if not m:
        return None
    line_end = src.index("\n", m.start()) + 1
    return src[:line_end] + IMPORT_LINE + "\n" + src[line_end:]


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    apply = "--apply" in sys.argv
    list_path = sys.argv[1]
    files = [ln.strip() for ln in open(list_path) if ln.strip()]
    changed = 0
    for f in files:
        p = pathlib.Path(f)
        if not p.exists():
            print(f"SKIP (missing): {f}")
            continue
        src = p.read_text()
        if "db_connect" in src:
            print(f"SKIP (db_connect already present): {f}")
            continue
        n = src.count("sqlite3.connect(")
        if n == 0:
            print(f"SKIP (no sqlite3.connect): {f}")
            continue
        new = _insert_import(src)
        if new is None:
            print(f"SKIP (no top-level import anchor): {f}")
            continue
        new = new.replace("sqlite3.connect(", "db_connect(")
        if apply:
            p.write_text(new)
            print(f"OK {f}: {n} connect(s) migrated")
        else:
            print(f"WOULD migrate {f}: {n} connect(s)")
        changed += 1
    print(f"\n{'Applied' if apply else 'Dry-run'}: {changed} file(s)")


if __name__ == "__main__":
    main()
