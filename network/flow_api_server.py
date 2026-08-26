"""
network/flow_api_server.py

图形化流程编辑器的后端 API 服务（独立端口，与命令端口 network/http_server.py 完全分开，
互不影响；实现风格与其保持一致，同样基于标准库 http.server，不引入额外依赖）。

职责：
    1. 托管 flow_editor/ 下的静态页面（拖拽画布 GUI，零构建依赖，见该目录 README）
    2. 提供流程 JSON 的加载 / 保存 / 校验 / 版本回滚 接口
    3. 提供"演练"（dry-run）接口：用连接 mock_rosbridge_server 的临时机器人跑一遍流程，
       返回逐节点执行轨迹，供前端在画布上高亮回放

多项目支持方式：每个支持"图形化编排"的项目提供一个 adapter 模块（当前只有
``programs/WRC_FLOW/dryrun_adapter.py``），在下面的 ``PROJECT_ADAPTERS`` 里注册一行即可，
本文件不需要认识任何项目的具体业务。

启动方式：
    python3 -m network.flow_api_server
或在 main.py 里跟命令 HTTP 服务器一起启动（可选）。
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from infrastructure.error_logger import get_error_logger
from infrastructure.config_loader import get_flow_api_server_config
from core.flow_engine import (
    FlowEngine, FlowResult, NodeRecord, SignalBus,
    validate_flow_graph, BUILTIN_NODE_TYPE_SCHEMAS,
)

logger = get_error_logger()
_LOG = "FlowAPIServer"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EDITOR_STATIC_DIR = os.path.join(_REPO_ROOT, "flow_editor")

# Docker 外部挂载的流程配置目录优先级高于项目内置目录，与 core/FLOW_ENGINE_GUIDE.md 第 7 节一致
_EXTERNAL_FLOWS_DIR = "/config/flows"


# ──────────────────────────────────────────────────────────────────────────────
# 项目适配器注册表——新增一个支持图形化编排的项目时，在这里加一行即可
# ──────────────────────────────────────────────────────────────────────────────

def _load_wrc_flow_adapter():
    from programs.WRC_FLOW import dryrun_adapter, node_handlers
    return {
        "local_flows_dir": os.path.join(_REPO_ROOT, "programs", "WRC_FLOW", "flows"),
        "node_type_schemas": {**BUILTIN_NODE_TYPE_SCHEMAS, **node_handlers.NODE_TYPE_SCHEMAS},
        "known_handler_types": list(node_handlers.NODE_TYPE_SCHEMAS.keys()),
        "dryrun_adapter": dryrun_adapter,
    }


PROJECT_ADAPTERS = {
    "WRC_FLOW": _load_wrc_flow_adapter,
}

# 缓存已加载的 adapter（避免每个请求都重新 import）
_adapter_cache: Dict[str, Dict[str, Any]] = {}


def _get_adapter(project: str) -> Optional[Dict[str, Any]]:
    if project not in PROJECT_ADAPTERS:
        return None
    if project not in _adapter_cache:
        _adapter_cache[project] = PROJECT_ADAPTERS[project]()
    return _adapter_cache[project]


# ──────────────────────────────────────────────────────────────────────────────
# 流程文件读写（沿用现有配置分层习惯：docker 外部优先，项目内置兜底）
# ──────────────────────────────────────────────────────────────────────────────

def _flow_file_paths(adapter: Dict[str, Any], flow_id: str) -> List[str]:
    """按优先级返回候选路径列表（先外部，后项目内置）。"""
    return [
        os.path.join(_EXTERNAL_FLOWS_DIR, f"{flow_id}.json"),
        os.path.join(adapter["local_flows_dir"], f"{flow_id}.json"),
    ]


def _list_flow_ids(adapter: Dict[str, Any]) -> List[str]:
    ids = set()
    for base_dir in (_EXTERNAL_FLOWS_DIR, adapter["local_flows_dir"]):
        if os.path.isdir(base_dir):
            for name in os.listdir(base_dir):
                if name.endswith(".json") and not name.endswith(".bak.json"):
                    ids.add(name[:-len(".json")])
    return sorted(ids)


def _read_flow(adapter: Dict[str, Any], flow_id: str) -> Optional[Dict[str, Any]]:
    for path in _flow_file_paths(adapter, flow_id):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    return None


def _write_flow(adapter: Dict[str, Any], flow_id: str, graph: Dict[str, Any]) -> str:
    """
    保存流程：若 /config/flows/ 目录存在（说明是挂载了外部配置的生产部署），写到外部目录；
    否则写回项目内置目录（本地开发场景）。保存前自动备份旧版本，方便回滚。
    """
    if os.path.isdir(_EXTERNAL_FLOWS_DIR):
        target_dir = _EXTERNAL_FLOWS_DIR
    else:
        target_dir = adapter["local_flows_dir"]
    os.makedirs(target_dir, exist_ok=True)

    path = os.path.join(target_dir, f"{flow_id}.json")
    if os.path.exists(path):
        backup_path = f"{path}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(path, backup_path)
        logger.info(_LOG, f"保存前备份旧版本: {backup_path}")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)
    logger.info(_LOG, f"流程 '{flow_id}' 已保存 -> {path}")
    return path


# ──────────────────────────────────────────────────────────────────────────────
# 演练（dry-run）
# ──────────────────────────────────────────────────────────────────────────────

def _run_dryrun(adapter: Dict[str, Any], graph: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    """
    用项目 adapter 提供的"临时机器人 + handler 注册表"跑一遍流程，最多跑 timeout 秒
    （演示循环流程时到时间即安全停止），返回逐节点执行轨迹供前端回放。
    """
    dryrun_mod = adapter["dryrun_adapter"]
    signal_bus = SignalBus()
    stop_event = threading.Event()
    pause_event = threading.Event()
    pause_event.set()

    handlers, robots, task_state_machine, auto_fire_thread = dryrun_mod.build_dryrun_engine_inputs(
        signal_bus, stop_event,
    )
    auto_fire_thread.start()

    trace: List[Dict[str, Any]] = []
    trace_lock = threading.Lock()

    def on_event(node_id: str, status: str, record: NodeRecord):
        with trace_lock:
            trace.append({
                "node_id": record.node_id, "type": record.type, "label": record.label,
                "status": record.status, "message": record.message,
                "started_at": record.started_at, "ended_at": record.ended_at,
            })

    engine = FlowEngine(
        graph, handlers=handlers, pause_event=pause_event, stop_event=stop_event,
        on_event=on_event, signal_bus=signal_bus,
    )

    result_box: Dict[str, Any] = {}

    def _runner():
        result_box["result"] = engine.run()

    t = threading.Thread(target=_runner, daemon=True, name="flow-dryrun-runner")
    t.start()
    t.join(timeout=timeout)
    stop_event.set()
    t.join(timeout=5)

    dryrun_mod.disconnect_dryrun_robots(robots)

    result: Optional[FlowResult] = result_box.get("result")
    return {
        "finished": not t.is_alive(),
        "status": result.status if result else "timeout",
        "success": result.success if result else False,
        "message": result.message if result else f"演练达到 {timeout}s 上限，已安全停止（循环流程属正常现象）",
        "context": result.context if result else {},
        "trace": trace,
    }


# ──────────────────────────────────────────────────────────────────────────────
# HTTP handler
# ──────────────────────────────────────────────────────────────────────────────

class FlowAPIHandler(BaseHTTPRequestHandler):

    def _send_json(self, payload: Dict[str, Any], status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel_path: str):
        rel_path = rel_path.lstrip("/") or "index.html"
        abs_path = os.path.normpath(os.path.join(_EDITOR_STATIC_DIR, rel_path))
        # 防止路径穿越
        if not abs_path.startswith(os.path.normpath(_EDITOR_STATIC_DIR)):
            self.send_error(403, "Forbidden")
            return
        if not os.path.isfile(abs_path):
            self.send_error(404, f"Not Found: {rel_path}")
            return
        ext = os.path.splitext(abs_path)[1]
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }.get(ext, "application/octet-stream")
        with open(abs_path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def do_OPTIONS(self):  # CORS 预检
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── GET ──────────────────────────────────────────────────────────────────

    def do_GET(self):
        try:
            path = urlparse(self.path).path

            if path == "/api/projects":
                return self._send_json({"success": True, "projects": list(PROJECT_ADAPTERS.keys())})

            if path.startswith("/api/projects/"):
                parts = path[len("/api/projects/"):].strip("/").split("/")
                project = parts[0]
                adapter = _get_adapter(project)
                if adapter is None:
                    return self._send_json({"success": False, "message": f"未知项目: {project}"}, 404)

                if len(parts) == 2 and parts[1] == "node-types":
                    return self._send_json({"success": True, "node_types": adapter["node_type_schemas"]})

                if len(parts) == 2 and parts[1] == "flows":
                    return self._send_json({"success": True, "flows": _list_flow_ids(adapter)})

                if len(parts) == 3 and parts[1] == "flows":
                    flow_id = parts[2]
                    graph = _read_flow(adapter, flow_id)
                    if graph is None:
                        return self._send_json({"success": False, "message": f"流程 '{flow_id}' 不存在"}, 404)
                    return self._send_json({"success": True, "flow_id": flow_id, "graph": graph})

            if path == "/" or not path.startswith("/api/"):
                return self._send_static(path if path != "/" else "index.html")

            self.send_error(404, f"Not Found: {path}")
        except Exception as e:  # noqa: BLE001
            logger.exception_occurred(_LOG, "处理GET请求", e)
            self.send_error(500, f"Internal Server Error: {e}")

    # ── POST ─────────────────────────────────────────────────────────────────

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if not path.startswith("/api/projects/"):
                return self.send_error(404, f"Not Found: {path}")

            parts = path[len("/api/projects/"):].strip("/").split("/")
            project = parts[0]
            adapter = _get_adapter(project)
            if adapter is None:
                return self._send_json({"success": False, "message": f"未知项目: {project}"}, 404)

            if len(parts) >= 3 and parts[1] == "flows":
                flow_id = parts[2]
                body = self._read_json_body()
                graph = body.get("graph", body)

                if len(parts) == 4 and parts[3] == "validate":
                    errors = validate_flow_graph(graph, known_handler_types=adapter["known_handler_types"])
                    return self._send_json({"success": len(errors) == 0, "errors": errors})

                if len(parts) == 4 and parts[3] == "dryrun":
                    errors = validate_flow_graph(graph, known_handler_types=adapter["known_handler_types"])
                    if errors:
                        return self._send_json({"success": False, "message": "流程校验未通过，无法演练", "errors": errors}, 400)
                    timeout = float(body.get("timeout", 30.0))
                    dryrun_result = _run_dryrun(adapter, graph, timeout=timeout)
                    return self._send_json({"success": True, "dryrun": dryrun_result})

                if len(parts) == 3:  # 保存
                    errors = validate_flow_graph(graph, known_handler_types=adapter["known_handler_types"])
                    if errors:
                        return self._send_json({"success": False, "message": "流程校验未通过，未保存", "errors": errors}, 400)
                    saved_path = _write_flow(adapter, flow_id, graph)
                    return self._send_json({"success": True, "message": "保存成功", "path": saved_path})

            self.send_error(404, f"Not Found: {path}")
        except Exception as e:  # noqa: BLE001
            logger.exception_occurred(_LOG, "处理POST请求", e)
            self.send_error(500, f"Internal Server Error: {e}")

    def log_message(self, format, *args):  # noqa: A002
        logger.info(_LOG, "HTTP请求: " + (format % args))


# ──────────────────────────────────────────────────────────────────────────────
# 服务器生命周期（风格与 network/http_server.py 的 HTTPCommandServer 一致）
# ──────────────────────────────────────────────────────────────────────────────

class FlowAPIServer:
    def __init__(self, host: str = "0.0.0.0", port: Optional[int] = None):
        cfg = get_flow_api_server_config()
        self.host = host
        self.port = port or cfg.get("port", 8099)
        self.server: Optional[HTTPServer] = None
        self.server_thread: Optional[threading.Thread] = None
        self.running = False

    def start(self):
        if self.running:
            logger.warning(_LOG, "服务器已在运行中")
            return
        try:
            self.server = HTTPServer((self.host, self.port), FlowAPIHandler)
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                # 最常见的情况是上一次启动的本服务还在后台跑着（比如用 & / nohup 起的，
                # 或者上次终端关掉了但进程没退），端口被自己占着。直接给出排查命令，
                # 免得只看到一串 socketserver 内部调用栈不知道从哪下手。
                self._log_port_in_use()
            raise
        self.running = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                               name="FlowAPIServer")
        self.server_thread.start()
        logger.info(_LOG, f"Flow API 服务器启动成功: {self.host}:{self.port}")
        print(f"图形化流程编辑器运行在: http://{self.host}:{self.port}/  "
              f"（API 前缀 /api/projects/<project>/...，与命令端口完全分开）")

    def _log_port_in_use(self):
        msg = (
            f"端口 {self.port} 已被占用，Flow API 服务器启动失败。\n"
            f"  多半是上一次启动的本服务还在后台运行。排查/处理：\n"
            f"    1) 查看谁占用：  ss -lptn 'sport = :{self.port}'\n"
            f"    2) 停掉旧进程：  pkill -f 'network.flow_api_server'\n"
            f"    3) 或换个端口：  改 robot_config.json 的 flow_api_server.port\n"
            f"  如果旧进程本来就在正常服务，直接浏览器打开 http://<本机IP>:{self.port}/ 即可，"
            f"不需要再启动一份。"
        )
        logger.error(_LOG, msg)
        print(msg)

    def stop(self):
        if not self.running:
            return
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.server_thread:
            self.server_thread.join(timeout=5)
        self.running = False
        logger.info(_LOG, "Flow API 服务器已停止")


if __name__ == "__main__":
    server = FlowAPIServer()
    try:
        server.start()
    except OSError:
        # 启动失败的原因已经在 start() 里打印成了可读的中文提示，这里不再抛一遍调用栈
        sys.exit(1)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()