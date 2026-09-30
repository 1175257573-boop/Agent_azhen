/** 后端 REST / SSE 客户端。所有路径同源（网关或单体都在同一端口）。 */

import type {
  ChatMessage,
  CredentialState,
  HealthPayload,
  MemoryStatus,
  MessageOut,
  Preference,
  QueuedOut,
  StreamEvent,
  SystemInfo,
  ThreadBrief,
  VerifyOut,
} from './types'

export class ApiError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(`${status} ${detail}`)
    this.status = status
    this.detail = detail
  }
}

async function parse<T>(resp: Response): Promise<T> {
  const text = await resp.text()
  if (!resp.ok) {
    let detail = text
    try {
      const body = JSON.parse(text)
      detail = body?.detail ?? body?.message ?? text
    } catch {
      /* 非 JSON（比如代理错误页）就用原文 */
    }
    throw new ApiError(resp.status, String(detail))
  }
  if (!text) return undefined as T
  try {
    return JSON.parse(text) as T
  } catch {
    return text as unknown as T
  }
}

async function request<T>(
  path: string,
  init: RequestInit & { params?: Record<string, string | number | boolean | undefined> } = {},
): Promise<T> {
  const { params, ...rest } = init
  const url = new URL(path, window.location.origin)
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v !== undefined && v !== '') url.searchParams.set(k, String(v))
  }
  return parse<T>(await fetch(url.toString(), rest))
}

const json = (body: unknown): RequestInit => ({
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

// ---------------------------------------------------------------- 系统
export const api = {
  health: () => request<HealthPayload>('/api/health', { params: { probe_backends: 'false' } }),
  info: () => request<SystemInfo>('/api/info'),
  modes: () => request<Record<string, string>>('/api/modes'),

  // ------------------------------------------------------------ 会话
  threads: () => request<ThreadBrief[]>('/api/chat/threads'),
  newThread: () => request<ThreadBrief>('/api/chat/threads', { method: 'POST', ...json({}) }),
  deleteThread: (threadId: string, params: ChatParams) =>
    request<{ ok: boolean }>(`/api/chat/threads/${encodeURIComponent(threadId)}`, {
      method: 'DELETE',
      params: { ...params, provider: params.provider ?? '' },
    }),
  history: (threadId: string, params: ChatParams) =>
    request<MessageOut[]>('/api/chat/history', {
      params: { thread_id: threadId, ...params, provider: params.provider ?? '' },
    }),

  // ------------------------------------------------------------ 排队
  queueList: (threadId: string) => request<QueuedOut[]>('/api/chat/queue', { params: { thread_id: threadId } }),
  queueAdd: (threadId: string, text: string, itemId?: string) =>
    request<QueuedOut>('/api/chat/queue', { method: 'POST', ...json({ thread_id: threadId, message: text, item_id: itemId }) }),
  queueRemove: (threadId: string, itemId: string) =>
    request<{ ok: boolean }>(`/api/chat/queue/${encodeURIComponent(itemId)}`, { method: 'DELETE', params: { thread_id: threadId } }),
  queueClear: (threadId: string) => request<{ ok: boolean; cleared: number }>('/api/chat/queue', { method: 'DELETE', params: { thread_id: threadId } }),

  // ------------------------------------------------------------ MCP
  mcpTools: (params: ChatParams & { connect: boolean }) =>
    request<{ connected: boolean; tools: string[] }>('/api/chat/mcp-tools', {
      params: { connect: params.connect, mode: params.mode, provider: params.provider ?? '', user_id: params.user_id, role: params.role },
    }),

  // ------------------------------------------------------------ 记忆
  memoryStatus: () => request<MemoryStatus>('/api/memory/status'),
  preferences: (userId: string) => request<Preference[]>('/api/memory/preferences', { params: { user_id: userId } }),
  putPreference: (userId: string, key: string, value: string) =>
    request<Preference>('/api/memory/preferences', { method: 'POST', ...json({ user_id: userId, key, value }) }),
  deletePreference: (userId: string, key: string) =>
    request<{ ok: boolean }>('/api/memory/preferences', { method: 'DELETE', ...json({ user_id: userId, key }) }),

  // ------------------------------------------------------------ 凭据
  credentials: () => request<CredentialState>('/api/credentials'),
  saveCredential: (provider: string, apiKey: string, remember: boolean) =>
    request<CredentialState>('/api/credentials', { method: 'POST', ...json({ provider, api_key: apiKey, remember }) }),
  deleteCredential: (provider: string) =>
    request<CredentialState>(`/api/credentials/${encodeURIComponent(provider)}`, { method: 'DELETE' }),
  revealCredential: (provider: string) =>
    request<{ provider: string; api_key: string; masked: string; expires_in: number }>('/api/credentials/reveal', {
      method: 'POST',
      ...json({ provider }),
    }),
  verifyCredential: (provider: string, modelName?: string) =>
    request<VerifyOut>('/api/credentials/verify', { method: 'POST', ...json({ provider, model_name: modelName ?? '' }) }),
}

export interface ChatParams {
  mode: string
  provider: string | null
  user_id: string
  role: string
}

// ---------------------------------------------------------------- SSE
/**
 * 逐帧消费 SSE。
 *
 * 用 fetch + ReadableStream 而不是 EventSource：后端是 POST + JSON 请求体，
 * EventSource 只能发 GET。心跳帧（`: ping`）直接丢弃。
 */
export async function* streamSse(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const resp = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!resp.ok || !resp.body) {
    let detail = `HTTP ${resp.status}`
    try {
      const text = await resp.text()
      const parsed = JSON.parse(text)
      detail = parsed?.detail ?? text
    } catch {
      /* 忽略 */
    }
    throw new ApiError(resp.status, String(detail))
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      // SSE 帧以空行分隔；最后一个帧可能不带尾部分隔符，靠 done 之后再冲一次
      let sep: number
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        const evt = parseFrame(frame)
        if (evt) yield evt
      }
    }
    const tail = parseFrame(buffer.trim())
    if (tail) yield tail
  } finally {
    reader.cancel().catch(() => undefined)
  }
}

function parseFrame(frame: string): StreamEvent | null {
  for (const line of frame.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed || trimmed.startsWith(':')) continue // 心跳
    if (!trimmed.startsWith('data:')) continue
    const raw = trimmed.slice(5).trim()
    if (!raw) continue
    try {
      return JSON.parse(raw) as StreamEvent
    } catch {
      return null
    }
  }
  return null
}

// ---------------------------------------------------------------- 工具
export function historyToMessages(rows: MessageOut[]): ChatMessage[] {
  const out: ChatMessage[] = []
  for (const row of rows) {
    const content = typeof row.content === 'string' ? row.content : ''
    const tools = (row.tool_calls ?? []).map((call, i) => ({
      id: `${out.length}-${i}`,
      name: String(call?.name ?? 'tool'),
      args: JSON.stringify(call?.args ?? {}),
      running: false,
    }))
    if (row.role === 'tool') {
      // 工具回执挂到最后一条 assistant 的同名工具上
      const last = out[out.length - 1]
      if (last && last.tools.length) {
        const target = last.tools.find((t) => t.name === row.name) ?? last.tools[last.tools.length - 1]
        target.output = content
      }
      continue
    }
    if (row.role === 'user') {
      out.push({ id: `h-${out.length}`, role: 'user', content, tools: [] })
    } else if (row.role === 'assistant') {
      if (!content && !tools.length) continue
      out.push({ id: `h-${out.length}`, role: 'assistant', content, tools })
    }
  }
  return out
}
