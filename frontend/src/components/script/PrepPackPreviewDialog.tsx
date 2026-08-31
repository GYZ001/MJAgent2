import { useFocusTrap } from '../../hooks/useFocusTrap'
import type { ActionPreview } from '../../pages/ScriptPage'

/**
 * 首次生成映射包的预检确认弹窗，从 ScriptPage.tsx 抽出（该文件贴着
 * FILE_CONVENTIONS.toml 的行数基线，见该文件顶部说明）。纯展示 + 两个回调，
 * 不持有除 `preview` 外的任何状态，焦点圈定（useFocusTrap）随组件一起搬迁。
 *
 * 这是映射台唯一一处"点下去会花钱、真正开始生成"之前的确认点，所以是说明
 * "点了会发生什么"的最佳位置（CLAUDE.md「界面承诺必须与实际行为一致」）：
 * 新架构下映射台是用户拿到角色卡的唯一入口——新角色/新场景会自动建卡并
 * 生成定妆照，已在人物谱/场景库里的会自动匹配复用，不需要用户分两步操作。
 * 第二条 <li> 就是这句承诺的落地，用户点击「启动首版映射包生成」之前必读。
 */
export default function PrepPackPreviewDialog({
  preview,
  onCancel,
  onConfirm,
}: {
  preview: ActionPreview | null
  onCancel: () => void
  onConfirm: () => void
}) {
  const trapRef = useFocusTrap(Boolean(preview), onCancel)
  if (!preview) return null
  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onCancel()
    }}>
      <section ref={trapRef} className="impact-dialog" role="dialog" aria-modal="true" aria-label={preview.title}>
        <h3>{preview.title}</h3>
        <p>预检不会创建任务；只有点击下方执行按钮才会发起。</p>
        <ul>
          <li>
            原文 {preview.data.input?.source_chars ?? '—'} 字，
            覆盖 {preview.data.input?.source_chapters?.length ?? '—'} 个源章节
          </li>
          <li>
            将发现本集出场的人物与场景：谱外新角色/新场景会自动建卡并生成定妆照，
            已在人物谱、场景库中的会自动匹配复用已有素材——不需要再单独去别处操作。
          </li>
          {preview.data.blueprint_budget?.requires_fresh_retry_grant && (
            <li className="danger">
              上次生成的模型调用被中断、结果未知（常见于服务重启或网络波动）。
              为避免重复扣费，系统已暂停自动重试；确认继续将授权对同一环节重新发起一次付费调用。
            </li>
          )}
        </ul>
        <div className="dialog-actions">
          <button className="btn" onClick={onCancel}>取消（不执行）</button>
          <button className="btn primary" onClick={onConfirm}>
            {preview.data.blueprint_budget?.requires_fresh_retry_grant
              ? '授权并重试（可能重新计费）'
              : '启动首版映射包生成'}
          </button>
        </div>
      </section>
    </div>
  )
}
