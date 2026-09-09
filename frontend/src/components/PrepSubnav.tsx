import { useNav, type View } from '../App'

const TABS: { key: View; label: string; description: string }[] = [
  { key: 'bible', label: '人物谱', description: '角色与定妆资产' },
  { key: 'scenes', label: '场景库', description: '场景设定与参考图' },
  { key: 'props', label: '物件库', description: '关键道具与参考图' },
  { key: 'episodes', label: '分集规划', description: '章节拆分与制作进度' },
]

/** 世界书四页共用导航，只展示名称、说明与当前选中项。 */
export default function PrepSubnav({
  current,
  onBeforeNavigate,
}: {
  current: View
  onBeforeNavigate?: (target: View) => boolean
}) {
  const { go, projectId } = useNav()
  if (!projectId) return null
  return (
    <nav className="prep-subnav" aria-label="世界书">
      {TABS.map((tab, index) => (
        <button
          key={tab.key}
          type="button"
          aria-current={current === tab.key ? 'page' : undefined}
          className={`prep-subnav-tab${current === tab.key ? ' active' : ''}`}
          onClick={() => {
            if (tab.key !== current && onBeforeNavigate && !onBeforeNavigate(tab.key)) return
            go(tab.key, projectId)
          }}
        >
          <span className="prep-subnav-index" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
          <span className="prep-subnav-copy">
            <strong>{tab.label}</strong>
            <small>{tab.description}</small>
          </span>
        </button>
      ))}
    </nav>
  )
}
