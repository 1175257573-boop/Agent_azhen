"""系统提示词：静态基座 + 动态注入。

LangChain 1.x 里 system_prompt 有两种给法：
  1. create_agent(system_prompt="...")              —— 静态
  2. @dynamic_prompt 装饰的函数放进 middleware      —— 每次请求前动态生成

生产里一般两者配合：静态部分写「人格与硬规则」，动态部分写「当前状态」。
"""

from __future__ import annotations

from datetime import datetime, timezone

from langchain.agents.middleware import dynamic_prompt
from langchain.agents.middleware.types import ModelRequest

BASE_SYSTEM_PROMPT = """\
你是一个严谨的研究助理 Agent，代号 Atlas。

## 工作方式（ReAct 循环）
1. 收到问题后先判断是否需要工具。需要就调用，不要凭记忆瞎猜。
2. 拿到工具结果后要**复核**：数字、单位、时间都要和数据来源一致。
3. 不要重复调用同一个工具、传同样的参数——发现重复立刻换思路。
4. 信息不足时，优先调用工具补充，而不是编造内容。确实查不到就明说。

## 硬性约束
- 禁止输出未经工具验证的数字。
- 禁止执行任何会修改用户环境的操作（写文件除外，且必须走 write_report 工具）。
- 所有结论必须标注来源；没有来源的结论不写进 key_findings。
- 回答使用与用户提问相同的语言。

## 效率原则
- 能一次调用解决的问题，不要拆成多次。
- 工具调用总次数建议控制在 6 次以内。
"""


@dynamic_prompt
def dynamic_context_prompt(request: ModelRequest) -> str:
    """每次模型调用前重算的动态提示词。

    request.state 里能拿到当前对话状态，request.runtime.context 里能拿到运行时上下文。
    这里演示三件事：注入当前时间、注入用户画像、按对话长度切换「节电模式」。
    """
    # 必须带上时区偏移：naive datetime 的 %Z 恒为空，
    # 模型会看到一个「没有时区的时间」，做时间推断必然出错。
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z").strip()

    ctx = getattr(request.runtime, "context", None)
    if ctx is not None:
        user_id = getattr(ctx, "user_id", "anonymous")
        role = getattr(ctx, "role", "user")
        locale = getattr(ctx, "locale", "zh-CN")
    else:
        user_id, role, locale = "anonymous", "user", "zh-CN"

    history_len = len(request.state.get("messages", []))
    mode_hint = (
        "当前对话较长，请优先复用已有上下文，避免重复调用工具。"
        if history_len >= 10
        else "对话刚开始，可以先澄清需求再动手。"
    )

    return (
        f"{BASE_SYSTEM_PROMPT}\n"
        f"## 当前运行时状态\n"
        f"- 当前时间：{now}\n"
        f"- 用户 ID：{user_id}（角色：{role}，语言：{locale}）\n"
        f"- 历史消息数：{history_len}\n"
        f"- 策略提示：{mode_hint}\n"
    )
