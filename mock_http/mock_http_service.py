#!/usr/bin/env python3
"""机械臂 HTTP 模拟：收到任务即视为动作已完成。

运行控制打开 Mock 时，由 flow_api_server 和 rosbridge 一起拉起。
默认监听 http://127.0.0.1:18080/ ，对任意路径返回 HTTP 200 与 {"success": true}。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18080


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        self._reply()

    def do_POST(self):  # noqa: N802
        self._reply()

    def _reply(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        task = ""
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("task"):
                task = str(body["task"])
        print(f"{self.command} {self.path} task={task or '-'}", flush=True)
        payload = json.dumps({"success": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):  # noqa: A003
        return


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"mock_http_service http://{host}:{port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="模拟机械臂 HTTP：任意请求返回动作已完成")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
