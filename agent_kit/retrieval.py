"""检索增强（RAG）：切分 → 向量化 → 索引 → 检索。

为什么要有这个模块
--------------------
Agent 只靠「关键词字面匹配」检索时，问「怎么复习才记得住」是搜不到
`forgetting-curve.md` 的——因为笔记里写的是「遗忘曲线」「间隔重复」。
语义检索解决的正是这个「说法不同但意思相同」的问题。

embedding 必须能降级
--------------------
CI 与测试环境没有 API Key，所以向量化分两条路：

  1. `DashScopeEmbedder` —— 真实语义向量，需要 `DASHSCOPE_API_KEY`；
  2. `HashingEmbedder`   —— 零依赖离线实现，无 Key 时自动降级。

降级是**显式**的：`get_embedder()` 会返回降级原因，调用方可以展示给用户，
而不是悄悄给出一个质量差很多的结果。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

DEFAULT_DIM = 384
DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 80

_WORD_RE = re.compile(r"[a-z0-9_]+")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


@dataclass(frozen=True)
class Chunk:
    """一个可检索的文本片段。"""

    doc_id: str
    text: str
    order: int


@dataclass(frozen=True)
class Hit:
    """一条检索结果。"""

    doc_id: str
    text: str
    score: float
    order: int


# ---------------------------------------------------------------------------
# 1) 切分
# ---------------------------------------------------------------------------
def chunk_text(
    text: str,
    doc_id: str = "",
    *,
    size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[Chunk]:
    """按字符数滑窗切分，优先在句子边界断开。

    为什么不用更聪明的方法：固定滑窗的召回更稳，而且不依赖任何分词库。
    代价是可能从句子中间切开——用 overlap 补回来。
    """
    if size <= 0:
        raise ValueError("size 必须为正")
    if overlap >= size:
        raise ValueError("overlap 必须小于 size")

    clean = text.strip()
    if not clean:
        return []

    chunks: list[Chunk] = []
    start = 0
    step = size - overlap
    order = 0
    while start < len(clean):
        end = min(start + size, len(clean))
        piece = clean[start:end]
        # 尽量在句末断开，避免在词中间硬切
        if end < len(clean):
            for sep in ("\n\n", "\n", "。", "；", "！", "？", ". "):
                idx = piece.rfind(sep)
                if idx > size * 0.5:
                    piece = piece[: idx + len(sep)]
                    break
        piece = piece.strip()
        if piece:
            chunks.append(Chunk(doc_id=doc_id, text=piece, order=order))
            order += 1
        if end >= len(clean):
            break
        start += max(step, 1)
    return chunks


# ---------------------------------------------------------------------------
# 2) 向量化
# ---------------------------------------------------------------------------
def _tokens(text: str) -> list[str]:
    """中英文混合分词：英文按词，中文按字 + 相邻二字组。

    中文用 bigram 是为了保留一点词序信息——「遗忘曲线」和「曲线遗忘」
    在纯 unigram 下完全一样，加了 bigram 就能区分。
    """
    low = text.lower()
    words = _WORD_RE.findall(low)
    han = _HAN_RE.findall(low)
    bigrams = [a + b for a, b in itertools.pairwise(han)]
    return words + han + bigrams


class HashingEmbedder:
    """零依赖离线向量化（hashing trick）。

    不是语义模型——它只保证「词面越像、向量越近」。
    价值在于：无 Key、无网络、无额外依赖时，检索链路依然完整可跑、可测。
    """

    name = "hashing"

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        if dim <= 0:
            raise ValueError("dim 必须为正")
        self.dim = dim

    def _hash(self, token: str) -> int:
        # 必须用 hashlib：内置 hash() 对 str 有进程级随机化，
        # 换一次进程索引就全失效了。
        return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            # 词频单独统计（无符号），符号只由哈希决定。
            # 早期版本把带符号的值累加后再取 log，两个符号相反的碰撞词会抵消成 0，
            # 于是 log(0) = -inf，整条向量变成 NaN。词频恒 >= 1 就不会有这个问题。
            tf: dict[str, int] = {}
            for token in _tokens(text):
                tf[token] = tf.get(token, 0) + 1
            for token, cnt in tf.items():
                h = self._hash(token)
                idx = h % self.dim
                sign = 1.0 if (h >> 40) & 1 else -1.0
                # 次线性 tf：同一个词出现 10 次不该有 10 倍权重
                out[i, idx] += sign * (1.0 + np.log(cnt))
        return _l2_normalize(out)


class DashScopeEmbedder:
    """阿里云百炼的真实语义向量（OpenAI 兼容接口）。

    注意：本实现依赖环境变量 `DASHSCOPE_API_KEY`，需要联网调用。
    在无 Key / 网络不可用时由 `get_embedder()` 自动降级到 HashingEmbedder。
    """

    name = "dashscope"

    def __init__(
        self,
        model: str = "text-embedding-v3",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        import os

        self.model = model
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        self.base_url = base_url or os.getenv(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        if not self.api_key:
            raise ValueError("缺少 DASHSCOPE_API_KEY，无法使用真实向量")
        try:
            from langchain_openai import OpenAIEmbeddings
        except ImportError as exc:  # pragma: no cover - 依赖缺失时才触发
            raise RuntimeError("未安装 langchain-openai，无法使用真实向量") from exc
        self._client = OpenAIEmbeddings(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
        )

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        vectors = self._client.embed_documents(list(texts))
        arr = np.asarray(vectors, dtype=np.float32)
        if arr.ndim != 2:
            raise RuntimeError(f"embedding 返回形状异常：{arr.shape}")
        return _l2_normalize(arr)


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    """归一化后余弦相似度就等于点积，检索时省一次除法。"""
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


@dataclass(frozen=True)
class EmbedderChoice:
    embedder: HashingEmbedder | DashScopeEmbedder
    backend: str
    degraded: bool
    reason: str


def get_embedder(backend: str = "auto", *, dim: int = DEFAULT_DIM) -> EmbedderChoice:
    """按可用性选择向量化后端。

    backend: auto（有 Key 用真实，否则降级）/ dashscope（强制真实）/ hashing（强制离线）
    """
    if backend == "hashing":
        return EmbedderChoice(HashingEmbedder(dim), "hashing", False, "显式指定离线后端")

    if backend in ("auto", "dashscope"):
        try:
            emb = DashScopeEmbedder()
            return EmbedderChoice(emb, "dashscope", False, "使用真实语义向量")
        except Exception as exc:
            if backend == "dashscope":
                raise
            return EmbedderChoice(
                HashingEmbedder(dim), "hashing", True, f"真实向量不可用（{exc}），已降级"
            )

    raise ValueError(f"未知的 embedding 后端：{backend}")


# ---------------------------------------------------------------------------
# 3) 索引与检索
# ---------------------------------------------------------------------------
class VectorIndex:
    """numpy 暴力检索。

    为什么不用 FAISS / Chroma：笔记库只有几十篇文档，线性扫描的耗时可忽略，
    却能省掉一个在 Windows 上经常装不上的重依赖。
    """

    def __init__(self, vectors: np.ndarray, chunks: Sequence[Chunk]) -> None:
        if len(vectors) != len(chunks):
            raise ValueError("向量数与片段数不一致")
        self.vectors = np.asarray(vectors, dtype=np.float32)
        self.chunks = list(chunks)

    def __len__(self) -> int:
        return len(self.chunks)

    def search(self, query_vec: np.ndarray, top_k: int = 3) -> list[Hit]:
        if not self.chunks:
            return []
        q = np.asarray(query_vec, dtype=np.float32).reshape(-1)
        scores = self.vectors @ q
        k = min(max(top_k, 1), len(self.chunks))
        # argpartition 比 argsort 快，但这里数据量小，用 argsort 更直观且稳定排序
        order = np.argsort(-scores)[:k]
        return [
            Hit(
                doc_id=self.chunks[i].doc_id,
                text=self.chunks[i].text,
                score=float(scores[i]),
                order=self.chunks[i].order,
            )
            for i in order
            if float(scores[i]) > 0
        ]

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(path.with_suffix(".npy"), self.vectors)
        meta = [
            {"doc_id": c.doc_id, "text": c.text, "order": c.order} for c in self.chunks
        ]
        path.with_suffix(".json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    @classmethod
    def load(cls, path: str | Path) -> VectorIndex:
        path = Path(path)
        vectors = np.load(path.with_suffix(".npy"))
        meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        chunks = [Chunk(doc_id=m["doc_id"], text=m["text"], order=m["order"]) for m in meta]
        return cls(vectors, chunks)


def build_index(
    docs: Iterable[tuple[str, str]],
    embedder: HashingEmbedder | DashScopeEmbedder,
    *,
    size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> VectorIndex:
    """把 (doc_id, text) 列表变成可检索的索引。"""
    chunks: list[Chunk] = []
    for doc_id, text in docs:
        chunks.extend(chunk_text(text, doc_id=doc_id, size=size, overlap=overlap))
    if not chunks:
        return VectorIndex(np.zeros((0, 1), dtype=np.float32), [])
    vectors = embedder.embed([c.text for c in chunks])
    return VectorIndex(vectors, chunks)


def load_docs(notes_dir: str | Path) -> list[tuple[str, str]]:
    """从目录加载 .md 语料。"""
    base = Path(notes_dir)
    if not base.exists():
        return []
    return [
        (p.stem, p.read_text(encoding="utf-8", errors="ignore"))
        for p in sorted(base.glob("*.md"))
    ]
