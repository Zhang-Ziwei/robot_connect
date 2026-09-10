"""KAIAO 走廊导航参数：图形化配置读写与校验。"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from programs.KAIAO.constants import (
    KAIAO_WAYPOINT_FIELDS,
    merge_kaiao_waypoint_config,
)
from network.flow_api_server import (
    _validate_waypoint_values,
    _read_waypoint_config,
    _write_waypoint_config,
)


def _kaiao_flow_adapter(config_dir: str):
    return {
        "poses_project": "KAIAO",
        "poses_config_dir": config_dir,
        "waypoint_config_key": "kaiao_waypoint",
    }


class TestKaiaoWaypointConfigApi(unittest.TestCase):
    def test_schema_covers_runtime_keys(self):
        keys = {f["key"] for f in KAIAO_WAYPOINT_FIELDS}
        self.assertIn("side_to_end_agv_clear_dy", keys)
        self.assertIn("side_to_side_approach_dx", keys)
        merged = merge_kaiao_waypoint_config({"_comment": "x", "side_to_end_agv_clear_dy": 0.5})
        self.assertEqual(merged["side_to_end_agv_clear_dy"], 0.5)
        self.assertNotIn("_comment", merged)
        self.assertEqual(merged["side_to_side_same_retreat_dx"], 0.3)

    def test_validate_rejects_bad_types(self):
        errors = _validate_waypoint_values(
            {"side_to_end_agv_clear_dy": "0.4", "side_to_end_agv_clear_from": "shelf0_3"},
            KAIAO_WAYPOINT_FIELDS,
        )
        self.assertTrue(any("side_to_end_agv_clear_dy" in e for e in errors))
        self.assertTrue(any("side_to_end_agv_clear_from" in e for e in errors))

    def test_write_then_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "robot_config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"active_project": "KAIAO", "robots": {}}, f)
            adapter = _kaiao_flow_adapter(tmp)
            with patch("network.flow_api_server._EXTERNAL_ROBOT_CONFIG", os.path.join(tmp, "missing.json")):
                saved = _write_waypoint_config(adapter, "KAIAO_FLOW", {
                    "side_to_end_agv_clear_dy": 0.55,
                    "side_to_end_agv_clear_from": ["shelf0_3"],
                })
                self.assertEqual(saved, path)
                info = _read_waypoint_config(adapter, "KAIAO_FLOW")
            self.assertEqual(info["values"]["side_to_end_agv_clear_dy"], 0.55)
            self.assertEqual(info["values"]["side_to_end_agv_clear_from"], ["shelf0_3"])
            self.assertTrue(any(f.get("comment") for f in info["schema"]))
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            self.assertIn("_comments", cfg["kaiao_waypoint"])
            self.assertEqual(cfg["active_project"], "KAIAO")


if __name__ == "__main__":
    unittest.main()
