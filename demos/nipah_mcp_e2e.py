#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Protocol smoke test for the unified Nipah Subagent MCP facade."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP = os.path.join(ROOT, "mcp", "nipah_subagent_mcp.py")


def load_module():
    spec = importlib.util.spec_from_file_location("nipah_e2e", MCP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    mod = load_module()
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "nipah-e2e", "version": "1"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
        {"jsonrpc": "2.0", "id": 4, "method": "resources/read",
         "params": {"uri": mod.PANEL_URI}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "nipah_providers", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "nipah_live_sessions", "arguments": {}}},
    ]
    proc = subprocess.run(
        [sys.executable, MCP],
        input="\n".join(json.dumps(x, ensure_ascii=False) for x in requests) + "\n",
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    rows = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
    by_id = {row.get("id"): row for row in rows}
    tools = {x["name"] for x in by_id[2]["result"]["tools"]}
    resources = by_id[3]["result"]["resources"]
    html = by_id[4]["result"]["contents"][0]["text"]
    providers = by_id[5]["result"]["content"][0]["text"]
    live = by_id[6]["result"]["structuredContent"]
    sources = {x.get("source") for x in live.get("sessions", [])}
    checks = {
        "run_tool": "nipah_run" in tools,
        "control_tools": {"nipah_status", "nipah_wait", "nipah_result", "nipah_stop", "nipah_resume"} <= tools,
        "panel_tool": "nipah_panel" in tools,
        "live_tools": {"nipah_live_sessions", "nipah_live_messages"} <= tools,
        "feedback_tool": "nipah_feedback" in tools,
        "panel_versioned": mod.PANEL_URI.startswith("ui://nipah/subagent/"),
        "resource_uri": bool(resources) and resources[0]["uri"] == mod.PANEL_URI,
        "panel_bridge": "nipah_live_sessions" in html and "nipah_live_messages" in html,
        "panel_engine_switch": "engineSwitch" in html and "engine-btn" in html,
        "panel_brand": "Nipah Subagent" in html,
        "providers": "engine=dimagent" in providers and "engine=codebuddy" in providers,
        "both_sources": {"dimagent", "codebuddy"} <= sources,
    }
    for key, value in checks.items():
        print(f"{key} {bool(value)}")
    if proc.stderr.strip():
        print("stderr", proc.stderr.strip()[:1000])
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
