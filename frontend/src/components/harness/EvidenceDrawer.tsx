import { useMemo, useState } from 'react'
import { api, ArtifactEvidence, EvidenceIssue } from '../../api'
import IssueList from './IssueList'
import TrustBadge from './TrustBadge'

export default function EvidenceDrawer({
  evidence,
  label = '查看证据',
}: {
  evidence: ArtifactEvidence
  label?: string
}) {
  const [open, setOpen] = useState(false)
  const [lineage, setLineage] = useState<{ ancestors: ArtifactEvidence[]; descendants: ArtifactEvidence[] } | null>(null)
  const issues = useMemo(
    () => evidence.evaluations.flatMap(item => item.issues || []),
    [evidence],
  )
  const blockers = issues.filter((issue: EvidenceIssue) => issue.severity === 'blocker')
  return (
    <>
      <button className="btn small evidence-trigger" type="button" onClick={() => {
        setOpen(true)
        api.get(`/artifacts/${evidence.id}/lineage`).then(setLineage).catch(() => setLineage(null))
      }}>
        <TrustBadge level={evidence.trust_level} /> {label}
      </button>
      {open && (
        <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
          if (event.currentTarget === event.target) setOpen(false)
        }}>
          <aside className="evidence-drawer" role="dialog" aria-modal="true" aria-label="产物证据">
            <header>
              <div>
                <span className="eyebrow">EVIDENCE</span>
                <h3>{evidence.type} 证据</h3>
              </div>
              <button type="button" onClick={() => setOpen(false)} aria-label="关闭">×</button>
            </header>
            <div className="evidence-summary">
              <TrustBadge level={evidence.trust_level} />
              <span className={'gate-state ' + (blockers.length ? 'blocked' : 'passed')}>
                {blockers.length ? blockers.length + ' 个 blocker' : '硬门禁通过'}
              </span>
            </div>
            <dl className="evidence-meta">
              <div><dt>Artifact</dt><dd><code>{evidence.id}</code></dd></div>
              <div><dt>版本</dt><dd>v{evidence.version} · {evidence.status}</dd></div>
              <div><dt>Hash</dt><dd><code>{evidence.content_hash.slice(0, 16)}…</code></dd></div>
              <div><dt>Contract</dt><dd>{evidence.contract_version || '未记录'}</dd></div>
            </dl>
            <h4>问题与修复目标</h4>
            <IssueList issues={issues} />
            <h4>评估记录</h4>
            <div className="evaluation-list">
              {evidence.evaluations.map(item => (
                <div key={item.id}>
                  <b>{item.evaluator_name}</b>
                  <span>{item.status} · {item.score ?? '—'} 分</span>
                  <small>{item.evaluator_type} / {item.evaluator_version}{item.recovered ? ' · 容错恢复' : ''}</small>
                </div>
              ))}
            </div>
            <h4>Artifact 血缘</h4>
            <div className="lineage-list">
              <span>上游 {lineage?.ancestors.length ?? evidence.parent_artifact_ids?.length ?? 0} 个</span>
              <span>下游 {lineage?.descendants.length ?? 0} 个</span>
              {lineage?.ancestors.slice(0, 8).map(item => (
                <code key={item.id}>↑ {item.type} v{item.version} · {item.id}</code>
              ))}
              {lineage?.descendants.slice(0, 8).map(item => (
                <code key={item.id}>↓ {item.type} v{item.version} · {item.status}</code>
              ))}
            </div>
          </aside>
        </div>
      )}
    </>
  )
}
