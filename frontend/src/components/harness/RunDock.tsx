import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, RunSummary } from '../../api'
import { AdaptivePoller } from '../../adaptivePoller'
import { statusLabel } from '../../lib/statusLabels'

const ACTIVE = new Set([
  'CREATED', 'RUNNING', 'WAITING_RETRY', 'WAITING_HUMAN', 'PAUSED_BUDGET', 'PAUSED_EXTERNAL',
])

const TERMINAL = new Set(['PARTIAL', 'FAILED', 'CANCELLED', 'SUCCEEDED'])

function formatDuration(seconds: number) {
  const safe = Math.max(0, Math.floor(seconds))
  if (safe < 60) return `${safe} 秒`
  return `${Math.floor(safe / 60)} 分 ${safe % 60} 秒`
}

function runElapsed(run: RunSummary, nowTs: number) {
  if (!run.started_at) return '尚未开始'
  const end = TERMINAL.has(run.status) || run.status === 'PAUSED_EXTERNAL'
    ? (run.finished_at || run.updated_at || nowTs)
    : nowTs
  return formatDuration(end - run.started_at)
}

function isBibleRelated(run: RunSummary) {
  const type = (run.workflow_type || '').toLowerCase()
  const step = (run.current_step_key || '').toLowerCase()
  return /bible|portrait|refs|character/.test(type) || /bible|portrait|refs|character/.test(step)
}

export default function RunDock({
  projectId,
  onOpen,
  page = 'other',
}: {
  projectId: string | null
  onOpen: () => void
  page?: 'bible' | 'scenes' | 'other'
}) {
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [error, setError] = useState('')
  const [dismissedNotice, setDismissedNotice] = useState<string | null>(null)
  const [actionBusy, setActionBusy] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [nowTs, setNowTs] = useState(() => Math.floor(Date.now() / 1000))

  const refresh = useCallback(async () => {
    const query = projectId
      ? '/runs?project_id=' + encodeURIComponent(projectId) + '&limit=20'
      : '/runs?limit=20'
    const items = await api.get(query) as RunSummary[]
    setRuns(items)
    setError('')
    return items
  }, [projectId])

  useEffect(() => {
    const poller = new AdaptivePoller(
      refresh,
      (data) => {
        const list = data ?? []
        return list.some(r => ACTIVE.has(r.status)) ? 4000 : 0
      },
      {
        onData: setRuns,
        onError: (reason) => setError(reason instanceof Error ? reason.message : String(reason)),
      },
    )
    poller.start()
    return () => poller.stop()
  }, [refresh])

  useEffect(() => {
    const hasLive = runs.some(r => ACTIVE.has(r.status) && r.status !== 'PAUSED_EXTERNAL')
    if (!hasLive) return
    const timer = window.setInterval(() => setNowTs(Math.floor(Date.now() / 1000)), 1000)
    return () => window.clearInterval(timer)
  }, [runs])

  const ranked = useMemo(() => {
    const scored = runs.map(run => {
      let score = 0
      if (projectId && run.scope_id === projectId) score += 10
      if (page === 'bible' && isBibleRelated(run)) score += 20
      if (ACTIVE.has(run.status)) score += 5
      return { run, score }
    })
    return scored.sort((a, b) => b.score - a.score).map(item => item.run)
  }, [runs, projectId, page])

  const relevant = page === 'bible'
    ? ranked.filter(run => isBibleRelated(run) || ACTIVE.has(run.status))
    : ranked
  const primary = relevant[0] || ranked[0]
  const noticeKey = primary
    ? [primary.id, primary.status, primary.failure_message || ''].join(':')
    : null
  if (!primary || dismissedNotice === noticeKey) return null

  const blocked = ['FAILED', 'WAITING_HUMAN', 'PAUSED_BUDGET', 'PAUSED_EXTERNAL'].includes(primary.status)
  const frozen = TERMINAL.has(primary.status) || primary.status === 'PAUSED_EXTERNAL'

  const runAction = async (fn: () => Promise<unknown>) => {
    if (actionBusy) return
    setActionBusy(true)
    try {
      await fn()
      await refresh()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setActionBusy(false)
    }
  }

  return (
    <aside className={'run-dock run-dock-notice' + (blocked ? ' blocked' : '') + (expanded ? ' expanded' : ' collapsed')} aria-live="polite">
      <button
        className="run-dock-close"
        type="button"
        aria-label="关闭运行提醒"
        title="关闭提醒"
        onClick={() => setDismissedNotice(noticeKey)}
      >
        ×
      </button>
      <button type="button" className="run-dock-toggle" onClick={() => setExpanded(v => !v)}>
        {expanded ? '收起' : '展开'}
      </button>
      <div className="run-dock-main">
        <span className={'run-dot' + (primary.status === 'RUNNING' ? ' pulse' : '')} />
        <div>
          <b>{primary.current_step_key || primary.workflow_type}</b>
          <span>
            {statusLabel(primary.status)}
            {frozen ? ' · 最终历时 ' : ' · 已用时 '}
            {runElapsed(primary, nowTs)}
            {frozen && primary.finished_at ? ` · 结束于 ${new Date(primary.finished_at * 1000).toLocaleString()}` : ''}
            {primary.cost_cny > 0 ? ' · ¥' + primary.cost_cny.toFixed(2) : ''}
          </span>
        </div>
      </div>
      {expanded && (
        <>
          {primary.failure_message && <p>{primary.failure_message}</p>}
          {error && <p>{error}</p>}
          {relevant.length > 1 && (
            <ul className="run-dock-list">
              {relevant.slice(1, 4).map(run => (
                <li key={run.id}>
                  {run.workflow_type} · {statusLabel(run.status)} · {runElapsed(run, nowTs)}
                </li>
              ))}
            </ul>
          )}
          <div className="run-dock-actions">
            <button type="button" onClick={onOpen}>查看详情</button>
            {primary.status === 'RUNNING' && (
              <button type="button" disabled={actionBusy} onClick={() => runAction(() => api.post('/runs/' + primary.id + '/cancel'))}>
                {actionBusy ? '处理中…' : '取消'}
              </button>
            )}
            {['PAUSED_EXTERNAL', 'PAUSED_BUDGET', 'WAITING_RETRY', 'WAITING_HUMAN'].includes(primary.status) && (
              <button type="button" disabled={actionBusy} onClick={() => runAction(() => api.post('/runs/' + primary.id + '/resume'))}>
                {actionBusy ? '处理中…' : '从检查点恢复'}
              </button>
            )}
          </div>
        </>
      )}
    </aside>
  )
}
