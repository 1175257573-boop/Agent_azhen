"""容器内健康检查。

只查自己，**绝不级联下游**。

那是有过教训的：网关的 `/api/health` 会顺带探测 Agent 与记忆服务，
把它当健康检查用，下游一挂编排器就把健康的网关也重启一遍——
局部故障被放大成整体抖动。所以：

    网关   → 查 /api/info（本地应答，不碰下游）
    Agent  → 查 /api/health?probe_backends=false
    记忆   → 查 /api/health?probe_backends=false

下游到底活没活，是 `/api/health` 聚合接口的事，留给运维去看。

路径与端口从环境变量读：`HEALTH_PATH` / `PORT`。
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

PORT = os.environ.get("PORT", "8000").strip() or "8000"
PATH = os.environ.get("HEALTH_PATH", "/api/health").strip() or "/api/health"
TIMEOUT = float(os.environ.get("HEALTH_TIMEOUT", "4"))


def main() -> int:
    url = f"http://127.0.0.1:{PORT}{PATH}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:  # noqa: S310 —— 只连本机
            status = resp.status
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"unhealthy: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if status != 200:
        print(f"unhealthy: HTTP {status}", file=sys.stderr)
        return 1

    print(f"healthy: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
