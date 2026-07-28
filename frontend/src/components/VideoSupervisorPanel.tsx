/** 视频补齐 Supervisor 运行面板：覆盖率 / 预算 / 修复层级。 */
import { useCallback, useEffect, useState } from 'react'
import AsyncButton from './AsyncButton'
import { useFocusTrap } from '../hooks/useFocusTrap'

export type VideoSupervisorSnapshot = {
  phase?: string
  goal?: string
  repair_epoch?: number
  tick_no?: number
  started_at?: number
  deadline_at?: number | null
  last_heartbeat_at?: number | null
  finished_at?: number | null
  terminal_reason?: string | null
  quality_target_missed?: boolean
  missing_shots?: number[]
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
    adopted?: number
    unadopted?: number
  }
  shot_state?: Record<string, {
    grade?: string
    last_issue_codes?: string[]
    repair_level?: string
    attempts_paid?: number
    attempts_budgeted?: number
    fallback_reason?: string
    continuity_degraded?: boolean
    adopted_version_id?: string | null
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
  task_running?: boolean
  run_status?: string | null
  heartbeat_stale?: boolean
  active_media_jobs?: number
  preserve_adopted?: boolean
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
      adopted_version_id?: string | null
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
  DEADLINE_CLOSING: '截止收口中',
  SUCCEEDED_COVERED: '全片已覆盖',
  COMPLETED_DEADLINE_FALLBACK: '截止已收口',
  PARTIAL_NO_USABLE_CANDIDATE: '部分收口（存在缺片）',
  RECOVERING_CONTROL_PLANE: '控制面恢复中',
  FAILED_CLOSED: '已安全停止',
  PAUSED_EXTERNAL: '已暂停',
  PAUSED_BUDGET: '预算暂停',
  WAITING_AUTHORIZATION: '等待追加授权',
  WAITING_HUMAN: '已转交人工',
  CANCELLED: '已取消',
}

const TERMINAL = new Set([
  'SUCCEEDED_COVERED', 'COMPLETED_DEADLINE_FALLBACK',
  'PARTIAL_NO_USABLE_CANDIDATE', 'FAILED_CLOSED', 'CANCELLED',
])
const ACTION_OK: Record<string, string> = {
  pause: '已请求暂停（下一轮生效）',
  handoff: '已转交人工',
  cancel: '已取消全片补齐',
  resume: '已继续补齐',
  topup: '已追加预算并继续',
}

export default function VideoSupervisorPanel({
  api,
  episodeId,
  runId,
  supervisor,
  running,
  onChanged,
  onToast,
  onDismiss,
}: {
  api: typeof import('../api').api
  episodeId: string
  runId?: string | null
  supervisor: VideoSupervisorSnapshot | null | undefined
  running?: boolean
  onChanged?: () => void | Promise<void>
  onToast?: (msg: string) => void
  onDismiss?: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [live, setLive] = useState<VideoSupervisorSnapshot | null>(null)
  const [pollFailures, setPollFailures] = useState(0)
  const [lastSuccessAt, setLastSuccessAt] = useState<number | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [actionDialog, setActionDialog] = useState<'cancel' | 'topup' | 'resume' | null>(null)
  const [topupBudget, setTopupBudget] = useState('50')
  const [topupHours, setTopupHours] = useState('1')
  const [legacyPreview, setLegacyPreview] = useState<{
    would_adopt?: unknown[]; would_retain?: unknown[]; would_mark_missing?: Array<{ shot_no?: number }>
  } | null>(null)
  const actionTrapRef = useFocusTrap(Boolean(actionDialog), () => {
    if (!busy) setActionDialog(null)
  })
  const legacyTrapRef = useFocusTrap(Boolean(legacyPreview), () => {
    if (!busy) setLegacyPreview(null)
  })

  const load = useCallback(async (manual = false) => {
    if (manual) setRefreshing(true)
    try {
      const data = await api.get(`/episodes/${episodeId}/video-completion`) as VideoSupervisorSnapshot
      setLive(data)
      setPollFailures(0)
      setLastSuccessAt(Date.now())
      return data
    } catch (error) {
      setPollFailures(value => value + 1)
      if (manual) onToast?.(error instanceof Error ? error.message : String(error))
      throw error
    } finally {
      if (manual) setRefreshing(false)
    }
  }, [api, episodeId, onToast])

  useEffect(() => {
    const poll = () => {
      load().catch(() => {
        // 保留旧快照，但失败计数和过期标记会告知用户。
      })
    }
    poll()
    const id = window.setInterval(poll, running ? 3000 : 10000)
    return () => window.clearInterval(id)
  }, [load, running])

  const snap = live || supervisor
  if (!snap && !running) return null

  const phase = snap?.phase || (running ? 'PREFLIGHT' : '')
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
  const waitingAuth = phase === 'WAITING_AUTHORIZATION'
  const succeeded = phase === 'SUCCEEDED_COVERED'
  const deadlineCompleted = phase === 'COMPLETED_DEADLINE_FALLBACK'
  const partialClosed = phase === 'PARTIAL_NO_USABLE_CANDIDATE' || phase === 'FAILED_CLOSED'
  const cancelledPhase = phase === 'CANCELLED'
  const terminalPhase = TERMINAL.has(phase)
  const pausedLike = ['PAUSED_EXTERNAL', 'PAUSED_BUDGET', 'WAITING_HUMAN', 'WAITING_AUTHORIZATION'].includes(phase)
  const resolvedRunId = runId || snap?.active_video_run_id || null
  const activelyRunning = Boolean(
    running
    || snap?.task_running
    || snap?.running
  )
  const historical = terminalPhase && !activelyRunning
  const runFailed = snap?.run_status === 'FAILED'
  const stale = pollFailures >= 2 || snap?.heartbeat_stale === true
  const snapshotTime = lastSuccessAt || (snap?.last_heartbeat_at ? snap.last_heartbeat_at * 1000 : null)

  const deadlineTerminal = deadlineCompleted || phase === 'PARTIAL_NO_USABLE_CANDIDATE'
  const adoptionCoverage = snap?.preserve_adopted === true || deadlineTerminal
  const missingCount = snap?.missing_shots?.length || 0
  const adoptedAtCloseout = Number(
    cov.adopted ?? (deadlineTerminal ? Math.max(0, total - missingCount) : a + b),
  )
  const unadopted = Number(cov.unadopted ?? Math.max(0, total - adoptedAtCloseout))
  const displayA = adoptionCoverage ? 0 : a
  const displayB = adoptionCoverage ? adoptedAtCloseout : b
  const displayC = adoptionCoverage ? unadopted : c
  const displayPct = total > 0 ? Math.min(100, Math.round((adoptedAtCloseout / total) * 100)) : 0
  const displayAPct = total > 0 ? (displayA / total) * 100 : 0
  const displayBPct = total > 0 ? (displayB / total) * 100 : 0
  const uncovered = (snap?.ledger?.entries || [])
    .filter(e => !e.adopted_version_id && e.grade === 'C')
    .slice(0, 6)

  const runAction = async (action: 'pause' | 'handoff' | 'resume' | 'topup' | 'cancel') => {
    if (stale) {
      onToast?.('当前补齐进度已过期，请先手动刷新')
      return
    }
    setBusy(true)
    try {
      if (action === 'resume' || action === 'topup') {
        const body: Record<string, unknown> = {
          mode: 'resume',
          completion_grant_id: snap?.grant_id,
        }
        if (action === 'topup') {
          const budgetValue = Number(topupBudget)
          const wallValue = Number(topupHours) * 3600
          if (!Number.isFinite(budgetValue) || budgetValue < 1 || budgetValue > 100000) {
            throw new Error('追加预算必须在 1–100000 元之间')
          }
          if (!Number.isFinite(wallValue) || wallValue < 60 || wallValue > 604800) {
            throw new Error('追加时长必须在 1 分钟–168 小时之间')
          }
          body.add_budget_cny = budgetValue
          body.add_wall_clock_s = wallValue
        }
        await api.post(`/episodes/${episodeId}/video-completion`, body)
      } else if (action === 'cancel') {
        if (resolvedRunId) {
          try {
            await api.post(`/runs/${resolvedRunId}/cancel`)
          } catch {
            await api.resetVideoCompletion(episodeId)
          }
        } else {
          await api.resetVideoCompletion(episodeId)
        }
      } else if (resolvedRunId) {
        await api.post(`/runs/${resolvedRunId}/${action}`)
      } else {
        throw new Error('没有可控制的补齐运行。请点「取消」复位，或清空本集后重试')
      }
      onToast?.(ACTION_OK[action] || '操作已提交')
      setActionDialog(null)
      onChanged?.()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      onToast?.(msg)
      throw e
    } finally {
      setBusy(false)
    }
  }

  const repairLegacyRun = async () => {
    setBusy(true)
    try {
      const preview = await api.get(`/episodes/${episodeId}/video-completion/repair-preview`) as {
        would_adopt?: unknown[]
        would_retain?: unknown[]
        would_mark_missing?: Array<{ shot_no?: number }>
      }
      setLegacyPreview(preview)
      return
    } catch (e: unknown) {
      onToast?.(e instanceof Error ? e.message : String(e))
      throw e
    } finally {
      setBusy(false)
    }
  }

  const confirmLegacyRepair = async () => {
    setBusy(true)
    try {
      const response = await api.post(`/episodes/${episodeId}/video-completion/repair`, { confirm: true }) as {
        result?: VideoSupervisorSnapshot
      }
      if (response.result) setLive(response.result)
      await onChanged?.()
      const adopted = Math.max(
        0,
        (response.result?.coverage?.total || 0) - (response.result?.missing_shots?.length || 0),
      )
      onToast?.(`收口完成：已采用 ${adopted} 镜，缺片 ${(response.result?.missing_shots || []).join('、') || '无'}`)
      setLegacyPreview(null)
    } catch (e: unknown) {
      onToast?.(e instanceof Error ? e.message : String(e))
      throw e
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className={`video-supervisor-panel${activelyRunning ? ' is-live' : ''}`}
      data-phase={phase}
      aria-live="polite"
    >
      <div className="vsp-head">
        {activelyRunning && <span className="vsp-pulse" aria-hidden />}
        <strong>{historical ? '上一次自动补齐' : '补齐到全片可用'}</strong>
        <span className="vsp-phase">
          {runFailed && !activelyRunning ? `自动补齐已失败（最后阶段：${PHASE_LABEL[phase] || phase}）` : PHASE_LABEL[phase] || phase || '启动中…'}
        </span>
        {typeof snap?.repair_epoch === 'number' && snap.repair_epoch > 0 && (
          <span className="vsp-epoch">修复周期 {snap.repair_epoch}</span>
        )}
        {activelyRunning && <span className="vsp-live-tag">运行中</span>}
        {stale && <span className="stamp red">数据已过期</span>}
        <span className="vsp-sync-time">
          {snapshotTime ? `最后同步 ${new Date(snapshotTime).toLocaleTimeString()}` : '尚未同步'}
          {pollFailures === 1 ? ' · 本次刷新失败' : ''}
        </span>
        <button type="button" className="btn ghost small" disabled={refreshing} onClick={() => { void load(true) }}>
          {refreshing ? '刷新中…' : '刷新'}
        </button>
      </div>

      <div className="vsp-coverage-bar" title={adoptionCoverage ? `已采用 ${displayB} · 未采用 ${displayC}` : `达到质量目标 ${a} · 可用待复核 ${b} · 未覆盖 ${c}`}>
        <div className="vsp-seg a" style={{ width: `${displayAPct}%` }} />
        <div className="vsp-seg b" style={{ width: `${displayBPct}%` }} />
        <div className="vsp-seg c" style={{ width: `${Math.max(0, 100 - displayAPct - displayBPct)}%` }} />
      </div>
      <div className="vsp-cov-label">
        {adoptionCoverage
          ? `${deadlineTerminal ? '交差覆盖' : '采用覆盖'} ${displayPct}% · 已采用 ${displayB} / ${deadlineTerminal ? '缺片' : '待补齐'} ${displayC}`
          : `覆盖 ${pct}% · 达到质量目标 ${a} / 可用待复核 ${b} / 未覆盖 ${c}${cov.fallback_quota != null ? `（待复核采用上限 ${cov.fallback_quota}）` : ''}`}
        {!snap?.phase && activelyRunning ? ' · 正在预检与建账…' : ''}
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

      {uncovered.length > 0 && !cancelledPhase && !terminalPhase && (
        <ul className="vsp-issues">
          {uncovered.map(e => (
            <li key={e.shot_no}>
              第 {e.shot_no} 镜
              {(e.last_issue_codes || []).slice(0, 2).join(', ') || '待生成'}
              {e.repair_level ? ` → ${e.repair_level}` : ''}
              {` · ${e.attempts_paid || 0}/${e.attempts_budgeted || 0} 次`}
              {e.continuity_degraded ? ' · 衔接需复核' : ''}
            </li>
          ))}
        </ul>
      )}

      {succeeded && (
        <div className="vsp-done">
          ✓ 全部 {total} 镜均有可用视频 · 达到质量目标 {a} · 可用待复核 {b}
          （待复核采用上限 {(cov.fallback_quota ?? 0)}）
          <div>✓ 覆盖报告已生成</div>
          <div>尚未拼接成片，尚未创建交付包</div>
        </div>
      )}

      {deadlineCompleted && (
        <div className="vsp-done">
          ✓ 已按截止协议停止生成并采用每镜最佳技术可播候选
          {snap?.quality_target_missed && <div>部分候选未达到原定质量目标，风险已写入覆盖报告。</div>}
        </div>
      )}

      {partialClosed && (
        <div className="vsp-plan">
          已停止继续生成。无技术可播候选的镜头：{(snap?.missing_shots || []).join('、') || '请查看终态报告'}。
        </div>
      )}

      {runFailed && !terminalPhase && !activelyRunning && (
        <div className="vsp-plan">
          自动补齐已失败并停止计时；仍有 {snap?.active_media_jobs || 0} 个媒体任务。请先查看收口预演，再确认执行遗留收口。
        </div>
      )}

      {cancelledPhase && (
        <div className="vsp-plan">这一次自动补齐已取消。这是历史结果，不代表之后单独创建的视频任务也已取消。</div>
      )}

      <div className="vsp-actions">
        {runFailed && !activelyRunning && !terminalPhase && (
          <AsyncButton className="btn primary small" busyLabel="预演中…" onAction={repairLegacyRun} disabled={busy || stale}>
            预演并确认收口
          </AsyncButton>
        )}
        {activelyRunning && !pausedLike && !terminalPhase && (
          <>
            <AsyncButton className="btn ghost small" busyLabel="…" onAction={() => runAction('pause')} disabled={busy || stale}>暂停</AsyncButton>
            <AsyncButton className="btn ghost small" busyLabel="…" onAction={() => runAction('handoff')} disabled={busy || stale}>转人工</AsyncButton>
            <button type="button" className="btn ghost small danger" onClick={() => setActionDialog('cancel')} disabled={busy || stale}>取消</button>
          </>
        )}
        {(waitingAuth || phase === 'PAUSED_EXTERNAL' || phase === 'WAITING_HUMAN') && (
          <>
            {waitingAuth ? (
              <button type="button" className="btn primary small" onClick={() => setActionDialog('topup')} disabled={busy || stale}>追加预算并继续</button>
            ) : (
              <button type="button" className="btn primary small" onClick={() => setActionDialog('resume')} disabled={busy || stale}>
                继续补齐
              </button>
            )}
            <button type="button" className="btn ghost small danger" onClick={() => setActionDialog('cancel')} disabled={busy || stale}>取消</button>
          </>
        )}
        {cancelledPhase && (
          <button type="button" className="btn ghost small" onClick={onDismiss}>关闭面板</button>
        )}
      </div>

      {actionDialog && (
        <div className="review-dialog-backdrop" role="presentation" onMouseDown={() => !busy && setActionDialog(null)}>
          <div ref={node => { actionTrapRef.current = node }} className="review-dialog" role="dialog" aria-modal="true" aria-labelledby="supervisor-action-title" onMouseDown={event => event.stopPropagation()}>
            <h3 id="supervisor-action-title">
              {actionDialog === 'cancel'
                ? '取消自动补齐'
                : actionDialog === 'topup'
                  ? '追加授权'
                  : '继续自动补齐'}
            </h3>
            {actionDialog === 'cancel' ? (
              <p>对象：{resolvedRunId || episodeId}。当前阶段「{PHASE_LABEL[phase] || phase}」，已花 ¥{spent.toFixed(1)}。本地会请求停止，已被供应商接单的任务可能继续执行和计费，未完成镜头保持未采用。</p>
            ) : actionDialog === 'topup' ? (
              <div className="review-form-grid">
                <label>追加预算（元）<input type="number" min="1" max="100000" value={topupBudget} onChange={event => setTopupBudget(event.target.value)} /></label>
                <label>追加时长（小时）<input type="number" min="0.0167" max="168" step="0.5" value={topupHours} onChange={event => setTopupHours(event.target.value)} /></label>
                <p className="full">旧上限 ¥{cap.toFixed(0)} → 新上限 ¥{(cap + (Number(topupBudget) || 0)).toFixed(0)}。将继续未覆盖的 {unadopted} 镜。</p>
              </div>
            ) : (
              <p>
                将从安全检查点继续处理尚未采用的 {unadopted} 镜，并可能产生新的模型费用。
                当前已花 ¥{spent.toFixed(1)}，预算上限 ¥{cap.toFixed(0)}；已有采用版本不会被覆盖。
              </p>
            )}
            <div className="dialog-actions">
              <button type="button" className="btn ghost" disabled={busy} onClick={() => setActionDialog(null)}>返回</button>
              <AsyncButton className={`btn ${actionDialog === 'cancel' ? 'danger' : 'primary'}`} busyLabel="提交中…" disabled={busy}
                onAction={() => runAction(actionDialog)}>
                {actionDialog === 'cancel'
                  ? '确认取消'
                  : actionDialog === 'topup'
                    ? '预演并提交授权'
                    : '确认继续补齐'}
              </AsyncButton>
            </div>
          </div>
        </div>
      )}

      {legacyPreview && (
        <div className="review-dialog-backdrop" role="presentation" onMouseDown={() => !busy && setLegacyPreview(null)}>
          <div ref={node => { legacyTrapRef.current = node }} className="review-dialog" role="dialog" aria-modal="true" aria-labelledby="legacy-repair-title" onMouseDown={event => event.stopPropagation()}>
            <h3 id="legacy-repair-title">遗留任务收口预演</h3>
            <p>将采用 {legacyPreview.would_adopt?.length || 0} 镜，保留 {legacyPreview.would_retain?.length || 0} 镜，缺片 {(legacyPreview.would_mark_missing || []).map(item => item.shot_no).filter(Boolean).join('、') || '无'}。不会启动新生成，也不会删除视频。</p>
            <div className="dialog-actions">
              <button type="button" className="btn ghost" disabled={busy} onClick={() => setLegacyPreview(null)}>返回</button>
              <AsyncButton className="btn primary" busyLabel="收口中…" disabled={busy} onAction={confirmLegacyRepair}>确认收口</AsyncButton>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
