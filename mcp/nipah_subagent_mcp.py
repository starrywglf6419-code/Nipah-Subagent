#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Nipah Subagent - one extensible MCP facade for local agent CLIs.

The first providers are DimAgent (`dim`) and CodeBuddy (`codebuddy`). Provider
implementations stay in their own modules; this file owns the shared protocol,
tool names, provider registry and combined monitoring panel.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
VIEWER_FILE = os.path.join(PROJECT_ROOT, "dim-live-viewer", "index.html")
VIEWER_SCRIPT = os.path.join(PROJECT_ROOT, "nipah-live-viewer", "server.py")
PANEL_MIME = "text/html;profile=mcp-app"
VIEWER_URL = os.environ.get("NIPAH_VIEWER_URL", "http://127.0.0.1:8792/")
VIEWER_PORT = int(os.environ.get("NIPAH_VIEWER_PORT", "8792"))
IS_WINDOWS = os.name == "nt"
VERSION = "2.0.0"
PROVIDERS_FILE = os.path.join(HERE, "providers.json")


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _provider_config() -> dict:
    try:
        with open(PROVIDERS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        engines = data.get("engines")
        return engines if isinstance(engines, dict) else {}
    except (OSError, ValueError):
        return {}


PROVIDERS = _provider_config()
ENGINE_IDS = list(PROVIDERS) or ["dimagent", "codebuddy"]
PROVIDER_MODULES = {
    key: _load(
        f"nipah_{key}_provider",
        os.path.join(HERE, str(spec.get("module") or f"{key}_mcp.py")),
    )
    for key, spec in PROVIDERS.items()
    if isinstance(spec, dict)
}
DA = PROVIDER_MODULES.get("dimagent") or _load(
    "nipah_dimagent_provider", os.path.join(HERE, "dimagent_mcp.py")
)
CB = PROVIDER_MODULES.get("codebuddy") or _load(
    "nipah_codebuddy_provider", os.path.join(HERE, "codebuddy_mcp.py")
)
PROVIDER_MODULES.setdefault("dimagent", DA)
PROVIDER_MODULES.setdefault("codebuddy", CB)


def _provider_spec(engine: str) -> dict:
    return PROVIDERS.get(engine) if isinstance(PROVIDERS.get(engine), dict) else {}


def _engine_label(engine: str) -> str:
    return str(_provider_spec(engine).get("label") or engine)


def _engine_from_job(job_id: str) -> str:
    value = str(job_id or "")
    for engine, spec in PROVIDERS.items():
        prefix = str((spec or {}).get("jobPrefix") or "")
        if prefix and value.startswith(prefix):
            return engine
    return "codebuddy" if value.startswith("cbjob_") else "dimagent"


def _revision() -> str:
    digest = hashlib.sha256()
    for path in (VIEWER_FILE, os.path.abspath(__file__), VIEWER_SCRIPT):
        try:
            with open(path, "rb") as fh:
                digest.update(fh.read())
        except OSError:
            pass
    return digest.hexdigest()[:12] or "fallback"


PANEL_URI = "ui://nipah/subagent/" + _revision()


def _viewer_url_at(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def _health_at(url: str) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=1.5) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _find_viewer_url() -> str:
    for port in range(VIEWER_PORT, VIEWER_PORT + 13):
        url = _viewer_url_at(port)
        if _health_at(url):
            return url
    return ""


def _viewer_health() -> bool:
    return bool(_find_viewer_url())


def _viewer_lock():
    """Cross-process startup lock so concurrent MCP instances don't spawn duplicates."""
    path = os.path.join(tempfile.gettempdir(), "nipah-subagent-viewer.lock")
    try:
        if os.path.isfile(path) and time.time() - os.path.getmtime(path) > 90:
            os.remove(path)
    except OSError:
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        return fd, path
    except FileExistsError:
        return None, path
    except OSError:
        return None, path


def ensure_viewer() -> dict:
    found = _find_viewer_url()
    if found:
        return {"running": True, "url": found, "started": False}
    lock_fd, lock_path = _viewer_lock()
    if lock_fd is None:
        for _ in range(24):
            time.sleep(0.35)
            found = _find_viewer_url()
            if found:
                return {"running": True, "url": found, "started": False}
        # The startup lock is advisory. If the owner died before the socket
        # came up, don't leave every caller stuck in the "starting" branch.
        try:
            os.remove(lock_path)
        except OSError:
            pass
        lock_fd, lock_path = _viewer_lock()
        if lock_fd is None:
            return {"running": False, "url": VIEWER_URL, "started": False,
                    "note": "viewer startup did not become healthy"}
    try:
        # Re-check after acquiring the lock; another process may have won.
        found = _find_viewer_url()
        if found:
            return {"running": True, "url": found, "started": False}
        if not os.path.isfile(VIEWER_SCRIPT):
            return {"running": False, "url": VIEWER_URL, "started": False,
                    "note": f"viewer script missing: {VIEWER_SCRIPT}"}
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
        try:
            subprocess.Popen(
                [sys.executable, VIEWER_SCRIPT, "--port", str(VIEWER_PORT)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, creationflags=creation, close_fds=True,
            )
        except OSError as exc:
            return {"running": False, "url": VIEWER_URL, "started": False, "note": str(exc)}
        for _ in range(24):
            time.sleep(0.35)
            found = _find_viewer_url()
            if found:
                return {"running": True, "url": found, "started": True}
        return {"running": False, "url": VIEWER_URL, "started": True,
                "note": "started but health check failed"}
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        try:
            os.remove(lock_path)
        except OSError:
            pass


def combined_sessions(limit: int = 500) -> list[dict]:
    out = []
    for engine, module in PROVIDER_MODULES.items():
        try:
            if engine == "dimagent":
                items = _load_viewer().fetch_sessions(module.db_path(), module.runs_dir())
            elif hasattr(module, "_live_sessions"):
                items = module._live_sessions().get("sessions", [])
            else:
                items = module.fetch_sessions(1000)
            for item in items:
                row = dict(item)
                row["source"] = engine
                row["engine"] = engine
                row["engineLabel"] = _engine_label(engine)
                row["providerId"] = row.get("providerId") or _engine_label(engine)
                out.append(row)
        except Exception:
            continue
    out.sort(key=lambda x: x.get("lastAt") or x.get("updatedAt") or "", reverse=True)
    return out[:max(1, int(limit))]


def _source_of(session_id: str) -> str:
    for item in combined_sessions(2000):
        if item.get("id") == session_id:
            return item.get("source") or "dimagent"
    return ""


def combined_messages(source: str, session_id: str, offset: int = 0) -> dict:
    source = source or _source_of(session_id)
    if source in PROVIDER_MODULES and source != "dimagent":
        module = PROVIDER_MODULES[source]
        if hasattr(module, "fetch_messages"):
            try:
                return module.fetch_messages(session_id, offset)
            except TypeError:
                return module.fetch_messages(source, session_id, offset)
    return _load_viewer().fetch_messages(DA.db_path(), session_id, offset)


def _engine(value: str = "", job_id: str = "", session_id: str = "") -> str:
    value = (value or "").strip().lower()
    aliases = {"cb": "codebuddy", "dim": "dimagent", "dimcode": "dimagent"}
    value = aliases.get(value, value)
    if value in PROVIDER_MODULES:
        return value
    if job_id:
        return _engine_from_job(str(job_id))
    if session_id:
        found = _source_of(session_id)
        if found:
            return found
    return "dimagent"


def _run_args(args: dict, engine: str) -> dict:
    out = dict(args)
    out.pop("engine", None)
    if engine == "dimagent" and out.get("resume_session"):
        out["session_id"] = out.pop("resume_session")
        out["resume"] = True
    if engine == "codebuddy":
        out.pop("resume", None)
        out["resume_session"] = out.get("resume_session") or out.pop("session_id", "")
    out["open_viewer"] = False
    return out


def nipah_run(args: dict) -> str:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return "错误：prompt 不能为空。"
    engine = _engine(str(args.get("engine") or ""))
    prepared = _run_args(dict(args, prompt=prompt), engine)
    text = PROVIDER_MODULES[engine].tool_run(prepared)
    viewer = ensure_viewer()
    lines = [
        text,
        "",
        f"Nipah 引擎：{engine}（{_engine_label(engine)}）",
        f"Nipah 监控页：{viewer.get('url')}",
    ]
    return "\n".join(lines)


def _provider_tool(engine: str, name: str, args: dict):
    prepared = _run_args(args, engine)
    fn = getattr(PROVIDER_MODULES[engine], name)
    return fn(prepared)


def nipah_status(args: dict) -> str:
    engine = _engine(str(args.get("engine") or ""), args.get("job_id") or "", args.get("session_id") or "")
    return _provider_tool(engine, "tool_status", args)


def nipah_wait(args: dict) -> str:
    engine = _engine(str(args.get("engine") or ""), args.get("job_id") or "", args.get("session_id") or "")
    return _provider_tool(engine, "tool_wait", args)


def nipah_result(args: dict) -> str:
    engine = _engine(str(args.get("engine") or ""), args.get("job_id") or "", args.get("session_id") or "")
    return _provider_tool(engine, "tool_result", args)


def nipah_stop(args: dict) -> str:
    engine = _engine(str(args.get("engine") or ""), args.get("job_id") or "", args.get("session_id") or "")
    return _provider_tool(engine, "tool_stop", args)


def nipah_resume(args: dict) -> str:
    engine = _engine(str(args.get("engine") or ""), "", args.get("session_id") or "")
    prepared = dict(args)
    prepared["resume_session"] = prepared.get("session_id")
    if engine == "codebuddy":
        prepared.pop("session_id", None)
    return nipah_run(prepared)


def nipah_feedback(args: dict) -> str:
    engine = _engine(str(args.get("engine") or ""))
    module = PROVIDER_MODULES.get(engine)
    if module is None or not hasattr(module, "tool_feedback"):
        return f"{_engine_label(engine)} 没有反馈入口。"
    return module.tool_feedback(args)


def nipah_sessions(args: dict) -> str:
    sessions = combined_sessions(int(args.get("limit") or 30))
    if not sessions:
        return "Nipah Subagent 还没有会话。"
    out = [f"最近 {len(sessions)} 个 Nipah 会话：", ""]
    for item in sessions:
        out.append(
            f"- [{item.get('engine')}] {item.get('id')}  [{item.get('jobStatus') or 'stored'}]  "
            f"{item.get('title') or item.get('subagentPrompt') or '(无标题)'}"
        )
        out.append(
            f"    {item.get('messageCount', 0)} 条 · {item.get('modelId') or item.get('model') or '-'} · "
            f"{item.get('cwd') or '-'}"
        )
    return "\n".join(out)


def nipah_viewer(args: dict) -> str:
    info = ensure_viewer()
    return (
        f"Nipah 统一监控页：{info.get('url')} "
        f"{'运行中' if info.get('running') else '未运行'}"
        + (f"（{info.get('note')}）" if info.get("note") else "")
        + "\n页面内可切换 " + " / ".join(["全部"] + [_engine_label(x) for x in ENGINE_IDS]) + "。"
    )


def nipah_providers(args: dict) -> str:
    lines = [f"Nipah Subagent {VERSION}"]
    for engine in ENGINE_IDS:
        spec = _provider_spec(engine)
        module = PROVIDER_MODULES.get(engine)
        cfg = getattr(module, "CFG", {}) if module is not None else {}
        lines.append(
            f"engine={engine}: {_engine_label(engine)} · default "
            f"{cfg.get('default_provider') or spec.get('defaultProvider') or '-'} / "
            f"{cfg.get('default_model') or spec.get('defaultModel') or '-'} / "
            f"effort {cfg.get('default_reasoning_effort') or spec.get('defaultReasoningEffort') or '-'}"
        )
    lines.append(
        "扩展方式：在 mcp/providers.json 增加 engine，并提供 run/status/wait/result/stop/"
        "sessions/fetch_messages；统一监控与 MCP 工具无需再改协议。"
    )
    return "\n".join(lines)


def nipah_doctor(args: dict) -> str:
    viewer = ensure_viewer()
    lines = [
        f"Nipah Subagent {VERSION} · pid {os.getpid()}",
        f"viewer: {viewer.get('url')} {'running' if viewer.get('running') else 'stopped'}",
        f"panel: {PANEL_URI}",
        f"engines: {', '.join(ENGINE_IDS)}",
        "provider registry: " + PROVIDERS_FILE,
        "",
    ]
    for engine in ENGINE_IDS:
        module = PROVIDER_MODULES.get(engine)
        if engine == "dimagent" and module is not None and hasattr(module, "dim_path"):
            detail = module.dim_path()
        elif hasattr(module, "codebuddy_version"):
            detail = module.codebuddy_version()
        else:
            detail = "-"
        lines.insert(1, f"engine {engine}: {detail}")
    for engine in ENGINE_IDS:
        lines.append(f"{_engine_label(engine)}:")
        module = PROVIDER_MODULES.get(engine)
        if module is not None and hasattr(module, "tool_doctor"):
            try:
                lines.append(module.tool_doctor({}))
            except Exception as exc:  # noqa: BLE001
                lines.append(f"doctor failed: {type(exc).__name__}: {exc}")
        else:
            lines.append("(no doctor method)")
        lines.append("")
    return "\n".join(lines)


def nipah_cli(args: dict) -> str:
    engine = _engine(str(args.get("engine") or "dimagent"))
    prepared = dict(args)
    prepared.pop("engine", None)
    module = PROVIDER_MODULES[engine]
    if not hasattr(module, "tool_cli"):
        return f"{_engine_label(engine)} 没有 CLI 透传入口。"
    return module.tool_cli(prepared)


def nipah_live_sessions(args: dict) -> dict:
    return {"ok": True, "now": None, "sessions": combined_sessions(1000)}


def _load_viewer():
    return _load("nipah_viewer_data", os.path.join(PROJECT_ROOT, "dim-live-viewer", "server.py"))


def nipah_live_messages(args: dict) -> dict:
    sid = str(args.get("session") or "").strip()
    if not sid:
        return {"ok": False, "error": "missing session"}
    source = str(args.get("source") or "").strip() or _source_of(sid)
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        return {"ok": True, **combined_messages(source, sid, offset)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def panel_html() -> str:
    try:
        with open(VIEWER_FILE, encoding="utf-8") as fh:
            html = fh.read()
    except OSError as exc:
        return "<!doctype html><meta charset='utf-8'><body>Nipah panel load failed: " + str(exc) + "</body>"
    html = html.replace("DimAgent · 实时会话", "Nipah Subagent · 实时会话")
    html = html.replace(
        '<span><span class="name">DimAgent</span><small>实时会话</small></span>',
        '<span><span class="name">Nipah</span><small>Subagent · 实时会话</small></span>',
    )
    html = html.replace("只读视图 · dimcode.sqlite", "只读视图 · Nipah 会话")
    html = html.replace("正在连接 DimAgent 数据", "正在连接 Nipah 数据")
    html = html.replace(
        "globalThis.NIPAH_LIVE_SESSIONS_TOOL || 'nipah_live_sessions'",
        "'nipah_live_sessions'",
    ).replace(
        "globalThis.NIPAH_LIVE_MESSAGES_TOOL || 'nipah_live_messages'",
        "'nipah_live_messages'",
    )
    return html


def nipah_panel(args: dict) -> str:
    sessions = combined_sessions(int(args.get("limit") or 12))
    viewer = ensure_viewer()
    return json.dumps({
        "ok": True,
        "version": VERSION,
        "viewer": viewer,
        "engines": ENGINE_IDS,
        "sessions": [
            {
                "id": x.get("id"), "engine": x.get("engine"), "title": x.get("title"),
                "messages": x.get("messageCount", 0), "jobStatus": x.get("jobStatus"),
            }
            for x in sessions
        ],
    }, ensure_ascii=False)


TOOLS = [
    {
        "name": "nipah_run",
        "description": "Nipah Subagent 统一派发入口：engine=dimagent 或 codebuddy，后台执行并返回 jobId/sessionId。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "engine": {"type": "string", "enum": ENGINE_IDS, "description": "默认 dimagent。"},
                "provider": {"type": "string", "description": "底层模型供应商；DimAgent 可用 magpie，CodeBuddy 默认 magpie。"},
                "model": {"type": "string", "description": "引擎默认值；DimAgent 默认按配置，CodeBuddy 默认 group/auto-deepseek-v4-1-flash。"},
                "prompt": {"type": "string"},
                "cwd": {"type": "string"},
                "reasoning_effort": {"type": "string", "enum": ["none", "minimal", "low", "medium", "high", "xhigh", "max"]},
                "policy": {"type": "string", "enum": ["read-only", "workspace-write", "full-access"],
                           "description": "默认 full-access；只读审查用 read-only。"},
                "resume_session": {"type": "string"},
                "session_id": {"type": "string", "description": "DimAgent 载入已有会话时使用；不要与模型/权限覆盖同时传。"},
                "resume": {"type": "boolean"},
                "resume_last": {"type": "boolean"},
                "mode": {"type": "string", "enum": ["agent", "plan", "goal"]},
                "verify": {"type": "string"},
                "repair_attempts": {"type": "integer"},
                "tools": {"type": "string"},
                "allowed_tools": {"type": "string"},
                "disallowed_tools": {"type": "string"},
                "images": {"type": "array", "items": {"type": "string"}},
                "allow_web": {"type": "boolean"},
                "wait_seconds": {"type": "number"},
            },
            "required": ["prompt"],
        },
    },
    {"name": "nipah_status", "description": "查 Nipah 子代理状态；可根据 jobId 前缀自动识别引擎。", "inputSchema": {"type": "object", "properties": {
        "engine": {"type": "string", "enum": ENGINE_IDS}, "job_id": {"type": "string"},
        "session_id": {"type": "string"}, "tail_chars": {"type": "integer"}}}},
    {"name": "nipah_wait", "description": "等待 Nipah 子代理结束并返回正文。", "inputSchema": {"type": "object", "properties": {
        "engine": {"type": "string", "enum": ENGINE_IDS}, "job_id": {"type": "string"},
        "session_id": {"type": "string"}, "timeout_seconds": {"type": "number"},
        "poll_seconds": {"type": "number"}, "max_chars": {"type": "integer"}}}},
    {"name": "nipah_result", "description": "读取 Nipah 子代理最终正文。", "inputSchema": {"type": "object", "properties": {
        "engine": {"type": "string", "enum": ENGINE_IDS}, "job_id": {"type": "string"},
        "session_id": {"type": "string"}, "max_chars": {"type": "integer"}}}},
    {"name": "nipah_stop", "description": "停止 Nipah 子代理任务。", "inputSchema": {"type": "object", "properties": {
        "engine": {"type": "string", "enum": ENGINE_IDS}, "job_id": {"type": "string"},
        "session_id": {"type": "string"}, "force": {"type": "boolean"}}}},
    {"name": "nipah_resume", "description": "续跑已存在的 DimAgent 或 CodeBuddy 会话。", "inputSchema": {"type": "object", "properties": {
        "engine": {"type": "string", "enum": ENGINE_IDS}, "session_id": {"type": "string"},
        "prompt": {"type": "string"}, "cwd": {"type": "string"},
        "reasoning_effort": {"type": "string"},
        "policy": {"type": "string", "description": "默认 full-access；只读审查用 read-only。"},
        "mode": {"type": "string"}, "verify": {"type": "string"}, "repair_attempts": {"type": "integer"},
        "tools": {"type": "string"}, "allowed_tools": {"type": "string"},
        "disallowed_tools": {"type": "string"}, "images": {"type": "array", "items": {"type": "string"}},
        "allow_web": {"type": "boolean"}, "wait_seconds": {"type": "number"}},
        "required": ["session_id", "prompt"]}},
    {"name": "nipah_feedback", "description": "把使用问题记录到对应引擎的反馈目录。", "inputSchema": {"type": "object", "properties": {
        "engine": {"type": "string", "enum": ENGINE_IDS}, "title": {"type": "string"},
        "scenario": {"type": "string"}, "expected": {"type": "string"}, "actual": {"type": "string"},
        "evidence": {"type": "string"}, "severity": {"type": "string"}}, "required": ["title"]}},
    {"name": "nipah_sessions", "description": "列出两个引擎的 Nipah 会话，并标记来源。", "inputSchema": {"type": "object", "properties": {
        "limit": {"type": "integer"}}}},
    {"name": "nipah_viewer", "description": "确保 Nipah 统一监控页运行并返回 URL。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "nipah_providers", "description": "列出已注册的 Nipah engine 与默认 provider/model。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "nipah_doctor", "description": "检查两个引擎、统一监控页和扩展状态。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "nipah_cli", "description": "透传指定引擎的 CLI 命令。", "inputSchema": {"type": "object", "properties": {
        "engine": {"type": "string", "enum": ENGINE_IDS},
        "args": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]},
        "timeout_seconds": {"type": "number"}, "max_chars": {"type": "integer"}}, "required": ["args"]}},
    {
        "name": "nipah_panel",
        "title": "Nipah Subagent 会话监控",
        "description": "打开统一 Nipah Subagent 监控面板，可切换 DimAgent / CodeBuddy。",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        "annotations": {"title": "Nipah Subagent 会话监控", "readOnlyHint": True},
        "_meta": {
            "ui": {"resourceUri": PANEL_URI},
            "openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]},
        },
    },
    {"name": "nipah_live_sessions", "description": "统一面板数据：两个引擎的会话列表。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "nipah_live_messages", "description": "统一面板数据：按会话来源读取消息增量。", "inputSchema": {
        "type": "object", "properties": {"source": {"type": "string"}, "session": {"type": "string"}, "offset": {"type": "integer"}},
        "required": ["session"]}},
]

HANDLERS = {
    "nipah_run": nipah_run,
    "nipah_status": nipah_status,
    "nipah_wait": nipah_wait,
    "nipah_result": nipah_result,
    "nipah_stop": nipah_stop,
    "nipah_resume": nipah_resume,
    "nipah_feedback": nipah_feedback,
    "nipah_sessions": nipah_sessions,
    "nipah_viewer": nipah_viewer,
    "nipah_providers": nipah_providers,
    "nipah_doctor": nipah_doctor,
    "nipah_cli": nipah_cli,
    "nipah_panel": nipah_panel,
    "nipah_live_sessions": nipah_live_sessions,
    "nipah_live_messages": nipah_live_messages,
}


def send(obj):
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def send_result(req_id, result):
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def send_error(req_id, code, message):
    send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def text_content(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


def handle(msg: dict) -> None:
    method = msg.get("method") or ""
    req_id = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        send_result(req_id, {
            "protocolVersion": params.get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}},
            "serverInfo": {"name": "nipah-subagent", "version": VERSION},
            "instructions": (
                "Nipah Subagent 统一入口：nipah_run(engine=dimagent|codebuddy) → "
                "nipah_status/wait/result。监控页用 nipah_panel，页面可切换两个 engine。"
            ),
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
            "uri": PANEL_URI, "name": "Nipah Subagent 会话监控",
            "description": "DimAgent / CodeBuddy 统一会话监控。",
            "mimeType": PANEL_MIME,
        }]})
        return
    if method == "resources/read":
        uri = params.get("uri") or ""
        if uri != PANEL_URI:
            send_error(req_id, -32602, f"Resource not found: {uri}")
            return
        send_result(req_id, {"contents": [{
            "uri": PANEL_URI, "mimeType": PANEL_MIME, "text": panel_html(),
            "_meta": {
                "openai/widgetCSP": {"connectDomains": [VIEWER_URL], "resourceDomains": []},
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
            send_result(req_id, text_content(f"Nipah tool failed: {type(exc).__name__}: {exc}", True))
        return
    if req_id is not None:
        send_error(req_id, -32601, f"Method not found: {method}")


def main() -> int:
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

