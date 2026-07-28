import { useEffect, useMemo, useRef, useState } from 'react'
import { api, ArtifactEvidence, EvidenceIssue } from '../../api'
import IssueList from './IssueList'
import TrustBadge from './TrustBadge'
import {
  artifactTypeLabel,
  statusLabel,
  statusTitle,
} from '../../lib/statusLabels'
import { useFocusTrap } from '../../hooks/useFocusTrap'
import OperationError from '../OperationError'

const PAGE_SIZE = 20

export function matchesEvidenceRelation(item: ArtifactEvidence, query: string): boolean {
  const normalized = query.trim().toLowerCase()
  if (!normalized) return true
  return [
    artifactTypeLabel(item.type),
    statusLabel(item.status),
    item.type,
    item.status,
    item.id,
  ].some(value => value.toLowerCase().includes(normalized))
}

function RelationItem({ item, direction }: {
  item: ArtifactEvidence
  direction: 'upstream' | 'downstream'
}) {
  return (
    <div className="lineage-item">
      <span className="lineage-direction">{direction === 'upstream' ? '上游来源' : '下游关联'}</span>
      <b>{artifactTypeLabel(item.type)}</b>
      <span>{statusLabel(item.status)}</span>
      <span>第 {item.version} 版</span>
      <details>
        <summary>技术标识</summary>
        <code>{item.id}</code>
        <small>类型：{item.type} · 状态：{item.status}</small>
      </details>
    </div>
  )
}

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
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'error'>('idle')
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
      setLineageError((error as Error).message || '上下游关联加载失败')
    })
    return () => { requestRef.current += 1 }
  }, [open, evidence.id, retryVersion])

  const descendants = (lineage?.descendants ?? []).filter(item => matchesEvidenceRelation(item, descFilter))
  const pageCount = Math.max(1, Math.ceil(descendants.length / PAGE_SIZE))
  const page = Math.min(descPage, pageCount - 1)
  const paged = descendants.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE)

  const copyContentHash = async () => {
    if (!navigator.clipboard) {
      setCopyState('error')
      return
    }
    try {
      await navigator.clipboard.writeText(evidence.content_hash)
      setCopyState('copied')
    } catch {
      setCopyState('error')
    }
  }

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
            aria-label="质检依据"
            ref={node => { containerRef.current = node }}
          >
            <header>
              <div>
                <span className="eyebrow">质检依据</span>
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
                    ? `可用（${warnings.length} 项需复核）`
                    : '必检项全部通过，可用'}
              </span>
            </div>
            {conclusion && <p className="evidence-conclusion">{conclusion}</p>}
            <h4>问题与下游影响</h4>
            <IssueList issues={issues} />
            {lineageState === 'loading' && <p className="hint" role="status">正在加载上下游来源关系……</p>}
            {lineageState === 'error' && (
              <OperationError
                title="上下游关联加载失败"
                message={lineageError}
                guidance="这不代表没有关联内容。当前质检结论仍可查看，可重试加载关联范围。"
              >
                <button type="button" className="btn small ghost" onClick={() => setRetryVersion(value => value + 1)}>重试加载</button>
              </OperationError>
            )}
            {lineageState === 'success' && (
              <p className="hint">下游关联共 {lineage?.descendants.length ?? 0} 个；上游来源 {lineage?.ancestors.length ?? 0} 个。</p>
            )}

            <button type="button" className="btn small ghost" onClick={() => setTechOpen(v => !v)}>
              {techOpen ? '收起技术信息' : '展开技术信息（标识 / 校验值 / 合同）'}
            </button>
            {techOpen && (
              <>
                <dl className="evidence-meta">
                  <div><dt>产物标识</dt><dd><code>{evidence.id}</code></dd></div>
                  <div>
                    <dt>版本</dt>
                    <dd title={statusTitle(evidence.status)}>
                      v{evidence.version} · {statusLabel(evidence.status)}
                    </dd>
                  </div>
                  <div><dt>内容校验值</dt><dd><code>{evidence.content_hash.slice(0, 16)}…</code> <button type="button" className="btn small ghost"
                    aria-label={copyState === 'copied' ? '内容校验值已复制' : copyState === 'error' ? '复制内容校验值失败，请检查剪贴板权限后重试' : '复制完整内容校验值'}
                    onClick={() => void copyContentHash()}>{copyState === 'copied' ? '已复制' : copyState === 'error' ? '重试复制' : '复制'}</button></dd></div>
                  <div><dt>规则合同</dt><dd>{evidence.contract_version || '未记录'}</dd></div>
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

            <h4>上下游关联内容</h4>
            <div className="lineage-list">
              <div className="lineage-filter-row">
                <input
                  className="library-search"
                  value={descFilter}
                  onChange={e => { setDescFilter(e.target.value); setDescPage(0) }}
                  placeholder="筛选下游内容类型或状态…"
                  aria-label="筛选下游关联内容"
                />
                {descFilter && <button type="button" className="btn small ghost" onClick={() => {
                  setDescFilter('')
                  setDescPage(0)
                }}>清除筛选</button>}
                <span role="status">{lineageState === 'success'
                  ? `下游显示 ${descendants.length} / 共 ${lineage?.descendants.length ?? 0}`
                  : '关联数量尚未确认'}</span>
              </div>
              {(lineage?.ancestors ?? []).map(item => (
                <RelationItem key={`a-${item.id}`} item={item} direction="upstream" />
              ))}
              {paged.map(item => (
                <RelationItem key={`d-${item.id}`} item={item} direction="downstream" />
              ))}
              {lineageState === 'success' && !descendants.length && (
                <div className="lineage-empty" role="status">
                  <b>没有符合当前条件的下游内容</b>
                  <button type="button" className="btn small" onClick={() => {
                    setDescFilter('')
                    setDescPage(0)
                  }}>清除筛选</button>
                </div>
              )}
              {pageCount > 1 && (
                <div className="lineage-pagination">
                  <button type="button" className="btn small" disabled={page <= 0}
                    aria-label={page <= 0 ? '上一页，暂不可用：当前已是第一页' : '上一页'}
                    onClick={() => setDescPage(page - 1)}>上一页</button>
                  <span>{page + 1} / {pageCount}</span>
                  <button type="button" className="btn small" disabled={page >= pageCount - 1}
                    aria-label={page >= pageCount - 1 ? '下一页，暂不可用：当前已是最后一页' : '下一页'}
                    onClick={() => setDescPage(page + 1)}>下一页</button>
                </div>
              )}
            </div>
          </aside>
        </div>
      )}
    </>
  )
}
