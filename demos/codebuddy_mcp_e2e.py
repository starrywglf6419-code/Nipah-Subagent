#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for the CodeBuddy MCP adapter and versioned MCP App panel."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP = os.path.join(ROOT, "mcp", "codebuddy_mcp.py")


def load_module():
    spec = importlib.util.spec_from_file_location("cb_e2e", MCP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    mod = load_module()
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
        {"jsonrpc": "2.0", "id": 4, "method": "resources/read",
         "params": {"uri": mod.PANEL_URI}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "codebuddy_doctor", "arguments": {}}},
    ]
    proc = subprocess.run(
        [sys.executable, MCP],
        input="\n".join(json.dumps(x) for x in requests) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    rows = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
    by_id = {row.get("id"): row for row in rows}
    tools = {x["name"] for x in by_id[2]["result"]["tools"]}
    resources = by_id[3]["result"]["resources"]
    html = by_id[4]["result"]["contents"][0]["text"]
    doctor = by_id[5]["result"]["content"][0]["text"]
    checks = {
        "run_tool": "codebuddy_run" in tools,
        "panel_tool": "codebuddy_panel" in tools,
        "live_tools": {"codebuddy_live_sessions", "codebuddy_live_messages"} <= tools,
        "panel_versioned": mod.PANEL_URI.startswith("ui://codebuddy/monitor/"),
        "resource_uri": resources and resources[0]["uri"] == mod.PANEL_URI,
        "panel_bridge": "codebuddy_live_sessions" in html and "dimagent_live_sessions" not in html,
        "panel_brand": "CodeBuddy · 实时会话" in html,
        "doctor": "codebuddy:" in doctor and "version:" in doctor,
    }
    for key, value in checks.items():
        print(f"{key} {bool(value)}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
