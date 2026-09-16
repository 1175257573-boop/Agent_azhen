# LangChain 1.x 核心变化

（演示用笔记，内容仅为说明 examples，不作为事实依据）

## 入口收口

LangChain 1.0 之后，构建 Agent 的标准入口收口为 `create_agent`。
它位于 `langchain.agents` 命名空间，而不是顶层 `langchain`。
旧的 `AgentExecutor`、`create_react_agent`、LCEL 管道写法已不再推荐。

## 中间件机制

中间件（middleware）承载所有横切逻辑：限流、重试、上下文压缩、脱敏、人工确认。
官方内置了十余种，同时也支持用 `@before_model`、`@after_model`、`@wrap_tool_call`
装饰器自定义钩子。

## 依赖拆分

主包不再包含模型厂商代码，需要单独安装 provider 包，
例如 `langchain-openai`、`langchain-anthropic`。
