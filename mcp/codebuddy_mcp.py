#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CodeBuddy Code subagent MCP adapter.

Reuses the DimAgent Subagent viewer and MCP protocol, but keeps CodeBuddy jobs,
event streams and sessions in a separate data namespace.
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
VIEWER_FILE = os.path.join(PROJECT_ROOT, "dim-live-viewer", "index.html")
PANEL_FILE = os.path.join(HERE, "panel.html")
VIEWER_SERVER = os.path.join(PROJECT_ROOT, "codebuddy-live-viewer", "server.py")
PANEL_MIME = "text/html;profile=mcp-app"
LOCAL_APP_DATA = os.environ.get("LOCALAPPDATA") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local"
)
DATA_DIR = os.environ.get("CODEBUDDY_SUBAGENT_DATA") or os.path.join(
    LOCAL_APP_DATA, "CodeBuddySubagent"
)
RUNS_DIR = os.path.join(DATA_DIR, "runs")
WORKSPACE_DIR = os.path.join(DATA_DIR, "workspace")
IS_WINDOWS = os.name == "nt"


def _panel_revision() -> str:
    digest = hashlib.sha256()
    for path in (VIEWER_FILE, VIEWER_SERVER, os.path.abspath(__file__)):
        try:
            with open(path, "rb") as fh:
                digest.update(fh.read())
        except OSError:
            pass
    return digest.hexdigest()[:12] or "fallback"


PANEL_URI = "ui://codebuddy/monitor/" + _panel_revision()


def _load_dimagent_module():
    path = os.path.join(HERE, "dimagent_mcp.py")
    spec = importlib.util.spec_from_file_location("dimagent_mcp_for_codebuddy", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DA = _load_dimagent_module()

DEFAULTS = {
    "codebuddy_bin": "",
    "default_cwd": "",
    "default_provider": "magpie",
    "default_model": "group/auto-deepseek-v4-1-flash",
    "default_reasoning_effort": "max",
    "default_policy": "workspace-write",
    "max_wait_seconds": 240,
    "stale_after_seconds": 90,
    "default_disallow_web": True,
    "viewer_url": "http://127.0.0.1:8791/",
    "viewer_script": os.path.join(PROJECT_ROOT, "codebuddy-live-viewer", "server.py"),
    "viewer_port": 8791,
    "auto_start_viewer": True,
    "runs_dir": RUNS_DIR,
    "search_globs": [
        os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "npm", "codebuddy.cmd"),
        os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "npm", "codebuddy.ps1"),
        os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "npm", "codebuddy"),
    ],
}


def _load_config() -> dict:
    cfg = dict(DEFAULTS)
    candidates = [
        os.environ.get("CODEBUDDY_SUBAGENT_CONFIG", ""),
        os.path.join(os.path.expanduser("~"), ".codebuddy-subagent", "config.json"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    cfg.update(data)
                    cfg["_config_file"] = path
            except (OSError, ValueError):
                pass
    if os.environ.get("CODEBUDDY_BIN"):
        cfg["codebuddy_bin"] = os.environ["CODEBUDDY_BIN"]
    return cfg


CFG = _load_config()
_bin_cache = {"path": "", "source": ""}
_live_procs: dict = {}


def _ensure_dirs() -> None:
    for path in (DATA_DIR, RUNS_DIR, WORKSPACE_DIR):
        os.makedirs(path, exist_ok=True)


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(str(value or "")))


def _newest(paths):
    found = [p for p in paths if p and os.path.isfile(p)]
    return max(found, key=os.path.getmtime) if found else ""


def resolve_codebuddy(force: bool = False):
    if not force and _bin_cache["path"] and os.path.isfile(_bin_cache["path"]):
        return _bin_cache["path"], _bin_cache["source"]
    explicit = _expand(str(CFG.get("codebuddy_bin") or ""))
    if explicit and os.path.isfile(explicit):
        _bin_cache.update(path=explicit, source="config")
        return explicit, "config"
    for name in ("codebuddy.cmd", "codebuddy.ps1", "codebuddy", "cbc"):
        found = shutil.which(name)
        if found:
            _bin_cache.update(path=found, source="PATH")
            return found, "PATH"
    for pattern in CFG.get("search_globs") or []:
        hit = _newest(__import__("glob").glob(_expand(pattern), recursive=True))
        if hit:
            _bin_cache.update(path=hit, source="scan")
            return hit, "scan"
    raise RuntimeError(
        "Cannot find CodeBuddy CLI (codebuddy). Install @tencent-ai/codebuddy-code "
        "or set CODEBUDDY_BIN / ~/.codebuddy-subagent/config.json."
    )


def _run_codebuddy(argv: list[str], timeout: float = 120) -> subprocess.CompletedProcess:
    path = resolve_codebuddy()[0]
    if path.lower().endswith(".ps1"):
        cmd = ["pwsh", "-NoLogo", "-NoProfile", "-File", path] + argv
    else:
        cmd = [path] + argv
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0,
    )


def codebuddy_version() -> str:
    try:
        proc = _run_codebuddy(["--version"], timeout=60)
        return (proc.stdout or proc.stderr or "").strip().splitlines()[0] if (proc.stdout or proc.stderr) else ""
    except Exception as exc:  # noqa: BLE001
        return f"(read failed: {exc})"


def _models() -> list[str]:
    try:
        proc = _run_codebuddy(["--help"], timeout=60)
    except Exception:
        return []
    text = proc.stdout or ""
    marker = "Currently supported: ("
    pos = text.find(marker)
    if pos < 0:
        return []
    end = text.find(")", pos)
    if end < 0:
        return []
    return [x.strip() for x in text[pos + len(marker):end].split(",") if x.strip()]


def _project_dir(cwd: str) -> str:
    # CodeBuddy uses a compressed project folder name; scanning by cwd avoids
    # depending on that private naming convention.
    root = os.path.join(os.path.expanduser("~"), ".codebuddy", "projects")
    if not os.path.isdir(root):
        return ""
    target = os.path.normcase(os.path.abspath(cwd))
    candidates = []
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        for file_name in os.listdir(path):
            if not file_name.endswith(".jsonl"):
                continue
            fp = os.path.join(path, file_name)
            try:
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    for _, line in zip(range(4), fh):
                        obj = json.loads(line)
                        if os.path.normcase(os.path.abspath(obj.get("cwd") or "")) == target:
                            candidates.append(fp)
                            break
            except (OSError, ValueError):
                continue
            if candidates:
                break
        if candidates:
            break
    return os.path.dirname(candidates[0]) if candidates else ""


def _jsonl(path: str) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    out.append(value)
    except OSError:
        pass
    return out


def _stream_events(path: str) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        pass
    return out


def _stream_summary(path: str) -> dict:
    info = {
        "status": "starting", "sessionId": "", "text": "", "thinkingChars": 0,
        "tools": [], "toolCalls": 0, "usage": {}, "error": "", "lastEvent": "",
    }
    pending = {}
    for obj in _stream_events(path):
        typ = str(obj.get("type") or "")
        sid = obj.get("session_id") or obj.get("sessionId") or ""
        if sid:
            info["sessionId"] = sid
        info["lastEvent"] = typ
        if typ == "system" and obj.get("subtype") == "init":
            if info["status"] not in ("completed", "failed", "cancelled", "stale"):
                info["status"] = "running"
        elif typ == "stream_event":
            ev = obj.get("event") or {}
            et = ev.get("type") or ""
            if et == "content_block_start":
                block = ev.get("content_block") or {}
                if block.get("type") == "tool_use":
                    item = {
                        "name": block.get("name") or "tool",
                        "input": block.get("input") or {},
                        "callId": block.get("id") or "",
                        "status": "running",
                    }
                    pending[item["callId"]] = item
                    info["tools"].append(item)
                    info["toolCalls"] += 1
            elif et == "content_block_delta":
                delta = ev.get("delta") or {}
                if delta.get("type") == "thinking_delta":
                    info["thinkingChars"] += len(delta.get("thinking") or "")
                elif delta.get("type") == "text_delta":
                    info["text"] += delta.get("text") or ""
                elif delta.get("type") == "input_json_delta":
                    pass
        elif typ == "assistant":
            msg = obj.get("message") or {}
            for block in msg.get("content") or []:
                if block.get("type") == "tool_use":
                    cid = block.get("id") or ""
                    item = pending.get(cid)
                    if item is None:
                        item = {"name": block.get("name") or "tool", "callId": cid,
                                "status": "running", "input": {}}
                        pending[cid] = item
                        info["tools"].append(item)
                        info["toolCalls"] += 1
                    item["input"] = block.get("input") or {}
                elif block.get("type") == "text":
                    info["text"] = block.get("text") or info["text"]
        elif typ == "user":
            msg = obj.get("message") or {}
            for block in msg.get("content") or []:
                if block.get("type") == "tool_result":
                    cid = block.get("tool_use_id") or ""
                    item = pending.get(cid)
                    if item:
                        item["status"] = "done"
                        item["result"] = block.get("content")
        elif typ == "result":
            info["status"] = "completed" if obj.get("subtype") == "success" else "failed"
            info["text"] = str(obj.get("result") or info["text"])
            info["usage"] = obj.get("usage") or {}
            if obj.get("errors"):
                info["error"] = " ".join(str(x) for x in obj["errors"])[:2000]
    return info


def _session_messages(session_file: str) -> list[dict]:
    rows = _jsonl(session_file)
    out = []
    for row in rows:
        typ = row.get("type")
        if typ == "message":
            role = row.get("role") or "assistant"
            parts = []
            for block in row.get("content") or []:
                bt = block.get("type")
                if bt in ("input_text", "output_text", "text"):
                    parts.append({"type": "text", "text": block.get("text") or ""})
            if parts:
                out.append({
                    "messageId": row.get("id") or uuid.uuid4().hex,
                    "role": role,
                    "parts": parts,
                    "createdAt": _iso(row.get("timestamp")),
                    "metadata": {"_codebuddy": {"type": typ}},
                })
        elif typ == "reasoning":
            text = "".join(x.get("text") or "" for x in row.get("rawContent") or []
                           if x.get("type") == "reasoning_text")
            if text:
                out.append({
                    "messageId": row.get("id") or uuid.uuid4().hex,
                    "role": "assistant",
                    "parts": [{"type": "thinking", "thinking": text}],
                    "createdAt": _iso(row.get("timestamp")),
                    "metadata": {"_codebuddy": {"type": typ}},
                })
        elif typ == "function_call":
            try:
                args = json.loads(row.get("arguments") or "{}")
            except ValueError:
                args = {"raw": row.get("arguments")}
            out.append({
                "messageId": row.get("id") or uuid.uuid4().hex,
                "role": "assistant",
                "parts": [{"type": "tool_use", "id": row.get("callId") or row.get("id"),
                           "name": row.get("name") or "tool", "input": args}],
                "createdAt": _iso(row.get("timestamp")),
                "metadata": {"_codebuddy": {"type": typ}},
            })
        elif typ == "function_call_result":
            output = row.get("output")
            content = output.get("text") if isinstance(output, dict) else str(output or "")
            out.append({
                "messageId": row.get("id") or uuid.uuid4().hex,
                "role": "tool_result",
                "parts": [{"type": "tool_result", "tool_use_id": row.get("callId"),
                           "content": content, "is_error": row.get("status") == "failed"}],
                "createdAt": _iso(row.get("timestamp")),
                "metadata": {"_codebuddy": {"type": typ}},
            })
    return out


def _iso(value):
    if not value:
        return ""
    try:
        ms = int(value)
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ms / 1000.0))
    except (TypeError, ValueError):
        return str(value)


def fetch_sessions(limit: int = 200) -> list[dict]:
    root = os.path.join(os.path.expanduser("~"), ".codebuddy", "projects")
    items = []
    if not os.path.isdir(root):
        return items
    for project in os.listdir(root):
        pdir = os.path.join(root, project)
        if not os.path.isdir(pdir):
            continue
        for name in os.listdir(pdir):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(pdir, name)
            rows = _jsonl(path)
            if not rows:
                continue
            sid = name[:-6]
            cwd = ""
            title = ""
            last_at = ""
            model = ""
            count = 0
            for row in rows:
                cwd = row.get("cwd") or cwd
                if row.get("type") == "message":
                    count += 1
                    role = row.get("role")
                    text = ""
                    for block in row.get("content") or []:
                        if block.get("type") in ("input_text", "output_text", "text"):
                            text += block.get("text") or ""
                    if role == "user" and not title:
                        title = " ".join(text.split())[:100]
                    model = ((row.get("providerData") or {}).get("model")
                             or row.get("providerData", {}).get("requestModelId") or model)
                    last_at = _iso(row.get("timestamp")) or last_at
            if not title:
                title = sid
            items.append({
                "id": sid, "cwd": cwd or "", "title": title,
                "status": "stored", "messageCount": count,
                "createdAt": _iso(rows[0].get("timestamp")),
                "updatedAt": last_at, "lastAt": last_at,
                "providerId": "codebuddy", "modelId": model,
                "mode": "agent", "isSubagent": False,
                "parentSessionId": None, "subagentType": None,
                "subagentPrompt": title, "usage": None,
                "jobRunning": False, "jobStatus": "stored",
                "_codebuddy": True, "_sessionFile": path,
            })
    items.sort(key=lambda x: x.get("lastAt") or "", reverse=True)
    return items[:max(1, int(limit))]


def fetch_messages(session: str, offset: int = 0) -> dict:
    sessions = fetch_sessions(1000)
    item = next((x for x in sessions if x["id"] == session), None)
    if not item:
        return {"total": 0, "messages": []}
    messages = _session_messages(item["_sessionFile"])
    return {"total": len(messages), "messages": messages[max(0, int(offset)):]}


def _job_meta_path(job_id: str) -> str:
    return os.path.join(RUNS_DIR, job_id + ".json")


def _job_stream_path(job_id: str) -> str:
    return os.path.join(RUNS_DIR, job_id + ".jsonl")


def _job_process_path(job_id: str) -> str:
    return os.path.join(RUNS_DIR, job_id + ".process.log")


def _job_save(meta: dict) -> None:
    _ensure_dirs()
    with open(_job_meta_path(meta["jobId"]), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)


def _job_load(job_id: str):
    try:
        with open(_job_meta_path(job_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _job_list(limit: int = 50):
    _ensure_dirs()
    paths = [
        os.path.join(RUNS_DIR, x) for x in os.listdir(RUNS_DIR)
        if x.startswith(("job_", "cbjob_")) and x.endswith(".json")
    ]
    paths.sort(key=os.path.getmtime, reverse=True)
    out = []
    for path in paths[:limit]:
        try:
            with open(path, encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            pass
    return out


def _newest_job():
    rows = _job_list(1)
    return rows[0] if rows else None


def _find_job(session_id: str = "", job_id: str = ""):
    if job_id:
        return _job_load(job_id)
    for row in _job_list(200):
        if row.get("sessionId") == session_id:
            return row
    return None


def _pid_alive(pid) -> bool:
    try:
        return DA._pid_alive(int(pid))
    except Exception:
        return False


def _job_status(job: dict) -> dict:
    stream = _job_stream_path(job["jobId"])
    info = _stream_summary(stream)
    proc = _live_procs.get(job["jobId"])
    code = proc.poll() if proc is not None else None
    alive = code is None if proc is not None else _pid_alive(job.get("pid"))
    if info["status"] in ("starting", "running"):
        if code is not None:
            info["status"] = "completed" if code == 0 else "failed"
            info["exitCode"] = code
        elif alive:
            info["status"] = "running"
        else:
            try:
                idle = time.time() - os.path.getmtime(stream)
            except OSError:
                idle = time.time() - float(job.get("startedAtEpoch") or 0)
            info["status"] = "stale" if idle > float(CFG["stale_after_seconds"]) else "starting"
    info.update({
        "jobId": job["jobId"], "pid": job.get("pid"), "pidAlive": alive,
        "cwd": job.get("cwd"), "sessionId": info.get("sessionId") or job.get("sessionId") or "",
        "effectiveModel": info.get("model") or job.get("model"),
        "requestedModel": job.get("model"),
    })
    if info["sessionId"] and job.get("sessionId") != info["sessionId"]:
        job["sessionId"] = info["sessionId"]
        _job_save(job)
    return info


def _build_cmd(args: dict) -> list[str]:
    cmd = [
        "codebuddy", "-p", "--output-format", "stream-json",
        "--include-partial-messages", "--verbose",
    ]
    if args.get("resume_session"):
        cmd += ["--resume", args["resume_session"]]
    else:
        if args.get("model"):
            cmd += ["--model", args["model"]]
        if args.get("reasoning_effort"):
            cmd += ["--effort", args["reasoning_effort"]]
        if args.get("policy") == "read-only":
            # CodeBuddy 2.168.3 has a headless bug: --permission-mode and
            # --disallowedTools can reset model/tools to unknown. A tool
            # whitelist works, so read-only is expressed that way.
            cmd += ["--tools", "Read,Glob,Grep"]
    cmd.append(args["prompt"])
    return cmd


def _run_path(bin_path: str) -> str:
    # npm installs .cmd/.ps1 shims; Popen needs the cmd shim on Windows.
    if IS_WINDOWS and bin_path.lower().endswith(".ps1"):
        return "pwsh"
    return bin_path


def start_job(args: dict) -> dict:
    _ensure_dirs()
    cwd = _expand(str(args.get("cwd") or CFG.get("default_cwd") or os.getcwd()))
    if not os.path.isdir(cwd):
        raise RuntimeError(f"cwd does not exist: {cwd}")
    job_id = "cbjob_" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    stream = _job_stream_path(job_id)
    process_log = _job_process_path(job_id)
    argv = _build_cmd(args)
    bin_path, _ = resolve_codebuddy()
    if bin_path.lower().endswith(".ps1"):
        argv = ["pwsh", "-NoLogo", "-NoProfile", "-File", bin_path] + argv[1:]
    else:
        argv[0] = bin_path
    meta = {
        "jobId": job_id, "sessionId": args.get("resume_session") or "",
        "cmd": argv, "cwd": cwd, "model": args.get("model") or CFG.get("default_model"),
        "provider": args.get("provider") or CFG.get("default_provider"),
        "reasoningEffort": args.get("reasoning_effort") or CFG.get("default_reasoning_effort"),
        "policy": args.get("policy") or CFG.get("default_policy"),
        "prompt": args["prompt"], "startedAtEpoch": time.time(),
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "logFile": stream, "processLog": process_log, "pid": None,
    }
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
    with open(stream, "w", encoding="utf-8", errors="replace") as out, \
         open(process_log, "w", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
            creationflags=creation, close_fds=True,
        )
    _live_procs[job_id] = proc
    meta["pid"] = proc.pid
    _job_save(meta)
    return meta


def wait_for_job(job: dict, timeout: float, poll: float = 2.0) -> dict:
    timeout = max(0.0, min(float(timeout or 0), float(CFG["max_wait_seconds"])))
    deadline = time.time() + timeout
    while True:
        info = _job_status(job)
        if info["status"] in ("completed", "failed", "cancelled", "stale"):
            return info
        if time.time() >= deadline:
            return info
        time.sleep(max(0.5, poll))


def _ensure_viewer() -> dict:
    # The panel itself is shared; reuse the existing DimAgent viewer lifecycle.
    url = str(CFG.get("viewer_url") or "")
    try:
        DA.CFG["viewer_url"] = url
        DA.CFG["viewer_script"] = CFG["viewer_script"]
        DA.CFG["viewer_port"] = CFG["viewer_port"]
        DA.CFG["auto_start_viewer"] = CFG["auto_start_viewer"]
        return DA.ensure_viewer()
    except Exception as exc:
        return {"running": False, "url": url, "started": False, "note": str(exc)}


def _live_sessions() -> dict:
    sessions = fetch_sessions(1000)
    for session in sessions:
        job = _find_job(session_id=session["id"])
        if job:
            info = _job_status(job)
            session.update({
                "jobId": job["jobId"], "jobRunning": info["status"] == "running",
                "jobStatus": info["status"], "lastPreview": info.get("text", "")[-200:],
                "pendingTool": any(x.get("status") == "running" for x in info.get("tools", [])),
                "pendingToolName": next((x.get("name") for x in info.get("tools", [])
                                         if x.get("status") == "running"), None),
            })
    return {"ok": True, "now": None, "sessions": sessions}


def tool_run(args: dict) -> str:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return "错误：prompt 不能为空。"
    prepared = dict(args)
    prepared["prompt"] = prompt
    prepared["model"] = str(args.get("model") or CFG.get("default_model"))
    prepared["reasoning_effort"] = str(args.get("reasoning_effort") or CFG.get("default_reasoning_effort"))
    prepared["policy"] = str(args.get("policy") or CFG.get("default_policy"))
    if not args.get("allow_web") and CFG.get("default_disallow_web", True):
        # The CLI's --disallowedTools flag currently breaks headless model
        # resolution, so enforce the default at the prompt boundary.
        prepared["prompt"] = (
            prepared["prompt"].rstrip()
            + "\n\n[运行约束] 不要使用 WebSearch 或 WebFetch；只使用本地文件、命令和已有上下文。"
        )
    meta = start_job(prepared)
    viewer = _ensure_viewer()
    wait_seconds = min(float(args.get("wait_seconds") or 0), float(CFG["max_wait_seconds"]))
    info = wait_for_job(meta, wait_seconds) if wait_seconds else _job_status(meta)
    lines = [
        f"已派发到 CodeBuddy：{meta['jobId']}",
        f"  工作目录 : {meta['cwd']}",
        f"  请求参数 : {meta.get('provider') or 'magpie'} / {meta['model']} / effort {meta['reasoningEffort']} / policy {meta['policy']}",
        f"  事件流   : {meta['logFile']}",
        f"  sessionId: {info.get('sessionId') or '（尚未产生）'}",
        f"  状态     : {info['status']}",
        f"  实时监控 : {viewer.get('url')}",
    ]
    if info.get("error"):
        lines.append("  错误     : " + info["error"][:1200])
    return "\n".join(lines)


def tool_status(args: dict) -> str:
    job = _job_load(args["job_id"]) if args.get("job_id") else (
        _find_job(session_id=args.get("session_id") or "") if args.get("session_id") else _newest_job())
    if not job:
        return "没有找到对应 CodeBuddy 任务。"
    info = _job_status(job)
    lines = [
        f"任务     : {job['jobId']}",
        f"状态     : {info['status']}",
        f"sessionId: {info.get('sessionId') or '-'}",
        f"目录     : {job.get('cwd')}",
        f"供应商   : {job.get('provider') or CFG.get('default_provider')}",
        f"模型     : {info.get('effectiveModel') or job.get('model')}",
        f"工具调用 : {info.get('toolCalls', 0)}",
        f"思考字符 : {info.get('thinkingChars', 0)}",
    ]
    if info.get("tools"):
        lines.append("最近工具 : " + " -> ".join(
            f"{x.get('name')}[{x.get('status')}]" for x in info["tools"][-4:]))
    if info.get("text"):
        tail = str(args.get("tail_chars") or 1200)
        lines += ["", f"当前输出（末 {tail} 字）：", info["text"][-int(tail):]]
    if info.get("error"):
        lines += ["", "错误：", info["error"][:1600]]
    return "\n".join(lines)


def tool_wait(args: dict) -> str:
    job = _job_load(args["job_id"]) if args.get("job_id") else (
        _find_job(session_id=args.get("session_id") or "") if args.get("session_id") else _newest_job())
    if not job:
        return "没有找到对应 CodeBuddy 任务。"
    info = wait_for_job(job, float(args.get("timeout_seconds") or 120),
                        float(args.get("poll_seconds") or 2.0))
    if info["status"] not in ("completed", "failed", "cancelled", "stale"):
        return f"还在跑：{job['jobId']}，状态 {info['status']}，工具调用 {info.get('toolCalls', 0)}。"
    body = info.get("text") or "（没有最终文本）"
    limit = int(args.get("max_chars") or 8000)
    if len(body) > limit:
        body = body[:limit] + f"\n…（已截断，共 {len(body)} 字）"
    return f"任务结束：{info['status']}\n\n{body}"


def tool_result(args: dict) -> str:
    job = _job_load(args["job_id"]) if args.get("job_id") else (
        _find_job(session_id=args.get("session_id") or "") if args.get("session_id") else _newest_job())
    if not job:
        return "没有找到对应 CodeBuddy 任务。"
    info = _job_status(job)
    body = info.get("text") or "（没有最终文本）"
    return f"sessionId: {info.get('sessionId') or '-'}\n状态: {info['status']}\n\n{body}"


def tool_stop(args: dict) -> str:
    job = _job_load(args["job_id"]) if args.get("job_id") else (
        _find_job(session_id=args.get("session_id") or "") if args.get("session_id") else _newest_job())
    if not job:
        return "没有找到对应 CodeBuddy 任务。"
    proc = _live_procs.get(job["jobId"])
    pid = job.get("pid")
    if not (proc is not None and proc.poll() is None) and not _pid_alive(pid):
        return f"任务 {job['jobId']} 已经结束。"
    try:
        if proc is not None:
            proc.kill()
        elif IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
        else:
            os.kill(int(pid), 9)
    except Exception as exc:  # noqa: BLE001
        return f"停止失败：{exc}"
    job["stopRequested"] = "force" if args.get("force") else "graceful"
    _job_save(job)
    return f"已停止 {job['jobId']}（pid {pid}）。"


def tool_sessions(args: dict) -> str:
    sessions = fetch_sessions(int(args.get("limit") or 15))
    if not sessions:
        return "CodeBuddy 里还没有会话。"
    lines = [f"最近 {len(sessions)} 个 CodeBuddy 会话：", ""]
    for item in sessions:
        lines.append(f"- {item['id']} [{item.get('modelId') or '-'}] {item['title']}")
        lines.append(f"    {item['messageCount']} 条  {item.get('cwd') or '-'}")
    return "\n".join(lines)


def tool_viewer(args: dict) -> str:
    info = _ensure_viewer()
    return (f"实时监控页：{info.get('url')} "
            f"{'运行中' if info.get('running') else '未运行'}"
            + (f"（{info.get('note')}）" if info.get("note") else ""))


def tool_doctor(args: dict) -> str:
    lines = [f"CodeBuddy adapter {DA.VERSION} · pid {os.getpid()}"]
    try:
        path, source = resolve_codebuddy(True)
        lines += [f"codebuddy: {path}", f"source: {source}", f"version: {codebuddy_version()}"]
    except Exception as exc:  # noqa: BLE001
        lines.append(f"codebuddy: missing ({exc})")
    lines += [
        f"models: {', '.join(_models()[:8]) or '-'}",
        f"default: {CFG.get('default_provider')} / {CFG.get('default_model')} / effort {CFG.get('default_reasoning_effort')}",
        f"runs: {RUNS_DIR}",
        f"viewer: {_ensure_viewer().get('url')}",
    ]
    return "\n".join(lines)


def tool_cli(args: dict) -> str:
    raw = args.get("args")
    if isinstance(raw, str):
        argv = raw.split()
    elif isinstance(raw, list):
        argv = [str(x) for x in raw]
    else:
        return "错误：args 需要是数组或字符串。"
    try:
        proc = _run_codebuddy(argv, timeout=float(args.get("timeout_seconds") or 120))
    except subprocess.TimeoutExpired:
        return "命令超时。"
    limit = int(args.get("max_chars") or 12000)
    body = f"$ codebuddy {' '.join(argv)}\nexit={proc.returncode}\n\n"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    body += out[:limit]
    if err:
        body += "\n\n[stderr]\n" + err[:limit]
    return body


def tool_live_sessions(args: dict) -> dict:
    return _live_sessions()


def tool_live_messages(args: dict) -> dict:
    session = str(args.get("session") or "").strip()
    if not session:
        return {"ok": False, "error": "missing session"}
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    return {"ok": True, **fetch_messages(session, offset)}


def panel_html() -> str:
    """Shared viewer HTML, with CodeBuddy brand and host-bridge tool names."""
    try:
        with open(VIEWER_FILE, encoding="utf-8") as fh:
            html = fh.read()
    except OSError as exc:
        return "<!doctype html><meta charset='utf-8'><body>CodeBuddy panel load failed: " + str(exc) + "</body>"
    html = html.replace("DimAgent · 实时会话", "CodeBuddy · 实时会话")
    html = html.replace('<span class="name">DimAgent</span>', '<span class="name">CodeBuddy</span>')
    html = html.replace("只读视图 · dimcode.sqlite", "只读视图 · CodeBuddy 会话")
    html = html.replace("正在连接 DimAgent 数据", "正在连接 CodeBuddy 数据")
    html = html.replace(
        "globalThis.NIPAH_LIVE_SESSIONS_TOOL || 'nipah_live_sessions'",
        "'codebuddy_live_sessions'",
    ).replace(
        "globalThis.NIPAH_LIVE_MESSAGES_TOOL || 'nipah_live_messages'",
        "'codebuddy_live_messages'",
    )
    return html


def tool_panel(args: dict) -> str:
    sessions = fetch_sessions(int(args.get("limit") or 12))
    viewer = _ensure_viewer()
    return json.dumps({
        "ok": True,
        "provider": "codebuddy",
        "version": codebuddy_version(),
        "viewer": viewer,
        "sessions": [
            {
                "id": item["id"], "title": item["title"],
                "messages": item["messageCount"], "model": item.get("modelId"),
                "jobStatus": item.get("jobStatus"),
            }
            for item in sessions
        ],
    }, ensure_ascii=False)


TOOLS = [
    {
        "name": "codebuddy_run",
        "description": "把任务派发给本机 CodeBuddy Code（腾讯 CLI）当子代理后台执行。返回 cbjob id，用 codebuddy_status/wait/result 收尾。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "cwd": {"type": "string"},
                "provider": {"type": "string", "description": "默认 magpie；CodeBuddy 通过 group/ 路由使用本地 Magpie。"},
                "model": {"type": "string", "description": "默认 group/auto-deepseek-v4-1-flash；也可选 hy4-preview、glm-5.3 等。"},
                "reasoning_effort": {"type": "string", "enum": ["minimal", "low", "medium", "high", "xhigh", "max"]},
                "policy": {"type": "string", "enum": ["read-only", "workspace-write", "full-access"]},
                "resume_session": {"type": "string", "description": "恢复 CodeBuddy session id。"},
                "allow_web": {"type": "boolean", "description": "默认禁用 WebSearch/WebFetch。"},
                "wait_seconds": {"type": "number"},
            },
            "required": ["prompt"],
        },
    },
    {"name": "codebuddy_status", "description": "查看 CodeBuddy 子代理任务状态。", "inputSchema": {"type": "object", "properties": {
        "job_id": {"type": "string"}, "session_id": {"type": "string"}, "tail_chars": {"type": "integer"}}}},
    {"name": "codebuddy_wait", "description": "等待 CodeBuddy 任务结束并返回正文。", "inputSchema": {"type": "object", "properties": {
        "job_id": {"type": "string"}, "session_id": {"type": "string"}, "timeout_seconds": {"type": "number"},
        "poll_seconds": {"type": "number"}, "max_chars": {"type": "integer"}}}},
    {"name": "codebuddy_result", "description": "读取 CodeBuddy 任务最终正文。", "inputSchema": {"type": "object", "properties": {
        "job_id": {"type": "string"}, "session_id": {"type": "string"}, "max_chars": {"type": "integer"}}}},
    {"name": "codebuddy_stop", "description": "停止 CodeBuddy 子代理任务。", "inputSchema": {"type": "object", "properties": {
        "job_id": {"type": "string"}, "session_id": {"type": "string"}, "force": {"type": "boolean"}}}},
    {"name": "codebuddy_sessions", "description": "列出 CodeBuddy 历史会话。", "inputSchema": {"type": "object", "properties": {
        "limit": {"type": "integer"}}}},
    {"name": "codebuddy_viewer", "description": "确保共享实时监控页运行并返回 URL。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "codebuddy_doctor", "description": "检查 CodeBuddy CLI、模型、数据目录和监控页。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "codebuddy_cli", "description": "透传 CodeBuddy CLI 命令。", "inputSchema": {"type": "object", "properties": {
        "args": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]},
        "timeout_seconds": {"type": "number"}, "max_chars": {"type": "integer"}}, "required": ["args"]}},
    {
        "name": "codebuddy_panel",
        "title": "CodeBuddy 会话监控",
        "description": "打开 CodeBuddy 实时会话面板（与 DimAgent Subagent 共用同一套消息/思考/工具 UI）。",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        "annotations": {"title": "CodeBuddy 会话监控", "readOnlyHint": True},
        "_meta": {
            "ui": {"resourceUri": PANEL_URI},
            "openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]},
        },
    },
    {"name": "codebuddy_live_sessions", "description": "面板数据通道：CodeBuddy 会话列表。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "codebuddy_live_messages", "description": "面板数据通道：CodeBuddy 消息增量。",
     "inputSchema": {"type": "object", "properties": {"session": {"type": "string"}, "offset": {"type": "integer"}},
                     "required": ["session"]}},
]

HANDLERS = {
    "codebuddy_run": tool_run,
    "codebuddy_status": tool_status,
    "codebuddy_wait": tool_wait,
    "codebuddy_result": tool_result,
    "codebuddy_stop": tool_stop,
    "codebuddy_sessions": tool_sessions,
    "codebuddy_viewer": tool_viewer,
    "codebuddy_doctor": tool_doctor,
    "codebuddy_cli": tool_cli,
    "codebuddy_panel": tool_panel,
    "codebuddy_live_sessions": tool_live_sessions,
    "codebuddy_live_messages": tool_live_messages,
}

PROTOCOL_FALLBACK = "2025-06-18"


def send(obj: dict) -> None:
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def send_result(req_id, result) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def send_error(req_id, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def text_content(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


def handle(msg: dict) -> None:
    method = msg.get("method") or ""
    req_id = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        send_result(req_id, {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_FALLBACK,
            "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}},
            "serverInfo": {"name": "codebuddy-subagent", "version": DA.VERSION},
            "instructions": "CodeBuddy subagent tools: codebuddy_run -> codebuddy_wait/status/result. Web disabled by default.",
        })
        return
    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return
    if method == "ping":
        if req_id is not None:
            send_result(req_id, {})
        return
    if method == "tools/list":
        send_result(req_id, {"tools": TOOLS})
        return
    if method == "resources/list":
        send_result(req_id, {"resources": [{
            "uri": PANEL_URI,
            "name": "CodeBuddy 会话监控",
            "description": "CodeBuddy 会话列表、思考链、工具调用与最终正文。",
            "mimeType": PANEL_MIME,
        }]})
        return
    if method == "resources/read":
        uri = params.get("uri") or ""
        if uri != PANEL_URI:
            send_error(req_id, -32602, f"Resource not found: {uri}")
            return
        send_result(req_id, {"contents": [{
            "uri": PANEL_URI,
            "mimeType": PANEL_MIME,
            "text": panel_html(),
            "_meta": {
                "openai/widgetCSP": {
                    "connectDomains": [str(CFG.get("viewer_url") or "")],
                    "resourceDomains": [],
                },
                "openai/widgetHeightHint": 640,
            },
        }]})
        return
    if method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            send_result(req_id, text_content(f"未知工具：{name}", True))
            return
        try:
            output = handler(args if isinstance(args, dict) else {})
            if isinstance(output, dict):
                send_result(req_id, {
                    "content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}],
                    "structuredContent": output,
                })
            else:
                send_result(req_id, text_content(output))
        except Exception as exc:  # noqa: BLE001
            send_result(req_id, text_content(f"CodeBuddy tool failed: {type(exc).__name__}: {exc}", True))
        return
    if req_id is not None:
        send_error(req_id, -32601, f"Method not found: {method}")


def main() -> int:
    _ensure_dirs()
    while True:
        try:
            raw = sys.stdin.buffer.readline()
        except (KeyboardInterrupt, OSError):
            break
        if not raw:
            break
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            handle(msg)
        except Exception as exc:  # noqa: BLE001
            if isinstance(msg, dict) and msg.get("id") is not None:
                send_error(msg["id"], -32603, f"Internal error: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
