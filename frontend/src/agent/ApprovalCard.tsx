import { useState } from 'react'
import type { ApprovalCardData } from './types'

export default function ApprovalCard({
  data,
  onApprove,
  onReject,
}: {
  data: ApprovalCardData
  onApprove: (reason: string) => Promise<void> | void
  onReject: (reason: string) => Promise<void> | void
}) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)

  const run = async (fn: (reason: string) => Promise<void> | void) => {
    setBusy(true)
    try {
      await fn(reason.trim())
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="agent-card approval-card" role="dialog" aria-label="需要批准">
      <h4>需要批准：{data.title || data.command}</h4>
      <p>{data.summary}</p>
      <ul>
        <li>风险等级：{data.risk}</li>
        {data.estimated_cost_cny != null && <li>预计费用：¥{data.estimated_cost_cny}</li>}
        {(data.warnings ?? []).map(w => <li key={w}>{w}</li>)}
      </ul>
      <label className="agent-reason">
        理由 / 风险接受说明
        <textarea
          rows={2}
          value={reason}
          onChange={e => setReason(e.target.value)}
          placeholder="采用、覆盖、带风险批准等决策请填写理由"
        />
      </label>
      <div className="dialog-actions">
        <button type="button" className="btn" disabled={busy} onClick={() => run(onReject)}>拒绝</button>
        <button type="button" className="btn primary" disabled={busy} onClick={() => run(onApprove)}>
          批准一次
        </button>
      </div>
    </section>
  )
}
