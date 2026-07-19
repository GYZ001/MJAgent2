import { useEffect, useState } from 'react'
import { api, ArtifactEvidence, RunEvent, RunSummary, StepRun } from '../../api'
import EvidenceDrawer from './EvidenceDrawer'

interface GateArtifact {
  id: string; type: string; scope_type: string; scope_id: string
  trust_level: string; episode_no?: number | null; episode_title?: string | null
}

const STATUS_LABELS: Record<string, string> = {
  CREATED: '待启动', RUNNING: '运行中', WAITING_RETRY: '等待重试',
  WAITING_HUMAN: '等待人工', PAUSED_BUDGET: '预算暂停',
  PAUSED_EXTERNAL: '外部中断', SUCCEEDED: '已完成', PARTIAL: '部分完成',
  FAILED: '失败', CANCELLED: '已取消',
}

function formatTime(value?: number | null) {
  return value ? new Date(value * 1000).toLocaleString() : '—'
}

export default function RunCenter() {
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [steps, setSteps] = useState<StepRun[]>([])
  const [events, setEvents] = useState<RunEvent[]>([])
  const [gates, setGates] = useState<GateArtifact[]>([])
  const [gateEvidence, setGateEvidence] = useState<Record<string, ArtifactEvidence>>({})

  useEffect(() => {
    const refresh = () => api.get('/runs?limit=50').then((items: RunSummary[]) => {
      setRuns(items)
      setSelected(current => current ?? items[0]?.id ?? null)
    })
    const refreshGates = () => api.get('/gates?limit=100').then((items: GateArtifact[]) => {
      setGates(items)
      items.forEach(item => {
        api.get(`/artifacts/${item.id}`).then((evidence: ArtifactEvidence) => {
          setGateEvidence(current => ({ ...current, [item.id]: evidence }))
        }).catch(() => undefined)
      })
    })
    refresh()
    refreshGates()
    const timer = window.setInterval(refresh, 3000)
    const gateTimer = window.setInterval(refreshGates, 5000)
    return () => { window.clearInterval(timer); window.clearInterval(gateTimer) }
  }, [])

  useEffect(() => {
    if (!selected) {
      setSteps([])
      setEvents([])
      return
    }
    const refresh = () => Promise.all([
      api.get('/runs/' + selected + '/steps'),
      api.get('/runs/' + selected + '/events?limit=100'),
    ]).then(([nextSteps, nextEvents]: [StepRun[], RunEvent[]]) => {
      setSteps(nextSteps)
      setEvents(nextEvents)
    })
    refresh()
    const timer = window.setInterval(refresh, 2500)
    return () => window.clearInterval(timer)
  }, [selected])

  const current = runs.find(run => run.id === selected)
  return (
    <section className="card run-center">
      <div className="run-center-head">
        <div>
          <span className="eyebrow">EVIDENCE HARNESS</span>
          <h3>运行中心</h3>
          <p>持久化展示每次运行、步骤、退出原因和证据产物。</p>
        </div>
        <span className="stamp gold">{runs.length} 次运行</span>
      </div>
      <div className="gate-queue">
        <div className="gate-queue-head"><b>人工门禁队列</b><span>{gates.length} 项待处理</span></div>
        {!gates.length ? <span className="gate-empty">当前没有待人工决定的核心产物</span> : gates.map(item => (
          <div className="gate-item" key={item.id}>
            <div><b>{item.type}</b><span>{item.episode_no ? `第 ${item.episode_no} 集 · ${item.episode_title || ''}` : `${item.scope_type}:${item.scope_id}`}</span></div>
            <code>{item.id}</code>
            {gateEvidence[item.id] && <EvidenceDrawer evidence={gateEvidence[item.id]} label="定位问题与证据" />}
          </div>
        ))}
      </div>
      {!runs.length ? (
        <div className="empty" style={{ padding: 26 }}>尚无 Harness 运行记录</div>
      ) : (
        <div className="run-center-grid">
          <div className="run-list">
            {runs.map(run => (
              <button
                type="button"
                key={run.id}
                className={run.id === selected ? 'active' : ''}
                onClick={() => setSelected(run.id)}
              >
                <b>{run.current_step_key || run.workflow_type}</b>
                <span>{STATUS_LABELS[run.status] || run.status} · {formatTime(run.updated_at)}</span>
                {run.failure_message && <small>{run.failure_message}</small>}
              </button>
            ))}
          </div>
          <div className="run-detail">
            {current && (
              <div className="run-detail-summary">
                <b>{current.workflow_type}</b>
                <code>{current.id}</code>
                <span>{current.scope_type}:{current.scope_id} · ¥{current.cost_cny.toFixed(2)}</span>
              </div>
            )}
            <div className="run-timeline">
              {steps.map(step => (
                <div className={'run-step ' + step.status.toLowerCase()} key={step.id}>
                  <span className="run-step-dot" />
                  <div>
                    <b>{step.step_key}</b>
                    <span>{STATUS_LABELS[step.status] || step.status} · {(step.latency_ms / 1000).toFixed(1)} 秒</span>
                    {(step.error_message || step.exit_reason) && <small>{step.error_message || step.exit_reason}</small>}
                  </div>
                </div>
              ))}
            </div>
            {!!events.length && (
              <details className="run-events">
                <summary>审计事件（{events.length}）</summary>
                {events.slice().reverse().map(event => (
                  <div key={event.id}>
                    <time>{formatTime(event.ts)}</time>
                    <span>{event.message}</span>
                  </div>
                ))}
              </details>
            )}
          </div>
        </div>
      )}
    </section>
  )
}
