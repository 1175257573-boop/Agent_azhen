import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import { ApiError, api, historyToMessages, streamSse } from './api'
import type { ConfirmAction, ConfirmOptions } from './components/ConfirmDialog'
import type {
  ChatMessage,
  Decision,
  InterruptPayload,
  PendingApproval,
  QueuedOut,
  StreamEvent,
  ToolRun,
} from './types'

const STORAGE_KEY = 'atlas.web.session.v1'

interface Persisted {
  threadId: string
  userId: string
  role: string
  mode: string
  provider: string | null
  enableMcp: boolean
}

const DEFAULTS: Persisted = {
  threadId: 'atlas-main',
  userId: 'demo',
  role: 'admin',
  mode: 'chat',
  provider: null,
  enableMcp: false,
}

function loadPersisted(): Persisted {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return DEFAULTS
    return { ...DEFAULTS, ...(JSON.parse(raw) as Partial<Persisted>) }
  } catch {
    return DEFAULTS
  }
}

export interface Toast {
  id: number
  kind: 'info' | 'error' | 'ok'
  text: string
}

interface Store {
  // 会话配置
  threadId: string
  userId: string
  role: string
  mode: string
  provider: string | null
  enableMcp: boolean
  setConfig: (patch: Partial<Persisted>) => void
  newThread: (id?: string) => void

  // 聊天
  messages: ChatMessage[]
  streaming: boolean
  send: (text: string) => Promise<void>
  stop: () => void
  reloadHistory: () => Promise<void>
  clearChat: () => void

  // 审批
  pending: PendingApproval[]
  decide: (approvalId: string, decisions: Decision[]) => Promise<void>
  dismiss: (approvalId: string) => void

  // 排队
  queue: QueuedOut[]
  refreshQueue: () => Promise<void>

  toast: (kind: Toast['kind'], text: string) => void
  toasts: Toast[]
  /** 全局确认弹窗：await confirm({...}) 拿到 true/false */
  confirm: (opts: ConfirmOptions) => Promise<boolean>
  /** 需要区分「主动取消」与「Esc/点遮罩什么都不做」时用它 */
  confirmAction: (opts: ConfirmOptions) => Promise<ConfirmAction>
  confirmState: ConfirmOptions | null
  resolveConfirm: (action: ConfirmAction) => void
}

const Ctx = createContext<Store | null>(null)

let seq = 0
const uid = (prefix: string) => `${prefix}-${Date.now().toString(36)}-${seq++}`

export function StoreProvider({ children }: { children: ReactNode }) {
  const [persisted, setPersisted] = useState<Persisted>(loadPersisted)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [streaming, setStreaming] = useState(false)
  const [pending, setPending] = useState<PendingApproval[]>([])
  const [queue, setQueue] = useState<QueuedOut[]>([])
  const [toasts, setToasts] = useState<Toast[]>([])
  const [confirmState, setConfirmState] = useState<ConfirmOptions | null>(null)
  const confirmResolver = useRef<((action: ConfirmAction) => void) | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  const toastId = useRef(0)

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(persisted))
  }, [persisted])

  const toast = useCallback((kind: Toast['kind'], text: string) => {
    const id = ++toastId.current
    setToasts((prev) => [...prev, { id, kind, text }])
    window.setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 4000)
  }, [])

  // 确认弹窗：用 Promise 把「用户点哪个」变成一段可以 await 的同步流程，
  // 调用方写起来跟原来的 window.confirm 一样直白。
  const confirmAction = useCallback(
    (opts: ConfirmOptions) =>
      new Promise<ConfirmAction>((resolve) => {
        confirmResolver.current = resolve
        setConfirmState(opts)
      }),
    [],
  )

  /** 两态够用的场景（绝大多数）：true = 点了确认 */
  const confirm = useCallback(
    async (opts: ConfirmOptions) => (await confirmAction(opts)) === 'confirm',
    [confirmAction],
  )

  const resolveConfirm = useCallback((action: ConfirmAction) => {
    confirmResolver.current?.(action)
    confirmResolver.current = null
    setConfirmState(null)
  }, [])

  const setConfig = useCallback((patch: Partial<Persisted>) => {
    setPersisted((prev) => ({ ...prev, ...patch }))
  }, [])

  const chatParams = useCallback(
    () => ({ mode: persisted.mode, provider: persisted.provider, user_id: persisted.userId, role: persisted.role }),
    [persisted],
  )

  const refreshQueue = useCallback(async () => {
    try {
      setQueue(await api.queueList(persisted.threadId))
    } catch {
      setQueue([])
    }
  }, [persisted.threadId])

  const reloadHistory = useCallback(async () => {
    try {
      const rows = await api.history(persisted.threadId, chatParams())
      setMessages(historyToMessages(rows))
    } catch (err) {
      toast('error', `历史加载失败：${err instanceof Error ? err.message : err}`)
    }
  }, [chatParams, persisted.threadId, toast])

  useEffect(() => {
    void reloadHistory()
    void refreshQueue()
    // 切会话时重新拉取；配置变化不触发（避免输入 user_id 时反复刷）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [persisted.threadId])

  const newThread = useCallback(
    (id?: string) => {
      const next = id ?? `atlas-${Date.now().toString(36)}`
      setPersisted((prev) => ({ ...prev, threadId: next }))
      setMessages([])
      setPending([])
      setQueue([])
    },
    [],
  )

  const applyEvent = useCallback((evt: StreamEvent, assistantId: string) => {
    switch (evt.type) {
      case 'token': {
        const piece = typeof evt.data === 'string' ? evt.data : ''
        if (!piece) return
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantId ? { ...m, content: m.content + piece } : m)),
        )
        return
      }
      case 'custom': {
        const note = typeof evt.data === 'string' ? evt.data : ''
        if (!note) return
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantId ? { ...m, content: `${m.content}\n> ${note}\n` } : m)),
        )
        return
      }
      case 'tool_start': {
        const tool: ToolRun = {
          id: uid('tool'),
          name: evt.data?.name ?? 'tool',
          args: JSON.stringify(evt.data?.args ?? {}),
          running: true,
        }
        setMessages((prev) => prev.map((m) => (m.id === assistantId ? { ...m, tools: [...m.tools, tool] } : m)))
        return
      }
      case 'tool_end': {
        const { name, output } = evt.data ?? {}
        setMessages((prev) =>
          prev.map((m) => {
            if (m.id !== assistantId) return m
            // 倒序找同名且还在跑的那条，找不到就落到最后一条
            const idx = [...m.tools].reverse().findIndex((t) => t.name === name && t.running)
            const tools = [...m.tools]
            if (idx === -1) {
              // 恢复执行的流里常常只有 tool_end（工具其实在上一段被中断前就跑完了），
              // 没有对应的 tool_start。这时候也要补一条记录，否则工具输出就凭空消失了。
              tools.push({
                id: uid('tool'),
                name: String(name ?? 'tool'),
                args: '',
                output: String(output ?? ''),
                running: false,
              })
              return { ...m, tools }
            }
            const target = m.tools.length - 1 - idx
            tools[target] = { ...tools[target], output: String(output ?? ''), running: false }
            return { ...m, tools }
          }),
        )
        return
      }
      case 'error': {
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantId ? { ...m, error: String(evt.data ?? '未知错误'), streaming: false } : m)),
        )
        return
      }
      default:
        return
    }
  }, [])

  const runStream = useCallback(
    async (path: string, body: unknown) => {
      const assistantId = uid('a')
      setStreaming(true)
      setMessages((prev) => [...prev, { id: assistantId, role: 'assistant', content: '', tools: [], streaming: true }])
      const controller = new AbortController()
      abortRef.current = controller
      let interrupted = false

      try {
        for await (const evt of streamSse(path, body, controller.signal)) {
          if (evt.type === 'interrupt') {
            interrupted = true
            const payloads = (evt.data ?? []) as InterruptPayload[]
            setPending((prev) => [
              ...prev,
              ...payloads.map((payload) => ({
                id: uid('appr'),
                thread_id: persisted.threadId,
                created_at: Date.now(),
                payload,
              })),
            ])
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId
                  ? { ...m, content: m.content ? m.content : '（等待人工确认）', streaming: false }
                  : m,
              ),
            )
            continue
          }
          applyEvent(evt, assistantId)
        }
      } catch (err) {
        if (controller.signal.aborted) {
          setMessages((prev) =>
            prev.map((m) => (m.id === assistantId ? { ...m, content: m.content + '\n（已中止）', streaming: false } : m)),
          )
        } else {
          const detail = err instanceof ApiError ? err.detail : String(err)
          setMessages((prev) =>
            prev.map((m) => (m.id === assistantId ? { ...m, error: detail, streaming: false } : m)),
          )
          toast('error', detail)
        }
      } finally {
        setMessages((prev) => prev.map((m) => (m.streaming ? { ...m, streaming: false } : m)))
        setStreaming(false)
        abortRef.current = null
        void refreshQueue()
      }
      return interrupted
    },
    [applyEvent, persisted.threadId, refreshQueue, toast],
  )

  const send = useCallback(
    async (text: string) => {
      const content = text.trim()
      if (!content || streaming) return
      setMessages((prev) => [...prev, { id: uid('u'), role: 'user', content, tools: [] }])
      await runStream('/api/chat/stream', {
        message: content,
        thread_id: persisted.threadId,
        mode: persisted.mode,
        provider: persisted.provider,
        user_id: persisted.userId,
        role: persisted.role,
        stream: true,
        enable_mcp: persisted.enableMcp,
      })
    },
    [persisted, runStream, streaming],
  )

  const decide = useCallback(
    async (approvalId: string, decisions: Decision[]) => {
      const target = pending.find((p) => p.id === approvalId)
      if (!target) return
      setPending((prev) => prev.filter((p) => p.id !== approvalId))
      await runStream('/api/chat/resume', {
        thread_id: target.thread_id,
        mode: persisted.mode,
        provider: persisted.provider,
        user_id: persisted.userId,
        role: persisted.role,
        enable_mcp: persisted.enableMcp,
        decisions,
      })
    },
    [pending, persisted, runStream],
  )

  const dismiss = useCallback((approvalId: string) => {
    setPending((prev) => prev.filter((p) => p.id !== approvalId))
  }, [])

  const stop = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  const clearChat = useCallback(() => setMessages([]), [])

  const value = useMemo<Store>(
    () => ({
      threadId: persisted.threadId,
      userId: persisted.userId,
      role: persisted.role,
      mode: persisted.mode,
      provider: persisted.provider,
      enableMcp: persisted.enableMcp,
      setConfig,
      newThread,
      messages,
      streaming,
      send,
      stop,
      reloadHistory,
      clearChat,
      pending,
      decide,
      dismiss,
      queue,
      refreshQueue,
      toast,
      toasts,
      confirm,
      confirmAction,
      confirmState,
      resolveConfirm,
    }),
    [
      clearChat,
      confirm,
      confirmAction,
      confirmState,
      decide,
      dismiss,
      messages,
      newThread,
      pending,
      persisted,
      queue,
      refreshQueue,
      reloadHistory,
      resolveConfirm,
      send,
      setConfig,
      stop,
      streaming,
      toast,
      toasts,
    ],
  )

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useStore(): Store {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useStore 必须在 StoreProvider 内使用')
  return ctx
}
