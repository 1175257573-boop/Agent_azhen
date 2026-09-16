"""记忆层服务 —— 对标 Java 的 @Service，专门管短期/长期记忆的读写。"""

from __future__ import annotations

from typing import Any

from agent_kit import memory as mem


class MemoryService:
    """把 memory 模块的能力包装成 Web 友好的结构。"""

    def status(self) -> dict[str, Any]:
        return {
            "short_term": mem.short_backend(),
            "long_term": mem.long_backend(),
            "window": mem.window_size(),
            "resolved": dict(mem.RESOLVED),
            "report": mem.report(),
        }

    def preferences(self, user_id: str = "demo") -> list[dict[str, Any]]:
        store = mem.build_store()
        try:
            items = store.search(("preferences", user_id))
        except Exception:  # noqa: BLE001
            return []
        return [{"key": it.key, "value": (it.value or {}).get("value"), "updated_at": (it.value or {}).get("updated_at")} for it in items]

    def put_preference(self, user_id: str, key: str, value: str) -> dict[str, Any]:
        from datetime import datetime, timezone

        store = mem.build_store()
        # 存进 PG 的时间必须带时区偏移，否则跨时区读取会错乱
        now = datetime.now(timezone.utc).astimezone().isoformat()
        store.put(("preferences", user_id), key, {"value": value, "updated_at": now})
        return {"user_id": user_id, "key": key, "value": value}

    def delete_preference(self, user_id: str, key: str) -> bool:
        store = mem.build_store()
        try:
            store.delete(("preferences", user_id), key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def threads(self) -> list[dict[str, Any]]:
        """列出 Redis 里所有会话（只有实现了扫描的 saver 才拿得到）。"""
        cp = mem.build_checkpointer()
        scanner = getattr(cp, "list_threads", None)
        if scanner is None:
            return []
        try:
            return scanner()
        except Exception:  # noqa: BLE001
            return []
