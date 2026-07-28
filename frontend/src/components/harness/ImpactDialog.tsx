import { useState } from 'react'
import { useFocusTrap } from '../../hooks/useFocusTrap'
import { artifactTypeLabel, artifactTypeTitle, statusLabel, statusTitle } from '../../lib/statusLabels'

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
  stale_assets?: Array<{ id: string; type: string; status: string; scope_type?: string | null; scope_id?: string | null }>
  stale_assets_truncated?: boolean
}

const CHANGE_TYPE_LABELS: Record<string, string> = {
  text_only: '仅文字修订',
  text_fields: '角色文字字段修订',
  character_appearance: '角色外观修订',
  global_style: '全局画风变更',
}

export function impactBusinessText(value: string): string {
  return value
    .replaceAll('人工门禁', '人工确认')
    .replaceAll('硬门禁', '必检项')
    .replaceAll('门禁', '必检项')
    .replaceAll('QA', '质检')
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
  const trapRef = useFocusTrap(open, onClose)
  const [stalePage, setStalePage] = useState(0)
  if (!open) return null
  const staleCount = impact?.stale_count ?? impact?.stale_descendant_ids?.length
  const byType = impact?.by_artifact_type || {}
  const typeLines = Object.entries(byType).map(([type, count]) => `${artifactTypeLabel(type)} × ${count}`)
  const changeLabels = (impact?.change_types || []).map(t => CHANGE_TYPE_LABELS[t] || impactBusinessText(t))
  const canConfirm = !loading && !error && !!impact
  const confirmDisabledReason = loading
    ? '正在计算影响与费用'
    : error
      ? '影响预览失败，请取消后重试'
      : !impact
        ? '尚未取得影响预览'
        : ''
  const staleAssets = impact?.stale_assets ?? []
  const stalePageCount = Math.max(1, Math.ceil(staleAssets.length / 20))
  const curStalePage = Math.min(stalePage, stalePageCount - 1)
  const pagedStaleAssets = staleAssets.slice(curStalePage * 20, curStalePage * 20 + 20)

  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog" role="dialog" aria-modal="true" aria-label={title}>
        <h3>{title}</h3>
        {loading && <p>正在计算下游影响与重建费用…</p>}
        {error && <p className="error-banner" style={{ whiteSpace: 'pre-wrap' }}>{error}</p>}
        {!loading && !error && impact && (
          <>
            <p>以下为执行前影响预览；只有点击“{confirmLabel}”才会写入新版本。</p>
            <ul>
              {changeLabels.length > 0 && <li>变更类型：{changeLabels.join('、')}</li>}
              <li>{impact.requires_reconfirm !== false ? '需要重新完成分镜必检项与人工确认' : '无需重新确认'}</li>
              <li>{impact.paid_media_invalidated
                ? '关联参考图、视频和交付包将失效，需重新生成'
                : '当前没有付费媒体受影响'}</li>
              <li>
                {typeof staleCount === 'number'
                  ? `需重新生成的下游内容：${staleCount}`
                  : '需重新生成的下游内容：暂无法估算'}
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
              {impact.old_asset_policy && <li>{impactBusinessText(impact.old_asset_policy)}</li>}
              {(knownEffects ?? []).map(item => <li key={item}>{impactBusinessText(item)}</li>)}
            </ul>
            {!!staleAssets.length && (
              <div className="impact-stale-assets">
                <h4>需重新生成的内容</h4>
                {impact.stale_assets_truncated && (
                  <p className="hint">这里只展示前 {staleAssets.length} 项，可在任务详情中查看完整范围。</p>
                )}
                <ul>
                  {pagedStaleAssets.map(asset => (
                    <li key={`${asset.type}:${asset.id}`}>
                      <span title={artifactTypeTitle(asset.type)}>{artifactTypeLabel(asset.type)}</span>
                      <span title={statusTitle(asset.status)}>{statusLabel(asset.status)}</span>
                      <details>
                        <summary>技术标识</summary>
                        <code>{asset.id}</code>
                        {(asset.scope_type || asset.scope_id) && (
                          <small>范围：{asset.scope_type || '未记录'} / {asset.scope_id || '未记录'}</small>
                        )}
                      </details>
                    </li>
                  ))}
                </ul>
                {stalePageCount > 1 && (
                  <div className="impact-stale-pager">
                    <button type="button" className="btn small" disabled={curStalePage <= 0}
                      aria-label={curStalePage <= 0 ? '上一页，暂不可用：当前已是第一页' : '上一页'}
                      onClick={() => setStalePage(curStalePage - 1)}>上一页</button>
                    <span>第 {curStalePage + 1} / {stalePageCount} 页</span>
                    <button type="button" className="btn small" disabled={curStalePage >= stalePageCount - 1}
                      aria-label={curStalePage >= stalePageCount - 1 ? '下一页，暂不可用：当前已是最后一页' : '下一页'}
                      onClick={() => setStalePage(curStalePage + 1)}>下一页</button>
                  </div>
                )}
              </div>
            )}
          </>
        )}
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose}>取消</button>
          <button type="button" className="btn primary" disabled={!canConfirm}
            aria-label={confirmDisabledReason ? `${confirmLabel}，暂不可用：${confirmDisabledReason}` : confirmLabel}
            onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </section>
    </div>
  )
}
