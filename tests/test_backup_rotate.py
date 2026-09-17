"""编辑器保存时的备份：写入 .backups/ 且只保留最近 10 份。"""

import os
import tempfile
import unittest

from network.flow_api_server import (
    _BACKUP_DIR_NAME,
    _BACKUP_KEEP,
    _backup_existing_file,
    _prune_backups,
)


class TestBackupRotate(unittest.TestCase):
    def test_writes_into_backups_folder_and_keeps_ten(self):
        self.assertEqual(_BACKUP_KEEP, 10)
        self.assertEqual(_BACKUP_DIR_NAME, ".backups")
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "robot_config.json")
            with open(src, "w", encoding="utf-8") as f:
                f.write("v0")
            bak_dir = os.path.join(tmp, ".backups")
            os.makedirs(bak_dir)
            stamps = ["20260831_%06d" % (150000 + i) for i in range(12)]
            for stamp in stamps:
                with open(os.path.join(bak_dir, "robot_config.json.bak." + stamp), "w") as f:
                    f.write(stamp)
            removed = _prune_backups(src, keep=10)
            left = sorted(os.listdir(bak_dir))
            self.assertEqual(len(removed), 2)
            self.assertEqual(len(left), 10)
            self.assertNotIn("robot_config.json.bak.20260831_150000", left)
            self.assertNotIn("robot_config.json.bak.20260831_150001", left)

    def test_new_backup_goes_to_folder_not_beside_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "flow.json")
            with open(src, "w", encoding="utf-8") as f:
                f.write("new")
            path = _backup_existing_file(src, "test backup")
            self.assertIsNotNone(path)
            self.assertIn(os.sep + ".backups" + os.sep, path)
            beside = [n for n in os.listdir(tmp) if ".bak." in n]
            self.assertEqual(beside, [])
            left = os.listdir(os.path.join(tmp, ".backups"))
            self.assertEqual(len(left), 1)

    def test_old_sidecar_baks_are_counted_and_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "flow.json")
            with open(src, "w", encoding="utf-8") as f:
                f.write("new")
            for i in range(10):
                with open(os.path.join(tmp, "flow.json.bak.20260910_12000%d" % i), "w") as f:
                    f.write(str(i))
            _backup_existing_file(src, "test backup")
            bak_dir = os.path.join(tmp, ".backups")
            sidecar = [n for n in os.listdir(tmp) if n.startswith("flow.json.bak.")]
            folder = os.listdir(bak_dir)
            self.assertEqual(len(sidecar) + len(folder), 10)
            self.assertNotIn("flow.json.bak.20260910_120000", sidecar)


if __name__ == "__main__":
    unittest.main()
