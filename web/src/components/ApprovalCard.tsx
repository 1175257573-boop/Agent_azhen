import { useState } from 'react'

import type { ActionRequest, Decision, DecisionType, PendingApproval } from '../types'

const LABEL: Record<string, string> = {
  approve: '批准',
  edit: '改参数后执行',
  reject: '拒绝',
  respond: '回复',
}

interface Draft {
  type: DecisionType | null
  args: string
  message: string
}

function allowedFor(approval: PendingApproval, index: number): DecisionType[] {
  const name = approval.payload.action_requests?.[index]?.name ?? ''
  const cfg = approval.payload.review_configs?.find((c) => c.action_name === name)
  const list = cfg?.allowed_decisions ?? ['approve', 'edit', 'reject']
  return list.filter((d): d is DecisionType => d in LABEL)
}

function pretty(value: unknown): string {
  try {
    return JSON.stringify(value ?? {}, null, 2)
  } catch {
    return String(value)
  }
}

export default function ApprovalCard({
  approval,
  onDecide,
  onDismiss,
  busy,
}: {
  approval: PendingApproval
  onDecide: (decisions: Decision[]) => void
  onDismiss?: () => void
  busy?: boolean
}) {
  const requests: ActionRequest[] = approval.payload.action_requests ?? []
  const [drafts, setDrafts] = useState<Draft[]>(() =>
    requests.map((r) => ({ type: null, args: pretty(r.args), message: '' })),
  )

  const patch = (i: number, next: Partial<Draft>) =>
    setDrafts((prev) => prev.map((d, idx) => (idx === i ? { ...d, ...next } : d)))

  const ready = requests.length > 0 && drafts.every((d) => d.type !== null)

  const submit = () => {
    if (!ready) return
    const decisions: Decision[] = requests.map((r, i) => {
      const d = drafts[i]
      if (d.type === 'edit') {
        let args: Record<string, unknown> = {}
        try {
          args = JSON.parse(d.args) as Record<string, unknown>
        } catch {
          args = {}
        }
        return { type: 'edit', edited_action: { name: r.name, args } }
      }
      if (d.type === 'reject' || d.type === 'respond') return { type: d.type, message: d.message }
      return { type: 'approve' }
    })
    onDecide(decisions)
  }

  if (!requests.length) {
    return (
      <div className="approval">
        <div className="approval-head">
          <strong>需要人工确认</strong>
          <span className="muted">{approval.thread_id}</span>
        </div>
        <pre className="code">{pretty(approval.payload)}</pre>
        <div className="approval-actions">
          <button className="btn" onClick={() => onDecide([{ type: 'approve' }])} disabled={busy}>
            批准
          </button>
          <button className="btn" onClick={() => onDecide([{ type: 'reject', message: '用户拒绝' }])} disabled={busy}>
            拒绝
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="approval">
      <div className="approval-head">
        <strong>需要人工确认（{requests.length} 个动作）</strong>
        <span className="muted">
          {approval.thread_id} · {new Date(approval.created_at).toLocaleTimeString()}
        </span>
      </div>

      {requests.map((r, i) => {
        const options = allowedFor(approval, i)
        const draft = drafts[i]
        return (
          <div className="approval-item" key={`${r.name}-${i}`}>
            <div className="approval-title">
              <code>{r.name}</code>
              {r.description ? <span className="muted">{r.description}</span> : null}
            </div>

            <pre className="code">{draft.args}</pre>

            <div className="decision-row">
              {options.map((opt) => (
                <button
                  key={opt}
                  className={`chip${draft.type === opt ? ' on' : ''}`}
                  onClick={() => patch(i, { type: opt })}
                  disabled={busy}
                >
                  {LABEL[opt]}
                </button>
              ))}
            </div>

            {draft.type === 'edit' ? (
              <textarea
                className="code-edit"
                rows={5}
                value={draft.args}
                onChange={(e) => patch(i, { args: e.target.value })}
                spellCheck={false}
                placeholder='{"filename": "x.md", "content": "..."}'
              />
            ) : null}

            {draft.type === 'reject' || draft.type === 'respond' ? (
              <input
                className="text-input"
                value={draft.message}
                onChange={(e) => patch(i, { message: e.target.value })}
                placeholder="给 Agent 的说明（可选）"
              />
            ) : null}
          </div>
        )
      })}

      <div className="approval-actions">
        <button className="btn primary" onClick={submit} disabled={!ready || busy}>
          {busy ? '提交中…' : '提交决策'}
        </button>
        {onDismiss ? (
          <button className="btn ghost" onClick={onDismiss} disabled={busy}>
            稍后处理
          </button>
        ) : null}
      </div>
    </div>
  )
}
