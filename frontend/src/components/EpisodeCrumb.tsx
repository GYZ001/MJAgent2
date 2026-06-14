import { numToCn } from '../api'
import { useNav, useProject } from '../App'
import type { View } from '../App'

interface EpisodeCrumbProps {
  label: string
  view: View
  episodeNo?: number
}

export default function EpisodeCrumb({ label, view, episodeNo }: EpisodeCrumbProps) {
  const { projectId, episodeId, go } = useNav()
  const { data: project } = useProject(projectId!, 0)
  const episodes = project?.episodes ?? []

  return (
    <div className="crumb crumb-switch">
      <button className="crumb-btn" type="button" onClick={() => go(view, projectId, episodeId)}>
        {label}
      </button>
      <span className="crumb-sep">/</span>
      <select
        className="episode-switch"
        value={episodeId ?? ''}
        aria-label="切换当前分集"
        disabled={!episodes.length}
        onChange={e => go(view, projectId, e.target.value)}
      >
        {episodes.map(ep => (
          <option key={ep.id} value={ep.id}>
            第{numToCn(ep.episode_no)}集 · {ep.title}
          </option>
        ))}
        {!episodes.length && episodeNo !== undefined && (
          <option value="">第{numToCn(episodeNo)}集</option>
        )}
      </select>
    </div>
  )
}
