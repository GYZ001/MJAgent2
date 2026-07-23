import type { ContextEnvelope } from './types'

export default function ContextChips({
  context,
  onClearShot,
}: {
  context: ContextEnvelope
  onClearShot?: () => void
}) {
  const chips: { key: string; label: string; onRemove?: () => void }[] = []
  if (context.project_id) chips.push({ key: 'project', label: `项目 ${context.project_id.slice(0, 8)}` })
  if (context.episode_id) chips.push({ key: 'episode', label: `分集 ${context.episode_id.slice(0, 8)}` })
  if (context.selected_shot_id) {
    chips.push({
      key: 'shot',
      label: `镜头 ${context.selected_shot_id.slice(0, 8)}`,
      onRemove: onClearShot,
    })
  }
  chips.push({ key: 'route', label: `页面 ${context.route}` })
  if (context.unsaved_draft) chips.push({ key: 'draft', label: '有未保存草稿' })

  return (
    <div className="agent-context-chips" aria-label="当前作用域">
      {chips.map(chip => (
        <span key={chip.key} className={`agent-chip ${chip.key === 'draft' ? 'warn' : ''}`}>
          {chip.label}
          {chip.onRemove && (
            <button type="button" className="agent-chip-x" aria-label="移除" onClick={chip.onRemove}>×</button>
          )}
        </span>
      ))}
    </div>
  )
}
