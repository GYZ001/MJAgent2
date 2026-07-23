import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { useNav } from '../App'
import AgentComposer from './AgentComposer'
import ApprovalCard from './ApprovalCard'
import ContextChips from './ContextChips'
import EvidenceCitation from './EvidenceCitation'
import PlanCard from './PlanCard'
import RunProgressCard from './RunProgressCard'
import ToolCallCard from './ToolCallCard'
import type { ApprovalCardData, ContextEnvelope, UiIntent } from './types'
import { applyUiIntent } from './uiBridge'
import { useAgentStream } from './useAgentStream'

export default function AgentDrawer({
  open,
  onClose,
  context,
}: {
  open: boolean
  onClose: () => void
  context: ContextEnvelope
}) {
  const { go, toast } = useNav()
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [turnId, setTurnId] = useState<string | null>(null)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [assistantText, setAssistantText] = useState('')
  const [planSteps, setPlanSteps] = useState<string[]>([])
  const [approvals, setApprovals] = useState<ApprovalCardData[]>([])
  const [pendingIntent, setPendingIntent] = useState<UiIntent | null>(null)
  const [toolCards, setToolCards] = useState<{
    id: string; name: string; status: string; summary?: string; runId?: string; risk?: string
  }[]>([])
  const [linkedRuns, setLinkedRuns] = useState<{ runId: string; summary?: string }[]>([])
  const [citations, setCitations] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)

  const streaming = Boolean(turnId) && sending
  const { events, status: streamStatus, reset: resetStream } = useAgentStream(turnId, Boolean(turnId))

  useEffect(() => {
    if (!open || conversationId) return
    let cancelled = false
    api.post('/agent/conversations', {
      project_id: context.project_id ?? null,
      title: context.project_id ? `项目 ${context.project_id.slice(0, 8)}` : '案头助手',
    }).then((conv: { id: string }) => {
      if (!cancelled) setConversationId(conv.id)
    }).catch((err: Error) => {
      if (!cancelled) setError(err.message)
    })
    return () => { cancelled = true }
  }, [open, conversationId, context.project_id])

  useEffect(() => {
    for (const ev of events) {
      const p = ev.payload || {}
      if (ev.event_type === 'assistant.delta') {
        const delta = String(p.text ?? p.delta ?? p.reply ?? '')
        if (delta) setAssistantText(prev => prev + delta)
      }
      if (ev.event_type === 'plan.updated') {
        const steps = Array.isArray(p.steps) ? p.steps.map(String) : []
        if (steps.length) setPlanSteps(steps)
        else if (p.reply) setAssistantText(String(p.reply))
      }
      if (ev.event_type === 'tool.proposed' || ev.event_type === 'tool.started') {
        const id = String(p.tool_call_id ?? p.id ?? `${ev.event_id}`)
        const name = String(p.command ?? p.tool ?? p.name ?? 'tool')
        setToolCards(prev => {
          if (prev.some(t => t.id === id)) {
            return prev.map(t => t.id === id ? {
              ...t,
              status: String(p.status ?? ev.event_type),
              summary: p.summary ? String(p.summary) : t.summary,
            } : t)
          }
          return [...prev, {
            id,
            name,
            status: String(p.status ?? ev.event_type),
            summary: p.summary ? String(p.summary) : undefined,
            risk: p.risk ? String(p.risk) : undefined,
            runId: p.run_id ? String(p.run_id) : undefined,
          }]
        })
      }
      if (ev.event_type === 'tool.completed' || ev.event_type === 'tool.failed') {
        const id = String(p.tool_call_id ?? p.id ?? '')
        setToolCards(prev => prev.map(t => t.id === id ? {
          ...t,
          status: ev.event_type === 'tool.failed' ? 'failed' : String(p.status ?? 'succeeded'),
          summary: p.summary ? String(p.summary) : t.summary,
          runId: p.run_id ? String(p.run_id) : t.runId,
        } : t))
        if (Array.isArray(p.resource_uris)) {
          for (const uri of p.resource_uris) {
            const s = String(uri)
            if (s.includes('/artifacts/')) {
              const aid = s.split('/artifacts/')[1]
              if (aid) setCitations(prev => prev.includes(aid) ? prev : [...prev, aid])
            }
          }
        }
      }
      if (ev.event_type === 'approval.required') {
        const card: ApprovalCardData = {
          tool_call_id: String(p.tool_call_id),
          command: String(p.command ?? p.tool ?? ''),
          title: String(p.title ?? p.command ?? p.tool ?? '操作'),
          summary: String(p.summary ?? ''),
          risk: String(p.risk ?? 'R2'),
          estimated_cost_cny: typeof p.estimated_cost_cny === 'number' ? p.estimated_cost_cny : null,
          warnings: Array.isArray(p.warnings) ? p.warnings.map(String) : [],
          approval_token: p.approval_token ? String(p.approval_token) : undefined,
          expires_at: typeof p.expires_at === 'number' ? p.expires_at : undefined,
          affected: typeof p.affected === 'object' && p.affected ? p.affected as Record<string, unknown> : undefined,
        }
        setApprovals(prev => prev.some(a => a.tool_call_id === card.tool_call_id) ? prev : [...prev, card])
      }
      if (ev.event_type === 'run.linked') {
        const runId = String(p.run_id ?? '')
        if (runId) {
          setLinkedRuns(prev => prev.some(r => r.runId === runId) ? prev : [...prev, {
            runId,
            summary: p.summary ? String(p.summary) : undefined,
          }])
        }
      }
      if (ev.event_type === 'ui.intent' && (p.intent || p.ui_intent)) {
        setPendingIntent((p.intent ?? p.ui_intent) as UiIntent)
      }
      if (ev.event_type === 'turn.completed' || ev.event_type === 'turn.cancelled') {
        setSending(false)
        if (p.reply || p.message) setAssistantText(String(p.reply ?? p.message))
      }
      if (ev.event_type === 'tool.failed' && p.error_id) {
        setError(`工具失败 · ${p.error_code ?? ''} · ${p.error_id}`)
      }
    }
  }, [events])

  const send = useCallback(async () => {
    if (!conversationId || !input.trim() || sending) return
    setSending(true)
    setError(null)
    setAssistantText('')
    setPlanSteps([])
    setApprovals([])
    setToolCards([])
    setLinkedRuns([])
    setCitations([])
    setPendingIntent(null)
    resetStream()
    try {
      const resp = await api.post(`/agent/conversations/${conversationId}/messages`, {
        content: input.trim(),
        context,
      }) as { turn_id: string }
      setInput('')
      setTurnId(resp.turn_id)
    } catch (err) {
      setSending(false)
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [conversationId, input, sending, context, resetStream])

  const stop = useCallback(async () => {
    if (!turnId) return
    try {
      await api.post(`/agent/turns/${turnId}/cancel`, { cancel_run: false })
      toast('已停止本轮对话（底层 Run 未取消）')
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), true)
    } finally {
      setSending(false)
    }
  }, [turnId, toast])

  const approve = useCallback(async (toolCallId: string, reason: string) => {
    await api.post(`/agent/tool-calls/${toolCallId}/approve`, { reason })
    setApprovals(prev => prev.filter(a => a.tool_call_id !== toolCallId))
  }, [])

  const reject = useCallback(async (toolCallId: string, reason: string) => {
    await api.post(`/agent/tool-calls/${toolCallId}/reject`, { reason })
    setApprovals(prev => prev.filter(a => a.tool_call_id !== toolCallId))
  }, [])

  const followIntent = useCallback(() => {
    if (!pendingIntent) return
    const result = applyUiIntent(pendingIntent, go, { toast })
    if (!result.ok) toast(result.message || '定位失败', true)
  }, [pendingIntent, go, toast])

  const header = useMemo(() => (
    <div className="agent-drawer-head">
      <div>
        <b>案头助手</b>
        <span className="agent-stream-status">{streamStatus}</span>
      </div>
      <button type="button" className="btn" onClick={onClose} aria-label="关闭助手">收起</button>
    </div>
  ), [onClose, streamStatus])

  if (!open) return null

  return (
    <aside className="agent-drawer" aria-label="案头助手">
      {header}
      <ContextChips context={context} />
      <div className="agent-transcript">
        {error && <div className="agent-error" role="alert">{error}</div>}
        <PlanCard steps={planSteps} />
        {assistantText && (
          <section className="agent-card assistant-card">
            <h4>助手</h4>
            <p className="agent-assistant-text">{assistantText}</p>
          </section>
        )}
        {toolCards.map(card => (
          <ToolCallCard key={card.id} {...card} />
        ))}
        {approvals.map(card => (
          <ApprovalCard
            key={card.tool_call_id}
            data={card}
            onApprove={reason => approve(card.tool_call_id, reason)}
            onReject={reason => reject(card.tool_call_id, reason)}
          />
        ))}
        {linkedRuns.map(run => (
          <RunProgressCard
            key={run.runId}
            runId={run.runId}
            summary={run.summary}
            onOpen={() => go('monitor')}
          />
        ))}
        {citations.map(id => (
          <EvidenceCitation key={id} artifactId={id} onOpen={() => toast(`证据 ${id}`)} />
        ))}
        {pendingIntent && (
          <div className="agent-card">
            <p>助手建议定位到页面</p>
            <button type="button" className="btn primary" onClick={followIntent}>定位</button>
          </div>
        )}
      </div>
      <AgentComposer
        value={input}
        onChange={setInput}
        onSend={send}
        onStop={stop}
        disabled={sending || !conversationId}
        stopping={streaming}
      />
    </aside>
  )
}
