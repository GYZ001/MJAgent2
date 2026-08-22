import { useEffect, useState } from 'react'
import { api, type Project } from '../api'
import type { AgentView, ContextEnvelope } from './types'

const PAGE_LABELS: Record<AgentView, string> = {
  studio: '项目空间',
  bible: '人物谱',
  scenes: '场景库',
  episodes: '分集规划',
  script: '剧本台',
  board: '分镜台',
  wall: '生成台',
  cinema: '成片台',
  monitor: '监制房',
  observability: '观测台',
  system: '系统设置',
  reader: '原著阅读',
}

/**
 * Agent 上下文标签只需要三个名字：项目名、分集名、镜头号。
 *
 * 这里曾经直接拉整份项目投影：千集项目 4.8 MB / 1616 集（每集还带镜头），
 * 而 picker 投影用同一套服务端逻辑给出项目名与当前分集，只有 1.4 KB。
 * 镜头号只在真的选中了镜头时才需要，那时才按分集取一次分镜投影。
 */
export function agentContextRequestPaths(context: {
  project_id?: string | null
  episode_id?: string | null
  selected_shot_id?: string | null
}): { project: string; shot: string | null } {
  const project =
    `/projects/${encodeURIComponent(String(context.project_id ?? ''))}?view=picker`
    + `&episode_limit=1`
    + (context.episode_id ? `&episode_cursor=${encodeURIComponent(context.episode_id)}` : '')
  const shot = context.selected_shot_id && context.episode_id
    ? `/episodes/${encodeURIComponent(context.episode_id)}?view=board`
    : null
  return { project, shot }
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
    const paths = agentContextRequestPaths(context)
    const shotPromise: Promise<string | null> = paths.shot
      ? api.get(paths.shot)
          .then((episode: { shots?: { id: string; shot_no: number }[] }) => {
            const shot = (episode.shots ?? [])
              .find(item => item.id === context.selected_shot_id)
            return shot ? `第${shot.shot_no}镜` : null
          })
          .catch(() => null)
      : Promise.resolve(null)
    Promise.all([api.get(paths.project) as Promise<Project>, shotPromise])
      .then(([project, shot]) => {
        if (cancelled) return
        const current = project.episode_current
        const episodeTitle = current?.title?.replace(/\s+/g, ' ').trim() || ''
        setNames({
          loading: false,
          project: project.name?.replace(/\s+/g, ' ').trim() || '当前项目',
          episode: context.episode_id && current ? episodeTitle || '当前章节' : null,
          shot,
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
    chips.push({ key: 'project', label: names.loading ? '加载中…' : names.project ?? '当前项目' })
  }
  if (context.episode_id) {
    chips.push({ key: 'episode', label: names.loading ? '加载中…' : names.episode ?? '当前章节' })
  }
  if (context.selected_shot_id) {
    chips.push({
      key: 'shot',
      label: `镜头 ${names.loading ? '加载中…' : names.shot ?? '当前选中'}`,
      onRemove: onClearShot,
    })
  }
  chips.push({ key: 'route', label: PAGE_LABELS[context.route] })
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
