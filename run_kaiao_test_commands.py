#!/usr/bin/env python3
"""
KAIAO 联调：按顺序 POST 测试命令。

- START_WORKING 发送成功后 input() 暂停一次（等待机器人连接）
- 业务命令：HTTP 受理成功后，轮询 GET_TASK_STATE，等任务真正结束后再发下一条
- 任务失败 / HTTP 失败 / curl 失败则立即退出

用法（在程序根目录）:
    python run_kaiao_test_commands.py
    python run_kaiao_test_commands.py --skip-start
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parent
URL = "http://localhost:8090"
POLL_INTERVAL_S = 2.0
TASK_TIMEOUT_S = 7200.0

COMMANDS = [
    "test_commands/START_WORKING_command.json",
    "test_commands/KAIAO_PICK_COMPONENT_TO_SP_command.json",
    "test_commands/KAIAO_PICK_BOX_TO_SP_command_1.json",
    "test_commands/KAIAO_PICK_BOX_TO_SP_command_2.json",
    "test_commands/KAIAO_PICK_BOX_TO_SP_command_3.json",
    "test_commands/KAIAO_PICK_BOX_TO_SP_command_4.json",
    "test_commands/KAIAO_PICK_BOX_TO_SP_command_5.json",
    "test_commands/KAIAO_PICK_BOX_TO_SP_command_6.json",
]

# 任务终态（相对 TaskStatus.value）
_DONE_OK = {"已完成"}
_DONE_FAIL = {"错误", "已取消"}
_RUNNING = {"运行中", "等待中"}


def _http_post_json(payload: Dict[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """POST JSON 到 HTTP 服务，返回 (curl_ok, parsed_dict_or_none, raw_body)。"""
    cmd = [
        "curl", "-sS", "-X", "POST", URL,
        "-H", "Content-Type: application/json",
        "-d", json.dumps(payload, ensure_ascii=False),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        return False, None, err

    body = (result.stdout or "").strip()
    try:
        data = json.loads(body) if body else None
    except json.JSONDecodeError:
        return True, None, body
    return True, data if isinstance(data, dict) else None, body


def _is_http_success(payload: Any) -> bool:
    return isinstance(payload, dict) and bool(payload.get("success"))


def post_command_file(rel_path: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """POST test_commands 文件；要求 HTTP success=true。"""
    path = ROOT / rel_path
    if not path.is_file():
        print(f"✗ 文件不存在: {path}")
        return False, None

    print("\n" + "=" * 60)
    print(f"→ POST {rel_path}")
    print("=" * 60)

    with path.open("r", encoding="utf-8") as f:
        req = json.load(f)

    curl_ok, payload, raw = _http_post_json(req)
    if not curl_ok:
        print(raw)
        print("✗ curl 失败")
        return False, None

    if payload is None:
        print(raw or "(empty response)")
        print("✗ HTTP 回传不是合法 JSON 对象")
        return False, None

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not _is_http_success(payload):
        print("✗ HTTP success!=true，停止后续命令")
        return False, payload

    print("✓ HTTP 已受理 (success=true)")
    return True, payload


def _read_cmd_id_from_file(rel_path: str) -> Optional[str]:
    try:
        with (ROOT / rel_path).open("r", encoding="utf-8") as f:
            return json.load(f).get("cmd_id")
    except Exception:
        return None


def wait_task_finished(cmd_id: str, *, poll_s: float = POLL_INTERVAL_S, timeout_s: float = TASK_TIMEOUT_S) -> bool:
    """
    轮询 GET_TASK_STATE，直到指定任务离开运行态。

    返回 True 表示任务「已完成」；False 表示失败/超时/查询异常。
    """
    print(f"\n… 等待任务完成: cmd_id={cmd_id}（每 {poll_s}s 查询 GET_TASK_STATE）")
    deadline = time.time() + timeout_s
    last_status = None

    while time.time() < deadline:
        req = {
            "cmd_id": f"poll-{cmd_id}-{int(time.time())}",
            "cmd_type": "GET_TASK_STATE",
            "params": {"target_cmd_id": cmd_id},
            "extra": {},
        }
        curl_ok, payload, raw = _http_post_json(req)
        if not curl_ok or payload is None:
            print(f"  ⚠ 状态查询失败，稍后重试: {raw[:200]}")
            time.sleep(poll_s)
            continue

        if not payload.get("success"):
            # 任务尚未登记等瞬时情况：继续等
            msg = payload.get("message", "")
            if last_status != msg:
                print(f"  · 查询未就绪: {msg}")
                last_status = msg
            time.sleep(poll_s)
            continue

        state = payload.get("data") or {}
        status = state.get("status", "")
        step = (state.get("current_step") or {}).get("name") or ""
        if status != last_status:
            print(f"  · status={status!r}  step={step!r}")
            last_status = status

        if status in _DONE_OK:
            print(f"✓ 任务已完成: {cmd_id}")
            return True
        if status in _DONE_FAIL:
            err = state.get("error_message") or payload.get("message") or status
            print(f"✗ 任务失败: {cmd_id} — {err}")
            return False
        if status not in _RUNNING and status not in ("未开始", ""):
            # 未知终态：保守当作失败
            print(f"✗ 未知任务状态: {status!r}，停止")
            return False

        time.sleep(poll_s)

    print(f"✗ 等待任务超时 ({timeout_s}s): {cmd_id}")
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="KAIAO 测试命令顺序下发（等任务完成再继续）")
    parser.add_argument(
        "--skip-start",
        action="store_true",
        help="跳过 START_WORKING（主程序已激活时使用）",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=POLL_INTERVAL_S,
        help=f"GET_TASK_STATE 轮询间隔秒（默认 {POLL_INTERVAL_S}）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=TASK_TIMEOUT_S,
        help=f"单任务最长等待秒（默认 {TASK_TIMEOUT_S}）",
    )
    args = parser.parse_args()

    commands = list(COMMANDS)
    if args.skip_start:
        commands = [c for c in commands if "START_WORKING" not in c]

    print(f"目标: {URL}")
    print(
        f"共 {len(commands)} 条命令；"
        f"仅 START_WORKING 后 input 一次；"
        f"业务命令等 GET_TASK_STATE 完成后再发下一条"
    )

    for i, rel in enumerate(commands, 1):
        ok, payload = post_command_file(rel)
        if not ok:
            print(f"\n✗ [{i}/{len(commands)}] 中止于: {rel}")
            sys.exit(1)

        is_start = "START_WORKING" in rel
        if is_start:
            if i < len(commands):
                try:
                    input(
                        f"\n[{i}/{len(commands)}] START_WORKING 已成功。"
                        f"确认机器人连接完成后按 Enter 发送下一条…"
                    )
                except (EOFError, KeyboardInterrupt):
                    print("\n已中断")
                    sys.exit(0)
            continue

        # 异步业务：受理成功后必须等任务真正结束
        cmd_id = (payload or {}).get("cmd_id") or _read_cmd_id_from_file(rel)
        if not cmd_id:
            print(f"✗ 无法确定 cmd_id，无法轮询任务状态: {rel}")
            sys.exit(1)

        if not wait_task_finished(cmd_id, poll_s=args.poll, timeout_s=args.timeout):
            print(f"\n✗ [{i}/{len(commands)}] 任务未成功完成，中止于: {rel}")
            sys.exit(1)

    print("\n✓ 全部命令已发送且任务均已完成")


if __name__ == "__main__":
    main()
