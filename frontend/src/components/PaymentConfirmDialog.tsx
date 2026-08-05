import { useEffect, useMemo, useState } from 'react'
import type { RefsCostPrecheck, SceneCostPrecheck } from '../api'
import { useFocusTrap } from '../hooks/useFocusTrap'

type PaymentSelection = { characters: string[]; scenes?: string[] }
type PaymentPrecheck = (RefsCostPrecheck | SceneCostPrecheck) & {
  estimated_duration_min?: number[]
  estimate_note?: string
  character_names?: string[]
}

function scopeCharacter(item: Record<string, unknown>): string {
  return String(item.character || item.name || item.character_name || '').trim()
}

function scopeScene(item: Record<string, unknown>): string {
  return String(item.scene || item.scene_name || '').trim()
}

const VIEW_LABELS: Record<string, string> = {
  front_full: '正面全身',
  three_quarter: '3/4 面',
  profile: '侧面',
  back_full: '背面全身',
  face_closeup: '面部特写',
  wide: '全景',
  medium: '中景',
  closeup: '细节',
}

export function paymentPolicyText(value: string): string {
  return value
    .replace(/同一\s*quote_id\s*/g, '同一报价')
    .replaceAll('quote_id', '报价')
    .replaceAll('服务端', '系统')
    .replace(/\s*QA\s*/g, '质检')
    .replaceAll('角色提示词', '角色设定')
    .replaceAll('提示词', '生成描述')
}

function viewLabel(value: unknown): string {
  const key = String(value || '')
  return VIEW_LABELS[key] || key
}

export function paymentSelectionSummary(
  precheck: PaymentPrecheck,
  selection: PaymentSelection,
): {
  itemCount: number
  imageCount: number
  estimatedCostCny: number
  maxRetryBudgetCny: number
  budgetCapCny: number
} {
  const sceneQuote = 'scene_count' in precheck
  const selected = new Set(sceneQuote ? (selection.scenes ?? []) : selection.characters)
  const scope = precheck.scope.filter(item => {
    const name = sceneQuote ? scopeScene(item) : scopeCharacter(item)
    return name && selected.has(name)
  })
  const imageCount = scope.reduce((total, item) => {
    if (Array.isArray(item.views)) return total + item.views.length
    return total + (item.view_role ? 1 : 0)
  }, 0)
  const estimatedCostCny = Number((imageCount * precheck.unit_price_cny).toFixed(2))
  const retryRatio = precheck.estimated_cost_cny > 0
    ? precheck.max_retry_budget_cny / precheck.estimated_cost_cny
    : 1
  const capRatio = precheck.estimated_cost_cny > 0
    ? precheck.budget_cap_cny / precheck.estimated_cost_cny
    : 1
  return {
    itemCount: selected.size,
    imageCount,
    estimatedCostCny,
    maxRetryBudgetCny: Number((estimatedCostCny * retryRatio).toFixed(2)),
    budgetCapCny: Number((estimatedCostCny * capRatio).toFixed(2)),
  }
}

export default function PaymentConfirmDialog({
  open,
  title,
  precheck,
  loading,
  error,
  onConfirm,
  onClose,
  enableScopeSelection = false,
  scopeSelectionTitle,
}: {
  open: boolean
  title: string
  precheck?: PaymentPrecheck | null
  loading?: boolean
  error?: string | null
  onConfirm: (selection: PaymentSelection) => void
  onClose: () => void
  enableScopeSelection?: boolean
  scopeSelectionTitle?: string
}) {
  const trapRef = useFocusTrap(open, onClose)
  const selectableCharacters = useMemo(() => {
    const names = new Set<string>()
    for (const item of precheck?.scope ?? []) {
      const name = scopeCharacter(item)
      if (name) names.add(name)
    }
    return [...names]
  }, [precheck])
  const selectableScenes = useMemo(() => {
    const names = new Set<string>()
    for (const item of precheck?.scope ?? []) {
      const name = scopeScene(item)
      if (name) names.add(name)
    }
    return [...names]
  }, [precheck])
  const [selectedCharacters, setSelectedCharacters] = useState<string[]>([])
  const [selectedScenes, setSelectedScenes] = useState<string[]>([])

  useEffect(() => {
    if (!open || !enableScopeSelection) return
    setSelectedCharacters(selectableCharacters)
    setSelectedScenes(selectableScenes)
  }, [enableScopeSelection, open, selectableCharacters, selectableScenes])

  if (!open) return null
  const duration = precheck?.estimated_duration_min
  const showSelection = enableScopeSelection && selectableCharacters.length > 1
  const showSceneSelection = enableScopeSelection && selectableScenes.length > 0
  const selectionEmpty = (showSelection && selectedCharacters.length === 0)
    || (showSceneSelection && selectedScenes.length === 0)
  const quoteIncomplete = !precheck || typeof precheck.image_count !== 'number'
  const canConfirm = !loading && !error && !quoteIncomplete && !selectionEmpty
  const confirmDisabledReason = loading
    ? '正在估算范围与费用'
    : error
      ? '费用预览失败，请取消后重试'
      : quoteIncomplete
        ? '尚未取得完整费用预览'
        : selectionEmpty
          ? '请至少选择一个生成对象'
          : ''
  const selectedSet = new Set(selectedCharacters)
  const selectedSceneSet = new Set(selectedScenes)
  const sceneQuote = precheck && 'scene_count' in precheck ? precheck as SceneCostPrecheck : null
  const selectedSummary = precheck && (showSelection || showSceneSelection)
    ? paymentSelectionSummary(precheck, {
      characters: selectedCharacters,
      scenes: selectedScenes,
    })
    : null

  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog" role="dialog" aria-modal="true" aria-label={title}>
        <h3>{title}</h3>
        {loading && <p>正在估算图片数量与费用…</p>}
        {error && <p className="error-banner" style={{ whiteSpace: 'pre-wrap' }}>{error}</p>}
        {!loading && !error && precheck && (
          <>
            <p>确认后才会创建付费任务；取消不会扣费、不会替换资产。</p>
            <ul>
              <li>范围：{sceneQuote
                ? `${selectedSummary?.itemCount ?? sceneQuote.scene_count} 个场景 · ${selectedSummary?.imageCount ?? sceneQuote.actual_view_count} 个实际视角`
                : `${selectedSummary?.itemCount ?? (precheck as RefsCostPrecheck).character_count} 个角色 · 每角色 ${(precheck as RefsCostPrecheck).views_per_character} 视角`}</li>
              {!!precheck.character_names?.length && (
                <li>角色：{precheck.character_names.slice(0, 12).join('、')}{precheck.character_names.length > 12 ? '…' : ''}</li>
              )}
              <li>预计图片：{selectedSummary?.imageCount ?? precheck.image_count} 张 × ¥{precheck.unit_price_cny} = ¥{selectedSummary?.estimatedCostCny ?? precheck.estimated_cost_cny}</li>
              <li>最大重试预算 / 费用上限：¥{selectedSummary?.maxRetryBudgetCny ?? precheck.max_retry_budget_cny} / ¥{selectedSummary?.budgetCapCny ?? precheck.budget_cap_cny}</li>
              {duration && <li>预计耗时：约 {duration[0]}~{duration[1]} 分钟</li>}
              {precheck.estimate_note && <li>{paymentPolicyText(precheck.estimate_note)}</li>}
              {precheck.old_asset_policy && <li>{paymentPolicyText(precheck.old_asset_policy)}</li>}
              {precheck.stop_policy && <li>{paymentPolicyText(precheck.stop_policy)}</li>}
              {precheck.idempotency_hint && <li>{paymentPolicyText(precheck.idempotency_hint)}</li>}
            </ul>
            {!!precheck.scope?.length && (
              <div className="pay-scope-list">
                <h4>{showSelection || showSceneSelection
                  ? (scopeSelectionTitle || '选择本次补齐角色（默认全选缺失项）')
                  : (scopeSelectionTitle || '本次生成明细')}</h4>
                {(showSelection || showSceneSelection) && (
                  <div className="pay-scope-actions">
                    <button type="button" className="btn small" onClick={() => {
                      setSelectedCharacters(selectableCharacters); setSelectedScenes(selectableScenes)
                    }}>
                      全选
                    </button>
                    <button type="button" className="btn small ghost" onClick={() => {
                      setSelectedCharacters([]); setSelectedScenes([])
                    }}>
                      清空
                    </button>
                    <span role="status">已选 {showSceneSelection ? selectedScenes.length : selectedCharacters.length} / {showSceneSelection ? selectableScenes.length : selectableCharacters.length}</span>
                  </div>
                )}
                <ul>
                  {precheck.scope.slice(0, 30).map((item, index) => (
                    <li key={index}>
                      {showSelection || showSceneSelection ? (
                        <label className="pay-scope-option">
                          <input
                            type="checkbox"
                            checked={showSceneSelection
                              ? selectedSceneSet.has(scopeScene(item))
                              : selectedSet.has(scopeCharacter(item))}
                            onChange={event => {
                              const name = showSceneSelection ? scopeScene(item) : scopeCharacter(item)
                              if (!name) return
                              const setter = showSceneSelection ? setSelectedScenes : setSelectedCharacters
                              setter(current => event.target.checked
                                ? [...new Set([...current, name])] : current.filter(item => item !== name))
                            }}
                          />
                          <span>
                            {scopeScene(item) || scopeCharacter(item) || (showSceneSelection ? '场景' : '角色')}
                            {Array.isArray(item.views) ? ` · ${(item.views as string[]).map(viewLabel).join('/')}` : ''}
                            {item.view_role ? ` · ${viewLabel(item.view_role)}` : ''}
                            {item.reason ? ` · ${paymentPolicyText(String(item.reason))}` : ''}
                          </span>
                        </label>
                      ) : (
                        <>
                          {String(item.scene || item.character || (sceneQuote ? '场景' : '角色'))}
                          {Array.isArray(item.views) ? ` · ${(item.views as string[]).map(viewLabel).join('/')}` : ''}
                          {item.view_role ? ` · ${viewLabel(item.view_role)}` : ''}
                          {item.reason ? ` · ${paymentPolicyText(String(item.reason))}` : ''}
                        </>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose}>取消</button>
          <button
            type="button"
            className="btn primary"
            disabled={!canConfirm}
            aria-label={confirmDisabledReason ? `确认并开始，暂不可用：${confirmDisabledReason}` : '确认并开始付费生成'}
            onClick={() => onConfirm({
              characters: showSelection ? selectedCharacters : [],
              scenes: showSceneSelection ? selectedScenes : [],
            })}
          >
            确认并开始
          </button>
        </div>
      </section>
    </div>
  )
}
