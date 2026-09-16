#!/usr/bin/env python3
"""
Launcher shim for server_fastmcp.py.

OpenClaw's MCP config pins /usr/bin/python3 as the interpreter, but the
official `mcp` Python SDK is installed in the Homebrew-managed Python 3.12
framework. This wrapper re-execs the target server under that interpreter
so the config does not need to change.
"""
import os
import sys

TARGET_PYTHON = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
TARGET_SCRIPT = os.path.join(os.path.dirname(__file__), "server_fastmcp.py")

os.execv(TARGET_PYTHON, [TARGET_PYTHON, TARGET_SCRIPT] + sys.argv[1:])
