#!/usr/bin/env python3
"""
临时测试客户端：连 mock_chem_project_action_server，跑三种 task，验证 action 协议。
"""
import asyncio, json, uuid, time, sys
import websockets
URI = "ws://127.0.0.1:19090/"
ACTION_BASE = "/robot_task"
def now():
    t = time.time()
    secs = int(t)
    return {"secs": secs, "nsecs": int((t - secs) * 1e9)}
async def subscribe(ws, topic, msg_type):
    await ws.send(json.dumps({
        "op": "subscribe", "topic": topic, "type": msg_type,
        "throttle_rate": 0, "queue_length": 1,
    }))
async def publish_goal(ws, task, area="area_1", extra_params=""):
    goal_id = f"test-{uuid.uuid4().hex[:8]}"
    msg = {
        "op": "publish",
        "topic": f"{ACTION_BASE}/goal",
        "msg": {
            "header": {"stamp": now(), "frame_id": ""},
            "goal_id": {"stamp": now(), "id": goal_id},
            "goal": {
                "robot_task_types": {"type": 0},
                "task": task,
                "area": area,
                "extra_params": extra_params,
            },
        },
    }
    await ws.send(json.dumps(msg))
    return goal_id
async def publish_cancel(ws, goal_id):
    await ws.send(json.dumps({
        "op": "publish",
        "topic": f"{ACTION_BASE}/cancel",
        "msg": {"stamp": now(), "id": goal_id},
    }))
async def drive_case(case_name, task_value, timeout=10.0, cancel_after=None):
    print(f"\n=== case: {case_name} (task={task_value!r}) ===")
    async with websockets.connect(URI, subprotocols=["rosbridge_v2"]) as ws:
        # 订阅 feedback + result
        await subscribe(ws, f"{ACTION_BASE}/feedback",
                        "navi_types/RobotActionActionFeedback")
        await subscribe(ws, f"{ACTION_BASE}/result",
                        "navi_types/RobotActionActionResult")
        await asyncio.sleep(0.3)
        goal_id = await publish_goal(ws, task_value)
        print(f"  [client] 发 goal id={goal_id}")
        feedbacks = []
        result_payload = None
        deadline = time.time() + timeout
        cancelled = False
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.time())
            except asyncio.TimeoutError:
                break
            m = json.loads(raw)
            if m.get("op") != "publish":
                continue
            topic = m.get("topic", "")
            payload = m.get("msg", {})
            # 过滤 goal_id 不匹配的（status_list/feedback/result 都有 goal_id 字段）
            payload_gid = (((payload.get("status") or {}).get("goal_id") or {}).get("id")
                           or ((payload.get("goal_id") or {}).get("id")))
            if payload_gid and payload_gid != goal_id:
                continue
            if topic.endswith("/feedback"):
                fb = payload.get("feedback", {})
                feedbacks.append(fb)
                print(f"  [client] ⬅ feedback: status={fb.get('status')!r} "
                      f"current_params={fb.get('current_params')!r}")
                if cancel_after is not None and len(feedbacks) >= cancel_after and not cancelled:
                    await publish_cancel(ws, goal_id)
                    cancelled = True
                    print(f"  [client] → 发 cancel goal={goal_id}")
            elif topic.endswith("/result"):
                result_payload = payload.get("result", {})
                print(f"  [client] ⬅ RESULT: success={result_payload.get('success')} "
                      f"error_msg={result_payload.get('error_msg')!r} "
                      f"return_params={result_payload.get('return_params')!r}")
                break
        return feedbacks, result_payload
async def main():
    # case 1: 默认多步成功
    fb, r = await drive_case("default-success", "anything", timeout=8)
    assert r is not None, "必须收到 result"
    assert r.get("success") is True, f"应该成功, r={r}"
    assert len(fb) >= 3, f"应该至少 3 条 feedback, got {len(fb)}"
    # case 2: 立即成功（无 feedback step）
    fb, r = await drive_case("success_fast", "success_fast", timeout=5)
    assert r is not None and r.get("success") is True
    # 允许 FINISHED 那条 feedback，但 step_ 开头的应该 0 条
    step_fbs = [x for x in fb if "step_" in (x.get("status") or "")]
    assert len(step_fbs) == 0, f"success_fast 不应有 step_* feedback, got {step_fbs}"
    # case 3: 失败
    fb, r = await drive_case("fail", "fail", timeout=5)
    assert r is not None and r.get("success") is False
    assert "fail" in r.get("error_msg", ""), f"error_msg 应包含 fail: {r}"
    # case 4: 步进失败
    fb, r = await drive_case("fail_with_steps", "fail_with_steps", timeout=8)
    assert r is not None and r.get("success") is False
    assert len([x for x in fb if "step_" in (x.get("status") or "")]) >= 1
    # case 5: 取消长任务
    fb, r = await drive_case("cancel-long", "long", timeout=15, cancel_after=2)
    assert r is not None and r.get("success") is False
    assert "cancel" in r.get("error_msg", "").lower()
    print("\n✅ 全部 case 通过")
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\n❌ 断言失败: {e}")
        sys.exit(1)
