import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import server  # noqa: E402

# Minimal fake OpenAPI spec: 2 allowlisted GETs, 1 non-allowlisted, 1 secured.
FAKE_SPEC = {
    "paths": {
        "/api/signals": {"get": {"summary": "junk summary", "parameters": []}},
        "/api/whale/outcomes": {
            "get": {
                "summary": "x",
                "parameters": [
                    {"name": "days", "in": "query", "schema": {"type": "integer", "default": 30}}
                ],
            }
        },
        "/api/not/allowed": {"get": {"summary": "nope", "parameters": []}},
        "/api/engine/status": {"get": {"summary": "s", "security": [{"ApiKey": []}], "parameters": []}},
    }
}


def test_build_tools_filters_to_allowlist_and_curates():
    tools = server.build_tools(FAKE_SPEC)
    names = {t["name"] for t in tools}
    # /api/not/allowed excluded (not in ALLOWLIST); /api/engine/status excluded (security)
    assert names == {"polyclawd_signals", "polyclawd_whale_outcomes"}
    by_path = {t["_path"]: t for t in tools}
    # curated description overrides the junk OpenAPI summary
    assert by_path["/api/signals"]["description"] == server.TOOL_META["/api/signals"][1]
    assert by_path["/api/signals"]["description"] != "junk summary"
    # input schema still auto-generated from params
    assert "days" in by_path["/api/whale/outcomes"]["inputSchema"]["properties"]
    assert by_path["/api/whale/outcomes"]["_method"] == "get"


def test_allowlist_and_tool_meta_cover_same_25_paths():
    assert len(server.ALLOWLIST) == 25
    assert set(server.TOOL_META) == server.ALLOWLIST  # every allowlisted path has curated meta
