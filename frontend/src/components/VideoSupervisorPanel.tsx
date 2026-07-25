/** 视频补齐 Supervisor 运行面板：覆盖率 / 预算 / 修复层级。 */
import { useEffect, useState } from 'react'
import AsyncButton from './AsyncButton'

export type VideoSupervisorSnapshot = {
  phase?: string
  goal?: string
  repair_epoch?: number
  tick_no?: number
  grant_id?: string | null
  budget?: {
    cap_cny?: number
    spent_cny?: number
    first_pass_soft_cap_cny?: number
    per_shot_cap_cny?: number
    remaining_cny?: number
  }
  coverage?: {
    A?: number
    B?: number
    C?: number
    total?: number
    fallback_quota?: number
    coverage_rate?: number
  }
  shot_state?: Record<string, {
    grade?: string
    last_issue_codes?: string[]
    repair_level?: string
    attempts_paid?: number
    attempts_budgeted?: number
    fallback_reason?: string
    continuity_degraded?: boolean
  }>
  last_plan?: {
    shot_no?: number
    level?: string
    strategy?: string
    reason?: string
    issue_codes?: string[]
  } | null
  outcome?: string | null
  pending_control?: { action: string; pending: boolean } | null
  active_video_run_id?: string | null
  running?: boolean
  ledger?: {
    entries?: Array<{
      shot_no: number
      grade: string
      last_issue_codes?: string[]
      repair_level?: string
      attempts_paid?: number
      attempts_budgeted?: number
      fallback_reason?: string | null
      continuity_degraded?: boolean
    }>
  }
}

const PHASE_LABEL: Record<string, string> = {
  PREFLIGHT: '预检中',
  PLANNING_COVERAGE: '重建覆盖台账',
  DISPATCHING: '正在派发',
  OBSERVING: '观察中',
  EVALUATING: '正在评估',
  REPAIRING: '正在修复',
  FINALIZING: '正在收尾',
  SUCCEEDED_COVERED: '全片已覆盖',
  PAUSED_EXTERNAL: '已暂停',
  PAUSED_BUDGET: '预算暂停',
  WAITING_AUTHORIZATION: '等待追加授权',
  WAITING_HUMAN: '已转交人工',
  CANCELLED: '已取消',
}

export default function VideoSupervisorPanel({
  api,
  episodeId,
  runId,
  supervisor,
  running,
  onChanged,
}: {
  api: typeof import('../api').api
  episodeId: string
  runId?: string | null
  supervisor: VideoSupervisorSnapshot | null | undefined
  running?: boolean
  onChanged?: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [live, setLive] = useState<VideoSupervisorSnapshot | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = () => {
      api.get(`/episodes/${episodeId}/video-completion`).then((data: VideoSupervisorSnapshot) => {
        if (!cancelled) setLive(data)
      }).catch(() => { /* ignore */ })
    }
    load()
    const id = window.setInterval(load, running ? 5000 : 15000)
    return () => { cancelled = true; window.clearInterval(id) }
  }, [api, episodeId, running])

  const snap = live || supervisor
  if (!snap && !running) return null

  const phase = snap?.phase || (running ? 'DISPATCHING' : '')
  const cov = snap?.coverage || {}
  const total = cov.total || 0
  const a = cov.A || 0
  const b = cov.B || 0
  const c = cov.C || 0
  const budget = snap?.budget || {}
  const spent = Number(budget.spent_cny || 0)
  const cap = Number(budget.cap_cny || 150)
  const soft = Number(budget.first_pass_soft_cap_cny || cap * 0.65)
  const pct = total > 0 ? Math.min(100, Math.round(((a + b) / total) * 100)) : 0
  const aPct = total > 0 ? (a / total) * 100 : 0
  const bPct = total > 0 ? (b / total) * 100 : 0
  const waitingAuth = phase === 'WAITING_AUTHORIZATION'
  const succeeded = phase === 'SUCCEEDED_COVERED'
  const pausedLike = ['PAUSED_EXTERNAL', 'PAUSED_BUDGET', 'WAITING_HUMAN', 'WAITING_AUTHORIZATION'].includes(phase)

  const uncovered = (snap?.ledger?.entries || []).filter(e => e.grade === 'C').slice(0, 6)

  const runAction = async (action: 'pause' | 'handoff' | 'resume' | 'topup' | 'cancel') => {
    setBusy(true)
    try {
      if (action === 'resume' || action === 'topup') {
        const body: Record<string, unknown> = {
          mode: 'resume',
          completion_grant_id: snap?.grant_id,
        }
        if (action === 'topup') {
          body.add_budget_cny = 50
          body.add_wall_clock_s = 3600
        }
        await api.post(`/episodes/${episodeId}/video-completion`, body)
      } else if (runId || snap?.active_video_run_id) {
        await api.post(`/runs/${runId || snap?.active_video_run_id}/${action === 'cancel' ? 'cancel' : action}`)
      }
      onChanged?.()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="video-supervisor-panel" data-phase={phase}>
      <div className="vsp-head">
        <strong>补齐到全片可用</strong>
        <span className="vsp-phase">{PHASE_LABEL[phase] || phase || '运行中'}</span>
        {typeof snap?.repair_epoch === 'number' && snap.repair_epoch > 0 && (
          <span className="vsp-epoch">修复周期 {snap.repair_epoch}</span>
        )}
      </div>

      <div className="vsp-coverage-bar" title={`A ${a} · B ${b} · 未覆盖 ${c}`}>
        <div className="vsp-seg a" style={{ width: `${aPct}%` }} />
        <div className="vsp-seg b" style={{ width: `${bPct}%` }} />
        <div className="vsp-seg c" style={{ width: `${Math.max(0, 100 - aPct - bPct)}%` }} />
      </div>
      <div className="vsp-cov-label">
        覆盖 {pct}% · A 级 {a} / B 级 {b} / 未覆盖 {c}
        {cov.fallback_quota != null ? `（B 配额 ${cov.fallback_quota}）` : ''}
      </div>

      <div className="vsp-budget">
        <div className="vsp-budget-track">
          <div className="vsp-budget-spent" style={{ width: `${Math.min(100, (spent / Math.max(cap, 0.01)) * 100)}%` }} />
          <div className="vsp-budget-soft" style={{ left: `${Math.min(100, (soft / Math.max(cap, 0.01)) * 100)}%` }} />
        </div>
        <div className="vsp-budget-label">
          预算 ¥{spent.toFixed(1)} / ¥{cap.toFixed(0)}
          （首轮软预算 ¥{soft.toFixed(0)}）
        </div>
      </div>

      {snap?.last_plan && (
        <div className="vsp-plan">
          第 {snap.last_plan.shot_no} 镜 → {snap.last_plan.level} {snap.last_plan.strategy}
          {snap.last_plan.reason ? `：${snap.last_plan.reason}` : ''}
        </div>
      )}

      {uncovered.length > 0 && (
        <ul className="vsp-issues">
          {uncovered.map(e => (
            <li key={e.shot_no}>
              第 {e.shot_no} 镜
              {(e.last_issue_codes || []).slice(0, 2).join(', ') || '待生成'}
              {e.repair_level ? ` → ${e.repair_level}` : ''}
              {` · ${e.attempts_paid || 0}/${e.attempts_budgeted || 0} 次`}
              {e.continuity_degraded ? ' · 衔接已降级' : ''}
            </li>
          ))}
        </ul>
      )}

      {succeeded && (
        <div className="vsp-done">
          ✓ 全部 {total} 镜均有可用视频 · A {a} · B {b}
          （在授权配额 {(cov.fallback_quota ?? 0)} 内）
          <div>✓ 覆盖报告已生成</div>
          <div>尚未拼接成片，尚未创建交付包</div>
        </div>
      )}

      <div className="vsp-actions">
        {!pausedLike && !succeeded && (runId || snap?.active_video_run_id) && (
          <>
            <AsyncButton className="btn ghost small" busyLabel="…" onAction={() => runAction('pause')} disabled={busy}>暂停</AsyncButton>
            <AsyncButton className="btn ghost small" busyLabel="…" onAction={() => runAction('handoff')} disabled={busy}>转人工</AsyncButton>
            <AsyncButton className="btn ghost small danger" busyLabel="…" onAction={() => runAction('cancel')} disabled={busy}>取消</AsyncButton>
          </>
        )}
        {(waitingAuth || phase === 'PAUSED_EXTERNAL' || phase === 'WAITING_HUMAN') && (
          <AsyncButton className="btn primary small" busyLabel="…" onAction={() => runAction(waitingAuth ? 'topup' : 'resume')} disabled={busy}>
            {waitingAuth ? '追加预算并继续' : '继续补齐'}
          </AsyncButton>
        )}
      </div>
    </div>
  )
}
