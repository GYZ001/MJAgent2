import { useState } from 'react'
import { api, numToCn } from '../api'
import { useNav, useProject } from '../App'
import { EpStamp } from './BiblePage'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'

function ScreenplayStamp({ status }: { status: string }) {
  const map: Record<string, [string, string]> = {
    pending: ['待剧本', 'grey'],
    running: ['剧本中', 'gold'],
    ready: ['剧本成', 'green'],
    failed: ['剧本败', 'red'],
  }
  const [label, color] = map[status] ?? [status, 'grey']
  return <span className={`stamp ${color}`}>{label}</span>
}

export default function EpisodesPage() {
  const { projectId, go, toast } = useNav()
  const { data: p, refresh } = useProject(projectId!)
  const [busy, setBusy] = useState(false)
  const eps = p?.episodes ?? []
  const screenplayTodoCount = eps.filter(e => e.screenplay_status === 'pending' || e.screenplay_status === 'failed' || !e.screenplay_mode || e.screenplay_mode === 'none').length
  const screenplayRunningCount = eps.filter(e => e.screenplay_status === 'running').length
  const storyboardReadyCount = eps.filter(e => e.screenplay_status === 'ready' && ['planned', 'script_failed'].includes(e.status)).length
  const scriptingCount = eps.filter(e => e.status === 'scripting').length
  const planTimer = useTaskTimer(`project.${projectId}.plan`, p?.plan_status === 'running')
  const screenplayAllTimer = useTaskTimer(`project.${projectId}.screenplay-all`, screenplayRunningCount > 0)
  const storyboardAllTimer = useTaskTimer(`project.${projectId}.storyboard-all`, scriptingCount > 0)

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
  // 可批量触发的 = 待分镜 + 卡在“分镜中”的（后端会回收无任务在跑的孤儿集）
  const pendingCount = storyboardReadyCount + scriptingCount

  const replan = () => {
    if (eps.length && !window.confirm('重新分集会清空本项目当前所有剧集（含已生成的分镜与视频），用全新方案替换。确定继续？')) return
    planTimer.start()
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
          <button className="btn" disabled={busy || p.plan_status === 'running' || screenplayTodoCount === 0}
            onClick={() => act(async () => {
              const needsConfirm = eps.some(e => ['scripted', 'confirmed', 'generating', 'done'].includes(e.status))
              if (needsConfirm && !window.confirm('批量生成剧本可能清空已有分镜、关键帧、视频和成片。确定继续？')) return
              screenplayAllTimer.start()
              const r = await api.post(`/projects/${p.id}/screenplay-all`) as { started: number }
              toast(`已为 ${r.started} 集发起剧本生成（完成后再展开分镜）`)
            })}>
            生成所有剧本{screenplayTodoCount ? `（${screenplayTodoCount} 集）` : ''}
          </button>
          <button className="btn" disabled={busy || p.plan_status === 'running' || pendingCount === 0}
            onClick={() => act(async () => {
              storyboardAllTimer.start()
              const r = await api.post(`/projects/${p.id}/storyboard-all`) as { started: number }
              toast(`已为 ${r.started} 集发起分镜生成（按剧本逐拍展开）`)
            })}>
            生成所有分镜{pendingCount ? `（${pendingCount} 集）` : ''}
          </button>
          {p.plan_status === 'running' && <span className="stamp gold">分集中（依据原文规划，篇幅长时需数分钟）</span>}
          {screenplayRunningCount > 0 && <span className="stamp gold">剧本中（{screenplayRunningCount} 集）</span>}
          {scriptingCount > 0 && <span className="stamp gold">分镜中（{scriptingCount} 集）</span>}
          <TaskTimer label="分集" timer={planTimer} />
          <TaskTimer label="批量剧本" timer={screenplayAllTimer} />
          <TaskTimer label="批量分镜" timer={storyboardAllTimer} />
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
              <ScreenplayStamp status={ep.screenplay_status} /> <EpStamp status={ep.status} /><br />
              {ep.screenplay_mode === 'full_script' && <span style={{ color: 'var(--ink-soft)' }}>完整剧本</span>}<br />
              {ep.screenplay_error && <span style={{ color: 'var(--cinnabar)' }}>剧本失败</span>}<br />
              <button className="btn small" style={{ marginTop: 4 }} onClick={() => go('script', p.id, ep.id)}>入剧本台 →</button><br />
              <button className="btn small" style={{ marginTop: 4 }} disabled={ep.screenplay_status !== 'ready'}
                onClick={() => go('board', p.id, ep.id)}>入分镜台 →</button>
            </div>
          </div>
        ))}
      </section>
    </>
  )
}
