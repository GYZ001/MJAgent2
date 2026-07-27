import { useEffect, useMemo, useRef, useState } from 'react'
import { api, ArtifactEvidence, EvidenceIssue } from '../../api'
import IssueList from './IssueList'
import TrustBadge from './TrustBadge'
import { statusLabel, statusTitle } from '../../lib/statusLabels'
import { useFocusTrap } from '../../hooks/useFocusTrap'

const PAGE_SIZE = 20

export default function EvidenceDrawer({
  evidence,
  label = '查看证据',
  conclusion,
  onOpenChange,
}: {
  evidence: ArtifactEvidence
  label?: string
  conclusion?: string
  onOpenChange?: (open: boolean) => void
}) {
  const [open, setOpen] = useState(false)
  const [lineage, setLineage] = useState<{ ancestors: ArtifactEvidence[]; descendants: ArtifactEvidence[] } | null>(null)
  const [lineageState, setLineageState] = useState<'idle' | 'loading' | 'success' | 'error'>('idle')
  const [lineageError, setLineageError] = useState('')
  const [retryVersion, setRetryVersion] = useState(0)
  const requestRef = useRef(0)
  const [techOpen, setTechOpen] = useState(false)
  const [descFilter, setDescFilter] = useState('')
  const [descPage, setDescPage] = useState(0)
  const setDrawerOpen = (value: boolean) => { setOpen(value); onOpenChange?.(value) }
  const containerRef = useFocusTrap(open, () => setDrawerOpen(false))

  const issues = useMemo(
    () => evidence.evaluations.flatMap(item => item.issues || []),
    [evidence],
  )
  const blockers = issues.filter((issue: EvidenceIssue) => issue.severity === 'blocker')
  const warnings = issues.filter((issue: EvidenceIssue) => issue.severity === 'warning')

  useEffect(() => {
    if (!open) return
    const requestId = ++requestRef.current
    setLineageState('loading')
    setLineageError('')
    api.get(`/artifacts/${evidence.id}/lineage`).then(value => {
      if (requestId !== requestRef.current) return
      setLineage(value)
      setLineageState('success')
    }).catch((error: unknown) => {
      if (requestId !== requestRef.current) return
      setLineageState('error')
      setLineageError((error as Error).message || '血缘请求失败')
    })
    return () => { requestRef.current += 1 }
  }, [open, evidence.id, retryVersion])

  const descendants = (lineage?.descendants ?? []).filter(item => {
    if (!descFilter.trim()) return true
    const q = descFilter.trim().toLowerCase()
    return item.type.toLowerCase().includes(q) || item.id.toLowerCase().includes(q) || item.status.toLowerCase().includes(q)
  })
  const pageCount = Math.max(1, Math.ceil(descendants.length / PAGE_SIZE))
  const page = Math.min(descPage, pageCount - 1)
  const paged = descendants.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE)

  return (
    <>
      <button className="btn small evidence-trigger" type="button" onClick={() => setDrawerOpen(true)}>
        <TrustBadge level={evidence.trust_level} /> {label}
      </button>
      {open && (
        <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
          if (event.currentTarget === event.target) setDrawerOpen(false)
        }}>
          <aside
            className="evidence-drawer"
            role="dialog"
            aria-modal="true"
            aria-label="产物证据"
            ref={node => { containerRef.current = node }}
          >
            <header>
              <div>
                <span className="eyebrow">EVIDENCE</span>
                <h3>可用性结论</h3>
              </div>
              <button type="button" onClick={() => setDrawerOpen(false)} aria-label="关闭">×</button>
            </header>
            <div className="evidence-summary">
              <TrustBadge level={evidence.trust_level} />
              <span className={'gate-state ' + (blockers.length ? 'blocked' : 'passed')}>
                {blockers.length
                  ? `${blockers.length} 个阻塞问题`
                  : warnings.length
                    ? `可用（${warnings.length} 个警告）`
                    : '硬门禁通过，可用'}
              </span>
            </div>
            {conclusion && <p className="evidence-conclusion">{conclusion}</p>}
            <h4>问题与下游影响</h4>
            <IssueList issues={issues} />
            {lineageState === 'loading' && <p className="hint" role="status">正在加载血缘……</p>}
            {lineageState === 'error' && (
              <div className="error-banner" role="alert">
                血缘加载失败，不等于 0 个下游：{lineageError}
                <button type="button" className="btn small ghost" onClick={() => setRetryVersion(value => value + 1)}>重试</button>
              </div>
            )}
            {lineageState === 'success' && (
              <p className="hint">下游血缘共 {lineage?.descendants.length ?? 0} 个；上游 {lineage?.ancestors.length ?? 0} 个。</p>
            )}

            <button type="button" className="btn small ghost" onClick={() => setTechOpen(v => !v)}>
              {techOpen ? '收起技术信息' : '展开技术信息（Artifact / Hash / 合同）'}
            </button>
            {techOpen && (
              <>
                <dl className="evidence-meta">
                  <div><dt>Artifact</dt><dd><code>{evidence.id}</code></dd></div>
                  <div>
                    <dt>版本</dt>
                    <dd title={statusTitle(evidence.status)}>
                      v{evidence.version} · {statusLabel(evidence.status)}
                    </dd>
                  </div>
                  <div><dt>Hash</dt><dd><code>{evidence.content_hash.slice(0, 16)}…</code> <button type="button" className="btn small ghost" onClick={() => void navigator.clipboard.writeText(evidence.content_hash)}>复制</button></dd></div>
                  <div><dt>Contract</dt><dd>{evidence.contract_version || '未记录'}</dd></div>
                </dl>
                <h4>评估记录</h4>
                <div className="evaluation-list">
                  {evidence.evaluations.map(item => (
                    <div key={item.id}>
                      <b>{item.evaluator_name}</b>
                      <span title={statusTitle(item.status)}>
                        {statusLabel(item.status)} · {item.score ?? '—'} 分
                      </span>
                      <small>{item.evaluator_type} / {item.evaluator_version}{item.recovered ? ' · 容错恢复' : ''}</small>
                    </div>
                  ))}
                </div>
              </>
            )}

            <h4>完整血缘</h4>
            <div className="lineage-list">
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
                <input
                  className="library-search"
                  value={descFilter}
                  onChange={e => { setDescFilter(e.target.value); setDescPage(0) }}
                  placeholder="筛选下游类型/状态…"
                  aria-label="筛选下游血缘"
                />
                <span>{lineageState === 'success' ? `下游命中 ${descendants.length} / 总计 ${lineage?.descendants.length ?? 0}` : '血缘数量尚未确认'}</span>
              </div>
              {(lineage?.ancestors ?? []).map(item => (
                <code key={`a-${item.id}`}>↑ {item.type} v{item.version} · {statusLabel(item.status)}</code>
              ))}
              {paged.map(item => (
                <code key={`d-${item.id}`}>↓ {item.type} v{item.version} · {statusLabel(item.status)} · {item.id}</code>
              ))}
              {pageCount > 1 && (
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
                  <button type="button" className="btn small" disabled={page <= 0} onClick={() => setDescPage(page - 1)}>上一页</button>
                  <span>{page + 1} / {pageCount}</span>
                  <button type="button" className="btn small" disabled={page >= pageCount - 1} onClick={() => setDescPage(page + 1)}>下一页</button>
                </div>
              )}
            </div>
          </aside>
        </div>
      )}
    </>
  )
}
