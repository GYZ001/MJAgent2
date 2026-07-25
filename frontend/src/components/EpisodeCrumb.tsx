import { useEffect, useMemo, useRef, useState } from 'react'
import { numToCn } from '../api'
import { useNav, useProject } from '../App'
import type { View } from '../App'
import { filterEpisodeOptions } from '../episodePicker'

interface EpisodeCrumbProps {
  label: string
  view: View
  episodeNo?: number
}

const episodeLabel = (episodeNo: number, title: string) => `第${numToCn(episodeNo)}集 · ${title}`

export default function EpisodeCrumb({ label, view, episodeNo }: EpisodeCrumbProps) {
  const { projectId, episodeId, go } = useNav()
  const { data: project, error, loading, refresh } = useProject(projectId!, 0, 'picker')
  const episodes = project?.episodes ?? []
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const rootRef = useRef<HTMLDivElement>(null)
  const currentIndex = episodes.findIndex(ep => ep.id === episodeId)
  const current = currentIndex >= 0 ? episodes[currentIndex] : null

  useEffect(() => {
    setOpen(false)
    setQuery('')
  }, [episodeId])

  useEffect(() => {
    if (!open) return
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node | null)) setOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', closeOnOutsidePointer)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('pointerdown', closeOnOutsidePointer)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [open])

  const matches = useMemo(() => {
    return filterEpisodeOptions(episodes, query)
  }, [episodes, query])

  const choose = (id: string) => {
    setOpen(false)
    setQuery('')
    go(view, projectId, id)
  }

  const toggle = async () => {
    if (episodes.length) {
      setOpen(value => !value)
      return
    }
    if (!error) return
    const next = await refresh()
    if (next?.episodes?.length) setOpen(true)
  }

  return (
    <div className="episode-crumb">
      <button className="crumb-btn" type="button" onClick={() => go(view, projectId, episodeId)}>{label}</button>
      <span className="crumb-sep">/</span>
      <button
        className="episode-step"
        type="button"
        aria-label="上一集"
        disabled={currentIndex <= 0}
        onClick={() => currentIndex > 0 && choose(episodes[currentIndex - 1].id)}
      >←</button>
      <div className="episode-picker" ref={rootRef}>
        <button
          className="episode-picker-trigger"
          type="button"
          aria-haspopup="listbox"
          aria-expanded={open}
          disabled={!error && !loading && !episodes.length}
          onClick={() => { void toggle() }}
        >
          <span>{current
            ? episodeLabel(current.episode_no, current.title)
            : error
              ? '分集加载失败，点击重试'
              : loading
                ? '正在加载分集…'
                : episodeNo
                  ? `第${numToCn(episodeNo)}集`
                  : '暂无分集'}</span>
          <i aria-hidden="true">⌄</i>
        </button>
        {open && (
          <div className="episode-picker-popover">
            <input
              autoFocus
              type="search"
              value={query}
              onChange={event => setQuery(event.target.value)}
              placeholder="搜索集数或标题"
              aria-label="搜索分集"
            />
            <div className="episode-picker-results" role="listbox" aria-label="分集列表">
              {matches.map(ep => (
                <button
                  key={ep.id}
                  type="button"
                  role="option"
                  aria-selected={ep.id === episodeId}
                  className={ep.id === episodeId ? 'selected' : ''}
                  onClick={() => choose(ep.id)}
                >
                  <b>第{numToCn(ep.episode_no)}集</b>
                  <span>{ep.title}</span>
                </button>
              ))}
              {!matches.length && <div className="episode-picker-empty">没有匹配的分集</div>}
            </div>
            <div className="episode-picker-foot">共 {episodes.length} 集 · 最多展示 60 条搜索结果</div>
          </div>
        )}
      </div>
      <button
        className="episode-step"
        type="button"
        aria-label="下一集"
        disabled={currentIndex < 0 || currentIndex >= episodes.length - 1}
        onClick={() => currentIndex >= 0 && currentIndex < episodes.length - 1 && choose(episodes[currentIndex + 1].id)}
      >→</button>
    </div>
  )
}
