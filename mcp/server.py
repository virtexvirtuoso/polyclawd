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
import re
import sys
import urllib.request
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# ── config ───────────────────────────────────────────────────────────────
BASE_URL = "https://virtuosocrypto.com/polyclawd"
PROTOCOL_VERSION = "2024-11-05"

# Endpoints to skip
SKIP_PATHS = {
    "/health",
    "/ready",
    "/metrics",
    "/api/visitor-log",
    "/",
    "/manifest.json",
    "/sw.js",
}
SKIP_PREFIXES = (
    "/docs",
    "/openapi",
    "/redoc",
    "/static",
)

# Skip POST endpoints that are mutators (only expose read-only tools)
# Allow specific safe POSTs
SAFE_POST_PATHS = {
    "/api/edge-scanner/calculate",
    "/api/phase/simulate",
    "/api/kelly/simulate",
}

# ── curated allowlist (read-only, GET-only) ──────────────────────────────
# path: (curated_tool_name, curated_description)
TOOL_META = {
    "/api/signals": (
        "polyclawd_signals",
        "Aggregated trade signals across all 15 sources (whales, news, volume, elections, edge). Slow (~30s).",
    ),
    "/api/signals/news": ("polyclawd_news_signals", "Google-News/Reddit-derived market-impact signals."),
    "/api/signals/elections": ("polyclawd_election_signals", "Current election-market signals (Kalshi/Polymarket)."),
    "/api/signals/mispriced-category": (
        "polyclawd_mispriced_category",
        "Category-mispricing + whale-confirmation signals.",
    ),
    "/api/edge/scan": ("polyclawd_edge_scan", "Cross-platform arbitrage/edge scan (Shin devig)."),
    "/api/edge/topics": ("polyclawd_edge_topics", "Topics currently surfacing cross-platform edge."),
    "/api/arb-scan": ("polyclawd_arb_scan", "Polymarket-vs-Kalshi arbitrage spread scan."),
    "/api/rewards": ("polyclawd_rewards", "Liquidity-reward (LP incentive) opportunities."),
    "/api/markets/search": ("polyclawd_markets_search", "Search prediction markets by keyword."),
    "/api/markets/trending": ("polyclawd_markets_trending", "Trending markets by volume/activity."),
    "/api/markets/new": ("polyclawd_markets_new", "Recently-listed markets."),
    "/api/markets/opportunities": ("polyclawd_opportunities", "Open positions + highest-edge opportunities widget."),
    "/api/vegas/odds": ("polyclawd_vegas_odds", "Sportsbook (Vegas) consensus odds."),
    "/api/vegas/edge": ("polyclawd_vegas_edge", "Sharp-odds edge vs market price."),
    "/api/espn/edge": ("polyclawd_espn_edge", "ESPN/DraftKings-derived edge."),
    "/api/whale/alerts": ("polyclawd_whale_alerts", "Recent whale-wallet alerts."),
    "/api/whale/stats": ("polyclawd_whale_stats", "Whale-tracker summary stats."),
    "/api/whale/top": ("polyclawd_whale_top", "Top whale wallets by activity/score."),
    "/api/whale/outcomes": ("polyclawd_whale_outcomes", "Whale-alert precision (hit-rate) by severity."),
    "/api/weather/ensemble-accuracy": ("polyclawd_weather_skill", "Forecast-source skill (RMSE/MAE) by source+city."),
    "/api/signals/elections/control-history": (
        "polyclawd_election_control_history",
        "Daily party-control probability series.",
    ),
    "/api/signals/elections/race-prices": ("polyclawd_election_race_prices", "Per-market election odds time-series."),
    "/api/engine/status": ("polyclawd_engine_status", "Trading-engine status (read-only)."),
    "/api/phase/current": ("polyclawd_phase_current", "Current scaling-phase + limits (read-only)."),
    "/api/source-health": ("polyclawd_source_health", "Per-source API health/uptime metrics."),
}
ALLOWLIST = set(TOOL_META)

# Per-path default `limit` for list-y endpoints (only injected when the
# endpoint's schema actually declares the param, to avoid a FastAPI 422).
DEFAULT_LIMITS = {
    "/api/signals/elections/race-prices": 100,
    "/api/signals/elections/control-history": 180,
    "/api/markets/search": 25,
    "/api/markets/trending": 25,
    "/api/whale/alerts": 25,
    "/api/whale/top": 25,
}


def _inject_default_limit(tool: dict, query_params: dict) -> dict:
    """Add a default limit ONLY when the tool's schema accepts it and the caller omitted it."""
    default = DEFAULT_LIMITS.get(tool["_path"])
    if default is None:
        return query_params
    props = tool.get("inputSchema", {}).get("properties", {})
    for key in ("limit", "n", "top"):
        if key in props and key not in query_params:
            query_params[key] = default
            break
    return query_params


MAX_RESULT_BYTES = 16384  # raised from 6K so flagship tools aren't truncated by default


def _find_largest_list(obj, _path=()):
    """Return (path_tuple, list) of the largest list found anywhere in obj, or (None, None)."""
    best = (None, None)
    best_len = 0
    if isinstance(obj, list):
        best, best_len = (_path, obj), len(obj)
        for i, v in enumerate(obj):
            p, lst = _find_largest_list(v, _path + (i,))
            if lst is not None and len(lst) > best_len:
                best, best_len = (p, lst), len(lst)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            p, lst = _find_largest_list(v, _path + (k,))
            if lst is not None and len(lst) > best_len:
                best, best_len = (p, lst), len(lst)
    return best


def _set_at(obj, path, value):
    """Mutate obj at the given path tuple (walk to parent, set final key)."""
    ref = obj
    for key in path[:-1]:
        ref = ref[key]
    ref[path[-1]] = value


_TRUNC_HINT = "result too large; pass a smaller limit or more specific filter"


def _cap_response(result):
    """Shrink the largest list until the serialized result fits MAX_RESULT_BYTES."""
    try:
        size = len(json.dumps(result).encode())
    except (TypeError, ValueError):
        return result
    if size <= MAX_RESULT_BYTES:
        return result

    path, lst = _find_largest_list(result)
    if lst is not None and path:
        keep = len(lst)
        while keep > 5:
            keep = max(5, keep // 2)
            _set_at(result, path, lst[:keep])
            if len(json.dumps(result).encode()) <= MAX_RESULT_BYTES:
                break

    if isinstance(result, dict):
        result["_truncated"] = True
        result["_hint"] = _TRUNC_HINT
        return result
    # top-level list / scalar (no mutable dict to stamp)
    return {"_truncated": True, "_hint": _TRUNC_HINT, "data": lst[: max(5, len(lst) // 5)] if lst is not None else None}


# Skip endpoints that are duplicates or internal
SKIP_PATTERNS = {
    "polyclawd_",  # bare root
}

# Friendly category prefixes for tool naming
CATEGORY_ORDER = [
    "signals",
    "portfolio",
    "archetype",
    "markets",
    "vegas",
    "espn",
    "kalshi",
    "manifold",
    "metaculus",
    "predictit",
    "betfair",
    "polyrouter",
    "basket-arb",
    "copy-trade",
    "engine",
    "phase",
    "kelly",
    "alerts",
    "llm",
    "paper",
    "simmer",
    "trading",
    "scan",
    "topics",
    "calculate",
    "rewards",
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
        url,
        data=body,
        method="POST",
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


# ── auto-discovery ───────────────────────────────────────────────────────


def build_tools(spec: dict) -> List[dict]:
    """Filter the OpenAPI spec to the curated ALLOWLIST and emit MCP tool defs.

    Guard order: ALLOWLIST first -> curated name -> dedup on final name.
    """
    tools: List[dict] = []
    seen_names: set = set()
    paths = spec.get("paths", {})
    for path in sorted(paths):
        if path not in ALLOWLIST:  # 1. cheapest filter first
            continue
        endpoint = paths[path].get("get")  # allowlist is GET-only
        if not endpoint:
            continue
        if endpoint.get("security"):  # skip any API-key endpoint
            continue
        meta = TOOL_META.get(path)  # 2. curated name override
        tool_name = meta[0] if meta else _path_to_tool_name(path)
        if tool_name in seen_names:  # 3. dedup keyed on FINAL name
            continue
        seen_names.add(tool_name)
        description = (
            meta[1]
            if meta
            else _path_to_description(path, "get", endpoint.get("summary", ""), endpoint.get("description", ""))
        )
        input_schema = _extract_params(endpoint.get("parameters", []), spec)
        tools.append(
            {
                "name": tool_name,
                "description": description,
                "inputSchema": input_schema,
                "_path": path,
                "_method": "get",
            }
        )
    return tools


def _save_cached_tools(tools):  # replaced in Task 5
    return None


def _load_cached_tools():  # replaced in Task 5
    return []


def discover_tools(base_url: str = None) -> List[dict]:
    """Fetch OpenAPI spec and build curated tools; fall back to cache on failure."""
    url = (base_url or BASE_URL).rstrip("/") + "/api/openapi.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd-MCP/2.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            spec = json.loads(resp.read().decode())
    except Exception as e:
        logger.error("Failed to fetch OpenAPI spec from %s: %s", url, e)
        return _load_cached_tools()
    tools = build_tools(spec)
    _save_cached_tools(tools)
    logger.info("Discovered %d curated MCP tools", len(tools))
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
    return [{k: v for k, v in t.items() if not k.startswith("_")} for t in TOOLS]


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
    query_params = {k: v for k, v in arguments.items() if "{" + k + "}" not in tool["_path"]}

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
    """Run MCP server in stdio mode."""
    _ensure_tools()
    logger.info("Polyclawd MCP Server started — %d tools", len(TOOLS))

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
            send_response(
                id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "serverInfo": {"name": "polyclawd", "version": "2.1.0"},
                    "capabilities": {"tools": {}},
                },
            )

        elif method == "notifications/initialized":
            pass

        elif method == "tools/list":
            send_response(id, {"tools": get_tools()})

        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            result = handle_tool_call(tool_name, arguments)
            send_response(id, {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]})

        else:
            send_error(id, -32601, f"Method not found: {method}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
