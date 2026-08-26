"""KAIAO 中间点导航失败分类与重试逻辑单元测试。"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hardware.navigation_utils import NavigationResult, NavigationState
from core.task_state_machine import TaskStateMachine
from programs.KAIAO.KAIAO import (
    KAIAOHandler,
    _kaiao_feedback_stamp_secs,
    _is_out_of_tolerate,
    _is_planning_failed,
    _jitter_waypoint_xy,
    _MID_JITTER_RADIUS_M,
    _MID_PLANNING_MAX_RETRIES,
)


class TestNavFailureClassification(unittest.TestCase):
    def test_planning_failed_code_10009(self):
        res = NavigationResult(
            state=NavigationState.ABORTED,
            causes=[{"code": 10009, "msg": "Planning Failed"}],
        )
        self.assertTrue(_is_planning_failed(res))
        self.assertFalse(_is_out_of_tolerate(res))

    def test_out_of_tolerance_code_10008(self):
        res = NavigationResult(
            state=NavigationState.FAILED,
            causes=[{"code": 10008, "msg": "Arrival accuracy out of tolerance"}],
        )
        self.assertTrue(_is_out_of_tolerate(res))
        self.assertFalse(_is_planning_failed(res))

    def test_planning_from_err_msg_when_causes_empty(self):
        """ABORTED 时 causes 可能未写入 result，但 err_msg 带 causes=。"""
        res = NavigationResult(state=NavigationState.ABORTED, causes=[])
        err = (
            "[robot_a] shelf0_3 平移中间点 失败: ABORTED "
            "causes=[{'code': 10009, 'msg': 'Planning Failed'}]"
        )
        self.assertTrue(_is_planning_failed(res, err))
        self.assertFalse(_is_out_of_tolerate(res, err))

    def test_10009_not_treated_as_tolerate(self):
        """修复前 bug：10009 被误判为 out of tolerate。"""
        res = NavigationResult(
            causes=[{"code": 10009, "msg": "Planning Failed"}],
        )
        self.assertFalse(_is_out_of_tolerate(res))


class TestSendMidWaypointRetry(unittest.TestCase):
    def test_planning_retries_then_succeeds(self):
        """Planning Failed 应抖动重试，不应第一次就 skip。"""
        planning_res = NavigationResult(
            state=NavigationState.ABORTED,
            causes=[{"code": 10009, "msg": "Planning Failed"}],
        )
        planning_err = "fail ABORTED causes=[{'code': 10009, 'msg': 'Planning Failed'}]"
        ok_res = NavigationResult(state=NavigationState.SUCCEEDED)

        seq = [
            (planning_res, planning_err),
            (planning_res, planning_err),
            (ok_res, None),
        ]
        calls = {"wps": []}
        attempt = {"n": 0}

        def send_nav(wps, label=""):
            out = seq[attempt["n"]]
            attempt["n"] += 1
            calls["wps"].append(list(wps[0]))
            return out

        mid_wp = [1.0, 2.0, 0.0, 0.0, 0.0, 0.71, 0.71]
        original = list(mid_wp)
        send_wp = mid_wp
        status = None
        for att in range(_MID_PLANNING_MAX_RETRIES + 1):
            if att > 0:
                send_wp = _jitter_waypoint_xy(original, _MID_JITTER_RADIUS_M)
            last_res, last_err = send_nav([send_wp])
            if last_err is None:
                status = "success"
                break
            if _is_planning_failed(last_res, last_err):
                if att < _MID_PLANNING_MAX_RETRIES:
                    continue
                status = "skip"
                break
            if _is_out_of_tolerate(last_res, last_err):
                status = "skip"
                break
            status = "fail"
            break

        self.assertEqual(status, "success")
        self.assertEqual(len(calls["wps"]), 3)
        self.assertNotEqual(calls["wps"][0][:2], calls["wps"][1][:2])

    def test_tolerate_classified_correctly(self):
        tolerate_res = NavigationResult(
            state=NavigationState.FAILED,
            causes=[{"code": 10008, "msg": "Arrival accuracy out of tolerance"}],
        )
        tolerate_err = (
            "fail FAILED causes=[{'code': 10008, "
            "'msg': 'Arrival accuracy out of tolerance'}]"
        )
        self.assertTrue(_is_out_of_tolerate(tolerate_res, tolerate_err))
        self.assertFalse(_is_planning_failed(tolerate_res, tolerate_err))

    def test_tolerate_on_rotate_returns_skip(self):
        """端点→端点 旋转180° out of tolerance 应 skip，不应 fail。"""
        tolerate_res = NavigationResult(
            state=NavigationState.FAILED,
            causes=[{"code": 10008, "msg": "Arrival accuracy out of tolerance"}],
        )
        tolerate_err = (
            "[robot_a] agv_car0_0 旋转180° 失败: FAILED "
            "causes=[{'code': 10008, 'msg': 'Arrival accuracy out of tolerance'}]"
        )
        self.assertTrue(_is_out_of_tolerate(tolerate_res, tolerate_err))
        # 模拟 _send_mid_waypoint 单次失败后的分支
        status = "skip" if _is_out_of_tolerate(tolerate_res, tolerate_err) else "fail"
        self.assertEqual(status, "skip")


class TestNavigationResultBoolBug(unittest.TestCase):
    def test_failed_result_must_not_be_replaced_by_or(self):
        """
        回归：NavigationResult 失败时 bool=False，
        `last_result or NavigationResult(...)` 会丢掉 causes，
        导致 KAIAO 把 Planning Failed 误判为未知错误并中止。
        """
        failed = NavigationResult(
            state=NavigationState.ABORTED,
            causes=[{"code": 10009, "msg": "Planning Failed"}],
        )
        self.assertFalse(bool(failed))
        # 错误写法（修复前）
        broken = failed or NavigationResult(state=NavigationState.ABORTED)
        self.assertEqual(broken.causes, [])
        self.assertFalse(_is_planning_failed(broken))
        # 正确写法
        kept = failed if failed is not None else NavigationResult(state=NavigationState.ABORTED)
        self.assertEqual(kept.causes[0]["msg"], "Planning Failed")
        self.assertTrue(_is_planning_failed(kept))
        # 失败对象应走 skip 重试分支，而不是 fail/中止
        if _is_planning_failed(kept):
            status = "retry_or_skip"
        elif _is_out_of_tolerate(kept):
            status = "skip"
        else:
            status = "fail"
        self.assertEqual(status, "retry_or_skip")


class TestKaiaoCustomNavigationFeedback(unittest.TestCase):
    def test_extract_feedback_stamp_secs(self):
        feedback = SimpleNamespace(
            raw_feedback={
                "header": {
                    "stamp": {"secs": 1, "nsecs": 0},
                },
                "state": {"value": 2},
                "faults": [],
            },
        )
        self.assertEqual(_kaiao_feedback_stamp_secs(feedback), 1)

    def test_secs_one_cancels_mid_and_advances_to_target(self):
        """
        中间点 feedback stamp.secs=1：
        取消当前 goal，并继续发送目标点；不能中止整条导航。
        """
        handler = KAIAOHandler(
            robots={},
            task_state_machine=TaskStateMachine(),
            callback_sender=object(),
        )
        robot = object()
        mid = (0.1, 0.2, 0.0, 0.0, 0.0, 0.0, 1.0)
        target = (1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0)
        sends = []

        def fake_send_navigation_action(robot_arg, goal, feedback_callback, **kwargs):
            sends.append(goal)
            if len(sends) == 1:
                feedback_callback(SimpleNamespace(
                    state=NavigationState.RUNNING,
                    raw_feedback={
                        "header": {"stamp": {"secs": 1, "nsecs": 0}},
                        "state": {"value": 2},
                        "faults": [],
                    },
                ))
                return NavigationResult(state=NavigationState.CANCELLED)
            return NavigationResult(state=NavigationState.SUCCEEDED)

        with patch.object(
            handler,
            "_prepend_intermediate_waypoint",
            return_value=([mid, target], "side_to_side"),
        ), patch(
            "programs.KAIAO.KAIAO.send_navigation_action",
            side_effect=fake_send_navigation_action,
        ), patch(
            "programs.KAIAO.KAIAO.cancel_navigation_action",
            return_value=True,
        ) as cancel:
            ok = handler._navigate(
                robot,
                area_name="target",
                label="target",
                robot_id="robot_a",
                waypoints=[target],
                mid_send="split",
            )

        self.assertTrue(ok)
        self.assertEqual(len(sends), 2)
        cancel.assert_called_once_with(robot)


if __name__ == "__main__":
    unittest.main()
