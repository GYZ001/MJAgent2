const STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  waiting_human: '待处理',
  paused: '已暂停',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

export default function RunProgressCard({
  runId,
  status,
  summary,
  onOpen,
}: {
  runId: string
  status?: string
  summary?: string
  onOpen?: () => void
}) {
  return (
    <section className="agent-card run-card">
      <div className="run-card-head">
        <strong>运行任务 {runId.slice(0, 12)}</strong>
        {status && <span className={`run-status status-${status}`}>{STATUS_LABELS[status] || '状态更新中'}</span>}
      </div>
      {summary && <p>{summary}</p>}
      {onOpen && (
        <button type="button" className="btn" onClick={onOpen}>在监制房查看</button>
      )}
    </section>
  )
}
