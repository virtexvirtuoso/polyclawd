"""Guard: every `polyproxy: central URL config` import must actually execute.

The polyproxy refactor rewrote ~131 modules to pull Polymarket URLs from
config.polymarket_urls. In odds/baseball_edge.py the inserted line landed
INSIDE the module docstring (line 18 of a 1-20 line docstring), so it was
inert text. `POLYMARKET_GAMMA` was never defined and every Polymarket fetch
raised NameError into a bare `except Exception` that logged and returned [].

The scanner therefore reported "15 Odds API games, 0 Polymarket game events"
and found 0 edges on every run from 2026-06-17 to 2026-08-28 — ten weeks in
which baseball_moneyline / baseball_spread / baseball_total (251 of 449
shadow rows, the learning loop's largest producer) wrote nothing at all,
while their resolver stayed green because it had already resolved 103/103,
85/85 and 63/63 of the old rows.

A programmatic insertion that exits 0 is not a verified insertion.
"""

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
MARKER = "polyproxy"
SKIP = ("/venv/", "/.git/", "__pycache__", "/node_modules/")


def _python_files():
    for p in sorted(REPO.rglob("*.py")):
        s = str(p)
        if any(x in s for x in SKIP):
            continue
        yield p


def _inert_marker_imports(path: pathlib.Path):
    """Names a polyproxy line claims to import but that never reach the AST."""
    try:
        src = path.read_text()
    except (OSError, UnicodeDecodeError):
        return []
    if MARKER not in src:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    real = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    inert = []
    for lineno, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        # Only import STATEMENTS, not prose that happens to mention the marker
        # (this file's own docstring would otherwise match itself).
        if not stripped.startswith(("from ", "import ")):
            continue
        if MARKER not in line or " import " not in line:
            continue
        for tok in line.split(" import ", 1)[1].split("#")[0].split(","):
            tok = tok.strip()
            if not tok:
                continue
            name = tok.split(" as ")[-1].strip() if " as " in tok else tok
            if name and name not in real:
                inert.append((lineno, name))
    return inert


def test_no_polyproxy_import_is_inert():
    """Every polyproxy import line must parse as a real module-level import."""
    offenders = []
    for path in _python_files():
        for lineno, name in _inert_marker_imports(path):
            offenders.append(f"{path.relative_to(REPO)}:{lineno} -> {name}")
    assert not offenders, (
        "polyproxy import lines that never execute (likely inserted inside a "
        "docstring or a comment block):\n  " + "\n  ".join(offenders)
    )


def test_the_guard_can_actually_fail(tmp_path):
    """Positive control: the detector must flag a known-bad file.

    Without this, a detector that silently matches nothing would pass this
    module forever and read as 'all clear'.
    """
    bad = tmp_path / "bad_module.py"
    bad.write_text(
        '"""Docstring.\n\n'
        "Usage:\n"
        "    from x import y\n"
        "from config.polymarket_urls import GAMMA_API as POLYMARKET_GAMMA  "
        "# polyproxy: central URL config\n"
        '"""\n\n'
        "import os\n"
    )
    found = _inert_marker_imports(bad)
    assert found == [(5, "POLYMARKET_GAMMA")], found


def test_baseball_edge_exposes_polymarket_gamma():
    """The specific regression: the module-level name must exist and be a URL."""
    mod = pytest.importorskip("odds.baseball_edge")
    assert hasattr(mod, "POLYMARKET_GAMMA"), (
        "odds.baseball_edge.POLYMARKET_GAMMA is undefined — "
        "_fetch_polymarket_baseball_sync will raise NameError and silently "
        "return zero Polymarket events"
    )
    assert str(mod.POLYMARKET_GAMMA).startswith("http")
