#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Nipah Subagent unified read-only viewer.

One local service exposes both provider session stores through the shared UI:

    GET /                                     unified front-end
    GET /api/sessions                         DimAgent + CodeBuddy sessions
    GET /api/messages?session=<id>&source=<engine>&offset=N

Only loopback clients are accepted. Source adapters stay in mcp/ and this
server owns no provider-specific parsing.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
BASE_SERVER = os.path.join(PROJECT_ROOT, "dim-live-viewer", "server.py")
FACADE = os.path.join(PROJECT_ROOT, "mcp", "nipah_subagent_mcp.py")
INDEX_FILE = os.path.join(PROJECT_ROOT, "dim-live-viewer", "index.html")


def load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = load_module("nipah_live_viewer_base", BASE_SERVER)
nipah = load_module("nipah_live_viewer_facade", FACADE)


def fetch_sessions(_db_path, _runs_dir=""):
    return nipah.combined_sessions(2000)


def fetch_messages(_db_path, session, offset, source=""):
    return nipah.combined_messages(source, session, offset)


class Handler(base.Handler):
    def do_GET(self):
        if not self._host_allowed():
            self._json({"ok": False, "error": "forbidden host/origin"}, 403)
            return
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                with open(INDEX_FILE, encoding="utf-8") as fh:
                    html = fh.read()
                html = html.replace("DimAgent · 实时会话", "Nipah Subagent · 实时会话")
                html = html.replace(
                    '<span class="name">DimAgent</span>',
                    '<span class="name">Nipah</span><small>Subagent</small>',
                )
                html = html.replace("只读视图 · dimcode.sqlite", "只读视图 · Nipah 会话")
                html = html.replace("正在连接 DimAgent 数据", "正在连接 Nipah 数据")
                html = html.replace(
                    "globalThis.NIPAH_LIVE_SESSIONS_TOOL || 'nipah_live_sessions'",
                    "'nipah_live_sessions'",
                )
                html = html.replace(
                    "globalThis.NIPAH_LIVE_MESSAGES_TOOL || 'nipah_live_messages'",
                    "'nipah_live_messages'",
                )
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/sessions":
                self._json({"ok": True, "now": None, "sessions": fetch_sessions("", "")})
                return
            if path == "/api/messages":
                qs = parse_qs(parsed.query)
                sid = (qs.get("session") or [""])[0]
                source = (qs.get("source") or [""])[0]
                try:
                    offset = max(0, int((qs.get("offset") or ["0"])[0]))
                except ValueError:
                    offset = 0
                if not sid:
                    self._json({"ok": False, "error": "missing session"}, 400)
                    return
                self._json({"ok": True, **fetch_messages("", sid, offset, source)})
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:  # noqa: BLE001 - keep the viewer alive
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def log_message(self, fmt, *args):
        pass


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Nipah Subagent unified live viewer")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8792)
    args = ap.parse_args()
    srv = None
    port = args.port
    for offset in range(12):
        try:
            port = args.port + offset
            srv = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        print("[!] no free port found")
        return 1
    print(f"Nipah Subagent viewer  ->  http://{args.host}:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
