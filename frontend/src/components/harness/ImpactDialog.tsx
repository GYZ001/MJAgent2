export interface ImpactSummary {
  stale_descendant_ids?: string[]
  requires_reconfirm?: boolean
  paid_media_invalidated?: boolean
}

export default function ImpactDialog({
  open,
  title = '修改影响预览',
  impact,
  knownEffects,
  onConfirm,
  onClose,
}: {
  open: boolean
  title?: string
  impact?: ImpactSummary | null
  knownEffects?: string[]
  onConfirm: () => void
  onClose: () => void
}) {
  if (!open) return null
  const staleCount = impact?.stale_descendant_ids?.length
  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section className="impact-dialog" role="dialog" aria-modal="true" aria-label={title}>
        <h3>{title}</h3>
        <p>以下为基于当前产物的<strong>预期影响</strong>；精确失效数量在保存后由服务端回传。</p>
        <ul>
          <li>{impact?.requires_reconfirm !== false ? '需要重新通过分镜人工门禁' : '无需重新确认'}</li>
          <li>{impact?.paid_media_invalidated
            ? '关联参考图、视频和交付包将失效，需重新生成'
            : impact?.paid_media_invalidated === false
              ? '当前没有付费媒体受影响'
              : '若本镜已有参考图/视频，保存后将清空并需重做'}</li>
          <li>
            {typeof staleCount === 'number'
              ? `预计失效下游 Artifact：${staleCount}`
              : '预计失效下游 Artifact：保存后计算'}
          </li>
          {(knownEffects ?? []).map(item => <li key={item}>{item}</li>)}
        </ul>
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose}>取消</button>
          <button type="button" className="btn primary" onClick={onConfirm}>确认并创建新版本</button>
        </div>
      </section>
    </div>
  )
}
