#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立测试本机到机器人 rosbridge 的连通性（不经过 main.py / RobotController）。

用法:
  python tools/test_rosbridge.py
  python tools/test_rosbridge.py --host 192.168.0.224 --port 9090
  python tools/test_rosbridge.py --host 192.168.217.100 --port 9090

成功时会打印 OK；失败时打印是 TCP 问题还是 WebSocket 握手问题。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import time


def _default_host_port():
    try:
        from infrastructure.config_loader import load_config
        robots = load_config(force_reload=True).get("robots") or {}
        for rid, cfg in robots.items():
            if cfg.get("enabled", True):
                return str(cfg.get("host", "127.0.0.1")), str(cfg.get("port", "9090"))
    except Exception:
        pass
    return "127.0.0.1", "9090"


def test_tcp(host: str, port: int, timeout: float = 5.0) -> bool:
    print(f"\n[1/3] TCP  {host}:{port}  (timeout={timeout}s)")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.time()
    try:
        s.connect((host, port))
        print(f"  ✓ TCP 连接成功  ({time.time()-t0:.2f}s)")
        return True
    except socket.timeout:
        print("  ✗ TCP 超时 — 网络不通 / 防火墙 / rosbridge 未监听")
        return False
    except ConnectionRefusedError:
        print(f"  ✗ TCP 被拒绝 — {port} 上没有服务")
        return False
    except OSError as e:
        print(f"  ✗ TCP 失败: {type(e).__name__}: {e}")
        return False
    finally:
        s.close()


async def _ws_once(uri: str, subprotocols=None, open_timeout: float = 8.0):
    import websockets

    kwargs = dict(open_timeout=open_timeout, close_timeout=3, ping_interval=None)
    if subprotocols:
        kwargs["subprotocols"] = list(subprotocols)
    async with websockets.connect(uri, **kwargs) as ws:
        sub = getattr(ws, "subprotocol", None)
        # 发一条无害的 rosbridge 探测（get_time；没有 rosapi 时也可能回 status）
        req = {
            "op": "call_service",
            "id": "test_rosbridge_1",
            "service": "/rosapi/get_time",
            "args": {},
        }
        await ws.send(json.dumps(req))
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        except asyncio.TimeoutError:
            return True, sub, "(无响应，但握手已成功 — rosbridge WebSocket 可用)"
        return True, sub, raw


async def test_websocket(host: str, port: int) -> bool:
    uri = f"ws://{host}:{port}/"
    print(f"\n[2/3] WebSocket 握手  {uri}")

    # 先试 rosbridge_v2，再试无子协议
    for label, subs in (
        ("subprotocol=rosbridge_v2", ["rosbridge_v2"]),
        ("无 subprotocol", None),
    ):
        print(f"  → 尝试 {label} ...")
        try:
            ok, sub, detail = await _ws_once(uri, subs)
            print(f"  ✓ 握手成功 ({label}, negotiated={sub!r})")
            print(f"  首包: {detail if isinstance(detail, str) and len(detail) < 200 else str(detail)[:200]}")
            return True
        except Exception as e:
            print(f"  ✗ 失败 ({label}): {type(e).__name__}: {e}")

    print("  ⇒ WebSocket 握手均失败 — 对端不像可用的 rosbridge，或主动断开升级请求")
    return False


async def test_subscribe_echo(host: str, port: int) -> bool:
    """握手成功后再试一次简单 subscribe（可选加固）。"""
    import websockets

    uri = f"ws://{host}:{port}/"
    print(f"\n[3/3] rosbridge subscribe 探测  /rosout")
    try:
        async with websockets.connect(
            uri,
            subprotocols=["rosbridge_v2"],
            open_timeout=8,
            close_timeout=3,
            ping_interval=None,
        ) as ws:
            await ws.send(json.dumps({
                "op": "subscribe",
                "id": "test_sub_1",
                "topic": "/rosout",
                "type": "rosgraph_msgs/Log",
            }))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                print(f"  ✓ 收到消息: {raw[:180]}...")
            except asyncio.TimeoutError:
                print("  ✓ subscribe 已发送，3s 内无推送（正常，说明连接仍保持）")
            return True
    except Exception as e:
        print(f"  ✗ subscribe 阶段失败: {type(e).__name__}: {e}")
        return False


def main():
    dh, dp = _default_host_port()
    p = argparse.ArgumentParser(description="独立测试 rosbridge WebSocket")
    p.add_argument("--host", default=dh)
    p.add_argument("--port", default=dp)
    args = p.parse_args()
    host = args.host
    port = int(args.port)

    print("=" * 60)
    print("rosbridge 独立连通性测试")
    print(f"目标: {host}:{port}")
    print("=" * 60)

    if not test_tcp(host, port):
        print("\n结论: TCP 不通 → 先查网络 / 机器人 IP / rosbridge 是否启动")
        print("  机器人上可查: ss -tlnp | grep 9090")
        return 2

    ws_ok = asyncio.run(test_websocket(host, port))
    if not ws_ok:
        print("\n结论: TCP 通但 WebSocket 握手失败 → rosbridge 异常或端口上不是 rosbridge")
        print("  与 main.py 里 InvalidMessage / 0 bytes 同类问题，不是 KAIAO 业务代码导致")
        return 3

    sub_ok = asyncio.run(test_subscribe_echo(host, port))
    print("\n" + "=" * 60)
    if sub_ok:
        print("结论: rosbridge 正常。若 main.py 仍连不上，多半是 RobotController 重连/事件循环问题")
        print("  （你看到的 TypeError: NoneType / Event loop stopped 属于这类）")
        return 0
    print("结论: 握手曾成功但后续失败 — rosbridge 可能不稳定，或连接数受限")
    return 4


if __name__ == "__main__":
    sys.exit(main())
