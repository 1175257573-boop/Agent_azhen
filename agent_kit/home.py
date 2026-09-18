"""ATLAS_HOME：本机运行态目录（对标 Codex 的 CODEX_HOME）。

解决的问题：状态文件散落在项目目录里——删掉项目目录、换台机器、重装系统，
会话与记忆就全丢了，而这些恰恰是最不该丢的东西。

Codex 的做法是把运行态统一收进一个家目录（`~/.codex`），里面按职责分：
sessions/ 存会话流水、memories/ 存记忆产物、state 库存结构化状态。
本项目照做，默认 `~/.atlas`，可用环境变量 `ATLAS_HOME` 覆盖（测试与 CI 都靠它隔离）。

目录约定：
    ~/.atlas/
    ├── atlas.db        SQLite：短期记忆 checkpoint + 长期记忆 store
    ├── sessions/       会话流水导出（rollout）
    ├── memories/       长期记忆的人可读快照
    └── logs/           运行日志

注意：`ensure_dirs()` 只在真正要写时才调用，不要在 import 时建目录——
否则跑个 `--help` 也会在用户机器上留下痕迹。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_ATLAS_HOME = "ATLAS_HOME"
DEFAULT_DIR_NAME = ".atlas"
DB_NAME = "atlas.db"


def atlas_home() -> Path:
    """解析家目录：环境变量 `ATLAS_HOME` 优先，否则 `~/.atlas`。"""
    raw = os.getenv(ENV_ATLAS_HOME, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / DEFAULT_DIR_NAME


@dataclass(frozen=True)
class HomeLayout:
    """家目录下的固定路径集合。纯数据，不做 IO。"""

    root: Path

    @property
    def db_path(self) -> Path:
        """SQLite 落盘位置：短期记忆与长期记忆共用一个库文件（表名不同）。"""
        return self.root / DB_NAME

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def memories_dir(self) -> Path:
        return self.root / "memories"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    def ensure_dirs(self) -> HomeLayout:
        """按需创建目录（已存在则跳过）。返回自身方便链式调用。"""
        for d in (self.root, self.sessions_dir, self.memories_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self


def layout(root: Path | str | None = None) -> HomeLayout:
    """取一份布局。传 root 则用指定根目录，否则解析家目录。"""
    if root is None:
        return HomeLayout(root=atlas_home())
    return HomeLayout(root=Path(root).expanduser())


def resolve_db_path() -> Path:
    """SQLite 库文件路径：`SQLITE_PATH` 环境变量优先，其次家目录下的 atlas.db。"""
    raw = os.getenv("SQLITE_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    return layout().db_path
