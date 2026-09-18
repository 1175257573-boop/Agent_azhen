"""SQLite 版长期记忆 store（自建实现）。

**为什么自己写**：langgraph 1.2 内置的 `BaseStore` 实现只有 memory / postgres / redis
三种——`langgraph.store.sqlite` 这个模块**并不存在**，PyPI 上也没有同名包
（`pip install langgraph-store-sqlite` 直接报 no matching distribution）。
而本地开发最需要的恰恰是「零服务、能落盘、重启不丢」这一档，所以这里照官方
`BaseStore` 契约自己实现一个，接口与 `InMemoryStore` 对齐，换后端不用改调用方。

实现范围（**如实标注，别当全能**）：
  · 支持 put / get / search / delete / list_namespaces 五种操作
  · `search` 的 filter 走**等值匹配**（与内存版一致），不支持 $gt / $contains 这类算子
  · `search` 的 query 在 SQLite 下按**子串匹配**处理；要做向量语义检索，
    请注册自己的检索器（见 `agent_kit.retrieval`），store 不负责这件事
  · ttl 以**分钟**计，过期项在读取时惰性清理，不跑后台线程
  · `abatch` 是同步实现（sqlite3 阻塞），异步图里会有事件循环阻塞，
    本项目长期记忆只在启动时读写几次，可接受；高并发场景请换 Postgres
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    Op,
    PutOp,
    Result,
    SearchOp,
)

_TABLE = "long_term_store"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    namespace   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    expires_at  TEXT,
    PRIMARY KEY (namespace, key)
);
CREATE INDEX IF NOT EXISTS idx_store_ns ON {_TABLE}(namespace);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _ns_key(namespace: tuple[str, ...]) -> str:
    """namespace 是 tuple，SQLite 里存成 JSON 数组，避免分隔符被内容污染。"""
    return json.dumps(list(namespace), ensure_ascii=False)


def _ns_load(raw: str) -> tuple[str, ...]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return tuple(raw.split("/")) if raw else ()
    return tuple(data) if isinstance(data, list) else (str(data),)


def _matches(value: Any, expected: Any) -> bool:
    """filter 的等值语义，对齐 InMemoryStore：值相等，或列表中包含该值。"""
    if isinstance(value, list):
        return expected in value or value == expected
    return value == expected


class SqliteStore(BaseStore):
    """长期记忆的 SQLite 落盘实现。单文件、无外部服务、进程重启不丢。"""

    # BaseStore 要求显式声明支持 TTL，否则 put(ttl=...) 会抛 NotImplementedError
    supports_ttl = True
    supports_index = False

    def __init__(self, db_path: str | Path, *, table: str = _TABLE) -> None:
        self.db_path = str(db_path)
        self.table = table
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_DDL)

    # ------------------------------------------------------------------
    # BaseStore 契约：batch / abatch
    # ------------------------------------------------------------------
    def batch(self, ops: Iterable[Op]) -> list[Result]:
        results: list[Result] = []
        with self._lock:
            for op in ops:
                if isinstance(op, GetOp):
                    results.append(self._get(op))
                elif isinstance(op, SearchOp):
                    results.append(self._search(op))
                elif isinstance(op, PutOp):
                    self._put(op)
                    results.append(None)
                elif isinstance(op, ListNamespacesOp):
                    results.append(self._list_namespaces(op))
                else:  # pragma: no cover - 未来新增 op 时的兜底
                    raise TypeError(f"不支持的操作类型：{type(op).__name__}")
        return results

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        """同步实现（见模块 docstring 的局限说明）。"""
        return self.batch(ops)

    def close(self) -> None:
        """关闭连接。调用方通常在进程退出时统一收尾（memory.py 用 atexit 注册）。"""
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # 各操作
    # ------------------------------------------------------------------
    def _get(self, op: GetOp) -> Item | None:
        row = self._conn.execute(
            f"SELECT * FROM {self.table} WHERE namespace = ? AND key = ?",
            (_ns_key(op.namespace), op.key),
        ).fetchone()
        if row is None:
            return None
        item = self._row_to_item(row)
        if item is None:  # 已过期
            self._conn.execute(
                f"DELETE FROM {self.table} WHERE namespace = ? AND key = ?",
                (_ns_key(op.namespace), op.key),
            )
            self._conn.commit()
            return None
        return item

    def _put(self, op: PutOp) -> None:
        ns = _ns_key(op.namespace)

        # BaseStore.delete 的实现就是「put 一个 value=None」，这里要认出来
        if op.value is None:
            self._conn.execute(
                f"DELETE FROM {self.table} WHERE namespace = ? AND key = ?",
                (ns, op.key),
            )
            self._conn.commit()
            return

        now = _now()
        expires = now + timedelta(minutes=op.ttl) if op.ttl else None
        self._conn.execute(
            f"""INSERT INTO {self.table} (namespace, key, value, created_at, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at""",
            (
                ns,
                op.key,
                json.dumps(op.value, ensure_ascii=False),
                _iso(now),
                _iso(now),
                _iso(expires) if expires else None,
            ),
        )
        self._conn.commit()

    def _search(self, op: SearchOp) -> list[Item]:
        prefix = json.dumps(list(op.namespace_prefix), ensure_ascii=False)[:-1]  # 去掉结尾的 ]
        rows = self._conn.execute(
            f"SELECT * FROM {self.table} WHERE namespace LIKE ? ORDER BY updated_at DESC",
            (prefix + "%",),
        ).fetchall()

        items: list[Item] = []
        for row in rows:
            item = self._row_to_item(row)
            if item is None:
                continue
            if op.filter and not all(
                _matches(item.value.get(k), v) for k, v in op.filter.items()
            ):
                continue
            if op.query and op.query.lower() not in json.dumps(item.value, ensure_ascii=False).lower():
                continue
            items.append(item)

        offset = op.offset or 0
        limit = op.limit if op.limit is not None else len(items)
        return items[offset : offset + limit]

    def _list_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        rows = self._conn.execute(f"SELECT DISTINCT namespace FROM {self.table}").fetchall()
        namespaces = [_ns_load(r["namespace"]) for r in rows]

        depth = op.max_depth
        result: list[tuple[str, ...]] = []
        for ns in namespaces:
            if op.match_conditions:
                ok = True
                for cond in op.match_conditions:
                    path = tuple(cond.path)
                    if cond.match_type == "prefix":
                        ok = ok and ns[: len(path)] == path
                    elif cond.match_type == "suffix":
                        ok = ok and (len(ns) < len(path) or ns[-len(path) :] == path)
                    else:  # 未知条件宁可不匹配，也不要误放行
                        ok = False
                if not ok:
                    continue
            trimmed = ns[:depth] if depth is not None else ns
            if trimmed and trimmed not in result:
                result.append(trimmed)
        return result

    # ------------------------------------------------------------------
    # 内部：行 → Item（顺带做过期判定）
    # ------------------------------------------------------------------
    def _row_to_item(self, row: sqlite3.Row) -> Item | None:
        expires = _parse(row["expires_at"])
        if expires is not None and expires <= _now():
            return None
        try:
            value = json.loads(row["value"])
        except ValueError:
            value = row["value"]
        return Item(
            value=value,
            key=row["key"],
            namespace=_ns_load(row["namespace"]),
            created_at=_parse(row["created_at"]) or _now(),
            updated_at=_parse(row["updated_at"]) or _now(),
        )
