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


def test_allowlist_and_tool_meta_cover_same_26_paths():
    assert len(server.ALLOWLIST) == 26
    assert "/api/portfolio/status" in server.ALLOWLIST
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


_SAMPLE = [
    {
        "name": "polyclawd_signals",
        "_path": "/api/signals",
        "_method": "get",
        "description": "d",
        "inputSchema": {"properties": {}},
    }
]


def _fresh_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CACHE_PATH", tmp_path / ".tool_cache.json")
    monkeypatch.setattr(server, "TOOLS", [])
    monkeypatch.setattr(server, "_TOOL_MAP", {})


def test_ensure_tools_serves_current_cache_without_waiting_on_network(tmp_path, monkeypatch):
    import threading
    import time

    _fresh_registry(monkeypatch, tmp_path)
    server._save_cached_tools(_SAMPLE)
    gate = threading.Event()
    calls = []

    def slow_discover(base_url=None):
        calls.append(1)
        gate.wait(5)
        return [dict(_SAMPLE[0], name="polyclawd_refreshed")]

    monkeypatch.setattr(server, "discover_tools", slow_discover)
    t0 = time.time()
    names = [t["name"] for t in server.get_tools()]
    assert time.time() - t0 < 1.0  # answered from cache, not blocked on the fetch
    assert names == ["polyclawd_signals"]
    gate.set()
    server._REFRESH_THREAD.join(5)
    assert calls == [1]  # exactly one background refresh
    assert [t["name"] for t in server.TOOLS] == ["polyclawd_refreshed"]
    assert "polyclawd_refreshed" in server._TOOL_MAP


def test_ensure_tools_fetches_synchronously_when_cache_is_from_other_code(tmp_path, monkeypatch):
    _fresh_registry(monkeypatch, tmp_path)
    server.CACHE_PATH.write_text(json.dumps({"fingerprint": "older-code", "tools": _SAMPLE}))
    monkeypatch.setattr(server, "discover_tools", lambda base_url=None: [dict(_SAMPLE[0], name="polyclawd_fresh")])
    assert [t["name"] for t in server.get_tools()] == ["polyclawd_fresh"]


def test_ensure_tools_fetches_synchronously_when_no_cache(tmp_path, monkeypatch):
    _fresh_registry(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "discover_tools", lambda base_url=None: [dict(_SAMPLE[0], name="polyclawd_fresh")])
    assert [t["name"] for t in server.get_tools()] == ["polyclawd_fresh"]


def test_legacy_list_cache_still_serves_as_fetch_fallback(tmp_path, monkeypatch):
    _fresh_registry(monkeypatch, tmp_path)
    server.CACHE_PATH.write_text(json.dumps(_SAMPLE))  # pre-fingerprint format
    assert server._load_cached_tools() == _SAMPLE
    tools = server.discover_tools(base_url="https://127.0.0.1:0")
    assert [t["name"] for t in tools] == ["polyclawd_signals"]


@pytest.mark.parametrize("content", ["null", "42", '"x"', "[1, 2]", '{"fingerprint": "f", "tools": "nope"}', '{"tools": [{"x": 1}]}'])
def test_malformed_cache_shapes_never_crash_and_fall_back_to_fetch(tmp_path, monkeypatch, content):
    _fresh_registry(monkeypatch, tmp_path)
    content = content.replace('"f"', '"%s"' % server._CODE_FINGERPRINT)
    server.CACHE_PATH.write_text(content)
    monkeypatch.setattr(server, "discover_tools", lambda base_url=None: [dict(_SAMPLE[0], name="polyclawd_fresh")])
    assert [t["name"] for t in server.get_tools()] == ["polyclawd_fresh"]
    assert server._load_cached_tools() == []


def test_concurrent_cache_writes_never_expose_a_partial_file(tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr(server, "CACHE_PATH", tmp_path / ".tool_cache.json")
    big = [dict(_SAMPLE[0], name="polyclawd_%d" % i, description="d" * 400) for i in range(200)]
    server._save_cached_tools(big)
    stop = threading.Event()
    bad = []

    def writer():
        while not stop.is_set():
            server._save_cached_tools(big)

    def reader():
        while not stop.is_set():
            _, tools = server._read_cache()
            if tools is None or len(tools) != 200:
                bad.append(1)

    threads = [threading.Thread(target=writer) for _ in range(8)] + [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    stop.wait(3)
    stop.set()
    for t in threads:
        t.join()
    assert bad == []


def test_cached_tool_without_path_is_not_trusted(tmp_path, monkeypatch):
    _fresh_registry(monkeypatch, tmp_path)
    no_path = [{k: v for k, v in _SAMPLE[0].items() if k != "_path"}]
    server.CACHE_PATH.write_text(json.dumps({"fingerprint": server._CODE_FINGERPRINT, "tools": no_path}))
    import threading

    callers = []

    def discover(base_url=None):
        callers.append(threading.current_thread() is threading.main_thread())
        return [dict(_SAMPLE[0], name="polyclawd_fresh")]

    monkeypatch.setattr(server, "discover_tools", discover)
    server.get_tools()
    assert callers and callers[0] is True  # untrusted cache -> synchronous fetch, not a background refresh


def test_tool_call_exception_returns_error_and_server_keeps_serving(monkeypatch, capsys):
    def boom(name, arguments):
        raise KeyError("_path")

    monkeypatch.setattr(server, "handle_tool_call", boom)
    server._handle_request({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "x", "arguments": {}}})
    server._handle_request({"jsonrpc": "2.0", "id": 8, "method": "initialize", "params": {}})
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert out[0]["id"] == 7 and out[0]["error"]["code"] == -32603
    assert out[1]["id"] == 8 and "result" in out[1]


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
def test_stdio_server_lists_26_and_calls_signals():
    proc = subprocess.Popen(
        [sys.executable, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1
    )
    try:
        _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        listed = _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = listed["result"]["tools"]
        assert len(tools) == 26
        assert all(t["name"].startswith("polyclawd_") for t in tools)
        # curated descriptions match TOOL_META exactly
        names_to_desc = {t["name"]: t["description"] for t in tools}
        for _path, (name, desc) in server.TOOL_META.items():
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
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except Exception:
                pass
