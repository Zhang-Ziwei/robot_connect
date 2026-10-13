"""口令登录、账号密封包、/config 整份覆盖。"""

import json
import os
import sys
import tempfile
import unittest

import infrastructure.auth as auth

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "PACK_DELETE"))

import make_auth_pack as make_pack  # noqa: E402


class TestAuthLogin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = make_pack.generate_keypair(512)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.factory = os.path.join(self.tmp.name, "factory.pack")
        self.override = os.path.join(self.tmp.name, "override.pack")
        self.pub = os.path.join(self.tmp.name, "pub.json")
        self._orig = (
            auth.AUTH_PACK_FACTORY, auth.AUTH_PACK_OVERRIDE, auth.AUTH_PUB_PATH,
        )
        auth.AUTH_PACK_FACTORY = self.factory
        auth.AUTH_PACK_OVERRIDE = self.override
        auth.AUTH_PUB_PATH = self.pub
        self._write_pub()
        auth._config_cache = None
        auth._config_mtime = None
        auth._pack_source = "none"
        with auth._sessions_lock:
            auth._sessions.clear()

    def tearDown(self):
        (auth.AUTH_PACK_FACTORY,
         auth.AUTH_PACK_OVERRIDE,
         auth.AUTH_PUB_PATH) = self._orig
        auth._config_cache = None
        auth._config_mtime = None
        auth._pack_source = "none"
        with auth._sessions_lock:
            auth._sessions.clear()
        self.tmp.cleanup()

    def _write_pub(self):
        with open(self.pub, "w", encoding="utf-8") as f:
            json.dump({"n": self.key["n"], "e": self.key["e"]}, f)

    def _install(self, src, dest=None):
        payload = make_pack.payload_from_plain(src)
        make_pack.write_pack(payload, self.key, dest or self.factory)
        auth._config_cache = None
        auth._config_mtime = None

    def test_developer_object_still_works(self):
        self._install({
            "keys": {
                "developer": {
                    "password": "one-key",
                    "allowed_projects": ["KAIAO_FLOW"],
                }
            },
            "users": [],
        })
        self.assertEqual(auth.login_mode(), "key")
        p = auth.authenticate_by_key("one-key")
        self.assertIsNotNone(p)
        self.assertEqual(p.role, auth.Role.DEVELOPER)
        self.assertEqual(p.allowed_projects, ["KAIAO_FLOW"])
        self.assertFalse(p.is_anonymous)

    def test_multiple_developer_keys_map_to_projects(self):
        self._install({
            "keys": {
                "maintainer": {"password": "root-key"},
                "developer": [
                    {
                        "name": "kaiao",
                        "password": "kaiao-key",
                        "allowed_projects": ["KAIAO_FLOW"],
                    },
                    {
                        "name": "const",
                        "password": "const-key",
                        "allowed_projects": ["CONST_FLOW"],
                    },
                ],
            },
            "users": [],
        })
        self.assertEqual(auth.login_mode(), "key")

        kaiao = auth.authenticate_by_key("kaiao-key")
        self.assertEqual(kaiao.name, "kaiao")
        self.assertTrue(kaiao.can_edit_project("KAIAO_FLOW"))
        self.assertFalse(kaiao.can_edit_project("CONST_FLOW"))

        const = auth.authenticate_by_key("const-key")
        self.assertEqual(const.name, "const")
        self.assertTrue(const.can_edit_project("CONST_FLOW"))

        maintainer = auth.authenticate_by_key("root-key")
        self.assertEqual(maintainer.role, auth.Role.MAINTAINER)
        self.assertIsNone(auth.authenticate_by_key("wrong"))

    def test_both_login_modes_when_keys_and_users_exist(self):
        self._install({
            "keys": {
                "developer": [
                    {
                        "name": "kaiao",
                        "password": "kaiao-key",
                        "allowed_projects": ["KAIAO_FLOW"],
                    }
                ]
            },
            "users": [
                {
                    "name": "zhang",
                    "role": "developer",
                    "password": "zhang-pass",
                    "allowed_projects": ["KAIAO_FLOW"],
                }
            ],
        })
        self.assertEqual(auth.login_mode(), "both")
        self.assertEqual(auth.authenticate_by_key("kaiao-key").name, "kaiao")
        self.assertEqual(auth.authenticate("zhang", "zhang-pass").name, "zhang")
        self.assertIsNone(auth.authenticate_by_key("zhang-pass"))
        self.assertIsNone(auth.authenticate("zhang", "kaiao-key"))

        token = auth.login("zhang", "zhang-pass")
        self.assertEqual(auth.principal_for_token(token).name, "zhang")
        self.assertEqual(auth.principal_for_token(auth.login("", "kaiao-key")).name, "kaiao")

    def test_user_only_login_mode(self):
        self._install({
            "keys": {},
            "users": [
                {"name": "li", "role": "maintainer", "password": "li-pass"},
            ],
        })
        self.assertEqual(auth.login_mode(), "user")
        self.assertIsNone(auth.authenticate_by_key("li-pass"))
        self.assertEqual(auth.authenticate("li", "li-pass").role, auth.Role.MAINTAINER)

    def test_disabled_user_cannot_login(self):
        self._install({
            "keys": {
                "developer": [
                    {"name": "kaiao", "password": "kaiao-key",
                     "allowed_projects": ["KAIAO_FLOW"]}
                ]
            },
            "users": [
                {
                    "name": "old",
                    "role": "developer",
                    "password": "old-pass",
                    "enabled": False,
                    "allowed_projects": ["KAIAO_FLOW"],
                }
            ],
        })
        self.assertIsNone(auth.authenticate("old", "old-pass"))
        self.assertEqual(auth.login_mode(), "key")

    def test_plaintext_is_hashed_out_of_the_pack(self):
        self._install({
            "keys": {
                "developer": [
                    {"name": "kaiao", "password": "plain-a",
                     "allowed_projects": ["KAIAO_FLOW"]},
                ]
            },
            "users": [
                {"name": "zhang", "role": "developer", "password": "plain-u",
                 "allowed_projects": ["KAIAO_FLOW"]},
            ],
        })
        raw = open(self.factory, encoding="utf-8").read()
        self.assertNotIn("plain-a", raw)
        self.assertNotIn("plain-u", raw)
        cfg = auth.load_auth_config()
        self.assertNotIn("password", cfg["keys"]["developer"][0])
        self.assertTrue(cfg["keys"]["developer"][0]["password_hash"].startswith("pbkdf2_sha256$"))
        self.assertEqual(auth.authenticate_by_key("plain-a").name, "kaiao")
        self.assertEqual(auth.authenticate("zhang", "plain-u").name, "zhang")

    def test_override_pack_wins_and_bad_override_falls_back(self):
        self._install({
            "keys": {"maintainer": {"password": "factory-key"}},
            "users": [],
        }, self.factory)
        self._install({
            "keys": {"maintainer": {"password": "site-key"}},
            "users": [],
        }, self.override)
        self.assertIsNotNone(auth.authenticate_by_key("site-key"))
        self.assertIsNone(auth.authenticate_by_key("factory-key"))
        self.assertEqual(auth._pack_source, "override")

        with open(self.override, "w", encoding="utf-8") as f:
            f.write('{"v":1,"salt":"00","box":"00","sig":"00"}\n')
        auth._config_cache = None
        auth._config_mtime = None
        self.assertIsNotNone(auth.authenticate_by_key("factory-key"))
        self.assertIsNone(auth.authenticate_by_key("site-key"))
        self.assertEqual(auth._pack_source, "factory")

    def test_forged_pack_without_private_key_is_rejected(self):
        other = make_pack.generate_keypair(512)
        payload = make_pack.payload_from_plain({
            "keys": {"maintainer": {"password": "forged"}},
            "users": [],
        })
        make_pack.write_pack(payload, other, self.override)
        self._install({
            "keys": {"maintainer": {"password": "factory-key"}},
            "users": [],
        }, self.factory)
        self.assertIsNone(auth.authenticate_by_key("forged"))
        self.assertIsNotNone(auth.authenticate_by_key("factory-key"))


if __name__ == "__main__":
    unittest.main()
