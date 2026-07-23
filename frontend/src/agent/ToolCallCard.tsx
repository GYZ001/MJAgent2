export default function ToolCallCard({
  name,
  status,
  summary,
  runId,
  risk,
}: {
  name: string
  status: string
  summary?: string
  runId?: string
  risk?: string
}) {
  return (
    <section className="agent-card tool-card">
      <div className="tool-card-head">
        <strong>{name}</strong>
        <span className={`tool-status status-${status}`}>{status}</span>
      </div>
      {risk && <div className="tool-meta">风险 {risk}</div>}
      {summary && <p>{summary}</p>}
      {runId && <div className="tool-meta">Run {runId}</div>}
    </section>
  )
}
