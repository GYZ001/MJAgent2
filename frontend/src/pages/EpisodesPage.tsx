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

const PAGE_SIZE = 10  // 分集台每页展示的剧集数（= 章数）

export default function EpisodesPage() {
  const { projectId, go, toast } = useNav()
  const { data: p, refresh } = useProject(projectId!)
  const [busy, setBusy] = useState(false)
  const [page, setPage] = useState(0)
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

  // 分页（每页 10 集）+ 章节预览映射（按源章号取该章前 100 字）
  const chapterPreview = new Map((p.chapters ?? []).map(c => [c.idx, c.preview ?? '']))
  const pageCount = Math.max(1, Math.ceil(eps.length / PAGE_SIZE))
  const curPage = Math.min(page, pageCount - 1)
  const pageEps = eps.slice(curPage * PAGE_SIZE, curPage * PAGE_SIZE + PAGE_SIZE)

  const replan = () => {
    if (eps.length && !window.confirm('重新分集会清空本项目当前所有剧集（含已生成的分镜与视频），用全新方案替换。确定继续？')) return
    planTimer.start()
    act(() => api.post(`/projects/${p.id}/plan`))
  }

  return (
    <>
      <header className="desk-head">
        <div className="crumb">书房 / 《{p.name}》</div>
        <h1>分集台 <span className="sub">按章正则切分，全书 {p.chapters?.length} 章 · 每章一集，预览取该章前 100 字</span></h1>
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
          {screenplayRunningCount > 0 && (
            <button className="btn ghost" disabled={busy}
              onClick={() => act(async () => {
                const r = await api.post(`/projects/${p.id}/screenplay-all/cancel`) as { stopped: number }
                toast(`已停止 ${r.stopped} 集剧本生成`)
              })}>
              停止剧本
            </button>
          )}
          <button className="btn" disabled={busy || p.plan_status === 'running' || pendingCount === 0}
            onClick={() => act(async () => {
              storyboardAllTimer.start()
              const r = await api.post(`/projects/${p.id}/storyboard-all`) as { started: number }
              toast(`已为 ${r.started} 集发起分镜生成（按剧本逐拍展开）`)
            })}>
            生成所有分镜{pendingCount ? `（${pendingCount} 集）` : ''}
          </button>
          <button className="btn" disabled={!p.chapters?.length}
            onClick={() => go('reader', p.id, undefined, p.chapters?.[0]?.idx ?? 1)}>
            看正文
          </button>
          {p.plan_status === 'running' && <span className="stamp gold">分集中（依据原文规划，篇幅长时需数分钟）</span>}
          {screenplayRunningCount > 0 && <span className="stamp gold">剧本中（{screenplayRunningCount} 集）</span>}
          {scriptingCount > 0 && <span className="stamp gold">分镜中（{scriptingCount} 集）</span>}
          <TaskTimer label="分集" timer={planTimer} />
          <TaskTimer label="批量剧本" timer={screenplayAllTimer} />
          <TaskTimer label="批量分镜" timer={storyboardAllTimer} />
        </div>
        {p.plan_status === 'failed' && <div className="error-banner">分集失败：{'\n'}{p.plan_error}</div>}

        {pageEps.map(ep => {
          const firstCh = ep.source_chapters[0]
          const preview = (chapterPreview.get(firstCh) ?? ep.synopsis ?? '').trim()
          return (
          <div key={ep.id} className="episode-row">
            <div className="ep-main">
              <div className="ep-no">第{numToCn(ep.episode_no)}集</div>
              <div className="ep-body">
                <div className="ep-title">{ep.title}</div>
                <div className="ep-syn">{preview ? `${preview}…` : '（本章无正文预览）'}</div>
              </div>
            </div>
            <div className="ep-side">
              <div className="ep-meta">
                <span>源章 {ep.source_chapters[0]}–{ep.source_chapters[ep.source_chapters.length - 1]}</span>
                <span>目标 {ep.target_duration_s}s</span>
                <span>已耗 ¥{ep.cost_cny.toFixed(1)}</span>
              </div>
              <div className="ep-stamps">
                <ScreenplayStamp status={ep.screenplay_status} />
                <EpStamp status={ep.status} />
                {ep.screenplay_mode === 'full_script' && <span className="ep-note">完整剧本</span>}
                {ep.screenplay_error && <span className="ep-note err">剧本失败</span>}
              </div>
              <div className="ep-actions">
                <button className="btn small" onClick={() => go('reader', p.id, undefined, firstCh)}>看正文 →</button>
                <button className="btn small" onClick={() => go('script', p.id, ep.id)}>入剧本台 →</button>
                <button className="btn small" disabled={ep.screenplay_status !== 'ready'}
                  onClick={() => go('board', p.id, ep.id)}>入分镜台 →</button>
              </div>
            </div>
          </div>
        )})}

        {pageCount > 1 && (
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', justifyContent: 'center', marginTop: 14 }}>
            <button className="btn small" disabled={curPage <= 0} onClick={() => setPage(curPage - 1)}>← 上一页</button>
            <span style={{ fontSize: 13, color: 'var(--ink-faint)' }}>第 {curPage + 1} / {pageCount} 页 · 共 {eps.length} 集</span>
            <button className="btn small" disabled={curPage >= pageCount - 1} onClick={() => setPage(curPage + 1)}>下一页 →</button>
          </div>
        )}
      </section>
    </>
  )
}
