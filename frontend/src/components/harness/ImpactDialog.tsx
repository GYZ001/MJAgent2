export interface ImpactSummary {
  stale_descendant_ids?: string[]
  requires_reconfirm?: boolean
  paid_media_invalidated?: boolean
}

export default function ImpactDialog({
  open,
  title = '修改影响预览',
  impact,
  onConfirm,
  onClose,
}: {
  open: boolean
  title?: string
  impact?: ImpactSummary | null
  onConfirm: () => void
  onClose: () => void
}) {
  if (!open) return null
  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section className="impact-dialog" role="dialog" aria-modal="true" aria-label={title}>
        <h3>{title}</h3>
        <p>该操作会生成新的 Artifact 版本，并保留旧版本用于复验。</p>
        <ul>
          <li>{impact?.requires_reconfirm !== false ? '需要重新通过分镜人工门禁' : '无需重新确认'}</li>
          <li>{impact?.paid_media_invalidated !== false ? '关联关键帧、视频和交付包可能失效' : '当前没有付费媒体受影响'}</li>
          <li>预计失效下游 Artifact：{impact?.stale_descendant_ids?.length ?? '待保存后计算'}</li>
        </ul>
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose}>取消</button>
          <button type="button" className="btn primary" onClick={onConfirm}>确认并创建新版本</button>
        </div>
      </section>
    </div>
  )
}
