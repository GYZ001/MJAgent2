/** 分镜 Supervisor 运行轨：融入工具栏，不叠第二层卡片。 */
import { useEffect, useMemo, useState } from 'react'
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

type RunEvent = {
  event_type: string
  message: string
  ts?: number
  level?: string
}

const PHASE_STEPS = [
  { id: 'outline', label: '大纲', phases: ['PREFLIGHT', 'PLANNING_OUTLINE', 'VALIDATING_OUTLINE'] },
  { id: 'shots', label: '逐镜', phases: ['GENERATING_SHOTS'] },
  { id: 'episode', label: '整集', phases: ['VALIDATING_EPISODE', 'REPAIRING'] },
  { id: 'confirm', label: '确认', phases: ['PREPARING_CONFIRM', 'CONFIRMING'] },
  { id: 'done', label: '完成', phases: ['SUCCEEDED'] },
] as const

const PHASE_LABEL: Record<string, string> = {
  PREFLIGHT: '预检中',
  PLANNING_OUTLINE: '正在规划大纲',
  VALIDATING_OUTLINE: '正在校验大纲',
  GENERATING_SHOTS: '正在生成镜头',
  VALIDATING_EPISODE: '正在整集检查',
  REPAIRING: '正在自动修复',
  PREPARING_CONFIRM: '准备确认',
  CONFIRMING: '正在自动确认',
  SUCCEEDED: '已完成',
  PAUSED_EXTERNAL: '已暂停',
  PAUSED_BUDGET: '预算已暂停',
  WAITING_AUTHORIZATION: '等待重新授权',
  WAITING_HUMAN: '已转交人工',
  WAITING_RETRY: '等待重试',
  CANCELLED: '已取消',
}

const STRATEGY_LABEL: Record<string, string> = {
  normalize: '整理格式',
  repair_current: '微调当前镜',
  repair_window: '修复相邻镜头',
  redo_suffix: '重做后续镜头',
  split_adjacent_shot: '拆分相邻镜头',
  replan_outline: '重规划大纲',
  waiting_human: '等待人工',
  waiting_retry: '等待重试',
  waiting_authorization: '等待授权',
}

const ISSUE_LABEL: Record<string, string> = {
  SPOKEN_CAPACITY_EXCEEDED: '口播超容量',
  STATE_CHAIN_INVALID: '镜头衔接',
  SPINE_MISSING: '主线缺失',
  KEY_LINE_MISSING: '关键台词',
  SCHEMA_INVALID: '结构问题',
  PLAN_EXHAUSTED_NOT_FINAL: '未正常收束',
}

/** 噪声事件：checkpoint / 迭代心跳等不对用户展示 */
const HIDDEN_EVENTS = new Set([
  'STORYBOARD_SUPERVISOR_CHECKPOINT',
  'AGENT_ITERATION_STARTED',
  'AGENT_ITERATION_FINISHED',
  'STEP_STARTED',
  'STEP_FINISHED',
])

function humanizeEvent(ev: RunEvent): { title: string; detail?: string } | null {
  if (HIDDEN_EVENTS.has(ev.event_type)) return null
  const msg = (ev.message || '').trim()
  const map: Record<string, (m: string) => { title: string; detail?: string }> = {
    STORYBOARD_SUPERVISOR_STARTED: () => ({ title: '分镜总控已启动' }),
    SUPERVISOR_PAUSE_REQUESTED: () => ({ title: '已请求暂停' }),
    SUPERVISOR_PAUSED: () => ({ title: '已在安全点暂停' }),
    SUPERVISOR_HANDOFF_REQUESTED: () => ({ title: '已请求转交人工' }),
    SUPERVISOR_HANDOFF: () => ({ title: '已转交人工处理' }),
    OUTLINE_VALIDATED: (m) => ({ title: '大纲已通过', detail: m || undefined }),
    SHOT_CHECKPOINT_VALIDATED: (m) => ({
      title: m.includes('镜') ? m.replace(/^.*?第/, '第').slice(0, 24) || '镜头已通过' : '镜头已通过校验',
      detail: m || undefined,
    }),
    EPISODE_VALIDATION_FAILED: () => ({ title: '整集检查未通过，正在安排修复' }),
    EPISODE_VALIDATION_PASSED: () => ({ title: '整集检查已通过' }),
    AUTO_CONFIRM_STARTED: () => ({ title: '开始自动确认' }),
    AUTO_CONFIRM_SUCCEEDED: () => ({ title: '已自动确认（尚未产生视频费用）' }),
    AUTO_CONFIRM_REJECTED: () => ({ title: '确认未通过，继续修复' }),
    WAITING_AUTHORIZATION: () => ({ title: '授权失效，需重新授权后继续' }),
    SUFFIX_INVALIDATED: () => ({ title: '已作废后续镜头，准备重做' }),
    OUTLINE_REPLANNED: () => ({ title: '已重规划分镜大纲' }),
    RUN_FAILED: (m) => ({ title: '运行遇到问题', detail: m || undefined }),
    RUN_CANCELLED: () => ({ title: '已取消本次生成' }),
  }
  const fn = map[ev.event_type]
  if (fn) return fn(msg)
  // 未知事件：有可读中文 message 才展示，否则丢掉码表名
  if (!msg) return null
  if (/^[A-Z][A-Z0-9_]+$/.test(msg)) return null
  if (msg.length < 4) return null
  return { title: msg.length > 48 ? `${msg.slice(0, 46)}…` : msg }
}

function formatTime(ts?: number): string {
  if (!ts) return ''
  const d = new Date(ts < 1e12 ? ts * 1000 : ts)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function stepState(phase: string, stepPhases: readonly string[]): 'done' | 'active' | 'todo' {
  if (phase === 'SUCCEEDED') return 'done'
  const currentIdx = PHASE_STEPS.findIndex(s => (s.phases as readonly string[]).includes(phase))
  const stepIdx = PHASE_STEPS.findIndex(s => s.phases === stepPhases)
  if (['PAUSED_EXTERNAL', 'PAUSED_BUDGET', 'WAITING_HUMAN', 'WAITING_AUTHORIZATION', 'WAITING_RETRY', 'CANCELLED'].includes(phase)) {
    if (stepIdx < 2) return 'done'
    if (stepIdx === 2) return 'active'
    return 'todo'
  }
  if (currentIdx < 0) return stepIdx === 0 ? 'active' : 'todo'
  if (stepIdx < currentIdx) return 'done'
  if (stepIdx === currentIdx) return 'active'
  return 'todo'
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
  const [events, setEvents] = useState<RunEvent[]>([])
  const [busy, setBusy] = useState(false)
  const [logOpen, setLogOpen] = useState(false)

  useEffect(() => {
    if (!runId) {
      setEvents([])
      return
    }
    let cancelled = false
    const load = () => {
      api.get(`/runs/${runId}/events?limit=40`).then((rows: RunEvent[]) => {
        if (!cancelled) setEvents(Array.isArray(rows) ? rows.slice().reverse() : [])
      }).catch(() => { /* ignore */ })
    }
    load()
    const id = window.setInterval(load, scripting ? 4000 : 12000)
    return () => { cancelled = true; window.clearInterval(id) }
  }, [api, runId, scripting])

  const activity = useMemo(() => {
    const out: Array<{ key: string; title: string; detail?: string; time: string }> = []
    for (let i = 0; i < events.length; i++) {
      const h = humanizeEvent(events[i])
      if (!h) continue
      out.push({
        key: `${events[i].event_type}-${i}`,
        title: h.title,
        detail: h.detail && h.detail !== h.title ? h.detail : undefined,
        time: formatTime(events[i].ts),
      })
      if (out.length >= 6) break
    }
    return out
  }, [events])

  if (!supervisor && !scripting) return null

  const phase = supervisor?.phase || (scripting ? 'PLANNING_OUTLINE' : '')
  const prefix = supervisor?.validated_prefix_end ?? 0
  const total = supervisor?.expected_total || 0
  const nextNo = supervisor?.next_shot_no || (prefix > 0 ? prefix + 1 : 1)
  const auto = supervisor?.completion_mode === 'auto_confirm' || supervisor?.goal === 'generate_and_confirm'
  const pausedLike = ['PAUSED_EXTERNAL', 'PAUSED_BUDGET', 'WAITING_RETRY', 'WAITING_HUMAN', 'WAITING_AUTHORIZATION'].includes(phase)
  const succeeded = phase === 'SUCCEEDED' || supervisor?.outcome?.startsWith('SUCCEEDED')
  const progressPct = total > 0
    ? Math.min(100, Math.round((prefix / total) * 100))
    : (succeeded ? 100 : phase === 'PLANNING_OUTLINE' || phase === 'PREFLIGHT' ? 8 : phase === 'VALIDATING_OUTLINE' ? 18 : scripting ? 28 : 0)

  const runAction = async (action: 'pause' | 'handoff' | 'resume') => {
    if (!runId && action !== 'resume') return
    setBusy(true)
    try {
      if (action === 'resume') {
        await api.post(`/episodes/${episodeId}/storyboard/resume`)
      } else if (runId) {
        await api.post(`/runs/${runId}/${action}`)
      }
      onChanged?.()
    } finally {
      setBusy(false)
    }
  }

  const tone = pausedLike ? 'wait' : succeeded ? 'done' : 'run'
  const headline = succeeded
    ? (supervisor?.outcome === 'SUCCEEDED_CONFIRMED' ? '分镜已自动确认' : '分镜已通过校验')
    : (PHASE_LABEL[phase] || '分镜生成中')

  return (
    <div className={`supervisor-rail tone-${tone}`} aria-live="polite">
      <div className="supervisor-rail-main">
        <div className="supervisor-rail-copy">
          <div className="supervisor-rail-kicker">
            <span className="supervisor-pulse" aria-hidden />
            <span>{auto ? '自动完成并确认' : '生成并等待确认'}</span>
            {supervisor && supervisor.repair_epoch > 0 && (
              <span className="supervisor-chip">修复 · 第 {supervisor.repair_epoch} 轮</span>
            )}
            {supervisor?.pending_control?.pending && (
              <span className="supervisor-chip">待执行控制</span>
            )}
          </div>
          <strong className="supervisor-rail-title">{headline}</strong>
          <p className="supervisor-rail-meta">
            {prefix > 0 ? `已通过 1–${String(prefix).padStart(2, '0')} 镜` : '尚未通过镜头'}
            {total > 0 ? ` · 计划 ${total} 镜` : ''}
            {!succeeded && scripting ? ` · 下一镜 ${String(nextNo).padStart(2, '0')}` : ''}
            {supervisor?.strategy ? ` · ${STRATEGY_LABEL[supervisor.strategy] || supervisor.strategy}` : ''}
            {supervisor?.frontier ? `（自第 ${supervisor.frontier} 镜起重做）` : ''}
          </p>
          {!!supervisor?.issue_codes?.length && (
            <p className="supervisor-rail-issues">
              待处理：{(supervisor.issue_codes || []).slice(0, 3).map(c => ISSUE_LABEL[c] || c).join(' · ')}
            </p>
          )}
        </div>

        <div className="supervisor-rail-side">
          {(scripting || pausedLike) && (
            <div className="supervisor-rail-actions">
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
                  onAction={async () => { await runAction('resume') }}>继续</AsyncButton>
              )}
            </div>
          )}
        </div>
      </div>

      <div className="supervisor-progress" aria-label={`进度 ${progressPct}%`}>
        <i style={{ width: `${progressPct}%` }} />
      </div>

      <ol className="supervisor-steps" aria-label="分镜阶段">
        {PHASE_STEPS.map(step => {
          const state = stepState(phase, step.phases)
          return (
            <li key={step.id} className={state}>
              <span>{step.label}</span>
            </li>
          )
        })}
      </ol>

      {activity.length > 0 && (
        <div className="supervisor-activity">
          <button
            type="button"
            className="supervisor-activity-toggle"
            aria-expanded={logOpen}
            onClick={() => setLogOpen(v => !v)}
          >
            <span>进展</span>
            <b>{activity[0]?.title}</b>
            <em>{logOpen ? '收起' : `全部 ${activity.length}`}</em>
          </button>
          {logOpen && (
            <ul className="supervisor-activity-list">
              {activity.map(item => (
                <li key={item.key}>
                  <time>{item.time || '···'}</time>
                  <div>
                    <strong>{item.title}</strong>
                    {item.detail && <small>{item.detail}</small>}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
