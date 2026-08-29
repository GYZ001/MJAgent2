import { useFocusTrap } from '../hooks/useFocusTrap'
import type { RefsCostPrecheck, SceneCostPrecheck } from '../api'

export interface StyleRegenQuote {
  quote_id: string
  action: string
  project_id: string
  characters: RefsCostPrecheck
  scenes: SceneCostPrecheck | null
  scene_bible_ready: boolean
  total_image_count: number
  total_estimated_cost_cny: number
  total_max_retry_budget_cny: number
  idempotency_hint?: string
  stop_policy?: string
}

/**
 * 风格切换（人物谱页「更换统一画风（不改人物设定）」/ 场景库页「配置统一画风」）
 * 的合并付费确认弹窗：人物定妆照与场景图两条腿的费用一次性摆出来，确认一次，
 * 两条线都会在后端同一次请求里被发起——不是确认了人物这条腿之后，场景那条
 * 腿再等一个前端 effect 或者用户之后访问场景库页面才继续。
 *
 * 独立于 PaymentConfirmDialog：那个组件按「单一角色域 or 单一场景域」设计
 * （scope 数组只能是其中一种形状），这里天然是两个域的合并展示，硬塞进去
 * 会牵动它现有 10+ 个调用点共用的渲染分支；新建一个专用组件零风险。
 */
export default function StyleRegenConfirmDialog({
  open,
  styleName,
  quote,
  loading,
  error,
  onClose,
  onConfirm,
}: {
  open: boolean
  styleName: string
  quote: StyleRegenQuote | null
  loading: boolean
  error: string | null
  onClose: () => void
  onConfirm: () => void
}) {
  const trapRef = useFocusTrap(open, onClose)
  if (!open) return null
  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog" role="dialog" aria-modal="true"
        aria-label={`确认按新画风「${styleName}」重新生成`}>
        <h3>按新画风「{styleName}」重新生成</h3>
        <p>
          确认后才会创建付费任务；取消不会扣费、不会替换资产。人物定妆照与场景图会在
          这一次确认里一起发起，无需分别到两个页面各点一次。
        </p>
        {loading && <div className="query-inline">正在估算费用…</div>}
        {error && <div className="error-banner" role="alert">{error}</div>}
        {!loading && !error && quote && (
          <ul>
            <li>
              人物定妆照：{quote.characters.character_count} 个角色 ·{' '}
              {quote.characters.image_count} 张图 · ¥{quote.characters.estimated_cost_cny}
            </li>
            {quote.scenes ? (
              <li>
                场景图：{quote.scenes.scene_count} 个场景 · {quote.scenes.actual_view_count} 张图 ·
                ¥{quote.scenes.estimated_cost_cny}
              </li>
            ) : (
              <li>场景图：场景清单尚未生成，本次确认不会生成场景图；请先准备场景清单后再单独触发</li>
            )}
            <li>合计：{quote.total_image_count} 张图 · ¥{quote.total_estimated_cost_cny}</li>
            <li>最大重试预算：¥{quote.total_max_retry_budget_cny}</li>
            {quote.stop_policy && <li>{quote.stop_policy}</li>}
            {quote.idempotency_hint && <li>{quote.idempotency_hint}</li>}
          </ul>
        )}
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose}>取消</button>
          <button
            type="button"
            className="btn primary"
            disabled={loading || !!error || !quote}
            onClick={onConfirm}
          >
            确认并开始
          </button>
        </div>
      </section>
    </div>
  )
}
