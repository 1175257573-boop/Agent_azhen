"""脱敏：全仓**唯一的密钥出口**。

解决的问题：
    密钥一旦进了日志或异常消息，就等于落盘了——而且散落在几万行日志里没人找得回来。
    光靠"写代码时小心点"是靠不住的（异常消息会把请求细节原样带出来），
    必须有一道**机制性**的兜底：凡是往外写的文本，都先过这里。

两道防线，缺一不可：
    1. **已知值替换**：本进程见过的密钥登记在册，出现即整体替换成掩码。
       这道防线最可靠——它认的是"真实的值"，不依赖格式猜测。
    2. **形态兜底**：按常见密钥的形态（sk-xxx、Bearer xxx、api_key="xxx"）做正则替换。
       万一某个密钥没登记（例如第三方库自己从文件里读的），形态这道能兜住。

为什么用 Formatter 而不是 Filter：
    Filter 只能改 `record.msg` / `record.args`，管不到异常堆栈（`exc_info`）——
    而堆栈恰恰是最容易把请求细节带出来的地方。Formatter 拿到的是格式化完成的整串文本，
    一刀切在最后一步，覆盖面最广。

代价：每条日志多跑几次正则。日志量小，这点开销换"不会意外泄露密钥"是划算的。
"""

from __future__ import annotations

import logging
import re

MASK = "****"

# 已知密钥登记表：只存值，不对外暴露；进程退出即消失
_SECRETS: set[str] = set()

# 形态兜底①：OpenAI / DeepSeek / DashScope / Anthropic 一族的 sk- 前缀密钥
_RE_SK = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")

# 形态兜底②：Bearer <token>
_RE_BEARER = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{12,})")

# 形态兜底③：key=value 形态（JSON 或 .env 都可能这么写）
_RE_KV = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|apikey|access[_-]?token|secret[_-]?key|password)[\"']?"
    r"\s*[:=]\s*[\"']?)([A-Za-z0-9._\-]{12,})"
)


def mask(value: str | None) -> str:
    """把密钥压成 `sk-1***cdef` 的形态，仅用于展示。

    太短的值不做"掐头去尾"——那反而会把短密钥的大部分暴露出来，直接整体打码。
    """
    if not value:
        return ""
    if len(value) <= 8:
        return MASK
    return f"{value[:4]}***{value[-4:]}"


def register_secret(value: str | None) -> None:
    """把一个真实密钥登记进"出现即脱敏"名单。"""
    if value and len(value) >= 8:
        _SECRETS.add(value)


def forget_secret(value: str | None) -> None:
    """从名单里移除（密钥被清除、不再需要保护时调用）。"""
    if value:
        _SECRETS.discard(value)


def known_secret_count() -> int:
    """当前登记了几个密钥。**只返回数量，不返回内容**——测试用它做断言。"""
    return len(_SECRETS)


def redact(text: str) -> str:
    """把文本里所有疑似密钥替换成掩码。非字符串原样返回。"""
    if not isinstance(text, str) or not text:
        return text

    out = text
    # 长值优先替换，避免短值先把长值切碎后匹配不上
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret in out:
            out = out.replace(secret, mask(secret))

    out = _RE_SK.sub(lambda m: mask(m.group(0)), out)
    out = _RE_BEARER.sub(lambda m: f"{m.group(1)}{mask(m.group(2))}", out)
    out = _RE_KV.sub(lambda m: f"{m.group(1)}{mask(m.group(2))}", out)
    return out


class RedactingFormatter(logging.Formatter):
    """在格式化完成的最后一步统一脱敏。见模块开头"为什么用 Formatter"。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
