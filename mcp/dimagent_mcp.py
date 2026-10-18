#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DimAgent 子代理 · MCP 服务器（stdio，零第三方依赖）

把 DimAgent CLI（dim）包装成 Codex / 其他 MCP 客户端可以直接调用的子代理工具：
派发任务、查状态、等结果、列会话、拉起实时监控页。

设计要点
  * dim.exe 的路径**绝不写死**：环境变量 → 配置文件 → PATH → 自动扫描（含版本化目录）
  * 任务在后台跑（不阻塞对话），输出以 --json 事件流落到 runs/<job>.jsonl
  * 结果以 SQLite（只读）为准，事件流只用来判断"跑到哪一步了"
  * 任何日志都写文件，stdout 只走 MCP 协议

配置（可选，按优先级）：
  DIMAGENT_SUBAGENT_CONFIG 环境变量指向的 json
  ~/.dimagent-subagent/config.json
可用字段见 DEFAULTS；未配置的字段走自动发现。
"""
from __future__ import annotations

import glob
import ctypes
import hashlib
import json
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

NAME = "dimagent-subagent"
VERSION = "1.2.0"
STARTED_AT = time.time()
HOME = os.path.expanduser("~")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(HOME, ".dimagent-subagent")
LOG_DIR = os.path.join(STATE_DIR, "logs")
LOCAL_APP_DATA = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
DATA_DIR = os.environ.get("DIMAGENT_SUBAGENT_DATA") or os.path.join(LOCAL_APP_DATA, "DimAgentSubagent")
RUNS_DIR = os.path.join(DATA_DIR, "runs")
WORKSPACE_DIR = os.path.join(DATA_DIR, "workspace")
IS_WINDOWS = os.name == "nt"

# ---- MCP App（Codex 侧边面板）----
# Codex 只认这两种 MIME：text/html;profile=mcp-app / text/html+skybridge
PANEL_MIME = "text/html;profile=mcp-app"
# 面板 = 实时会话查看器本体（和浏览器里 127.0.0.1:8790 打开的是同一个页面），
# 次选同目录 panel.html 副本（同步备份，防查看器被挪走）。
VIEWER_FILE = os.path.join(PROJECT_ROOT, "dim-live-viewer", "index.html")
PANEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")


def _panel_revision() -> str:
    """Cache-bust the MCP App resource whenever the panel files change."""
    digest = hashlib.sha256()
    for path in (VIEWER_FILE, PANEL_FILE):
        try:
            with open(path, "rb") as fh:
                digest.update(fh.read())
        except OSError:
            pass
    return digest.hexdigest()[:12] or "fallback"


PANEL_URI = "ui://dimagent/monitor/" + _panel_revision()

DEFAULTS = {
    # 显式指定 dim 可执行文件；留空则自动发现
    "dim_bin": "",
    # 留空时自动跟随 Codex 启动 MCP 时的项目根目录；显式配置可覆盖。
    "default_cwd": "",
    # 默认模型 / 推理档位 / 权限预设
    "default_model": "deepseek-v4.1-flash",
    # `dim exec` without --provider inherits the app's selected provider (often a
    # custom one), so always pin the intended default provider explicitly.
    "default_provider": os.environ.get("DIMAGENT_PROVIDER") or "opencode-go",
    "default_reasoning_effort": "max",
    "default_policy": "workspace-write",
    # 宿主单次工具调用约 300s 截断，MCP 等待必须留余量
    "max_wait_seconds": 240,
    # 无进程可查时，事件流超过该秒数没有更新就降级为 stale
    "stale_after_seconds": 90,
    # 默认禁用联网工具，需要时显式传 allow_web=True 或 allow_web_tools 工具名
    "default_disallow_web": True,
    # 模型/policy 预检；模型表缓存 5 分钟
    "preflight_model": True,
    # 给长输出任务加一条分段落盘规则，缓解 output_truncated
    "output_guard": True,
    # DimAgent 数据目录（留空则按 DIMCODE_HOME / ~/.dimcode/v2 解析）
    "dimcode_home": "",
    # 实时监控页
    "viewer_url": "http://127.0.0.1:8790/",
    "viewer_script": os.path.join(PROJECT_ROOT, "dim-live-viewer", "server.py"),
    "viewer_port": 8790,
    "auto_start_viewer": True,
    # 任务运行数据：项目目录只放代码，数据统一放 LOCALAPPDATA
    "runs_dir": RUNS_DIR,
    # 自动发现时额外扫描的目录（支持 * 通配）
    "search_globs": [
        r"C:\Program Files\DimAgent\resources\runtime\cli\dim.exe",
        r"C:\Program Files (x86)\DimAgent\resources\runtime\cli\dim.exe",
        os.path.join(HOME, "AppData", "Local", "Programs", "DimAgent", "resources", "runtime", "cli", "dim.exe"),
        os.path.join(HOME, "AppData", "Local", "DimAgent", "resources", "runtime", "cli", "dim.exe"),
        # 版本化目录 / standalone 安装的兜底
        r"C:\Program Files\DimAgent\**\runtime\cli\dim.exe",
        r"C:\Program Files\DimAgent\**\cli\*\dim.exe",
        os.path.join(HOME, ".dimcode", "dimcode", "**", "dim.exe"),
        os.path.join(HOME, "AppData", "Roaming", "npm", "dim.cmd"),
    ],
}


def _ensure_dirs() -> None:
    for d in (STATE_DIR, LOG_DIR, DATA_DIR, RUNS_DIR, WORKSPACE_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


def log(message: str) -> None:
    """只写文件：stdout 是 MCP 协议通道，绝不能污染。"""
    _ensure_dirs()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    path = os.path.join(LOG_DIR, "mcp-" + time.strftime("%Y%m%d") + ".log")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    candidates = [
        os.environ.get("DIMAGENT_SUBAGENT_CONFIG", ""),
        os.path.join(HOME, ".dimagent-subagent", "config.json"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    cfg.update(data)
                    cfg["_config_file"] = path
            except (OSError, ValueError) as exc:
                log(f"config load failed {path}: {exc}")
    if os.environ.get("DIMAGENT_BIN"):
        cfg["dim_bin"] = os.environ["DIMAGENT_BIN"]
    return cfg


CFG = load_config()

# ---------------------------------------------------------------- dim 解析

_dim_cache: dict = {"path": "", "source": ""}
_model_cache: dict = {"at": 0.0, "items": [], "error": ""}


def _expand(entry: str) -> str:
    return os.path.expandvars(os.path.expanduser(entry))


def default_cwd() -> str:
    """Use the Codex project cwd by default, with a safe standalone fallback."""
    configured = _expand(str(CFG.get("default_cwd") or "").strip())
    if configured:
        return configured
    cwd = os.getcwd()
    return cwd if os.path.isdir(cwd) else WORKSPACE_DIR


def resolve_provider_id(value: str) -> str:
    """Accept enabled provider ids or display names such as 'magpie'."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        con = db_connect()
        try:
            row = con.execute(
                """
                SELECT providerId
                FROM providers
                WHERE enabled = 1
                  AND (lower(providerId) = lower(?) OR lower(displayName) = lower(?))
                ORDER BY CASE WHEN lower(providerId) = lower(?) THEN 0 ELSE 1 END,
                         providerId
                LIMIT 1
                """,
                (raw, raw, raw),
            ).fetchone()
        finally:
            con.close()
        return row["providerId"] if row else raw
    except (RuntimeError, sqlite3.Error):
        return raw


def _newest(paths):
    paths = [p for p in paths if p and os.path.isfile(p)]
    if not paths:
        return ""
    return max(paths, key=lambda p: os.path.getmtime(p))


def resolve_dim(force: bool = False):
    """按 环境变量/配置 → PATH → 自动扫描 的顺序找 dim 可执行文件。

    结果做缓存，但每次都会重新校验文件是否还存在——DimAgent 升级换目录后能自动跟上。
    """
    if not force and _dim_cache["path"] and os.path.isfile(_dim_cache["path"]):
        return _dim_cache["path"], _dim_cache["source"]

    tried = []

    # 1) 显式指定
    explicit = _expand(str(CFG.get("dim_bin") or ""))
    if explicit:
        tried.append(explicit)
        if os.path.isfile(explicit):
            _dim_cache.update(path=explicit, source="配置 dim_bin")
            return explicit, "配置 dim_bin"

    # 2) PATH
    for name in ("dim", "dim.exe", "dim.cmd", "dimcode"):
        found = shutil.which(name)
        if found:
            _dim_cache.update(path=found, source="PATH")
            return found, "PATH"
    tried.append("PATH: dim/dim.exe/dim.cmd/dimcode")

    # 3) 自动扫描（含版本化目录，取最新）
    for pattern in CFG.get("search_globs") or []:
        pat = _expand(pattern)
        tried.append(pat)
        if any(ch in pat for ch in "*?["):
            hit = _newest(glob.glob(pat, recursive=True))
            if hit:
                _dim_cache.update(path=hit, source="自动扫描")
                return hit, "自动扫描"
        elif os.path.isfile(pat):
            _dim_cache.update(path=pat, source="默认安装路径")
            return pat, "默认安装路径"

    raise RuntimeError(
        "找不到 DimAgent CLI（dim）。\n"
        "请任选一种方式指定，然后重试：\n"
        "  1) 设置环境变量 DIMAGENT_BIN=<dim.exe 绝对路径>\n"
        "  2) 写配置文件 ~/.dimagent-subagent/config.json 的 \"dim_bin\"\n"
        "  3) 把它加进 PATH\n"
        "已尝试：\n  - " + "\n  - ".join(tried)
    )


def dim_path() -> str:
    return resolve_dim()[0]


def dim_version() -> str:
    try:
        out = subprocess.run([dim_path(), "version"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
        return (out.stdout or out.stderr or "").strip().splitlines()[0] if (out.stdout or out.stderr) else ""
    except Exception as exc:  # noqa: BLE001
        return f"(读取失败: {exc})"


def list_models(force: bool = False) -> dict:
    """Return the flat model table exposed by `dim model list`.

    DimAgent's table can drift from the provider's actual capability matrix,
    so this is a cheap name check, not a guarantee that a request will work.
    """
    now = time.time()
    if not force and _model_cache["items"] and now - _model_cache["at"] < 300:
        return {"items": list(_model_cache["items"]), "error": _model_cache["error"], "cached": True}
    # SQLite already has the same provider/model table. Reading it avoids a
    # ~9s CLI cold start on the first dispatch of every MCP process.
    try:
        con = db_connect()
        rows = con.execute(
            "SELECT providerId, models FROM providers WHERE enabled = 1 ORDER BY providerId"
        ).fetchall()
        con.close()
        items = []
        for row in rows:
            try:
                models = json.loads(row["models"] or "[]")
            except (TypeError, ValueError):
                continue
            for item in models:
                if item.get("status", "active") == "active" and item.get("modelId"):
                    items.append(f"{row['providerId']}/{item['modelId']}")
        if items:
            items = sorted(set(items), key=str.lower)
            _model_cache.update(at=now, items=items, error="")
            return {"items": items, "error": "", "cached": False, "source": "sqlite"}
    except (RuntimeError, sqlite3.Error):
        pass
    try:
        proc = subprocess.run(
            [dim_path(), "model", "list"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=25, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"items": [], "error": f"{type(exc).__name__}: {exc}", "cached": False}
    items = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    error = "" if proc.returncode == 0 else ((proc.stderr or "").strip()[:1000] or f"exit={proc.returncode}")
    _model_cache.update(at=now, items=items, error=error)
    return {"items": items, "error": error, "cached": False}


def _model_candidates(model: str, provider: str = "") -> list:
    model = (model or "").strip()
    provider = (provider or "").strip()
    out = []
    if provider and model.startswith(provider + "/"):
        out.append(model)
    elif provider:
        out.append(provider + "/" + model)
    out.append(model)
    return list(dict.fromkeys(x for x in out if x))


def preflight_model(args: dict) -> str:
    """Return a readable error for clearly unknown model ids; web status is not probed."""
    if not CFG.get("preflight_model", True):
        return ""
    model = str(args.get("model") or CFG.get("default_model") or "").strip()
    provider = resolve_provider_id(
        str(args.get("provider") or CFG.get("default_provider") or "opencode-go")
    )
    if not model:
        return ""
    table = list_models()
    if not table["items"]:
        return f"无法读取模型表，未做预检（{table.get('error') or '空输出'}）"
    candidates = _model_candidates(model, provider)
    if not any(x in table["items"] for x in candidates):
        same_provider = [
            x for x in table["items"]
            if x.startswith(provider + "/") and x.count("/") == 1
        ]
        sample = ", ".join((same_provider or table["items"])[:12])
        return (
            f"模型预检失败：{model} 不在 model list 中（provider={provider}）。\n"
            f"可用示例：{sample}\n"
            "用 dimagent_cli(\"model list --provider " + provider + "\") 看该 provider 的完整列表。"
        )
    selected = next((x for x in candidates if x in table["items"]), model)
    # `provider/model` and `provider/model` inside the same table is normal;
    # only warn when the requested bare model is shadowed by a different provider.
    if "/" not in model and not any(x.startswith(provider + "/") and x.endswith("/" + model) for x in table["items"]):
        return f"模型预检提示：{model} 未在 provider {provider} 下出现（表中有同名项也可能来自别的 provider）。"
    return "" if selected else ""


def _compact_error_message(text: str, limit: int = 2000) -> str:
    """Keep the actionable provider error without the huge request body."""
    value = " ".join(str(text or "").split())
    lowered = value.lower()
    for marker in ("request body:", "requestbodyvalues:", "messages: ["):
        idx = lowered.find(marker)
        if idx > 0:
            value = value[:idx].rstrip() + "\n…（请求体已省略）"
            break
    return value if len(value) <= limit else value[:limit] + "…"


def preflight_inputs(args: dict, check_vision: bool = True) -> str:
    """Check local/known limits that can fail after expensive tool calls."""
    images = args.get("images") or []
    if not images:
        return ""
    missing = [p for p in images if not os.path.isfile(_expand(str(p)))]
    if missing:
        return "图片预检失败：文件不存在：\n  " + "\n  ".join(missing)
    unsupported_ext = [
        p for p in images
        if os.path.splitext(str(p))[1].lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif")
    ]
    if unsupported_ext:
        return "图片预检失败：只支持 png/jpg/jpeg/webp/gif：\n  " + "\n  ".join(unsupported_ext)
    if not check_vision:
        return ""
    provider = resolve_provider_id(
        str(args.get("provider") or CFG.get("default_provider") or "opencode-go")
    )
    model = str(args.get("model") or CFG.get("default_model") or "")
    if model.startswith(provider + "/"):
        model = model[len(provider) + 1:]
    try:
        con = db_connect()
        row = con.execute(
            "SELECT models FROM providers WHERE providerId = ?", (provider,)
        ).fetchone()
        con.close()
        if row and row["models"]:
            for item in json.loads(row["models"]):
                if item.get("modelId") == model:
                    caps = item.get("capabilities") or {}
                    if caps.get("vision") is False:
                        return (
                            f"图片预检失败：provider={provider} / model={model} "
                            "在本地能力表里不支持视觉输入。换支持图片的模型/ provider。"
                        )
                    if caps.get("vision") is not True:
                        return (
                            f"图片预检失败：provider={provider} / model={model} "
                            "的能力表没有声明 vision=true，无法保证图片输入；请改用明确支持视觉的模型。"
                        )
                    break
    except (RuntimeError, sqlite3.Error, ValueError, TypeError):
        pass
    return ""


def _fallback_provider_model(args: dict):
    """Pick another connected provider that exposes the requested model."""
    model = str(args.get("model") or CFG.get("default_model") or "").strip()
    requested_provider = str(args.get("provider") or "")
    try:
        con = db_connect()
        rows = con.execute(
            "SELECT providerId, models FROM providers WHERE enabled = 1 ORDER BY providerId"
        ).fetchall()
        con.close()
    except (RuntimeError, sqlite3.Error):
        return None
    preferred = ["opencode-go", "dimcode-api-oauth"]
    rows = sorted(rows, key=lambda r: (preferred.index(r["providerId"]) if r["providerId"] in preferred else 99, r["providerId"]))
    for row in rows:
        provider = row["providerId"]
        if provider == requested_provider:
            continue
        try:
            models = json.loads(row["models"] or "[]")
        except (TypeError, ValueError):
            continue
        if any(m.get("modelId") == model and m.get("status", "active") == "active" for m in models):
            return provider, model
    return None


def _uses_existing_session(args: dict) -> bool:
    """Both resume forms load an existing session instead of creating one."""
    return bool(args.get("resume_last") or args.get("session_id"))


def _normalize_model_args(args: dict) -> dict:
    """Make provider/model explicit before launching a new run."""
    args = dict(args)
    if not _uses_existing_session(args):
        explicit_provider = resolve_provider_id(str(args.get("provider") or ""))
        args["provider"] = explicit_provider or CFG.get("default_provider") or "opencode-go"
        model = str(args.get("model") or CFG.get("default_model") or "").strip()
        provider = str(args["provider"]).strip()
        # Accept provider-prefixed ids from provider list, but pass bare ids to
        # dim --model while keeping the provider explicit on the command line.
        if model.startswith(provider + "/"):
            model = model[len(provider) + 1:]
        args["model"] = model
        if not explicit_provider:
            args["_providerDefaulted"] = True
    return args


# ---------------------------------------------------------------- 数据读取

def db_path() -> str:
    home = str(CFG.get("dimcode_home") or os.environ.get("DIMCODE_HOME") or "")
    home = _expand(home) if home else os.path.join(HOME, ".dimcode")
    return os.path.join(home, "v2", "dimcode.sqlite")


def db_connect():
    path = db_path()
    if not os.path.isfile(path):
        raise RuntimeError(f"找不到 DimAgent 数据库：{path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _parse_json(text, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def short_id(sid: str) -> str:
    if not sid:
        return ""
    parts = sid.rsplit("_", 2)
    return sid if len(parts) < 3 else ("sub_" if sid.startswith("subagent") else "s_") + parts[-1][:10]


def tool_brief(tool_input, limit: int = 80) -> str:
    """把工具入参压成一行摘要，联网搜索/抓取类工具优先显示关键词或网址。"""
    if isinstance(tool_input, dict):
        for key in ("query", "url", "path", "file_path", "command", "prompt", "description", "taskId"):
            value = tool_input.get(key)
            if value:
                return " ".join(str(value).split())[:limit]
        try:
            return json.dumps(tool_input, ensure_ascii=False)[:limit]
        except (TypeError, ValueError):
            return ""
    if tool_input is None:
        return ""
    return " ".join(str(tool_input).split())[:limit]


def list_sessions(limit: int = 15):
    con = db_connect()
    try:
        rows = con.execute(
            """
            SELECT s.sessionId, s.cwd, s.title, s.status, s.createdAt, s.updatedAt,
                   st.selectedProviderId AS provider, st.selectedModelId AS model,
                   st.settings AS settings,
                   (SELECT COUNT(*) FROM messages m WHERE m.sessionId = s.sessionId) AS n,
                   (SELECT createdAt FROM messages m WHERE m.sessionId = s.sessionId
                     ORDER BY createdAt DESC, orderKey DESC LIMIT 1) AS lastAt
            FROM sessions s
            LEFT JOIN session_states st ON st.sessionId = s.sessionId
            ORDER BY COALESCE(
                (SELECT createdAt FROM messages m WHERE m.sessionId = s.sessionId
                  ORDER BY createdAt DESC, orderKey DESC LIMIT 1), s.updatedAt, s.createdAt) DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        out = []
        for r in rows:
            meta = (_parse_json(r["settings"], {}) or {}).get("metadata") or {}
            si = meta.get("subagentInput") or {}
            out.append({
                "id": r["sessionId"],
                "short": short_id(r["sessionId"]),
                "title": r["title"] or "",
                "cwd": r["cwd"] or "",
                "model": r["model"] or "",
                "provider": r["provider"] or "",
                "messages": r["n"] or 0,
                "lastAt": r["lastAt"] or r["updatedAt"] or r["createdAt"],
                "isSubagent": bool(meta.get("__subagentOrigin")) or r["sessionId"].startswith("subagent_"),
                "parentSessionId": meta.get("parentSessionId"),
                "subagentPrompt": (si.get("prompt") if isinstance(si, dict) else None),
            })
        return out
    finally:
        con.close()


def session_messages(sid: str, limit: int = 0):
    con = db_connect()
    try:
        rows = con.execute(
            "SELECT messageId, role, parts, metadata, createdAt FROM messages "
            "WHERE sessionId = ? ORDER BY createdAt, orderKey",
            (sid,),
        ).fetchall()
        if limit and len(rows) > limit:
            rows = rows[-limit:]
        return [
            {
                "messageId": r["messageId"],
                "role": r["role"],
                "parts": _parse_json(r["parts"], []) or [],
                "metadata": _parse_json(r["metadata"]),
                "createdAt": r["createdAt"],
            }
            for r in rows
        ]
    finally:
        con.close()


def last_assistant_text(sid: str, max_chars: int = 20000) -> str:
    for msg in reversed(session_messages(sid)):
        if msg["role"] != "assistant":
            continue
        texts = [p.get("text", "") for p in msg["parts"] if p.get("type") == "text" and p.get("text")]
        body = "\n".join(t.strip() for t in texts if t.strip())
        if body:
            return body if len(body) <= max_chars else body[:max_chars] + f"\n…（已截断，共 {len(body)} 字）"
    return ""


def final_assistant_text(sid: str, max_chars: int = 20000) -> str:
    """Prefer the last assistant text after the last user/run-terminal message."""
    msgs = session_messages(sid)
    last_user = -1
    for i, msg in enumerate(msgs):
        if msg.get("role") == "user":
            last_user = i
    candidates = msgs[last_user + 1:] if last_user >= 0 else msgs
    for msg in reversed(candidates):
        if msg.get("role") != "assistant":
            continue
        texts = [p.get("text", "") for p in msg.get("parts", []) if p.get("type") == "text" and p.get("text")]
        body = "\n".join(t.strip() for t in texts if t.strip())
        if body:
            return body if len(body) <= max_chars else body[:max_chars] + f"\n…（已截断，共 {len(body)} 字）"
    return ""


def session_usage(sid: str) -> dict:
    try:
        con = db_connect()
    except RuntimeError:
        return {}
    try:
        row = con.execute(
            "SELECT SUM(COALESCE(json_extract(usage,'$.totalTokens'),0)) AS tokens, "
            "SUM(COALESCE(cost,0)) AS cost FROM usage_ledger WHERE sessionId = ?",
            (sid,),
        ).fetchone()
        if row and row["tokens"]:
            return {"tokens": row["tokens"], "costUSD": round(row["cost"] or 0, 4)}
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return {}


def session_exists(sid: str) -> bool:
    try:
        con = db_connect()
    except RuntimeError:
        return False
    try:
        return con.execute("SELECT 1 FROM sessions WHERE sessionId = ?", (sid,)).fetchone() is not None
    finally:
        con.close()


def session_context(sid: str) -> dict:
    try:
        con = db_connect()
    except RuntimeError:
        return {}
    try:
        row = con.execute(
            """
            SELECT s.cwd, st.selectedProviderId AS provider, st.selectedModelId AS model
            FROM sessions s
            LEFT JOIN session_states st ON st.sessionId = s.sessionId
            WHERE s.sessionId = ?
            """,
            (sid,),
        ).fetchone()
        if not row:
            return {}
        return {
            "cwd": row["cwd"] or "",
            "provider": row["provider"] or "",
            "model": row["model"] or "",
        }
    finally:
        con.close()


def session_cwd(sid: str) -> str:
    return session_context(sid).get("cwd", "")


# ---------------------------------------------------------------- 任务与日志

def runs_dir() -> str:
    path = _expand(str(CFG.get("runs_dir")))
    os.makedirs(path, exist_ok=True)
    return path


def job_meta_path(job_id: str) -> str:
    return os.path.join(runs_dir(), job_id + ".json")


def job_log_path(job_id: str) -> str:
    return os.path.join(runs_dir(), job_id + ".jsonl")


def job_process_log_path(job_id: str) -> str:
    return os.path.join(runs_dir(), job_id + ".process.log")


def job_feedback_dir() -> str:
    path = os.path.join(DATA_DIR, "feedback")
    os.makedirs(path, exist_ok=True)
    return path


def job_pid_path(job_id: str) -> str:
    return os.path.join(runs_dir(), job_id + ".pid")


def job_save(meta: dict) -> None:
    with open(job_meta_path(meta["jobId"]), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)


def job_load(job_id: str):
    path = job_meta_path(job_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def job_list(limit: int = 20):
    files = glob.glob(os.path.join(runs_dir(), "job_*.json"))
    files.sort(key=os.path.getmtime, reverse=True)
    out = []
    for f in files[:limit]:
        try:
            with open(f, encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return out


def newest_job():
    jobs = job_list(1)
    return jobs[0] if jobs else None


def find_job(session_id: str = "", job_id: str = ""):
    if job_id:
        return job_load(job_id)
    for job in job_list(200):
        if job.get("sessionId") == session_id:
            return job
    return None


_live_procs: dict = {}


def scan_stream(log_file: str) -> dict:
    """解析 --json 事件流，判断进度。"""
    info = {
        "events": 0, "sessionId": "", "runId": "", "status": "unknown", "reason": "",
        "text": "", "textChars": 0, "lastEvent": "", "toolCalls": 0, "errors": [],
        "usage": {}, "contextTokens": None, "startedAt": "", "endedAt": "",
        "lastEventAt": "", "tools": [], "thinkingChars": 0, "thinkingTail": "",
        "provider": "", "model": "", "errorName": "", "errorMessage": "",
        "errorCode": "", "toolFailures": [], "runError": {},
        "outputTruncated": False, "continuationExhausted": False,
        "insufficientBalance": False, "httpStatus": None,
        "webCalls": 0,
    }
    if not os.path.isfile(log_file):
        return info
    cur_msg = None
    parts: dict = {}
    try:
        with open(log_file, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    if line:
                        info["errors"].append(line[:300])
                    continue
                try:
                    evt = json.loads(line)
                except ValueError:
                    continue
                info["events"] += 1
                info["sessionId"] = evt.get("sessionId") or info["sessionId"]
                info["runId"] = evt.get("runId") or info["runId"]
                info["lastEventAt"] = evt.get("createdAt") or info["lastEventAt"]
                etype = evt.get("eventType") or ""
                info["lastEvent"] = etype
                payload = evt.get("payload") or {}
                info["provider"] = payload.get("providerId") or info["provider"]
                info["model"] = payload.get("modelId") or info["model"]
                if not info["startedAt"]:
                    info["startedAt"] = evt.get("createdAt") or ""
                if etype == "run:started":
                    info["status"] = "running"
                elif etype == "message:started" and payload.get("role") == "assistant":
                    cur_msg = payload.get("messageId")
                    parts.setdefault(cur_msg, [])
                elif etype == "text:delta":
                    mid = payload.get("messageId") or cur_msg
                    parts.setdefault(mid, []).append(payload.get("delta") or "")
                elif etype == "thinking:delta":
                    delta = payload.get("delta") or ""
                    info["thinkingChars"] += len(delta)
                    info["thinkingTail"] = (info["thinkingTail"] + delta)[-600:]
                elif etype in ("tool:started", "tool:call:started"):
                    info["toolCalls"] += 1
                    tool_name = str(payload.get("toolName") or "").lower()
                    if tool_name in ("websearch", "webfetch", "web_search", "web_fetch"):
                        info["webCalls"] += 1
                    info["tools"].append({
                        "name": payload.get("toolName") or "?",
                        "input": payload.get("toolInput"),
                        "callId": payload.get("toolCallId"),
                        "status": "running",
                    })
                    if len(info["tools"]) > 50:
                        info["tools"] = info["tools"][-50:]
                elif etype in ("tool:completed", "tool:call:completed"):
                    call_id = payload.get("toolCallId")
                    for item in reversed(info["tools"]):
                        if call_id and item.get("callId") == call_id:
                            item["status"] = "done"
                            result = payload.get("toolResult")
                            try:
                                item["resultChars"] = len(json.dumps(result, ensure_ascii=False))
                            except (TypeError, ValueError):
                                item["resultChars"] = 0
                            break
                elif etype == "tool:failed":
                    err = payload.get("error") or {}
                    entry = {
                        "name": err.get("name") or "ToolError",
                        "message": str(err.get("message") or "")[:1200],
                        "tool": payload.get("toolName") or "",
                    }
                    info["toolFailures"].append(entry)
                    low = entry["message"].lower()
                    if "output_truncated" in low:
                        info["outputTruncated"] = True
                    if "continuation budget" in low:
                        info["continuationExhausted"] = True
                elif etype == "context:usage":
                    info["contextTokens"] = payload.get("usedTokens")
                elif etype == "run:ended":
                    status = payload.get("status") or "ended"
                    # A force-stopped task can emit a synthetic failed run:ended
                    # when its child process dies. Keep the user-visible outcome.
                    if info["status"] != "cancelled":
                        info["status"] = {"completed": "completed", "failed": "failed",
                                          "interrupted": "interrupted"}.get(status, status)
                    info["reason"] = payload.get("reason") or ""
                    info["usage"] = payload.get("usage") or {}
                    info["endedAt"] = evt.get("createdAt") or ""
                    err = payload.get("error") or {}
                    if isinstance(err, dict) and err:
                        info["runError"] = {
                            k: err.get(k) for k in ("name", "message", "code") if err.get(k) is not None
                        }
                        info["errorName"] = err.get("name") or ""
                        info["errorMessage"] = _compact_error_message(str(err.get("message") or ""))
                        info["errorCode"] = err.get("code") or ""
                    terminal = payload.get("terminalMessage") or {}
                    if not isinstance(terminal, dict):
                        terminal = {}
                    tmeta = (terminal.get("metadata") or {}).get("_dimTranscript") or {}
                    if not isinstance(tmeta, dict):
                        tmeta = {}
                    notice = tmeta.get("notice") or {}
                    if not isinstance(notice, dict):
                        notice = {}
                    params_notice = notice.get("params") or {}
                    if not isinstance(params_notice, dict):
                        params_notice = {}
                    debug = params_notice.get("debugDetails") or notice.get("debugDetails") or {}
                    if not isinstance(debug, dict):
                        debug = {}
                    err_class = notice.get("errorClass") or ""
                    display = params_notice.get("displayCode") or notice.get("displayCode") or ""
                    status_code = debug.get("statusCode")
                    if status_code is not None:
                        info["httpStatus"] = status_code
                    if err_class == "quota_exhausted" or display in ("insufficient_balance", "quota_exhausted") or status_code == 402:
                        info["insufficientBalance"] = True
                        if not info["errorCode"]:
                            info["errorCode"] = display or "insufficient_balance"
                        info["errorMessage"] = _compact_error_message(
                            str(debug.get("message") or info["errorMessage"])
                        )
                    low = (
                        info["errorMessage"] + " " + info["reason"] + " "
                        + json.dumps(notice.get("debugDetails") or {}, ensure_ascii=False)
                    ).lower()
                    if "output_truncated" in low:
                        info["outputTruncated"] = True
                    if "continuation budget" in low and not info["insufficientBalance"]:
                        info["continuationExhausted"] = True
                    if "insufficient_balance" in low or "no active credit bucket" in low or "http error 402" in low:
                        info["insufficientBalance"] = True
                        info["httpStatus"] = info.get("httpStatus") or 402
                        info["continuationExhausted"] = False
    except OSError as exc:
        info["errors"].append(str(exc))
    # 以最后一条完整 assistant 消息为当前文本
    if parts:
        last_key = list(parts.keys())[-1]
        info["text"] = "".join(parts[last_key])
        info["textChars"] = sum(len("".join(v)) for v in parts.values())
    return info


def _windows_process_start(pid: int) -> float:
    """Return process creation time as Unix epoch, or 0 if inaccessible."""
    if not IS_WINDOWS:
        return 0.0
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return 0.0
    try:
        created = ctypes.c_ulonglong()
        exited = ctypes.c_ulonglong()
        kernel = ctypes.c_ulonglong()
        user = ctypes.c_ulonglong()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        ):
            return 0.0
        # FILETIME is 100ns intervals since 1601-01-01.
        return float(created.value) / 10_000_000.0 - 11644473600.0
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid, started_at_epoch=None) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        created = _windows_process_start(pid)
        if created <= 0:
            return False
        job_start = float(started_at_epoch or 0)
        # Fast executable identity check for long-running jobs. A task's CLI
        # process keeps its own PID; tasklist adds negligible cost for one PID.
        if job_start and time.time() - job_start > 120 and not _pid_is_dimagent_process(pid):
            return False
        if started_at_epoch:
            start = float(started_at_epoch)
            # A recycled PID can also be newer than the job; only accept the
            # actual startup window.
            if created < start - 10 or created > start + 120:
                return False
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_is_dimagent_process(pid) -> bool:
    if not IS_WINDOWS:
        return True
    try:
        import subprocess as _sp
        out = _sp.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=4,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout or ""
        low = out.lower()
        return "dim.exe" in low or "dimcode" in low
    except (OSError, subprocess.SubprocessError):
        return False


def _pid_alive_loose(pid) -> bool:
    """Only used for MCP peer discovery; no start-time window."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        return _windows_process_start(pid) > 0
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_matches(job: dict, proc=None) -> bool:
    """Strict liveness check: PID must be alive and created at job start."""
    if proc is not None:
        return proc.poll() is None
    return _pid_alive(job.get("pid"), job.get("startedAtEpoch"))


def _mtime_epoch(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def job_status(job: dict) -> dict:
    log_file = job.get("logFile") or job_log_path(job.get("jobId", ""))
    info = scan_stream(log_file)
    job_id = job.get("jobId", "")
    proc = _live_procs.get(job_id)
    code = proc.poll() if proc is not None else None
    alive = False
    if proc is not None:
        alive = code is None
    else:
        alive = _pid_alive(job.get("pid"), job.get("startedAtEpoch"))
    if info["status"] in ("unknown", "running"):
        if job.get("stopRequested") and not alive:
            info["status"] = "cancelled"
            info["reason"] = info.get("reason") or (
                "force_stopped" if job.get("stopRequested") == "force" else "stopped"
            )
        elif code is not None:
            info["status"] = "completed" if code == 0 else "failed"
            info["exitCode"] = code
        elif info.get("endedAt"):
            # run:ended is authoritative even if the OS has briefly recycled/kept a pid.
            if info["status"] == "unknown":
                info["status"] = "completed"
        elif not alive and not job.get("stopRequested"):
            last_activity = max(
                _mtime_epoch(log_file),
                _mtime_epoch(job_process_log_path(job_id)),
            )
            idle = time.time() - (last_activity or (job.get("startedAtEpoch") or 0))
            stale_after = float(CFG.get("stale_after_seconds") or 90)
            info["idleSeconds"] = int(max(0, idle))
            if idle > stale_after:
                info["status"] = "stale"
                info["reason"] = info.get("reason") or "process_missing_or_no_events"
            else:
                info["status"] = "starting"
        else:
            info["status"] = "running"
    if info.get("endedAt") and info["status"] in ("running", "starting") and not job.get("stopRequested"):
        info["status"] = "completed"
    if job.get("stopRequested") and info["status"] in ("failed", "interrupted") and not info.get("insufficientBalance"):
        info["status"] = "cancelled"
        info["reason"] = "force_stopped" if job.get("stopRequested") == "force" else "stopped"
    if job.get("stopRequested") and info["status"] in ("running", "starting") and not alive:
        info["status"] = "cancelled"
        info["reason"] = "force_stopped" if job.get("stopRequested") == "force" else "stopped"
    info["jobId"] = job_id
    info["pid"] = job.get("pid")
    info["pidAlive"] = alive
    info["cwd"] = job.get("cwd")
    info["requestedModel"] = job.get("model")
    info["requestedProvider"] = job.get("provider")
    info["requestedReasoningEffort"] = job.get("reasoningEffort")
    info["requestedPolicy"] = job.get("policy")
    info["effectiveModel"] = info.get("model") or ""
    info["effectiveProvider"] = info.get("provider") or ""
    info["model"] = info.get("effectiveModel") or job.get("model")
    info["provider"] = info.get("effectiveProvider") or job.get("provider")
    info["resume"] = bool(job.get("resume") or job.get("resumeLast"))
    sid = info.get("sessionId") or job.get("sessionId") or ""
    info["sessionId"] = sid
    if sid and job.get("sessionId") != sid:
        job["sessionId"] = sid
        job_save(job)
    return info


def build_exec_cmd(args: dict):
    resume = bool(args.get("session_id") and args.get("resume"))
    resume_last = bool(args.get("resume_last"))
    if resume or resume_last:
        cmd = [dim_path(), "exec", "--json", "resume"]
        if resume_last:
            cmd.append("--last")
        else:
            cmd.append(args["session_id"])
        # resume 保留旧会话的模型与权限，但官方支持覆盖本轮 mode、推理档位、
        # 验收命令和修复轮数；不要把这些能生效的参数静默丢掉。
        if args.get("mode"):
            cmd += ["--mode", args["mode"]]
        if args.get("reasoning_effort"):
            cmd += ["--reasoning-effort", args["reasoning_effort"]]
        if args.get("verify"):
            cmd += ["--verify", args["verify"]]
        if args.get("repair_attempts") is not None:
            cmd += ["--repair-attempts", str(args["repair_attempts"])]
        for img in (args.get("images") or []):
            cmd += ["--image", img]
        cmd.append(args["prompt"])
        return cmd

    cmd = [dim_path(), "exec", "--json"]
    if args.get("session_id"):
        cmd += ["--session-id", args["session_id"]]
    if args.get("provider"):
        cmd += ["--provider", args["provider"]]
    if args.get("model"):
        cmd += ["--model", args["model"]]
    if args.get("reasoning_effort"):
        cmd += ["--reasoning-effort", args["reasoning_effort"]]
    if args.get("mode"):
        cmd += ["--mode", args["mode"]]
    if args.get("policy"):
        cmd += ["--policy", args["policy"]]
    if args.get("verify"):
        cmd += ["--verify", args["verify"]]
    if args.get("repair_attempts") is not None:
        cmd += ["--repair-attempts", str(args["repair_attempts"])]
    if args.get("allowed_tools"):
        cmd += ["--allowed-tools", args["allowed_tools"]]
    if args.get("disallowed_tools"):
        cmd += ["--disallowed-tools", args["disallowed_tools"]]
    if args.get("tools"):
        cmd += ["--tools", args["tools"]]
    if args.get("append_system_prompt"):
        cmd += ["--append-system-prompt", args["append_system_prompt"]]
    for img in (args.get("images") or []):
        cmd += ["--image", img]
    cmd.append(args["prompt"])
    return cmd


def _merge_tool_lists(*values) -> str:
    seen = []
    for value in values:
        if not value:
            continue
        for part in str(value).split(","):
            part = part.strip()
            if part and part.lower() not in {x.lower() for x in seen}:
                seen.append(part)
    return ",".join(seen)


def _prepare_run_args(args: dict) -> tuple[dict, list, list]:
    """Normalize defaults and return (args, warnings, rejected_conflicts)."""
    args = _normalize_model_args(args)
    warnings = []
    conflicts = []
    if args.get("session_id"):
        context = session_context(args["session_id"])
        inherited_cwd = context.get("cwd", "")
        if inherited_cwd and not args.get("cwd"):
            args["cwd"] = inherited_cwd
        if context.get("provider"):
            args["_sessionProvider"] = context["provider"]
        if context.get("model"):
            args["_sessionModel"] = context["model"]
    existing_session = _uses_existing_session(args)
    if existing_session:
        permanent_overrides = {
            "provider": args.get("provider"),
            "model": args.get("model"),
            "policy": args.get("policy"),
            "allowed_tools": args.get("allowed_tools"),
            "disallowed_tools": args.get("disallowed_tools"),
        }
        conflicts = [k for k, v in permanent_overrides.items() if v not in (None, "", False)]
        if args.get("resume") or args.get("resume_last"):
            resume_only_dropped = {
                "tools": args.get("tools"),
                "append_system_prompt": args.get("append_system_prompt"),
            }
            conflicts += [
                k for k, v in resume_only_dropped.items() if v not in (None, "", False)
            ]
        warnings.append(
            "载入已有会话会沿用原模型、provider 与权限；默认 WebSearch/WebFetch "
            "禁用规则也不会改写旧会话。需要严格隔离时请新建任务。"
        )
    else:
        if args.get("_providerDefaulted"):
            warnings.append(
                f"未显式指定 provider，已优先使用 {args.get('provider')}（避免继承桌面端 custom provider）。"
            )
        if CFG.get("default_disallow_web", True) and not args.get("allow_web"):
            args["disallowed_tools"] = _merge_tool_lists(
                args.get("disallowed_tools"), "WebSearch,WebFetch"
            )
        elif args.get("allow_web"):
            warnings.append("本次显式允许 WebSearch/WebFetch，会消耗后台搜索配额。")
        if CFG.get("default_disallow_web", True):
            warnings.append("默认已禁用 WebSearch/WebFetch（allow_web=true 可开启）。")
        prompt_text = args.get("prompt") or ""
        writing_task = any(k in prompt_text for k in ("写", "创建", "生成", "修改", "实现", "补全", "write", "edit", "create"))
        if CFG.get("output_guard", True) and writing_task:
            guard = (
                "\n\n[输出约束] 长文件分段写盘，单次 write/edit 控制在 500 行以内；"
                "每段完成后说明已写路径和下一步。简单问答不需要落盘，也不要额外写文件。"
            )
            if guard.strip() not in (args.get("prompt") or ""):
                args["prompt"] = (args.get("prompt") or "").rstrip() + guard
    return args, warnings, conflicts


def _attach_actual_runtime(meta: dict, info: dict) -> None:
    changed = False
    if info.get("effectiveProvider") and meta.get("actualProvider") != info["effectiveProvider"]:
        meta["actualProvider"] = info["effectiveProvider"]
        changed = True
    if info.get("effectiveModel") and meta.get("actualModel") != info["effectiveModel"]:
        meta["actualModel"] = info["effectiveModel"]
        changed = True
    if info.get("endedAt") and meta.get("endedAt") != info["endedAt"]:
        meta["endedAt"] = info["endedAt"]
        changed = True
    if changed:
        job_save(meta)


def start_job(args: dict) -> dict:
    cwd = _expand(str(args.get("cwd") or default_cwd() or HOME))
    os.makedirs(cwd, exist_ok=True)
    job_id = "job_" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    log_file = job_log_path(job_id)
    process_log = job_process_log_path(job_id)
    cmd = build_exec_cmd(args)
    existing_session = _uses_existing_session(args)
    meta = {
        "jobId": job_id,
        "sessionId": args.get("session_id") or "",
        "cmd": cmd,
        "cwd": cwd,
        "model": args.get("model") or args.get("_sessionModel") or (
            "" if existing_session else CFG.get("default_model")
        ),
        "reasoningEffort": args.get("reasoning_effort") or (
            "" if existing_session else CFG.get("default_reasoning_effort")
        ),
        "policy": args.get("policy") or (
            "" if existing_session else CFG.get("default_policy")
        ),
        "mode": args.get("mode") or "agent",
        "provider": args.get("provider") or args.get("_sessionProvider") or "",
        "resume": bool(args.get("session_id") and args.get("resume")),
        "resumeLast": bool(args.get("resume_last")),
        "prompt": args["prompt"],
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "startedAtEpoch": time.time(),
        "logFile": log_file,
        "processLog": process_log,
        "pid": None,
    }
    creation = 0
    if IS_WINDOWS:
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    log_handle = open(log_file, "w", encoding="utf-8", errors="replace")
    err_handle = open(process_log, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=log_handle, stderr=err_handle,
        stdin=subprocess.DEVNULL, creationflags=creation, close_fds=True,
    )
    log_handle.close()
    err_handle.close()
    _live_procs[job_id] = proc
    meta["pid"] = proc.pid
    job_save(meta)
    try:
        with open(job_pid_path(job_id), "w", encoding="ascii") as fh:
            fh.write(str(proc.pid))
    except OSError:
        pass
    log(f"start {job_id} pid={proc.pid} cwd={cwd} cmd={' '.join(cmd[:8])}…")
    return meta


def wait_for_job(job: dict, timeout: float, poll: float = 2.0) -> dict:
    timeout = max(0.0, min(float(timeout or 0), float(CFG.get("max_wait_seconds") or 240)))
    deadline = time.time() + max(0.0, timeout)
    while True:
        info = job_status(job)
        if info["status"] in ("completed", "failed", "interrupted", "cancelled", "stale"):
            return info
        if time.time() >= deadline:
            return info
        time.sleep(poll)


# ---------------------------------------------------------------- 监控页

def viewer_health(url: str = "") -> bool:
    target = (url or CFG.get("viewer_url") or "").rstrip("/") + "/api/sessions"
    try:
        with urllib.request.urlopen(target, timeout=1.5) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def ensure_viewer() -> dict:
    url = str(CFG.get("viewer_url") or "")
    if viewer_health(url):
        return {"running": True, "url": url, "started": False}
    if not CFG.get("auto_start_viewer", True):
        return {"running": False, "url": url, "started": False,
                "note": "auto_start_viewer 关闭，未自动拉起"}
    script = _expand(str(CFG.get("viewer_script") or ""))
    if not os.path.isfile(script):
        return {"running": False, "url": url, "started": False,
                "note": f"找不到监控页服务脚本：{script}"}
    port = str(CFG.get("viewer_port") or 8790)
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
    try:
        subprocess.Popen([sys.executable, script, "--port", port],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=creation, close_fds=True)
    except OSError as exc:
        return {"running": False, "url": url, "started": False, "note": f"启动失败：{exc}"}
    for _ in range(20):
        time.sleep(0.35)
        if viewer_health(url):
            return {"running": True, "url": url, "started": True}
    return {"running": False, "url": url, "started": True, "note": "已启动但健康检查未通过"}


# ---------------------------------------------------------------- 工具实现

MODELS_HINT = (
    f"常用模型（默认 provider: {CFG.get('default_provider') or 'opencode-go'}；每个模型仍需按 provider 核对可用性）：\n"
    "  deepseek-v4.1-flash  · 默认，便宜快；在图/文档支持上不要盲信旧标注\n"
    "  qwen3.8-max          · 强但贵；xhigh 时最常用\n"
    "  qwen3.8-flash        · 便宜\n"
    "  glm-5.3-flash        · 部分 provider 有，可能不支持工具协议\n"
    "  mimo-v2.6-flash/pro  · 型号可用性随 provider 变化\n"
    "  用 dimagent_cli(\"model list\") 看全部\n"
)

TOOLS_HINT = (
    "自带工具：read / write / edit / exec / glob / grep / skill / agent / schedule / todowrite / wait / workflow / goal；"
    "内置 Web 插件还提供 WebSearch 与 WebFetch（走 dim-oauth 额度）。\n"
    "⚠️ WebSearch/WebFetch 有配额限制：默认已自动禁用；确需联网时显式传 allow_web=true。"
    "写代码/读文件/跑命令不消耗配额。\n"
    "想限定工具范围：tools=\"read,glob,grep\"，或 disallowed_tools=\"exec\"；只读审查用 policy=\"read-only\"。"
)


def tool_doctor(args: dict) -> str:
    lines = []
    lines.append(f"MCP版本  : {VERSION} · pid {os.getpid()} · 工具 {len(TOOLS)}")
    lines.append(f"启动时间 : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(STARTED_AT))}")
    try:
        path, source = resolve_dim(force=True)
        lines.append(f"dim      : {path}")
        lines.append(f"解析来源 : {source}")
        lines.append(f"版本     : {dim_version()}")
    except RuntimeError as exc:
        lines.append("dim      : 未找到")
        lines.append(str(exc))
    cfg_file = CFG.get("_config_file")
    lines.append(f"配置文件 : {cfg_file or '（未使用，走自动发现）'}")
    lines.append(f"数据库   : {db_path()} {'✓' if os.path.isfile(db_path()) else '✗ 不存在'}")
    lines.append(
        f"默认 cwd : {default_cwd()}"
        + ("（自动跟随 Codex 项目目录）" if not CFG.get("default_cwd") else "")
    )
    lines.append(f"MCP cwd  : {os.getcwd()}")
    lines.append(f"默认provider: {CFG.get('default_provider')}")
    lines.append(f"默认模型 : {CFG.get('default_model')} / effort {CFG.get('default_reasoning_effort')} / policy {CFG.get('default_policy')}")
    lines.append(f"等待上限 : {CFG.get('max_wait_seconds')}s（宿主约 300s 截断）")
    lines.append(f"联网默认 : {'禁用 WebSearch/WebFetch' if CFG.get('default_disallow_web', True) else '开放'}")
    try:
        n = len(list_sessions(200))
        lines.append(f"会话数   : {n}")
    except RuntimeError as exc:
        lines.append(f"会话数   : 读取失败（{exc}）")
    v = ensure_viewer()
    lines.append(f"监控页   : {v['url']} {'运行中' if v['running'] else '未运行'}"
                 + (f"（{v.get('note')}）" if v.get("note") else ""))
    lines.append(f"MCP实例  : {os.getpid()}（每个宿主会话可能各有一个 stdio 实例，状态不共享）")
    lines.append(f"模型表来源: {'SQLite providers' if list_models().get('source') == 'sqlite' else 'CLI model list'}")
    mcp_dir = os.path.join(STATE_DIR, "mcp")
    procs = []
    if os.path.isdir(mcp_dir):
        for name in sorted(os.listdir(mcp_dir)):
            if not name.endswith(".pid"):
                continue
            try:
                with open(os.path.join(mcp_dir, name), encoding="ascii") as fh:
                    pid = int(fh.read().strip())
            except (OSError, ValueError):
                continue
            if _pid_alive_loose(pid):
                procs.append((name, pid))
    if procs:
        lines.append("其他MCP  : " + ", ".join(f"{n}={p}" for n, p in procs[:12]))
    lines.append(f"任务目录 : {runs_dir()}")
    lines.append(f"反馈目录 : {job_feedback_dir()}")
    lines.append(f"状态目录 : {STATE_DIR}")
    return "\n".join(lines)


def tool_run(args: dict) -> str:
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return "错误：prompt 不能为空。"
    if args.get("resume_last") and args.get("session_id"):
        return "错误：resume_last 与 session_id 只能选一个。"
    if args.get("resume") and not args.get("session_id") and not args.get("resume_last"):
        return "错误：resume=true 需要同时提供 session_id；恢复最近会话请改用 resume_last=true。"
    args, warnings, conflicts = _prepare_run_args(args)
    if conflicts:
        return (
            "错误：载入已有会话（session_id / resume_last）时，DimAgent 会沿用旧会话的"
            "模型、provider 与权限配置；本次传入的覆盖参数不会生效，已拒绝执行：\n  "
            + ", ".join(conflicts)
            + "\n\n请去掉这些参数后重试；如果要换模型/权限，请新建任务。"
        )
    if args.get("session_id") and not session_exists(args["session_id"]):
        return f"错误：session {args['session_id']} 不存在（用 dimagent_sessions 查 id）。"
    existing_session = _uses_existing_session(args)
    if not existing_session:
        check = preflight_model(args)
        if check.startswith("模型预检失败"):
            if args.get("_providerDefaulted"):
                fallback = _fallback_provider_model(args)
                if fallback:
                    args["provider"] = fallback[0]
                    args["model"] = fallback[1]
                    check = ""
                    warnings.append(
                        f"默认 provider 不可用，已回退到 {fallback[0]} / {fallback[1]}。"
                    )
            if check:
                return "错误：" + check
        if check:
            warnings.append(check)
    input_check = preflight_inputs(args, check_vision=not existing_session)
    if input_check:
        return "错误：" + input_check
    cwd = _expand(str(args.get("cwd") or default_cwd() or HOME))
    if not os.path.isdir(cwd):
        return f"错误：工作目录不存在：{cwd}\n先创建它，或改传一个存在的 cwd。"
    meta = start_job(args)
    viewer = ensure_viewer() if args.get("open_viewer", True) else {"running": viewer_health(), "url": str(CFG.get("viewer_url") or "")}
    requested_wait = max(0.0, float(args.get("wait_seconds") or 0))
    max_wait = float(CFG.get("max_wait_seconds") or 240)
    wait_seconds = min(requested_wait, max_wait)
    lines = [
        f"已派发到 DimAgent：{meta['jobId']}",
        f"  工作目录 : {meta['cwd']}",
        f"  请求参数 : {meta['provider'] or '-'} / {meta['model']} / effort {meta['reasoningEffort']} / policy {meta['policy']} / mode {meta['mode']}",
        f"  事件流   : {meta['logFile']}",
        f"  进程日志 : {meta['processLog']}",
    ]
    if wait_seconds > 0:
        info = wait_for_job(meta, wait_seconds)
        _attach_actual_runtime(meta, info)
        capped = f"（请求 {requested_wait:g}s，已按上限封顶）" if requested_wait > max_wait else ""
        lines.append(f"  等待 {wait_seconds:g}s 后状态：{info['status']}{capped}")
        if info.get("sessionId"):
            meta["sessionId"] = info["sessionId"]
            job_save(meta)
    info = job_status(meta)
    _attach_actual_runtime(meta, info)
    if info.get("sessionId"):
        meta["sessionId"] = info["sessionId"]
        job_save(meta)
        lines.append(f"  sessionId: {info['sessionId']}")
    if info.get("effectiveModel") or info.get("effectiveProvider"):
        lines.append(f"  实际运行 : {info.get('effectiveProvider') or '-'} / {info.get('effectiveModel') or '-'}")
    if info.get("pid"):
        lines.append(f"  pid      : {info['pid']}（{'存活' if info.get('pidAlive') else '未存活'}）")
    for warning in warnings:
        lines.append(f"  注意     : {warning}")
    if viewer.get("running"):
        lines.append(f"实时监控页：{viewer['url']}（已在运行{'，本次新拉起' if viewer.get('started') else ''}）")
    else:
        lines.append(f"实时监控页：{viewer.get('url')} 未运行（{viewer.get('note', '未知原因')}）")
    lines.append("")
    lines.append("下一步：dimagent_status / dimagent_wait / dimagent_result；监控页可用 open_in_codex 的 browser 打开。")
    lines.append(TOOLS_HINT)
    lines.append("")
    lines.append(MODELS_HINT)
    return "\n".join(lines)


def tool_status(args: dict) -> str:
    job = None
    if args.get("job_id"):
        job = job_load(args["job_id"])
    elif args.get("session_id"):
        job = find_job(session_id=args["session_id"])
    else:
        job = newest_job()
    if not job:
        sid = args.get("session_id") or ""
        if sid and session_exists(sid):
            # 不是这个工具派发的会话（比如用户在 TUI/桌面版里跑的）：只给数据库视角
            msgs = session_messages(sid)
            usage = session_usage(sid)
            lines = [
                f"sessionId: {sid}",
                "来源     : 不是本工具派发（没有事件流），以下是数据库视角",
                f"消息数   : {len(msgs)}",
            ]
            if usage:
                lines.append(f"用量     : {usage.get('tokens')} tokens")
            tools_used = []
            for m in msgs:
                for part in m["parts"]:
                    if part.get("type") == "tool_use" and part.get("name"):
                        tools_used.append(part["name"])
            if tools_used:
                lines.append("用过工具 : " + ", ".join(tools_used))
            body = last_assistant_text(sid, 1500)
            if body:
                lines += ["", "最后回答：", body]
            lines += ["", f"实时监控页：{CFG.get('viewer_url')}（能看到思考链与工具调用全过程）"]
            return "\n".join(lines)
        return "没有找到对应任务（用 dimagent_sessions 看会话，或检查 runs 目录）。"
    info = job_status(job)
    _attach_actual_runtime(job, info)
    sid = info.get("sessionId") or job.get("sessionId") or ""
    found_jobs = [j for j in job_list(300) if j.get("sessionId") == sid and sid]
    prior_jobs = [j for j in found_jobs if j.get("jobId") != job["jobId"]]
    lines = [
        f"任务     : {job['jobId']}",
        f"状态     : {info['status']}"
        + (f"（reason={info['reason']}）" if info.get("reason") else ""),
        f"sessionId: {sid or '（尚未产生）'}",
        f"目录     : {job.get('cwd')}",
        f"请求参数 : {job.get('provider') or '-'} / {job.get('model')} / {job.get('reasoningEffort')} / policy {job.get('policy')}",
        f"实际运行 : {info.get('effectiveProvider') or '-'} / {info.get('effectiveModel') or '-'}",
        f"进程     : pid {job.get('pid') or '-'}（{'存活' if info.get('pidAlive') else '未存活'}）"
        + (f"，idle {info['idleSeconds']}s" if info.get("idleSeconds") is not None else ""),
        f"事件     : {info['events']} 条，最后事件 {info.get('lastEvent') or '-'}，工具调用 {info['toolCalls']}",
        f"触发原因 : {info.get('reason') or '-'}",
    ]
    if prior_jobs:
        lines.append(f"会话历史 : 同一会话有 {len(prior_jobs)} 个更早 job（列表显示的是最新 job）")
    if info.get("errorName") or info.get("errorMessage"):
        lines.append(
            "错误     : " + (info.get("errorName") or "Error")
            + (f"（{info.get('errorCode')}）" if info.get("errorCode") else "")
        )
        msg = " ".join(str(info.get("errorMessage") or "").split())
        if msg:
            lines.append("           " + (msg[:1200] + ("…" if len(msg) > 1200 else "")))
    if info.get("insufficientBalance"):
        lines.append("余额状态 : provider 返回 402 / insufficient_balance，本轮额度不足")
    if info.get("toolFailures"):
        last = info["toolFailures"][-1]
        lines.append(f"工具错误 : {last.get('tool') or '-'} · {last.get('name')} · {last.get('message', '')[:600]}")
    if info.get("outputTruncated"):
        lines.append("输出状态 : 发生 output_truncated（已尽量落盘；建议拆分输出并 resume）")
    if info.get("continuationExhausted"):
        lines.append("续跑预算 : continuation budget 已耗尽，任务被判失败")
    if info.get("contextTokens"):
        lines.append(f"上下文   : {info['contextTokens']} tokens")
    if info.get("usage"):
        u = info["usage"]
        lines.append(f"用量     : prompt {u.get('promptTokens', 0)} + completion {u.get('completionTokens', 0)} = {u.get('totalTokens', 0)}")
    if info.get("tools"):
        recent = info["tools"][-4:]
        lines.append("工具调用 : " + " -> ".join(
            f"{t['name']}({tool_brief(t.get('input'))})"
            + ("[完成]" if t.get("status") == "done" else "[进行中]")
            for t in recent))
    if info.get("webCalls"):
        lines.append(f"联网调用 : {info['webCalls']} 次（WebSearch/WebFetch）")
    if info.get("thinkingChars"):
        tail = " ".join(str(info.get("thinkingTail") or "").split())
        lines.append(f"思考链   : 已输出 {info['thinkingChars']} 字" + (f"，最新：…{tail[-140:]}" if tail else ""))
    if info["text"]:
        tail = args.get("tail_chars") or 1200
        text = info["text"]
        lines.append("")
        lines.append(f"当前输出（末 {tail} 字）：")
        lines.append(text[-int(tail):])
    if sid:
        try:
            n = len(session_messages(sid))
            lines.append("")
            lines.append(f"数据库里这个会话已有 {n} 条消息。")
        except RuntimeError:
            pass
    process_log = job.get("processLog") or job_process_log_path(job.get("jobId", ""))
    if os.path.isfile(process_log):
        try:
            with open(process_log, encoding="utf-8", errors="replace") as fh:
                tail = fh.readlines()[-8:]
        except OSError:
            tail = []
        if tail:
            lines.append("")
            lines.append("进程 stderr（末 8 行）：")
            lines.extend(" ".join(x.strip().split())[:500] for x in tail if x.strip())
    return "\n".join(lines)


def tool_wait(args: dict) -> str:
    job = job_load(args["job_id"]) if args.get("job_id") else (
        find_job(session_id=args["session_id"]) if args.get("session_id") else newest_job())
    if not job:
        return "没有找到对应任务。"
    max_wait = float(CFG.get("max_wait_seconds") or 240)
    requested = float(args.get("timeout_seconds") or 120)
    timeout = min(requested, max_wait)
    info = wait_for_job(job, timeout, poll=float(args.get("poll_seconds") or 2.5))
    _attach_actual_runtime(job, info)
    sid = info.get("sessionId") or job.get("sessionId") or ""
    if sid:
        job["sessionId"] = sid
        job_save(job)
    capped = "（已按宿主限制封顶）" if requested > max_wait else ""
    if info["status"] not in ("completed", "failed", "interrupted", "cancelled", "stale"):
        return (f"还在跑（等了 {timeout:g}s{capped}）：{job['jobId']}\n"
                f"最后事件 {info.get('lastEvent') or '-'}，工具调用 {info['toolCalls']}\n"
                f"再调一次 dimagent_wait 可以继续等（单次上限 {max_wait:g}s）。")
    if info["status"] == "stale":
        return (
            f"任务卡死/进程已不在：{job['jobId']}\n"
            f"pid {job.get('pid')}，事件流 {info.get('idleSeconds')}s 无更新，最后事件 {info.get('lastEvent') or '-'}\n"
            "可用 dimagent_stop(job_id=..., force=true) 清理，或 dimagent_result 读取已落库内容。"
        )
    body = last_assistant_text(sid, int(args.get("max_chars") or 8000)) if sid else ""
    head = f"任务结束：{info['status']}" + (f"（{info['reason']}）" if info.get("reason") else "")
    usage = session_usage(sid) if sid else {}
    if usage:
        head += f"\n用量：{usage.get('tokens')} tokens"
    return head + "\n\n" + (body or "（没有拿到最终文本，用 dimagent_result 或监控页看看）")


def tool_result(args: dict) -> str:
    sid = args.get("session_id") or ""
    job = None
    if not sid:
        job = job_load(args["job_id"]) if args.get("job_id") else newest_job()
        if job:
            sid = job_status(job).get("sessionId") or job.get("sessionId") or ""
    elif args.get("job_id"):
        job = job_load(args["job_id"])
    if not job and sid:
        job = find_job(session_id=sid)
    if not sid:
        sessions = list_sessions(1)
        sid = sessions[0]["id"] if sessions else ""
    if not sid:
        return "没有可用会话。"
    text = final_assistant_text(sid, int(args.get("max_chars") or 20000))
    head = [f"sessionId: {sid}", f"短名: {short_id(sid)}"]
    info = job_status(job) if job else None
    if info:
        if job:
            _attach_actual_runtime(job, info)
        head.append(f"状态: {info['status']}" + (f"（reason={info.get('reason')}）" if info.get("reason") else ""))
        if info.get("sessionId") == "":
            head.append("finalText: null")
        if info.get("errorName") or info.get("errorMessage"):
            head.append(f"error: {info.get('errorName') or 'Error'}")
            head.append(f"errorMessage: {str(info.get('errorMessage') or '')[:1200]}")
        if info.get("outputTruncated"):
            head.append("outputTruncated: true")
        if info.get("continuationExhausted"):
            head.append("continuationBudgetExhausted: true")
        if info.get("insufficientBalance"):
            head.append("insufficientBalance: true")
            head.append("httpStatus: 402")
    try:
        head.append(f"消息数: {len(session_messages(sid))}")
    except RuntimeError:
        pass
    usage = session_usage(sid)
    if usage:
        head.append(f"用量: {usage.get('tokens')} tokens" + (f" · ${usage['costUSD']}" if usage.get("costUSD") else ""))
    body = text or "（这个会话还没有最终文本输出）"
    if info and info["status"] in ("cancelled",):
        body = "finalText: null\n\n（任务已被停止，最后一段文本不是最终答案）"
    elif info and info["status"] not in ("completed", "running", "starting") and not text:
        body = "finalText: null\n\n（会话没有成功的最终回答，失败原因见上方 errorMessage）"
    elif info and info["status"] in ("failed", "interrupted") and text:
        body = "finalText: null\n\n（任务未成功结束，最后一段文本不是最终答案）"
    return "\n".join(head) + "\n\n" + body


def tool_sessions(args: dict) -> str:
    limit = min(int(args.get("limit") or 15), 100)
    sessions = list_sessions(limit)
    if not sessions:
        return "DimAgent 里还没有会话。"
    all_jobs = job_list(300)
    jobs_by_session = {}
    for job in all_jobs:
        sid = job.get("sessionId") or ""
        if not sid:
            continue
        prior = jobs_by_session.get(sid)
        if prior is None or (job.get("startedAtEpoch") or 0) > (prior.get("startedAtEpoch") or 0):
            jobs_by_session[sid] = job
    out = [f"最近 {len(sessions)} 个会话（时间倒序）：", ""]
    for s in sessions:
        title = s["title"] or (s["subagentPrompt"] or "")[:60] or "(无标题)"
        kind = "子代理" if s["isSubagent"] else "主会话"
        job = jobs_by_session.get(s["id"])
        state = "stored"
        if job:
            info = job_status(job)
            state = info["status"]
        out.append(f"- {s['short']}  [{kind}] [{state}] {title}")
        out.append(f"    id={s['id']}  {s['messages']} 条  {s['model'] or '-'}  {s['lastAt'] or ''}")
        if job and job.get("sessionId"):
            count = sum(1 for x in all_jobs if x.get("sessionId") == s["id"])
            if count > 1:
                out.append(f"    jobs={count}（列表仅展示最新；查历史用 job_id）")
        if s["cwd"]:
            out.append(f"    cwd={s['cwd']}")
    out.append("")
    out.append("说明：stored = 只有历史记录、没有本工具派发的运行任务；")
    out.append("提示：dimagent_result(session_id=...) 取最终结果；dimagent_stop 可停止仍在跑的任务。")
    return "\n".join(out)


def tool_viewer(args: dict) -> str:
    action = (args.get("action") or "ensure").lower()
    if action == "status":
        running = viewer_health()
        return f"监控页 {CFG.get('viewer_url')}：{'运行中' if running else '未运行'}"
    if action == "stop":
        return "监控页是只读小服务，这里不提供停止（需要的话用任务管理器结束 python 进程）。"
    info = ensure_viewer()
    if not info["running"]:
        return f"监控页未能启动：{info.get('note', '未知原因')}\n脚本：{CFG.get('viewer_script')}"
    note = "（本次刚拉起）" if info.get("started") else "（本来就在跑）"
    return (f"实时监控页：{info['url']} {note}\n"
            "在 Codex 侧边栏打开：用 open_in_codex 打开这个 URL（browser 类型），"
            "或直接在 @浏览器 里粘贴。页面 1.5 秒刷新一次，思考过程、工具调用、结果都能展开看。")


def tool_cli(args: dict) -> str:
    raw = args.get("args")
    if isinstance(raw, str):
        argv = [raw] if " " not in raw else raw.split()
    elif isinstance(raw, list):
        argv = [str(x) for x in raw]
    else:
        return "错误：args 需要是数组或字符串，例如 [\"session\",\"list\"] 或 \"model list\"。"
    if not argv:
        return "错误：args 不能为空。"
    joined = " ".join(argv).lower()
    if "provider" in joined and "list" in joined and "--json" in joined:
        return (
            "错误：provider list --json 输出约 190 万 token，已拦截。\n"
            "改用 dimagent_cli(\"model list\")（精简），或 dimagent_cli(\"provider get <id>\")。"
        )
    timeout = min(float(args.get("timeout_seconds") or 120), float(CFG.get("max_wait_seconds") or 240))
    cmd = [dim_path()] + argv
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"命令超时（{timeout:g}s）：dim {' '.join(argv)}"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    limit = int(args.get("max_chars") or 12000)
    def clip(text: str) -> str:
        return text if len(text) <= limit else text[:limit] + f"\n…（截断，共 {len(text)} 字）"
    body = [f"$ dim {' '.join(argv)}", f"exit={proc.returncode}"]
    if out:
        body += ["", clip(out)]
    if err:
        body += ["", "[stderr]", clip(err)]
    return "\n".join(body)


def tool_stop(args: dict) -> str:
    """优雅停止任务；默认只发中断，force=true 才强杀。"""
    job = None
    if args.get("job_id"):
        job = job_load(args["job_id"])
    elif args.get("session_id"):
        job = find_job(session_id=args["session_id"])
    else:
        job = newest_job()
    if not job:
        return "没有找到对应任务。"
    job_id = job["jobId"]
    pid = job.get("pid")
    proc = _live_procs.get(job_id)
    alive = (
        (proc is not None and proc.poll() is None)
        or _pid_alive(pid, job.get("startedAtEpoch"))
    )
    force = bool(args.get("force"))
    if not alive:
        info = job_status(job)
        return f"任务 {job_id} 已经不在运行（状态 {info['status']}，pid {pid}）。"
    if force:
        try:
            if proc is not None:
                proc.kill()
            elif IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=10,
                )
            else:
                os.kill(int(pid), signal.SIGKILL)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return f"强制停止失败：{type(exc).__name__}: {exc}"
        job["stopRequested"] = "force"
        job_save(job)
        time.sleep(0.8)
        info = job_status(job)
        if not info.get("pidAlive"):
            info["status"] = "cancelled"
            info["reason"] = "force_stopped"
        return (
            f"已强制停止 {job_id}（pid {pid}）。当前状态：{info['status']}。"
            "未落盘的工具结果可能丢失，请用 dimagent_result 检查。"
        )
    try:
        if proc is not None:
            if IS_WINDOWS:
                proc.terminate()
            else:
                proc.send_signal(signal.SIGINT)
        elif IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T"],
                capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=10,
            )
        else:
            os.kill(int(pid), signal.SIGTERM)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"停止请求失败：{type(exc).__name__}: {exc}\n可改用 force=true 强杀。"
    job["stopRequested"] = "graceful"
    job_save(job)
    deadline = time.time() + 5.0
    info = job_status(job)
    while info.get("pidAlive") and info["status"] in ("running", "starting") and time.time() < deadline:
        time.sleep(0.5)
        info = job_status(job)
    return (
        f"已向 {job_id}（pid {pid}）发送停止请求。\n"
        f"5s 观察窗口后：{info['status']}"
        + ("（进程仍在，可能正在收尾当前工具）" if info.get("pidAlive") else "（进程已退出）")
        + "\nDimAgent 可能先完成当前工具调用再退出；继续用 dimagent_status 确认。"
        "如果确实需要立即结束，可调 dimagent_stop(force=true)。"
    )


def tool_resume(args: dict) -> str:
    """Resume a session without silently passing ineffective override flags."""
    sid = str(args.get("session_id") or "").strip()
    if not sid:
        return "错误：session_id 不能为空（用 dimagent_sessions 查 id）。"
    if not session_exists(sid):
        return f"错误：session {sid} 不存在。"
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return "错误：prompt 不能为空。"
    prepared = {
        "prompt": prompt,
        "session_id": sid,
        "resume": True,
        "cwd": args.get("cwd"),
        "wait_seconds": args.get("wait_seconds") or 0,
        "open_viewer": args.get("open_viewer", True),
        "images": args.get("images") or [],
    }
    for key in ("mode", "reasoning_effort", "verify", "repair_attempts"):
        if args.get(key) is not None:
            prepared[key] = args[key]
    return tool_run(prepared)


def tool_feedback(args: dict) -> str:
    """收集使用反馈到应用数据目录，默认裁剪请求体。"""
    component = str(args.get("component") or "dimagent-subagent").strip()[:80]
    title = str(args.get("title") or "").strip()[:160]
    if not title:
        return "错误：title 不能为空。"
    severity = str(args.get("severity") or "medium").strip().lower()
    if severity not in ("low", "medium", "high"):
        severity = "medium"
    fields = []
    for key, label in (
        ("scenario", "场景"), ("expected", "预期"), ("actual", "实际"),
        ("evidence", "复现/证据"), ("impact", "影响"), ("improvement", "改进方向"),
    ):
        value = str(args.get(key) or "").strip()
        if value:
            fields.append(f"- {label}：{value[:4000]}")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry = (
        f"\n## {stamp} · {title}\n\n"
        f"- 组件/版本：{component} / MCP {VERSION}\n"
        f"- 严重度：{severity}\n"
        + ("\n".join(fields) + "\n" if fields else "")
        + "- 状态：待确认\n"
    )
    path = os.path.join(job_feedback_dir(), "feedback.md")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(entry)
    except OSError as exc:
        return f"写入反馈失败：{exc}"
    return f"反馈已写入：{path}"


def write_startup_pid() -> None:
    path = os.path.join(STATE_DIR, "mcp", f"{os.getpid()}.pid")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="ascii") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass


def remove_startup_pid() -> None:
    path = os.path.join(STATE_DIR, "mcp", f"{os.getpid()}.pid")
    try:
        os.remove(path)
    except OSError:
        pass


def panel_html() -> str:
    """面板 HTML = 实时会话查看器本体；每次读盘，改完不用重启 MCP 进程。"""
    last_exc = None
    for path in (VIEWER_FILE, PANEL_FILE):
        try:
            with open(path, encoding="utf-8") as fh:
                html = fh.read()
                return html.replace(
                    "globalThis.NIPAH_LIVE_SESSIONS_TOOL || 'nipah_live_sessions'",
                    "'dimagent_live_sessions'",
                ).replace(
                    "globalThis.NIPAH_LIVE_MESSAGES_TOOL || 'nipah_live_messages'",
                    "'dimagent_live_messages'",
                )
        except OSError as exc:
            last_exc = exc
    return ("<!doctype html><meta charset='utf-8'>"
            "<body style='font:14px system-ui;padding:16px;color:#c00'>"
            f"面板页面读取失败：{last_exc}</body>")


def tool_panel(args: dict) -> str:
    """给面板/调用方一份快照。宿主可能把这段结果注入给 widget 直接用。"""
    limit = int(args.get("limit") or 12)
    try:
        sessions = list_sessions(limit)
    except RuntimeError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    # 打开面板时顺带确保监控服务在跑（已在跑则直接通过，不会重复启动）
    viewer = ensure_viewer()
    snapshot = {
        "ok": True,
        "version": VERSION,
        "pid": os.getpid(),
        "toolCount": len(TOOLS),
        "viewer": viewer,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sessions": [
            {
                "id": s["id"], "short": s["short"], "title": s["title"] or s["subagentPrompt"] or "",
                "isSubagent": s["isSubagent"], "parentSessionId": s["parentSessionId"],
                "model": s["model"], "messages": s["messages"], "lastAt": s["lastAt"],
            }
            for s in sessions
        ],
    }
    return json.dumps(snapshot, ensure_ascii=False)


# ---- 面板数据通道（沙箱宿主桥 window.openai.callTool 使用）----
# 沙箱 CSP 只放行 https/wss 域，http://127.0.0.1 会被宿主剔除，
# 所以面板在沙箱里不能直连本地监控服务，改走这两个工具。
# 数据源与 dim-live-viewer/server.py 完全同一份（直接复用它的函数），零分叉。
_VIEWER_MOD = None
_VIEWER_MOD_TRIED = False


def viewer_module():
    global _VIEWER_MOD, _VIEWER_MOD_TRIED
    if _VIEWER_MOD_TRIED:
        return _VIEWER_MOD
    _VIEWER_MOD_TRIED = True
    script = os.path.join(os.path.dirname(VIEWER_FILE), "server.py")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("dim_live_viewer_server", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _VIEWER_MOD = mod
    except Exception as exc:  # noqa: BLE001 - 面板通道降级，不影响其它工具
        log(f"viewer module load failed: {exc!r}")
        _VIEWER_MOD = None
    return _VIEWER_MOD


def tool_live_sessions(args: dict) -> dict:
    """面板轮询用：会话列表（与监控页 /api/sessions 同构）。"""
    mod = viewer_module()
    if mod is None:
        return {"ok": False, "error": "实时查看器 server.py 不可用"}
    try:
        return {"ok": True, "now": None,
                "sessions": mod.fetch_sessions(db_path(), runs_dir())}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def tool_live_messages(args: dict) -> dict:
    """面板轮询用：某会话从 offset 起的消息增量。"""
    mod = viewer_module()
    if mod is None:
        return {"ok": False, "error": "实时查看器 server.py 不可用"}
    sid = str(args.get("session") or "").strip()
    if not sid:
        return {"ok": False, "error": "missing session"}
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        return {"ok": True, **mod.fetch_messages(db_path(), sid, offset)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


TOOLS = [
    {
        "name": "dimagent_run",
        "description": (
            "把任务派发给 DimAgent（另一个本地 AI Agent CLI）当子代理跑，后台执行、不阻塞当前对话。"
            "返回 jobId / sessionId 和实时监控页地址。之后用 dimagent_status 看进度、"
            "dimagent_wait 等结束、dimagent_result 取最终答案。适合并行开工、换个模型审一遍、"
            "长任务后台跑等场景。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "任务内容（必填）。写清楚目标、约束、验收标准。"},
                "cwd": {"type": "string", "description": "工作目录；默认跟随当前 Codex 项目根目录，无法判断时回退到独立 workspace。"},
                "provider": {"type": "string", "description": f"provider id 或显示名（如 magpie），默认 {CFG.get('default_provider') or 'opencode-go'}；会解析为启用 provider 后显式传给 dim --provider。"},
                "model": {"type": "string", "description": "模型 id，默认 deepseek-v4.1-flash；可写带 provider 前缀的 id，会归一化为裸模型名。"},
                "reasoning_effort": {"type": "string",
                                     "enum": ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
                                     "description": "推理档位，默认 max（deepseek-v4.1-flash 支持 high|max）。"},
                "mode": {"type": "string", "enum": ["agent", "plan", "goal"], "description": "运行模式，默认 agent。"},
                "policy": {"type": "string", "enum": ["read-only", "workspace-write", "full-access"],
                           "description": "权限预设，默认 workspace-write。只读审查用 read-only。"},
                "verify": {"type": "string", "description": "完成后的验收命令（可选，可写多条用 ; 分隔）。"},
                "repair_attempts": {"type": "integer", "description": "验收失败后的自动修复轮数。"},
                "session_id": {"type": "string", "description": "载入已有 session 继续跑；会沿用旧模型与权限，model/provider/policy 覆盖会被拒绝。"},
                "resume_last": {"type": "boolean", "description": "恢复最近一次会话继续跑。"},
                "resume": {"type": "boolean", "description": "配合 session_id 使用 exec resume；沿用旧模型与权限，传覆盖参数会被拒绝。"},
                "allowed_tools": {"type": "string", "description": "额外放行的工具，逗号分隔。"},
                "disallowed_tools": {"type": "string", "description": "禁用的工具，逗号分隔（优先级最高）。"},
                "tools": {"type": "string", "description": "只暴露这些工具，逗号分隔。"},
                "append_system_prompt": {"type": "string", "description": "追加到 system prompt 的文本。"},
                "images": {"type": "array", "items": {"type": "string"}, "description": "附带图片路径。"},
                "wait_seconds": {"type": "number", "description": f"最多在本次调用里等多少秒（0=立即返回，默认 0，上限 {CFG.get('max_wait_seconds')}）。"},
                "allow_web": {"type": "boolean", "description": "显式允许 WebSearch/WebFetch；默认自动禁用，避免消耗搜索配额。"},
                "open_viewer": {"type": "boolean", "description": "同时确保实时监控页在跑，默认 true。"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "dimagent_status",
        "description": "查看 DimAgent 子代理任务的实时状态（跑没跑完、最后事件、工具调用数、当前输出）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "dimagent_run 返回的 jobId。"},
                "session_id": {"type": "string", "description": "也可以按 sessionId 查。"},
                "tail_chars": {"type": "integer", "description": "展示末尾多少字，默认 1200。"},
            },
        },
    },
    {
        "name": "dimagent_wait",
        "description": "阻塞等待 DimAgent 子代理任务结束（或超时），结束后直接返回最终答案。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "session_id": {"type": "string"},
                "timeout_seconds": {"type": "number", "description": f"最长等待秒数，默认 120，单次上限 {CFG.get('max_wait_seconds')}s（宿主约 300s 截断）。"},
                "poll_seconds": {"type": "number", "description": "轮询间隔，默认 2.5。"},
                "max_chars": {"type": "integer", "description": "返回文本上限，默认 8000。"},
            },
        },
    },
    {
        "name": "dimagent_stop",
        "description": "停止一个正在运行的 DimAgent 子代理任务；默认优雅停止，force=true 强制结束进程树。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "要停止的 jobId；默认最近任务。"},
                "session_id": {"type": "string", "description": "也可以按 sessionId 找对应任务。"},
                "force": {"type": "boolean", "description": "true 强制杀进程树；默认 false，先发送优雅终止。"},
            },
        },
    },
    {
        "name": "dimagent_resume",
        "description": "继续一个已有 DimAgent 会话。沿用旧会话模型/权限；可覆盖本轮 mode、推理档位和验收配置。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "要续跑的会话 id（必填）。"},
                "prompt": {"type": "string", "description": "续跑指令（必填）。"},
                "cwd": {"type": "string", "description": "可选工作目录；通常用原会话目录。"},
                "reasoning_effort": {
                    "type": "string",
                    "enum": ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
                    "description": "本轮推理档位（可选），仅模型支持时生效。",
                },
                "mode": {
                    "type": "string",
                    "enum": ["agent", "plan", "goal"],
                    "description": "本轮运行模式，默认沿用 CLI 的 agent。",
                },
                "verify": {"type": "string", "description": "完成后的验收命令（可选）。"},
                "repair_attempts": {"type": "integer", "description": "验收失败后的自动修复轮数。"},
                "wait_seconds": {"type": "number", "description": f"派发后最多等多少秒，默认 0，上限 {CFG.get('max_wait_seconds')}。"},
                "images": {"type": "array", "items": {"type": "string"}, "description": "附带图片路径。"},
                "open_viewer": {"type": "boolean", "description": "确保监控页在跑，默认 true。"},
            },
            "required": ["session_id", "prompt"],
        },
    },
    {
        "name": "dimagent_result",
        "description": "取某个 DimAgent 会话的最终回答文本与用量（默认取最近一次任务）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "job_id": {"type": "string"},
                "max_chars": {"type": "integer", "description": "默认 20000。"},
            },
        },
    },
    {
        "name": "dimagent_sessions",
        "description": "列出 DimAgent 最近的会话（含主会话/子代理、模型、消息数、目录）。",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "默认 15，上限 100。"}},
        },
    },
    {
        "name": "dimagent_viewer",
        "description": "确保实时会话监控页在运行，并返回在 Codex 侧边栏打开的 URL。",
        "inputSchema": {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["ensure", "status"], "description": "默认 ensure。"}},
        },
    },
    {
        "name": "dimagent_doctor",
        "description": "自检：dim 路径解析结果、版本、数据库、监控页、默认参数。出问题时先跑这个。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "dimagent_cli",
        "description": (
            "直接调用 dim CLI 的其它子命令（session/provider/model/mcp/usage/plugin…）。"
            "派发任务请用 dimagent_run。注意：provider list --json 会输出全部模型元数据，很大，别用。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "args": {"description": "参数数组，如 [\"session\",\"list\",\"--all\"] 或字符串 \"usage --json\"。",
                         "anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]},
                "timeout_seconds": {"type": "number", "description": "默认 120。"},
                "max_chars": {"type": "integer", "description": "输出截断长度，默认 12000。"},
            },
            "required": ["args"],
        },
    },
    {
        "name": "dimagent_feedback",
        "description": "记录使用中遇到的缺陷或可用性问题，默认写入应用数据目录并裁剪长文本。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "一句话标题（必填）。"},
                "component": {"type": "string", "description": "组件/版本，默认 dimagent-subagent。"},
                "severity": {"type": "string", "enum": ["low", "medium", "high"], "description": "严重度，默认 medium。"},
                "scenario": {"type": "string"},
                "expected": {"type": "string"},
                "actual": {"type": "string"},
                "evidence": {"type": "string"},
                "impact": {"type": "string"},
                "improvement": {"type": "string"},
            },
            "required": ["title"],
        },
    },
    {
        # 这个工具同时是两个「App 入口」（Codex 读 _meta.ui.resourceUri + entrypoints）：
        #   global → 左侧边栏菜单里的固定入口（可固定，和 Figma 插件同位置）
        #   thread → 浏览器新标签页「更多工具 → 插件和 MCP」里的入口（点开为侧边面板标签页）
        # 两个入口打开的都是同一个版本化 ui://dimagent/monitor/<hash> 面板。
        "name": "dimagent_panel",
        "title": "DimAgent 会话监控",
        "description": (
            "打开 DimAgent 实时会话面板（Codex 侧边面板）——就是浏览器里那个实时会话查看器："
            "会话列表 + 消息流 / 思考链 / 工具调用实时刷新，打开时自动确保本地监控服务在跑。"
            "直接调用则返回一份当前快照 JSON。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "快照里带多少个会话，默认 12。"},
            },
        },
        "annotations": {"title": "DimAgent 会话监控", "readOnlyHint": True},
        "_meta": {
            "ui": {"resourceUri": PANEL_URI},
            "openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]},
        },
    },
    {
        # 面板沙箱数据通道（window.openai.callTool）；给模型留个可读描述也没坏处
        "name": "dimagent_live_sessions",
        "description": "（面板数据通道）返回实时会话列表 JSON，与监控页 /api/sessions 同构。",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "dimagent_live_messages",
        "description": "（面板数据通道）返回某会话从 offset 起的消息 JSON，与监控页 /api/messages 同构。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "会话 id"},
                "offset": {"type": "integer", "description": "已读消息数，默认 0"},
            },
            "required": ["session"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "dimagent_monitor_html",
        "description": "返回监控页 HTML。",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
]

HANDLERS = {
    "dimagent_live_sessions": tool_live_sessions,
    "dimagent_live_messages": tool_live_messages,
    "dimagent_panel": tool_panel,
    "dimagent_run": tool_run,
    "dimagent_status": tool_status,
    "dimagent_wait": tool_wait,
    "dimagent_stop": tool_stop,
    "dimagent_resume": tool_resume,
    "dimagent_result": tool_result,
    "dimagent_sessions": tool_sessions,
    "dimagent_viewer": tool_viewer,
    "dimagent_doctor": tool_doctor,
    "dimagent_cli": tool_cli,
    "dimagent_feedback": tool_feedback,
    "dimagent_monitor_html": lambda args: panel_html(),
}

# ---------------------------------------------------------------- MCP 协议

PROTOCOL_FALLBACK = "2025-06-18"


def send(obj: dict) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(data + b"\n")
    sys.stdout.buffer.flush()


def send_result(req_id, result) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def send_error(req_id, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def text_content(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


INSTRUCTIONS = (
    "DimAgent 子代理工具：把任务交给本地 DimAgent CLI 后台执行。"
    "典型流程：dimagent_run(prompt, cwd, model, reasoning_effort) → "
    "dimagent_wait 等结果 / dimagent_status 看进度 → dimagent_result 取正文。"
    "WebSearch/WebFetch 默认禁用，只有 allow_web=true 才开放。"
    "等待单次上限 240s，超时继续调即可；失败时 result 会带 status/error，"
    "卡死任务用 dimagent_stop（force=true 才强杀）。"
    "session_id / resume_last 会载入旧会话并沿用模型与权限，传 model/provider/policy "
    "等覆盖参数会被拒绝；resume 允许覆盖本轮 mode/effort/verify/repair。"
    "需要看思考过程与工具调用时：点 Codex 侧边面板的固定入口「DimAgent 会话监控」，"
    "或用 dimagent_viewer 拿到 URL 后在侧边栏浏览器打开（同一个实时页面）。"
    "⚠️ 派发被拒排查：若立刻被拒且 0 次工具调用（headless:run:rejected / model_missing），"
    "这是 provider 层故障，别连续重试或乱换模型——依次：dimagent_cli(\"model refresh dimcode-api-oauth\") → "
    "报 OAuth model resolver 时 dimagent_cli(\"auth status\") 与 (\"auth refresh\") → 再跑一行探针"
    "（只回复：收到）确认 completed 后，才成批重派真任务。"
)


def handle(msg: dict) -> None:
    method = msg.get("method") or ""
    req_id = msg.get("id")
    params = msg.get("params") or {}
    is_notification = req_id is None

    if method == "initialize":
        send_result(req_id, {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_FALLBACK,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
            "serverInfo": {"name": NAME, "version": VERSION},
            "instructions": INSTRUCTIONS,
        })
        return

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return

    if method == "ping":
        if not is_notification:
            send_result(req_id, {})
        return

    if method == "tools/list":
        send_result(req_id, {"tools": TOOLS})
        return

    if method == "resources/list":
        send_result(req_id, {"resources": [{
            "uri": PANEL_URI,
            "name": "DimAgent 会话监控",
            "description": "实时查看 DimAgent 子代理会话：消息流、思考链、工具调用（与 127.0.0.1:8790 同款页面）。",
            "mimeType": PANEL_MIME,
        }]})
        return

    if method == "resources/read":
        uri = params.get("uri") or ""
        if uri != PANEL_URI:
            send_error(req_id, -32602, f"Resource not found: {uri}")
            return
        # 说明：宿主对 connectDomains 只放行 https/wss，http://127.0.0.1 会被静默剔除，
        # 面板在沙箱里实际走宿主桥 window.openai.callTool（见 dimagent_live_* 工具）。
        # 这里仍保留声明：不伤人，将来宿主放开 localhost 时可直接生效。
        viewer_origin = "http://127.0.0.1:" + str(CFG.get("viewer_port") or 8790)
        send_result(req_id, {"contents": [{
            "uri": PANEL_URI,
            "mimeType": PANEL_MIME,
            "text": panel_html(),
            "_meta": {
                "openai/widgetCSP": {
                    "connectDomains": [viewer_origin],
                    "resourceDomains": [],
                },
                "openai/widgetHeightHint": 640,
            },
        }]})
        return

    if method == "prompts/list":
        send_result(req_id, {"prompts": []})
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
                # 字典结果同时给文本与 structuredContent，宿主桥（callTool）取后者
                send_result(req_id, {
                    "content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}],
                    "structuredContent": output,
                })
            else:
                send_result(req_id, text_content(output))
        except RuntimeError as exc:
            log(f"tool {name} RuntimeError: {exc}")
            send_result(req_id, text_content(f"{exc}", True))
        except Exception as exc:  # noqa: BLE001 - 工具错误按协议返回给调用方
            log(f"tool {name} failed: {exc!r}")
            send_result(req_id, text_content(f"工具执行失败：{type(exc).__name__}: {exc}", True))
        return

    if is_notification:
        return
    send_error(req_id, -32601, f"Method not found: {method}")


def main() -> int:
    _ensure_dirs()
    write_startup_pid()
    log(f"start pid={os.getpid()} python={sys.version.split()[0]} config={CFG.get('_config_file')}")
    stream = sys.stdin.buffer
    try:
        while True:
            try:
                raw = stream.readline()
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
                log(f"bad json line: {line[:200]}")
                continue
            try:
                handle(msg)
            except Exception as exc:  # noqa: BLE001 - 单个请求失败不能拖垮服务器
                log(f"handle failed: {exc!r}")
                if isinstance(msg, dict) and msg.get("id") is not None:
                    send_error(msg["id"], -32603, f"Internal error: {exc}")
    finally:
        remove_startup_pid()
    log("exit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
