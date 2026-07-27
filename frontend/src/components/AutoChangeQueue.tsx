import { AutoChangeItem, api } from '../api'
import { statusLabel } from '../lib/statusLabels'
import { useEffect, useState } from 'react'

export default function AutoChangeQueue({
  projectId,
  onChanged,
}: {
  projectId: string
  onChanged?: () => void
}) {
  const [items, setItems] = useState<AutoChangeItem[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<string | null>(null)

  const refresh = async () => {
    try {
      const res = await api.listAutoChanges(projectId)
      setItems(res.items || [])
      setError('')
    } catch (e: unknown) {
      setError((e as Error).message)
    }
  }

  useEffect(() => { void refresh() }, [projectId])

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
        {items.map(item => (
          <li key={item.id}>
            <div>
              <b>{item.character || '未命名'}</b>
              <span> · {item.kind === 'appearance_drift' ? '外观漂移' : '自动变更'}</span>
              <span> · {statusLabel(item.status)}</span>
              {item.ep_start != null && <span> · 自第{item.ep_start}集</span>}
            </div>
            <p>{item.reason || '无说明'}{(item.change_dimensions || []).length ? `（${item.change_dimensions?.join('/')}）` : ''}</p>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {['approve', 'reject', 'rollback', 'merge'].map(decision => (
                <button
                  key={decision}
                  type="button"
                  className="btn small"
                  disabled={!!busy}
                  onClick={async () => {
                    setBusy(item.id)
                    try {
                      await api.decideAutoChange(projectId, item.id, decision)
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
        ))}
      </ul>
    </section>
  )
}
