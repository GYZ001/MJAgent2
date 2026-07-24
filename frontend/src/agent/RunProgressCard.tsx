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
        <strong>Run {runId.slice(0, 12)}</strong>
        {status && <span className={`run-status status-${status}`}>{status}</span>}
      </div>
      {summary && <p>{summary}</p>}
      {onOpen && (
        <button type="button" className="btn" onClick={onOpen}>在监制房查看</button>
      )}
    </section>
  )
}
