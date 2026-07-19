import { useCallback, useEffect, useState } from 'react'
import { api, RunSummary } from '../../api'

const STATUS_LABELS: Record<string, string> = {
  CREATED: '待启动',
  RUNNING: '运行中',
  WAITING_RETRY: '等待重试',
  WAITING_HUMAN: '等待人工确认',
  PAUSED_BUDGET: '预算暂停',
  PAUSED_EXTERNAL: '外部中断',
  PARTIAL: '部分完成',
  FAILED: '失败',
  CANCELLED: '已取消',
  SUCCEEDED: '已完成',
}

function elapsed(startedAt?: number | null) {
  if (!startedAt) return '尚未开始'
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - startedAt))
  if (seconds < 60) return String(seconds) + ' 秒'
  return String(Math.floor(seconds / 60)) + ' 分 ' + String(seconds % 60) + ' 秒'
}

export default function RunDock({
  projectId,
  onOpen,
}: {
  projectId: string | null
  onOpen: () => void
}) {
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [error, setError] = useState('')
  const [dismissedNotice, setDismissedNotice] = useState<string | null>(null)

  const refresh = useCallback(() => {
    const query = projectId
      ? '/runs?active=true&project_id=' + encodeURIComponent(projectId) + '&limit=10'
      : '/runs?active=true&limit=10'
    api.get(query)
      .then((items: RunSummary[]) => { setRuns(items); setError('') })
      .catch((reason: Error) => setError(reason.message))
  }, [projectId])

  useEffect(() => {
    refresh()
    const timer = window.setInterval(refresh, 2500)
    return () => window.clearInterval(timer)
  }, [refresh])

  const run = runs[0]
  const noticeKey = run
    ? [run.id, run.status, run.failure_message || ''].join(':')
    : null
  if (!run || dismissedNotice === noticeKey) {
    return (
      <button className="run-dock run-dock-idle" type="button" onClick={onOpen} title={error || '打开运行中心'}>
        <span className="run-dot" />运行中心
      </button>
    )
  }
  const blocked = ['FAILED', 'WAITING_HUMAN', 'PAUSED_BUDGET', 'PAUSED_EXTERNAL'].includes(run.status)
  return (
    <aside className={'run-dock run-dock-notice' + (blocked ? ' blocked' : '')} aria-live="polite">
      <button
        className="run-dock-close"
        type="button"
        aria-label="关闭运行提醒"
        title="关闭提醒"
        onClick={() => setDismissedNotice(noticeKey)}
      >
        ×
      </button>
      <div className="run-dock-main">
        <span className={'run-dot' + (run.status === 'RUNNING' ? ' pulse' : '')} />
        <div>
          <b>{run.current_step_key || run.workflow_type}</b>
          <span>
            {STATUS_LABELS[run.status] || run.status} · 已用时 {elapsed(run.started_at)}
            {run.cost_cny > 0 ? ' · ¥' + run.cost_cny.toFixed(2) : ''}
          </span>
        </div>
      </div>
      {run.failure_message && <p>{run.failure_message}</p>}
      <div className="run-dock-actions">
        <button type="button" onClick={onOpen}>查看详情</button>
        {run.status === 'RUNNING' && (
          <button
            type="button"
            onClick={async () => {
              await api.post('/runs/' + run.id + '/cancel')
              refresh()
            }}
          >
            取消
          </button>
        )}
        {['PAUSED_EXTERNAL', 'PAUSED_BUDGET', 'WAITING_RETRY', 'WAITING_HUMAN'].includes(run.status) && (
          <button
            type="button"
            onClick={async () => {
              await api.post('/runs/' + run.id + '/resume')
              refresh()
            }}
          >
            从检查点恢复
          </button>
        )}
      </div>
    </aside>
  )
}
