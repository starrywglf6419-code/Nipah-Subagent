#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DimAgent 实时会话查看器 · 本地只读小服务

读取 DimAgent 的 SQLite（默认 ~/.dimcode/v2/dimcode.sqlite，只读模式），
为前端页面提供会话列表与增量消息接口：

    GET /                                    前端页面
    GET /api/sessions                        会话列表（含子代理、父子关系、用量）
    GET /api/messages?session=<id>&offset=N  会话消息（增量）

用法:
    py -3 server.py                 # http://127.0.0.1:8790
    py -3 server.py --port 8811
    py -3 server.py --db D:\\path\\dimcode.sqlite
"""
from __future__ import annotations

import argparse
import ctypes
import glob
import json
import os
import sqlite3
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".dimcode", "v2", "dimcode.sqlite")
INDEX_FILE = os.path.join(HERE, "index.html")


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def parse_json(text, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _windows_process_start(pid: int) -> float:
    if os.name != "nt":
        return 0.0
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return 0.0
    try:
        created = ctypes.c_ulonglong()
        exited = ctypes.c_ulonglong()
        kernel = ctypes.c_ulonglong()
        user = ctypes.c_ulonglong()
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        ):
            return 0.0
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
    if os.name == "nt":
        created = _windows_process_start(pid)
        if created <= 0:
            return False
        if started_at_epoch and time.time() - float(started_at_epoch) > 120:
            try:
                import subprocess as _sp
                out = _sp.run(
                    ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=4,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                ).stdout or ""
                if "dim.exe" not in out.lower() and "dimcode" not in out.lower():
                    return False
            except (OSError, subprocess.SubprocessError):
                return False
        if started_at_epoch:
            start = float(started_at_epoch)
            # A recycled PID can be newer than the job; only accept a process
            # created in the job's startup window.
            if created < start - 10 or created > start + 120:
                return False
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


_session_cache = {"at": 0.0, "key": "", "data": None}
_pid_cache = {}


def _pid_alive_cached(pid, started_at_epoch=None) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    now = time.time()
    hit = _pid_cache.get(pid)
    # 5s is enough for UI liveness and avoids 62 tasklist spawns per poll.
    if hit and now - hit[0] < 5.0:
        return hit[1]
    alive = _pid_alive(pid, started_at_epoch)
    _pid_cache[pid] = (now, alive)
    return alive


def _job_outcome(log_path: str) -> dict:
    """Read the last run:ended event so the list can show a real terminal state."""
    out = {"status": "", "reason": "", "errorName": "", "errorMessage": "", "at": ""}
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 131072))
            tail = fh.read().decode("utf-8", "replace")
        for line in reversed(tail.splitlines()):
            if not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
            except (TypeError, ValueError):
                continue
            if evt.get("eventType") != "run:ended":
                continue
            payload = evt.get("payload") or {}
            err = payload.get("error") or {}
            if not isinstance(err, dict):
                err = {}
            out.update({
                "status": str(payload.get("status") or ""),
                "reason": str(payload.get("reason") or ""),
                "errorName": str(err.get("name") or ""),
                "errorMessage": " ".join(str(err.get("message") or "").split())[:1200],
                "at": str(evt.get("createdAt") or ""),
            })
            return out
    except OSError:
        pass
    return out


def _job_session_id(log_path: str) -> str:
    """Recover sessionId for older job metadata that was saved before it arrived."""
    try:
        with open(log_path, "rb") as fh:
            head = fh.read(65536).decode("utf-8", "replace")
        for line in head.splitlines():
            if not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
            except (TypeError, ValueError):
                continue
            sid = str(evt.get("sessionId") or "")
            if sid:
                return sid
    except OSError:
        pass
    return ""


def fetch_sessions(db_path: str, runs_dir: str = ""):
    db_mtime = os.path.getmtime(db_path) if os.path.isfile(db_path) else 0
    runs_mtime = 0.0
    if runs_dir and os.path.isdir(runs_dir):
        try:
            runs_mtime = max((os.path.getmtime(p) for p in glob.glob(os.path.join(runs_dir, "job_*.json"))), default=0)
        except OSError:
            runs_mtime = 0
    key = f"{db_path}|{db_mtime}|{runs_dir}|{runs_mtime}"
    now = time.time()
    if _session_cache["data"] is not None and _session_cache["key"] == key and now - _session_cache["at"] < 1.0:
        return _session_cache["data"]
    con = connect(db_path)
    try:
        counts = {
            r["sessionId"]: r["n"]
            for r in con.execute("SELECT sessionId, COUNT(*) AS n FROM messages GROUP BY sessionId")
        }
        usage = {}
        try:
            for r in con.execute(
                "SELECT sessionId, "
                "SUM(COALESCE(json_extract(usage,'$.totalTokens'),0)) AS tokens, "
                "SUM(COALESCE(cost,0)) AS cost "
                "FROM usage_ledger GROUP BY sessionId"
            ):
                usage[r["sessionId"]] = {"tokens": r["tokens"], "cost": r["cost"], "currency": "USD"}
        except sqlite3.Error:
            pass

        last = {}
        resolved = {}
        for r in con.execute(
            "SELECT sessionId, role, parts, createdAt FROM messages ORDER BY createdAt, orderKey"
        ):
            sid = r["sessionId"]
            parts = parse_json(r["parts"], []) or []
            for p in parts:
                if isinstance(p, dict) and p.get("type") == "tool_result" and p.get("tool_use_id"):
                    resolved.setdefault(sid, set()).add(p["tool_use_id"])
            last[sid] = (r, parts)

        job_states = {}
        if runs_dir and os.path.isdir(runs_dir):
            import glob as _glob
            for meta_path in _glob.glob(os.path.join(runs_dir, "job_*.json")):
                try:
                    with open(meta_path, encoding="utf-8") as fh:
                        job = json.load(fh)
                    log_path = job.get("logFile") or os.path.join(
                        runs_dir, (job.get("jobId") or "") + ".jsonl"
                    )
                    sid = job.get("sessionId") or _job_session_id(log_path)
                    if sid:
                        job["sessionId"] = sid
                    if sid:
                        terminal = _job_outcome(log_path)
                        ended_meta = bool(
                            job.get("endedAt") or job.get("finishedAt") or terminal.get("status")
                        )
                        prior = job_states.get(sid)
                        chosen = prior is None or (job.get("startedAtEpoch") or 0) > (prior.get("startedAtEpoch") or 0)
                        if chosen:
                            job_states[sid] = job
                            if terminal.get("status"):
                                job_states[sid]["__outcome"] = terminal
                            if job.get("stopRequested"):
                                job_states[sid]["__stopped"] = True
                        elif prior is not None and terminal.get("status"):
                            # An older job must not overwrite the newest job's outcome.
                            pass
                        if chosen and ended_meta:
                            job_states[sid]["__ended"] = True
                except (OSError, ValueError):
                    continue

        sessions = []
        rows = con.execute(
            """SELECT s.sessionId, s.cwd, s.title, s.status, s.heldBy, s.createdAt, s.updatedAt,
                      st.selectedProviderId, st.selectedModelId, st.currentMode, st.settings
               FROM sessions s
               LEFT JOIN session_states st ON st.sessionId = s.sessionId"""
        ).fetchall()
        for r in rows:
            settings = parse_json(r["settings"], {}) or {}
            meta = settings.get("metadata") or {}
            si = meta.get("subagentInput") or {}
            item = {
                "id": r["sessionId"],
                "cwd": r["cwd"],
                "title": r["title"],
                "status": r["status"],
                "heldBy": r["heldBy"],
                "createdAt": r["createdAt"],
                "updatedAt": r["updatedAt"],
                "providerId": r["selectedProviderId"],
                "modelId": r["selectedModelId"],
                "mode": r["currentMode"],
                "messageCount": counts.get(r["sessionId"], 0),
                "usage": usage.get(r["sessionId"]),
                "isSubagent": bool(meta.get("__subagentOrigin")) or r["sessionId"].startswith("subagent_"),
                "subagentType": meta.get("subagentType"),
                "parentSessionId": meta.get("parentSessionId"),
                "parentToolCallId": meta.get("parentToolCallId"),
                "subagentPrompt": si.get("prompt") if isinstance(si, dict) else None,
            }
            job = job_states.get(r["sessionId"])
            if job:
                outcome = job.get("__outcome") or {}
                alive = (not job.get("__ended")) and _pid_alive_cached(job.get("pid"), job.get("startedAtEpoch"))
                if job.get("__stopped") and not alive:
                    alive = False
                item["jobId"] = job.get("jobId")
                item["jobPid"] = job.get("pid")
                item["jobRunning"] = alive
                item["jobReason"] = outcome.get("reason") or ""
                item["jobError"] = outcome.get("errorName") or ""
                if alive:
                    item["jobStatus"] = "running"
                elif job.get("__stopped"):
                    item["jobStatus"] = "cancelled"
                elif outcome.get("status"):
                    item["jobStatus"] = outcome["status"]
                elif job.get("endedAt") or job.get("finishedAt") or job.get("__ended"):
                    item["jobStatus"] = "completed"
                else:
                    log_path = job.get("logFile") or os.path.join(
                        runs_dir, (job.get("jobId") or "") + ".jsonl"
                    )
                    process_log = job.get("processLog") or os.path.join(
                        runs_dir, (job.get("jobId") or "") + ".process.log"
                    )
                    stamps = []
                    for path in (log_path, process_log):
                        try:
                            stamps.append(os.path.getmtime(path))
                        except OSError:
                            pass
                    last = max(stamps or [job.get("startedAtEpoch") or 0])
                    idle = int(max(0, time.time() - last))
                    item["jobIdleSeconds"] = idle
                    item["jobStatus"] = "stale" if idle > 90 else "starting"
            else:
                item["jobRunning"] = False
                item["jobStatus"] = "stored"
            lm = last.get(r["sessionId"])
            if lm is not None:
                lm_row, parts = lm
                open_tools = [
                    p for p in parts
                    if isinstance(p, dict) and p.get("type") == "tool_use"
                    and p.get("id") and p.get("id") not in resolved.get(r["sessionId"], set())
                ]
                item["pendingTool"] = bool(open_tools)
                item["pendingToolName"] = open_tools[0].get("name") if open_tools else None
                preview = ""
                for p in parts:
                    if not isinstance(p, dict):
                        continue
                    t = p.get("type")
                    if t == "text":
                        preview = (p.get("text") or "").strip()
                    elif t == "thinking":
                        preview = preview or "（思考）"
                    elif t == "tool_use":
                        tool_input = p.get("input") or {}
                        s = (
                            tool_input.get("command")
                            or tool_input.get("path")
                            or tool_input.get("prompt")
                            or ""
                        )
                        preview = f"[{p.get('name')}] {s}".strip()
                    elif t == "tool_result":
                        preview = "→ " + (p.get("content") or "")[:140]
                    if preview:
                        break
                item["lastPreview"] = " ".join(str(preview).split())[:200]
                item["lastRole"] = lm_row["role"]
                item["lastAt"] = lm_row["createdAt"]
            sessions.append(item)

        sessions.sort(key=lambda s: (s.get("lastAt") or s["updatedAt"] or ""), reverse=True)
        _session_cache.update(at=now, key=key, data=sessions)
        return sessions
    finally:
        con.close()


def fetch_messages(db_path: str, sid: str, offset: int):
    con = connect(db_path)
    try:
        rows = con.execute(
            "SELECT messageId, role, parts, attachments, toolMetadata, metadata, "
            "orderKey, createdAt, updatedAt "
            "FROM messages WHERE sessionId = ? ORDER BY createdAt, orderKey",
            (sid,),
        ).fetchall()
        out = []
        for r in rows[offset:]:
            out.append(
                {
                    "messageId": r["messageId"],
                    "role": r["role"],
                    "parts": parse_json(r["parts"], []) or [],
                    "attachments": parse_json(r["attachments"]),
                    "toolMetadata": parse_json(r["toolMetadata"]),
                    "metadata": parse_json(r["metadata"]),
                    "createdAt": r["createdAt"],
                    "updatedAt": r["updatedAt"],
                }
            )
        return {"total": len(rows), "messages": out}
    finally:
        con.close()


class Handler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB
    runs_dir = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local"),
        "DimAgentSubagent", "runs",
    )
    server_version = "DimLiveViewer/1.1"

    def _host_allowed(self) -> bool:
        """Only accept loopback Host/Origin, defeating DNS rebinding."""
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        if host not in ("127.0.0.1", "localhost", "::1", "[::1]"):
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            try:
                parsed = urlparse(origin)
                if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
                    return False
            except ValueError:
                return False
        return True

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        origin = (self.headers.get("Origin") or "").strip()
        if origin and self._host_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        if not self._host_allowed():
            self._json({"ok": False, "error": "forbidden host/origin"}, 403)
            return
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                if not os.path.exists(INDEX_FILE):
                    self._send(500, b"index.html not found", "text/plain; charset=utf-8")
                    return
                with open(INDEX_FILE, "rb") as f:
                    body = f.read()
                self._send(200, body, "text/html; charset=utf-8")
                return
            if path == "/api/sessions":
                sessions = fetch_sessions(self.db_path, self.runs_dir)
                self._json({"ok": True, "now": None, "sessions": sessions})
                return
            if path == "/api/messages":
                qs = parse_qs(parsed.query)
                sid = (qs.get("session") or [""])[0]
                try:
                    offset = max(0, int((qs.get("offset") or ["0"])[0]))
                except ValueError:
                    offset = 0
                if not sid:
                    self._json({"ok": False, "error": "missing session"}, 400)
                    return
                self._json({"ok": True, **fetch_messages(self.db_path, sid, offset)})
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:  # noqa: BLE001 - keep the server alive
            self._json({"ok": False, "error": str(exc)}, 500)

    def log_message(self, fmt, *args):  # quiet console
        pass


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="DimAgent live session viewer (read-only)")
    ap.add_argument("--db", default=DEFAULT_DB, help="path to dimcode.sqlite")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[!] sqlite not found: {args.db}")
        return 1

    srv = None
    port = args.port
    for i in range(12):
        try:
            srv = ThreadingHTTPServer((args.host, port + i), Handler)
            port = port + i
            break
        except OSError:
            continue
    if srv is None:
        print("[!] no free port found")
        return 1
    Handler.db_path = args.db
    print(f"DimAgent live viewer  ->  http://{args.host}:{port}")
    print(f"db: {args.db} (read-only)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
