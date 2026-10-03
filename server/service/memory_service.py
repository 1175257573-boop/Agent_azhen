"""记忆层服务 —— 对标 Java 的 @Service，专门管短期/长期记忆的读写。"""

from __future__ import annotations

from typing import Any

from agent_kit import memory as mem
from agent_kit.logging_conf import get_logger

log = get_logger("web.memory")


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

    def threads(self, scan_limit: int = 500) -> list[dict[str, Any]]:
        """列出所有会话及其消息条数（Web UI 的会话列表用）。

        为什么要两条路径：`list_threads()` 只有项目自己的 Redis saver 实现，
        而默认后端是 SQLite —— 原先只认 `list_threads`，没有就返回空列表，
        于是**用 SQLite 时会话列表永远是空的**（历史会话一个都看不到）。

        现在先走后端自带实现，没有则退回 langgraph 通用的 `list()`：
        它在所有 saver 上都有，按 checkpoint 倒序返回，所以同一个 thread
        第一次出现的就是它的最新快照，据此去重即可。
        """
        cp = mem.shared_checkpointer()

        scanner = getattr(cp, "list_threads", None)
        if callable(scanner):
            try:
                return scanner()
            except Exception as exc:  # noqa: BLE001
                log.warning("后端自带 list_threads 失败，改用通用扫描：%s", exc)

        out: dict[str, dict[str, Any]] = {}
        try:
            for tup in cp.list(None, limit=scan_limit):
                conf = (tup.config or {}).get("configurable", {}) or {}
                tid = conf.get("thread_id")
                if not tid or tid in out:
                    continue  # 同一 thread 只认第一条（倒序 = 最新）
                values = (tup.checkpoint or {}).get("channel_values", {}) or {}
                messages = values.get("messages", []) or []
                out[tid] = {"thread_id": tid, "message_count": len(messages)}
        except Exception as exc:  # noqa: BLE001 —— 列不出来不该让接口报错
            log.warning("会话列表扫描失败：%s", exc)
            return []

        return sorted(out.values(), key=lambda x: x["thread_id"])
