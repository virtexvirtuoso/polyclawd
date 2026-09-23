#!/usr/bin/env python3
"""
Polyclawd MCP Server — Auto-discovering

Fetches OpenAPI spec from the Polyclawd API and exposes every GET endpoint
as an MCP tool.  No more manual TOOLS list to maintain.

Stdio transport:  python server.py
HTTP transport:   imported by http_server.py (FastMCP wrapper)
"""

import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── config ───────────────────────────────────────────────────────────────
BASE_URL = "https://virtuosocrypto.com/polyclawd"
PROTOCOL_VERSION = "2024-11-05"

# Endpoints to skip
SKIP_PATHS = {
    "/health", "/ready", "/metrics",
    "/api/visitor-log",
    "/", "/manifest.json", "/sw.js",
}
SKIP_PREFIXES = (
    "/docs", "/openapi", "/redoc",
    "/static",
)

# Skip POST endpoints that are mutators (only expose read-only tools)
# Allow specific safe POSTs
SAFE_POST_PATHS = {
    "/api/edge-scanner/calculate",
    "/api/phase/simulate",
    "/api/kelly/simulate",
}

# Skip endpoints that are duplicates or internal
SKIP_PATTERNS = {
    "polyclawd_",  # bare root
}

# Friendly category prefixes for tool naming
CATEGORY_ORDER = [
    "signals", "portfolio", "archetype", "markets", "vegas", "espn",
    "kalshi", "manifold", "metaculus", "predictit", "betfair",
    "polyrouter", "basket-arb", "copy-trade", "engine", "phase",
    "kelly", "alerts", "llm", "paper", "simmer", "trading",
    "scan", "topics", "calculate", "rewards",
]


# ── helpers ──────────────────────────────────────────────────────────────

def api_get(path: str, timeout: int = 60) -> dict:
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd-MCP/2.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def api_post(path: str, params: dict = None, timeout: int = 30) -> dict:
    url = f"{BASE_URL}{path}"
    body = json.dumps(params or {}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"User-Agent": "Polyclawd-MCP/2.1", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def _path_to_tool_name(path: str) -> str:
    """Convert /api/signals/weather → polyclawd_signals_weather"""
    # Strip /api/ prefix
    clean = re.sub(r"^/api/", "", path)
    clean = re.sub(r"^/", "", clean)
    # Replace slashes and hyphens with underscores
    clean = clean.replace("/", "_").replace("-", "_")
    # Remove path parameters like {market_id}
    clean = re.sub(r"\{[^}]+\}", "", clean).strip("_")
    return f"polyclawd_{clean}"


def _path_to_description(path: str, method: str, summary: str, docstring: str) -> str:
    """Build a concise description from OpenAPI metadata."""
    if summary:
        return summary
    if docstring:
        # First sentence
        first = docstring.split(".")[0].strip()
        if first:
            return first
    return f"{method.upper()} {path}"


def _extract_params(schema: dict, openapi_spec: dict) -> dict:
    """Convert OpenAPI parameters to MCP inputSchema."""
    properties = {}
    required = []
    for param in schema:
        name = param.get("name", "")
        if param.get("in") == "header":
            continue  # skip headers
        p_schema = param.get("schema", {})
        # Resolve $ref
        if "$ref" in p_schema:
            ref_path = p_schema["$ref"].replace("#/", "").split("/")
            resolved = openapi_spec
            for part in ref_path:
                resolved = resolved.get(part, {})
            p_schema = resolved
        prop = {"type": p_schema.get("type", "string")}
        desc = param.get("description", "")
        if desc:
            prop["description"] = desc
        if "default" in p_schema:
            prop["default"] = p_schema["default"]
        if "enum" in p_schema:
            prop["enum"] = p_schema["enum"]
        properties[name] = prop
        if param.get("required"):
            required.append(name)
    return {"type": "object", "properties": properties, "required": required}


# ── OpenAPI spec cache ───────────────────────────────────────────────────
# The API serves this 162 KB document in 21-34s, which overruns the 20s budget
# an MCP client allows for `initialize`.  Keep the spec on disk, serve it from
# there, and refresh it in the background so no startup path blocks on the
# network.

CACHE_DIR = Path(os.environ.get(
    "POLYCLAWD_CACHE_DIR", Path.home() / ".cache" / "polyclawd"))
SPEC_CACHE = CACHE_DIR / "openapi.json"
SPEC_TTL_SECONDS = 24 * 3600
SPEC_FETCH_TIMEOUT = 60

_spec_lock = threading.Lock()


def _fetch_spec(base_url: str = None) -> Optional[dict]:
    url = (base_url or BASE_URL).rstrip("/") + "/api/openapi.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd-MCP/2.1"})
        with urllib.request.urlopen(req, timeout=SPEC_FETCH_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.error("Failed to fetch OpenAPI spec from %s: %s", url, e)
        return None


def _read_spec_cache():
    """Return (spec, age_seconds), or (None, None) when there is no usable cache."""
    try:
        if not SPEC_CACHE.is_file():
            return None, None
        age = time.time() - SPEC_CACHE.stat().st_mtime
        return json.loads(SPEC_CACHE.read_text()), age
    except Exception as e:
        logger.warning("Ignoring unreadable spec cache %s: %s", SPEC_CACHE, e)
        return None, None


def _write_spec_cache(spec: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SPEC_CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(spec))
        os.replace(tmp, SPEC_CACHE)
    except Exception as e:
        logger.warning("Could not write spec cache %s: %s", SPEC_CACHE, e)


def _refresh_spec_cache() -> None:
    """Fetch the spec and update the cache.  Safe to call from a thread."""
    with _spec_lock:
        spec = _fetch_spec()
        if spec:
            _write_spec_cache(spec)


def warm_spec_cache_async() -> None:
    """Refresh the cache off the critical path."""
    threading.Thread(target=_refresh_spec_cache, name="spec-refresh", daemon=True).start()


def _load_spec(base_url: str = None) -> Optional[dict]:
    """Return the OpenAPI spec, preferring the on-disk cache.

    A non-default base_url bypasses the cache entirely, so callers pointing at
    another host (or at an unreachable one, as the curation tests do) always
    see that host's real answer.
    """
    if base_url is not None and base_url.rstrip("/") != BASE_URL.rstrip("/"):
        return _fetch_spec(base_url)

    spec, age = _read_spec_cache()
    if spec is not None:
        if age > SPEC_TTL_SECONDS:
            warm_spec_cache_async()   # serve what we have, freshen for next time
        return spec

    # Cold start: no cache yet.  Wait behind any in-flight refresh, then re-check
    # so two callers never fetch the same document twice.
    with _spec_lock:
        spec, _ = _read_spec_cache()
        if spec is not None:
            return spec
        spec = _fetch_spec()
        if spec:
            _write_spec_cache(spec)
        return spec


# ── auto-discovery ───────────────────────────────────────────────────────

def discover_tools(base_url: str = None) -> List[dict]:
    """Convert the OpenAPI spec's GET endpoints to MCP tool definitions."""
    spec = _load_spec(base_url)
    if not spec:
        return []

    tools = []
    seen_names = set()
    paths = spec.get("paths", {})

    for path, methods in sorted(paths.items()):
        # Skip excluded paths
        if path in SKIP_PATHS:
            continue
        if any(path.startswith(p) for p in SKIP_PREFIXES):
            continue

        for method in ("get", "post"):
            if method not in methods:
                continue

            # POST endpoints: only allow explicitly safe ones
            if method == "post" and path not in SAFE_POST_PATHS:
                continue

            endpoint = methods[method]

            # Skip if requires API key (mutating endpoints)
            security = endpoint.get("security", [])
            if security:
                continue

            tool_name = _path_to_tool_name(path)
            # Deduplicate (GET wins over POST)
            if tool_name in seen_names:
                continue
            seen_names.add(tool_name)

            summary = endpoint.get("summary", "")
            description = _path_to_description(
                path, method, summary, endpoint.get("description", "")
            )

            # Build input schema from query/path parameters
            params = endpoint.get("parameters", [])
            input_schema = _extract_params(params, spec)

            # Skip junk tool names
            if tool_name in SKIP_PATTERNS or len(tool_name) <= len("polyclawd_"):
                continue
            # Skip static file extensions
            if any(tool_name.endswith(ext) for ext in (".js", ".json", ".html", ".css", ".png")):
                continue

            tools.append({
                "name": tool_name,
                "description": description,
                "inputSchema": input_schema,
                "_path": path,
                "_method": method,
            })

    logger.info("Auto-discovered %d MCP tools from OpenAPI spec", len(tools))
    return tools


# ── global tool registry (populated on first use) ───────────────────────

TOOLS: List[dict] = []
_TOOL_MAP: Dict[str, dict] = {}


def _ensure_tools():
    """Lazy-load tools on first access."""
    global TOOLS, _TOOL_MAP
    if TOOLS:
        return
    TOOLS = discover_tools()
    _TOOL_MAP = {t["name"]: t for t in TOOLS}


def get_tools() -> List[dict]:
    """Return tool definitions (without internal fields)."""
    _ensure_tools()
    return [
        {k: v for k, v in t.items() if not k.startswith("_")}
        for t in TOOLS
    ]


# ── tool execution ───────────────────────────────────────────────────────

def handle_tool_call(name: str, arguments: dict) -> Any:
    """Execute a tool by routing to the corresponding API endpoint."""
    _ensure_tools()
    tool = _TOOL_MAP.get(name)
    if not tool:
        return {"error": f"Unknown tool: {name}"}

    path = tool["_path"]
    method = tool["_method"]

    # Substitute path parameters like {symbol}, {position_id}
    for key, val in arguments.items():
        placeholder = "{" + key + "}"
        if placeholder in path:
            path = path.replace(placeholder, str(val))

    # Remaining arguments become query params for GET
    query_params = {
        k: v for k, v in arguments.items()
        if "{" + k + "}" not in tool["_path"]
    }

    if method == "get":
        if query_params:
            qs = "&".join(f"{k}={v}" for k, v in query_params.items())
            path = f"{path}?{qs}"
        return api_get(path)
    else:
        return api_post(path, query_params)


# ── stdio MCP transport ─────────────────────────────────────────────────

def send_response(id, result):
    msg = json.dumps({"jsonrpc": "2.0", "id": id, "result": result})
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def send_error(id, code, message):
    msg = json.dumps({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def main():
    """Run MCP server in stdio mode.

    Tools are discovered lazily from the cached spec, so `initialize` is
    answered immediately even when the upstream spec endpoint is slow.
    """
    _, age = _read_spec_cache()
    if age is None or age > SPEC_TTL_SECONDS:
        warm_spec_cache_async()
    logger.info("Polyclawd MCP Server started (tools load on first use)")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})

        if method == "initialize":
            send_response(id, {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": "polyclawd", "version": "2.1.0"},
                "capabilities": {"tools": {}},
            })

        elif method == "notifications/initialized":
            pass

        elif method == "tools/list":
            send_response(id, {"tools": get_tools()})

        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            result = handle_tool_call(tool_name, arguments)
            send_response(id, {
                "content": [
                    {"type": "text", "text": json.dumps(result, indent=2)}
                ]
            })

        else:
            send_error(id, -32601, f"Method not found: {method}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
