"""
图形编辑器的角色权限。

三个角色，按现场的分工划分：

    operator   使用者    —— 看流程图、启停调度、改现场配置（机器人 IP / MQTT 地址）。
                            现场操作人员的日常工作，不需要登录
    developer  开发者    —— 加上：在**授权的项目**里改流程图/点位、跑演练、导入导出；
                            其他项目只读
    maintainer 维护者    —— 全部权限，且不受项目范围限制

设计上的两个要点：

**账号密封包单独读取，不走 config_loader，也不和 robot_config.json 合并。**
明文只在维护者本机；发给现场的是签名密封包 ``auth_accounts.pack``。
有 ``/config/auth_accounts.pack`` 且验签通过就用它（整份替换），否则用
``infrastructure/auth_accounts.pack`` 出厂包。验签失败当没有，退回出厂。
``/config/robot_config.json`` 里塞角色不会生效。

**匿名等于使用者。**
没带 token 的请求按 operator 处理，现场操作人员不用记密码；要改东西才登录提权。
所以校验的重点不是"有没有登录"，而是"这个身份能不能做这件事"。

注意这套机制防的是误操作和越权使用，不是防拿到 root 的人——能改容器内文件的人
总能绕过任何本地校验，那属于代码保护的范畴，是另一件事。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from infrastructure.error_logger import get_error_logger

logger = get_error_logger()
_LOG = "Auth"


# ──────────────────────────────────────────────────────────────────────────────
# 角色与权限
# ──────────────────────────────────────────────────────────────────────────────

class Role:
    OPERATOR = "operator"
    DEVELOPER = "developer"
    MAINTAINER = "maintainer"


ROLE_LABELS = {
    Role.OPERATOR: "使用者",
    Role.DEVELOPER: "开发者",
    Role.MAINTAINER: "维护者",
}


class Perm:
    """
    权限点。粒度按"操作后果"分，不按接口分——同一个后果的接口共用一个权限点，
    以后加接口时对着后果挑一个即可。
    """
    VIEW = "view"                          # 看流程图、点位、运行状态
    RUN_CONTROL = "run_control"            # 启动/暂停/停止调度
    DRYRUN = "dryrun"                      # 演练（会连 mock、起临时连接）
    EDIT_FLOW = "edit_flow"                # 改流程图、改激活开关
    EDIT_POSES = "edit_poses"              # 改导航点位
    #: 改 robot_config（机器人 IP、端口、MQTT 地址）。这是「运行控制」面板里的
    #: 「配置文件」，Docker 下写的是外部挂载的 /config/robot_config.json——
    #: 属于现场运维（换机器人、改网段就得动），和启停调度同性质，所以三个角色都有。
    #: 单独留一个权限点而不是并进 RUN_CONTROL，是为了以后想收紧时改一行就够。
    EDIT_ROBOT_CONFIG = "edit_robot_config"
    IMPORT_EXPORT = "import_export"        # 导入/导出流程包


#: 角色 → 权限集合。上层角色不自动继承，全部列出，便于一眼看全边界。
ROLE_PERMISSIONS: Dict[str, frozenset] = {
    Role.OPERATOR: frozenset({
        Perm.VIEW, Perm.RUN_CONTROL, Perm.EDIT_ROBOT_CONFIG,
    }),
    Role.DEVELOPER: frozenset({
        Perm.VIEW, Perm.RUN_CONTROL, Perm.EDIT_ROBOT_CONFIG, Perm.DRYRUN,
        Perm.EDIT_FLOW, Perm.EDIT_POSES, Perm.IMPORT_EXPORT,
    }),
    Role.MAINTAINER: frozenset({
        Perm.VIEW, Perm.RUN_CONTROL, Perm.DRYRUN,
        Perm.EDIT_FLOW, Perm.EDIT_POSES, Perm.IMPORT_EXPORT,
        Perm.EDIT_ROBOT_CONFIG,
    }),
}

#: 这些权限要额外看项目范围：开发者只能动自己被授权的项目。
PROJECT_SCOPED_PERMS = frozenset({
    Perm.EDIT_FLOW, Perm.EDIT_POSES, Perm.IMPORT_EXPORT,
})


# ──────────────────────────────────────────────────────────────────────────────
# 密码哈希（PBKDF2-HMAC-SHA256，标准库实现，不引入新依赖）
# ──────────────────────────────────────────────────────────────────────────────

_HASH_PREFIX = "pbkdf2_sha256"
_DEFAULT_ITERATIONS = 200_000


def hash_password(password: str, iterations: int = _DEFAULT_ITERATIONS) -> str:
    """生成 ``pbkdf2_sha256$迭代次数$盐$哈希``，由本机生成脚本写入密封包。"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations,
    ).hex()
    return f"{_HASH_PREFIX}${iterations}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令。用 compare_digest 比较，避免按字节比较带来的时序差异。"""
    if not stored or not isinstance(stored, str):
        return False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != _HASH_PREFIX:
        logger.warning(
            _LOG,
            "password_hash 格式不认识，这条口令无法登录。请在本机明文里重写 password 后重新生成密封包",
        )
        return False
    try:
        iterations = int(parts[1])
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"), parts[2].encode("utf-8"), iterations,
    ).hex()
    return hmac.compare_digest(digest, parts[3])


# 密封包：XOR 只是让打开文件看不见 JSON；改不了靠 RSA 验签（公钥在 auth_accounts.pub.json）。
_PACK_VERSION = 1
_PACK_OBFUSCATE_PEPPER = b"robot_connect.auth.pack.obfuscate.v1"
_PACK_OBFUSCATE_ITERATIONS = 80_000
_SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")

AUTH_PACK_NAME = "auth_accounts.pack"
AUTH_PACK_FACTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), AUTH_PACK_NAME)
AUTH_PACK_OVERRIDE = os.path.join("/config", AUTH_PACK_NAME)
AUTH_PUB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth_accounts.pub.json")


def _pkcs1_emsa_sha256(message: bytes, k: int) -> bytes:
    digest = hashlib.sha256(message).digest()
    t = _SHA256_DIGESTINFO + digest
    if k < len(t) + 11:
        raise ValueError("RSA 钥匙太短")
    ps = b"\xff" * (k - len(t) - 3)
    return b"\x00\x01" + ps + b"\x00" + t


def _rsa_public() -> Optional[Tuple[int, int]]:
    try:
        with open(AUTH_PUB_PATH, "r", encoding="utf-8") as f:
            pub = json.load(f)
        n = int(pub["n"], 16)
        e = int(pub["e"], 16)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if n <= 0 or e <= 0:
        return None
    return n, e


def rsa_verify(message: bytes, signature: bytes, n: int, e: int) -> bool:
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    sig_int = int.from_bytes(signature, "big")
    if sig_int >= n:
        return False
    em = pow(sig_int, e, n).to_bytes(k, "big")
    try:
        expected = _pkcs1_emsa_sha256(message, k)
    except ValueError:
        return False
    return hmac.compare_digest(em, expected)


def unwrap_auth_pack(raw: bytes) -> Optional[Dict[str, Any]]:
    """验签并解开密封包。失败返回 None（调用方改用下一份，不要放行）。"""
    pub = _rsa_public()
    if pub is None:
        logger.error(_LOG, f"读不到公钥 {AUTH_PUB_PATH}，无法核对账号密封包")
        return None
    n, e = pub
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(envelope, dict) or envelope.get("v") != _PACK_VERSION:
        return None
    try:
        salt = bytes.fromhex(envelope["salt"])
        box = bytes.fromhex(envelope["box"])
        sig = bytes.fromhex(envelope["sig"])
    except (KeyError, ValueError, TypeError):
        return None
    if not rsa_verify(salt + box, sig, n, e):
        return None
    if not box:
        return None
    key = hashlib.pbkdf2_hmac(
        "sha256", _PACK_OBFUSCATE_PEPPER, salt, _PACK_OBFUSCATE_ITERATIONS, dklen=len(box),
    )
    try:
        cfg = json.loads(bytes(a ^ b for a, b in zip(box, key)).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(cfg, dict):
        return None
    users = cfg.get("users")
    cfg["users"] = users if isinstance(users, list) else []
    keys = cfg.get("keys")
    cfg["keys"] = keys if isinstance(keys, dict) else {}
    if cfg.get("anonymous_role") not in ROLE_PERMISSIONS:
        cfg["anonymous_role"] = Role.OPERATOR
    return cfg


# ──────────────────────────────────────────────────────────────────────────────
# 权限配置
# ──────────────────────────────────────────────────────────────────────────────

_DEFAULT_SESSION_MINUTES = 480

_config_cache: Optional[Dict[str, Any]] = None
_config_mtime: Optional[Tuple[Any, ...]] = None
_pack_source: str = "none"
_config_lock = threading.Lock()


def _empty_auth_config() -> Dict[str, Any]:
    return {
        "users": [],
        "keys": {},
        "anonymous_role": Role.OPERATOR,
        "session_timeout_minutes": _DEFAULT_SESSION_MINUTES,
    }


def _read_pack_file(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    return unwrap_auth_pack(raw)


def _pack_stamp() -> Tuple[Any, ...]:
    stamp = []
    for path in (AUTH_PACK_OVERRIDE, AUTH_PACK_FACTORY, AUTH_PUB_PATH):
        try:
            stamp.append(os.path.getmtime(path))
        except OSError:
            stamp.append(None)
    return tuple(stamp)


def load_auth_config(force_reload: bool = False) -> Dict[str, Any]:
    """
    读账号密封包。优先 /config 覆盖，验签失败或没有则用出厂包。

    两份都没有或都验不过：权限视为未启用（与升级前「没配口令」相同）。
    """
    global _config_cache, _config_mtime, _pack_source
    with _config_lock:
        stamp = _pack_stamp()
        if _config_cache is not None and not force_reload and stamp == _config_mtime:
            return _config_cache

        cfg = None
        source = "none"
        if os.path.isfile(AUTH_PACK_OVERRIDE):
            cfg = _read_pack_file(AUTH_PACK_OVERRIDE)
            if cfg is not None:
                source = "override"
            else:
                logger.warning(
                    _LOG,
                    f"{AUTH_PACK_OVERRIDE} 无效（损坏或签名对不上），改用出厂密封包",
                )
        if cfg is None and os.path.isfile(AUTH_PACK_FACTORY):
            cfg = _read_pack_file(AUTH_PACK_FACTORY)
            if cfg is not None:
                source = "factory"
            else:
                logger.error(_LOG, f"{AUTH_PACK_FACTORY} 无效，权限管理按未启用处理")
        if cfg is None:
            cfg = _empty_auth_config()
            if not os.path.isfile(AUTH_PACK_FACTORY) and not os.path.isfile(AUTH_PACK_OVERRIDE):
                logger.info(_LOG, "未找到账号密封包，权限管理未启用（所有人拥有全部权限）")

        _config_cache = cfg
        _config_mtime = stamp
        _pack_source = source
        n_users = len(cfg.get("users") or [])
        logger.info(_LOG, f"已加载权限配置：来源={source}，{n_users} 个具名账号")
        return _config_cache


# ──────────────────────────────────────────────────────────────────────────────
# 身份
# ──────────────────────────────────────────────────────────────────────────────

class Principal:
    """一次请求的身份。匿名请求也有 Principal，角色是配置里的 anonymous_role。"""

    def __init__(self, name: str, role: str, allowed_projects: Optional[List[str]] = None):
        self.name = name
        self.role = role if role in ROLE_PERMISSIONS else Role.OPERATOR
        #: 空列表对开发者表示"一个项目都不能改"；维护者不看这个字段
        self.allowed_projects = list(allowed_projects or [])

    @property
    def is_anonymous(self) -> bool:
        return not self.name

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)

    def permissions(self) -> frozenset:
        return ROLE_PERMISSIONS.get(self.role, frozenset())

    def can(self, perm: str, project: Optional[str] = None) -> bool:
        """能不能做这件事。带 project 的话还要看项目范围。"""
        if perm not in self.permissions():
            return False
        if perm in PROJECT_SCOPED_PERMS and project:
            return self.can_edit_project(project)
        return True

    def can_edit_project(self, project: str) -> bool:
        """维护者不限项目；开发者只能动授权列表里的；使用者一个都不能改。"""
        if self.role == Role.MAINTAINER:
            return True
        if self.role != Role.DEVELOPER:
            return False
        return project in self.allowed_projects

    def describe(self) -> Dict[str, Any]:
        """给前端用：它据此隐藏/置灰按钮（真正的拦截在后端）。"""
        return {
            "name": self.name or "",
            "role": self.role,
            "role_label": self.role_label,
            "anonymous": self.is_anonymous,
            "permissions": sorted(self.permissions()),
            "allowed_projects": ([] if self.role == Role.MAINTAINER else self.allowed_projects),
            "all_projects": self.role == Role.MAINTAINER,
        }


def _spec_has_password(spec: Any) -> bool:
    return isinstance(spec, dict) and bool(spec.get("password_hash") or spec.get("password"))


def _role_key_specs(role: str) -> List[Dict[str, Any]]:
    """
    某个角色在 keys 里配了几条口令。

    开发者可以写成数组（多条口令、各自 allowed_projects）；维护者通常一条对象。
    两种写法都认。
    """
    keys = load_auth_config().get("keys")
    if not isinstance(keys, dict) or role not in ROLE_PERMISSIONS:
        return []
    raw = keys.get(role)
    if isinstance(raw, list):
        return [s for s in raw if _spec_has_password(s)]
    if _spec_has_password(raw):
        return [raw]
    return []


def _configured_keys() -> Dict[str, List[Dict[str, Any]]]:
    """keys 段里实际配了口令的角色 → 口令条目列表。"""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for role in (Role.MAINTAINER, Role.DEVELOPER, Role.OPERATOR):
        specs = _role_key_specs(role)
        if specs:
            out[role] = specs
    return out


def _configured_users(*, include_disabled: bool = True) -> List[Dict[str, Any]]:
    users = load_auth_config().get("users")
    if not isinstance(users, list):
        return []
    out = []
    for u in users:
        if not isinstance(u, dict) or not str(u.get("name") or "").strip():
            continue
        if not _spec_has_password(u):
            continue
        if not include_disabled and not u.get("enabled", True):
            continue
        out.append(u)
    return out


def _key_principal(role: str, spec: Dict[str, Any]) -> Principal:
    """口令登录没有账号名时，用条目上的 name，否则用角色名（开发者带上项目以免撞名）。"""
    label = ROLE_LABELS.get(role, role)
    given = str(spec.get("name") or "").strip()
    if given:
        display = given
    elif role == Role.DEVELOPER:
        projects = [str(x) for x in (spec.get("allowed_projects") or []) if x]
        display = f"{label}({','.join(projects)})" if projects else label
    else:
        display = label
    return Principal(
        name=display,
        role=role,
        allowed_projects=spec.get("allowed_projects"),
    )


def _spec_password_ok(spec: Dict[str, Any], password: str) -> bool:
    """
    核对一条配置里的口令。

    支持两种写法：``password`` 直接写明文（手改配置时方便），``password_hash`` 存哈希。
    两个都有时以哈希为准——改口令时只改了明文却忘了删旧哈希，不该新旧都能进。
    """
    stored = spec.get("password_hash")
    if stored:
        return verify_password(password, stored)
    plain = spec.get("password")
    if isinstance(plain, str) and plain:
        return hmac.compare_digest(password, plain)
    return False


def is_auth_enabled() -> bool:
    """
    是否启用了权限管理：配了口令（keys）或账号（users）任一即算启用。

    两者都没配时视为未启用，此时所有人按维护者处理——即升级前的行为。
    否则现有现场一升级会突然什么都改不了，而他们手上并没有口令。
    """
    return bool(_configured_keys()) or bool(_configured_users())


def login_mode() -> str:
    """
    登录界面该长什么样：

        "key"  只配了口令 —— 只要口令
        "user" 只配了账号 —— 账号 + 口令
        "both" 两种都配了 —— 登录框里切换
        "off"  没启用
    """
    has_keys = bool(_configured_keys())
    has_users = bool(_configured_users(include_disabled=False))
    if has_keys and has_users:
        return "both"
    if has_users:
        return "user"
    if has_keys:
        return "key"
    return "off"


def anonymous_principal() -> Principal:
    cfg = load_auth_config()
    if not is_auth_enabled():
        return Principal(name="", role=Role.MAINTAINER)
    return Principal(name="", role=cfg.get("anonymous_role", Role.OPERATOR))


# ──────────────────────────────────────────────────────────────────────────────
# 登录与会话
# ──────────────────────────────────────────────────────────────────────────────

class _Session:
    __slots__ = ("principal", "expires_at")

    def __init__(self, principal: Principal, ttl_seconds: float):
        self.principal = principal
        self.expires_at = time.time() + ttl_seconds


_sessions: Dict[str, _Session] = {}
_sessions_lock = threading.Lock()


def _session_ttl() -> float:
    cfg = load_auth_config()
    try:
        minutes = float(cfg.get("session_timeout_minutes", _DEFAULT_SESSION_MINUTES))
    except (TypeError, ValueError):
        minutes = _DEFAULT_SESSION_MINUTES
    return max(60.0, minutes * 60.0)


def authenticate_by_key(password: str) -> Optional[Principal]:
    """
    只凭口令登录，不需要账号名。

    拿口令去比对 keys 里配的各个角色，命中即取那个角色。按权限从高到低试，
    这样万一两个角色配了同一个口令，给到的是更高的那个而不是随机一个。
    """
    for role in (Role.MAINTAINER, Role.DEVELOPER, Role.OPERATOR):
        for spec in _role_key_specs(role):
            if _spec_password_ok(spec, password):
                return _key_principal(role, spec)
    return None


def authenticate(name: str, password: str) -> Optional[Principal]:
    """核对账号口令。成功返回身份，失败返回 None（不区分"用户不存在"和"密码错"）。"""
    cfg = load_auth_config()
    for user in cfg.get("users", []):
        if not isinstance(user, dict) or user.get("name") != name:
            continue
        if not user.get("enabled", True):
            logger.warning(_LOG, f"账号 {name} 已停用")
            return None
        if not _spec_password_ok(user, password):
            return None
        return Principal(
            name=name,
            role=user.get("role", Role.OPERATOR),
            allowed_projects=user.get("allowed_projects"),
        )
    return None


def login(name: str, password: str) -> Optional[str]:
    """
    登录成功返回会话 token，失败返回 None。

    name 留空就走口令模式（只认口令、不认账号），这也是只配了 keys 时的用法。
    """
    principal = authenticate_by_key(password) if not name else authenticate(name, password)
    if principal is None:
        logger.warning(_LOG, f"登录失败：{name or '(口令模式)'}")
        return None
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        _prune_expired_locked()
        _sessions[token] = _Session(principal, _session_ttl())
    scope = ""
    if principal.role != Role.MAINTAINER:
        scope = " 项目=" + (",".join(principal.allowed_projects) or "无")
    logger.info(_LOG, f"登录成功：{principal.name}（{principal.role_label}{scope}）")
    return token


def logout(token: Optional[str]) -> bool:
    if not token:
        return False
    with _sessions_lock:
        session = _sessions.pop(token, None)
    if session:
        logger.info(_LOG, f"已退出：{session.principal.name}")
    return session is not None


def principal_for_token(token: Optional[str]) -> Principal:
    """token 认不出来（没带/过期/服务重启过）就退回匿名使用者，而不是报错。"""
    if not token:
        return anonymous_principal()
    with _sessions_lock:
        session = _sessions.get(token)
        if session is None:
            return anonymous_principal()
        if session.expires_at <= time.time():
            _sessions.pop(token, None)
            logger.info(_LOG, f"会话超时：{session.principal.name}")
            return anonymous_principal()
        # 有活动就续期，避免正在改图的人被踢出去
        session.expires_at = time.time() + _session_ttl()
        return session.principal


def _prune_expired_locked():
    now = time.time()
    for token in [t for t, s in _sessions.items() if s.expires_at <= now]:
        _sessions.pop(token, None)


def has_any_account() -> bool:
    """是否配了账号。没配时前端不显示登录入口（权限管理未启用）。"""
    return is_auth_enabled()


def status_line() -> str:
    """
    一行说清当前权限状态，服务启动时打印。

    没有这行提示的话，"页面上没有登录入口"到底是没启用还是坏了，只能去翻配置文件。
    """
    if not is_auth_enabled():
        return ("权限管理：未启用（所有人拥有全部权限，页面上没有登录入口）。"
                "启用：在本机改 PACK_DELETE/auth_accounts.json 后运行 "
                "python3 PACK_DELETE/make_auth_pack.py")
    cfg = load_auth_config()
    anon = ROLE_LABELS.get(cfg.get("anonymous_role", Role.OPERATOR), "使用者")
    origin = {"override": "现场覆盖 /config/auth_accounts.pack",
              "factory": "出厂密封包"}.get(_pack_source, "无")
    parts = []
    for role, specs in _configured_keys().items():
        label = ROLE_LABELS.get(role, role)
        for spec in specs:
            piece = label
            extra = str(spec.get("name") or "").strip()
            if extra:
                piece += "/" + extra
            if role == Role.DEVELOPER:
                projects = spec.get("allowed_projects") or []
                piece += "(%s)" % ("、".join(str(x) for x in projects) if projects else "未指定项目")
            parts.append(piece)
    users = _configured_users(include_disabled=False)
    if users:
        parts.append("%d 个具名账号" % len(users))
    return ("权限管理：已启用（%s）。未登录=%s；已设口令：%s；无操作 %s 分钟后退出"
            % (origin, anon, "、".join(parts) or "无", cfg.get("session_timeout_minutes", 480)))
