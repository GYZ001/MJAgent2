import { useNav, type View } from '../App'

const TABS: { key: View; label: string; description: string }[] = [
  { key: 'bible', label: '人物谱', description: '角色与定妆资产' },
  { key: 'scenes', label: '场景库', description: '场景锚点与参考图' },
  { key: 'episodes', label: '分集规划', description: '章节拆分与制作进度' },
]

/** 前期准备三页共用的一级任务导航。 */
export default function PrepSubnav({ current }: { current: View }) {
  const { go, projectId } = useNav()
  if (!projectId) return null
  return (
    <nav className="prep-subnav" aria-label="前期准备">
      {TABS.map((tab, index) => (
        <button
          key={tab.key}
          type="button"
          aria-current={current === tab.key ? 'page' : undefined}
          className={`prep-subnav-tab${current === tab.key ? ' active' : ''}`}
          onClick={() => go(tab.key, projectId)}
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
