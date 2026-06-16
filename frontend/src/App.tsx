import { createContext, useCallback, useContext, useEffect, useState } from 'react'
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

const SECTIONS: { key: View; label: string; icon: string; needProject?: boolean; needEpisode?: boolean }[] = [
  { key: 'studio', label: '书房', icon: '书' },
  { key: 'bible', label: '人物谱', icon: '人', needProject: true },
  { key: 'scenes', label: '场景图', icon: '景', needProject: true },
  { key: 'episodes', label: '分集', icon: '集', needProject: true },
  { key: 'script', label: '剧本台', icon: '剧', needEpisode: true },
  { key: 'board', label: '分镜台', icon: '镜', needEpisode: true },
  { key: 'wall', label: '评审墙', icon: '审', needEpisode: true },
  { key: 'cinema', label: '成片台', icon: '片', needEpisode: true },
  { key: 'monitor', label: '监制房', icon: '控' },
]

export default function App() {
  const [view, setView] = useState<View>('studio')
  const [projectId, setProjectId] = useState<string | null>(null)
  const [episodeId, setEpisodeId] = useState<string | null>(null)
  const [chapterIdx, setChapterIdx] = useState<number | null>(null)
  const [toastMsg, setToastMsg] = useState<{ text: string; err: boolean } | null>(null)
  const [spineCollapsed, setSpineCollapsed] = useState(false)

  const toast = useCallback((text: string, isErr = false) => {
    setToastMsg({ text, err: isErr })
    window.setTimeout(() => setToastMsg(null), isErr ? 8000 : 3000)
  }, [])

  const go = useCallback((v: View, pid?: string | null, eid?: string | null, cidx?: number | null) => {
    if (pid !== undefined) setProjectId(pid)
    if (eid !== undefined) setEpisodeId(eid)
    if (cidx !== undefined) setChapterIdx(cidx)
    setView(v)
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
  const visibleSections = projectId ? SECTIONS : SECTIONS.filter(s => s.key === 'studio')

  const openSection = (s: (typeof SECTIONS)[number]) => {
    setView(s.key)
  }

  return (
    <NavCtx.Provider value={nav}>
      <aside className={`spine ${spineCollapsed ? 'collapsed' : ''}`}>
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
        </div>
        <nav>
          {visibleSections.map(s => (
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
        </nav>
        <div className="spine-foot">漫剧案头 · 贰</div>
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
      {toastMsg && <div className={`toast ${toastMsg.err ? 'err' : ''}`}>{toastMsg.text}</div>}
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

/** 轮询某资源；interval=0 不轮询 */
export function usePoll<T>(fetcher: () => Promise<T>, intervalMs: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const refresh = useCallback(() => {
    fetcher().then(d => { setData(d); setError(null) }).catch(e => setError(String(e.message || e)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  useEffect(() => {
    if (deps.some(d => d == null)) return
    refresh()
    if (!intervalMs) return
    const t = window.setInterval(refresh, intervalMs)
    return () => window.clearInterval(t)
  }, [refresh, intervalMs])
  return { data, error, refresh }
}

export const useProject = (projectId: string, intervalMs = 4000) =>
  usePoll<Project>(() => api.get(`/projects/${projectId}`), intervalMs, [projectId])

export const useEpisode = (episodeId: string, intervalMs = 4000) =>
  usePoll<Episode>(() => api.get(`/episodes/${episodeId}`), intervalMs, [episodeId])
