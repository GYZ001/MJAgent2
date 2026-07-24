import { useNav, type View } from '../App'

const TABS: { key: View; label: string }[] = [
  { key: 'bible', label: '人物谱' },
  { key: 'scenes', label: '场景库' },
  { key: 'episodes', label: '分集规划' },
]

/** 前期准备三页共用的轻量子导航，减少侧栏心智负担。 */
export default function PrepSubnav({ current }: { current: View }) {
  const { go, projectId } = useNav()
  if (!projectId) return null
  return (
    <div className="prep-subnav" role="tablist" aria-label="前期准备">
      {TABS.map(tab => (
        <button
          key={tab.key}
          type="button"
          role="tab"
          aria-selected={current === tab.key}
          className={`prep-subnav-tab${current === tab.key ? ' active' : ''}`}
          onClick={() => go(tab.key, projectId)}
        >
          {tab.label}
        </button>
      ))}
    </div>
  )
}
