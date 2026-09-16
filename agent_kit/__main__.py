"""让 `python -m agent_kit <子命令>` 也能启动。

Python 的包也可以像 JAR 一样被 `-m` 执行，前提是包内有 __main__.py。
这里是薄薄一层，真正逻辑都在根目录的 main.py。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main import main

if __name__ == "__main__":
    raise SystemExit(main())
