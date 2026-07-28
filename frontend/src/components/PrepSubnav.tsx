import { useNav, type View } from '../App'
import { prepStepLabel, type PrepStepStatus } from '../lib/statusLabels'

const TABS: { key: View; label: string; description: string }[] = [
  { key: 'bible', label: '人物谱', description: '角色与定妆资产' },
  { key: 'scenes', label: '场景库', description: '场景设定与参考图' },
  { key: 'episodes', label: '分集规划', description: '章节拆分与制作进度' },
]

export type PrepStepStatuses = Partial<Record<'bible' | 'scenes' | 'episodes', PrepStepStatus>>

/** 前期准备三页共用的一级任务导航（含任务状态）。 */
export default function PrepSubnav({
  current,
  statuses,
  onProblemClick,
  onBeforeNavigate,
}: {
  current: View
  statuses?: PrepStepStatuses
  onProblemClick?: (key: 'bible' | 'scenes' | 'episodes') => void
  onBeforeNavigate?: (target: View) => boolean
}) {
  const { go, projectId } = useNav()
  if (!projectId) return null
  return (
    <nav className="prep-subnav" aria-label="前期准备">
      {TABS.map((tab, index) => {
        const status = statuses?.[tab.key as 'bible' | 'scenes' | 'episodes']
        const label = status ? prepStepLabel(status) : ''
        return (
          <button
            key={tab.key}
            type="button"
            aria-current={current === tab.key ? 'page' : undefined}
            className={`prep-subnav-tab${current === tab.key ? ' active' : ''}${status ? ` prep-status-${status}` : ''}`}
            onClick={() => {
              if (tab.key !== current && onBeforeNavigate && !onBeforeNavigate(tab.key)) return
              if (status === 'problem' && onProblemClick) {
                onProblemClick(tab.key as 'bible' | 'scenes' | 'episodes')
              }
              go(tab.key, projectId)
            }}
          >
            <span className="prep-subnav-index" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
            <span className="prep-subnav-copy">
              <strong>{tab.label}</strong>
              <small>{tab.description}</small>
              {status && (
                <em className="prep-step-status" title={label}>
                  <span aria-hidden="true">{status === 'done' ? '✓' : status === 'problem' ? '!' : status === 'running' ? '…' : '○'}</span>
                  {label}
                </em>
              )}
            </span>
          </button>
        )
      })}
    </nav>
  )
}
