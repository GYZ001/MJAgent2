import { useFocusTrap } from '../hooks/useFocusTrap'

export type VisualStyleOption = {
  name: string
  description: string
  sample_image: string
  /** 照片级真人摄影质感：视频阶段有较高概率因供应商隐私政策判定疑似真人而被
   *  拒收，弹窗据此显示提示，不禁止选择。 */
  photographic: boolean
}

/**
 * 人物谱与场景库共用的统一画风选择弹窗。
 *
 * 画风是项目级设置：确认后会同时影响人物谱定妆照与场景库场景图的生成用词，
 * 不是只改当前页面看到的东西。两个页面必须复用同一份组件与同一套状态获取
 * 逻辑（见 useVisualStyleDialog），不得各自拷贝一份——那样以后改一处、漏另
 * 一处，画风口径必然漂移。
 *
 * 两个页面确认后触发的下游动作不同（人物谱页生成定妆照，场景库页生成场景
 * 图），由调用方通过 onConfirm 决定，这个组件本身不关心下游。
 */
export default function VisualStyleDialog({
  open,
  loading,
  error,
  options,
  selected,
  scopeNote,
  confirmLabel = '确认风格并预览影响',
  onSelect,
  onClose,
  onConfirm,
}: {
  open: boolean
  loading: boolean
  error: string | null
  options: VisualStyleOption[]
  selected: string
  scopeNote?: string
  confirmLabel?: string
  onSelect: (name: string) => void
  onClose: () => void
  onConfirm: () => void
}) {
  const trapRef = useFocusTrap(open, onClose)
  if (!open) return null
  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog" role="dialog" aria-modal="true" aria-label="选择统一画面风格">
        <h3>选择统一画面风格</h3>
        <p>
          该风格是项目级设置：写入后端任务合同后，人物定妆照、场景图、分镜和视频都会沿用同一份画风，
          不只影响当前页面。示例图供参考，实际生成效果以最终成片为准。
        </p>
        {scopeNote && <p className="hint">{scopeNote}</p>}
        {loading && <div className="query-inline">正在读取风格列表…</div>}
        {error && <div className="error-banner" role="alert">{error}</div>}
        {!loading && !error && (
          <div className="visual-style-list" role="listbox" aria-label="统一画面风格列表">
            {options.map(option => (
              <button
                key={option.name}
                type="button"
                className={`visual-style-option${selected === option.name ? ' selected' : ''}`}
                aria-pressed={selected === option.name}
                onClick={() => onSelect(option.name)}
              >
                {option.sample_image && (
                  <img
                    className="visual-style-thumb"
                    src={option.sample_image}
                    alt={`${option.name}示例图`}
                    loading="lazy"
                  />
                )}
                <span>
                  <b>{option.name}</b>
                  <small>{option.description}</small>
                  {option.photographic && (
                    <small className="warning-banner">
                      视频阶段较高概率因疑似真人被供应商隐私政策拒收，仅出图可放心选用；需要出视频建议改选其它画风
                    </small>
                  )}
                </span>
                {selected === option.name && <em>已选</em>}
              </button>
            ))}
          </div>
        )}
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose}>取消</button>
          <button
            type="button"
            className="btn primary"
            disabled={loading || !!error || !selected}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </section>
    </div>
  )
}
