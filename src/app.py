#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — 抓取管理 Web UI 服务端

基于标准库 http.server，零额外依赖。提供：
  GET  /                      仪表盘页面 (web/index.html)
  GET  /api/status            运行态：总数/目标/cursor/页数/各类别进度/状态
  POST /api/start  {target,per_cat,recency_days,max_pages,dry_run}  开始/续传抓取
  POST /api/pause                                     暂停（保存 cursor，可续传）
  POST /api/reset                                     清空 DB 与 papers.jsonl
  GET  /api/papers?category=&limit=&offset=          论文列表（可按类别筛选）

启动：python src/app.py [port]   # 默认 8080
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_fetch import FetchEngine, DEFAULT_DB, ROOT  # noqa: E402

INDEX = ROOT / "web" / "index.html"


class Handler(BaseHTTPRequestHandler):
    server_version = "BiomniFetchUI/1.0"

    # ---- helpers ----
    def _send(self, code: int, obj=None, body: bytes | None = None,
              ctype: str = "application/json; charset=utf-8"):
        if obj is not None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body) if body else 0))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            return {}

    def log_message(self, *args):  # 静默默认访问日志
        pass

    # ---- GET ----
    def do_GET(self):
        p = urlparse(self.path)
        eng: FetchEngine = self.server.eng

        if p.path in ("/", "/index.html"):
            if INDEX.exists():
                self._send(200, body=INDEX.read_bytes(), ctype="text/html; charset=utf-8")
            else:
                self._send(404, body=b"web/index.html not found")
            return

        if p.path == "/api/status":
            self._send(200, eng.status())
            return

        if p.path == "/api/papers":
            q = parse_qs(p.query)
            category = q.get("category", [None])[0]
            limit = int(q.get("limit", [50])[0])
            offset = int(q.get("offset", [0])[0])
            self._send(200, eng.list_papers(category, limit, offset))
            return

        self._send(404, {"error": "not found"})

    # ---- POST ----
    def do_POST(self):
        p = urlparse(self.path)
        eng: FetchEngine = self.server.eng
        data = self._json_body()

        if p.path == "/api/start":
            if eng.is_running():
                self._send(409, {"error": "已在运行中", "status": eng.status()})
                return
            ok = eng.start(
                target=int(data.get("target", 2500)),
                per_cat=int(data.get("per_cat", 100)),
                recency_days=int(data.get("recency_days", 540)),
                max_pages=int(data.get("max_pages", 0)),
                dry_run=bool(data.get("dry_run", True)),
            )
            self._send(200, eng.status())
            return

        if p.path == "/api/pause":
            eng.pause()
            self._send(200, eng.status())
            return

        if p.path == "/api/reset":
            eng.reset()
            self._send(200, eng.status())
            return

        self._send(404, {"error": "not found"})


def run(eng: FetchEngine, port: int = 8080) -> None:
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.eng = eng
    print(f"[app] 抓取管理 UI 已启动: http://localhost:{port}", flush=True)
    print(f"[app] DB: {eng.db_path}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[app] 已停止", flush=True)


if __name__ == "__main__":
    eng = FetchEngine(DEFAULT_DB)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    run(eng, port)
