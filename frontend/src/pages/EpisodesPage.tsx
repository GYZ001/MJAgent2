import { useState } from 'react'
import { api, numToCn } from '../api'
import { useNav, useProject } from '../App'
import { EpStamp } from './BiblePage'

export default function EpisodesPage() {
  const { projectId, go, toast } = useNav()
  const { data: p, refresh } = useProject(projectId!)
  const [busy, setBusy] = useState(false)

  if (!p) return <div className="empty">展卷中……</div>

  const act = async (fn: () => Promise<unknown>, doneMsg?: string) => {
    setBusy(true)
    try {
      await fn()
      if (doneMsg) toast(doneMsg)
      refresh()
    } catch (e: unknown) {
      toast((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const eps = p.episodes ?? []
  const plannedCount = eps.filter(e => e.status === 'planned').length
  const scriptingCount = eps.filter(e => e.status === 'scripting').length
  // 可批量触发的 = 待分镜 + 卡在“分镜中”的（后端会回收无任务在跑的孤儿集）
  const pendingCount = plannedCount + scriptingCount

  const replan = () => {
    if (eps.length && !window.confirm('重新分集会清空本项目当前所有剧集（含已生成的分镜与视频），用全新方案替换。确定继续？')) return
    act(() => api.post(`/projects/${p.id}/plan`))
  }

  return (
    <>
      <header className="desk-head">
        <div className="crumb">书房 / 《{p.name}》</div>
        <h1>分集台 <span className="sub">覆盖全书全部 {p.chapters?.length} 章，分批续写，每集 {40}~{60} 秒</span></h1>
        <hr className="rule" />
      </header>

      <section className="card">
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginBottom: 8 }}>
          <button className="btn primary" disabled={busy || p.plan_status === 'running'}
            onClick={replan}>
            {eps.length ? '重新分集' : '开始分集'}
          </button>
          <button className="btn" disabled={busy || p.plan_status === 'running' || pendingCount === 0}
            onClick={() => act(async () => {
              const r = await api.post(`/projects/${p.id}/storyboard-all`) as { started: number }
              toast(`已为 ${r.started} 集发起分镜生成（限并发逐集进行，约数分钟）`)
            })}>
            生成所有分镜{pendingCount ? `（${pendingCount} 集）` : ''}
          </button>
          {p.plan_status === 'running' && <span className="stamp gold">分集中（依据原文规划，篇幅长时需数分钟）</span>}
          {scriptingCount > 0 && <span className="stamp gold">分镜中（{scriptingCount} 集）</span>}
        </div>
        {p.plan_status === 'failed' && <div className="error-banner">分集失败：{'\n'}{p.plan_error}</div>}

        {p.episodes?.map(ep => (
          <div key={ep.id} className="episode-row">
            <div className="ep-no">第{numToCn(ep.episode_no)}集</div>
            <div className="ep-body">
              <div className="ep-title">{ep.title}</div>
              <div className="ep-hook">钩子：{ep.hook}</div>
              <div className="ep-syn">{ep.synopsis}</div>
              <div className="ep-hook">尾钩：{ep.cliffhanger}</div>
            </div>
            <div className="ep-side">
              源章 {ep.source_chapters[0]}–{ep.source_chapters[ep.source_chapters.length - 1]}<br />
              目标 {ep.target_duration_s}s · 已耗 ¥{ep.cost_cny.toFixed(1)}<br />
              <EpStamp status={ep.status} /><br />
              <button className="btn small" style={{ marginTop: 4 }} onClick={() => go('board', p.id, ep.id)}>入分镜台 →</button>
            </div>
          </div>
        ))}
      </section>
    </>
  )
}
