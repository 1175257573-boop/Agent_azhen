import { afterEach, beforeEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'
import type { StreamEvent } from '../types'

/**
 * 页面级测试用的 fetch 桩。
 *
 * 要点：SSE 走的是 fetch + ReadableStream，所以桩必须能吐**分帧的文本流**，
 * 只返回一个完整 JSON 是测不出流式渲染的。
 */
export interface Reply {
  status?: number
  json?: unknown
  text?: string
  /** 逐帧发送的 SSE 事件 */
  sse?: StreamEvent[]
  /** 原始 SSE 文本分片：用来测「一帧被拆进两个网络包」这种粘包/断帧场景 */
  chunks?: string[]
  /**
   * 推完上面的内容后**不要立刻关闭**连接，改为延迟 holdMs 毫秒再关。
   * 用来构造「任务还在执行中」这个中间态 —— 比如任务执行时点发送
   * 应该弹出「立即发送 / 等结束」二选一，而这个分支在流已关闭时根本走不到。
   */
  holdMs?: number
}

export type FetchHandler = (path: string, init?: RequestInit) => Reply | undefined

interface Recorded {
  path: string
  search: string
  method: string
  body: unknown
}

export function installFetch(handler: FetchHandler) {
  const calls: Recorded[] = []

  const fake = (input: unknown, init?: RequestInit) => {
    const raw = typeof input === 'string' ? input : String(input)
    const parsed = new URL(raw, 'http://unit.test')
    let body: unknown = undefined
    if (typeof init?.body === 'string') {
      try {
        body = JSON.parse(init.body)
      } catch {
        body = init.body
      }
    }
    calls.push({
      path: parsed.pathname,
      search: parsed.search,
      method: init?.method ?? 'GET',
      body,
    })

    const reply = handler(parsed.pathname, init) ?? { status: 404, json: { detail: `未打桩：${parsed.pathname}` } }
    const status = reply.status ?? 200
    const payload = reply.sse
      ? reply.sse.map((evt) => `data: ${JSON.stringify(evt)}\n\n`).join('')
      : (reply.text ?? JSON.stringify(reply.json ?? {}))

    let stream: ReadableStream<Uint8Array> | null = null
    if (reply.sse || reply.chunks) {
      const pieces = reply.chunks ?? [payload]
      const encoder = new TextEncoder()
      let holdTimer: ReturnType<typeof setTimeout> | undefined
      let ctrl: ReadableStreamDefaultController<Uint8Array> | null = null
      stream = new ReadableStream<Uint8Array>({
        start(controller) {
          ctrl = controller
          for (const piece of pieces) controller.enqueue(encoder.encode(piece))
          if (reply.holdMs && reply.holdMs > 0) {
            // 保持连接：模拟「任务还在跑」，期间可以测执行中的交互
            holdTimer = setTimeout(() => {
              try {
                controller.close()
              } catch {
                /* 已被 abort */
              }
            }, reply.holdMs)
            return
          }
          controller.close()
        },
        cancel() {
          if (holdTimer) clearTimeout(holdTimer)
        },
      })

      // 真实 fetch 收到 abort 会让 reader.read() 抛 AbortError。桩如果不接 signal，
      // 流就会一直挂着 → 调用方的 finally 永不执行（streaming 卡在 true），
      // 「立即发送」这类必须靠中断才能走通的逻辑就测不了。
      const signal = init?.signal
      if (signal) {
        const onAbort = () => {
          try {
            if (holdTimer) clearTimeout(holdTimer)
            ctrl?.error(new DOMException('The operation was aborted.', 'AbortError'))
          } catch {
            /* 流已结束，忽略 */
          }
        }
        if (signal.aborted) onAbort()
        else signal.addEventListener('abort', onAbort, { once: true })
      }
    }

    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      headers: { get: () => (reply.sse ? 'text/event-stream' : 'application/json') },
      text: () => Promise.resolve(payload),
      body: stream,
    } as unknown as Response)
  }

  globalThis.fetch = fake as unknown as typeof fetch
  return calls
}

export function findCall(calls: Recorded[], method: string, path: string) {
  return calls.find((c) => c.method === method.toUpperCase() && c.path === path)
}

beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})
