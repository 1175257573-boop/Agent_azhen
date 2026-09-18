"""内置检索器：字面匹配（keyword / phrase 两种打分）。

这是项目**默认自带**的唯一检索能力：读本地笔记目录，按词频与标题命中的加权打分。
零依赖、离线可跑、结果可解释——代价是「说法不同但意思相近」命中不了。

需要语义检索时不用改这里：另写一个 Retriever 注册进去，
把 atlas.toml 的 `[retrieval] name` 指过去即可（见 base.py 的说明）。
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import Hit
from .registry import register

# 项目根/notes：与 tools.py 的笔记目录保持一致
NOTES_DIR = Path(__file__).resolve().parents[1].parent / "notes"


class KeywordRetriever:
    """字面检索器。

    keyword 策略：把问题切成词，按词频加权（正文 ×2 / 标题命中 ×10）
    phrase  策略：整句作为一个整体去数出现次数（正文 ×5 / 标题 ×10）
    """

    name = "keyword"

    def __init__(self, notes_dir: Path | str | None = None, *, strategy: str = "keyword") -> None:
        self.notes_dir = Path(notes_dir) if notes_dir else NOTES_DIR
        self.strategy = strategy if strategy in ("keyword", "phrase") else "keyword"

    def _load(self) -> list[tuple[str, str]]:
        if not self.notes_dir.exists():
            return []
        return [
            (p.stem, p.read_text(encoding="utf-8"))
            for p in sorted(self.notes_dir.glob("*.md"))
        ]

    def search(self, query: str, *, top_k: int = 3) -> list[Hit]:
        notes = self._load()
        if not notes:
            return []

        q = query.strip().lower()
        if not q:
            return []

        hits: list[Hit] = []
        for title, body in notes:
            low = body.lower()
            title_low = title.lower()
            if self.strategy == "phrase":
                score = low.count(q) * 5 + title_low.count(q) * 10
            else:
                words = [w for w in re.split(r"\s+", q) if w]
                score = sum(low.count(w) * 2 for w in words) + title_low.count(q) * 10
            if score > 0:
                hits.append(
                    Hit(doc_id=title, text=body, score=float(score), source=f"notes/{title}.md")
                )

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]


# 两种打分各注册一个名字：工具层的 strategy 参数直接映射到这里
register("keyword", lambda: KeywordRetriever(strategy="keyword"))
register("phrase", lambda: KeywordRetriever(strategy="phrase"))
