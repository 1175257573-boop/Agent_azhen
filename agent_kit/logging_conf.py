"""日志配置（对标 Java 里的 logback.xml / application.yml logging 段）。

为什么不用 print：
    print 写死到 stdout、没有级别、没有时间戳和来源模块，也无法按环境关闭。
    生产代码统一走 logging.getLogger(__name__)，入口处调用一次 setup_logging()。

    from agent_kit.logging_conf import setup_logging
    setup_logging(level="DEBUG", quiet_stdout_for=("httpx", "openai", "anthropic"))

日志级别建议：
    INFO   日常运行（默认）
    DEBUG  排查工具调用链路时打开，会打印每个中间件的进出
    WARNING 只关心异常与降级
"""

from __future__ import annotations

import logging
import sys

DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)-18s | %(message)s"
DEFAULT_DATEFMT = "%H:%M:%S"

# 第三方库刷屏严重，默认压到 WARNING
NOISY_LIBRARIES = (
    "httpx",
    "httpcore",
    "openai",
    "anthropic",
    "mcp",
    "fastmcp",
    "urllib3",
    "asyncio",
)


def setup_logging(
    level: str = "INFO",
    *,
    quiet_stdout_for: tuple[str, ...] = NOISY_LIBRARIES,
    stream=None,
) -> logging.Logger:
    """初始化根 logger。**只需在程序入口调用一次。**

    Args:
        level: DEBUG / INFO / WARNING / ERROR
        quiet_stdout_for: 这些第三方库的日志压到 WARNING，避免刷屏
        stream: 输出目标，默认 stdout
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    # 重复调用（如交互式环境）时避免叠加多个 handler
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter(DEFAULT_FORMAT, datefmt=DEFAULT_DATEFMT))
    root.addHandler(handler)

    for lib in quiet_stdout_for:
        logging.getLogger(lib).setLevel(logging.WARNING)

    return logging.getLogger("agent")


def get_logger(name: str) -> logging.Logger:
    """模块内获取 logger 的统一入口，等价于 Java 里的 `private static final Logger log = ...`。"""
    return logging.getLogger(name)
