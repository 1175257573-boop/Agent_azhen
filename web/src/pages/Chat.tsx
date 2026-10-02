import { useEffect, useRef, useState } from 'react'

import ApprovalCard from '../components/ApprovalCard'
import { api } from '../api'
import { useStore } from '../store'
import type { CredentialState } from '../types'

export default function Chat() {
  const {
    messages,
    streaming,
    send,
    stop,
    reloadHistory,
    clearChat,
    pending,
    decide,
    dismiss,
    threadId,
    queue,
    refreshQueue,
    toast,
    provider,
    setConfig,
  } = useStore()
  const [draft, setDraft] = useState('')
  // 对话界面直接指定用哪条 key：选项只列已配置的，并把掩码一起显示出来，
  // 这样「当前到底用的是哪条」一眼可见，不用回设置页去猜。
  const [creds, setCreds] = useState<CredentialState | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const scroller = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const el = scroller.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages])

  // 每次进入对话页重新拉一次凭据：在设置页新增/删除 key 后回到这里能立刻看到
  useEffect(() => {
    void api.credentials().then(setCreds).catch(() => setCreds(null))
  }, [])

  const mine = pending.filter((p) => p.thread_id === threadId)

  const submit = async () => {
    const text = draft
    if (!text.trim() || streaming) return
    setDraft('')
    await send(text)
  }

  const enqueue = async () => {
    const text = draft.trim()
    if (!text) return
    try {
      await api.queueAdd(threadId, text)
      setDraft('')
      await refreshQueue()
      toast('ok', '已加入队列，本轮结束后自动发送')
    } catch (err) {
      toast('error', `入队失败：${err instanceof Error ? err.message : err}`)
    }
  }

  const onDecide = async (id: string, decisions: Parameters<typeof decide>[1]) => {
    setBusyId(id)
    try {
      await decide(id, decisions)
      toast('ok', '决策已提交')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="chat">
      <div className="chat-head">
        <code className="tid">{threadId}</code>
        <label className="key-pick" title="指定这条对话用哪条 API key">
          <span>密钥</span>
          <select
            className="select"
            value={provider ?? ''}
            disabled={streaming}
            onChange={(e) => {
              const next = e.target.value || null
              setConfig({ provider: next })
              const hit = creds?.items.find((c) => c.provider === next)
              if (!next) {
                toast('ok', '已改回自动探测')
              } else {
                toast('ok', `本次对话改用 ${hit?.label ?? next}${hit?.masked ? ` · ${hit.masked}` : ''}`)
              }
            }}
          >
            <option value="">自动{creds ? `（当前 ${creds.active_provider}）` : ''}</option>
            {(creds?.items ?? [])
              .filter((c) => c.configured)
              .map((c) => (
                <option key={c.provider} value={c.provider}>
                  {c.label} · {c.masked}
                </option>
              ))}
            {/* 选中的 provider 恰好被删掉了 key 时，别让下拉显示成空白 */}
            {provider && !(creds?.items ?? []).some((c) => c.configured && c.provider === provider) ? (
              <option value={provider}>{provider}（未配置）</option>
            ) : null}
          </select>
        </label>
        <span className="grow" />
        <button className="btn tiny ghost" onClick={() => void reloadHistory()} disabled={streaming}>
          刷新历史
        </button>
        <button className="btn tiny ghost" onClick={clearChat} disabled={streaming}>
          清屏
        </button>
      </div>

      <div className="stream" ref={scroller}>
        {messages.length === 0 ? (
          <p className="empty">还没有消息。发一句话试试；触发写文件类工具时会先弹审批。</p>
        ) : null}

        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            <div className="bubble">
              {m.content ? <div className="text">{m.content}</div> : null}
              {m.streaming && !m.content ? <span className="typing">…</span> : null}

              {m.tools.map((t) => (
                <details className="tool" key={t.id} open={t.running}>
                  <summary>
                    <span className="tool-name">{t.name}</span>
                    <span className={`tool-state${t.running ? ' run' : ''}`}>{t.running ? '执行中' : '完成'}</span>
                  </summary>
                  <pre className="code">{t.args}</pre>
                  {t.output ? <pre className="code out">{t.output}</pre> : null}
                </details>
              ))}

              {m.error ? <div className="error-box">{m.error}</div> : null}
            </div>
          </div>
        ))}

        {mine.map((p) => (
          <div className="msg assistant" key={p.id}>
            <div className="bubble">
              <ApprovalCard
                approval={p}
                busy={busyId === p.id}
                onDecide={(decisions) => void onDecide(p.id, decisions)}
                onDismiss={() => dismiss(p.id)}
              />
            </div>
          </div>
        ))}
      </div>

      <div className="composer">
        {queue.length ? (
          <div className="queue-strip">
            队列中有 {queue.length} 条待发消息 ·{' '}
            <button className="link" onClick={() => void refreshQueue()}>
              刷新
            </button>
          </div>
        ) : null}

        <div className="row">
          <textarea
            value={draft}
            rows={2}
            placeholder="输入消息 · Enter 发送 · Shift+Enter 换行"
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                void submit()
              }
            }}
          />
          <div className="composer-btns">
            {streaming ? (
              <>
                <button className="btn primary" onClick={stop}>
                  中止
                </button>
                <button className="btn" onClick={() => void enqueue()} disabled={!draft.trim()}>
                  排队
                </button>
              </>
            ) : (
              <button className="btn primary" onClick={() => void submit()} disabled={!draft.trim()}>
                发送
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
