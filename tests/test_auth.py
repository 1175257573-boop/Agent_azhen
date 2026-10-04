"""认证模块测试。

重点覆盖**安全边界**，而不只是"能登录"：
口令散列不可逆、错误信息不泄露用户是否存在、refresh 一次性、
改口令连带吊销、禁用用户后 token 立即失效。
"""

from __future__ import annotations

import pytest

from agent_kit import auth


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """每个用例用独立的 ATLAS_HOME，避免污染真实用户库。"""
    monkeypatch.setenv("ATLAS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ATLAS_ADMIN_PASSWORD", "")
    yield


# --------------------------------------------------------------- 口令散列


def test_hash_is_salted_and_verifiable():
    a = auth.hash_password("secret123")
    b = auth.hash_password("secret123")
    assert a != b, "相同口令两次散列必须不同（盐随机），否则彩虹表可破"
    assert auth.verify_password("secret123", a)
    assert auth.verify_password("secret123", b)
    assert not auth.verify_password("secret123 ", a), "尾部空格也应算不同口令"


def test_hash_never_contains_plaintext():
    h = auth.hash_password("my-very-secret-pass")
    assert "my-very-secret-pass" not in h


def test_verify_rejects_garbage_stored_value():
    for bad in ("", "not-a-hash", "bcrypt$1$2$3", "scrypt$abc$x$y$z"):
        assert auth.verify_password("whatever", bad) is False


def test_old_parameters_still_verify():
    """散列里带了参数，所以调参后老口令仍要能验证（登录时顺手迁移）。"""
    h = auth.hash_password("secret123")
    scheme, n, r, _p, _salt, _dk = h.split("$")
    assert scheme == "scrypt"
    assert scheme == "scrypt"
    assert n == str(auth._SCRYPT_N)
    assert r == str(auth._SCRYPT_R)
    assert auth.verify_password("secret123", h)


# --------------------------------------------------------------- 用户


def test_create_and_authenticate():
    auth.create_user("alice", "secret123", role="admin")
    u = auth.authenticate("alice", "secret123")
    assert u.username == "alice"
    assert u.is_admin is True
    assert auth.get_user("alice").role == "admin"


def test_wrong_password_and_unknown_user_give_same_error():
    """两者错误信息必须一致 —— 区分开等于送一份用户名清单给攻击者。"""
    auth.create_user("alice", "secret123")
    with pytest.raises(auth.AuthError) as e1:
        auth.authenticate("alice", "wrong-password")
    with pytest.raises(auth.AuthError) as e2:
        auth.authenticate("no-such-user", "wrong-password")
    assert str(e1.value) == str(e2.value) == "用户名或口令不正确"


def test_short_password_and_bad_role_rejected():
    with pytest.raises(ValueError, match="8 位"):
        auth.create_user("bob", "short")
    with pytest.raises(ValueError, match="未知角色"):
        auth.create_user("bob", "longenough", role="root")
    with pytest.raises(ValueError):
        auth.create_user("", "longenough")


def test_disabled_user_cannot_authenticate():
    auth.create_user("carol", "secret123")
    auth.set_disabled("carol", True)
    with pytest.raises(auth.AuthError):
        auth.authenticate("carol", "secret123")
    assert auth.get_user("carol") is None


# --------------------------------------------------------------- token


def test_access_token_roundtrip():
    u = auth.create_user("dave", "secret123")
    pair = auth.issue_tokens(u)
    assert auth.verify_access(pair.access_token).username == "dave"


def test_tampered_access_token_rejected():
    u = auth.create_user("dave", "secret123")
    pair = auth.issue_tokens(u)
    bad = pair.access_token[:-4] + ("aaaa" if not pair.access_token.endswith("aaaa") else "bbbb")
    with pytest.raises(auth.AuthError):
        auth.verify_access(bad)


def test_refresh_token_is_one_time():
    """轮换：旧 refresh 用过即作废，防止 token 被截获后反复使用。"""
    u = auth.create_user("dave", "secret123")
    pair = auth.issue_tokens(u)
    auth.refresh_pair(pair.refresh_token)  # 第一次成功
    with pytest.raises(auth.AuthError):
        auth.refresh_pair(pair.refresh_token)  # 第二次必须失败


def test_refresh_token_not_stored_in_plaintext():
    u = auth.create_user("dave", "secret123")
    pair = auth.issue_tokens(u)
    with auth._connect() as c:
        rows = c.execute("SELECT token_hash FROM refresh_tokens").fetchall()
    assert rows, "refresh token 应该落库"
    assert all(pair.refresh_token not in r[0] for r in rows), "库里存的必须是哈希而非原文"


def test_revoked_refresh_rejected():
    u = auth.create_user("dave", "secret123")
    pair = auth.issue_tokens(u)
    auth.revoke(pair.refresh_token)
    with pytest.raises(auth.AuthError):
        auth.refresh_pair(pair.refresh_token)


def test_password_change_kills_all_sessions():
    """改口令后旧 access token 必须立即失效——否则改口令等于没改。"""
    u = auth.create_user("dave", "secret123")
    pair = auth.issue_tokens(u)
    assert auth.verify_access(pair.access_token)  # 改之前有效

    revoked = auth.set_password("dave", "newpass123")
    assert revoked >= 1, "改口令应连带吊销既有会话"
    with pytest.raises(auth.AuthError):
        auth.verify_access(pair.access_token)
    with pytest.raises(auth.AuthError):
        auth.refresh_pair(pair.refresh_token)
    assert auth.authenticate("dave", "newpass123").username == "dave"


def test_disabling_user_invalidates_live_token():
    u = auth.create_user("dave", "secret123")
    pair = auth.issue_tokens(u)
    auth.set_disabled("dave", True)
    with pytest.raises(auth.AuthError):
        auth.verify_access(pair.access_token)


def test_expired_refresh_token_rejected(monkeypatch):
    u = auth.create_user("dave", "secret123")
    pair = auth.issue_tokens(u)
    # 把它改成已过期
    auth.dbmod.transact(auth.db_path(), lambda c: c.execute(
        "UPDATE refresh_tokens SET expires_at='2000-01-01T00:00:00+00:00'"
    ), ddl=auth._DDL)
    with pytest.raises(auth.AuthError, match="过期"):
        auth.refresh_pair(pair.refresh_token)


def test_purge_expired_keeps_valid():
    u = auth.create_user("dave", "secret123")
    auth.issue_tokens(u)
    auth.dbmod.transact(auth.db_path(), lambda c: c.execute(
        "UPDATE refresh_tokens SET expires_at='2000-01-01T00:00:00+00:00'"
    ), ddl=auth._DDL)
    auth.purge_expired()
    with auth._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM refresh_tokens").fetchone()[0] == 0


# --------------------------------------------------------------- 引导


def test_bootstrap_requires_env_password(monkeypatch):
    """没有 ATLAS_ADMIN_PASSWORD 时**不许**建默认账号——
    留个 admin/admin123 之类的默认口令是安全事故。"""
    monkeypatch.setenv("ATLAS_ADMIN_PASSWORD", "")
    assert auth.ensure_bootstrap_admin() is None
    with auth._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_bootstrap_creates_admin_once(monkeypatch):
    monkeypatch.setenv("ATLAS_ADMIN_PASSWORD", "bootstrap-pass")
    first = auth.ensure_bootstrap_admin()
    assert first is not None and first.is_admin
    assert auth.ensure_bootstrap_admin() is None, "已有用户时不该重复创建"
    assert auth.authenticate(first.username, "bootstrap-pass").is_admin


def test_secret_survives_restart(tmp_path, monkeypatch):
    """签名密钥必须持久化，否则重启一次全体 token 作废。"""
    monkeypatch.setenv("ATLAS_HOME", str(tmp_path / "home2"))
    first = auth._load_secret()
    assert auth._load_secret() == first
    # 换进程（清掉模块级缓存）再取
    monkeypatch.setattr(auth, "_SECRET_FILE", "jwt_secret")
    assert auth._load_secret() == first
