import { useEffect, useState } from 'react'
import { api, numToCn, type Project } from '../api'
import type { AgentView, ContextEnvelope } from './types'

const PAGE_LABELS: Record<AgentView, string> = {
  studio: '项目中心',
  bible: '人物谱',
  scenes: '场景库',
  episodes: '分集规划',
  script: '剧本台',
  board: '分镜台',
  wall: '评审墙',
  cinema: '成片台',
  monitor: '监制房',
  reader: '原著阅读',
}

interface ContextNames {
  loading: boolean
  project: string | null
  episode: string | null
  shot: string | null
}

export default function ContextChips({
  context,
  onClearShot,
}: {
  context: ContextEnvelope
  onClearShot?: () => void
}) {
  const [names, setNames] = useState<ContextNames>({
    loading: Boolean(context.project_id), project: null, episode: null, shot: null,
  })

  useEffect(() => {
    if (!context.project_id) {
      setNames({ loading: false, project: null, episode: null, shot: null })
      return
    }
    let cancelled = false
    setNames({ loading: true, project: null, episode: null, shot: null })
    api.get(`/projects/${encodeURIComponent(context.project_id)}`)
      .then((project: Project) => {
        if (cancelled) return
        const episode = context.episode_id
          ? (project.episodes ?? []).find(item => item.id === context.episode_id)
          : null
        const shot = context.selected_shot_id
          ? episode?.shots?.find(item => item.id === context.selected_shot_id)
          : null
        const episodeTitle = episode?.title?.replace(/\s+/g, ' ').trim() || ''
        setNames({
          loading: false,
          project: project.name?.replace(/\s+/g, ' ').trim() || '当前项目',
          episode: episode
            ? `第${numToCn(episode.episode_no)}集${episodeTitle ? ` · ${episodeTitle}` : ''}`
            : null,
          shot: shot ? `第${shot.shot_no}镜` : null,
        })
      })
      .catch(() => {
        if (!cancelled) {
          setNames({ loading: false, project: '当前项目', episode: null, shot: null })
        }
      })
    return () => { cancelled = true }
  }, [context.project_id, context.episode_id, context.selected_shot_id])

  const chips: { key: string; label: string; onRemove?: () => void }[] = []
  if (context.project_id) {
    chips.push({ key: 'project', label: `项目 ${names.loading ? '加载中…' : names.project ?? '当前项目'}` })
  }
  if (context.episode_id) {
    chips.push({ key: 'episode', label: `分集 ${names.loading ? '加载中…' : names.episode ?? '当前分集'}` })
  }
  if (context.selected_shot_id) {
    chips.push({
      key: 'shot',
      label: `镜头 ${names.loading ? '加载中…' : names.shot ?? '当前选中'}`,
      onRemove: onClearShot,
    })
  }
  chips.push({ key: 'route', label: `页面 ${PAGE_LABELS[context.route]}` })
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
