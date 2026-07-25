"""分镜 Supervisor 运行面板（PRD §14.2）。"""
import { useEffect, useState } from 'react'
import { api } from '../api'
import AsyncButton from './AsyncButton'

export type SupervisorSnapshot = {
  phase: string
  goal?: string
  completion_mode?: string
  repair_epoch: number
  validated_prefix_end: number
  next_shot_no: number
  expected_total: number
  outcome?: string | null
  strategy?: string
  frontier?: number
  issue_codes?: string[]
  last_repair?: Record<string, unknown> | null
  completion_grant_id?: string | null
  pending_control?: { action: string; pending: boolean } | null
}

const PHASE_LABEL: Record<string, string> = {
  PREFLIGHT: '预检',
  PLANNING_OUTLINE: '规划大纲',
  VALIDATING_OUTLINE: '校验大纲',
  GENERATING_SHOTS: '生成镜头',
  VALIDATING_EPISODE: '整集检查',
  REPAIRING: '自动修复',
  PREPARING_CONFIRM: '准备确认',
  CONFIRMING: '自动确认中',
  SUCCEEDED: '已完成',
  PAUSED_EXTERNAL: '已暂停',
  PAUSED_BUDGET: '预算暂停',
  WAITING_AUTHORIZATION: '等待授权',
  WAITING_HUMAN: '等待人工',
  WAITING_RETRY: '等待重试',
  CANCELLED: '已取消',
}

const STRATEGY_LABEL: Record<string, string> = {
  normalize: '确定性归一',
  repair_current: '修当前镜',
  repair_window: '修相邻窗口',
  redo_suffix: '重做后缀',
  split_adjacent_shot: '相邻插镜',
  replan_outline: '重规划大纲',
  waiting_human: '等待人工',
  waiting_retry: '等待重试',
  waiting_authorization: '等待授权',
}

export default function SupervisorPanel({
  api,
  episodeId,
  runId,
  supervisor,
  scripting,
  onChanged,
}: {
  api: typeof import('../api').api
  episodeId: string
  runId?: string | null
  supervisor: SupervisorSnapshot | null | undefined
  scripting: boolean
  onChanged?: () => void
}) {
  const [events, setEvents] = useState<Array<{ event_type: string; message: string; ts?: number }>>([])
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!runId) {
      setEvents([])
      return
    }
    let cancelled = false
    const load = () => {
      api.get(`/runs/${runId}/events?limit=30`).then((rows: any[]) => {
        if (!cancelled) setEvents(Array.isArray(rows) ? rows.slice().reverse() : [])
      }).catch(() => { /* ignore */ })
    }
    load()
    const id = window.setInterval(load, scripting ? 4000 : 12000)
    return () => { cancelled = true; window.clearInterval(id) }
  }, [api, runId, scripting])

  if (!supervisor && !scripting) return null

  const phase = supervisor?.phase || (scripting ? 'GENERATING_SHOTS' : '')
  const prefix = supervisor?.validated_prefix_end ?? 0
  const total = supervisor?.expected_total || 0
  const auto = supervisor?.completion_mode === 'auto_confirm' || supervisor?.goal === 'generate_and_confirm'
  const pausedLike = ['PAUSED_EXTERNAL', 'PAUSED_BUDGET', 'WAITING_RETRY', 'WAITING_HUMAN', 'WAITING_AUTHORIZATION'].includes(phase)
  const canControl = scripting || pausedLike

  const runAction = async (action: 'pause' | 'handoff' | 'resume' | 'cancel') => {
    if (!runId && action !== 'cancel' && action !== 'resume') return
    setBusy(true)
    try {
      if (action === 'cancel') {
        await api.post(`/episodes/${episodeId}/storyboard/cancel`)
      } else if (action === 'resume') {
        // 优先走分镜 resume（保留 completion_mode / grant）；run 可能已是 PARTIAL
        await api.post(`/episodes/${episodeId}/storyboard/resume`)
      } else if (runId) {
        await api.post(`/runs/${runId}/${action}`)
      }
      onChanged?.()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="supervisor-panel" style={{
      marginTop: 12, padding: '12px 14px', border: '1px solid var(--line, #ddd)',
      borderRadius: 8, background: 'var(--paper, #faf8f5)',
    }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center' }}>
        <strong>{auto ? '自动完成并确认' : '生成并等待确认'}</strong>
        <span className="stamp gold">{PHASE_LABEL[phase] || phase || '运行中'}</span>
        {supervisor && supervisor.repair_epoch > 0 && (
          <span className="stamp grey">修复周期 {supervisor.repair_epoch}</span>
        )}
        {supervisor?.pending_control?.pending && (
          <span className="stamp grey">待执行：{supervisor.pending_control.action}</span>
        )}
        {supervisor?.outcome === 'SUCCEEDED_CONFIRMED' && (
          <span className="stamp green">已自动确认 · 尚未产生视频费用</span>
        )}
        {supervisor?.outcome === 'SUCCEEDED_READY_FOR_CONFIRM' && (
          <span className="stamp green">已通过 · 等待确认</span>
        )}
      </div>
      <div style={{ marginTop: 8, fontSize: 13, color: 'var(--ink-soft)', lineHeight: 1.55 }}>
        <div>
          已验证：{prefix > 0 ? `1–${prefix}` : '无'} 镜
          {total > 0 ? ` / 计划 ${total} 镜` : ''}
          {supervisor?.next_shot_no ? ` · 下一镜 ${String(supervisor.next_shot_no).padStart(2, '0')}` : ''}
        </div>
        {supervisor?.strategy && (
          <div>
            当前策略：{STRATEGY_LABEL[supervisor.strategy] || supervisor.strategy}
            {supervisor.frontier ? `（失效边界第 ${supervisor.frontier} 镜）` : ''}
          </div>
        )}
        {!!supervisor?.issue_codes?.length && (
          <div>最近 Issue：{(supervisor.issue_codes || []).slice(0, 4).join(' · ')}</div>
        )}
        {supervisor?.completion_grant_id && auto && (
          <div>自动确认授权：已签发</div>
        )}
      </div>
      {canControl && (
        <div style={{ marginTop: 10, display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {scripting && !pausedLike && runId && (
            <>
              <AsyncButton className="btn ghost" disabled={busy} busyLabel="暂停中…"
                onAction={async () => { await runAction('pause') }}>暂停</AsyncButton>
              <AsyncButton className="btn ghost" disabled={busy} busyLabel="转交中…"
                onAction={async () => { await runAction('handoff') }}>转人工</AsyncButton>
            </>
          )}
          {pausedLike && (
            <AsyncButton className="btn primary" disabled={busy} busyLabel="恢复中…"
              onAction={async () => { await runAction('resume') }}>继续自动修复</AsyncButton>
          )}
          {(scripting || pausedLike) && (
            <AsyncButton className="btn ghost" disabled={busy} busyLabel="取消中…"
              onAction={async () => { await runAction('cancel') }}>取消</AsyncButton>
          )}
        </div>
      )}
      {events.length > 0 && (
        <details style={{ marginTop: 10 }}>
          <summary style={{ cursor: 'pointer', fontSize: 13 }}>最近事件（{events.length}）</summary>
          <ul style={{ margin: '8px 0 0', paddingLeft: 18, fontSize: 12, color: 'var(--ink-soft)' }}>
            {events.slice(0, 12).map((ev, i) => (
              <li key={`${ev.event_type}-${i}`}>
                <code>{ev.event_type}</code> {ev.message}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}
