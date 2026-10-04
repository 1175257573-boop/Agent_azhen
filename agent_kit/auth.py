"""认证核心：用户、口令散列、JWT 签发与校验、refresh token 撤销。

为什么分两层 token
--------------------
只发 JWT 会有一个绕不过去的坑：**JWT 天然无法撤销**。员工离职、
管理员改了口令、或者只是怀疑 token 泄露，在纯 JWT 方案里都只能干等过期。
企业场景 unacceptable，所以这里：

    · access token  = JWT，15 分钟，无状态，校验零数据库查询
    · refresh token = 随机串，**存表**，可撤销、可追踪

access 过期后用 refresh 换一个，refresh 失效则必须重新登录。

口令散列为什么不用 bcrypt / argon2
----------------------------------
那两个都要编译 C 扩展，在 Windows 上装失败是常见问题（本项目的使用者大多在
Windows）。标准库的 ``hashlib.scrypt`` 同样是内存硬（memory-hard）算法，
抗 GPU 暴破，**零新增依赖**。参数与轮换策略见 `hash_password`。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import jwt

from agent_kit import db as dbmod
from agent_kit.logging_conf import get_logger

log = get_logger("auth")

# ---- 配置 -------------------------------------------------------------------

def _atlas_home() -> Path:
    return Path(os.environ.get("ATLAS_HOME", Path.home() / ".atlas"))


def db_path() -> Path:
    return _atlas_home() / "auth.db"


#: JWT 签名密钥。落盘一次，之后复用（重启后旧 token 仍然有效）。
_SECRET_FILE = "jwt_secret"


def _load_secret() -> bytes:
    """取签名密钥，不存在就生成一个并落盘。

    用文件而不是每次随机：否则**重启一次，所有已签发的 token 全部失效**。
    """
    path = _atlas_home() / _SECRET_FILE
    if path.is_file():
        data = path.read_bytes().strip()
        if data:
            return data
    secret = secrets.token_urlsafe(48).encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(secret)
    os.replace(tmp, path)  # 原子替换，避免半截文件
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows 上 chmod 只影响只读位
    log.info("已生成新的 JWT 签名密钥：%s", path)
    return secret


# ---- 口令散列 ---------------------------------------------------------------

#: scrypt 参数。n=2**14 约需 16MB 内存、耗时 50-100ms（单次登录足够，扛不住批量爆破）。
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    """口令散列，格式 ``scrypt$n$r$p$salt_b64$hash_b64``。

    把参数写进结果里：以后调参（提高 n/r）后，老口令仍能按旧参数验证，
    用户只需在下次登录时重新散列即可迁移。
    """
    salt = os.urandom(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=64 * 1024 * 1024,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令。

    用 `hmac.compare_digest` 而不是 `==`：后者按字节短路比较，
    理论上会泄漏"前几位对了"的信息。
    """
    try:
        scheme, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n_s), r=int(r_s), p=int(p_s),
            dklen=len(base64.b64decode(hash_b64)),
            maxmem=64 * 1024 * 1024,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, base64.b64decode(hash_b64))


# ---- 数据表 -----------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS users (
    username    TEXT PRIMARY KEY,
    pwd_hash    TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'user',
    created_at  TEXT NOT NULL,
    disabled    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    issued_at  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens(username);
CREATE INDEX IF NOT EXISTS idx_refresh_exp  ON refresh_tokens(expires_at);
"""


def _connect() -> sqlite3.Connection:
    return dbmod.connect(db_path(), ddl=_DDL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- 用户 -------------------------------------------------------------------

ROLES = ("admin", "user")


@dataclass(frozen=True)
class User:
    username: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class AuthError(Exception):
    """认证失败。调用方转成 401。"""


def create_user(username: str, password: str, role: str = "user") -> User:
    if role not in ROLES:
        raise ValueError(f"未知角色：{role}")
    if not username or len(username) > 64:
        raise ValueError("用户名长度需在 1-64 之间")
    if not password or len(password) < 8:
        raise ValueError("口令至少 8 位")
    dbmod.transact(db_path(), lambda c: c.execute(
        "INSERT INTO users(username, pwd_hash, role, created_at) VALUES(?,?,?,?)",
        (username, hash_password(password), role, _now()),
    ), ddl=_DDL)
    log.info("已创建用户 %s（角色 %s）", username, role)
    return User(username=username, role=role)


def get_user(username: str) -> User | None:
    with _connect() as c:
        row = c.execute(
            "SELECT username, role, disabled FROM users WHERE username=?", (username,)
        ).fetchone()
    if row is None or row[2]:
        return None
    return User(username=row[0], role=row[1])


def list_users() -> list[User]:
    with _connect() as c:
        rows = c.execute(
            "SELECT username, role FROM users WHERE disabled=0 ORDER BY username"
        ).fetchall()
    return [User(username=r[0], role=r[1]) for r in rows]


def authenticate(username: str, password: str) -> User:
    """口令校验。失败一律抛 `AuthError`，**不区分"用户不存在"和"口令错"**——
    区分开等于送给攻击者一份用户名清单。
    """
    with _connect() as c:
        row = c.execute(
            "SELECT username, role, pwd_hash, disabled FROM users WHERE username=?",
            (username,),
        ).fetchone()
    if row is None or row[3] or not verify_password(password, row[2]):
        raise AuthError("用户名或口令不正确")
    return User(username=row[0], role=row[1])


def set_password(username: str, password: str) -> int:
    """改口令。返回被连带吊销的会话数。

    改口令后**必须吊销该用户全部会话**：否则旧 access token 在有效期内
    仍能继续用，改口令等于没改。
    """
    if not password or len(password) < 8:
        raise ValueError("口令至少 8 位")
    dbmod.transact(db_path(), lambda c: c.execute(
        "UPDATE users SET pwd_hash=? WHERE username=?", (hash_password(password), username)
    ), ddl=_DDL)
    revoked = revoke_all(username)
    log.info("已修改 %s 的口令并吊销其 %d 个会话", username, revoked)
    return revoked


def set_disabled(username: str, disabled: bool) -> None:
    dbmod.transact(db_path(), lambda c: c.execute(
        "UPDATE users SET disabled=? WHERE username=?", (1 if disabled else 0, username)
    ), ddl=_DDL)
    if disabled:
        revoke_all(username)


# ---- token ------------------------------------------------------------------

ACCESS_TTL_S = 15 * 60
REFRESH_TTL_S = 30 * 24 * 3600
ALGORITHM = "HS256"


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    user: User


def _token_hash(raw: str) -> str:
    """refresh token 存哈希而非原文。

    库被读走时，攻击者拿到的不能直接用于换 access —— 这是最基本的防御。
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pwd_fingerprint(username: str) -> str:
    """取用户当前口令散列的短指纹。

    access token 里带上它，改口令后指纹立刻对不上，旧 token 随之失效。

    ⚠️ **不能直接取 `pwd_hash[:16]`**——散列的格式是
    ``scrypt$n$r$p$salt$hash``，前 16 个字符恰好是 ``scrypt$16384$8$1``，
    也就是**格式头和参数**：每次散列都一模一样，指纹根本不随口令变化，
    于是「改口令后旧 token 失效」这条形同虚设（实测踩过）。
    这里对**整个散列**再取一次 SHA-256，长度固定且必然随口令变化。
    """
    with _connect() as c:
        row = c.execute("SELECT pwd_hash FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        return ""
    return hashlib.sha256(row[0].encode("utf-8")).hexdigest()[:16]


def issue_tokens(user: User) -> TokenPair:
    now = int(time.time())
    access = jwt.encode(
        {
            "sub": user.username,
            "role": user.role,
            "typ": "access",
            # 口令指纹：改口令后旧 token 立即失效（否则改口令等于没改）
            "pwd": _pwd_fingerprint(user.username),
            "iat": now,
            "exp": now + ACCESS_TTL_S,
        },
        _load_secret(),
        algorithm=ALGORITHM,
    )
    refresh = secrets.token_urlsafe(32)
    dbmod.transact(db_path(), lambda c: c.execute(
        "INSERT INTO refresh_tokens(token_hash, username, issued_at, expires_at) "
        "VALUES(?,?,?,?)",
        (_token_hash(refresh), user.username, _now(),
         datetime.fromtimestamp(now + REFRESH_TTL_S, timezone.utc).isoformat(timespec="seconds")),
    ), ddl=_DDL)
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=ACCESS_TTL_S,
        user=user,
    )


def verify_access(token: str) -> User:
    """校验 access token。

    失败**一律**抛 `AuthError`，不区分"过期"和"签名不对"——告诉调用方
    具体原因等于帮他区分"改一个字试试"和"换一个 token 试试"。
    """
    try:
        payload = jwt.decode(token, _load_secret(), algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:
        raise AuthError("access token 无效") from exc
    if payload.get("typ") != "access":
        raise AuthError("token 类型不正确")
    username = str(payload.get("sub", ""))
    user = get_user(username)
    if user is None:
        # 用户在签发后被禁用或删除 —— token 虽未过期，但已不该被接受
        raise AuthError("用户已失效")
    # 口令指纹对不上 = 签发之后改过口令（或口令被重置），旧 token 一律作废
    if not hmac.compare_digest(str(payload.get("pwd", "")), _pwd_fingerprint(username)):
        raise AuthError("口令已变更，请重新登录")
    # 角色也放进 token：管理员被降级后，旧 token 不能继续享受 admin 权限
    if payload.get("role") != user.role:
        raise AuthError("角色已变更，请重新登录")
    return user


def refresh_pair(raw_refresh: str) -> TokenPair:
    """用 refresh token 换一对新 token，并作废旧的（一次性使用）。"""
    th = _token_hash(raw_refresh)
    with _connect() as c:
        row = c.execute(
            "SELECT username, expires_at, revoked FROM refresh_tokens WHERE token_hash=?",
            (th,),
        ).fetchone()
    if row is None or row[2]:
        raise AuthError("refresh token 无效")
    if row[1] < _now():
        raise AuthError("refresh token 已过期，请重新登录")
    user = get_user(row[0])
    if user is None:
        raise AuthError("用户已失效")
    revoke(raw_refresh)  # 轮换：旧的立即作废
    return issue_tokens(user)


def revoke(raw_refresh: str) -> None:
    dbmod.transact(db_path(), lambda c: c.execute(
        "UPDATE refresh_tokens SET revoked=1 WHERE token_hash=?", (_token_hash(raw_refresh),)
    ), ddl=_DDL)


def revoke_all(username: str) -> int:
    """吊销某用户全部会话（登出所有设备 / 改口令 / 禁用账号时调用）。"""
    return int(dbmod.transact(db_path(), lambda c: c.execute(
        "UPDATE refresh_tokens SET revoked=1 WHERE username=? AND revoked=0", (username,)
    ).rowcount, ddl=_DDL) or 0)


def purge_expired() -> int:
    """清理过期 token，避免表无限增长。由调用方定期触发。"""
    return int(dbmod.transact(db_path(), lambda c: c.execute(
        "DELETE FROM refresh_tokens WHERE expires_at < ?", (_now(),)
    ).rowcount, ddl=_DDL) or 0)


# ---- 首次启动引导 -----------------------------------------------------------

BOOTSTRAP_USER = os.environ.get("ATLAS_ADMIN_USER", "admin")


def ensure_bootstrap_admin() -> User | None:
    """库里一个用户都没有时，用环境变量里的口令建一个管理员。

    - 口令只从 `ATLAS_ADMIN_PASSWORD` 读，**没有默认值**：
      留个 `admin/admin123` 之类的默认口令是安全事故，宁可起不来。
    - 已经建过就直接返回 None，不覆盖既有口令。
    """
    with _connect() as c:
        n = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if n:
        return None
    password = os.environ.get("ATLAS_ADMIN_PASSWORD", "")
    if not password:
        log.warning(
            "用户库为空且未设置 ATLAS_ADMIN_PASSWORD，"
            "请先执行 `python main.py user add <用户名> <口令>` 创建账号"
        )
        return None
    return create_user(BOOTSTRAP_USER, password, role="admin")
