# LangGraph 与 Agent 执行模型

（演示用笔记，内容仅为说明 examples，不作为事实依据）

## 运行时关系

LangGraph 是 LangChain 1.x Agent 底层的编排运行时。
`create_agent` 返回的对象本质是一个 `CompiledStateGraph`，
因此天然支持持久化、断点恢复、流式与时间旅行。

## 状态与节点

典型执行链路是：model → 判断是否需要工具 → tools → model → … → END。
状态图（StateGraph）中每个节点共享一份 state，
节点之间靠 channel 传递增量更新。

## 记忆分工

短期记忆由 checkpointer 按 thread_id 保存；
长期记忆由 store 按 namespace 保存。
两者互不干扰，生产环境可分别替换为 Postgres 实现。
