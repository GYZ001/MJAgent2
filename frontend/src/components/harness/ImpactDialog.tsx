export interface ImpactSummary {
  stale_descendant_ids?: string[]
  requires_reconfirm?: boolean
  paid_media_invalidated?: boolean
  by_artifact_type?: Record<string, number>
  change_types?: string[]
  rebuild?: {
    image_count?: number
    unit_price_cny?: number
    estimated_cost_cny?: number
    max_retry_budget_cny?: number
    note?: string
  }
  paid_assets?: { character_portraits?: number; scene_references?: number }
  old_asset_policy?: string
  stale_count?: number
}

const CHANGE_TYPE_LABELS: Record<string, string> = {
  text_only: '仅文字修订',
  text_fields: '角色文字字段修订',
  character_appearance: '角色外观修订',
  global_style: '全局画风变更',
}

export default function ImpactDialog({
  open,
  title = '修改影响预览',
  impact,
  knownEffects,
  loading,
  error,
  confirmLabel = '确认并创建新版本',
  onConfirm,
  onClose,
}: {
  open: boolean
  title?: string
  impact?: ImpactSummary | null
  knownEffects?: string[]
  loading?: boolean
  error?: string | null
  confirmLabel?: string
  onConfirm: () => void
  onClose: () => void
}) {
  if (!open) return null
  const staleCount = impact?.stale_count ?? impact?.stale_descendant_ids?.length
  const byType = impact?.by_artifact_type || {}
  const typeLines = Object.entries(byType).map(([type, count]) => `${type} × ${count}`)
  const changeLabels = (impact?.change_types || []).map(t => CHANGE_TYPE_LABELS[t] || t)
  const canConfirm = !loading && !error && !!impact

  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section className="impact-dialog" role="dialog" aria-modal="true" aria-label={title}>
        <h3>{title}</h3>
        {loading && <p>正在计算下游影响与重建费用…</p>}
        {error && <p className="error-banner" style={{ whiteSpace: 'pre-wrap' }}>{error}</p>}
        {!loading && !error && impact && (
          <>
            <p>以下为服务端定稿前预检结果；确认后才会写入新版本。</p>
            <ul>
              {changeLabels.length > 0 && <li>变更类型：{changeLabels.join('、')}</li>}
              <li>{impact.requires_reconfirm !== false ? '需要重新通过分镜人工门禁' : '无需重新确认'}</li>
              <li>{impact.paid_media_invalidated
                ? '关联参考图、视频和交付包将失效，需重新生成'
                : '当前没有付费媒体受影响'}</li>
              <li>
                {typeof staleCount === 'number'
                  ? `预计失效下游 Artifact：${staleCount}`
                  : '预计失效下游 Artifact：未知'}
              </li>
              {typeLines.length > 0 && <li>按类型：{typeLines.join('；')}</li>}
              {impact.paid_assets && (
                <li>
                  已付费资产：定妆 {impact.paid_assets.character_portraits ?? 0}、
                  场景图 {impact.paid_assets.scene_references ?? 0}
                </li>
              )}
              {impact.rebuild && (
                <li>
                  预计重建：{impact.rebuild.image_count ?? 0} 张 × ¥{impact.rebuild.unit_price_cny ?? 0}
                  ＝ ¥{impact.rebuild.estimated_cost_cny ?? 0}
                  （最大重试预算 ¥{impact.rebuild.max_retry_budget_cny ?? 0}）
                </li>
              )}
              {impact.old_asset_policy && <li>{impact.old_asset_policy}</li>}
              {(knownEffects ?? []).map(item => <li key={item}>{item}</li>)}
            </ul>
          </>
        )}
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose}>取消</button>
          <button type="button" className="btn primary" disabled={!canConfirm} onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </section>
    </div>
  )
}
