import { useEffect, useMemo, useState } from 'react'
import type { RefsCostPrecheck, SceneCostPrecheck } from '../api'
import { useFocusTrap } from '../hooks/useFocusTrap'

type PaymentSelection = { characters: string[]; scenes?: string[] }

function scopeCharacter(item: Record<string, unknown>): string {
  return String(item.character || item.name || item.character_name || '').trim()
}

function scopeScene(item: Record<string, unknown>): string {
  return String(item.scene || item.scene_name || '').trim()
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
  precheck?: ((RefsCostPrecheck | SceneCostPrecheck) & {
    estimated_duration_min?: number[]
    estimate_note?: string
    character_names?: string[]
  }) | null
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
  const canConfirm = !loading && !error && !!precheck && (precheck.image_count ?? 0) >= 0
  const duration = precheck?.estimated_duration_min
  const showSelection = enableScopeSelection && selectableCharacters.length > 1
  const showSceneSelection = enableScopeSelection && selectableScenes.length > 0
  const selectedSet = new Set(selectedCharacters)
  const selectedSceneSet = new Set(selectedScenes)
  const sceneQuote = precheck && 'scene_count' in precheck ? precheck as SceneCostPrecheck : null

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
                ? `${sceneQuote.scene_count} 个场景 · ${sceneQuote.actual_view_count} 个实际视角`
                : `${(precheck as RefsCostPrecheck).character_count} 个角色 · 每角色 ${(precheck as RefsCostPrecheck).views_per_character} 视角`}</li>
              {!!precheck.character_names?.length && (
                <li>角色：{precheck.character_names.slice(0, 12).join('、')}{precheck.character_names.length > 12 ? '…' : ''}</li>
              )}
              <li>预计图片：{precheck.image_count} 张 × ¥{precheck.unit_price_cny} = ¥{precheck.estimated_cost_cny}</li>
              <li>最大重试预算 / 上限：¥{precheck.max_retry_budget_cny} / ¥{precheck.budget_cap_cny}</li>
              {duration && <li>预计耗时：约 {duration[0]}~{duration[1]} 分钟</li>}
              {precheck.estimate_note && <li>{precheck.estimate_note}</li>}
              {precheck.old_asset_policy && <li>{precheck.old_asset_policy}</li>}
              {precheck.stop_policy && <li>{precheck.stop_policy}</li>}
              {precheck.idempotency_hint && <li>{precheck.idempotency_hint}</li>}
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
                    <span>已选 {showSceneSelection ? selectedScenes.length : selectedCharacters.length} / {showSceneSelection ? selectableScenes.length : selectableCharacters.length}</span>
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
                            {Array.isArray(item.views) ? ` · ${(item.views as string[]).join('/')}` : ''}
                            {item.view_role ? ` · ${String(item.view_role)}` : ''}
                            {item.reason ? ` · ${String(item.reason)}` : ''}
                          </span>
                        </label>
                      ) : (
                        <>
                          {String(item.scene || item.character || (sceneQuote ? '场景' : '角色'))}
                          {Array.isArray(item.views) ? ` · ${(item.views as string[]).join('/')}` : ''}
                          {item.view_role ? ` · ${String(item.view_role)}` : ''}
                          {item.reason ? ` · ${String(item.reason)}` : ''}
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
            disabled={!canConfirm || (showSelection && selectedCharacters.length === 0) || (showSceneSelection && selectedScenes.length === 0)}
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
