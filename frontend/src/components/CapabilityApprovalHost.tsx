import { useEffect, useState } from 'react'
import {
  describeImpact,
  getPendingApproval,
  resolveCapabilityApproval,
  subscribeApprovalPrompt,
  type WaitingApprovalPayload,
} from '../capabilityApproval'

/** 全局批准卡：拦截 REST 202 waiting_approval，展示服务端 Impact。 */
export default function CapabilityApprovalHost() {
  const [payload, setPayload] = useState<WaitingApprovalPayload | null>(getPendingApproval())

  useEffect(() => subscribeApprovalPrompt(() => {
    setPayload(getPendingApproval())
  }), [])

  if (!payload) return null
  const lines = describeImpact(payload)

  return (
    <div
      className="evidence-backdrop"
      role="presentation"
      onMouseDown={event => {
        if (event.currentTarget === event.target) resolveCapabilityApproval(false)
      }}
    >
      <section className="impact-dialog" role="dialog" aria-modal="true" aria-label="操作批准">
        <h3>{payload.command ? `批准：${payload.command}` : '需要批准后继续'}</h3>
        <p>以下影响来自服务端预检；未批准前不会执行任何业务变更。</p>
        <ul>
          {lines.map(line => <li key={line}>{line}</li>)}
        </ul>
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={() => resolveCapabilityApproval(false)}>
            拒绝
          </button>
          <button type="button" className="btn primary" onClick={() => resolveCapabilityApproval(true)}>
            批准一次
          </button>
        </div>
      </section>
    </div>
  )
}
