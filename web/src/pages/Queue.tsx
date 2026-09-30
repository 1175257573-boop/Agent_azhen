import { useState } from 'react'

import { api } from '../api'
import { useStore } from '../store'

export default function Queue() {
  const { queue, refreshQueue, threadId, toast } = useStore()
  const [text, setText] = useState('')
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null)

  const add = async () => {
    const value = text.trim()
    if (!value) return
    try {
      await api.queueAdd(threadId, value)
      setText('')
      await refreshQueue()
      toast('ok', '已入队')
    } catch (err) {
      toast('error', `入队失败：${err instanceof Error ? err.message : err}`)
    }
  }

  const saveEdit = async () => {
    if (!editing) return
    const value = editing.text.trim()
    if (!value) return
    try {
      await api.queueAdd(threadId, value, editing.id)
      setEditing(null)
      await refreshQueue()
      toast('ok', '已更新')
    } catch (err) {
      toast('error', `更新失败：${err instanceof Error ? err.message : err}`)
    }
  }

  const remove = async (id: string) => {
    try {
      await api.queueRemove(threadId, id)
      await refreshQueue()
    } catch (err) {
      toast('error', `撤回失败：${err instanceof Error ? err.message : err}`)
    }
  }

  const clear = async () => {
    try {
      const res = await api.queueClear(threadId)
      await refreshQueue()
      toast('ok', `已清空 ${res.cleared} 条`)
    } catch (err) {
      toast('error', `清空失败：${err instanceof Error ? err.message : err}`)
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <h2>消息队列</h2>
        <span className="muted">
          一轮对话要跑几十秒，这期间的输入先入队，本轮结束后由服务端按序自动发出。
        </span>
      </div>

      <div className="toolbar">
        <input
          className="text-input grow"
          placeholder="输入要排队的消息"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void add()
          }}
        />
        <button className="btn primary" onClick={() => void add()} disabled={!text.trim()}>
          入队
        </button>
        <button className="btn ghost" onClick={() => void refreshQueue()}>
          刷新
        </button>
        <button className="btn ghost" onClick={() => void clear()} disabled={!queue.length}>
          清空
        </button>
      </div>

      <div className="muted small">当前会话：<code>{threadId}</code></div>

      {queue.length === 0 ? (
        <p className="empty">队列为空。</p>
      ) : (
        <ul className="queue-list">
          {queue.map((q) => (
            <li key={q.id}>
              {editing?.id === q.id ? (
                <div className="queue-edit">
                  <textarea
                    rows={3}
                    value={editing.text}
                    onChange={(e) => setEditing({ id: q.id, text: e.target.value })}
                  />
                  <div className="btn-row">
                    <button className="btn primary tiny" onClick={() => void saveEdit()}>
                      保存
                    </button>
                    <button className="btn tiny ghost" onClick={() => setEditing(null)}>
                      取消
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  <div className="queue-main">
                    <span className="seq">#{q.seq}</span>
                    <span className="text">{q.text}</span>
                  </div>
                  <div className="btn-row">
                    <button className="btn tiny" onClick={() => setEditing({ id: q.id, text: q.text })}>
                      编辑
                    </button>
                    <button className="btn tiny ghost" onClick={() => void remove(q.id)}>
                      撤回
                    </button>
                  </div>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
