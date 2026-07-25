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
import AgentDrawer from './agent/AgentDrawer'
import type { ContextEnvelope } from './agent/types'
import CapabilityApprovalHost from './components/CapabilityApprovalHost'
import EpisodeCrumb from './components/EpisodeCrumb'
import { useScrollContainment } from './useScrollContainment'
import { AdaptivePoller, type PollInterval } from './adaptivePoller'
import { resolveEpisodeId } from './episodePicker'

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

const SECTIONS: { key: View; label: string; icon: string; group: string; needProject?: boolean; needEpisode?: boolean; matchViews?: View[] }[] = [
  { key: 'studio', label: '项目中心', icon: '书', group: '项目' },
  { key: 'bible', label: '前期准备', icon: '备', group: '前期准备', needProject: true, matchViews: ['bible', 'scenes', 'episodes'] },
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
  const [agentOpen, setAgentOpen] = useState(false)
  const [agentEnabled, setAgentEnabled] = useState(true)
  const toastTimerRef = useRef<number>()
  const spineRef = useRef<HTMLElement | null>(null)
  useScrollContainment(spineRef, true)

  useEffect(() => {
    api.get('/settings')
      .then((settings: Record<string, string>) => {
        const raw = String(settings.agent_enabled ?? 'true').trim().toLowerCase()
        setAgentEnabled(['1', 'true', 'yes', 'on'].includes(raw))
      })
      .catch(() => setAgentEnabled(true))
  }, [])

  const toast = useCallback((text: string, isErr = false) => {
    setToastMsg({ text, err: isErr })
    if (toastTimerRef.current) window.clearTimeout(toastTimerRef.current)
    toastTimerRef.current = window.setTimeout(() => setToastMsg(null), isErr ? 8000 : 3000)
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
    const frame = window.requestAnimationFrame(() => {
      window.scrollTo({ top: 0, left: 0, behavior: 'auto' })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [view, projectId, episodeId, chapterIdx])

  useEffect(() => {
    const root = document.getElementById('root')
    root?.classList.toggle('agent-open', agentOpen)
    return () => { root?.classList.remove('agent-open') }
  }, [agentOpen])

  useEffect(() => {
    if (!projectId) {
      setEpisodeId(null)
      return
    }
    let cancelled = false
    api.get(`/projects/${projectId}?view=picker`)
      .then((project: Project) => {
        if (cancelled) return
        const episodes = project.episodes ?? []
        if (!episodes.length) {
          setEpisodeId(null)
          return
        }
        setEpisodeId(current => resolveEpisodeId(episodes, current))
      })
      .catch(() => {
        // 临时请求失败不能等同于“项目没有分集”。保留当前选择，侧栏进入工作台时会重试。
      })
    return () => { cancelled = true }
  }, [projectId])

  const nav: Nav = { view, projectId, episodeId, chapterIdx, go, toast }
  const visibleSections = projectId ? SECTIONS : SECTIONS.filter(s => s.key === 'studio' || s.key === 'monitor')
  const agentContext: ContextEnvelope = {
    route: view,
    project_id: projectId,
    episode_id: episodeId,
    unsaved_draft: false,
  }

  const openSection = async (s: (typeof SECTIONS)[number]) => {
    if (!s.needEpisode || !projectId) {
      go(s.key)
      return
    }

    try {
      // 分集可能在项目打开后才生成，进入制作工作台前必须重新读取，不能依赖首次加载的快照。
      const project = await api.get(`/projects/${projectId}?view=picker`) as Project
      const nextEpisodeId = resolveEpisodeId(project.episodes ?? [], episodeId)
      setEpisodeId(nextEpisodeId)
      go(s.key, projectId, nextEpisodeId)
    } catch (error) {
      toast(`读取分集失败：${String((error as Error).message || error)}`, true)
      go(s.key)
    }
  }

  const groupedSections = visibleSections.reduce<Record<string, typeof visibleSections>>((groups, section) => {
    ;(groups[section.group] ??= []).push(section)
    return groups
  }, {})

  return (
    <NavCtx.Provider value={nav}>
      <button className="mobile-nav-trigger" type="button" aria-label="打开导航" onClick={() => setMobileNavOpen(true)}>☰</button>
      {mobileNavOpen && <button className="mobile-nav-backdrop" type="button" aria-label="关闭导航" onClick={() => setMobileNavOpen(false)} />}
      <aside
        ref={spineRef}
        className={`spine ${spineCollapsed ? 'collapsed' : ''} ${mobileNavOpen ? 'mobile-open' : ''}`}
      >
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
              {sections.map(s => {
                const active = (s.matchViews ? s.matchViews.includes(view) : view === s.key)
                return (
                <button
                  key={s.key}
                  className={`spine-item ${active ? 'active' : ''}`}
                  onClick={() => { void openSection(s) }}
                  title={spineCollapsed ? s.label : undefined}
                >
                  <span className="spine-icon" aria-hidden="true">{s.icon}</span>
                  <span className="spine-label">{s.label}</span>
                </button>
                )
              })}
            </div>
          ))}
        </nav>
        <div className="spine-foot">MANJU STUDIO · 2.0</div>
      </aside>
      <main className={`desk ${view === 'board' ? 'board-desk' : ''}`}>
        {view === 'studio' && <Studio />}
        {view === 'bible' && projectId && <BiblePage key={projectId} />}
        {view === 'scenes' && projectId && <ScenesPage key={projectId} />}
        {view === 'episodes' && projectId && <EpisodesPage key={projectId} />}
        {view === 'reader' && projectId && <ReaderPage key={projectId} />}
        {view === 'script' && (episodeId ? <ScriptPage key={episodeId} /> : <WorkspaceEmpty label="剧本台" view="script" />)}
        {view === 'board' && (episodeId ? <BoardPage key={episodeId} /> : <WorkspaceEmpty label="分镜台" view="board" />)}
        {view === 'wall' && (episodeId ? <WallPage key={episodeId} /> : <WorkspaceEmpty label="评审墙" view="wall" />)}
        {view === 'cinema' && (episodeId ? <CinemaPage key={episodeId} /> : <WorkspaceEmpty label="成片台" view="cinema" />)}
        {view === 'monitor' && <MonitorPage />}
      </main>
      <RunDock projectId={projectId} onOpen={() => go('monitor')} />
      {agentEnabled && !agentOpen && (
        <button
          type="button"
          className="agent-toggle"
          aria-label="打开案头助手"
          aria-expanded={false}
          aria-controls="agent-drawer"
          title="打开案头助手"
          onClick={() => setAgentOpen(true)}
        >
          <svg className="agent-toggle-icon" viewBox="0 0 22 18" aria-hidden="true" focusable="false">
            <rect x="1.25" y="1.25" width="19.5" height="15.5" rx="2.2" ry="2.2" fill="none" stroke="currentColor" strokeWidth="1.5" />
            <path d="M15.25 1.25v15.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
          </svg>
        </button>
      )}
      {agentEnabled && (
        <AgentDrawer open={agentOpen} onClose={() => setAgentOpen(false)} context={agentContext} />
      )}
      <CapabilityApprovalHost />
      {toastMsg && <div role="status" className={`toast ${toastMsg.err ? 'err' : ''}`}>{toastMsg.text}</div>}
    </NavCtx.Provider>
  )
}

function WorkspaceEmpty({ label, view }: { label: string; view: View }) {
  return (
    <>
      <header className="desk-head">
        <EpisodeCrumb label={label} view={view} />
        <h1>{label} <span className="sub">当前项目还没有可进入的分集</span></h1>
        <hr className="rule" />
      </header>
      <div className="empty"><div className="big">集</div>暂无分集<br />可先到分集台生成分集</div>
    </>
  )
}

/** 轮询某资源；interval=0 或函数返回 0 不轮询。intervalMs 传函数时可按最新数据动态调间隔。
 *  手动 refresh 会重新唤醒并计算轮询间隔，覆盖 idle → running 的异步任务状态切换。
 *  内置单飞、卸载后响应保护；页面重新获得焦点时立即追平一次后端状态。 */
export function usePoll<T>(
  fetcher: () => Promise<T>,
  intervalMs: PollInterval<T>,
  deps: unknown[] = [],
) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const pollerRef = useRef<AdaptivePoller<T>>()
  if (!pollerRef.current) {
    pollerRef.current = new AdaptivePoller(fetcher, intervalMs, {
      onData: next => {
        setData(next)
        setError(null)
        setLoading(false)
      },
      onError: (e: unknown) => {
        setError(String((e as Error).message || e))
        setLoading(false)
      },
    })
  }
  pollerRef.current.update(fetcher, intervalMs, {
    onData: next => {
      setData(next)
      setError(null)
      setLoading(false)
    },
    onError: (e: unknown) => {
      setError(String((e as Error).message || e))
      setLoading(false)
    },
  })

  const refresh = useCallback(
    (): Promise<T | null> => pollerRef.current!.refresh(),
    [],
  )

  useEffect(() => {
    if (deps.some(d => d == null)) return
    const poller = pollerRef.current!
    void poller.start()
    const catchUp = () => {
      if (document.visibilityState === 'visible') void poller.refresh()
    }
    window.addEventListener('focus', catchUp)
    document.addEventListener('visibilitychange', catchUp)
    return () => {
      window.removeEventListener('focus', catchUp)
      document.removeEventListener('visibilitychange', catchUp)
      poller.stop()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, error, loading, refresh }
}

/** 项目是否处于运行态——空闲时停轮询，避免反复拉取数 MB 的项目 payload。 */
const projectBusy = (p: Project | null): boolean => {
  if (!p) return true
  if (p.bible_status === 'running' || p.plan_status === 'running') return true
  if (p.refs_status === 'running' || p.scene_refs_status === 'running') return true
  if (p.episodes?.some(ep =>
    ep.screenplay_status === 'running'
    || ep.status === 'scripting'
    || ep.status === 'generating'
  )) return true
  return false
}

export const useProject = (
  projectId: string,
  intervalMs: PollInterval<Project> = (p) => projectBusy(p) ? 3000 : 0,
  view?: 'bible' | 'scenes' | 'episodes' | 'picker',
) =>
  usePoll<Project>(
    () => api.get(`/projects/${projectId}${view ? `?view=${view}` : ''}`),
    intervalMs,
    [projectId, view],
  )

/** 分集是否处于运行态（编剧/分镜/参考图视频）—— 决定是否需要高频轮询。
 *  空闲时彻底停轮询，避免反复拉取 1MB+ 的分集 payload 拖垮页面。 */
const episodeBusy = (ep: Episode | null): boolean => {
  if (!ep) return true  // 首次未拿到数据时，按可能忙碌处理触发首次拉取后的轮询
  if (ep.screenplay_status === 'running') return true
  if (ep.status === 'scripting' || ep.status === 'drafting' || ep.status === 'generating') return true
  if (ep.shots?.some(s =>
    s.versions?.some(v =>
      v.status === 'queued' || v.status === 'running' || v.status === 'waiting_provider'
    ) || (s.pipeline != null && ['queued', 'running', 'waiting_provider', 'blocked'].includes(s.pipeline.pipeline_status))
  )) return true
  return false
}

export const useEpisode = (
  episodeId: string,
  view?: 'script' | 'board' | 'wall' | 'cinema',
  intervalMs: PollInterval<Episode> = (ep) => episodeBusy(ep) ? 2000 : 0,
) =>
  usePoll<Episode>(
    () => api.get(`/episodes/${episodeId}${view ? `?view=${view}` : ''}`),
    intervalMs,
    [episodeId, view],
  )
