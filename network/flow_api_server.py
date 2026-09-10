"""
network/flow_api_server.py

图形化流程编辑器的后端 API 服务（独立端口，与命令端口 network/http_server.py 完全分开，
互不影响；实现风格与其保持一致，同样基于标准库 http.server，不引入额外依赖）。

职责：
    1. 托管 flow_editor/ 下的静态页面（拖拽画布 GUI，零构建依赖，见该目录 README）
    2. 提供流程 JSON 的加载 / 保存 / 校验 / 版本回滚 / 导出导入（软件更新迁移）接口
    3. 提供"演练"（dry-run）接口：用连接 mock_rosbridge_server 的临时机器人跑一遍流程，
       返回逐节点执行轨迹，供前端在画布上高亮回放
    4. 托管「主程序输出」页：读取 main.py 镜像到 logs/main_console.log 的终端日志

多项目支持方式：每个支持"图形化编排"的项目提供一个 adapter 模块（当前只有
``programs/WRC_FLOW/dryrun_adapter.py``），在下面的 ``PROJECT_ADAPTERS`` 里注册一行即可，
本文件不需要认识任何项目的具体业务。

启动方式：
    python3 -m network.flow_api_server
或在 main.py 里跟命令 HTTP 服务器一起启动（可选）。
"""

from __future__ import annotations

import atexit
import copy
import errno
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

from infrastructure.error_logger import get_error_logger
from infrastructure.config_loader import get_flow_api_server_config
from core.common_nodes import COMMON_NODE_TYPE_SCHEMAS
from core.flow_engine import (
    FlowEngine, FlowResult, NodeRecord, SignalBus,
    validate_flow_graph, BUILTIN_NODE_TYPE_SCHEMAS,
)
from core.flow_store import (
    canonical_flow_id,
    safe_flow_id,
    list_flow_summaries as _store_list_flow_summaries,
    load_flow as _store_load_flow,
)

FLOW_PACK_FORMAT = "robot_connect.flow_pack"
FLOW_PACK_VERSION = 1

logger = get_error_logger()
_LOG = "FlowAPIServer"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EDITOR_STATIC_DIR = os.path.join(_REPO_ROOT, "flow_editor")
_CONSOLE_LOG = os.path.join(_REPO_ROOT, "logs", "main_console.log")
_CONSOLE_CHUNK = 256 * 1024

# Docker 外部挂载的流程配置目录优先级高于项目内置目录，与 core/FLOW_ENGINE_GUIDE.md 第 7 节一致
_EXTERNAL_FLOWS_DIR = "/config/flows"


# ──────────────────────────────────────────────────────────────────────────────
# 项目适配器注册表——新增一个支持图形化编排的项目时，在这里加一行即可
# ──────────────────────────────────────────────────────────────────────────────

def _load_wrc_flow_adapter():
    from programs.WRC_FLOW import dryrun_adapter, node_handlers
    return {
        "display_name": "WRC",
        "local_flows_dir": os.path.join(_REPO_ROOT, "programs", "WRC_FLOW", "flows"),
        "project_node_schemas": node_handlers.NODE_TYPE_SCHEMAS,
        "common_nodes": node_handlers.COMMON_NODES,
        "known_handler_types": (
            list(node_handlers.NODE_TYPE_SCHEMAS.keys()) + list(node_handlers.COMMON_NODES)
        ),
        "dryrun_adapter": dryrun_adapter,
        "poses_project": "WRC_FLOW",
        "poses_config_dir": os.path.join(_REPO_ROOT, "programs", "WRC_FLOW"),
        "robot_options": ["robot_a", "robot_b", "robot_c"],
        "step_options": [],   # WRC 的步骤名是自由文本，不做枚举
        "service_options": node_handlers.SERVICE_OPTIONS,
        # WRC_FLOW 的流程靠 PROCESS_BEGINS 常驻循环，中途只等人工复位这一个信号
        "command_options": ["PROCESS_BEGINS", "MANUAL_RESET_COMPLETED", "manual_reset"],
        "exclusive_enabled_flow": True,
        "run_control": {
            "hint": (
                "START_WORKING 只连机器人。「流程开始」才打开已激活的那份展会图。"
                "图上等待人工复位时点「人工复位」，不要再发 PROCESS_BEGINS。"
                "备选图关掉「激活」。main.py 的 active_project 必须是 WRC_FLOW。"
            ),
            "buttons": [
                {"action": "begin", "text": "启动", "cmd": "START_WORKING", "group": "power",
                 "hint": "连接机器人、下发导航地图等初始化。完成后即可随时接收业务命令。"},
                {"action": "reset_system", "text": "重置系统", "cmd": "RESET_SYSTEM",
                 "group": "power", "confirm": True, "danger": True,
                 "hint": "清调度侧记忆（任务状态、忙闲等）并休眠。需再次启动才能收命令。",
                 "confirm_message": (
                     "重置主要作用于调度系统本身：抹掉任务状态、忙闲等记忆参数，"
                     "断开连接并回到休眠。\n"
                     "不是一条发给机器人的业务动作。重置后需再次「启动」才能收命令。\n\n"
                     "确定发送 RESET_SYSTEM？"
                 )},
                {"action": "process_begins", "text": "流程开始", "cmd": "PROCESS_BEGINS",
                 "group": "flow",
                 "hint": "打开已激活的流程图。先「启动」连上机器人，本条只发一次；之后用「人工复位」。"},
                {"action": "pause", "text": "流程暂停", "cmd": "PROCESS_PAUSED", "group": "flow",
                 "hint": "暂停正在跑的调度流程，机器人保持已连接。"},
                {"action": "resume", "text": "流程恢复", "cmd": "PROCESS_RESUMED", "group": "flow",
                 "hint": "从暂停处继续调度流程。"},
                {"action": "end", "text": "流程结束", "cmd": "PROCESS_ENDED", "group": "flow",
                 "confirm": True,
                 "hint": "安全终止当前调度流程。",
                 "confirm_message": "确定发送 PROCESS_ENDED、结束当前调度流程吗？"},
                {"action": "manual_reset", "text": "人工复位",
                 "cmd": "MANUAL_RESET_COMPLETED", "group": "flow",
                 "hint": "唤醒流程里等待人工复位的节点。"},
                {"action": "cancel_op", "text": "操作取消", "group": "interrupt",
                 "disabled": True,
                 "hint": "取消当前操作动作。需要操作以 Action 方式调用，并且操作代码支持。"},
                {"action": "cancel_nav", "text": "导航取消", "group": "interrupt",
                 "disabled": True,
                 "hint": "取消当前正在运行的导航任务。本项目暂未开放该命令。"},
            ],
            "command_templates": [
                {"label": "PROCESS_BEGINS",
                 "path": "test_commands/PROCESS_BEGINS_command.json"},
                {"label": "PROCESS_PAUSED",
                 "path": "test_commands/PROCESS_PAUSED_command.json"},
                {"label": "PROCESS_RESUMED",
                 "path": "test_commands/PROCESS_RESUMED_command.json"},
                {"label": "PROCESS_ENDED",
                 "path": "test_commands/PROCESS_ENDED_command.json"},
                {"label": "MANUAL_RESET_COMPLETED",
                 "path": "test_commands/MANUAL_RESET_COMPLETED_command.json"},
            ],
            "allowed_commands": [
                "PROCESS_BEGINS", "PROCESS_PAUSED", "PROCESS_RESUMED",
                "PROCESS_ENDED", "MANUAL_RESET_COMPLETED",
                "START_WORKING", "RESET_SYSTEM", "GET_TASK_STATE",
            ],
        },
    }


def _load_kaiao_flow_adapter():
    from programs.KAIAO_FLOW import dryrun_adapter, node_handlers
    from programs.KAIAO_FLOW import KAIAO_FLOW
    return {
        "display_name": "KAIAO",
        "local_flows_dir": os.path.join(_REPO_ROOT, "programs", "KAIAO_FLOW", "flows"),
        "project_node_schemas": node_handlers.NODE_TYPE_SCHEMAS,
        "common_nodes": node_handlers.COMMON_NODES,
        "known_handler_types": (
            list(node_handlers.NODE_TYPE_SCHEMAS.keys()) + list(node_handlers.COMMON_NODES)
        ),
        "dryrun_adapter": dryrun_adapter,
        "robot_options": ["robot_a", "robot_b"],
        "step_options": node_handlers.STEP_OPTIONS,
        "service_options": node_handlers.SERVICE_OPTIONS,
        "command_options": list(KAIAO_FLOW.ALL_COMMANDS) + [
            "PROCESS_BEGINS", "PROCESS_PAUSED", "PROCESS_RESUMED", "PROCESS_ENDED",
        ],
        "exclusive_enabled_flow": False,
        # KAIAO_FLOW 复用 KAIAO 的点位（同一个现场、同一张地图，本就该是同一份）：
        # 运行时 programs/KAIAO/constants.py 读的就是 navigation_poses.KAIAO，
        # 编辑器也必须改这一份，否则界面上改了点位而机器人用的还是另一份。
        "poses_project": "KAIAO",
        "poses_config_dir": os.path.join(_REPO_ROOT, "programs", "KAIAO"),
        # 走廊中间点与点位同一份 KAIAO robot_config，避免 FLOW 精简配置里看不见、两套参数漂移。
        "waypoint_config_key": "kaiao_waypoint",
        "run_control": {
            "hint": (
                "PROCESS_BEGINS / PAUSED / ENDED 控制流程开关。"
                "本项目默认不强制 PROCESS_BEGINS（上位机用 PICK_BOX_TO_SP 等开任务）；"
                "若 robot_config.flow_control.require_process_begins=true 则必须先开开关。"
            ),
            "buttons": [
                {"action": "begin", "text": "启动", "cmd": "START_WORKING", "group": "power",
                 "hint": "连接机器人、下发导航地图等初始化。完成后即可随时接收业务命令。"},
                {"action": "reset_system", "text": "重置系统", "cmd": "RESET_SYSTEM",
                 "group": "power", "confirm": True, "danger": True,
                 "hint": "清调度侧记忆（任务状态、持箱、货架重量等）并休眠。需再次启动才能收命令。",
                 "confirm_message": (
                     "重置主要作用于调度系统本身：抹掉任务状态、忙闲、持箱、货架重量等记忆参数，"
                     "断开连接并回到休眠。\n"
                     "不是一条发给机器人的业务动作。重置后需再次「启动」才能收命令。\n\n"
                     "确定发送 RESET_SYSTEM？"
                 )},
                {"action": "pause", "text": "流程暂停", "cmd": "PROCESS_PAUSED", "group": "flow",
                 "hint": "暂停正在跑的流程图。"},
                {"action": "resume", "text": "流程恢复", "cmd": "PROCESS_RESUMED", "group": "flow",
                 "hint": "从暂停处继续。"},
                {"action": "end", "text": "流程结束", "cmd": "PROCESS_ENDED", "group": "flow",
                 "confirm": True,
                 "hint": "结束当前流程图任务。",
                 "confirm_message": "确定发送 PROCESS_ENDED、结束当前流程图吗？"},
                {"action": "cancel_op", "text": "操作取消", "group": "interrupt",
                 "disabled": True,
                 "hint": "取消当前操作动作。需要操作以 Action 方式调用，并且操作代码支持。"},
                {"action": "cancel_nav", "text": "导航取消", "cmd": "CANCEL_NAVIGATION",
                 "group": "interrupt",
                 "hint": "取消当前正在运行的导航任务。",
                 "command": {
                     "cmd_type": "CANCEL_NAVIGATION",
                     "params": {"robot_id": "robot_a", "goal_id": ""},
                 }},
            ],
            "command_templates": [
                {"label": "PROCESS_BEGINS",
                 "path": "test_commands/PROCESS_BEGINS_command.json"},
                {"label": "PICK_BOX_TO_SP（货架→AGV）",
                 "path": "test_commands/KAIAO_PICK_BOX_TO_SP_command.json"},
                {"label": "PICK_COMPONENT_TO_SP",
                 "path": "test_commands/KAIAO_PICK_COMPONENT_TO_SP_command.json"},
                {"label": "NAVIGATION",
                 "path": "test_commands/KAIAO_NAVIGATION_command.json"},
                {"label": "PICK_UP_BOX",
                 "path": "test_commands/KAIAO_PICK_UP_BOX_command.json"},
                {"label": "PUT_DOWN_BOX",
                 "path": "test_commands/KAIAO_PUT_DOWN_BOX_command.json"},
                {"label": "CANCEL_NAVIGATION",
                 "path": "test_commands/KAIAO_CANCEL_NAVIGATION_command.json"},
            ],
            "allowed_commands": [
                "PROCESS_BEGINS", "PROCESS_PAUSED", "PROCESS_RESUMED", "PROCESS_ENDED",
                "PICK_BOX_TO_SP", "PICK_COMPONENT_TO_SP", "NAVIGATION",
                "PICK_UP_BOX", "PUT_DOWN_BOX", "CANCEL_NAVIGATION",
                "START_WORKING", "RESET_SYSTEM", "GET_TASK_STATE",
            ],
        },
    }


def _load_const_flow_adapter():
    from programs.CONST_FLOW import dryrun_adapter, node_handlers
    return {
        "display_name": "ConST",
        "local_flows_dir": os.path.join(_REPO_ROOT, "programs", "CONST_FLOW", "flows"),
        "project_node_schemas": node_handlers.NODE_TYPE_SCHEMAS,
        "common_nodes": node_handlers.COMMON_NODES,
        # 处理器仍注册着（旧流程图能跑），只是不希望有人再拖它到新图上，
        # 理由见 node_handlers.HIDDEN_NODE_TYPES 的说明
        "hidden_node_types": node_handlers.HIDDEN_NODE_TYPES,
        "graph_validator": node_handlers.validate_graph,
        "known_handler_types": (
            list(node_handlers.NODE_TYPE_SCHEMAS.keys()) + list(node_handlers.COMMON_NODES)
        ),
        "dryrun_adapter": dryrun_adapter,
        "poses_project": "CONST_FLOW",
        "poses_config_dir": os.path.join(_REPO_ROOT, "programs", "CONST_FLOW"),
        "robot_options": ["robot_a"],
        "step_options": node_handlers.STEP_OPTIONS,
        "service_options": node_handlers.SERVICE_OPTIONS,
        "command_options": ["PROCESS_BEGINS", "CONST_HUMAN_HANDLED"],
        "exclusive_enabled_flow": True,
        "run_control": {
            "hint": (
                "PROCESS_BEGINS 打开检定循环。本项目默认不强制该开关（上位机/MQTT）；"
                "robot_config.flow_control.require_process_begins=true 时必须先发 PROCESS_BEGINS。"
            ),
            "buttons": [
                {"action": "begin", "text": "启动", "cmd": "START_WORKING", "group": "power",
                 "hint": "连接机器人。是否立刻开跑检定循环，看 flow_control.require_process_begins。"},
                {"action": "reset_system", "text": "重置系统", "cmd": "RESET_SYSTEM",
                 "group": "power", "confirm": True, "danger": True,
                 "hint": "停自动流程、断开 MQTT 与机器人，回到休眠。",
                 "confirm_message": "确定发送 RESET_SYSTEM？自动检定循环会停。"},
                {"action": "pause", "text": "流程暂停", "cmd": "PROCESS_PAUSED", "group": "flow",
                 "hint": "暂停正在跑的检定循环。"},
                {"action": "resume", "text": "流程恢复", "cmd": "PROCESS_RESUMED", "group": "flow",
                 "hint": "从暂停处继续。"},
                {"action": "end", "text": "流程结束", "cmd": "PROCESS_ENDED", "group": "flow",
                 "confirm": True,
                 "hint": "停止自动循环，需再次启动才会再跑。",
                 "confirm_message": "确定结束当前 ConST 自动循环吗？"},
                {"action": "human_handled", "text": "人工已处理",
                 "cmd": "CONST_HUMAN_HANDLED", "group": "flow",
                 "hint": "清除心跳里的 needHuman，让等待人工的步骤继续。"},
                {"action": "cancel_op", "text": "操作取消", "group": "interrupt",
                 "disabled": True,
                 "hint": "当前动作走 Service，没有操作取消。"},
                {"action": "cancel_nav", "text": "导航取消", "cmd": "CANCEL_NAVIGATION",
                 "group": "interrupt",
                 "hint": "取消当前正在运行的导航任务。",
                 "command": {
                     "cmd_type": "CANCEL_NAVIGATION",
                     "params": {"robot_id": "robot_a", "goal_id": ""},
                 }},
            ],
            "command_templates": [
                {"label": "PROCESS_BEGINS",
                 "path": "test_commands/PROCESS_BEGINS_command.json"},
            ],
            "allowed_commands": [
                "START_WORKING", "RESET_SYSTEM", "GET_TASK_STATE",
                "PROCESS_BEGINS", "PROCESS_PAUSED", "PROCESS_RESUMED", "PROCESS_ENDED",
                "CONST_HUMAN_HANDLED", "CANCEL_NAVIGATION",
            ],
        },
    }


PROJECT_ADAPTERS = {
    "WRC_FLOW": _load_wrc_flow_adapter,
    "KAIAO_FLOW": _load_kaiao_flow_adapter,
    "CONST_FLOW": _load_const_flow_adapter,
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

def _project_path_parts(path: str) -> List[str]:
    """/api/projects/{project}/... 按段拆开，并把百分号编码还原成中文。"""
    raw = path[len("/api/projects/"):].strip("/")
    return [unquote(p) for p in raw.split("/") if p]


def _list_flow_ids(adapter: Dict[str, Any]) -> List[str]:
    return [row["id"] for row in _list_flow_summaries(adapter)]


def _list_flow_summaries(adapter: Dict[str, Any]) -> List[Dict[str, Any]]:
    return _store_list_flow_summaries(adapter["local_flows_dir"], _EXTERNAL_FLOWS_DIR)


def _disable_other_enabled_flows(adapter: Dict[str, Any], keep_id: str) -> List[str]:
    """同一项目只允许一份激活时，把其它已激活的图关掉。"""
    disabled = []
    for flow_id in _list_flow_ids(adapter):
        if flow_id == keep_id:
            continue
        graph = _read_flow(adapter, flow_id)
        if not graph or not graph.get("enabled"):
            continue
        graph["enabled"] = False
        _write_flow(adapter, flow_id, graph)
        disabled.append(flow_id)
    return disabled


def _read_flow(adapter: Dict[str, Any], flow_id: str) -> Optional[Dict[str, Any]]:
    return _store_load_flow(flow_id, adapter["local_flows_dir"], _EXTERNAL_FLOWS_DIR)


#: 保存时不能丢的元信息字段。role 尤其要紧：它标记一张图是子流程还是可独立运行的
#: 入口，丢了会被当成入口，导致单入口项目报"同时激活了多份流程"而跑不起来。
_META_FIELDS = ("id", "name", "description", "role")


def _keep_meta_fields(adapter: Dict[str, Any], flow_id: str, graph: Dict[str, Any]):
    """
    客户端没带元信息字段时，从磁盘上的旧版本补回来。

    前端已经会透传这些字段，这里是第二道保险：导入流程包、第三方脚本直接 POST
    这类路径未必带全，而丢一次 role 的代价是现场主流程起不来。
    """
    if not isinstance(graph, dict):
        return
    missing = [f for f in _META_FIELDS if f not in graph]
    if not missing:
        return
    old = _read_flow(adapter, flow_id)
    if not isinstance(old, dict):
        return
    for field in missing:
        if field in old:
            graph[field] = old[field]
            logger.info(_LOG, f"流程 '{flow_id}' 保存时补回元信息字段 {field}")


def _write_flow(adapter: Dict[str, Any], flow_id: str, graph: Dict[str, Any]) -> str:
    """
    保存流程：若 /config/flows/ 目录存在（说明是挂载了外部配置的生产部署），写到外部目录；
    否则写回项目内置目录（本地开发场景）。保存前自动备份旧版本，方便回滚。
    中文流程名按原文写文件，不再把 URL 编码写进文件名。
    """
    flow_id = canonical_flow_id(flow_id)
    if os.path.isdir(_EXTERNAL_FLOWS_DIR):
        target_dir = _EXTERNAL_FLOWS_DIR
    else:
        target_dir = adapter["local_flows_dir"]
    os.makedirs(target_dir, exist_ok=True)

    path = os.path.join(target_dir, f"{flow_id}.json")
    legacy_encoded = os.path.join(target_dir, quote(flow_id, safe="") + ".json")
    existing = path if os.path.exists(path) else (
        legacy_encoded if os.path.abspath(legacy_encoded) != os.path.abspath(path)
        and os.path.exists(legacy_encoded) else None
    )
    if existing:
        backup_path = f"{path}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(existing, backup_path)
        logger.info(_LOG, f"保存前备份旧版本: {backup_path}")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)
    if os.path.abspath(legacy_encoded) != os.path.abspath(path) and os.path.exists(legacy_encoded):
        try:
            os.remove(legacy_encoded)
            logger.info(_LOG, f"已删除旧版 URL 编码文件名: {legacy_encoded}")
        except OSError as e:
            logger.warning(_LOG, f"删除旧版编码文件名失败 {legacy_encoded}: {e}")
    logger.info(_LOG, f"流程 '{flow_id}' 已保存 -> {path}")
    return path


def _build_flow_pack(adapter: Dict[str, Any], project: str) -> Dict[str, Any]:
    """本项目全部流程图打成一份迁移包，给软件更新后导回用。"""
    flows: Dict[str, Any] = {}
    for row in _list_flow_summaries(adapter):
        fid = row.get("id")
        if not fid:
            continue
        graph = _read_flow(adapter, fid)
        if isinstance(graph, dict):
            flows[fid] = graph
    return {
        "format": FLOW_PACK_FORMAT,
        "version": FLOW_PACK_VERSION,
        "exported_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "project": project,
        "flows": flows,
    }


def _import_flow_pack(adapter: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    把输出的 JSON 写回当前项目。不因校验失败拒绝写入——软件更新后节点 schema
    可能变了，先保住现场图画，校验问题当作警告让人在编辑器里修。
    """
    raw_flows = payload.get("flows")
    if not isinstance(raw_flows, dict) or not raw_flows:
        return {"success": False, "message": "文件里没有可导入的流程（缺少 flows）"}

    written: List[Dict[str, str]] = []
    skipped: List[str] = []
    warnings: List[str] = []
    known = adapter.get("known_handler_types")

    for raw_id, graph in raw_flows.items():
        fid = safe_flow_id(str(raw_id))
        if not fid:
            skipped.append(str(raw_id))
            warnings.append("跳过非法流程 id: " + str(raw_id))
            continue
        if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
            skipped.append(str(raw_id))
            warnings.append("'" + fid + "' 不是有效流程图，已跳过")
            continue
        try:
            errors = validate_flow_graph(graph, known_handler_types=known)
        except Exception as e:  # noqa: BLE001
            errors = ["校验异常: " + str(e)]
        path = _write_flow(adapter, fid, graph)
        written.append({"id": fid, "path": path})
        if errors:
            warnings.append("'" + fid + "' 已写入，但校验有问题: " + "；".join(errors))

    if not written:
        return {
            "success": False,
            "message": "没有写入任何流程",
            "skipped": skipped,
            "warnings": warnings,
        }
    return {
        "success": True,
        "message": "已导入 " + str(len(written)) + " 份流程",
        "written": written,
        "skipped": skipped,
        "warnings": warnings,
        "source_project": payload.get("project"),
    }


# ──────────────────────────────────────────────────────────────────────────────
# 导航点位读写（robot_config.json 的 navigation_poses.<项目> 段）
# ──────────────────────────────────────────────────────────────────────────────
#
# 点位坐标本来只能手改 robot_config.json，现场调试很不方便。这里提供读写接口，
# 让编辑器可以直接改点位。写入目标沿用与流程文件一致的分层习惯：
# 挂载了 /config/robot_config.json 的生产部署写外部文件，否则写项目内置文件。

_EXTERNAL_ROBOT_CONFIG = "/config/robot_config.json"


def _poses_target(adapter: Optional[Dict[str, Any]], project: str) -> Tuple[str, str]:
    """
    点位存在哪：返回 (navigation_poses 里的段名, 项目配置目录)。

    通常两者都等于项目名，但 KAIAO_FLOW 这类"图形化试点"项目复用原项目的点位，
    所以由 adapter 显式指定，避免编辑器改的和运行时读的不是同一份。
    """
    if adapter:
        return (
            adapter.get("poses_project", project),
            adapter.get("poses_config_dir", os.path.join(_REPO_ROOT, "programs", project)),
        )
    return project, os.path.join(_REPO_ROOT, "programs", project)


def _pose_config_path(adapter: Optional[Dict[str, Any]], project: str) -> str:
    """点位写到哪个文件：外部挂载配置优先，否则项目内置配置。"""
    if os.path.isfile(_EXTERNAL_ROBOT_CONFIG):
        return _EXTERNAL_ROBOT_CONFIG
    _, config_dir = _poses_target(adapter, project)
    return os.path.join(config_dir, "robot_config.json")


def _robot_config_path(project: str) -> str:
    """
    运行控制里编辑的 robot_config：Docker 挂载的 /config 优先（运行时最高层），
    否则写当前图形化项目自己的 programs/<project>/robot_config.json。

    注意不要复用点位路径：KAIAO_FLOW 的点位在 programs/KAIAO/，但 robots / IP
    写在 programs/KAIAO_FLOW/。这里改的是后者。
    """
    if os.path.isfile(_EXTERNAL_ROBOT_CONFIG):
        return _EXTERNAL_ROBOT_CONFIG
    return os.path.join(_REPO_ROOT, "programs", project, "robot_config.json")


def _read_robot_config(project: str) -> Dict[str, Any]:
    path = _robot_config_path(project)
    if not os.path.isfile(path):
        return {"success": True, "path": path, "exists": False, "config": {}, "text": "{\n}\n"}
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        cfg = json.loads(text)
    except json.JSONDecodeError as e:
        return {"success": False, "path": path, "message": f"配置文件不是合法 JSON: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "path": path, "message": f"读取失败: {e}"}
    if not isinstance(cfg, dict):
        return {"success": False, "path": path, "message": "配置文件必须是 JSON 对象"}
    indent = _detect_json_indent(path)
    return {
        "success": True, "path": path, "exists": True, "config": cfg,
        "text": _dump_config_json(cfg, indent),
    }


def _write_robot_config(project: str, cfg: Dict[str, Any]) -> str:
    path = _robot_config_path(project)
    target_dir = os.path.dirname(path)
    os.makedirs(target_dir, exist_ok=True)
    indent = _detect_json_indent(path) if os.path.isfile(path) else 4
    if os.path.isfile(path):
        backup_path = f"{path}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(path, backup_path)
        logger.info(_LOG, f"保存 robot_config 前备份: {backup_path}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_dump_config_json(cfg, indent))
    try:
        from infrastructure.config_loader import reload_config
        reload_config()
    except Exception as e:  # noqa: BLE001
        logger.warning(_LOG, f"配置已写入但编辑器进程 reload_config 失败: {e}")
    logger.info(_LOG, f"[{project}] robot_config 已保存 -> {path}")
    return path


def _waypoint_schema_bundle():
    from programs.KAIAO.constants import (
        KAIAO_WAYPOINT_FIELDS,
        KAIAO_WAYPOINT_INTRO,
        KAIAO_WAYPOINT_KEY,
        kaiao_waypoint_comments,
        kaiao_waypoint_defaults,
        merge_kaiao_waypoint_config,
    )
    return {
        "key": KAIAO_WAYPOINT_KEY,
        "intro": KAIAO_WAYPOINT_INTRO,
        "fields": KAIAO_WAYPOINT_FIELDS,
        "defaults": kaiao_waypoint_defaults(),
        "comments": kaiao_waypoint_comments(),
        "merge": merge_kaiao_waypoint_config,
    }


def _read_waypoint_config(adapter: Optional[Dict[str, Any]], project: str) -> Optional[Dict[str, Any]]:
    """
    读走廊中间点段。KAIAO_FLOW 与点位相同：外部 /config 优先，否则 programs/KAIAO。
    """
    key = (adapter or {}).get("waypoint_config_key")
    if not key:
        return None
    bundle = _waypoint_schema_bundle()
    found_path = _pose_config_path(adapter, project)
    raw: Dict[str, Any] = {}
    _, config_dir = _poses_target(adapter, project)
    for path in (_EXTERNAL_ROBOT_CONFIG, os.path.join(config_dir, "robot_config.json")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning(_LOG, f"读取走廊导航参数失败 {path}: {e}")
            continue
        section = cfg.get(key)
        if isinstance(section, dict):
            raw = section
            found_path = path
            break
    values = bundle["merge"](raw)
    return {
        "key": key,
        "path": found_path,
        "intro": bundle["intro"],
        "schema": bundle["fields"],
        "values": values,
    }


def _validate_waypoint_values(values: Any, schema: List[Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    if not isinstance(values, dict):
        return ["走廊导航参数必须是对象"]
    by_key = {f["key"]: f for f in schema}
    for key, field in by_key.items():
        if key not in values:
            continue
        val = values[key]
        ftype = field.get("type")
        label = field.get("label") or key
        if ftype == "number":
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                errors.append(f"{label}（{key}）必须是数字")
        elif ftype == "string_list":
            if not isinstance(val, list) or not all(isinstance(x, str) and x.strip() for x in val):
                errors.append(f"{label}（{key}）必须是非空字符串数组，例如 [\"shelf0_3\",\"shelf1_3\"]")
        elif ftype == "json":
            if not isinstance(val, list):
                errors.append(f"{label}（{key}）必须是 JSON 数组")
    return errors


def _write_waypoint_config(
    adapter: Optional[Dict[str, Any]],
    project: str,
    values: Dict[str, Any],
) -> str:
    """只改 kaiao_waypoint 段，其余配置原样保留。写入点位同一文件。"""
    bundle = _waypoint_schema_bundle()
    key = (adapter or {}).get("waypoint_config_key") or bundle["key"]
    path = _pose_config_path(adapter, project)
    cfg: Dict[str, Any] = {}
    indent = 4
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        indent = _detect_json_indent(path)
        backup_path = f"{path}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(path, backup_path)
        logger.info(_LOG, f"保存走廊导航参数前备份: {backup_path}")

    current = cfg.get(key) if isinstance(cfg.get(key), dict) else {}
    merged = bundle["merge"](current)
    for field in bundle["fields"]:
        fname = field["key"]
        if fname in values:
            merged[fname] = values[fname]
    section: Dict[str, Any] = {
        "_comment": bundle["intro"],
        "_comments": bundle["comments"],
    }
    for field in bundle["fields"]:
        section[field["key"]] = merged[field["key"]]
    cfg[key] = section

    target_dir = os.path.dirname(path)
    os.makedirs(target_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_dump_config_json(cfg, indent))
    try:
        from infrastructure.config_loader import reload_config
        reload_config()
    except Exception as e:  # noqa: BLE001
        logger.warning(_LOG, f"走廊导航参数已写入但 reload_config 失败: {e}")
    logger.info(_LOG, f"[{project}] 走廊导航参数已保存 -> {path} 的 {key} 段")
    return path


def _read_poses(project: str, adapter: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    按 infrastructure/pose_loader.py 的同一套优先级读点位（外部 > 项目内置）。
    这里直接读文件而不复用 pose_loader，是因为它带进程级缓存、且会用硬编码默认值
    补全缺失项；编辑器要展示的是"配置文件里真实写了什么"。
    """
    poses_project, config_dir = _poses_target(adapter, project)
    for path in (_EXTERNAL_ROBOT_CONFIG, os.path.join(config_dir, "robot_config.json")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning(_LOG, f"读取点位配置失败 {path}: {e}")
            continue
        poses = (cfg.get("navigation_poses") or {}).get(poses_project)
        if isinstance(poses, dict):
            return {"path": path, "poses": poses, "poses_project": poses_project}
    return {"path": _pose_config_path(adapter, project), "poses": {}, "poses_project": poses_project}


def _validate_poses(poses: Any) -> List[str]:
    """校验点位格式，规则与 pose_loader._is_valid_pose 保持一致（7 元坐标或其列表）。"""
    errors: List[str] = []
    if not isinstance(poses, dict):
        return ["点位数据必须是对象 {点位名: 坐标}"]
    for name, value in poses.items():
        if not isinstance(name, str) or not name:
            errors.append(f"非法点位名: {name!r}")
            continue
        if not isinstance(value, list) or not value:
            errors.append(f"点位 '{name}': 坐标必须是非空数组")
            continue
        segments = value if isinstance(value[0], list) else [value]
        for i, seg in enumerate(segments):
            if not isinstance(seg, list) or len(seg) != 7:
                errors.append(f"点位 '{name}' 第 {i + 1} 段: 需要 7 个数字 [x,y,z,qx,qy,qz,qw]")
                continue
            if not all(isinstance(v, (int, float)) for v in seg):
                errors.append(f"点位 '{name}' 第 {i + 1} 段: 坐标必须全部是数字")
    return errors


def _detect_json_indent(path: str, default: int = 4) -> int:
    """
    探测已有 JSON 文件的缩进宽度。

    robot_config.json 是人工维护的文件，写回时若换一种缩进会把整个文件重排，
    git diff 里几百行全是空格变化，真正的改动反而被淹没。所以沿用原有风格。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.lstrip(" ")
                if stripped.startswith('"') and stripped != line:
                    return len(line) - len(stripped)
    except Exception:  # noqa: BLE001
        pass
    return default


def _is_compact_numeric_array(value: Any) -> bool:
    """点位坐标这类纯数字数组（含一段或多段 7 元组）适合压成单行。"""
    if not isinstance(value, list) or not value:
        return False
    return all(
        isinstance(x, (int, float))
        or (isinstance(x, list) and x and all(isinstance(n, (int, float)) for n in x))
        for x in value
    )


def _dump_config_json(cfg: Dict[str, Any], indent: int) -> str:
    """
    序列化配置文件，但让点位坐标等数字数组保持单行。

    `json.dump` 会把 ``[[x, y, z, qx, qy, qz, qw]]`` 摊成十几行，于是每次在界面上
    改一个点位，整个 navigation_poses 段都会被重排——git diff 里几百行噪音，
    现场想对照"这次到底改了哪个点"根本看不出来。

    做法是先把纯数字数组换成占位符，正常序列化之后再替换回单行写法。
    """
    frozen: Dict[str, str] = {}
    counter = 0

    def freeze(obj: Any) -> Any:
        nonlocal counter
        if _is_compact_numeric_array(obj):
            token = f"__NUMARR_{counter}__"
            counter += 1
            frozen[token] = json.dumps(obj, ensure_ascii=False)
            return token
        if isinstance(obj, dict):
            return {k: freeze(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [freeze(x) for x in obj]
        return obj

    text = json.dumps(freeze(copy.deepcopy(cfg)), ensure_ascii=False, indent=indent)
    for token, compact in frozen.items():
        text = text.replace(f'"{token}"', compact)
    return text + "\n"


def _write_poses(project: str, poses: Dict[str, Any],
                 adapter: Optional[Dict[str, Any]] = None) -> str:
    """把点位写回 robot_config.json，只替换 navigation_poses.<段名> 段，其余配置原样保留。"""
    poses_project, _ = _poses_target(adapter, project)
    path = _pose_config_path(adapter, project)
    cfg: Dict[str, Any] = {}
    indent = 4
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        indent = _detect_json_indent(path)
        backup_path = f"{path}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(path, backup_path)
        logger.info(_LOG, f"保存点位前备份旧配置: {backup_path}")

    nav = cfg.get("navigation_poses")
    if not isinstance(nav, dict):
        nav = {}
    nav[poses_project] = poses
    cfg["navigation_poses"] = nav

    with open(path, "w", encoding="utf-8") as f:
        f.write(_dump_config_json(cfg, indent))
    logger.info(_LOG, f"[{project}] 导航点位已保存 -> {path} 的 {poses_project} 段（{len(poses)} 个）")
    return path


def _robot_conn_info(robot_id: str) -> Optional[Dict[str, Any]]:
    """从合并后的配置里取一台机器人的连接信息。"""
    from infrastructure.config_loader import load_config
    robots = (load_config() or {}).get("robots") or {}
    if robot_id and robot_id in robots:
        info = dict(robots[robot_id])
        info["id"] = robot_id
        return info
    for rid, rc in robots.items():
        if isinstance(rc, dict) and rc.get("enabled", True):
            info = dict(rc)
            info["id"] = rid
            return info
    return None


def _with_live_robot(robot_id: str):
    """
    临时连一台真机。navigation_map=None，避免这次只读位姿时再触发自动 set_map。
    返回 (robot, info, error)；error 非空时 robot 为 None。
    """
    from hardware.robot_controller import RobotController

    info = _robot_conn_info(robot_id)
    if not info:
        return None, None, "未找到机器人配置"
    host = info.get("host")
    try:
        port = int(info.get("port") or 9090)
    except (TypeError, ValueError):
        port = 9090
    robot = RobotController(
        host=host, port=port, robot_type=info["id"],
        max_retry_attempts=1, retry_interval=1,
        navigation_map=None,
    )
    try:
        if not robot.connect():
            return None, info, f"无法连接 {info['id']} ({host}:{port})"
        return robot, info, None
    except Exception as e:  # noqa: BLE001
        return None, info, str(e)


def _close_live_robot(robot) -> None:
    if robot is None:
        return
    try:
        robot.stop_reconnect()
        robot.close()
    except Exception:  # noqa: BLE001
        pass


def _live_current_pose(robot_id: str) -> Dict[str, Any]:
    from hardware.navigation_utils import get_robot_odom

    robot, info, err = _with_live_robot(robot_id)
    if err:
        return {"success": False, "message": err}
    try:
        odom = get_robot_odom(robot, timeout=4.0)
        if not odom:
            return {"success": False, "message": "未收到导航里程计 /zj_humanoid/navigation/odom_info（导航可能未开）"}
        pose = [[
            float(odom.get("Position_Point_X_In") or 0),
            float(odom.get("Position_Point_Y_In") or 0),
            float(odom.get("Position_Point_Z_In") or 0),
            float(odom.get("Orientation_X_In") or 0),
            float(odom.get("Orientation_Y_In") or 0),
            float(odom.get("Orientation_Z_In") or 0),
            float(odom.get("Orientation_W_In") or 1),
        ]]
        return {
            "success": True,
            "robot_id": info["id"],
            "host": info.get("host"),
            "pose": pose,
            "odom": odom,
        }
    finally:
        _close_live_robot(robot)


def _validate_graph(graph: Dict[str, Any], adapter: Dict[str, Any]) -> List[str]:
    """通用结构校验 + 项目自定义校验（adapter 可选提供 graph_validator）。"""
    errors = validate_flow_graph(graph, known_handler_types=adapter["known_handler_types"])
    validator = adapter.get("graph_validator")
    if validator:
        try:
            errors = errors + list(validator(graph) or [])
        except Exception as exc:  # noqa: BLE001
            logger.error(_LOG, f"项目自定义校验异常: {exc}")
    return errors


def _flow_ids(adapter: Dict[str, Any]) -> List[str]:
    """本项目现有的流程图 id，供「调用子流程」节点做下拉。"""
    ids = []
    for d in (adapter.get("local_flows_dir"), _EXTERNAL_FLOWS_DIR):
        if not d or not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.endswith(".json") and fn[:-5] not in ids:
                ids.append(fn[:-5])
    return ids


def _node_types_with_dynamic_options(project: str, adapter: Dict[str, Any]) -> Dict[str, Any]:
    """
    组装返回给编辑器的节点 schema：按来源打上 scope 标记，并把"选项来自实时配置"
    的字段填上当前值。

    **scope** 决定节点在编辑器面板里归到哪一栏，三层的含义见 core/common_nodes.py：
        builtin —— 引擎内置          common —— 跨项目通用          project —— 本项目专有
    分栏是为了避免"同名不同参"的坑：不同项目对同一概念的参数可能完全不同
    （KAIAO 的导航要选走廊中间点策略，WRC 的导航没有这些），混在一起容易用错。

    **options_source** 让选项跟着配置走，不必改代码重启：
        poses  —— robot_config.json 当前的导航点位
        steps  —— 项目的任务步骤枚举（adapter 提供，为空则前端降级成自由文本）
        robots —— 项目的机器人列表

    注意要深拷贝：adapter 是进程级缓存的，直接改会把注入结果永久写进缓存，
    后面点位变了也刷不掉。
    """
    # 通用节点是"项目可选实现"：只给出项目在 common_nodes 里声明支持的那几个，
    # 否则面板上会出现本项目没有处理器的块，拖上去要等跑起来才报"未知节点类型"。
    enabled_common = set(adapter.get("common_nodes") or COMMON_NODE_TYPE_SCHEMAS.keys())
    common_schemas = {k: v for k, v in COMMON_NODE_TYPE_SCHEMAS.items() if k in enabled_common}

    schemas: Dict[str, Any] = {}
    for scope, source in (
        ("builtin", BUILTIN_NODE_TYPE_SCHEMAS),
        ("common", common_schemas),
        ("project", adapter.get("project_node_schemas") or {}),
    ):
        for name, schema in copy.deepcopy(source).items():
            schema["scope"] = scope
            if scope == "project":
                schema["project"] = adapter.get("display_name", project)
            schemas[name] = schema

    # 处理器还注册着（旧流程图要能跑），但不希望有人再拖到新图上的节点
    for name in (adapter.get("hidden_node_types") or []):
        schemas.pop(name, None)

    option_sources = {
        "poses": sorted((_read_poses(project, adapter).get("poses") or {}).keys()),
        "steps": list(adapter.get("step_options") or []),
        "robots": list(adapter.get("robot_options") or []),
        "services": list(adapter.get("service_options") or []),
        "commands": list(adapter.get("command_options") or []),
        # 「调用子流程」的可选项：本项目已有的流程图，省得手敲 flow_id 敲错
        "flows": sorted(_flow_ids(adapter)),
    }

    for schema in schemas.values():
        for field in schema.get("fields", []):
            options = option_sources.get(field.get("options_source"))
            if not options:
                continue
            # extra_options 是固定附加项，用于让操作人员能直接选到流程变量模板
            # （KAIAO 这类项目的目标点位是命令参数算出来的，画图时并不确定）
            field["options"] = options + list(field.get("extra_options") or [])

    dryrun_defaults = dict(getattr(adapter.get("dryrun_adapter"), "DEFAULT_DRYRUN_SIGNALS", None) or {})
    wait_schema = schemas.get("wait_for_command")
    if wait_schema and dryrun_defaults:
        for field in wait_schema.get("fields") or []:
            if field.get("name") == "event_name":
                field["option_defaults"] = copy.deepcopy(dryrun_defaults)
                break
    return schemas


# ──────────────────────────────────────────────────────────────────────────────
# 转发业务命令到命令端口（供编辑器启动/暂停/结束"真实"流程）
# ──────────────────────────────────────────────────────────────────────────────
#
# 真实调度流程跑在 main.py 那个进程里（命令端口默认 8090），和本编辑器服务
# （默认 8099）是两个完全独立的进程。编辑器不直接碰机器人，而是把命令按现有的
# HTTP 命令协议转发过去，因此不会绕过任何既有的校验、状态机和日志。
#
# 「启动」一律只发 START_WORKING。业务命令（WRC 的 PROCESS_BEGINS、
# KAIAO 的 PICK_BOX_TO_SP 等）由外部设备或 /control/send 模拟下发。

_CONTROL_ACTIONS = {
    "begin": "START_WORKING",
    "reset_system": "RESET_SYSTEM",
    "process_begins": "PROCESS_BEGINS",
    "pause": "PROCESS_PAUSED",
    "resume": "PROCESS_RESUMED",
    "end": "PROCESS_ENDED",
    "manual_reset": "MANUAL_RESET_COMPLETED",
    "human_handled": "CONST_HUMAN_HANDLED",
}


def _command_server_url() -> str:
    try:
        from infrastructure.config_loader import get_http_server_port
        port = get_http_server_port()
    except Exception:  # noqa: BLE001
        port = 8090
    return f"http://127.0.0.1:{port}"


def _forward_payload(payload: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
    cmd_type = payload.get("cmd_type") or ""
    url = _command_server_url()
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        logger.info(_LOG, f"转发命令 {cmd_type} -> {url} 成功")
        return {"success": True, "command": payload, "response": json.loads(body)}
    except urllib.error.HTTPError as e:
        # 业务失败（休眠 409 等）仍然带 JSON 体，不能当成"连不上命令端口"。
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001
            parsed = {"success": False, "message": raw or f"HTTP {e.code}: {e.reason}"}
        if "success" not in parsed:
            parsed["success"] = False
        logger.info(_LOG, f"转发命令 {cmd_type} -> {url} HTTP {e.code}: {parsed.get('message')}")
        return {"success": True, "command": payload, "response": parsed}
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        msg = (f"无法连接业务命令服务 {url}（{reason}）。"
               f"请确认主程序 main.py 已启动，且 robot_config.json 的 http_server.port 与之一致。")
        logger.error(_LOG, f"转发命令 {cmd_type} 失败: {msg}")
        return {"success": False, "message": msg}
    except Exception as e:  # noqa: BLE001
        logger.error(_LOG, f"转发命令 {cmd_type} 异常: {e}")
        return {"success": False, "message": f"转发命令失败: {e}"}


def _forward_command(cmd_type: str, params: Optional[Dict[str, Any]] = None,
                     timeout: float = 10.0) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "cmd_type": cmd_type,
        "cmd_id": f"flow_editor_{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
    }
    if params:
        payload.update(params)
    return _forward_payload(payload, timeout=timeout)


def _forward_raw_command(command: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
    payload = dict(command)
    payload["cmd_id"] = f"flow_editor_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    return _forward_payload(payload, timeout=timeout)


def _run_control_payload(adapter: Dict[str, Any]) -> Dict[str, Any]:
    rc = adapter.get("run_control") or {}
    templates: List[Dict[str, Any]] = []
    repo_root = os.path.normpath(_REPO_ROOT)
    for t in rc.get("command_templates") or []:
        item: Dict[str, Any] = {
            "label": t.get("label") or t.get("path"),
            "path": t.get("path"),
            "command": None,
        }
        rel = t.get("path") or ""
        path = os.path.normpath(os.path.join(_REPO_ROOT, rel))
        if not path.startswith(repo_root + os.sep) and path != repo_root:
            item["error"] = "非法模板路径"
            templates.append(item)
            continue
        if not os.path.isfile(path):
            item["error"] = f"文件不存在: {rel}"
            templates.append(item)
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                item["command"] = json.load(f)
        except Exception as e:  # noqa: BLE001
            item["error"] = f"读取失败: {e}"
        templates.append(item)
    runtime_project = ""
    try:
        from infrastructure.pose_loader import get_active_project
        runtime_project = get_active_project() or ""
    except Exception:  # noqa: BLE001
        runtime_project = ""
    return {
        "hint": rc.get("hint") or "",
        "buttons": rc.get("buttons") or [],
        "command_templates": templates,
        "allowed_commands": list(rc.get("allowed_commands") or []),
        "config_active_project": runtime_project,
    }


def _read_console_log(qs: Dict[str, List[str]]) -> Dict[str, Any]:
    """
    读取 main.py 镜像出来的控制台日志。
    after = 已读到的字节偏移；tail = 首次加载时从文件尾往前取的字节数。
    文件被轮转导致 after 超过当前大小时，从尾部重新跟。
    """
    path = _CONSOLE_LOG
    if not os.path.isfile(path):
        return {
            "success": True, "exists": False, "offset": 0, "text": "",
            "path": path,
            "hint": "还没有主程序控制台日志。请启动 main.py（不要只开编辑器进程）。",
        }
    size = os.path.getsize(path)
    try:
        after = int((qs.get("after") or ["0"])[0])
    except (TypeError, ValueError):
        after = 0
    try:
        tail = int((qs.get("tail") or ["0"])[0])
    except (TypeError, ValueError):
        tail = 0
    tail = max(0, min(tail, _CONSOLE_CHUNK * 4))

    if after > size:
        after = 0
        tail = tail or _CONSOLE_CHUNK
    if after < 0:
        after = 0

    start = after
    if tail and after == 0:
        start = max(0, size - tail)

    with open(path, "rb") as f:
        f.seek(start)
        chunk = f.read(_CONSOLE_CHUNK)
    text = chunk.decode("utf-8", errors="replace")
    return {
        "success": True,
        "exists": True,
        "offset": start + len(chunk),
        "size": size,
        "text": text,
        "truncated": start > 0 and after == 0,
        "path": path,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 演练（dry-run）
# ──────────────────────────────────────────────────────────────────────────────

_MOCK_SCRIPT = os.path.join(_REPO_ROOT, "mock_rosbridge", "mock_rosbridge_server.py")
_MOCK_READY_TIMEOUT = 6.0
_owned_mock_lock = threading.Lock()
_owned_mock_proc: Optional[subprocess.Popen] = None
_mock_keep_alive = False
_USER_MOCK_PORTS: List[Tuple[str, int]] = [
    ("robot_a", 9090),
    ("robot_b", 9091),
    ("robot_c", 9092),
]


def _tcp_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _localhost_mock_specs(dryrun_mod) -> List[Tuple[str, str, int]]:
    """演练需要的本机 mock 端口：[(robot_id, host, port), ...]。远程地址不自动拉起。"""
    mapping = getattr(dryrun_mod, "MOCK_ROBOTS", None) or {}
    specs: List[Tuple[str, str, int]] = []
    for rid, addr in mapping.items():
        host, port = str(addr[0]), int(addr[1])
        if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
            specs.append((str(rid), "127.0.0.1", port))
    return specs


def _offline_mock_targets(dryrun_mod) -> List[str]:
    offline: List[str] = []
    for rid, host, port in _localhost_mock_specs(dryrun_mod):
        if not _tcp_open(host, port):
            offline.append(f"{rid} ({host}:{port})")
    return offline


def _find_mock_pids() -> List[int]:
    """找出本机正在跑的 mock_rosbridge_server.py（不含当前编辑器进程）。"""
    pids: List[int] = []
    my_pid = os.getpid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == my_pid:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", errors="replace")
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        if "mock_rosbridge_server.py" in cmd:
            pids.append(pid)
    return pids


def _kill_pids(pids: List[int], timeout: float = 3.0) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    deadline = time.time() + timeout
    pending = set(pids)
    while pending and time.time() < deadline:
        still = set()
        for pid in pending:
            try:
                os.kill(pid, 0)
                still.add(pid)
            except (ProcessLookupError, OSError):
                pass
        pending = still
        if pending:
            time.sleep(0.1)
    for pid in pending:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _stop_owned_mock(force: bool = False):
    """关掉本服务 Popen 记下的那份 mock。"""
    global _owned_mock_proc
    if _mock_keep_alive and not force:
        return
    with _owned_mock_lock:
        proc = _owned_mock_proc
        _owned_mock_proc = None
    if proc is None or proc.poll() is not None:
        return
    logger.info(_LOG, f"关闭自动启动的 mock pid={proc.pid}")
    _kill_pids([proc.pid])


def _mock_port_status() -> List[Dict[str, Any]]:
    return [
        {"robot_id": rid, "port": port, "listening": _tcp_open("127.0.0.1", port)}
        for rid, port in _USER_MOCK_PORTS
    ]


def _mock_owned_alive() -> bool:
    with _owned_mock_lock:
        proc = _owned_mock_proc
    return proc is not None and proc.poll() is None


def _mock_status_payload(message: str = "") -> Dict[str, Any]:
    ports = _mock_port_status()
    running = any(p["listening"] for p in ports)
    owned = _mock_owned_alive()
    payload: Dict[str, Any] = {
        "success": True,
        "running": running,
        "owned": owned,
        "keep": _mock_keep_alive,
        "script": _MOCK_SCRIPT,
        "ports": ports,
    }
    if message:
        payload["message"] = message
    elif not running:
        payload["message"] = "mock 未运行。打开开关将启动 mock_rosbridge_server.py（9090/9091/9092）。"
    elif owned:
        payload["message"] = "mock 由本面板启动，关闭开关会停掉它。"
    else:
        payload["message"] = "mock 已在运行（可能是上次残留或别处启动）。关闭开关会停掉本机 mock_rosbridge_server.py。"
    return payload


def _mock_fail(message: str) -> Dict[str, Any]:
    payload = _mock_status_payload(message)
    payload["success"] = False
    return payload


def _start_editor_mock() -> Dict[str, Any]:
    """运行控制开关：拉起默认三端口 mock，并标记为保持，演练结束也不关。"""
    global _mock_keep_alive, _owned_mock_proc
    if _mock_owned_alive():
        _mock_keep_alive = True
        return _mock_status_payload("mock 已在运行")
    ports = _mock_port_status()
    if all(p["listening"] for p in ports):
        return _mock_status_payload("mock 已在运行")
    if any(p["listening"] for p in ports):
        listening = ", ".join(f"{p['robot_id']}:{p['port']}" for p in ports if p["listening"])
        return _mock_fail(f"部分端口已被占用（{listening}），无法再启动一份 mock。")
    if not os.path.isfile(_MOCK_SCRIPT):
        return _mock_fail(f"找不到 mock 脚本: {_MOCK_SCRIPT}")

    robots_cli = [f"{rid}:{port}" for rid, port in _USER_MOCK_PORTS]
    cmd = [sys.executable, _MOCK_SCRIPT, "--robots", *robots_cli]
    logger.info(_LOG, "面板启动 mock: " + " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=os.path.dirname(_MOCK_SCRIPT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        return _mock_fail(f"启动 mock 失败: {e}")

    with _owned_mock_lock:
        _owned_mock_proc = proc
    _mock_keep_alive = True

    deadline = time.time() + _MOCK_READY_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            _mock_keep_alive = False
            return _mock_fail(
                f"mock 进程启动后立刻退出（exit={proc.returncode}）。请检查端口是否被占用。"
            )
        if all(_tcp_open("127.0.0.1", port) for _, port in _USER_MOCK_PORTS):
            return _mock_status_payload("mock 已启动")
        time.sleep(0.15)

    missing = [f"{rid}:{port}" for rid, port in _USER_MOCK_PORTS if not _tcp_open("127.0.0.1", port)]
    return _mock_fail(
        f"已启动 mock 但 {_MOCK_READY_TIMEOUT:.0f}s 内端口仍不可达（{', '.join(missing)}）"
    )


def _stop_editor_mock() -> Dict[str, Any]:
    """面板关闭开关：停掉本面板启动的 mock，以及本机残留的 mock_rosbridge_server.py。"""
    global _mock_keep_alive
    _mock_keep_alive = False
    pids = set(_find_mock_pids())
    with _owned_mock_lock:
        proc = _owned_mock_proc
        if proc is not None and proc.poll() is None:
            pids.add(proc.pid)
    if pids:
        logger.info(_LOG, f"面板关闭 mock pids={sorted(pids)}")
        _kill_pids(sorted(pids))
    _stop_owned_mock(force=True)
    deadline = time.time() + 2.0
    while time.time() < deadline and any(p["listening"] for p in _mock_port_status()):
        time.sleep(0.1)
    leftover = [p for p in _mock_port_status() if p["listening"]]
    if leftover:
        still = ", ".join(f"{p['robot_id']}:{p['port']}" for p in leftover)
        return _mock_fail(f"已发停止信号，但仍有端口在听（{still}）。可能被真机 rosbridge 占用。")
    return _mock_status_payload("已关闭 mock")


def _ensure_dryrun_mock(dryrun_mod) -> Tuple[Optional[str], bool]:
    """
    所需本机端口都已在听则复用（started=False，演练结束不关）。
    否则拉起 mock_rosbridge_server.py，等最多 _MOCK_READY_TIMEOUT 秒。
    返回 (错误信息或 None, 是否由本进程启动)。
    """
    global _owned_mock_proc
    specs = _localhost_mock_specs(dryrun_mod)
    if not specs:
        return None, False
    if all(_tcp_open(host, port) for _, host, port in specs):
        return None, False
    if not os.path.isfile(_MOCK_SCRIPT):
        return f"找不到 mock 脚本: {_MOCK_SCRIPT}", False

    robots_cli = [f"{rid}:{port}" for rid, _, port in specs]
    cmd = [sys.executable, _MOCK_SCRIPT, "--robots", *robots_cli]
    logger.info(_LOG, "演练自动启动 mock: " + " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=os.path.dirname(_MOCK_SCRIPT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        return f"自动启动 mock 失败: {e}", False

    with _owned_mock_lock:
        _owned_mock_proc = proc

    deadline = time.time() + _MOCK_READY_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            missing = ", ".join(f"{rid}:{port}" for rid, _, port in specs)
            return (
                f"mock 进程启动后立刻退出（exit={proc.returncode}）。"
                f"请检查端口 {missing} 是否被占用，或 websockets 依赖是否可用。",
                True,
            )
        if all(_tcp_open(host, port) for _, host, port in specs):
            return None, True
        time.sleep(0.15)

    missing = [f"{rid} ({host}:{port})" for rid, host, port in specs if not _tcp_open(host, port)]
    return (
        f"已启动 mock 但 {_MOCK_READY_TIMEOUT:.0f}s 内端口仍不可达（{', '.join(missing)}）",
        True,
    )


atexit.register(lambda: _stop_owned_mock(force=True))


def _connected_robot_ids(robots: Dict[str, Any]) -> List[str]:
    connected: List[str] = []
    for rid, robot in (robots or {}).items():
        check = getattr(robot, "is_connected", None)
        if callable(check) and check():
            connected.append(str(rid))
    return connected


def _dryrun_timeout_message(timeout: float, trace: List[Dict[str, Any]]) -> str:
    stuck = None
    for rec in reversed(trace or []):
        if rec.get("status") == "running":
            stuck = rec
            break
    if stuck:
        label = stuck.get("label") or stuck.get("type") or stuck.get("node_id")
        return (
            f"演练达到 {timeout}s 上限，节点「{label}」仍在执行。"
            f"常见原因：导航/动作未在超时内返回结果。"
        )
    return f"演练达到 {timeout}s 上限，已安全停止"


def _as_signal_data(raw: Any) -> Dict[str, Any]:
    """把演练弹窗 / 节点上的 JSON 收成 SignalBus.fire 用的 dict。"""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if isinstance(raw, list):
        return {"jobs": raw}
    if isinstance(raw, dict):
        # 允许直接粘贴 test_commands/*.json 整包
        if "cmd_type" in raw and ("params" in raw or "extra" in raw):
            data: Dict[str, Any] = {}
            params = raw.get("params")
            extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
            if isinstance(params, list):
                data["jobs"] = params
            elif isinstance(params, dict):
                data.update(params)
            for k, v in extra.items():
                data.setdefault(k, v)
            return data
        return raw
    return {}


def _resolve_dryrun_signals(
    graph: Dict[str, Any],
    override: Optional[List[Any]],
    dryrun_mod,
) -> List[Dict[str, Any]]:
    """
    演练要 fire 的信号：优先用 POST body.signals（弹窗里刚改的），
    否则读图上每个 wait_for_command 的 dryrun_params。
    """
    defaults = getattr(dryrun_mod, "DEFAULT_DRYRUN_SIGNALS", None) or {}
    if override:
        out: List[Dict[str, Any]] = []
        for item in override:
            if not isinstance(item, dict):
                continue
            event = item.get("event") or item.get("event_name")
            if not event:
                continue
            raw = item["data"] if "data" in item else item.get("params")
            out.append({"event": str(event), "data": _as_signal_data(raw)})
        return out

    out = []
    for node in graph.get("nodes") or []:
        if node.get("type") != "wait_for_command":
            continue
        params = node.get("params") or {}
        event = params.get("event_name")
        if not event:
            continue
        if "dryrun_params" in params and params.get("dryrun_params") not in (None, ""):
            data = _as_signal_data(params.get("dryrun_params"))
        elif event in defaults:
            data = dict(defaults[event] or {})
        else:
            data = {}
        out.append({"event": str(event), "data": data})
    return out


_active_dryrun_lock = threading.Lock()
_active_dryrun_stop: Optional[threading.Event] = None


def _register_dryrun_stop(stop_event: threading.Event) -> None:
    global _active_dryrun_stop
    with _active_dryrun_lock:
        prev = _active_dryrun_stop
        _active_dryrun_stop = stop_event
    if prev is not None and prev is not stop_event:
        prev.set()


def _clear_dryrun_stop(stop_event: threading.Event) -> None:
    global _active_dryrun_stop
    with _active_dryrun_lock:
        if _active_dryrun_stop is stop_event:
            _active_dryrun_stop = None


def _cancel_active_dryrun() -> bool:
    with _active_dryrun_lock:
        ev = _active_dryrun_stop
    if ev is None:
        return False
    ev.set()
    return True


def _run_dryrun(adapter: Dict[str, Any], graph: Dict[str, Any], timeout: float = 60.0,
                event_sink: Optional[Any] = None,
                signals: Optional[List[Any]] = None) -> Dict[str, Any]:
    """
    用项目 adapter 提供的"临时机器人 + handler 注册表"跑一遍流程，最多跑 timeout 秒
    （演示循环流程时到时间即安全停止），返回逐节点执行轨迹。

    event_sink 若给出，每个节点开始/结束立刻回调一行（供 HTTP 流式推给编辑器），
    最后再回调一次 type=done 的汇总。浏览器不必等整趟跑完才看到高亮。
    """
    dryrun_mod = adapter["dryrun_adapter"]
    signal_bus = SignalBus()
    stop_event = threading.Event()
    _register_dryrun_stop(stop_event)
    pause_event = threading.Event()
    pause_event.set()
    sink_failed = threading.Event()

    def _emit_sink(payload: Dict[str, Any]) -> None:
        if event_sink is None or sink_failed.is_set():
            return
        try:
            event_sink(payload)
        except Exception:  # noqa: BLE001 —— 浏览器断开就停演练，不必再写
            sink_failed.set()
            stop_event.set()

    def _fail_payload(message: str, status: str = "error") -> Dict[str, Any]:
        payload = {
            "finished": True,
            "status": status,
            "success": False,
            "message": message,
            "context": {},
            "trace": [],
        }
        _emit_sink({"type": "done", "dryrun": payload})
        return payload

    started_mock = False
    robots: Dict[str, Any] = {}
    try:
        mock_err, started_mock = _ensure_dryrun_mock(dryrun_mod)
        if mock_err:
            return _fail_payload(mock_err)

        handlers, robots, task_state_machine, auto_fire_thread = dryrun_mod.build_dryrun_engine_inputs(
            signal_bus, stop_event,
        )

        if not _connected_robot_ids(robots):
            names = ", ".join(str(rid) for rid in (robots or {})) or "none"
            offline = ", ".join(_offline_mock_targets(dryrun_mod)) or names
            return _fail_payload(
                f"演练机器人未连上 mock rosbridge（{offline}）。"
                f"已尝试自动启动 {_MOCK_SCRIPT}。"
            )

        # 先把演练入参打进总线，再跑图：wait_for_command 一到就能拿到。
        # 入参来自弹窗 / 节点 dryrun_params，不再写死在 adapter 的 Python 里。
        resolved = _resolve_dryrun_signals(graph, signals, dryrun_mod)
        for sig in resolved:
            signal_bus.fire(sig["event"], data=sig["data"])
            logger.info(_LOG, f"演练注入命令 {sig['event']}: {list((sig['data'] or {}).keys())}")

        auto_fire_thread.start()

        trace: List[Dict[str, Any]] = []
        trace_lock = threading.Lock()

        def on_event(node_id: str, status: str, record: NodeRecord):
            rec = {
                "node_id": record.node_id, "type": record.type, "label": record.label,
                "status": record.status, "message": record.message,
                "started_at": record.started_at, "ended_at": record.ended_at,
            }
            with trace_lock:
                trace.append(rec)
            _emit_sink({"type": "node", "record": rec})

        engine = FlowEngine(
            graph, handlers=handlers, pause_event=pause_event, stop_event=stop_event,
            on_event=on_event, signal_bus=signal_bus,
            flow_loader=getattr(dryrun_mod, "load_flow", None),
        )

        # 参数化的流程（如 KAIAO 的每条命令一张图）真实运行时靠 HTTP 入参填充变量，
        # 演练时用 dryrun_params / 弹窗 JSON 代替；这里只预置 robot_id 等兜底。
        dryrun_context = dict(getattr(dryrun_mod, "DRYRUN_CONTEXT", None) or {})
        if resolved:
            first = resolved[0].get("data") or {}
            if isinstance(first, dict) and first.get("robot_id"):
                dryrun_context["robot_id"] = first["robot_id"]
            dryrun_context["action_type"] = resolved[0].get("event") or dryrun_context.get("action_type")

        result_box: Dict[str, Any] = {}

        def _runner():
            result_box["result"] = engine.run(
                initial_context=dryrun_context, extra={"dryrun": True, "stop_event": stop_event},
            )

        t = threading.Thread(target=_runner, daemon=True, name="flow-dryrun-runner")
        t.start()
        t.join(timeout=timeout)
        stop_event.set()
        t.join(timeout=5)

        result: Optional[FlowResult] = result_box.get("result")
        payload = {
            "finished": not t.is_alive(),
            "status": result.status if result else "timeout",
            "success": result.success if result else False,
            "message": result.message if result else _dryrun_timeout_message(timeout, trace),
            "context": result.context if result else {},
            "trace": trace,
        }
        _emit_sink({"type": "done", "dryrun": payload})
        return payload
    finally:
        _clear_dryrun_stop(stop_event)
        if robots:
            try:
                dryrun_mod.disconnect_dryrun_robots(robots)
            except Exception as e:  # noqa: BLE001
                logger.warning(_LOG, f"演练断开机器人失败: {e}")
        if started_mock:
            _stop_owned_mock()


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

    def _begin_ndjson(self):
        """演练流式输出：每行一个 JSON，浏览器边收边画。"""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self._ndjson_lock = threading.Lock()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:  # noqa: BLE001
            pass

    def _write_ndjson(self, payload: Dict[str, Any]) -> None:
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        lock = getattr(self, "_ndjson_lock", None)
        if lock is None:
            self.wfile.write(line)
            self.wfile.flush()
            return
        with lock:
            self.wfile.write(line)
            self.wfile.flush()

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
        # 编辑器的 html/js/css 会随版本更新，浏览器缓存住旧版会让人以为"改了没生效"
        # （还得教现场人员按 Ctrl+F5）。这几个文件都很小，直接禁用缓存更省事。
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _is_current_pose_path(parts: List[str]) -> bool:
        # /api/projects/<p>/current-pose  或  /api/projects/<p>/poses/current
        return (
            (len(parts) == 2 and parts[1] == "current-pose")
            or (len(parts) == 3 and parts[1] == "poses" and parts[2] == "current")
        )

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
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            robot_id = (qs.get("robot_id") or ["robot_a"])[0]

            if path == "/api/projects":
                return self._send_json({"success": True, "projects": list(PROJECT_ADAPTERS.keys())})

            if path == "/api/console":
                return self._send_json(_read_console_log(qs))

            if path == "/api/mock-rosbridge":
                return self._send_json(_mock_status_payload())

            if path.startswith("/api/projects/"):
                parts = _project_path_parts(path)
                project = parts[0]
                adapter = _get_adapter(project)
                if adapter is None:
                    return self._send_json({"success": False, "message": f"未知项目: {project}"}, 404)

                if len(parts) == 2 and parts[1] == "node-types":
                    return self._send_json({
                        "success": True,
                        "node_types": _node_types_with_dynamic_options(project, adapter),
                    })

                if len(parts) == 2 and parts[1] == "flows":
                    return self._send_json({
                        "success": True,
                        "flows": _list_flow_summaries(adapter),
                        "exclusive_enabled": bool(adapter.get("exclusive_enabled_flow")),
                    })

                if len(parts) == 3 and parts[1] == "flows":
                    flow_id = parts[2]
                    graph = _read_flow(adapter, flow_id)
                    if graph is None:
                        return self._send_json({"success": False, "message": f"流程 '{flow_id}' 不存在"}, 404)
                    return self._send_json({"success": True, "flow_id": flow_id, "graph": graph})

                if len(parts) == 2 and parts[1] == "poses":
                    info = _read_poses(project, adapter)
                    return self._send_json({
                        "success": True, "poses": info["poses"], "path": info["path"],
                        "poses_project": info["poses_project"],
                    })

                if self._is_current_pose_path(parts):
                    return self._send_json(_live_current_pose(robot_id))

                # 真实流程的运行状态：直接向命令端口查 GET_TASK_STATE
                if len(parts) == 2 and parts[1] == "run-control":
                    return self._send_json({
                        "success": True,
                        **_run_control_payload(adapter),
                    })

                if len(parts) == 2 and parts[1] == "runtime-state":
                    return self._send_json(_forward_command("GET_TASK_STATE"))

                if len(parts) == 2 and parts[1] == "robot-config":
                    payload = _read_robot_config(project)
                    wp = _read_waypoint_config(adapter, project)
                    if wp:
                        payload["waypoint"] = wp
                    return self._send_json(payload)

                if len(parts) == 2 and parts[1] == "export":
                    pack = _build_flow_pack(adapter, project)
                    return self._send_json({
                        "success": True,
                        "pack": pack,
                        "count": len(pack.get("flows") or {}),
                    })

                return self._send_json({"success": False, "message": f"Not Found: {path}"}, 404)

            if path == "/" or not path.startswith("/api/"):
                return self._send_static(path if path != "/" else "index.html")

            return self._send_json({"success": False, "message": f"Not Found: {path}"}, 404)
        except Exception as e:  # noqa: BLE001
            logger.exception_occurred(_LOG, "处理GET请求", e)
            self.send_error(500, f"Internal Server Error: {e}")

    # ── POST ─────────────────────────────────────────────────────────────────

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/mock-rosbridge":
                body = self._read_json_body()
                enabled = body.get("enabled")
                if enabled is True:
                    return self._send_json(_start_editor_mock())
                if enabled is False:
                    return self._send_json(_stop_editor_mock())
                return self._send_json(
                    {"success": False, "message": "请传 {\"enabled\": true} 或 {\"enabled\": false}"}, 400)

            if path == "/api/dryrun/cancel":
                cancelled = _cancel_active_dryrun()
                return self._send_json({
                    "success": True,
                    "cancelled": cancelled,
                    "message": "已请求停止演练" if cancelled else "当前没有正在进行的演练",
                })

            if not path.startswith("/api/projects/"):
                return self._send_json({"success": False, "message": f"Not Found: {path}"}, 404)

            parts = _project_path_parts(path)
            project = parts[0]
            adapter = _get_adapter(project)
            if adapter is None:
                return self._send_json({"success": False, "message": f"未知项目: {project}"}, 404)

            if len(parts) == 2 and parts[1] == "import":
                body = self._read_json_body()
                result = _import_flow_pack(adapter, body if isinstance(body, dict) else {})
                status = 200 if result.get("success") else 400
                return self._send_json(result, status)

            if len(parts) >= 3 and parts[1] == "flows":
                flow_id = parts[2]
                body = self._read_json_body()
                graph = body.get("graph", body)

                if len(parts) == 4 and parts[3] == "enabled":
                    enabled = bool(body.get("enabled"))
                    current = _read_flow(adapter, flow_id)
                    if current is None:
                        return self._send_json(
                            {"success": False, "message": f"流程 '{flow_id}' 不存在，请先保存"}, 404)
                    current["enabled"] = enabled
                    saved_path = _write_flow(adapter, flow_id, current)
                    disabled = []
                    if enabled and adapter.get("exclusive_enabled_flow"):
                        disabled = _disable_other_enabled_flows(adapter, flow_id)
                    return self._send_json({
                        "success": True, "path": saved_path, "enabled": enabled,
                        "disabled": disabled,
                        "exclusive_enabled": bool(adapter.get("exclusive_enabled_flow")),
                    })

                if len(parts) == 4 and parts[3] == "validate":
                    errors = _validate_graph(graph, adapter)
                    return self._send_json({"success": len(errors) == 0, "errors": errors})

                if len(parts) == 4 and parts[3] == "dryrun":
                    errors = _validate_graph(graph, adapter)
                    if errors:
                        return self._send_json({"success": False, "message": "流程校验未通过，无法演练", "errors": errors}, 400)
                    timeout = float(body.get("timeout", 60.0))
                    self._begin_ndjson()
                    try:
                        _run_dryrun(
                            adapter, graph, timeout=timeout,
                            event_sink=self._write_ndjson,
                            signals=body.get("signals"),
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.exception_occurred(_LOG, "演练流式输出", e)
                        try:
                            self._write_ndjson({
                                "type": "done",
                                "dryrun": {
                                    "finished": False, "status": "error",
                                    "success": False, "message": str(e),
                                    "context": {}, "trace": [],
                                },
                            })
                        except Exception:  # noqa: BLE001
                            pass
                    return

                if len(parts) == 3:  # 保存
                    errors = _validate_graph(graph, adapter)
                    if errors:
                        return self._send_json({"success": False, "message": "流程校验未通过，未保存", "errors": errors}, 400)
                    _keep_meta_fields(adapter, flow_id, graph)
                    saved_path = _write_flow(adapter, flow_id, graph)
                    if graph.get("enabled") and adapter.get("exclusive_enabled_flow"):
                        _disable_other_enabled_flows(adapter, flow_id)
                    return self._send_json({"success": True, "message": "保存成功", "path": saved_path})

            if self._is_current_pose_path(parts):
                body = self._read_json_body()
                qs = parse_qs(parsed.query)
                rid = ((body.get("robot_id") if isinstance(body, dict) else None)
                       or (qs.get("robot_id") or [None])[0]
                       or "robot_a")
                return self._send_json(_live_current_pose(rid))

            if len(parts) == 2 and parts[1] == "poses":
                body = self._read_json_body()
                poses = body.get("poses", body)
                errors = _validate_poses(poses)
                if errors:
                    return self._send_json(
                        {"success": False, "message": "点位格式校验未通过，未保存", "errors": errors}, 400)
                saved_path = _write_poses(project, poses, adapter)
                return self._send_json({
                    "success": True, "path": saved_path,
                    "message": ("点位已保存。注意：正在运行的主程序仍在用启动时加载的旧点位，"
                                "需要发送 RESET_SYSTEM 命令或重启主程序后才会生效。"),
                })

            if len(parts) == 2 and parts[1] == "robot-config":
                body = self._read_json_body()
                cfg = body.get("config") if isinstance(body.get("config"), dict) else body
                if not isinstance(cfg, dict):
                    return self._send_json(
                        {"success": False, "message": "配置必须是 JSON 对象"}, 400)
                wp_key = (adapter or {}).get("waypoint_config_key")
                waypoint = body.get("waypoint") if isinstance(body.get("waypoint"), dict) else None
                if wp_key and wp_key in cfg:
                    extracted = cfg.pop(wp_key)
                    if waypoint is None and isinstance(extracted, dict):
                        waypoint = extracted
                if waypoint is not None:
                    if not wp_key:
                        return self._send_json(
                            {"success": False, "message": "本项目没有走廊导航参数段，请不要提交 waypoint"}, 400)
                    bundle = _waypoint_schema_bundle()
                    errors = _validate_waypoint_values(waypoint, bundle["fields"])
                    if errors:
                        return self._send_json(
                            {"success": False, "message": "走廊导航参数校验未通过，未保存",
                             "errors": errors}, 400)
                saved_path = _write_robot_config(project, cfg)
                wp_path = None
                if waypoint is not None:
                    wp_path = _write_waypoint_config(adapter, project, waypoint)
                msg = ("配置已保存。正在运行的 main.py 仍用启动时加载的旧配置，"
                       "需要发送 RESET_SYSTEM 或重启主程序后才会生效。")
                if wp_path and wp_path != saved_path:
                    msg += f" 走廊导航参数写在 {wp_path}。"
                return self._send_json({
                    "success": True, "path": saved_path, "waypoint_path": wp_path,
                    "message": msg,
                })

            # 控制"真实"流程：转发到业务命令端口（不经过本服务的机器人连接）
            if len(parts) == 3 and parts[1] == "control":
                action = parts[2]
                body = self._read_json_body()
                rc = adapter.get("run_control") or {}

                if action == "send":
                    allowed = set(rc.get("allowed_commands") or [])
                    if not allowed:
                        return self._send_json(
                            {"success": False,
                             "message": "本项目不开放模拟 HTTP 发送"}, 400)
                    command = body.get("command") if isinstance(body.get("command"), dict) else body
                    cmd_type = command.get("cmd_type")
                    if not cmd_type:
                        return self._send_json(
                            {"success": False, "message": "命令缺少 cmd_type"}, 400)
                    if cmd_type not in allowed:
                        return self._send_json(
                            {"success": False,
                             "message": f"不允许发送 '{cmd_type}'，本项目开放: {sorted(allowed)}"}, 400)
                    return self._send_json(_forward_raw_command(command))

                cmd_type = _CONTROL_ACTIONS.get(action)
                if cmd_type is None:
                    return self._send_json(
                        {"success": False,
                         "message": f"未知控制动作 '{action}'，支持: "
                                    f"{sorted(list(_CONTROL_ACTIONS) + ['send'])}"}, 400)
                return self._send_json(_forward_command(cmd_type, body.get("params")))

            return self._send_json({"success": False, "message": f"Not Found: {path}"}, 404)
        except Exception as e:  # noqa: BLE001
            logger.exception_occurred(_LOG, "处理POST请求", e)
            self.send_error(500, f"Internal Server Error: {e}")

    def log_message(self, format, *args):  # noqa: A002
        msg = format % args
        if any(p in msg for p in ("/api/console", "/api/mock-rosbridge")):
            return
        logger.info(_LOG, "HTTP请求: " + msg)


# ──────────────────────────────────────────────────────────────────────────────
# 服务器生命周期（风格与 network/http_server.py 的 HTTPCommandServer 一致）
# ──────────────────────────────────────────────────────────────────────────────

class ThreadingFlowAPIServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


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
            self.server = ThreadingFlowAPIServer((self.host, self.port), FlowAPIHandler)
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
        _stop_owned_mock()
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