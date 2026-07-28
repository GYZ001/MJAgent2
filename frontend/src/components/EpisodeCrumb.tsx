import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { numToCn } from '../api'
import { useNav, useProject } from '../App'
import type { View } from '../App'
import {
  episodeProductionStatus,
  filterEpisodeOptions,
  type EpisodeProductionFilter,
} from '../episodePicker'

interface EpisodeCrumbProps {
  label: string
  view: View
  episodeNo?: number
  showProductionFilters?: boolean
  onBeforeEpisodeChange?: (episodeId: string) => boolean
}

const episodeLabel = (episodeNo: number, title: string) => `第${numToCn(episodeNo)}集 · ${title}`

export default function EpisodeCrumb({ label, view, episodeNo, showProductionFilters = false, onBeforeEpisodeChange }: EpisodeCrumbProps) {
  const { projectId, episodeId, go } = useNav()
  const { data: project, error, loading, refresh } = useProject(projectId!, 0, showProductionFilters ? 'picker_generation' : 'picker')
  const episodes = project?.episodes ?? []
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [productionFilter, setProductionFilter] = useState<EpisodeProductionFilter>('all')
  const [activeIndex, setActiveIndex] = useState(0)
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const listboxId = useId()
  const currentIndex = episodes.findIndex(ep => ep.id === episodeId)
  const current = currentIndex >= 0 ? episodes[currentIndex] : null
  const hasPickerFilter = Boolean(query.trim()) || productionFilter !== 'all'
  const projectHasNoEpisodes = !error && !loading && episodes.length === 0
  const pickerText = current
    ? episodeLabel(current.episode_no, current.title)
    : error
      ? '分集加载失败，点击重试'
      : loading
        ? '正在加载分集…'
        : episodeNo
          ? `第${numToCn(episodeNo)}集`
          : episodes.length
            ? '选择分集'
            : '项目暂无分集'
  const pickerLabel = current
    ? `当前分集：${episodeLabel(current.episode_no, current.title)}；点击搜索或切换`
    : error
      ? '分集加载失败，点击重试'
      : loading
        ? '正在加载分集'
        : episodes.length
          ? '选择分集'
          : '选择分集，暂不可用：项目暂无分集，请先到分集规划创建'

  const closePicker = useCallback((restoreFocus = false) => {
    setOpen(false)
    if (restoreFocus) {
      window.requestAnimationFrame(() => triggerRef.current?.focus())
    }
  }, [])

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
      if (event.key !== 'Escape' || event.defaultPrevented) return
      event.preventDefault()
      closePicker(true)
    }
    document.addEventListener('pointerdown', closeOnOutsidePointer)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('pointerdown', closeOnOutsidePointer)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [closePicker, open])

  const matches = useMemo(() => {
    return filterEpisodeOptions(episodes, query, 60, {
      production: productionFilter,
    })
  }, [episodes, productionFilter, query])

  useEffect(() => {
    if (!open) return
    const selectedIndex = !query.trim() && productionFilter === 'all'
      ? matches.findIndex(ep => ep.id === episodeId)
      : -1
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : 0)
  }, [episodeId, matches, open, productionFilter, query])

  useEffect(() => {
    if (!open) return
    rootRef.current?.querySelector<HTMLElement>(`[data-option-index="${activeIndex}"]`)
      ?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex, open])

  const choose = (id: string) => {
    if (onBeforeEpisodeChange && !onBeforeEpisodeChange(id)) return
    closePicker()
    setQuery('')
    go(view, projectId, id)
  }

  const clearPickerFilters = () => {
    setQuery('')
    setProductionFilter('all')
    setActiveIndex(0)
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
      <span className="crumb-btn episode-workspace-label" aria-current="page">{label}</span>
      <span className="crumb-sep">/</span>
      <button
        className="episode-step"
        type="button"
        aria-label={currentIndex < 0
          ? episodeId
            ? '上一集，当前分集不在项目列表中'
            : '上一集，尚未选择分集'
          : currentIndex > 0
            ? `上一集：${episodeLabel(episodes[currentIndex - 1].episode_no, episodes[currentIndex - 1].title)}`
            : '上一集，当前已是第一集'}
        title={currentIndex < 0 ? episodeId ? '当前分集不在项目列表中' : '尚未选择分集' : currentIndex === 0 ? '当前已是第一集' : '切换到上一集'}
        disabled={currentIndex <= 0}
        onClick={() => currentIndex > 0 && choose(episodes[currentIndex - 1].id)}
      >←</button>
      <div className="episode-picker" ref={rootRef}>
        <button
          ref={triggerRef}
          className="episode-picker-trigger"
          type="button"
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls={open ? listboxId : undefined}
          aria-label={pickerLabel}
          disabled={projectHasNoEpisodes}
          title={projectHasNoEpisodes
            ? '项目暂无分集，请先到分集规划创建'
            : error
              ? '点击重新加载分集'
              : '搜索或切换分集'}
          onClick={() => { void toggle() }}
        >
          <span>{pickerText}</span>
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
                } else if (event.key === 'Home') {
                  event.preventDefault()
                  setActiveIndex(0)
                } else if (event.key === 'End') {
                  event.preventDefault()
                  setActiveIndex(Math.max(matches.length - 1, 0))
                } else if (event.key === 'Enter' && matches[activeIndex]) {
                  event.preventDefault()
                  choose(matches[activeIndex].id)
                } else if (event.key === 'Escape') {
                  event.preventDefault()
                  event.stopPropagation()
                  closePicker(true)
                }
              }}
              placeholder="搜索集数或标题"
              aria-label="搜索分集"
              aria-controls={listboxId}
              aria-activedescendant={matches[activeIndex] ? `episode-option-${matches[activeIndex].id}` : undefined}
            />
            {showProductionFilters && (
              <div className="episode-picker-filters">
                <label>制作状态
                  <select value={productionFilter} onChange={event => setProductionFilter(event.target.value as EpisodeProductionFilter)}>
                    <option value="all">全部</option><option value="with_video">有视频</option>
                    <option value="pending_adoption">待采纳</option><option value="failed">有失败</option>
                    <option value="unproduced">未制作</option>
                  </select>
                </label>
              </div>
            )}
            <div id={listboxId} className="episode-picker-results" role="listbox" aria-label="分集列表">
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
                  <span>{ep.title}{showProductionFilters && <small>{ep.shot_count ?? 0} 镜 · {ep.video_count ?? 0} 已采用{ep.pending_adoption_count ? ` · ${ep.pending_adoption_count} 待采纳` : ''}{ep.failed_count ? ` · ${ep.failed_count} 失败` : ''}</small>}</span>
                  <i className={`episode-production-status status-${ep.status || 'pending'}`}>{episodeProductionStatus(ep)}</i>
                </button>
              ))}
            </div>
            {!matches.length && (
              <div className="episode-picker-empty" role="status">
                <span>{query.trim() ? `没有匹配“${query.trim()}”的分集` : '当前筛选下没有分集'}</span>
                {hasPickerFilter && (
                  <button type="button" onClick={clearPickerFilters}>清除搜索与筛选</button>
                )}
              </div>
            )}
            <div className="episode-picker-foot">共 {episodes.length} 集 · 最多展示 60 条搜索结果</div>
          </div>
        )}
      </div>
      <button
        className="episode-step"
        type="button"
        aria-label={currentIndex >= 0 && currentIndex < episodes.length - 1
          ? `下一集：${episodeLabel(episodes[currentIndex + 1].episode_no, episodes[currentIndex + 1].title)}`
          : currentIndex < 0
            ? episodeId
              ? '下一集，当前分集不在项目列表中'
              : '下一集，尚未选择分集'
            : '下一集，当前已是最后一集'}
        title={currentIndex < 0
          ? episodeId ? '当前分集不在项目列表中' : '尚未选择分集'
          : currentIndex >= episodes.length - 1
            ? '当前已是最后一集'
            : '切换到下一集'}
        disabled={currentIndex < 0 || currentIndex >= episodes.length - 1}
        onClick={() => currentIndex >= 0 && currentIndex < episodes.length - 1 && choose(episodes[currentIndex + 1].id)}
      >→</button>
    </div>
  )
}
