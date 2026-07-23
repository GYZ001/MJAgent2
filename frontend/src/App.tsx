import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { api, Episode, Project } from './api'
import Studio from './pages/Studio'
import BiblePage from './pages/BiblePage'
import ScenesPage from './pages/ScenesPage'
import EpisodesPage from './pages/EpisodesPage'
import ScriptPage from './pages/ScriptPage'
import BoardPage from './pages/BoardPage'
import WallPage from './pages/WallPage'
import CinemaPage from './pages/CinemaPage'
import MonitorPage from './pages/MonitorPage'
import ReaderPage from './pages/ReaderPage'
import RunDock from './components/harness/RunDock'

export type View = 'studio' | 'bible' | 'scenes' | 'episodes' | 'script' | 'board' | 'wall' | 'cinema' | 'monitor' | 'reader'

interface Nav {
  view: View
  projectId: string | null
  episodeId: string | null
  chapterIdx: number | null
  go: (v: View, projectId?: string | null, episodeId?: string | null, chapterIdx?: number | null) => void
  toast: (msg: string, isErr?: boolean) => void
}

const NavCtx = createContext<Nav>(null as unknown as Nav)
export const useNav = () => useContext(NavCtx)

const SECTIONS: { key: View; label: string; icon: string; group: string; needProject?: boolean; needEpisode?: boolean }[] = [
  { key: 'studio', label: '项目中心', icon: '书', group: '项目' },
  { key: 'bible', label: '人物谱', icon: '人', group: '前期准备', needProject: true },
  { key: 'scenes', label: '场景库', icon: '景', group: '前期准备', needProject: true },
  { key: 'episodes', label: '分集规划', icon: '集', group: '前期准备', needProject: true },
  { key: 'script', label: '剧本台', icon: '剧', group: '内容制作', needEpisode: true },
  { key: 'board', label: '分镜台', icon: '镜', group: '内容制作', needEpisode: true },
  { key: 'wall', label: '评审墙', icon: '审', group: '质量交付', needEpisode: true },
  { key: 'cinema', label: '成片台', icon: '片', group: '质量交付', needEpisode: true },
  { key: 'monitor', label: '监制房', icon: '控', group: '系统' },
]

const decodePart = (value?: string) => value ? decodeURIComponent(value) : null

function readLocation(): Pick<Nav, 'view' | 'projectId' | 'episodeId' | 'chapterIdx'> {
  const parts = window.location.pathname.split('/').filter(Boolean)
  if (parts[0] === 'monitor') return { view: 'monitor', projectId: null, episodeId: null, chapterIdx: null }
  if (parts[0] !== 'projects' || !parts[1]) {
    return { view: 'studio', projectId: null, episodeId: null, chapterIdx: null }
  }
  const projectId = decodePart(parts[1])
  if (parts[2] === 'reader') {
    const idx = Number(parts[3])
    return { view: 'reader', projectId, episodeId: null, chapterIdx: Number.isFinite(idx) ? idx : 1 }
  }
  if (parts[2] === 'episodes' && parts[3]) {
    const episodeId = decodePart(parts[3])
    const page = parts[4]
    const view: View = page === 'board' || page === 'wall' || page === 'cinema' ? page : 'script'
    return { view, projectId, episodeId, chapterIdx: null }
  }
  const view: View = parts[2] === 'scenes' || parts[2] === 'episodes' || parts[2] === 'bible'
    ? parts[2] : 'bible'
  return { view, projectId, episodeId: null, chapterIdx: null }
}

function locationFor(view: View, projectId: string | null, episodeId: string | null, chapterIdx: number | null) {
  if (view === 'studio') return '/'
  if (view === 'monitor') return '/monitor'
  if (!projectId) return '/'
  const project = `/projects/${encodeURIComponent(projectId)}`
  if (view === 'reader') return `${project}/reader/${chapterIdx ?? 1}`
  if (view === 'script' || view === 'board' || view === 'wall' || view === 'cinema') {
    return episodeId ? `${project}/episodes/${encodeURIComponent(episodeId)}/${view}` : `${project}/episodes`
  }
  return `${project}/${view}`
}

export default function App() {
  const initial = readLocation()
  const [view, setView] = useState<View>(initial.view)
  const [projectId, setProjectId] = useState<string | null>(initial.projectId)
  const [episodeId, setEpisodeId] = useState<string | null>(initial.episodeId)
  const [chapterIdx, setChapterIdx] = useState<number | null>(initial.chapterIdx)
  const [toastMsg, setToastMsg] = useState<{ text: string; err: boolean } | null>(null)
  const [spineCollapsed, setSpineCollapsed] = useState(false)
  const [mobileNavOpen, setMobileNavOpen] = useState(false)

  const toast = useCallback((text: string, isErr = false) => {
    setToastMsg({ text, err: isErr })
    window.setTimeout(() => setToastMsg(null), isErr ? 8000 : 3000)
  }, [])

  const go = useCallback((v: View, pid?: string | null, eid?: string | null, cidx?: number | null) => {
    const nextProjectId = pid === undefined ? projectId : pid
    const nextEpisodeId = eid === undefined ? episodeId : eid
    const nextChapterIdx = cidx === undefined ? chapterIdx : cidx
    const target = locationFor(v, nextProjectId, nextEpisodeId, nextChapterIdx)
    if (`${window.location.pathname}${window.location.search}` !== target) {
      window.history.pushState({}, '', target)
    }
    setProjectId(nextProjectId)
    setEpisodeId(nextEpisodeId)
    setChapterIdx(nextChapterIdx)
    setView(v)
    setMobileNavOpen(false)
    window.scrollTo({ top: 0, behavior: 'auto' })
  }, [chapterIdx, episodeId, projectId])

  useEffect(() => {
    const onPopState = () => {
      const next = readLocation()
      setView(next.view)
      setProjectId(next.projectId)
      setEpisodeId(next.episodeId)
      setChapterIdx(next.chapterIdx)
      setMobileNavOpen(false)
      window.scrollTo({ top: 0, behavior: 'auto' })
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  useEffect(() => {
    if (!projectId) {
      setEpisodeId(null)
      return
    }
    let cancelled = false
    api.get(`/projects/${projectId}`)
      .then((project: Project) => {
        if (cancelled) return
        const episodes = project.episodes ?? []
        if (!episodes.length) {
          setEpisodeId(null)
          return
        }
        setEpisodeId(current =>
          current && episodes.some(ep => ep.id === current) ? current : episodes[0].id
        )
      })
      .catch(() => {
        if (!cancelled) setEpisodeId(null)
      })
    return () => { cancelled = true }
  }, [projectId])

  const nav: Nav = { view, projectId, episodeId, chapterIdx, go, toast }
  const visibleSections = projectId ? SECTIONS : SECTIONS.filter(s => s.key === 'studio' || s.key === 'monitor')

  const openSection = (s: (typeof SECTIONS)[number]) => go(s.key)

  const groupedSections = visibleSections.reduce<Record<string, typeof visibleSections>>((groups, section) => {
    ;(groups[section.group] ??= []).push(section)
    return groups
  }, {})

  return (
    <NavCtx.Provider value={nav}>
      <button className="mobile-nav-trigger" type="button" aria-label="打开导航" onClick={() => setMobileNavOpen(true)}>☰</button>
      {mobileNavOpen && <button className="mobile-nav-backdrop" type="button" aria-label="关闭导航" onClick={() => setMobileNavOpen(false)} />}
      <aside className={`spine ${spineCollapsed ? 'collapsed' : ''} ${mobileNavOpen ? 'mobile-open' : ''}`}>
        <div className="spine-top">
          <button
            className="seal"
            type="button"
            aria-label={spineCollapsed ? '展开菜单栏' : '隐藏菜单栏'}
            aria-expanded={!spineCollapsed}
            onClick={() => setSpineCollapsed(v => !v)}
          >
            漫
          </button>
          <div className="brand-copy"><b>漫剧案头</b><span>AI PRODUCTION</span></div>
          <button className="spine-close" type="button" aria-label="关闭导航" onClick={() => setMobileNavOpen(false)}>×</button>
        </div>
        <nav>
          {Object.entries(groupedSections).map(([group, sections]) => (
            <div className="spine-group" key={group}>
              <div className="spine-group-label">{group}</div>
              {sections.map(s => (
                <button
                  key={s.key}
                  className={`spine-item ${view === s.key ? 'active' : ''}`}
                  onClick={() => openSection(s)}
                  title={spineCollapsed ? s.label : undefined}
                >
                  <span className="spine-icon" aria-hidden="true">{s.icon}</span>
                  <span className="spine-label">{s.label}</span>
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="spine-foot">MANJU STUDIO · 2.0</div>
      </aside>
      <main className="desk">
        {view === 'studio' && <Studio />}
        {view === 'bible' && projectId && <BiblePage key={projectId} />}
        {view === 'scenes' && projectId && <ScenesPage key={projectId} />}
        {view === 'episodes' && projectId && <EpisodesPage key={projectId} />}
        {view === 'reader' && projectId && <ReaderPage key={projectId} />}
        {view === 'script' && (episodeId ? <ScriptPage key={episodeId} /> : <WorkspaceEmpty label="剧本台" />)}
        {view === 'board' && (episodeId ? <BoardPage key={episodeId} /> : <WorkspaceEmpty label="分镜台" />)}
        {view === 'wall' && (episodeId ? <WallPage key={episodeId} /> : <WorkspaceEmpty label="评审墙" />)}
        {view === 'cinema' && (episodeId ? <CinemaPage key={episodeId} /> : <WorkspaceEmpty label="成片台" />)}
        {view === 'monitor' && <MonitorPage />}
      </main>
      <RunDock projectId={projectId} onOpen={() => go('monitor')} />
      {toastMsg && <div role="status" className={`toast ${toastMsg.err ? 'err' : ''}`}>{toastMsg.text}</div>}
    </NavCtx.Provider>
  )
}

function WorkspaceEmpty({ label }: { label: string }) {
  return (
    <>
      <header className="desk-head">
        <div className="crumb crumb-switch">
          <button className="crumb-btn" type="button">{label}</button>
          <span className="crumb-sep">/</span>
          <select className="episode-switch" aria-label="切换当前分集" value="" disabled />
        </div>
        <h1>{label} <span className="sub">当前项目还没有可进入的分集</span></h1>
        <hr className="rule" />
      </header>
      <div className="empty"><div className="big">集</div>暂无分集<br />可先到分集台生成分集</div>
    </>
  )
}

/** 轮询某资源；interval=0 或函数返回 0 不轮询。intervalMs 传函数时可按最新数据动态调间隔
 *  （例如只在有运行态任务时高频轮询，空闲时不轮询），避免反复拉取大 payload 拖垮页面。 */
export function usePoll<T>(
  fetcher: () => Promise<T>,
  intervalMs: number | ((data: T | null) => number),
  deps: unknown[] = [],
) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const fetcherRef = useRef(fetcher); fetcherRef.current = fetcher
  const intervalRef = useRef(intervalMs); intervalRef.current = intervalMs
  const dataRef = useRef<T | null>(null); dataRef.current = data
  const refresh = useCallback(() => {
    fetcherRef.current()
      .then(d => { setData(d); dataRef.current = d; setError(null) })
      .catch(e => setError(String(e.message || e)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  useEffect(() => {
    if (deps.some(d => d == null)) return
    refresh()
    let timer: number | undefined
    const tick = () => {
      const ms = typeof intervalRef.current === 'function'
        ? intervalRef.current(dataRef.current)
        : intervalRef.current
      if (ms > 0) {
        timer = window.setTimeout(async () => {
          await refresh()
          tick()
        }, ms)
      }
    }
    tick()
    return () => { if (timer) window.clearTimeout(timer) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refresh])
  return { data, error, refresh }
}

export const useProject = (projectId: string, intervalMs = 4000) =>
  usePoll<Project>(() => api.get(`/projects/${projectId}`), intervalMs, [projectId])

/** 分集是否处于运行态（编剧/分镜/生成中）—— 决定是否需要高频轮询。
 *  空闲时彻底停轮询，避免反复拉取 1MB+ 的分集 payload 拖垮页面。 */
const episodeBusy = (ep: Episode | null): boolean => {
  if (!ep) return true  // 首次未拿到数据时，按可能忙碌处理触发首次拉取后的轮询
  if (ep.screenplay_status === 'running') return true
  if (ep.status === 'scripting' || ep.status === 'drafting') return true
  if (ep.shots?.some(s => s.scene_status === 'generating')) return true
  return false
}

export const useEpisode = (
  episodeId: string,
  intervalMs: number | ((ep: Episode | null) => number) = (ep) => episodeBusy(ep) ? 5000 : 0,
) =>
  usePoll<Episode>(() => api.get(`/episodes/${episodeId}`), intervalMs, [episodeId])
