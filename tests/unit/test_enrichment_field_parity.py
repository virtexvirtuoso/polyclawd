"""baseball_edge's inline enrichment must set every field the shared one does.

odds/sports_edge_common.enrich_executable_edge() is the canonical copy of the
executable-edge result onto an Edge. odds/baseball_edge.py does NOT call it --
it has its own inline copy inside find_baseball_edges (it needs a team-name
side label and its own token resolution). That duplicate omitted two fields:

    fillable_usd   -> p2_depth_ok(None) returns
                      (False, "P2: depth unavailable (book not fetched)")
    net_edge_pct   -> fee-adjusted edge never populated on the edge

sec.log_shadow gates on p2_depth_ok, so NO baseball edge could ever be logged
as a shadow trade, no matter how good it was. Observed on prod 2026-08-28: a
New York Yankees moneyline edge with edge_pct=+0.054, tradeable=True,
fee-adjusted edge=+0.0139 and p1_edge_ok=True was dropped solely because
fillable_usd was None.

A partial field copy is invisible: every individual field that IS copied looks
right, and the missing one only surfaces as a downstream gate that always
fails.
"""

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
SEC = REPO / "odds" / "sports_edge_common.py"
BASEBALL = REPO / "odds" / "baseball_edge.py"


def _assigned_attrs(node, obj_names):
    """Attribute names assigned as `<obj>.<attr> = ...` anywhere under node."""
    found = set()
    for n in ast.walk(node):
        if not isinstance(n, ast.Assign):
            continue
        for tgt in n.targets:
            if isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name) and tgt.value.id in obj_names:
                found.add(tgt.attr)
    return found


def _func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _canonical_fields():
    fn = _func(ast.parse(SEC.read_text()), "enrich_executable_edge")
    assert fn is not None, "enrich_executable_edge not found in sports_edge_common"
    return _assigned_attrs(fn, {"edge"})


def _baseball_fields():
    """Fields baseball sets on an Edge anywhere in find_baseball_edges.

    Scoped to the whole function, not just the `if _ex.get("available"):`
    block: live_book is legitimately set earlier, in the WebSocket-book
    branch. The invariant is "the field gets set somewhere during
    enrichment", not "in one particular block".
    """
    fn = _func(ast.parse(BASEBALL.read_text()), "find_baseball_edges")
    assert fn is not None, "find_baseball_edges not found in baseball_edge"
    return _assigned_attrs(fn, {"_e"})


def test_canonical_enrichment_sets_the_expected_fields():
    """Positive control: if this set is empty the parity test is vacuous."""
    fields = _canonical_fields()
    assert "fillable_usd" in fields and "tradeable" in fields, fields


def test_baseball_enrichment_is_not_missing_fields():
    canonical = _canonical_fields()
    baseball = _baseball_fields()
    assert baseball, "found no enrichment block in find_baseball_edges"
    missing = canonical - baseball
    assert not missing, (
        "baseball_edge's inline enrichment omits fields that "
        f"sports_edge_common.enrich_executable_edge sets: {sorted(missing)}. "
        "fillable_usd in particular makes p2_depth_ok fail for every edge, so "
        "no baseball shadow trade can ever be logged."
    )


@pytest.mark.parametrize("field", ["fillable_usd", "net_edge_pct"])
def test_specific_regression_fields_present(field):
    assert field in _baseball_fields(), (
        f"{field} is not set by baseball_edge's enrichment — this is the exact "
        "omission that silently blocked baseball shadow logging"
    )
