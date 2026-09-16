"""Guard: every script the scheduler shells out to must import cleanly.

services/scheduler.py launches several project scripts as SUBPROCESSES. A
subprocess does not inherit the parent's sys.path, so a module-level
`from config.polymarket_urls import ...` in one of those scripts raises
ModuleNotFoundError unless PYTHONPATH includes the project root.

task_shadow_resolution ran signals/shadow_tracker.py every 5 minutes with
capture_output=True and no returncode check, so from 2026-06 to 2026-08-28
the generic shadow resolver crashed on import ~288 times a day in total
silence. The sport-specific resolvers are in-process imports and kept
working, which is why baseball showed 103/103 resolved while
MispricedCategoryWhale sat at 36/71 and cross_platform_arb at 0/21.

This test discovers the invoked scripts from scheduler.py's own AST, so it
keeps covering new call sites without being updated.
"""

import ast
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCHEDULER = REPO / "services" / "scheduler.py"


def _invoked_scripts():
    """Find `PROJECT_ROOT / "dir" / "name.py"` expressions in scheduler.py."""
    tree = ast.parse(SCHEDULER.read_text())
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            continue
        parts = []
        cur = node
        while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
            if isinstance(cur.right, ast.Constant) and isinstance(cur.right.value, str):
                parts.append(cur.right.value)
            cur = cur.left
        if isinstance(cur, ast.Name) and cur.id == "PROJECT_ROOT" and parts:
            rel = "/".join(reversed(parts))
            if rel.endswith(".py"):
                found.add(rel)
    return sorted(found)


def test_scheduler_invokes_some_scripts():
    """Positive control: the AST walk must actually find call sites.

    If this ever returns nothing, the parametrised test below would silently
    pass on an empty set and read as an all-clear.
    """
    scripts = _invoked_scripts()
    assert scripts, "found no PROJECT_ROOT-relative scripts in scheduler.py"


@pytest.mark.parametrize("rel", _invoked_scripts())
def test_invoked_script_imports_without_inherited_syspath(rel):
    """Import the script the way the scheduler actually launches it.

    Compile-and-import only (`-c "import runpy..."` would execute it), so we
    exec the module's imports via a bare `python -c` that imports the file's
    package path. Simplest faithful proxy: run the file with a flag it does
    not recognise and assert it did not die on an ImportError.
    """
    path = REPO / rel
    if not path.exists():
        pytest.skip(f"{rel} not present")

    proc = subprocess.run(
        [sys.executable, str(path), "--__import_probe__"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO),
    )
    err = proc.stderr or ""
    assert "ModuleNotFoundError" not in err and "ImportError" not in err, (
        f"{rel} fails to import when run as a subprocess:\n"
        f"{err.strip()[-600:]}\n\n"
        "The scheduler must pass PYTHONPATH=PROJECT_ROOT in the subprocess env."
    )
