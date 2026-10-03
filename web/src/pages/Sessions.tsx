import { useCallback, useEffect, useState } from 'react'

import { api } from '../api'
import { useStore } from '../store'
import type { ThreadBrief } from '../types'

export default function Sessions() {
  const { threadId, newThread, reloadHistory, setConfig, userId, role, mode, provider, toast } = useStore()
  const [threads, setThreads] = useState<ThreadBrief[]>([])
  const [loading, setLoading] = useState(false)
  const [manual, setManual] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setThreads(await api.threads())
    } catch (err) {
      toast('error', `会话列表加载失败：${err instanceof Error ? err.message : err}`)
    } finally {
      setLoading(false)
    }
  }, [toast])

  useEffect(() => {
    void load()
  }, [load])

  const create = async () => {
    try {
      const created = await api.newThread()
      newThread(created.thread_id)
      // 先拉后端列表，再乐观补一条：新会话此刻一条消息都没有，
      // 后端是从 checkpoint 里扫会话的，它必然不在返回结果里，
      // 只 load() 的话列表看起来完全没变化，用户会以为新建没生效。
      // 顺序上必须「先 load 后插入」，否则会盖掉后端返回的真实顺序。
      await load()
      setThreads((prev) =>
        prev.some((t) => t.thread_id === created.thread_id)
          ? prev
          : [{ thread_id: created.thread_id, message_count: 0 }, ...prev],
      )
      toast('ok', `已新建会话 ${created.thread_id}，发送第一条消息后自动保存`)
    } catch (err) {
      toast('error', `新建失败：${err instanceof Error ? err.message : err}`)
    }
  }

  const remove = async (id: string) => {
    try {
      await api.deleteThread(id, { mode, provider, user_id: userId, role })
      await load()
      if (id === threadId) newThread()
      toast('ok', `已删除会话 ${id}`)
    } catch (err) {
      toast('error', `删除失败：${err instanceof Error ? err.message : err}`)
    }
  }

  const pick = async (id: string) => {
    setConfig({ threadId: id })
    await reloadHistory()
  }

  return (
    <div className="page">
      <div className="page-head">
        <h2>会话</h2>
        <span className="muted">
          会话 ID 是短期记忆的主键。切换会话后聊天页会重新拉取该会话的历史。
        </span>
      </div>

      <div className="toolbar">
        <button className="btn primary" onClick={() => void create()} disabled={loading}>
          ＋ 新建会话
        </button>
        <button className="btn ghost" onClick={() => void load()} disabled={loading}>
          刷新
        </button>
        <span className="grow" />
        <input
          className="text-input"
          placeholder="直接切换到指定 ID"
          value={manual}
          onChange={(e) => setManual(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && manual.trim()) {
              void pick(manual.trim())
              setManual('')
            }
          }}
        />
      </div>

      {threads.length === 0 ? (
        <p className="empty">
          {loading
            ? '加载中…'
            : '还没有任何历史会话。新建一个开始对话，发送消息后会自动出现在这里；也可以在下方直接输入会话 ID 切换。'}
        </p>
      ) : (
        <ul className="thread-list">
          {threads.map((t) => (
            <li key={t.thread_id} className={t.thread_id === threadId ? 'on' : ''}>
              <button className="thread-main" onClick={() => void pick(t.thread_id)}>
                <code>{t.thread_id}</code>
                <span className="muted">{t.message_count} 条</span>
              </button>
              <button className="btn tiny ghost" onClick={() => void remove(t.thread_id)}>
                删除
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
