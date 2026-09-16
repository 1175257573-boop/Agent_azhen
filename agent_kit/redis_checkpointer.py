"""免模块版 Redis checkpointer（短期记忆）。

**为什么要有这个文件**

官方 `langgraph-checkpoint-redis` 用 `JSON.SET / JSON.GET` 存 checkpoint，
依赖 **RedisJSON** 模块。而 Windows 上的 Redis 构建（5.0 / 8.x 社区版）都不带这个模块，
一调用就报 `unknown command 'json.set'`。

这个文件用**原生 STRING / HASH 命令**实现同样的 `BaseCheckpointSaver` 接口，
不依赖任何 Redis 模块，因此：
  · 官方实现可用（Redis Stack / Linux RedisJSON）时优先用官方；
  · 纯 Redis（Windows 本地开发、云 Redis 基础版）时自动切到这里。
上层代码完全无感，都是 `checkpointer=xxx`。

数据布局（key 前缀默认 `atlas:ck`）：
  atlas:ck:{thread}:{ns}:seq            LIST，按写入顺序记录 checkpoint_id，末位即最新
  atlas:ck:{thread}:{ns}:{cpid}         STRING，checkpoint 主体（不含 channel_values）
  atlas:ck:{thread}:{ns}:{cpid}:meta    STRING，元数据 + 父 checkpoint_id
  atlas:ck:{thread}:{ns}:{cpid}:w       HASH，pending writes：field={task_id}:{idx}
  atlas:ck:{thread}:{ns}:blob         HASH，channel 值：field={channel}:{version}
每个 key 都会按 ttl_seconds 设置过期时间。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    SerializerProtocol,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

SEP = ":"


class PlainRedisSaver(BaseCheckpointSaver[str]):
    """只用原生命令的 Redis checkpointer，不依赖 RedisJSON / RediSearch。"""

    def __init__(
        self,
        redis_url: str = "redis://127.0.0.1:6379/0",
        *,
        prefix: str = "atlas:ck",
        ttl_seconds: int | None = None,
        serde: SerializerProtocol | None = None,
        protocol: int = 2,
    ) -> None:
        super().__init__(serde=serde or JsonPlusSerializer())
        import redis

        self._redis = redis.Redis.from_url(
            redis_url,
            socket_connect_timeout=3,
            socket_timeout=5,
            protocol=protocol,      # Redis 5.x 不认识 RESP3 的 HELLO
            decode_responses=False,
        )
        self._redis.ping()
        self.prefix = prefix
        self.ttl = ttl_seconds
        self._url = redis_url
        self._protocol = protocol
        self._aredis_client: Any = None

    # ------------------------------------------------------------ 序列化
    # serde.dumps_typed() 返回 (type, bytes) 元组，Redis 只能存字节，
    # 所以外面再包一层 {"t": type, "b": blob}。
    def _dump_typed(self, value: Any) -> bytes:
        typ, blob = self.serde.dumps_typed(value)
        return typ.encode("utf-8") + b"\x00" + blob

    def _load_typed(self, raw: bytes) -> Any:
        typ, _, blob = raw.partition(b"\x00")
        return self.serde.loads_typed((typ.decode("utf-8"), blob))

    def _aredis(self) -> Any:
        """异步客户端（懒加载）。

        LangGraph 走 `astream` 时会调 `aget_tuple / aput / aput_writes`，
        基类默认实现直接 `raise NotImplementedError`，所以这里必须自己提供。
        """
        if self._aredis_client is None:
            import redis.asyncio as aioredis

            self._aredis_client = aioredis.Redis.from_url(
                self._url,
                socket_connect_timeout=3,
                socket_timeout=5,
                protocol=self._protocol,
                decode_responses=False,
            )
        return self._aredis_client

    # ------------------------------------------------------------ key 工具
    def _k(self, thread: str, ns: str, *parts: str) -> str:
        return SEP.join((self.prefix, thread, ns or "", *parts))

    def _expire(self, *keys: str) -> None:
        if not self.ttl:
            return
        pipe = self._redis.pipeline(transaction=False)
        for k in keys:
            pipe.expire(k, self.ttl)
        pipe.execute()

    # ------------------------------------------------------------ 读
    def _load_blobs(self, thread: str, ns: str, versions: ChannelVersions) -> dict[str, Any]:
        if not versions:
            return {}
        # blobs 按 (thread, ns) 共享：同一会话的多个 checkpoint 复用同一份 channel 值
        key = self._k(thread, ns, "blob")
        fields = [f"{c}{SEP}{v}" for c, v in versions.items()]
        raw = self._redis.hmget(key, fields)
        out: dict[str, Any] = {}
        for (channel, _ver), blob in zip(versions.items(), raw):
            if blob is None or blob == b"empty":
                continue
            out[channel] = self._load_typed(blob)
        return out

    def _load_writes(self, thread: str, ns: str, cpid: str) -> list[tuple[str, str, Any]]:
        raw = self._redis.hgetall(self._k(thread, ns, cpid, "w"))
        out: list[tuple[str, str, Any]] = []
        for field, blob in raw.items():
            task_id = field.decode().split(SEP)[0] if isinstance(field, bytes) else field.split(SEP)[0]
            _tid, channel, value = self._load_typed(blob)
            out.append((task_id, channel, value))
        return out

    def _build_tuple(
        self,
        thread: str,
        ns: str,
        cpid: str,
        body: bytes,
        meta_raw: bytes | None,
        blobs: dict[str, Any] | None = None,
        writes: list[tuple[str, str, Any]] | None = None,
    ) -> CheckpointTuple | None:
        """同步/异步两条链路共用；异步侧把预取好的 blobs / writes 传进来。"""
        if body is None:
            return None
        checkpoint: Checkpoint = self._load_typed(body)
        parent_id = None
        metadata: CheckpointMetadata = {}
        if meta_raw:
            meta = self._load_typed(meta_raw)
            parent_id = meta.get("parent")
            metadata = meta.get("metadata") or {}
        if blobs is None:
            blobs = self._load_blobs(thread, ns, checkpoint.get("channel_versions", {}))
        if writes is None:
            writes = self._load_writes(thread, ns, cpid)
        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread,
                    "checkpoint_ns": ns,
                    "checkpoint_id": cpid,
                }
            },
            checkpoint={**checkpoint, "channel_values": blobs},
            metadata=metadata,
            pending_writes=writes,
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread,
                        "checkpoint_ns": ns,
                        "checkpoint_id": parent_id,
                    }
                }
                if parent_id
                else None
            ),
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")

        cpid = get_checkpoint_id(config)
        if not cpid:
            seq = self._redis.lrange(self._k(thread, ns, "seq"), -1, -1)
            if not seq:
                return None
            cpid = seq[0].decode()

        body = self._redis.get(self._k(thread, ns, cpid))
        if body is None:
            return None
        meta_raw = self._redis.get(self._k(thread, ns, cpid, "meta"))
        return self._build_tuple(thread, ns, cpid, body, meta_raw)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        thread = (config or {}).get("configurable", {}).get("thread_id")
        if thread is None:
            return
        ns = (config or {}).get("configurable", {}).get("checkpoint_ns", "")
        ids = [i.decode() for i in self._redis.lrange(self._k(thread, ns, "seq"), 0, -1)]
        ids.reverse()  # 最新的在前

        n = 0
        for cpid in ids:
            if limit is not None and n >= limit:
                return
            if before and get_checkpoint_id(before) and cpid >= get_checkpoint_id(before):
                continue
            body = self._redis.get(self._k(thread, ns, cpid))
            if body is None:
                continue
            tup = self._build_tuple(thread, ns, cpid, body, self._redis.get(self._k(thread, ns, cpid, "meta")))
            if tup is None:
                continue
            if filter and any(tup.metadata.get(k) != v for k, v in filter.items()):
                continue
            n += 1
            yield tup

    # ------------------------------------------------------------ 写
    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        cpid = checkpoint["id"]

        body = dict(checkpoint)
        values = body.pop("channel_values", {}) or {}

        pipe = self._redis.pipeline(transaction=False)
        # channel 值单独存 hash，按 channel:version 粒度复用
        blob_key = self._k(thread, ns, "blob")
        for channel, version in (new_versions or {}).items():
            field = f"{channel}{SEP}{version}"
            if channel in values:
                pipe.hset(blob_key, field, self._dump_typed(values[channel]))
            else:
                pipe.hset(blob_key, field, b"empty")

        pipe.set(self._k(thread, ns, cpid), self._dump_typed(body))
        pipe.set(
            self._k(thread, ns, cpid, "meta"),
            self._dump_typed(
                {"metadata": get_checkpoint_metadata(config, metadata),
                 "parent": config["configurable"].get("checkpoint_id")}
            ),
        )
        # 顺序索引：已存在就不再重复追加（同一 checkpoint 可能被多次 put）
        seq_key = self._k(thread, ns, "seq")
        existing = self._redis.lrange(seq_key, -1, -1)
        if not existing or existing[0].decode() != cpid:
            pipe.rpush(seq_key, cpid)
        pipe.execute()

        self._expire(
            self._k(thread, ns, cpid),
            self._k(thread, ns, cpid, "meta"),
            blob_key,
            seq_key,
        )
        return {
            "configurable": {
                "thread_id": thread,
                "checkpoint_ns": ns,
                "checkpoint_id": cpid,
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        cpid = config["configurable"]["checkpoint_id"]
        if not cpid:
            return

        key = self._k(thread, ns, cpid, "w")
        pipe = self._redis.pipeline(transaction=False)
        for idx, (channel, value) in enumerate(writes):
            inner = WRITES_IDX_MAP.get(channel, idx)
            if inner < 0:
                continue
            field = f"{task_id}{SEP}{inner}"
            if self._redis.hexists(key, field):
                continue
            pipe.hset(key, field, self._dump_typed((task_id, channel, value)))
        pipe.execute()
        self._expire(key)

    # ------------------------------------------------------------ 维护
    def delete_thread(self, thread_id: str) -> None:
        keys: list[str] = []
        for k in self._redis.scan_iter(match=f"{self.prefix}{SEP}{thread_id}{SEP}*"):
            keys.append(k.decode() if isinstance(k, bytes) else k)
        if keys:
            self._redis.delete(*keys)

    def thread_count(self) -> int:
        """当前 Redis 里有多少个会话（调试用）。"""
        seq_keys = list(self._redis.scan_iter(match=f"{self.prefix}{SEP}*{SEP}seq"))
        return len(seq_keys)

    def list_threads(self) -> list[dict[str, Any]]:
        """列出所有会话及其消息条数（Web UI 的会话列表用）。

        key 形如 atlas:ck:{thread}:{ns}:seq，反解出 thread 与 ns。
        """

        out: list[dict[str, Any]] = []
        for raw in self._redis.scan_iter(match=f"{self.prefix}{SEP}*{SEP}seq"):
            key = raw.decode() if isinstance(raw, bytes) else raw
            parts = key.split(SEP)
            # [prefix, ck, ...thread..., ns, seq] —— thread 里可能自带 ':'
            if len(parts) < 5:
                continue
            thread_id = SEP.join(parts[2:-2])
            ns = parts[-2]
            if not thread_id:
                continue
            count = 0
            latest = self.get_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ns}})
            if latest:
                count = len(latest.checkpoint.get("channel_values", {}).get("messages", []) or [])
            out.append({"thread_id": thread_id, "namespace": ns, "message_count": count})
        out.sort(key=lambda x: x["thread_id"])
        return out

    # ============================================================
    # 异步接口：LangGraph 的 astream / ainvoke 只会走这一套
    # ============================================================
    async def _aexpire(self, *keys: str) -> None:
        if not self.ttl:
            return
        pipe = self._aredis().pipeline(transaction=False)
        for k in keys:
            pipe.expire(k, self.ttl)
        await pipe.execute()

    async def _aload_blobs(self, thread: str, ns: str, versions: ChannelVersions) -> dict[str, Any]:
        if not versions:
            return {}
        key = self._k(thread, ns, "blob")
        fields = [f"{c}{SEP}{v}" for c, v in versions.items()]
        raw = await self._aredis().hmget(key, fields)
        out: dict[str, Any] = {}
        for (channel, _ver), blob in zip(versions.items(), raw):
            if blob is None or blob == b"empty":
                continue
            out[channel] = self._load_typed(blob)
        return out

    async def _aload_writes(self, thread: str, ns: str, cpid: str) -> list[tuple[str, str, Any]]:
        raw = await self._aredis().hgetall(self._k(thread, ns, cpid, "w"))
        out: list[tuple[str, str, Any]] = []
        for field, blob in raw.items():
            task_id = field.decode().split(SEP)[0] if isinstance(field, bytes) else field.split(SEP)[0]
            _tid, channel, value = self._load_typed(blob)
            out.append((task_id, channel, value))
        return out

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        r = self._aredis()
        thread = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")

        cpid = get_checkpoint_id(config)
        if not cpid:
            seq = await r.lrange(self._k(thread, ns, "seq"), -1, -1)
            if not seq:
                return None
            cpid = seq[0].decode()

        body, meta_raw = await r.mget([self._k(thread, ns, cpid), self._k(thread, ns, cpid, "meta")])
        if body is None:
            return None

        checkpoint: Checkpoint = self._load_typed(body)
        blobs, writes = await asyncio.gather(
            self._aload_blobs(thread, ns, checkpoint.get("channel_versions", {})),
            self._aload_writes(thread, ns, cpid),
        )
        return self._build_tuple(thread, ns, cpid, body, meta_raw, blobs=blobs, writes=writes)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        r = self._aredis()
        thread = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        cpid = checkpoint["id"]

        body = dict(checkpoint)
        values = body.pop("channel_values", {}) or {}

        pipe = r.pipeline(transaction=False)
        blob_key = self._k(thread, ns, "blob")
        for channel, version in (new_versions or {}).items():
            field = f"{channel}{SEP}{version}"
            pipe.hset(blob_key, field, self._dump_typed(values[channel]) if channel in values else b"empty")

        pipe.set(self._k(thread, ns, cpid), self._dump_typed(body))
        pipe.set(
            self._k(thread, ns, cpid, "meta"),
            self._dump_typed(
                {"metadata": get_checkpoint_metadata(config, metadata),
                 "parent": config["configurable"].get("checkpoint_id")}
            ),
        )
        seq_key = self._k(thread, ns, "seq")
        existing = await r.lrange(seq_key, -1, -1)
        if not existing or existing[0].decode() != cpid:
            pipe.rpush(seq_key, cpid)
        await pipe.execute()

        await self._aexpire(self._k(thread, ns, cpid), self._k(thread, ns, cpid, "meta"), blob_key, seq_key)
        return {"configurable": {"thread_id": thread, "checkpoint_ns": ns, "checkpoint_id": cpid}}

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        r = self._aredis()
        thread = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        cpid = config["configurable"].get("checkpoint_id")
        if not cpid:
            return

        key = self._k(thread, ns, cpid, "w")
        pipe = r.pipeline(transaction=False)
        for idx, (channel, value) in enumerate(writes):
            inner = WRITES_IDX_MAP.get(channel, idx)
            if inner < 0:
                continue
            field = f"{task_id}{SEP}{inner}"
            if await r.hexists(key, field):
                continue
            pipe.hset(key, field, self._dump_typed((task_id, channel, value)))
        await pipe.execute()
        await self._aexpire(key)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        r = self._aredis()
        thread = (config or {}).get("configurable", {}).get("thread_id")
        if thread is None:
            return
        ns = (config or {}).get("configurable", {}).get("checkpoint_ns", "")
        ids = [i.decode() for i in await r.lrange(self._k(thread, ns, "seq"), 0, -1)]
        ids.reverse()

        n = 0
        for cpid in ids:
            if limit is not None and n >= limit:
                return
            if before and get_checkpoint_id(before) and cpid >= get_checkpoint_id(before):
                continue
            body, meta_raw = await r.mget([self._k(thread, ns, cpid), self._k(thread, ns, cpid, "meta")])
            if body is None:
                continue
            checkpoint: Checkpoint = self._load_typed(body)
            blobs, writes = await asyncio.gather(
                self._aload_blobs(thread, ns, checkpoint.get("channel_versions", {})),
                self._aload_writes(thread, ns, cpid),
            )
            tup = self._build_tuple(thread, ns, cpid, body, meta_raw, blobs=blobs, writes=writes)
            if tup is None:
                continue
            if filter and any(tup.metadata.get(k) != v for k, v in filter.items()):
                continue
            n += 1
            yield tup

    async def adelete_thread(self, thread_id: str) -> None:
        r = self._aredis()
        keys: list[str] = []
        async for k in r.scan_iter(match=f"{self.prefix}{SEP}{thread_id}{SEP}*"):
            keys.append(k.decode() if isinstance(k, bytes) else k)
        if keys:
            await r.delete(*keys)
