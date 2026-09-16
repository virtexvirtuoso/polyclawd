#!/usr/bin/env python3
"""
Polyclawd MCP Server - HTTP Transport (FastMCP)
Auto-discovers tools from Polyclawd API OpenAPI spec.
"""

import json
import argparse
import os
import importlib.util
from typing import Optional

from app import mcp

BASE_URL = "http://localhost:8420"

# Import auto-discovery from server.py
_server_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
_spec = importlib.util.spec_from_file_location("server", _server_path)
_server_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_server_mod)

# Point server module at localhost for internal calls
_server_mod.BASE_URL = BASE_URL

# Discover tools from OpenAPI spec (hits localhost:8420/openapi.json)
TOOLS = _server_mod.discover_tools(BASE_URL)
_server_mod.TOOLS = TOOLS
_server_mod._TOOL_MAP = {t["name"]: t for t in TOOLS}

print(f"✅ Auto-discovered {len(TOOLS)} tools from OpenAPI spec")


def _call_tool(name: str, arguments: dict) -> str:
    result = _server_mod.handle_tool_call(name, arguments)
    return json.dumps(result, indent=2)


# ── Register tools with FastMCP ──────────────────────────────────────────

# Group 1: No-parameter tools (bulk register)
_no_param_tools = [t for t in TOOLS if not t.get("inputSchema", {}).get("properties")]

for tool_def in _no_param_tools:
    name = tool_def["name"]
    desc = tool_def.get("description", name)
    # Create async function via closure
    def _make_fn(n):
        async def fn() -> str:
            return _call_tool(n, {})
        fn.__name__ = n
        fn.__doc__ = desc
        return fn
    mcp.tool(name=name)(_make_fn(name))

# Group 2: Tools with parameters — generate typed functions via exec
_param_tools = [t for t in TOOLS if t.get("inputSchema", {}).get("properties")]

for tool_def in _param_tools:
    name = tool_def["name"]
    desc = tool_def.get("description", name).replace('"', '\\"')
    props = tool_def["inputSchema"].get("properties", {})
    req_params = set(tool_def["inputSchema"].get("required", []))

    # Build typed parameter list
    params = []
    for pname, pschema in props.items():
        ptype = pschema.get("type", "string")
        py_type = {"string": "str", "integer": "int", "number": "float", "boolean": "bool"}.get(ptype, "str")
        default = pschema.get("default")
        if pname in req_params:
            params.append(f"{pname}: {py_type}")
        elif default is not None:
            params.append(f"{pname}: {py_type} = {repr(default)}")
        else:
            params.append(f"{pname}: str = ''")

    param_str = ", ".join(params)
    # Collect args into dict
    arg_names = list(props.keys())
    args_dict = "{" + ", ".join(f'"{k}": {k}' for k in arg_names) + "}"

    code = f'''
async def {name}({param_str}) -> str:
    """{desc}"""
    args = {args_dict}
    # Strip empty optional args
    args = {{k: v for k, v in args.items() if v != "" and v is not None}}
    return _call_tool("{name}", args)
mcp.tool(name="{name}")({name})
'''
    try:
        exec(code, {"_call_tool": _call_tool, "mcp": mcp})
    except Exception as e:
        print(f"⚠️ Failed to register {name}: {e}")


print(f"✅ Registered {len(_no_param_tools)} no-param + {len(_param_tools)} parameterized tools with FastMCP")


def main():
    parser = argparse.ArgumentParser(description="Polyclawd MCP HTTP Server")
    parser.add_argument("--transport", choices=["sse", "streamable-http"], default="streamable-http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8421)
    args = parser.parse_args()

    print(f"🚀 Starting Polyclawd MCP Server")
    print(f"   Transport: {args.transport}")
    print(f"   Endpoint: http://{args.host}:{args.port}/mcp")
    print(f"   API Backend: {BASE_URL}")
    print(f"   Tools: {len(TOOLS)}")

    mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
