"""语义检索演示：字面匹配 vs 语义检索的对照。

离线可跑，不需要任何 API Key：
没有 DASHSCOPE_API_KEY 时自动降级到 HashingEmbedder，仍然能演示完整链路。

    python main.py rag
"""

from __future__ import annotations

from pathlib import Path

from agent_kit.retrieval import build_index, get_embedder, load_docs

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _rule(title: str) -> None:
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


def main() -> int:
    notes_dir = PROJECT_ROOT / "notes"
    docs = load_docs(notes_dir)

    _rule(f"1. 语料：{notes_dir.name}/ 共 {len(docs)} 篇")
    for name, text in docs:
        print(f"  - {name:<24} {len(text):>5} 字符")

    choice = get_embedder()
    _rule(f"2. 向量化后端：{choice.backend}")
    print(f"  降级：{choice.degraded}")
    if choice.reason:
        print(f"  原因：{choice.reason}")

    index = build_index(docs, choice.embedder)
    _rule(f"3. 建索引：{len(index)} 个片段")

    # 这几个问法刻意不含笔记里的原词，用来暴露字面检索的短板
    queries = [
        "怎么复习才记得住",
        "接别的 MCP 服务之前要先看什么",
        "Agent 之间互相甩锅怎么办",
    ]

    for q in queries:
        _rule(f"4. 提问：{q}")
        qv = choice.embedder.embed([q])[0]
        hits = index.search(qv, top_k=2)
        if not hits:
            print("  （无命中）")
            continue
        for h in hits:
            snippet = h.text[:80].replace("\n", " ")
            print(f"  [{h.score:.3f}] {h.doc_id}: {snippet}...")

    _rule("5. 为什么需要语义检索")
    from agent_kit.tools import search_notes

    q = queries[0]
    literal = search_notes.invoke({"query": q, "top_k": 1})
    print(f"  字面检索 search_notes({q!r})：")
    print(f"    {literal[:60]}...")
    print("  → 整句提问被当成一个长词，字面匹配必然落空；语义检索能命中。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
