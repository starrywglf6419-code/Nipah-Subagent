#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only CodeBuddy session viewer.

Reuses the existing DimAgent Subagent front-end and HTTP handler, but serves
CodeBuddy session JSONL files and its own job metadata.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
BASE_SERVER = os.path.join(PROJECT_ROOT, "dim-live-viewer", "server.py")
CB_SERVER = os.path.join(PROJECT_ROOT, "mcp", "codebuddy_mcp.py")
INDEX_FILE = os.path.join(PROJECT_ROOT, "dim-live-viewer", "index.html")


def load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = load_module("dim_live_viewer_base_for_codebuddy", BASE_SERVER)
cb = load_module("codebuddy_mcp_viewer", CB_SERVER)


def fetch_sessions(_db_path, _runs_dir=""):
    return cb._live_sessions().get("sessions", [])


def fetch_messages(_db_path, session, offset):
    return cb.fetch_messages(session, offset)


# Base Handler resolves these names in its module globals.
base.fetch_sessions = fetch_sessions
base.fetch_messages = fetch_messages


class Handler(base.Handler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            if not self._host_allowed():
                self._json({"ok": False, "error": "forbidden host/origin"}, 403)
                return
            try:
                with open(INDEX_FILE, encoding="utf-8") as fh:
                    html = fh.read()
            except OSError as exc:
                self._send(500, f"index.html not found: {exc}".encode(), "text/plain; charset=utf-8")
                return
            # The shared UI has the same layout and APIs; only brand strings
            # differ for the CodeBuddy source.
            html = html.replace("DimAgent · 实时会话", "CodeBuddy · 实时会话")
            html = html.replace('<span class="name">DimAgent</span>', '<span class="name">CodeBuddy</span>')
            html = html.replace("只读视图 · dimcode.sqlite", "只读视图 · CodeBuddy 会话")
            html = html.replace("正在连接 DimAgent 数据", "正在连接 CodeBuddy 数据")
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        return super().do_GET()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="CodeBuddy live session viewer")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8791)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"CodeBuddy live viewer  ->  http://{args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
