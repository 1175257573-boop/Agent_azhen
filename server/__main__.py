"""支持 `python -m server` 启动 Web 服务。"""

from __future__ import annotations

import uvicorn

from server.app import app


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
