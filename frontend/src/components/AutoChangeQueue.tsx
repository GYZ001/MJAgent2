import { AutoChangeItem, api } from '../api'
import { statusLabel } from '../lib/statusLabels'
import { useEffect, useState } from 'react'

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

  if (!items.length && !error) {
    return (
      <section className="card">
        <h3>自动变更记录</h3>
        <div className="empty">暂无自动发现或漂移记录</div>
      </section>
    )
  }

  return (
    <section className="card">
      <h3>自动变更记录 / 待审队列</h3>
      {error && <div className="error-banner">{error}</div>}
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
                onChange={event => setReasons(current => ({ ...current, [item.id]: event.target.value }))}
                placeholder="审核原因（可选）" />
              <input value={episodeStarts[item.id] ?? String(item.ep_start ?? '')}
                onChange={event => setEpisodeStarts(current => ({ ...current, [item.id]: event.target.value }))}
                inputMode="numeric" placeholder="适用起始集" />
              <input value={mergeTargets[item.id] || ''}
                onChange={event => setMergeTargets(current => ({ ...current, [item.id]: event.target.value }))}
                placeholder={`合并时填写已有${item.scene ? '场景' : '角色'}名`} />
            </div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {['approve', 'reject', 'rollback', 'merge'].map(decision => (
                <button
                  key={decision}
                  type="button"
                  className="btn small"
                  disabled={!!busy}
                  onClick={async () => {
                    const mergeTarget = (mergeTargets[item.id] || '').trim()
                    if (decision === 'merge' && !mergeTarget) {
                      setError(`合并重复${item.scene ? '场景' : '角色'}时必须填写目标${item.scene ? '场景' : '角色'}名`)
                      return
                    }
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
                    } catch (e: unknown) {
                      setError((e as Error).message)
                    } finally {
                      setBusy(null)
                    }
                  }}
                >
                  {decision === 'approve' ? '批准' : decision === 'reject' ? '拒绝' : decision === 'rollback' ? '回滚' : '合并重复'}
                </button>
              ))}
            </div>
          </li>
        })}
      </ul>
    </section>
  )
}
