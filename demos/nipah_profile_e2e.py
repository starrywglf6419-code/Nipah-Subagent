#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end check for CodeBuddy account profiles (cn / intl).

派两个极小任务分别走 cn、intl profile，验证：
1. 子进程确实换了账号（返回 *_PROFILE_OK）
2. 会话落进对应 profile 的 projects 目录，并在统一会话列表里带上 profile 标记
3. 未知 profile 会被拒绝，不会静默落到默认账号
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP = os.path.join(ROOT, "mcp", "nipah_subagent_mcp.py")


def load_module():
    spec = importlib.util.spec_from_file_location("nipah_profile_e2e", MCP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    mod = load_module()
    checks = {}
    seen = {}

    for profile in ("cn", "intl"):
        want = profile.upper() + "_PROFILE_OK"
        text = mod.nipah_run({
            "engine": "codebuddy",
            "profile": profile,
            "prompt": f"只回复这一串，不要做别的事：{want}",
            "wait_seconds": 240,
            "cwd": ROOT,
        })
        match = re.search(r"cbjob_[0-9_]+_[0-9a-f]+", text)
        body = mod.nipah_result({"engine": "codebuddy", "job_id": match.group(0)}) if match else ""
        seen[profile] = want in body
        checks[f"run_{profile}"] = seen[profile]
        print(f"--- {profile} ---")
        print(text[:600])
        print("正文：", body.strip()[:200])
        print()

    sessions = mod.combined_sessions(80)
    tagged = {s.get("profile") for s in sessions if s.get("engine") == "codebuddy"}
    checks["sessions_tagged"] = {"cn", "intl"} <= tagged
    checks["messages_reachable"] = True
    for profile in ("cn", "intl"):
        item = next((s for s in sessions
                     if s.get("engine") == "codebuddy" and s.get("profile") == profile), None)
        if not item:
            checks["messages_reachable"] = False
            continue
        data = mod.nipah_live_messages({"source": "codebuddy", "session": item["id"],
                                        "profile": profile})
        if not data.get("ok") or not data.get("messages"):
            checks["messages_reachable"] = False

    bad = mod.nipah_run({"engine": "codebuddy", "profile": "nope",
                         "prompt": "只回复：X", "wait_seconds": 0, "cwd": ROOT})
    checks["unknown_profile_rejected"] = "unknown profile" in bad

    print(json.dumps({"profiles": sorted(tagged), "checks": checks},
                     ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
