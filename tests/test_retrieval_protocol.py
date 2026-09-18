"""检索扩展点的测试。

这个测试文件的存在意义：证明「检索」现在是一个**能接东西的接缝**，而不是死代码。
内置实现要能跑，自定义实现要能注册，没注册的要明确报错（不许静默降级）。
"""

from __future__ import annotations

import pytest

from agent_kit.retrieval import (
    Hit,
    KeywordRetriever,
    Retriever,
    RetrieverNotFoundError,
    available,
    create,
    register,
)


def test_builtin_retrievers_are_registered():
    assert {"keyword", "phrase"} <= set(available())


def test_builtin_instance_satisfies_protocol():
    assert isinstance(create("keyword"), Retriever)


def test_unknown_retriever_raises_instead_of_silently_falling_back():
    with pytest.raises(RetrieverNotFoundError) as excinfo:
        create("不存在的检索器")
    assert "已注册" in str(excinfo.value)


def test_keyword_retriever_scores(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "alpha.md").write_text("这里写 MCP 接入流程与路径越界检查。", encoding="utf-8")
    (notes / "beta.md").write_text("别的内容，和查询词无关。", encoding="utf-8")

    hits = KeywordRetriever(notes).search("MCP 接入", top_k=3)
    assert hits and hits[0].doc_id == "alpha"
    assert hits[0].score > 0
    assert hits[0].source.endswith("alpha.md")


def test_phrase_strategy_matches_whole_phrase(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "doc.md").write_text("路径越界检查是接入前必做的第一步。", encoding="utf-8")
    hits = KeywordRetriever(notes, strategy="phrase").search("路径越界", top_k=3)
    assert [h.doc_id for h in hits] == ["doc"]


def test_empty_notes_dir_returns_no_hits(tmp_path):
    assert KeywordRetriever(tmp_path / "没有这个目录").search("随便查查") == []


def test_custom_retriever_can_be_registered():
    class Dummy:
        """自定义检索器示例。"""

        name = "dummy"

        def search(self, query: str, *, top_k: int = 3) -> list[Hit]:
            return [Hit(doc_id="d1", text=f"命中 {query}", score=1.0, source="dummy")]

    register("dummy", Dummy)
    try:
        assert "dummy" in available()
        instance = create("dummy")
        assert isinstance(instance, Retriever)
        assert instance.search("测试")[0].text == "命中 测试"
    finally:
        # 注册表是全局的，测试完必须还原，否则会污染其他用例
        from agent_kit.retrieval import registry

        registry._REGISTRY.pop("dummy", None)


def test_registry_returns_fresh_instance_each_time():
    """每次 create 都应是新实例，避免调用方之间共享可变状态。"""
    assert create("keyword") is not create("keyword")
