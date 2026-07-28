import { AutoChangeItem, api } from '../api'
import { statusLabel } from '../lib/statusLabels'
import { useEffect, useState } from 'react'
import DecisionDialog from './DecisionDialog'
import OperationError from './OperationError'

type AutoChangeDecision = 'approve' | 'reject' | 'rollback' | 'merge'

const DECISION_LABELS: Record<AutoChangeDecision, string> = {
  approve: '采用建议',
  reject: '忽略建议',
  rollback: '恢复原状态',
  merge: '并入已有项',
}

export function autoChangeDecisionCopy(
  item: Pick<AutoChangeItem, 'scene' | 'character' | 'kind'>,
  decision: AutoChangeDecision,
): { title: string; message: string; details: string[]; danger: boolean } {
  const objectType = item.scene ? '场景' : '角色'
  const objectName = item.scene || item.character || '未命名项'
  if (decision === 'approve') {
    return {
      title: `采用“${objectName}”更新建议？`,
      message: item.kind === 'scene_discovery'
        ? `会把新${objectType}加入当前${objectType}库并更新版本；不会自动生成图片或产生费用。`
        : `会记录该${objectType}变化为已采用，后续制作将按新状态处理。`,
      details: ['已有图片和历史记录不会删除', '需要出图时仍会另行展示范围与费用'],
      danger: false,
    }
  }
  if (decision === 'merge') {
    return {
      title: `把“${objectName}”并入已有${objectType}？`,
      message: `会将这条重复建议归入填写的已有${objectType}；原始发现记录会保留。`,
      details: ['不会自动生成图片或产生费用', '后续制作只使用合并后的已有项'],
      danger: false,
    }
  }
  if (decision === 'rollback') {
    return {
      title: `恢复“${objectName}”的原状态？`,
      message: `会撤销对这条${objectType}变化建议的采用状态；已有历史记录不会删除。`,
      details: ['当前已生成图片不会被自动删除', '后续引用按恢复后的状态处理'],
      danger: true,
    }
  }
  return {
    title: `忽略“${objectName}”更新建议？`,
    message: `会把这条${objectType}建议标记为不采用；未采用的候选不会进入后续制作。`,
    details: ['当前已采用版本和历史记录保持不变', '忽略操作不会产生费用'],
    danger: true,
  }
}

export function normalizeEvidenceFragments(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value
      .filter((fragment): fragment is string => typeof fragment === 'string')
      .map(fragment => fragment.trim())
      .filter(Boolean)
  }
  if (typeof value === 'string') {
    const fragment = value.trim()
    return fragment ? [fragment] : []
  }
  return []
}

export function filterAutoChangeItems(
  items: AutoChangeItem[],
  scope: 'all' | 'scene',
): AutoChangeItem[] {
  if (scope === 'all') return items
  return items.filter(item => (
    !!item.scene || item.kind === 'scene_discovery' || item.kind === 'scene_state_change'
  ))
}

export default function AutoChangeQueue({
  projectId,
  onChanged,
  scope = 'all',
}: {
  projectId: string
  onChanged?: () => void
  scope?: 'all' | 'scene'
}) {
  const [items, setItems] = useState<AutoChangeItem[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const [mergeTargets, setMergeTargets] = useState<Record<string, string>>({})
  const [episodeStarts, setEpisodeStarts] = useState<Record<string, string>>({})
  const [pendingDecision, setPendingDecision] = useState<{
    item: AutoChangeItem
    decision: AutoChangeDecision
  } | null>(null)

  const refresh = async () => {
    try {
      const res = await api.listAutoChanges(projectId)
      const next = Array.isArray(res.items) ? res.items : []
      setItems(filterAutoChangeItems(next, scope))
      setError('')
    } catch (e: unknown) {
      setError((e as Error).message)
    }
  }

  useEffect(() => { void refresh() }, [projectId, scope])

  const submitDecision = async () => {
    if (!pendingDecision) return
    const { item, decision } = pendingDecision
    const mergeTarget = (mergeTargets[item.id] || '').trim()
    setPendingDecision(null)
    setBusy(item.id)
    try {
      const epStart = Number(episodeStarts[item.id] ?? item.ep_start)
      await api.decideAutoChange(projectId, item.id, decision, {
        reason: (reasons[item.id] || '').trim() || undefined,
        merge_into_character: decision === 'merge' && !item.scene ? mergeTarget : undefined,
        merge_into_scene: decision === 'merge' && !!item.scene ? mergeTarget : undefined,
        ep_start: Number.isFinite(epStart) && epStart > 0 ? epStart : undefined,
      })
      await refresh()
      onChanged?.()
    } catch (caught: unknown) {
      setError((caught as Error).message)
    } finally {
      setBusy(null)
    }
  }

  if (!items.length && !error) {
    return (
      <section className="card">
        <h3>{scope === 'scene' ? '场景更新建议' : '人物与场景更新建议'}</h3>
        <div className="empty">暂无系统发现的更新建议</div>
      </section>
    )
  }

  return (
    <section className="card">
      <h3>{scope === 'scene' ? '场景更新建议' : '人物与场景更新建议'}</h3>
      {error && <OperationError
        title="更新建议操作未完成"
        message={error}
        guidance="当前人物谱、场景库和已生成图片未改变。可检查填写项后重试。"
      >
        <button type="button" className="btn small ghost" onClick={() => void refresh()}>重新加载建议</button>
      </OperationError>}
      <ul className="auto-change-list">
        {items.map(item => {
          const evidenceFragments = normalizeEvidenceFragments(item.payload?.evidence_fragments)
          return <li key={item.id}>
            <div>
              <b>{item.scene || item.character || '未命名'}</b>
              <span> · {item.kind === 'appearance_drift' ? '外观漂移' : item.kind === 'scene_discovery' ? '新场景发现' : item.kind === 'scene_state_change' ? '场景状态变化' : '自动变更'}</span>
              <span> · {statusLabel(item.status)}</span>
              {item.ep_start != null && <span> · 自第{item.ep_start}集</span>}
            </div>
            <p>{item.reason || '无说明'}{(item.change_dimensions || []).length ? `（${item.change_dimensions?.join('/')}）` : ''}</p>
            {!!evidenceFragments.length && (
              <details>
                <summary>查看原文证据</summary>
                <ul>{evidenceFragments.map((text, index) => <li key={index}>{text}</li>)}</ul>
              </details>
            )}
            <div style={{ display: 'grid', gap: 6, marginBottom: 8 }}>
              <input value={reasons[item.id] || ''}
                aria-label={`${item.scene || item.character || '当前建议'}的处理原因`}
                onChange={event => setReasons(current => ({ ...current, [item.id]: event.target.value }))}
                placeholder="处理原因（可选）" />
              <input value={episodeStarts[item.id] ?? String(item.ep_start ?? '')}
                aria-label={`${item.scene || item.character || '当前建议'}的适用起始集`}
                onChange={event => setEpisodeStarts(current => ({ ...current, [item.id]: event.target.value }))}
                inputMode="numeric" placeholder="适用起始集" />
              <input value={mergeTargets[item.id] || ''}
                aria-label={`${item.scene || item.character || '当前建议'}要并入的已有${item.scene ? '场景' : '角色'}`}
                onChange={event => setMergeTargets(current => ({ ...current, [item.id]: event.target.value }))}
                placeholder={`合并时填写已有${item.scene ? '场景' : '角色'}名`} />
            </div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {(['approve', 'reject', 'rollback', 'merge'] as AutoChangeDecision[]).map(decision => (
                <button
                  key={decision}
                  type="button"
                  className="btn small"
                  disabled={!!busy}
                  aria-label={busy
                    ? `${DECISION_LABELS[decision]}，暂不可用：正在处理上一条更新建议`
                    : `${DECISION_LABELS[decision]}；下一步确认影响`}
                  onClick={() => {
                    const mergeTarget = (mergeTargets[item.id] || '').trim()
                    if (decision === 'merge' && !mergeTarget) {
                      setError(`合并重复${item.scene ? '场景' : '角色'}时必须填写目标${item.scene ? '场景' : '角色'}名`)
                      return
                    }
                    setError('')
                    setPendingDecision({ item, decision })
                  }}
                >
                  {DECISION_LABELS[decision]}
                </button>
              ))}
            </div>
          </li>
        })}
      </ul>
      {pendingDecision && (() => {
        const copy = autoChangeDecisionCopy(pendingDecision.item, pendingDecision.decision)
        return <DecisionDialog
          title={copy.title}
          summary={`${pendingDecision.item.scene || pendingDecision.item.character || '当前建议'} · ${DECISION_LABELS[pendingDecision.decision]}`}
          message={copy.message}
          details={copy.details}
          confirmLabel={`确认${DECISION_LABELS[pendingDecision.decision]}`}
          cancelLabel="返回检查"
          danger={copy.danger}
          onClose={() => setPendingDecision(null)}
          onConfirm={() => void submitDecision()}
        />
      })()}
    </section>
  )
}
