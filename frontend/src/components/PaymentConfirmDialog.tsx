import type { RefsCostPrecheck } from '../api'

export default function PaymentConfirmDialog({
  open,
  title,
  precheck,
  loading,
  error,
  onConfirm,
  onClose,
}: {
  open: boolean
  title: string
  precheck?: RefsCostPrecheck | null
  loading?: boolean
  error?: string | null
  onConfirm: () => void
  onClose: () => void
}) {
  if (!open) return null
  const canConfirm = !loading && !error && !!precheck && (precheck.image_count ?? 0) >= 0

  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section className="impact-dialog" role="dialog" aria-modal="true" aria-label={title}>
        <h3>{title}</h3>
        {loading && <p>正在估算图片数量与费用…</p>}
        {error && <p className="error-banner" style={{ whiteSpace: 'pre-wrap' }}>{error}</p>}
        {!loading && !error && precheck && (
          <>
            <p>确认后才会创建付费任务；取消不会扣费、不会替换资产。</p>
            <ul>
              <li>范围：{precheck.character_count} 个角色 · 每角色 {precheck.views_per_character} 视角</li>
              <li>预计图片：{precheck.image_count} 张 × ¥{precheck.unit_price_cny} = ¥{precheck.estimated_cost_cny}</li>
              <li>最大重试预算：¥{precheck.max_retry_budget_cny}</li>
              {precheck.old_asset_policy && <li>{precheck.old_asset_policy}</li>}
              {precheck.stop_policy && <li>{precheck.stop_policy}</li>}
              {precheck.idempotency_hint && <li>{precheck.idempotency_hint}</li>}
            </ul>
          </>
        )}
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose}>取消</button>
          <button type="button" className="btn primary" disabled={!canConfirm} onClick={onConfirm}>
            确认并开始
          </button>
        </div>
      </section>
    </div>
  )
}
