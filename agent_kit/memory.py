"""记忆层：短期记忆（消息窗口 / Redis）与长期记忆（事实 / PostgreSQL）。

这两层解决的问题完全不同，别混为一谈：

  短期记忆 checkpointer —— 按 thread_id 保存**对话状态**，回答「这轮聊到哪了」。
      本项目用 **Redis** 存，并叠加**消息窗口**（只把最近 N 条喂给模型）：
        · Redis 负责「可恢复 + 可过期」（TTL），进程重启不丢；
        · 消息窗口负责「上下文不膨胀」，避免长对话把 token 吃光。
      无 Redis 时自动降级为 InMemorySaver（进程内，重启即失）。

  长期记忆 store —— 按 namespace 保存**跨会话事实**，回答「这个用户是谁」。
      本项目用 **PostgreSQL** 存（需要落盘、可 SQL 查询、能跨服务共享）。
      无 PG 时自动降级为 InMemoryStore。

环境变量（全部可选，不配就走内存降级）：
    SHORT_TERM_BACKEND   memory | sqlite | redis       默认：配了 REDIS_URL 就 redis
    LONG_TERM_BACKEND    memory | sqlite | postgres    默认：配了 PG_DSN 就 postgres
    REDIS_URL            redis://localhost:6379/0
    REDIS_TTL_MINUTES    checkpoint 过期分钟数，默认 10080（7 天）
    MEMORY_WINDOW        短期记忆消息窗口大小，默认 20 条
    PG_DSN               postgresql://user:pwd@host:5432/dbname
                         （也认 POSTGRES_URL / DATABASE_URL）
    MEMORY_STRICT        1 = 后端连不上直接报错；0（默认）= 告警并降级内存
"""

from __future__ import annotations

import atexit
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from agent_kit.logging_conf import get_logger

log = get_logger("agent.memory")

# 本次进程实际解析出的后端，供 `main.py info` / `ui.py /info` 展示
RESOLVED: dict[str, str] = {"short": "?", "long": "?"}

# 需要在进程退出时释放的资源（Postgres 连接池等）
_CLEANUPS: list[Any] = []


# ---------------------------------------------------------------------------
# 环境变量辅助
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r 不是整数，回落到 %d", name, raw, default)
        return default


def _dsn(*names: str) -> str | None:
    """按顺序找第一个非空的环境变量。"""
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return None


def _strict() -> bool:
    return os.getenv("MEMORY_STRICT", "0") in {"1", "true", "True", "yes"}


def _degrade(target: str, backend: str, exc: BaseException) -> bool:
    """后端不可用时怎么办。返回 True 表示调用方应改用内存实现。"""
    msg = f"{target} 后端 {backend} 不可用（{type(exc).__name__}: {exc}）"
    if _strict():
        raise RuntimeError(f"{msg}；MEMORY_STRICT=1 要求严格失败。") from exc
    log.warning("%s，已降级为内存模式（进程重启即丢失）", msg)
    return True


def default_dir() -> Path:
    """SQLite 落盘目录（runs/ 已被 .gitignore 忽略）。"""
    return Path(__file__).resolve().parent.parent / "runs"


def short_backend() -> str:
    """短期记忆后端：显式配置 > 有 REDIS_URL > 内存。"""
    return (os.getenv("SHORT_TERM_BACKEND") or "").lower() or ("redis" if _dsn("REDIS_URL") else "memory")


def long_backend() -> str:
    """长期记忆后端：显式配置 > 有 PG_DSN > 内存。"""
    has_pg = bool(_dsn("PG_DSN", "POSTGRES_URL", "DATABASE_URL"))
    return (os.getenv("LONG_TERM_BACKEND") or "").lower() or ("postgres" if has_pg else "memory")


def window_size() -> int:
    """短期记忆消息窗口大小（最近 N 条进入上下文）。"""
    return _env_int("MEMORY_WINDOW", 20)


# ---------------------------------------------------------------------------
# 短期记忆
# ---------------------------------------------------------------------------
def build_checkpointer(mode: str | None = None) -> Any:
    """短期记忆。redis 为主，sqlite 次之，兜底内存。"""
    mode = (mode or short_backend()).lower()

    if mode == "redis":
        url = _dsn("REDIS_URL")
        if not url:
            log.warning("SHORT_TERM_BACKEND=redis 但未设置 REDIS_URL，降级为内存模式")
        else:
            try:
                from langgraph.checkpoint.redis import RedisSaver

                _ping_redis(url)
                ttl_minutes = _env_int("REDIS_TTL_MINUTES", 60 * 24 * 7)

                if _redis_has_json(url):
                    saver = RedisSaver(
                        url,
                        ttl={"default_ttl": ttl_minutes, "refresh_on_read": True},
                        # redis-py 6+ 默认走 RESP3（先发 HELLO），Redis 5.x 不认识，
                        # 显式指定 RESP2 向下兼容（6/7 同样支持 RESP2）。
                        connection_args={"protocol": _env_int("REDIS_PROTOCOL", 2)},
                    )
                    try:
                        saver.setup()   # 建 RediSearch 索引；没有该模块只影响检索
                    except Exception as idx_exc:  # noqa: BLE001
                        log.warning("Redis 索引创建失败（%s），基础读写仍可用", idx_exc)
                    RESOLVED["short"] = f"redis[官方](ttl={ttl_minutes}min)"
                else:
                    from agent_kit.redis_checkpointer import PlainRedisSaver

                    saver = PlainRedisSaver(
                        url,
                        ttl_seconds=ttl_minutes * 60,
                        protocol=_env_int("REDIS_PROTOCOL", 2),
                    )
                    RESOLVED["short"] = f"redis[免模块](ttl={ttl_minutes}min)"
                    log.info("当前 Redis 无 RedisJSON，已启用免模块实现 redis_checkpointer.py")

                log.info("短期记忆 → Redis %s，TTL=%d 分钟", _hide_pwd(url), ttl_minutes)
                return saver
            except Exception as exc:  # noqa: BLE001
                _degrade("短期记忆", "redis", exc)

    if mode == "sqlite":
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver

            db_path = Path(os.getenv("SQLITE_PATH") or (default_dir() / "checkpoints.sqlite"))
            db_path.parent.mkdir(parents=True, exist_ok=True)
            RESOLVED["short"] = "sqlite"
            return SqliteSaver.from_conn_string(str(db_path))
        except Exception as exc:  # noqa: BLE001
            _degrade("短期记忆", "sqlite", exc)

    RESOLVED["short"] = "memory"
    return InMemorySaver()


def _redis_client(url: str, **extra: Any) -> Any:
    import redis

    return redis.Redis.from_url(
        url,
        socket_connect_timeout=3,
        socket_timeout=3,
        protocol=_env_int("REDIS_PROTOCOL", 2),   # 兼容 Redis 5.x
        **extra,
    )


def _ping_redis(url: str) -> None:
    """拨一次 PING，让「连不上」在启动时就暴露，而不是等到写 checkpoint 时才炸。"""
    client = _redis_client(url)
    try:
        client.ping()
    finally:
        client.close()


def probe() -> dict[str, dict[str, str]]:
    """**真实**探测两个记忆后端的连通性（不是只看配置里写了什么）。

    返回形如：
        {"short_term": {"backend": "redis", "alive": True, ...}, "long_term": {...}}

    用途：`/api/health` 探活、`python main.py check --ping` 自查。
    注意：这里是纯读探测（PING / 建连后立刻关闭），不会写任何数据。
    """
    result: dict[str, dict[str, str]] = {}

    # ---- 短期：Redis ----
    short = short_backend()
    url = os.getenv("REDIS_URL")
    info: dict[str, str] = {"backend": short}
    if not url:
        info.update(alive="False", reason="未配置 REDIS_URL")
    else:
        try:
            _ping_redis(url)
        except Exception as exc:  # noqa: BLE001
            info.update(alive="False", reason=f"{type(exc).__name__}: {exc}")
        else:
            info["alive"] = "True"
    result["short_term"] = info

    # ---- 长期：PostgreSQL ----
    long = long_backend()
    dsn = _dsn("PG_DSN", "POSTGRES_URL", "DATABASE_URL")
    pinfo: dict[str, str] = {"backend": long}
    if not dsn:
        pinfo.update(alive="False", reason="未配置 PG_DSN")
    else:
        try:
            import psycopg

            conn = psycopg.connect(_pg_dsn_with_timeout(dsn))
            conn.close()
        except Exception as exc:  # noqa: BLE001
            pinfo.update(alive="False", reason=f"{type(exc).__name__}: {exc}")
        else:
            pinfo["alive"] = "True"
    result["long_term"] = pinfo

    return result


def _redis_has_json(url: str) -> bool:
    """探测 Redis 是否带 RedisJSON 模块。

    官方 langgraph RedisSaver 依赖 JSON.SET / JSON.GET；
    Windows 上的 Redis 构建基本都不带这个模块，此时要走免模块实现。
    """
    client = _redis_client(url)
    try:
        names = {m.get("name", "").lower() for m in client.module_list()}
        return any("json" in n for n in names)
    except Exception:  # noqa: BLE001
        return False
    finally:
        client.close()


# ---------------------------------------------------------------------------
# 长期记忆
# ---------------------------------------------------------------------------
def build_store(mode: str | None = None) -> BaseStore:
    """长期记忆。postgres 为主，sqlite 次之，兜底内存。"""
    mode = (mode or long_backend()).lower()

    if mode == "postgres":
        dsn = _dsn("PG_DSN", "POSTGRES_URL", "DATABASE_URL")
        if not dsn:
            log.warning("LONG_TERM_BACKEND=postgres 但未设置 PG_DSN，降级为内存模式")
        else:
            try:
                from langgraph.store.postgres import PostgresStore

                # from_conn_string 返回的是上下文管理器；REPL 是长生命周期进程，
                # 这里手动 __enter__ 并在退出时关闭连接池。
                cm = PostgresStore.from_conn_string(_pg_dsn_with_timeout(dsn))
                store = cm.__enter__()
                atexit.register(_close_quietly, cm)
                store.setup()  # 建表（幂等）
                RESOLVED["long"] = "postgres"
                log.info("长期记忆 → PostgreSQL %s", _hide_pwd(dsn))
                return store
            except Exception as exc:  # noqa: BLE001
                _degrade("长期记忆", "postgres", exc)

    if mode == "sqlite":
        try:
            from langgraph.store.sqlite import SqliteStore

            db_path = Path(os.getenv("SQLITE_PATH") or (default_dir() / "store.sqlite"))
            db_path.parent.mkdir(parents=True, exist_ok=True)
            RESOLVED["long"] = "sqlite"
            return SqliteStore.from_conn_string(str(db_path))
        except Exception as exc:  # noqa: BLE001
            _degrade("长期记忆", "sqlite", exc)

    RESOLVED["long"] = "memory"
    return InMemoryStore()


def _close_quietly(cm: Any) -> None:
    """关连接池 / 上下文管理器，失败也别把进程退出流程崩掉。"""
    try:
        cm.__exit__(None, None, None)
    except Exception as exc:  # noqa: BLE001
        # 静默 ≠ 无声：至少落一条 debug，真出问题才有线索
        log.debug("关闭资源时出错（已忽略）：%s: %s", type(exc).__name__, exc)


def _pg_dsn_with_timeout(dsn: str) -> str:
    """给 PG 连接串补 connect_timeout。

    libpq 默认没有连接超时，机器/端口不通时会一直挂着，
    让「数据库没起来」表现为卡死而不是报错。这里强制补一个短超时。
    """
    timeout = _env_int("PG_CONNECT_TIMEOUT", 5)
    if "connect_timeout" in dsn:
        return dsn
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}connect_timeout={timeout}"


def _hide_pwd(dsn: str) -> str:
    """打码连接串里的密码，避免进日志。"""
    if "@" not in dsn:
        return dsn
    head, _, tail = dsn.partition("://")
    if not tail:
        return dsn
    userinfo, _, host = tail.rpartition("@")
    if ":" in userinfo:
        user, _, _pwd = userinfo.partition(":")
        userinfo = f"{user}:***"
    return f"{head}://{userinfo}@{host}"


# ---------------------------------------------------------------------------
# 消息窗口：短期记忆的「上下文侧」策略
# ---------------------------------------------------------------------------
def make_message_window(window: int | None = None) -> Any:
    """只把最近 N 条消息喂给模型，其余仍留在 Redis 里可回溯。

    为什么不用「直接截断 state」：
      · AI 消息带 tool_calls 时，紧跟的 ToolMessage 必须一起留，否则 API 报错；
      · 系统消息要保留。
    所以这里用 langchain 的 trim_messages，而不是 list[-N:]。
    """
    from langchain.agents.middleware import before_model
    from langchain_core.messages import trim_messages

    size = window or window_size()

    @before_model
    def _window(state: Any, runtime: Any) -> dict | None:
        msgs = state.get("messages", []) if isinstance(state, dict) else []
        if len(msgs) <= size:
            return None
        trimmed = trim_messages(
            msgs,
            token_counter=len,          # 按「条数」而不是 token 数计
            max_tokens=size,
            strategy="last",
            start_on="human",           # 从一条人类消息开始，不切断对话语义
            end_on=("human", "tool"),   # 结尾落在完整轮次边界
            include_system=True,        # 系统消息永远保留
            allow_partial=False,
        )
        dropped = len(msgs) - len(trimmed)
        if dropped > 0:
            log.info("消息窗口：%d 条 → %d 条（丢弃最早的 %d 条，仍在 Redis 中可回溯）", len(msgs), len(trimmed), dropped)
        return {"messages": trimmed}

    return _window


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def seed_long_term_memory(store: BaseStore, user_id: str = "demo", overwrite: bool = False) -> None:
    """预置几条用户偏好，用来演示「跨会话」效果。真实场景里由工具写入。

    接了 PostgreSQL 后每次启动都覆盖写一遍没有意义（还会刷新 updated_at），
    所以默认 overwrite=False：该用户已有偏好就直接跳过。
    """
    if not overwrite and store.search(("preferences", user_id)):
        return

    now = datetime.now(timezone.utc).astimezone().isoformat()
    for key, value in {
        "language": "简体中文",
        "tone": "结构化表达，善用表格对比",
        "domain": "区块链安全 / 联邦图神经网络",
    }.items():
        store.put(("preferences", user_id), key, {"value": value, "updated_at": now})


def thread_config(thread_id: str = "demo-thread") -> dict[str, Any]:
    """thread_id 是短期记忆的唯一主键。

    同一个 thread_id = 同一段对话；换一个 thread_id = 全新会话。
    """
    return {"configurable": {"thread_id": thread_id}}


def new_thread_id(prefix: str = "thread") -> str:
    """生成一个新的会话 ID。"""
    return os.getenv("THREAD_ID") or f"{prefix}-{uuid.uuid4().hex[:8]}"


def pad_display(text: str, width: int = 14) -> str:
    """按「显示宽度」补空格（中文占 2 列，直接 ljust 会歪）。"""
    import unicodedata

    used = sum(2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1 for ch in text)
    return text + " " * max(0, width - used)


def report() -> dict[str, Any]:
    """给 `info` / `/mem` 用的记忆层概况。"""
    short = RESOLVED.get("short", "?")
    long_ = RESOLVED.get("long", "?")
    return {
        "短期记忆后端": short if short != "?" else f"{short_backend()}（未初始化）",
        "长期记忆后端": long_ if long_ != "?" else f"{long_backend()}（未初始化）",
        "消息窗口": f"{window_size()} 条",
        "REDIS_URL": _hide_pwd(_dsn("REDIS_URL") or "未配置"),
        "PG_DSN": _hide_pwd(_dsn("PG_DSN", "POSTGRES_URL", "DATABASE_URL") or "未配置"),
        "严格模式": "开" if _strict() else "关（连不上则降级内存）",
    }


def report_lines() -> list[str]:
    """已经按显示宽度对齐好的行，直接 print 即可。"""
    return [f"    {pad_display(k)}{v}" for k, v in report().items()]
