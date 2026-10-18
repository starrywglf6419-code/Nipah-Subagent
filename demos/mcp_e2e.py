#!/usr/bin/env python3
"""E2E smoke checks for DimAgent MCP. Run after MCP/viewer changes."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "mcp", "dimagent_mcp.py")


def load_module():
    spec = importlib.util.spec_from_file_location("dimagent_mcp_e2e", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    m = load_module()
    print("version", m.VERSION, "tools", len(m.TOOLS))
    print("default_cwd_valid", bool(m.default_cwd()) and os.path.isdir(m.default_cwd()))
    print("provider_alias", m.resolve_provider_id("magpie"))

    blocked = m.tool_run({"prompt": "x", "model": "definitely-not-a-model"})
    print("unknown_model_blocked", blocked.startswith("错误：模型预检失败"))

    bad_cwd = m.tool_run({
        "prompt": "x", "cwd": "C:/__dimagent_missing_cwd__",
        "provider": "opencode-go", "model": "deepseek-v4.1-flash",
    })
    print("missing_cwd_blocked", bad_cwd.startswith("错误：工作目录不存在"))

    bad_image = m.tool_run({"prompt": "x", "images": ["C:/__missing__.png"]})
    print("missing_image_blocked", bad_image.startswith("错误：图片预检失败"))

    conflict = m.tool_run({
        "prompt": "x",
        "session_id": "sess_missing_for_test",
        "resume": True,
        "model": "qwen3.8-max",
    })
    print("resume_conflict_blocked", "已有会话" in conflict and "model" in conflict)

    missing_sid = m.tool_run({
        "prompt": "x",
        "resume": True,
    })
    print("resume_without_sid_blocked", missing_sid.startswith("错误：resume=true"))

    sessions = m.list_sessions(1)
    if sessions:
        sid = sessions[0]["id"]
        inherited = m.session_cwd(sid)
        prepared, _, prepared_conflicts = m._prepare_run_args({
            "prompt": "x", "session_id": sid,
        })
        print(
            "session_id_cwd_inherited",
            bool(inherited) and not prepared_conflicts and prepared.get("cwd") == inherited,
        )
        sid_override = m.tool_run({
            "prompt": "x", "session_id": sid, "model": "qwen3.8-max",
        })
        print(
            "session_id_override_blocked",
            "已有会话" in sid_override and "model" in sid_override,
        )
        resume_prepared, _, resume_conflicts = m._prepare_run_args({
            "prompt": "x", "session_id": sid, "resume": True,
            "mode": "plan", "reasoning_effort": "xhigh",
            "verify": "python -m py_compile demo.py", "repair_attempts": 1,
        })
        resume_cmd = m.build_exec_cmd(resume_prepared)
        print(
            "resume_runtime_flags_kept",
            not resume_conflicts
            and "--mode" in resume_cmd
            and "--reasoning-effort" in resume_cmd
            and "--verify" in resume_cmd
            and "--repair-attempts" in resume_cmd,
        )
    else:
        print("session_id_cwd_inherited", False)
        print("session_id_override_blocked", False)
        print("resume_runtime_flags_kept", False)

    proc = subprocess.Popen(
        [sys.executable, SCRIPT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8",
    )

    def send(obj):
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def recv():
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP server closed stdout")
        return json.loads(line)

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "dimagent-e2e", "version": "1"},
    }})
    init = recv()
    print("mcp_version", init.get("result", {}).get("serverInfo", {}).get("version"))
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = [x["name"] for x in recv().get("result", {}).get("tools", [])]
    print("has_stop", "dimagent_stop" in tools)
    print("has_resume", "dimagent_resume" in tools)
    print("has_feedback", "dimagent_feedback" in tools)

    send({"jsonrpc": "2.0", "id": 5, "method": "resources/list", "params": {}})
    resources = recv().get("result", {}).get("resources", [])
    panel_uri = resources[0].get("uri") if resources else ""
    print("panel_resource_versioned", panel_uri.startswith("ui://dimagent/monitor/"))
    if panel_uri:
        send({"jsonrpc": "2.0", "id": 6, "method": "resources/read", "params": {"uri": panel_uri}})
        panel_text = recv().get("result", {}).get("contents", [{}])[0].get("text", "")
        print("panel_resource_current", "project-group" in panel_text and "finalOnlyBtn" in panel_text)
    else:
        print("panel_resource_current", False)

    send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
        "name": "dimagent_doctor", "arguments": {},
    }})
    doctor = recv().get("result", {})
    doctor_text = (doctor.get("content") or [{}])[0].get("text") or ""
    print("doctor_has_version", f"MCP版本  : {m.VERSION}" in doctor_text)
    print("doctor_has_web_default", "联网默认 : 禁用" in doctor_text)

    send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "dimagent_feedback",
        "arguments": {
            "title": "[E2E] feedback entry smoke",
            "scenario": "automated smoke",
            "expected": "entry written",
            "actual": "entry written",
            "evidence": "demos/mcp_e2e.py",
            "severity": "low",
        },
    }})
    fb = recv().get("result", {})
    print("feedback_ok", "反馈已写入" in ((fb.get("content") or [{}])[0].get("text") or ""))

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
