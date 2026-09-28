"""运行时凭据库与密钥接口的测试。

这组测试守的是**保密契约**，不是功能点。功能写错了用户看得见，
保密写错了没人看得见——所以要靠断言把几条硬约束钉住：

    1. 对外永远只有掩码（`CredentialInfo` / 状态接口里搜不到明文）；
    2. 默认不落盘；落盘要显式勾选，清除时文件也一起删；
    3. 清除界面注入的密钥时，**原本的环境变量必须被恢复**（不能被误删）；
    4. 日志与异常里的密钥一律打码，且脱敏不能反过来弄坏正常的日志参数；
    5. 密钥接口只认回环来源。
"""

from __future__ import annotations

import json
import logging
import os

import pytest

from agent_kit import credentials as creds
from agent_kit.redact import (
    forget_secret,
    known_secret_count,
    mask,
    redact,
    register_secret,
)

KEY = "sk-abcdef0123456789abcdef0123456789"


@pytest.fixture
def store(tmp_path):
    """每个用例一个全新的凭据库，避免单例把状态带到下个用例。"""
    return creds.CredentialsStore(home=tmp_path / "home")


# ============================================================ 掩码与脱敏

def test_mask_hides_middle_and_shorts():
    assert mask(KEY) == "sk-a***6789"
    assert mask("short") == "****"       # 太短就整体打码，不掐头去尾
    assert mask("") == ""


def test_redact_replaces_registered_secret():
    register_secret(KEY)
    try:
        out = redact(f"请求头里带了 {KEY} 这个值")
        assert KEY not in out
        assert "sk-a***6789" in out
    finally:
        forget_secret(KEY)


def test_redact_catches_unregistered_sk_and_bearer():
    """形态兜底：即使某个密钥没登记过，也不能原样漏出去。"""
    assert "sk-unregistered-key-123456" not in redact("token=sk-unregistered-key-123456")
    assert "abcdefghijklmnop1234" not in redact("Authorization: Bearer abcdefghijklmnop1234")


def test_redacting_formatter_keeps_numeric_args_intact():
    """脱敏不能把 %d 的参数换成字符串——那会让原本正常的日志直接抛异常。"""
    from agent_kit.redact import RedactingFormatter

    formatter = RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="处理了 %d 条，耗时 %.1fms，key=%s", args=(3, 12.5, KEY), exc_info=None,
    )
    register_secret(KEY)
    try:
        text = formatter.format(record)
    finally:
        forget_secret(KEY)
    assert "处理了 3 条" in text
    assert "12.5ms" in text
    assert KEY not in text


# ============================================================ 输入清洗

@pytest.mark.parametrize(
    "raw",
    [
        KEY,
        f"DASHSCOPE_API_KEY={KEY}",
        f'export DASHSCOPE_API_KEY="{KEY}"',
        f"  {KEY}  ",
        f"'{KEY}'",
    ],
)
def test_clean_key_input_strips_paste_wrappers(raw):
    assert creds.clean_key_input(raw) == KEY


def test_clean_key_input_keeps_equal_sign_inside_key():
    """密钥本体含 '=' 时不能被当成 kv 分隔符切掉。"""
    weird = "abc=def=ghi=jkl=mnop"
    assert creds.clean_key_input(weird) == weird


# ============================================================ 注入与恢复

def test_set_injects_env_and_reports_state(store, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    info = store.set("dashscope", KEY)
    assert os.environ["DASHSCOPE_API_KEY"] == KEY
    assert info.configured is True
    assert info.origin == "runtime"
    assert info.masked == "sk-a***6789"
    assert info.length == len(KEY)
    assert info.persistent is False          # 默认不落盘
    assert store.active_provider() == "dashscope"


def test_info_never_contains_plaintext(store, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    store.set("dashscope", KEY)
    dumped = json.dumps([i.to_dict() for i in store.list()], ensure_ascii=False)
    assert KEY not in dumped
    assert "sk-a***6789" in dumped


def test_clear_restores_pre_existing_env(store, monkeypatch):
    """关键回归点：界面清了密钥，不能把用户原本配好的环境变量一起抹掉。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-original-from-system-env")
    assert store.info("dashscope").origin == "env"

    store.set("dashscope", KEY)
    assert os.environ["DASHSCOPE_API_KEY"] == KEY
    assert store.info("dashscope").shadowed is True

    store.clear("dashscope")
    assert os.environ["DASHSCOPE_API_KEY"] == "sk-original-from-system-env"
    assert store.info("dashscope").origin == "env"


def test_clear_removes_env_when_nothing_was_there(store, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store.set("openai", KEY)
    store.clear("openai")
    assert "OPENAI_API_KEY" not in os.environ


def test_reject_bad_input(store):
    with pytest.raises(ValueError):
        store.set("openai", "short")
    with pytest.raises(ValueError):
        store.set("openai", "sk-has space in it")
    with pytest.raises(ValueError):
        store.set("not-a-provider", KEY)


def test_active_provider_falls_back_to_fake(store, monkeypatch):
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert store.active_provider() == "fake"


# ============================================================ 落盘

def test_remember_writes_file_and_reloads(store, tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    store.set("dashscope", KEY, remember=True)
    assert store.file_exists()

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["credentials"]["dashscope"]["api_key"] == KEY

    # 换一个全新的 store 模拟"服务重启"
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    fresh = creds.CredentialsStore(home=tmp_path / "home")
    assert fresh.load_persisted() == ["dashscope"]
    assert os.environ["DASHSCOPE_API_KEY"] == KEY
    assert fresh.info("dashscope").persistent is True


def test_load_persisted_does_not_override_real_env(store, tmp_path, monkeypatch):
    """环境变量是用户更明确的意图，文件里的那份不该盖掉它。"""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    store.set("dashscope", KEY, remember=True)

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-from-system-env")
    fresh = creds.CredentialsStore(home=tmp_path / "home")
    assert fresh.load_persisted() == []
    assert os.environ["DASHSCOPE_API_KEY"] == "sk-from-system-env"


def test_forget_deletes_file(store, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    store.set("dashscope", KEY, remember=True)
    assert store.file_exists()

    store.clear("dashscope", forget=True)
    assert not store.file_exists()          # 不留空壳文件


def test_corrupt_file_is_ignored_not_fatal(store, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ 这不是合法 JSON", encoding="utf-8")
    assert store.load_persisted() == []     # 文件坏了只让用户重填，不能拖垮启动


# ============================================================ 显式读取

def test_reveal_returns_plaintext(store, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    store.set("dashscope", KEY)
    assert store.reveal("dashscope") == KEY
    assert store.reveal("openai") is None   # 没配的 provider 不返回任何东西


# ============================================================ 接口层

def _client(host="127.0.0.1"):
    """构造一个来源地址可控的测试客户端 —— 用来验证"仅本机"这道门。

    `raise_server_exceptions=False`：本项目用 `@app.exception_handler(Exception)`
    把未捕获异常统一转成带状态码的 JSON。Starlette 在生成这个响应之后**仍会把异常
    重新抛出**（给服务器日志用），所以测试里必须关掉重抛，否则拿到的不是响应而是异常。
    """
    from fastapi.testclient import TestClient

    from server.app import app

    return TestClient(app, client=(host, 50000), raise_server_exceptions=False)


@pytest.fixture
def api(monkeypatch):
    """干净环境下的接口客户端；单例 store 的落盘位置指到临时目录。"""
    client = _client()
    monkeypatch.setattr(creds, "store", creds.CredentialsStore(home=os.environ["ATLAS_HOME"]))
    yield client
    for name in creds.ENV_NAME.values():
        os.environ.pop(name, None)


def test_api_rejects_non_loopback_source():
    """服务本身没有鉴权，来源限制就是最后一道门——必须真的挡住。"""
    resp = _client(host="192.168.1.20").get("/api/credentials")
    assert resp.status_code == 403


def test_api_roundtrip(api):
    assert api.get("/api/credentials").status_code == 200

    resp = api.post("/api/credentials", json={"provider": "dashscope", "api_key": KEY, "remember": False})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert KEY not in resp.text                       # 响应体里绝不能出现明文
    assert body["active_provider"] == "dashscope"
    item = next(i for i in body["items"] if i["provider"] == "dashscope")
    assert item["configured"] is True
    assert item["masked"] == "sk-a***6789"
    assert item["persistent"] is False

    # 显式读取是唯一会回传明文的出口
    reveal = api.post("/api/credentials/reveal", json={"provider": "dashscope"})
    assert reveal.status_code == 200
    assert reveal.json()["api_key"] == KEY
    assert "no-store" in reveal.headers.get("cache-control", "")

    # 清除后恢复到"未配置"
    resp = api.delete("/api/credentials/dashscope?forget=true")
    assert resp.status_code == 200
    assert all(not i["configured"] for i in resp.json()["items"])
    assert api.post("/api/credentials/reveal", json={"provider": "dashscope"}).status_code == 404


def test_api_rejects_short_key(api):
    resp = api.post("/api/credentials", json={"provider": "openai", "api_key": "abc"})
    assert resp.status_code == 422        # pydantic 的 min_length 先拦下


def test_api_unknown_provider(api):
    resp = api.post("/api/credentials", json={"provider": "nope", "api_key": KEY})
    assert resp.status_code == 400        # ValueError → 全局处理器映射成 400


def test_api_verify_without_key(api):
    resp = api.post("/api/credentials/verify", json={"provider": "openai"})
    assert resp.status_code == 400


def test_api_verify_fake_needs_no_key(api):
    resp = api.post("/api/credentials/verify", json={"provider": "fake"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_api_notes_explain_storage(api):
    """保密说明要落到接口里，避免前后端各写一套口径。"""
    body = api.get("/api/credentials").json()
    assert body["notes"]
    assert any("凭据" in n or "密钥" in n for n in body["notes"])
    assert body["storage_path"].endswith("credentials.json")
