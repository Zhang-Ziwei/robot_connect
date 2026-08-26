"""
callback_server.py — 回调接收服务器（含确认流程模拟）

模拟 robot_connect CallbackSender 的完整交互：
  1. 接收 POST 回调 JSON
  2. 模拟业务处理（可选延迟）
  3. 返回 HTTP 200 + { "received": true } 表示已收到
  4. 若未确认，CallbackSender 将按 retry_interval 重发

用法：

    # 正常模式（监听 8099，与 robot_config.json callback.url 匹配）
    python3 tools/callback_server.py --port 8099

    # 同时记录日志
    python3 tools/callback_server.py --port 8099 --log callbacks.jsonl

    # 模拟：处理耗时 2 秒后再确认（测试 timeout 配置）
    python3 tools/callback_server.py --port 8099 --delay 2

    # 模拟：前 2 次不确认，第 3 次才返回 received:true（测试重发）
    python3 tools/callback_server.py --port 8099 --fail-count 2

    # 模拟：前 2 次直接 HTTP 500（测试连接/重发）
    python3 tools/callback_server.py --port 8099 --fail-count 2 --fail-http

配置 robot_connect（端口与调度 8090 无关）：
    robot_config.json → "callback": {
        "enabled": true,
        "url": "http://127.0.0.1:8099/callback",
        "timeout": 10,
        "retry": 3,
        "retry_interval": 10
    }
"""

import argparse
import errno
import json
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional


# ── 全局配置（由 main 填充）────────────────────────────────────────────────
_log_path: Optional[Path] = None
_count = 0
_delay: float = 0.0
_fail_remaining: int = 0
_fail_http: bool = False


def _log_incoming(body: dict, client: str) -> None:
    global _count
    _count += 1
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    status = "任务成功" if body.get("success") else "任务失败"

    sep = "=" * 70
    print(f"\n{sep}")
    print(f"[{_count:04d}] {ts}  来自 {client}")
    print(f"  cmd_type : {body.get('cmd_type', '?')}")
    print(f"  cmd_id   : {body.get('cmd_id', '?')}")
    print(f"  success  : {status}")
    print("  请求体:")
    print(json.dumps(body, ensure_ascii=False, indent=4))
    print(sep)
    sys.stdout.flush()

    if _log_path:
        record = {"_ts": ts, "_client": client, **body}
        with _log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    resp = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(resp)))
    handler.end_headers()
    handler.wfile.write(resp)


class CallbackHandler(BaseHTTPRequestHandler):
    """处理 POST 回调（任意路径）。"""

    def do_POST(self):
        global _fail_remaining
        client = f"{self.client_address[0]}:{self.client_address[1]}"

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                body = {"_raw": raw.decode("utf-8", errors="replace")}

            _log_incoming(body, client)

            # ── 模拟业务处理 ──────────────────────────────────────────────
            print("  → [1/3] 收到回调")
            if _delay > 0:
                print(f"  → [2/3] 模拟处理中，等待 {_delay}s ...")
                time.sleep(_delay)
            else:
                print("  → [2/3] 模拟处理中 ...")

            if _fail_remaining > 0:
                _fail_remaining -= 1
                if _fail_http:
                    print(f"  → [3/3] 模拟 HTTP 500（剩余失败次数 {_fail_remaining}）")
                    print("       CallbackSender 将视为未确认并重发\n")
                    _send_json(self, 500, {
                        "received": False,
                        "message": "模拟：服务器内部错误",
                    })
                    return

                print(f"  → [3/3] 模拟未确认 received:false（剩余失败次数 {_fail_remaining}）")
                print("       CallbackSender 将按 retry_interval 重发\n")
                _send_json(self, 200, {
                    "received": False,
                    "message": "模拟：尚未确认收到，请重发",
                })
                return

            print("  → [3/3] 确认收到，返回 received:true")
            print("       CallbackSender 停止重发\n")
            _send_json(self, 200, {"received": True})

        except Exception as exc:
            print(f"[错误] 处理请求失败: {exc}", file=sys.stderr)
            self.send_error(500, str(exc))

    def do_GET(self):
        """健康检查。"""
        _send_json(self, 200, {
            "status": "ok",
            "received_count": _count,
            "simulation": {
                "delay": _delay,
                "fail_remaining": _fail_remaining,
                "fail_http": _fail_http,
            },
        })

    def log_message(self, fmt, *args):
        pass


def main():
    global _log_path, _delay, _fail_remaining, _fail_http

    parser = argparse.ArgumentParser(
        description="robot_connect 回调接收服务器（模拟 received 确认流程）",
    )
    parser.add_argument("--port", type=int, default=8099, help="监听端口（默认 8099）")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="绑定地址（默认 0.0.0.0）")
    parser.add_argument("--log", type=str, default="", help="将回调追加写入此 JSONL 文件")
    parser.add_argument(
        "--delay", type=float, default=0.0,
        help="模拟处理延迟（秒），延迟后返回 received:true",
    )
    parser.add_argument(
        "--fail-count", type=int, default=0,
        help="前 N 次请求不确认（测试 CallbackSender 重发）",
    )
    parser.add_argument(
        "--fail-http", action="store_true",
        help="与 --fail-count 配合：失败时返回 HTTP 500 而非 received:false",
    )
    args = parser.parse_args()

    _delay = max(0.0, args.delay)
    _fail_remaining = max(0, args.fail_count)
    _fail_http = args.fail_http

    if args.log:
        _log_path = Path(args.log)
        print(f"日志文件: {_log_path.resolve()}")

    HTTPServer.allow_reuse_address = True
    try:
        server = HTTPServer((args.host, args.port), CallbackHandler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(
                f"错误: 端口 {args.port} 已被占用。\n"
                f"  换端口: python3 tools/callback_server.py --port 8091\n"
                f"  查占用: lsof -i :{args.port}",
                file=sys.stderr,
            )
            sys.exit(1)
        raise

    print(f"回调服务器: http://{args.host}:{args.port}/")
    if _delay:
        print(f"模拟延迟: {_delay}s")
    if _fail_remaining:
        mode = "HTTP 500" if _fail_http else "received:false"
        print(f"模拟失败: 前 {_fail_remaining} 次返回 {mode}")
    print("等待 CallbackSender 回调...（Ctrl-C 停止）\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n已停止。共收到 {_count} 条回调")


if __name__ == "__main__":
    main()
