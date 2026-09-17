"""检索模块测试。

刻意不依赖任何 API Key：全部走 HashingEmbedder 离线路径。
真实向量（DashScope）只在运行时按可用性启用，测试里碰不到。
"""

from __future__ import annotations

import numpy as np
import pytest

from agent_kit.retrieval import (
    Chunk,
    DashScopeEmbedder,
    HashingEmbedder,
    VectorIndex,
    build_index,
    chunk_text,
    get_embedder,
    load_docs,
)


# ---------------------------------------------------------------------------
# 切分
# ---------------------------------------------------------------------------
def test_chunk_text_empty_returns_nothing():
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == []


def test_chunk_text_short_text_kept_whole():
    chunks = chunk_text("一句话。", doc_id="d", size=500)
    assert len(chunks) == 1
    assert chunks[0].text == "一句话。"
    assert chunks[0].doc_id == "d"


def test_chunk_text_splits_with_overlap():
    text = "甲" * 300
    chunks = chunk_text(text, doc_id="d", size=100, overlap=20)
    assert len(chunks) > 1
    # 有 overlap，相邻片段的总长一定大于原文
    assert sum(len(c.text) for c in chunks) >= len(text)


def test_chunk_text_rejects_bad_params():
    with pytest.raises(ValueError):
        chunk_text("x", size=0)
    with pytest.raises(ValueError):
        chunk_text("x", size=10, overlap=10)


def test_chunk_text_breaks_at_sentence_boundary():
    text = "第一句。" * 50
    chunk = chunk_text(text, doc_id="d", size=120, overlap=20)[0]
    assert chunk.text.endswith("。")


# ---------------------------------------------------------------------------
# 向量化
# ---------------------------------------------------------------------------
def test_hashing_is_deterministic_across_instances():
    a = HashingEmbedder(dim=64).embed(["遗忘曲线与间隔重复"])
    b = HashingEmbedder(dim=64).embed(["遗忘曲线与间隔重复"])
    assert np.array_equal(a, b)


def test_hashing_output_is_normalized():
    vec = HashingEmbedder(dim=64).embed(["任意文本 abc 123"])
    assert np.isclose(float(np.linalg.norm(vec[0])), 1.0)


def test_hashing_identical_text_has_similarity_one():
    emb = HashingEmbedder(dim=64)
    a = emb.embed(["艾宾浩斯遗忘曲线"])[0]
    b = emb.embed(["艾宾浩斯遗忘曲线"])[0]
    assert np.isclose(float(a @ b), 1.0)


def test_hashing_unrelated_text_scores_lower_than_related():
    emb = HashingEmbedder(dim=256)
    q = emb.embed(["遗忘曲线 复习"])[0]
    related = emb.embed(["遗忘曲线与复习计划安排"])[0]
    unrelated = emb.embed(["MCP 协议 传输层 stdio"])[0]
    assert float(q @ related) > float(q @ unrelated)


def test_hashing_uses_bigram_so_word_order_matters():
    emb = HashingEmbedder(dim=256)
    a = emb.embed(["遗忘曲线"])[0]
    b = emb.embed(["曲线遗忘"])[0]
    # 纯 unigram 下两者完全相同；加了 bigram 之后应当能区分
    assert not np.isclose(float(a @ b), 1.0)


def test_dashscope_requires_api_key(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(ValueError):
        DashScopeEmbedder()


# ---------------------------------------------------------------------------
# 后端选择
# ---------------------------------------------------------------------------
def test_get_embedder_hashing_is_explicit():
    choice = get_embedder("hashing")
    assert choice.backend == "hashing"
    assert choice.degraded is False


def test_get_embedder_auto_degrades_without_key(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    choice = get_embedder("auto")
    assert choice.backend == "hashing"
    # 降级必须显式可见，不能悄悄换后端
    assert choice.degraded is True
    assert choice.reason


def test_get_embedder_rejects_unknown_backend():
    with pytest.raises(ValueError):
        get_embedder("nope")


# ---------------------------------------------------------------------------
# 索引与检索
# ---------------------------------------------------------------------------
def test_index_rejects_mismatched_shapes():
    with pytest.raises(ValueError):
        VectorIndex(np.zeros((2, 4), dtype=np.float32), [Chunk("d", "t", 0)])


def test_search_returns_empty_for_empty_index():
    idx = VectorIndex(np.zeros((0, 1), dtype=np.float32), [])
    assert idx.search(np.ones(1, dtype=np.float32)) == []
    assert len(idx) == 0


def test_search_ranks_by_similarity():
    emb = HashingEmbedder(dim=256)
    idx = build_index(
        [("a", "Redis 记忆窗口与 TTL"), ("b", "MCP 传输层 stdio 与 HTTP")],
        emb,
        size=200,
        overlap=20,
    )
    hits = idx.search(emb.embed(["Redis 过期时间"])[0], top_k=2)
    assert hits
    assert hits[0].doc_id == "a"
    assert hits[0].score >= hits[-1].score


def test_search_respects_top_k():
    emb = HashingEmbedder(dim=128)
    idx = build_index([(f"d{i}", f"第 {i} 篇关于检索的文档内容") for i in range(6)], emb)
    assert len(idx.search(emb.embed(["检索"])[0], top_k=2)) <= 2


def test_build_index_on_empty_corpus_is_safe():
    idx = build_index([], HashingEmbedder(dim=32))
    assert len(idx) == 0


def test_index_save_and_load_roundtrip(tmp_path):
    emb = HashingEmbedder(dim=64)
    idx = build_index([("a", "向量索引持久化测试内容")], emb)
    idx.save(tmp_path / "idx")
    loaded = VectorIndex.load(tmp_path / "idx")
    assert len(loaded) == len(idx)
    q = emb.embed(["向量索引持久化"])[0]
    assert loaded.search(q)[0].doc_id == idx.search(q)[0].doc_id


# ---------------------------------------------------------------------------
# 语义检索相对字面检索的增量（这条用例是这个功能存在的理由）
# ---------------------------------------------------------------------------
def test_semantic_beats_literal_on_paraphrased_query():
    """字面检索按空格切词，整句提问会退化成一个长词，必然搜不到。"""
    from agent_kit import tools

    query = "怎么复习才记得住"
    literal = tools.search_notes.invoke({"query": query, "top_k": 3})
    assert "没有与" in literal  # 字面检索落空

    semantic = tools.search_knowledge.invoke({"query": query, "top_k": 3})
    assert "forgetting-curve" in semantic  # 语义检索命中


def test_load_docs_reads_markdown_files(project_root):
    # conftest 把 cwd 切到了 tmp_path，所以必须用绝对路径取项目根
    docs = load_docs(project_root / "notes")
    assert docs, "notes/ 目录应有笔记"
    assert all(name and text for name, text in docs)


def test_load_docs_missing_dir_is_empty(tmp_path):
    assert load_docs(tmp_path / "nope") == []
