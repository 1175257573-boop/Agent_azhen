"""自建 SQLite 长期记忆 store 的测试。

这个 store 是自己实现的（官方没有 SQLite 版），所以每个操作都要有测试兜底，
尤其是「过期清理」和「namespace 前缀匹配」这两处最容易写错的地方。
"""

from __future__ import annotations

import pytest

from agent_kit.sqlite_store import SqliteStore


@pytest.fixture()
def store(tmp_path) -> SqliteStore:
    instance = SqliteStore(tmp_path / "store.db")
    yield instance
    instance.close()


def test_put_and_get(store):
    store.put(("user", "u1"), "city", {"value": "武汉"})
    item = store.get(("user", "u1"), "city")
    assert item is not None
    assert item.value == {"value": "武汉"}
    assert tuple(item.namespace) == ("user", "u1")


def test_put_overwrites_same_key(store):
    store.put(("user", "u1"), "city", {"value": "武汉"})
    store.put(("user", "u1"), "city", {"value": "长沙"})
    assert store.get(("user", "u1"), "city").value == {"value": "长沙"}


def test_get_missing_key_returns_none(store):
    assert store.get(("user", "nobody"), "nothing") is None


def test_search_by_prefix_and_filter(store):
    store.put(("user", "u1"), "a", {"city": "武汉", "level": 1})
    store.put(("user", "u1"), "b", {"city": "长沙", "level": 2})
    store.put(("other", "x"), "c", {"city": "武汉", "level": 3})

    assert len(store.search(("user",))) == 2
    assert len(store.search(("user",), filter={"city": "武汉"})) == 1
    assert len(store.search((), filter={"city": "武汉"})) == 2


def test_search_respects_limit_and_offset(store):
    for i in range(5):
        store.put(("u",), f"k{i}", {"i": i})
    page = store.search(("u",), limit=2, offset=1)
    assert len(page) == 2


def test_delete(store):
    store.put(("user", "u1"), "k", {"v": 1})
    store.delete(("user", "u1"), "k")
    assert store.get(("user", "u1"), "k") is None


def test_list_namespaces(store):
    store.put(("user", "u1"), "k", {"v": 1})
    store.put(("user", "u2"), "k", {"v": 2})
    assert set(store.list_namespaces()) == {("user", "u1"), ("user", "u2")}


def test_expired_item_is_invisible_and_cleaned(store):
    # ttl 单位是分钟，负数即「已经过期」
    store.put(("user", "u1"), "old", {"v": 1}, ttl=-1)
    assert store.get(("user", "u1"), "old") is None
    assert store.search(("user",)) == []


def test_data_survives_reopen(tmp_path):
    """落盘的意义：换一个实例（等价于进程重启）数据还在。"""
    path = tmp_path / "persist.db"
    first = SqliteStore(path)
    first.put(("user", "u1"), "city", {"value": "武汉"})
    first.close()

    second = SqliteStore(path)
    try:
        assert second.get(("user", "u1"), "city").value == {"value": "武汉"}
    finally:
        second.close()
