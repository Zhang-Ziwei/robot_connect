#!/usr/bin/env python3
"""
WRC 联调：按顺序 POST 测试命令。

1. START_WORKING
2. PROCESS_BEGINS
   - 若回传 success=false 且 code=4006（系统激活中），等待 2 秒后重发，直到成功或非 4006 失败
3. 成功后进入循环：按 Enter 发送 MANUAL_RESET_COMPLETED，可重复发送；输入 q 退出

用法（在程序根目录）:
    python run_wrc_test_commands.py
    python run_wrc_test_commands.py --skip-start
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

START_WORKING = "test_commands/START_WORKING_command.json"
PROCESS_BEGINS = "test_commands/PROCESS_BEGINS_command.json"
MANUAL_RESET = "test_commands/MANUAL_RESET_COMPLETED_command.json"

# 系统激活中 / 机器人尚未就绪，可重试
SYSTEM_ACTIVATING_CODE = 4006
ROBOT_NOT_FOUND_CODE = 2001
RETRYABLE_CODES = {SYSTEM_ACTIVATING_CODE, ROBOT_NOT_FOUND_CODE}
RETRY_WAIT_S = 2.0
MAX_BEGINS_RETRIES = 120  # 最多约 4 分钟


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


def _is_retryable_begins_error(payload: Optional[Dict[str, Any]]) -> bool:
    """PROCESS_BEGINS 可重试：系统激活中(4006) / 机器人尚不存在(2001)。"""
    if not isinstance(payload, dict) or payload.get("success"):
        return False
    code = payload.get("code")
    if code in RETRYABLE_CODES:
        return True
    msg = str(payload.get("message") or "")
    return "系统激活中" in msg or ("机器人" in msg and "不存在" in msg)


def post_command_file(rel_path: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """POST 一次命令文件，打印回传；不根据 success 决定是否重试。"""
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
    return True, payload


def post_until_success(
    rel_path: str,
    *,
    retry_on_activating: bool = False,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    POST 命令；若 retry_on_activating 且遇到 4006/2001，则等待后重发。
    """
    attempts = 0
    while True:
        attempts += 1
        ok, payload = post_command_file(rel_path)
        if not ok:
            return False, payload

        if _is_http_success(payload):
            print("✓ HTTP success=true")
            return True, payload

        if retry_on_activating and _is_retryable_begins_error(payload):
            if attempts >= MAX_BEGINS_RETRIES:
                print(f"✗ 等待超时（已重试 {attempts} 次）")
                return False, payload
            print(
                f"… 暂不可用 (code={payload.get('code')}, "
                f"message={payload.get('message')!r})，"
                f"{RETRY_WAIT_S}s 后重发（第 {attempts} 次）…"
            )
            time.sleep(RETRY_WAIT_S)
            continue

        print("✗ HTTP success!=true，停止")
        return False, payload


def main() -> None:
    parser = argparse.ArgumentParser(description="WRC：START_WORKING → PROCESS_BEGINS → MANUAL_RESET")
    parser.add_argument(
        "--skip-start",
        action="store_true",
        help="跳过 START_WORKING（主程序已激活时使用）",
    )
    args = parser.parse_args()

    print(f"目标: {URL}")
    print(
        "流程: "
        + ("START_WORKING → " if not args.skip_start else "")
        + "PROCESS_BEGINS(4006/2001 则重试) → 循环 MANUAL_RESET_COMPLETED"
    )

    if not args.skip_start:
        ok, _ = post_until_success(START_WORKING)
        if not ok:
            print(f"\n✗ 中止于: {START_WORKING}")
            sys.exit(1)

    ok, _ = post_until_success(PROCESS_BEGINS, retry_on_activating=True)
    if not ok:
        print(f"\n✗ 中止于: {PROCESS_BEGINS}")
        sys.exit(1)

    print(
        "\nPROCESS_BEGINS 已成功。"
        "按 Enter 发送 MANUAL_RESET_COMPLETED（可重复）；输入 q 后 Enter 退出。"
    )
    reset_count = 0
    while True:
        try:
            user_in = input(
                f"\n[{reset_count}] Enter=发送 MANUAL_RESET_COMPLETED，q=退出… "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出 MANUAL_RESET 循环")
            break

        if user_in in ("q", "quit", "exit"):
            print("已退出 MANUAL_RESET 循环")
            break

        ok, _ = post_until_success(MANUAL_RESET)
        if not ok:
            print(f"✗ MANUAL_RESET_COMPLETED 发送失败，可再次按 Enter 重试，或输入 q 退出")
            continue

        reset_count += 1
        print(f"✓ MANUAL_RESET_COMPLETED 已发送（第 {reset_count} 次）")

    print("\n✓ WRC 测试命令脚本结束")


if __name__ == "__main__":
    main()
