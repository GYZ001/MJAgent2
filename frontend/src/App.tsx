import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { api, Episode, Project } from './api'
import Studio from './pages/Studio'
import BiblePage from './pages/BiblePage'
import BoardPage from './pages/BoardPage'
import WallPage from './pages/WallPage'
import MonitorPage from './pages/MonitorPage'

export type View = 'studio' | 'bible' | 'board' | 'wall' | 'monitor'

interface Nav {
  view: View
  projectId: string | null
  episodeId: string | null
  go: (v: View, projectId?: string | null, episodeId?: string | null) => void
  toast: (msg: string, isErr?: boolean) => void
}

const NavCtx = createContext<Nav>(null as unknown as Nav)
export const useNav = () => useContext(NavCtx)

const SECTIONS: { key: View; label: string; needProject?: boolean; needEpisode?: boolean }[] = [
  { key: 'studio', label: '书房' },
  { key: 'bible', label: '人物谱', needProject: true },
  { key: 'board', label: '分镜台', needEpisode: true },
  { key: 'wall', label: '评审墙', needEpisode: true },
  { key: 'monitor', label: '监制房' },
]

export default function App() {
  const [view, setView] = useState<View>('studio')
  const [projectId, setProjectId] = useState<string | null>(null)
  const [episodeId, setEpisodeId] = useState<string | null>(null)
  const [toastMsg, setToastMsg] = useState<{ text: string; err: boolean } | null>(null)

  const toast = useCallback((text: string, isErr = false) => {
    setToastMsg({ text, err: isErr })
    window.setTimeout(() => setToastMsg(null), isErr ? 8000 : 3000)
  }, [])

  const go = useCallback((v: View, pid?: string | null, eid?: string | null) => {
    if (pid !== undefined) setProjectId(pid)
    if (eid !== undefined) setEpisodeId(eid)
    setView(v)
  }, [])

  const nav: Nav = { view, projectId, episodeId, go, toast }

  return (
    <NavCtx.Provider value={nav}>
      <aside className="spine">
        <div className="seal">漫</div>
        <nav>
          {SECTIONS.map(s => (
            <button
              key={s.key}
              className={`spine-item ${view === s.key ? 'active' : ''}`}
              disabled={(s.needProject && !projectId) || (s.needEpisode && !episodeId)}
              onClick={() => setView(s.key)}
            >
              {s.label}
            </button>
          ))}
        </nav>
        <div className="spine-foot">漫剧案头 · 贰</div>
      </aside>
      <main className="desk">
        {view === 'studio' && <Studio />}
        {view === 'bible' && projectId && <BiblePage key={projectId} />}
        {view === 'board' && episodeId && <BoardPage key={episodeId} />}
        {view === 'wall' && episodeId && <WallPage key={episodeId} />}
        {view === 'monitor' && <MonitorPage />}
      </main>
      {toastMsg && <div className={`toast ${toastMsg.err ? 'err' : ''}`}>{toastMsg.text}</div>}
    </NavCtx.Provider>
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
