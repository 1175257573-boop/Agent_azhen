import ApprovalCard from '../components/ApprovalCard'
import { useStore } from '../store'
import { useState } from 'react'

export default function Approvals() {
  const { pending, decide, dismiss, toast } = useStore()
  const [busyId, setBusyId] = useState<string | null>(null)

  const onDecide = async (id: string, decisions: Parameters<typeof decide>[1]) => {
    setBusyId(id)
    try {
      await decide(id, decisions)
      toast('ok', '决策已提交，Agent 继续执行')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <h2>待审批动作</h2>
        <span className="muted">
          写文件等高危工具会在执行前中断，等你批准 / 改参数 / 拒绝。决策提交后由 Agent 继续执行。
        </span>
      </div>

      {pending.length === 0 ? (
        <p className="empty">当前没有待审批的动作。</p>
      ) : (
        <div className="approval-list">
          {pending.map((p) => (
            <ApprovalCard
              key={p.id}
              approval={p}
              busy={busyId === p.id}
              onDecide={(decisions) => void onDecide(p.id, decisions)}
              onDismiss={() => dismiss(p.id)}
            />
          ))}
        </div>
      )}
    </div>
  )
}
