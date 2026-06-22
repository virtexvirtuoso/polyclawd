import json
import os
import subprocess
import sys
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import server  # noqa: E402

# Minimal fake OpenAPI spec: 2 allowlisted GETs, 1 non-allowlisted, 1 secured.
FAKE_SPEC = {
    "paths": {
        "/api/signals": {"get": {"summary": "junk summary", "parameters": []}},
        "/api/whale/outcomes": {
            "get": {
                "summary": "x",
                "parameters": [{"name": "days", "in": "query", "schema": {"type": "integer", "default": 30}}],
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


def test_inject_default_limit_only_when_param_supported():
    # tool that accepts `limit`
    tool_with = {"_path": "/api/markets/search", "inputSchema": {"properties": {"q": {}, "limit": {}}}}
    qp = server._inject_default_limit(tool_with, {"q": "btc"})
    assert qp["limit"] == 25
    # caller-provided limit is preserved
    qp2 = server._inject_default_limit(tool_with, {"q": "btc", "limit": 5})
    assert qp2["limit"] == 5
    # tool with no limit param in schema -> never inject (would 422)
    tool_without = {"_path": "/api/markets/search", "inputSchema": {"properties": {"q": {}}}}
    qp3 = server._inject_default_limit(tool_without, {"q": "btc"})
    assert "limit" not in qp3
    # path not in DEFAULT_LIMITS -> untouched
    tool_other = {"_path": "/api/engine/status", "inputSchema": {"properties": {}}}
    assert server._inject_default_limit(tool_other, {}) == {}


def test_cap_response_small_passthrough():
    small = {"a": 1, "items": [1, 2, 3]}
    assert server._cap_response(small) == small  # under cap, unchanged


def test_cap_response_truncates_nested_largest_list():
    # nested big list (mimics /api/signals' nested structure)
    big = {"meta": {"k": "v"}, "sources": {"whales": [{"x": "y" * 50} for _ in range(2000)]}}
    capped = server._cap_response(big)
    assert capped["_truncated"] is True
    assert "_hint" in capped
    # the nested list was shortened
    assert len(capped["sources"]["whales"]) < 2000
    assert len(json.dumps(capped).encode()) <= server.MAX_RESULT_BYTES


def test_cap_response_no_list_payload():
    huge_scalar = {"blob": "z" * 40000}
    capped = server._cap_response(huge_scalar)
    assert capped["_truncated"] is True


def test_wrap_envelope_shape():
    w = server._wrap({"x": 1})
    assert w["untrusted_data"] == {"x": 1}
    assert "external" in w["_note"].lower()


def test_handle_tool_call_unknown_tool_is_wrapped_error(monkeypatch):
    # force tool registry so name is unknown but registry is non-empty
    monkeypatch.setattr(
        server,
        "TOOLS",
        [{"name": "x", "_path": "/api/x", "_method": "get", "description": "", "inputSchema": {"properties": {}}}],
    )
    monkeypatch.setattr(server, "_TOOL_MAP", {"x": server.TOOLS[0]})
    out = server.handle_tool_call("does_not_exist", {})
    assert out["untrusted_data"] == {"error": "Unknown tool: does_not_exist"}


def test_cache_roundtrip_and_fallback(tmp_path, monkeypatch):
    cache = tmp_path / ".tool_cache.json"
    monkeypatch.setattr(server, "CACHE_PATH", cache)
    sample = [
        {
            "name": "polyclawd_signals",
            "description": "d",
            "inputSchema": {"properties": {}},
            "_path": "/api/signals",
            "_method": "get",
        }
    ]
    server._save_cached_tools(sample)
    assert cache.exists()
    assert server._load_cached_tools() == sample


def test_discover_falls_back_to_cache_on_fetch_failure(tmp_path, monkeypatch):
    cache = tmp_path / ".tool_cache.json"
    monkeypatch.setattr(server, "CACHE_PATH", cache)
    sample = [
        {
            "name": "polyclawd_signals",
            "_path": "/api/signals",
            "_method": "get",
            "description": "d",
            "inputSchema": {"properties": {}},
        }
    ]
    server._save_cached_tools(sample)
    # dead host -> fetch fails -> cache served
    tools = server.discover_tools(base_url="https://127.0.0.1:0")
    assert [t["name"] for t in tools] == ["polyclawd_signals"]


SERVER = os.path.join(os.path.dirname(__file__), "server.py")


def _api_up():
    try:
        urllib.request.urlopen("https://virtuosocrypto.com/polyclawd/api/openapi.json", timeout=8)
        return True
    except Exception:
        return False


def _rpc(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


@pytest.mark.skipif(not _api_up(), reason="public Polyclawd API unreachable")
def test_stdio_server_lists_25_and_calls_signals():
    proc = subprocess.Popen(
        [sys.executable, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1
    )
    try:
        _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        listed = _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = listed["result"]["tools"]
        assert len(tools) == 25
        assert all(t["name"].startswith("polyclawd_") for t in tools)
        # curated descriptions match TOOL_META exactly
        names_to_desc = {t["name"]: t["description"] for t in tools}
        for path, (name, desc) in server.TOOL_META.items():
            assert names_to_desc[name] == desc
        # call one tool, assert envelope + real data
        called = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "polyclawd_signals", "arguments": {}},
            },
        )
        payload = json.loads(called["result"]["content"][0]["text"])
        assert "untrusted_data" in payload and "_note" in payload
        assert payload["untrusted_data"] != {"error": "Unknown tool: polyclawd_signals"}
    finally:
        proc.terminate()
