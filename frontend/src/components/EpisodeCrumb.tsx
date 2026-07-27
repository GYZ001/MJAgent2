import { useEffect, useMemo, useRef, useState } from 'react'
import { numToCn } from '../api'
import { useNav, useProject } from '../App'
import type { View } from '../App'
import { filterEpisodeOptions, type EpisodeProductionFilter, type EpisodeReviewFilter } from '../episodePicker'

interface EpisodeCrumbProps {
  label: string
  view: View
  episodeNo?: number
  showReviewFilters?: boolean
  onBeforeEpisodeChange?: (episodeId: string) => boolean
}

const episodeLabel = (episodeNo: number, title: string) => `第${numToCn(episodeNo)}集 · ${title}`

function productionStatus(ep: { status?: string; screenplay_status?: string }): string {
  if (ep.screenplay_status !== 'ready') return ep.screenplay_status === 'running' ? '剧本中' : '待剧本'
  if (ep.status === 'scripting') return '分镜中'
  if (ep.status === 'script_failed') return '需处理'
  if (['confirmed', 'generating', 'done'].includes(ep.status || '')) return '已确认'
  if (ep.status === 'scripted') return '待确认'
  return '待分镜'
}

export default function EpisodeCrumb({ label, view, episodeNo, showReviewFilters = false, onBeforeEpisodeChange }: EpisodeCrumbProps) {
  const { projectId, episodeId, go } = useNav()
  const { data: project, error, loading, refresh } = useProject(projectId!, 0, showReviewFilters ? 'picker_review' : 'picker')
  const episodes = project?.episodes ?? []
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [productionFilter, setProductionFilter] = useState<EpisodeProductionFilter>('all')
  const [reviewFilter, setReviewFilter] = useState<EpisodeReviewFilter>('all')
  const [activeIndex, setActiveIndex] = useState(0)
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
    return filterEpisodeOptions(episodes, query, 60, {
      production: productionFilter,
      review: reviewFilter,
    })
  }, [episodes, productionFilter, query, reviewFilter])

  useEffect(() => {
    setActiveIndex(0)
  }, [query, open])

  useEffect(() => {
    if (!open) return
    rootRef.current?.querySelector<HTMLElement>(`[data-option-index="${activeIndex}"]`)
      ?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex, open])

  const choose = (id: string) => {
    if (onBeforeEpisodeChange && !onBeforeEpisodeChange(id)) return
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
              onKeyDown={event => {
                if (event.key === 'ArrowDown') {
                  event.preventDefault()
                  setActiveIndex(value => Math.min(value + 1, Math.max(matches.length - 1, 0)))
                } else if (event.key === 'ArrowUp') {
                  event.preventDefault()
                  setActiveIndex(value => Math.max(value - 1, 0))
                } else if (event.key === 'Enter' && matches[activeIndex]) {
                  event.preventDefault()
                  choose(matches[activeIndex].id)
                } else if (event.key === 'Escape') {
                  event.preventDefault()
                  setOpen(false)
                }
              }}
              placeholder="搜索集数或标题"
              aria-label="搜索分集"
              aria-controls="episode-picker-listbox"
              aria-activedescendant={matches[activeIndex] ? `episode-option-${matches[activeIndex].id}` : undefined}
            />
            {showReviewFilters && (
              <div className="episode-picker-filters">
                <label>制作状态
                  <select value={productionFilter} onChange={event => setProductionFilter(event.target.value as EpisodeProductionFilter)}>
                    <option value="all">全部</option><option value="with_video">有视频</option>
                    <option value="pending_adoption">待采纳</option><option value="failed">有失败</option>
                    <option value="unproduced">未制作</option>
                  </select>
                </label>
                <label>评审状态
                  <select value={reviewFilter} onChange={event => setReviewFilter(event.target.value as EpisodeReviewFilter)}>
                    <option value="all">全部</option><option value="problem">有问题</option>
                    <option value="unreviewed">未评完</option><option value="completed">已评完</option>
                  </select>
                </label>
              </div>
            )}
            <div id="episode-picker-listbox" className="episode-picker-results" role="listbox" aria-label="分集列表">
              {matches.map((ep, index) => (
                <button
                  key={ep.id}
                  id={`episode-option-${ep.id}`}
                  data-option-index={index}
                  type="button"
                  role="option"
                  aria-selected={ep.id === episodeId}
                  className={`${ep.id === episodeId ? 'selected ' : ''}${index === activeIndex ? 'active' : ''}`.trim()}
                  onMouseEnter={() => setActiveIndex(index)}
                  onClick={() => choose(ep.id)}
                >
                  <b>第{numToCn(ep.episode_no)}集</b>
                  <span>{ep.title}{showReviewFilters && <small>{ep.shot_count ?? 0} 镜 · {ep.video_count ?? 0} 已采用{ep.pending_adoption_count ? ` · ${ep.pending_adoption_count} 待采纳` : ''}{ep.failed_count ? ` · ${ep.failed_count} 失败` : ''}</small>}</span>
                  <i className={`episode-production-status status-${(ep as { status?: string }).status || 'pending'}`}>{productionStatus(ep as { status?: string; screenplay_status?: string })}</i>
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
