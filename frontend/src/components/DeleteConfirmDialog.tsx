import { useFocusTrap } from '../hooks/useFocusTrap'
import type { ApprovalPreflight } from '../api/client'

/**
 * 「删除资源」类命令的通用确认弹窗：与 hooks/useDeleteConfirm 配对使用。
 * 展示后端 preflight 给出的影响范围（summary + affected），确认后才真正
 * 提交删除。不针对具体某个页面/命令定制文案——那会退化回「每个删除各写一个
 * 弹窗」，catalog 里新登记的删除类命令也用不上它。
 */
export default function DeleteConfirmDialog({
  pending,
  busy,
  onCancel,
  onConfirm,
}: {
  pending: ApprovalPreflight | null
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const trapRef = useFocusTrap(!!pending, onCancel, { suspended: busy })
  if (!pending) return null
  const affected = pending.affected
  const scopeLines: string[] = []
  if (affected?.projects?.length) scopeLines.push(`项目 ${affected.projects.length} 个`)
  if (affected?.episodes?.length) scopeLines.push(`分集 ${affected.episodes.length} 个`)
  if (affected?.shot_count) scopeLines.push(`镜头 ${affected.shot_count} 个`)
  if (affected?.versions?.length) scopeLines.push(`版本 ${affected.versions.length} 个`)
  if (affected?.packages?.length) scopeLines.push(`交付包 ${affected.packages.length} 个`)

  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target && !busy) onCancel()
    }}>
      <section ref={trapRef} className="impact-dialog" role="dialog" aria-modal="true" aria-label="确认删除">
        <h3>确认删除</h3>
        <p>{pending.summary}</p>
        {scopeLines.length > 0 && <p>影响范围：{scopeLines.join(' · ')}</p>}
        <p className="hint">此操作不可撤销。</p>
        <div className="dialog-actions">
          <button type="button" className="btn" disabled={busy} onClick={onCancel}>取消</button>
          <button
            type="button"
            className="btn danger"
            disabled={busy}
            aria-label={busy ? '确认删除，暂不可用：正在删除' : '确认删除'}
            onClick={onConfirm}
          >
            {busy ? '删除中…' : '确认删除'}
          </button>
        </div>
      </section>
    </div>
  )
}
