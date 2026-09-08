"""
流程图 JSON 的磁盘读写与「激活」开关。

编辑器里可以保存多张图（展会正流程、备选方案、测试图），但现场一条
PROCESS_BEGINS / 一条业务命令只能跑被激活的那份，避免多图同时被同一条
命令拉起来。激活标记写在流程 JSON 顶层的 ``enabled``，不写死在 Python 里。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

EXTERNAL_FLOWS_DIR = "/config/flows"


def canonical_flow_id(flow_id: str) -> str:
    """URL 里的中文 id 会被 encode，落盘/列表一律还原成原文。"""
    return unquote(str(flow_id or "").strip())


def safe_flow_id(flow_id: str) -> Optional[str]:
    """导入/保存用：拒绝路径穿越和备份文件名，避免写出 ``../x`` 或被列表逻辑忽略的 ``.bak.``。"""
    fid = canonical_flow_id(flow_id)
    if not fid or fid in (".", ".."):
        return None
    if any(ch in fid for ch in ("/", "\\", "\x00")) or ".." in fid:
        return None
    if ".bak." in fid:
        return None
    return fid


def flow_id_candidates(flow_id: str) -> List[str]:
    """
    读文件时兼容两种历史文件名：
      - 中文原文 ``测试.json``（正确）
      - 旧版把 URL 编码写进文件名 ``%E6%B5%8B%E8%AF%95.json``
    """
    raw = str(flow_id or "").strip()
    names: List[str] = []
    decoded = unquote(raw)
    encoded = quote(decoded, safe="")
    for candidate in (raw, decoded, encoded):
        if candidate and candidate not in names:
            names.append(candidate)
    return names


def is_flow_filename(name: str) -> bool:
    if not name.endswith(".json"):
        return False
    if ".bak." in name or name.endswith(".bak.json"):
        return False
    return True


def list_flow_ids(local_dir: str, external_dir: str = EXTERNAL_FLOWS_DIR) -> List[str]:
    ids = set()
    for base_dir in (external_dir, local_dir):
        if not os.path.isdir(base_dir):
            continue
        for name in os.listdir(base_dir):
            if is_flow_filename(name):
                ids.add(canonical_flow_id(name[:-len(".json")]))
    return sorted(ids)


def load_flow(flow_id: str, local_dir: str,
              external_dir: str = EXTERNAL_FLOWS_DIR) -> Optional[Dict[str, Any]]:
    for base_dir in (external_dir, local_dir):
        if not os.path.isdir(base_dir):
            continue
        for fid in flow_id_candidates(flow_id):
            path = os.path.join(base_dir, f"{fid}.json")
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
    return None


def is_enabled(graph: Optional[Dict[str, Any]]) -> bool:
    """未写 enabled 的旧图视为激活（默认打开）。明确写 false 才是备选/测试图。"""
    if not isinstance(graph, dict):
        return False
    if "enabled" not in graph:
        return True
    return bool(graph.get("enabled"))


def list_flow_summaries(local_dir: str,
                        external_dir: str = EXTERNAL_FLOWS_DIR) -> List[Dict[str, Any]]:
    rows = []
    for flow_id in list_flow_ids(local_dir, external_dir):
        graph = load_flow(flow_id, local_dir, external_dir) or {}
        rows.append({"id": flow_id, "enabled": is_enabled(graph)})
    return rows


def find_enabled_flows(local_dir: str,
                       external_dir: str = EXTERNAL_FLOWS_DIR) -> List[Tuple[str, Dict[str, Any]]]:
    found: List[Tuple[str, Dict[str, Any]]] = []
    for flow_id in list_flow_ids(local_dir, external_dir):
        graph = load_flow(flow_id, local_dir, external_dir)
        if is_enabled(graph):
            found.append((flow_id, graph))  # type: ignore[arg-type]
    return found


def require_one_enabled(local_dir: str,
                        external_dir: str = EXTERNAL_FLOWS_DIR
                        ) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """
    单入口项目（WRC / CONST）用：现场只能有一份激活的图。
    返回 (flow_id, graph, error_message)。
    """
    found = find_enabled_flows(local_dir, external_dir)
    if not found:
        return None, None, "没有已激活的流程。请在图形编辑器打开要跑的那份，打开「激活」开关并保存。"
    if len(found) > 1:
        names = ", ".join(fid for fid, _ in found)
        return None, None, (
            f"同时激活了多份流程（{names}）。"
            "一条 PROCESS_BEGINS 只能跑一份，请只打开其中一份的激活开关。"
        )
    return found[0][0], found[0][1], None


def start_wait_event(graph: Optional[Dict[str, Any]]) -> Optional[str]:
    """起点若是 wait_for_command，返回它等待的命令名。"""
    if not graph:
        return None
    start = graph.get("start")
    for node in graph.get("nodes") or []:
        if node.get("id") != start:
            continue
        if node.get("type") != "wait_for_command":
            return None
        event = (node.get("params") or {}).get("event_name")
        return str(event) if event else None
    return None


def find_enabled_flow_for_command(action_type: str, local_dir: str,
                                  mapped_flow_id: Optional[str] = None,
                                  external_dir: str = EXTERNAL_FLOWS_DIR
                                  ) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """
    命令触发型项目（KAIAO）用：优先跑已激活的映射图；映射图没激活时，
    再找起点等待该命令、且已激活的备选图。
    """
    if mapped_flow_id:
        graph = load_flow(mapped_flow_id, local_dir, external_dir)
        if is_enabled(graph):
            return mapped_flow_id, graph, None
    for flow_id, graph in find_enabled_flows(local_dir, external_dir):
        if start_wait_event(graph) == action_type:
            return flow_id, graph, None
    mapped_note = f"（默认图 '{mapped_flow_id}' 未激活）" if mapped_flow_id else ""
    return None, None, (
        f"没有已激活、可响应 {action_type} 的流程{mapped_note}。"
        "请在图形编辑器打开对应流程并打开「激活」开关。"
    )
