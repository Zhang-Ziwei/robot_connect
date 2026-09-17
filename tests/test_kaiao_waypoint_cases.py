"""KAIAO 走廊中间点：近 AGV 货架持箱避让、货架互搬有箱/无箱。"""

import math
import unittest
from unittest.mock import patch

from hardware.navigation_utils import NavigationResult, NavigationState
from core.task_state_machine import TaskStateMachine
from programs.KAIAO.constants import get_nav_pose
from programs.KAIAO.KAIAO import KAIAOHandler, _first_xyqw, _yaw_from_quat


def _pose7(area: str):
    p = _first_xyqw(get_nav_pose(area))
    assert p is not None, area
    x, y, qz, qw = p
    return (x, y, 0.0, 0.0, 0.0, qz, qw)


def _odom_at(area: str):
    x, y, _z, _qx, _qy, qz, qw = _pose7(area)
    return {
        "Position_Point_X_In": x,
        "Position_Point_Y_In": y,
        "Orientation_Z_In": qz,
        "Orientation_W_In": qw,
    }


def _wp_cfg():
    return {
        "angle_threshold_deg": 45.0,
        "end_yaw_half_width_deg": 45.0,
        "angle_offset_deg": 90.0,
        "end_to_side_dx": 0.1,
        "end_to_side_dy": 0.0,
        "end_to_side_agv_extra_dy": 0.2,
        "end_to_side_agv_from": ["agv_car0_0"],
        "end_to_side_agv_to": ["shelf0_3", "shelf1_3"],
        "side_to_end_dx": 0.4,
        "side_to_end_dy": 0.0,
        "side_to_end_agv_clear_dy": 0.4,
        "side_to_end_agv_clear_from": ["shelf0_3", "shelf1_3"],
        "side_to_end_agv_clear_to": ["agv_car0_0"],
        "pose_match_radius": 0.28,
        "side_to_side_same_retreat_dx": 0.3,
        "side_to_side_approach_dx": 0.4,
    }


class TestKaiaoWaypointCases(unittest.TestCase):
    def setUp(self):
        self.handler = KAIAOHandler(
            robots={},
            task_state_machine=TaskStateMachine(),
            callback_sender=object(),
        )

    def _prepend_from_odom(self, odom, tgt_wp, holding: bool):
        self.handler._holding_box["robot_a"] = holding
        with patch.object(self.handler, "_get_waypoint_config", return_value=_wp_cfg()), patch(
            "programs.KAIAO.KAIAO.get_robot_odom",
            return_value=odom,
        ):
            return self.handler._prepend_intermediate_waypoint(
                robot=object(),
                waypoints=[tgt_wp],
                robot_id="robot_a",
            )

    def _prepend(self, src_area: str, tgt_area: str, holding: bool):
        return self._prepend_from_odom(_odom_at(src_area), _pose7(tgt_area), holding)

    def test_shelf03_holding_to_agv_clears_away_then_rotates(self):
        wps, case = self._prepend("shelf0_3", "agv_car0_0", holding=True)
        self.assertEqual(case, "side_to_end_clear")
        self.assertEqual(len(wps), 4)
        retreat, shift, rot, tgt = wps
        src = _pose7("shelf0_3")
        self.assertGreater(retreat[0], src[0])  # yaw=180° 后退增大 x
        self.assertAlmostEqual(shift[0], retreat[0], places=4)
        self.assertAlmostEqual(shift[1] - retreat[1], 0.4, places=4)
        self.assertAlmostEqual(rot[0], shift[0], places=4)
        self.assertAlmostEqual(rot[1], shift[1], places=4)
        rot_yaw = _yaw_from_quat(rot[5], rot[6])
        self.assertAlmostEqual(math.sin(rot_yaw), -1.0, delta=0.15)  # 朝向 AGV（-Y）

    def test_shelf13_holding_to_agv_clears(self):
        wps, case = self._prepend("shelf1_3", "agv_car0_0", holding=True)
        self.assertEqual(case, "side_to_end_clear")
        retreat, shift, _rot, _tgt = wps[0], wps[1], wps[2], wps[3]
        src = _pose7("shelf1_3")
        self.assertLess(retreat[0], src[0])  # yaw=0 后退减小 x
        self.assertAlmostEqual(shift[1] - retreat[1], 0.4, places=4)

    def test_shelf03_empty_to_agv_keeps_old_side_to_end(self):
        _wps, case = self._prepend("shelf0_3", "agv_car0_0", holding=False)
        self.assertEqual(case, "side_to_end")

    def test_other_shelf_holding_to_agv_no_clear(self):
        _wps, case = self._prepend("shelf0_0", "agv_car0_0", holding=True)
        self.assertEqual(case, "side_to_end")

    def test_shelf_to_shelf_holding_approaches_in_front(self):
        wps, case = self._prepend("shelf0_0", "shelf1_1", holding=True)
        self.assertEqual(case, "side_to_side_carry")
        self.assertEqual(len(wps), 5)
        retreat, rot_cc, approach, rot_tgt, tgt = wps
        src = _pose7("shelf0_0")
        self.assertGreater(retreat[0], src[0])
        cc_yaw = _yaw_from_quat(rot_cc[5], rot_cc[6])
        self.assertAlmostEqual(math.sin(cc_yaw), 1.0, delta=0.15)  # 零件车 +Y
        tgt_yaw = _yaw_from_quat(tgt[5], tgt[6])
        self.assertAlmostEqual(approach[0], tgt[0] - math.cos(tgt_yaw) * 0.4, places=4)
        self.assertAlmostEqual(approach[1], tgt[1] - math.sin(tgt_yaw) * 0.4, places=4)
        self.assertAlmostEqual(rot_tgt[0], approach[0], places=4)
        self.assertAlmostEqual(rot_tgt[1], approach[1], places=4)
        self.assertAlmostEqual(_yaw_from_quat(rot_tgt[5], rot_tgt[6]), tgt_yaw, places=4)

    def test_shelf_to_shelf_empty_retreat_then_target(self):
        wps, case = self._prepend("shelf0_0", "shelf1_1", holding=False)
        self.assertEqual(case, "side_to_side_empty")
        self.assertEqual(len(wps), 2)
        retreat, tgt = wps
        src = _pose7("shelf0_0")
        self.assertGreater(retreat[0], src[0])
        self.assertEqual(tgt, _pose7("shelf1_1"))

    def test_same_shelf_holding_retreat_translate_forward(self):
        wps, case = self._prepend("shelf0_3", "shelf0_0", holding=True)
        self.assertEqual(case, "side_to_side_same")
        self.assertEqual(len(wps), 3)
        retreat, align, tgt = wps
        src = _pose7("shelf0_3")
        dst = _pose7("shelf0_0")
        self.assertGreater(retreat[0], src[0])
        self.assertAlmostEqual(retreat[1], src[1], places=4)
        self.assertAlmostEqual(_yaw_from_quat(retreat[5], retreat[6]), _yaw_from_quat(src[5], src[6]), places=4)
        self.assertAlmostEqual(align[0], retreat[0], places=4)
        self.assertAlmostEqual(align[1], dst[1], places=4)
        self.assertAlmostEqual(_yaw_from_quat(align[5], align[6]), _yaw_from_quat(src[5], src[6]), places=4)
        self.assertEqual(tgt, dst)

    def test_same_cell_layer_change_retreat_then_forward(self):
        wps, case = self._prepend("shelf0_3", "shelf0_3", holding=True)
        self.assertEqual(case, "side_to_side_same")
        self.assertEqual(len(wps), 3)
        retreat, align, tgt = wps
        src = _pose7("shelf0_3")
        self.assertGreater(retreat[0], src[0])
        self.assertAlmostEqual(align[0], retreat[0], places=4)
        self.assertAlmostEqual(align[1], src[1], places=4)
        self.assertEqual(tgt, src)

    def test_same_shelf_empty_goes_direct(self):
        _wps, case = self._prepend("shelf0_3", "shelf0_0", holding=False)
        self.assertEqual(case, "no_change")

    def test_same_shelf_ignores_yaw_noise(self):
        odom = _odom_at("shelf0_3")
        odom["Orientation_Z_In"] = 0.0
        odom["Orientation_W_In"] = 1.0
        _wps, case = self._prepend_from_odom(odom, _pose7("shelf0_0"), holding=True)
        self.assertEqual(case, "side_to_side_same")

    def test_opposite_shelf_uses_xy_not_yaw(self):
        src = _pose7("shelf0_3")
        tgt = list(_pose7("shelf1_1"))
        tgt[5], tgt[6] = src[5], src[6]
        _wps, case = self._prepend_from_odom(_odom_at("shelf0_3"), tuple(tgt), holding=True)
        self.assertEqual(case, "side_to_side_carry")

    def _assert_agv_to_col3(self, tgt_area: str, expect_cos_yaw: float):
        wps, case = self._prepend("agv_car0_0", tgt_area, holding=True)
        self.assertEqual(case, "end_to_side_agv")
        self.assertEqual(len(wps), 4)
        extra, rot, align, tgt = wps
        src = _pose7("agv_car0_0")
        dst = _pose7(tgt_area)
        dx = dst[0] - src[0]
        front_x = src[0] + ((-math.copysign(1.0, dx) * 0.1) if abs(dx) > 1e-6 else 0.0)
        front_y = dst[1]
        self.assertAlmostEqual(extra[0], front_x, places=4)
        self.assertAlmostEqual(extra[1], front_y + 0.2, places=4)
        self.assertAlmostEqual(_yaw_from_quat(extra[5], extra[6]), _yaw_from_quat(src[5], src[6]), places=4)
        self.assertAlmostEqual(rot[0], extra[0], places=4)
        self.assertAlmostEqual(rot[1], extra[1], places=4)
        self.assertAlmostEqual(math.cos(_yaw_from_quat(rot[5], rot[6])), expect_cos_yaw, delta=0.15)
        self.assertAlmostEqual(align[0], front_x, places=4)
        self.assertAlmostEqual(align[1], front_y, places=4)
        self.assertAlmostEqual(_yaw_from_quat(align[5], align[6]), _yaw_from_quat(rot[5], rot[6]), places=4)
        self.assertEqual(tgt, dst)

    def test_agv_to_shelf03_extra_retreat_then_shift_back(self):
        self._assert_agv_to_col3("shelf0_3", expect_cos_yaw=-1.0)

    def test_agv_to_shelf13_extra_retreat_then_shift_back(self):
        self._assert_agv_to_col3("shelf1_3", expect_cos_yaw=1.0)

    def test_agv_to_shelf03_empty_also_uses_extra_path(self):
        _wps, case = self._prepend("agv_car0_0", "shelf0_3", holding=False)
        self.assertEqual(case, "end_to_side_agv")

    def test_agv_to_other_shelf_keeps_end_to_side(self):
        _wps, case = self._prepend("agv_car0_0", "shelf0_0", holding=True)
        self.assertEqual(case, "end_to_side")


class TestKaiaoSplitNavigateMultiMid(unittest.TestCase):
    def test_side_to_end_clear_sends_each_mid_then_target(self):
        handler = KAIAOHandler(
            robots={},
            task_state_machine=TaskStateMachine(),
            callback_sender=object(),
        )
        mids = [
            (0.1, 0.2, 0.0, 0.0, 0.0, 1.0, 0.0),
            (0.1, 0.6, 0.0, 0.0, 0.0, 1.0, 0.0),
            (0.1, 0.6, 0.0, 0.0, 0.0, -0.71, 0.7),
        ]
        target = ( -0.23, -1.51, 0.0, 0.0, 0.0, -0.71, 0.7)
        sends = []
        leave = {"n": 0}

        def fake_send(robot_arg, goal, feedback_callback=None, **kwargs):
            sends.append(goal)
            return NavigationResult(state=NavigationState.SUCCEEDED)

        with patch.object(
            handler,
            "_prepend_intermediate_waypoint",
            return_value=(mids + [target], "side_to_end_clear"),
        ), patch(
            "programs.KAIAO.KAIAO.send_navigation_action",
            side_effect=fake_send,
        ):
            ok = handler._navigate(
                object(),
                area_name="agv_car0_0",
                label="agv_car",
                robot_id="robot_a",
                waypoints=[target],
                mid_send="split",
                after_leave_shelf=lambda: leave.__setitem__("n", leave["n"] + 1) or True,
            )

        self.assertTrue(ok)
        self.assertEqual(len(sends), 4)
        self.assertEqual(leave["n"], 1)

    def test_end_to_side_agv_sends_each_mid_then_target(self):
        handler = KAIAOHandler(
            robots={},
            task_state_machine=TaskStateMachine(),
            callback_sender=object(),
        )
        mids = [
            (0.1, 0.15, 0.0, 0.0, 0.0, -0.71, 0.7),
            (0.1, 0.15, 0.0, 0.0, 0.0, 1.0, 0.0),
            (0.1, -0.05, 0.0, 0.0, 0.0, 1.0, 0.0),
        ]
        target = (-0.82, -0.05, 0.0, 0.0, 0.0, 1.0, 0.0)
        sends = []
        leave = {"n": 0}

        def fake_send(robot_arg, goal, feedback_callback=None, **kwargs):
            sends.append(goal)
            return NavigationResult(state=NavigationState.SUCCEEDED)

        with patch.object(
            handler,
            "_prepend_intermediate_waypoint",
            return_value=(mids + [target], "end_to_side_agv"),
        ), patch(
            "programs.KAIAO.KAIAO.send_navigation_action",
            side_effect=fake_send,
        ):
            ok = handler._navigate(
                object(),
                area_name="shelf0_3",
                label="shelf0_3",
                robot_id="robot_a",
                waypoints=[target],
                mid_send="split",
                after_leave_shelf=lambda: leave.__setitem__("n", leave["n"] + 1) or True,
            )

        self.assertTrue(ok)
        self.assertEqual(len(sends), 4)
        self.assertEqual(leave["n"], 1)

    def test_side_to_side_same_sends_retreat_shift_then_target(self):
        handler = KAIAOHandler(
            robots={},
            task_state_machine=TaskStateMachine(),
            callback_sender=object(),
        )
        mids = [
            (-0.52, -0.05, 0.0, 0.0, 0.0, 1.0, 0.0),
            (-0.52, 1.32, 0.0, 0.0, 0.0, 1.0, 0.0),
        ]
        target = (-0.82, 1.32, 0.0, 0.0, 0.0, 1.0, 0.0)
        sends = []
        leave = {"n": 0}

        def fake_send(robot_arg, goal, feedback_callback=None, **kwargs):
            sends.append(goal)
            return NavigationResult(state=NavigationState.SUCCEEDED)

        with patch.object(
            handler,
            "_prepend_intermediate_waypoint",
            return_value=(mids + [target], "side_to_side_same"),
        ), patch(
            "programs.KAIAO.KAIAO.send_navigation_action",
            side_effect=fake_send,
        ):
            ok = handler._navigate(
                object(),
                area_name="shelf0_0",
                label="shelf0_0",
                robot_id="robot_a",
                waypoints=[target],
                mid_send="split",
                after_leave_shelf=lambda: leave.__setitem__("n", leave["n"] + 1) or True,
            )

        self.assertTrue(ok)
        self.assertEqual(len(sends), 3)
        self.assertEqual(leave["n"], 1)


if __name__ == "__main__":
    unittest.main()
