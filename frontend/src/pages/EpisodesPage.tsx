import { useEffect, useRef, useState } from 'react'
import { api, numToCn } from '../api'
import { useNav, useProject } from '../App'
import { EpStamp } from './BiblePage'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'
import SearchField from '../components/SearchField'

function ScreenplayStamp({ status }: { status: string }) {
  const map: Record<string, [string, string]> = {
    pending: ['待剧本', 'grey'],
    running: ['剧本中', 'gold'],
    ready: ['剧本成', 'green'],
    warning: ['候选待修', 'red'],
    failed: ['剧本败', 'red'],
  }
  const [label, color] = map[status] ?? [status, 'grey']
  return <span className={`stamp ${color}`}>{label}</span>
}

const PAGE_SIZE = 15

export default function EpisodesPage() {
  const { projectId, go, toast } = useNav()
  const { data: p, refresh, error, loading } = useProject(projectId!)
  const [busy, setBusy] = useState(false)
  const [page, setPage] = useState(0)
  const [pageDraft, setPageDraft] = useState('1')
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const pageInputFocused = useRef(false)
  const eps = p?.episodes ?? []
  const screenplayTodoCount = eps.filter(e => ['pending', 'failed', 'warning'].includes(e.screenplay_status) || !e.screenplay_mode || e.screenplay_mode === 'none').length
  const screenplayRunningCount = eps.filter(e => e.screenplay_status === 'running').length
  const storyboardReadyCount = eps.filter(e => e.screenplay_status === 'ready' && ['planned', 'script_failed'].includes(e.status)).length
  const scriptingCount = eps.filter(e => e.status === 'scripting').length
  const planTimer = useTaskTimer(`project.${projectId}.plan`, p?.plan_status === 'running')
  const screenplayAllTimer = useTaskTimer(`project.${projectId}.screenplay-all`, screenplayRunningCount > 0)
  const storyboardAllTimer = useTaskTimer(`project.${projectId}.storyboard-all`, scriptingCount > 0)
  const query = search.trim().toLowerCase()
  const filteredEps = eps.filter(ep => {
    if (query && !`${ep.episode_no} ${ep.title} ${ep.source_chapters.join(' ')}`.toLowerCase().includes(query)) return false
    if (statusFilter === 'running') return ep.screenplay_status === 'running' || ep.status === 'scripting' || ep.status === 'generating'
    if (statusFilter === 'failed') return ep.screenplay_status === 'failed' || ep.screenplay_status === 'warning' || ep.status.includes('failed') || !!ep.screenplay_error
    if (statusFilter === 'done') return ep.status === 'done'
    if (statusFilter === 'pending') return ep.screenplay_status === 'pending' || ['planned', 'drafting'].includes(ep.status)
    return true
  })
  const pageCount = Math.max(1, Math.ceil(filteredEps.length / PAGE_SIZE))
  const curPage = Math.min(page, pageCount - 1)

  useEffect(() => {
    // 输入框聚焦编辑时不要被轮询/钳位覆盖草稿，否则正在输入的页码会被悄悄清空。
    if (!pageInputFocused.current) {
      setPageDraft(String(curPage + 1))
    }
  }, [curPage])

  if (error && !p) return <div className="empty">{error}</div>
  if (loading && !p) return <div className="empty">展卷中……</div>
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
  const pageEps = filteredEps.slice(curPage * PAGE_SIZE, curPage * PAGE_SIZE + PAGE_SIZE)

  const jumpToPage = () => {
    const raw = Number.parseInt(pageDraft.trim(), 10)
    if (!Number.isFinite(raw)) {
      setPageDraft(String(curPage + 1))
      return
    }
    const next = Math.min(pageCount, Math.max(1, raw))
    pageInputFocused.current = false
    setPage(next - 1)
    setPageDraft(String(next))
  }

  const replan = () => {
    if (eps.length && !window.confirm('重新分集会清空本项目当前所有剧集（含已生成的分镜与视频），用全新方案替换。确定继续？')) return
    planTimer.start()
    act(() => api.post(`/projects/${p.id}/plan`))
  }

  return (
    <>
      <header className="desk-head">
        <div className="crumb">书房 / 《{p.name}》</div>
        <h1>分集规划 <span className="sub">{p.chapters?.length} 章 · {eps.length} 集 · 追踪每集从剧本到成片的制作状态</span></h1>
        <hr className="rule" />
      </header>

      <section className="episode-overview">
        <div><span>全部分集</span><b>{eps.length}</b><small>每章一集</small></div>
        <div><span>待写剧本</span><b>{screenplayTodoCount}</b><small>可批量生成</small></div>
        <div><span>制作进行中</span><b>{screenplayRunningCount + scriptingCount}</b><small>剧本 / 分镜</small></div>
        <div><span>已成片</span><b>{eps.filter(ep => ep.status === 'done').length}</b><small>可进入交付</small></div>
      </section>

      <section className="card episode-workspace">
        <div className="episode-toolbar">
          <SearchField value={search} onChange={value => { setSearch(value); setPage(0) }} placeholder="搜索集数、标题或章节…" ariaLabel="搜索分集" />
          <select aria-label="按制作状态筛选" value={statusFilter} onChange={event => { setStatusFilter(event.target.value); setPage(0) }}>
            <option value="all">全部状态</option><option value="pending">待制作</option><option value="running">进行中</option>
            <option value="failed">需要处理</option><option value="done">已成片</option>
          </select>
          <button className="btn" disabled={!p.chapters?.length} onClick={() => go('reader', p.id, undefined, p.chapters?.[0]?.idx ?? 1)}>阅读原著</button>
          <details className="batch-actions">
            <summary className="btn primary">批量操作</summary>
            <div className="batch-actions-menu">
              <b>批量制作</b><span>操作前会再次确认影响范围</span>
              <button className="btn" disabled={busy || p.plan_status === 'running'} onClick={replan}>{eps.length ? '重新分集' : '开始分集'}</button>
              <button className="btn" disabled={busy || p.plan_status === 'running' || screenplayTodoCount === 0}
                onClick={() => act(async () => {
                  const needsConfirm = eps.some(e => ['scripted', 'confirmed', 'generating', 'done'].includes(e.status))
                  if (needsConfirm && !window.confirm(`将为 ${screenplayTodoCount} 集生成剧本，可能使对应下游素材失效。确定继续？`)) return
                  screenplayAllTimer.start()
                  const r = await api.post(`/projects/${p.id}/screenplay-all`) as { started: number }
                  toast(`已为 ${r.started} 集发起剧本生成`)
                })}>生成待办剧本（{screenplayTodoCount} 集）</button>
              <button className="btn" disabled={busy || p.plan_status === 'running' || pendingCount === 0}
                onClick={() => act(async () => {
                  if (!window.confirm(`将为 ${pendingCount} 集展开分镜。确定继续？`)) return
                  storyboardAllTimer.start()
                  const r = await api.post(`/projects/${p.id}/storyboard-all`) as { started: number }
                  toast(`已为 ${r.started} 集发起分镜生成`)
                })}>生成待办分镜（{pendingCount} 集）</button>
              {screenplayRunningCount > 0 && <button className="btn ghost" disabled={busy} onClick={() => act(async () => {
                const r = await api.post(`/projects/${p.id}/screenplay-all/cancel`) as { stopped: number }; toast(`已停止 ${r.stopped} 集剧本生成`)
              })}>停止批量剧本</button>}
            </div>
          </details>
        </div>
        <div className="episode-active-tasks">
          {p.plan_status === 'running' && <span className="stamp gold">分集中（依据原文规划，篇幅长时需数分钟）</span>}
          {screenplayRunningCount > 0 && <span className="stamp gold">剧本中（{screenplayRunningCount} 集）</span>}
          {scriptingCount > 0 && <span className="stamp gold">分镜中（{scriptingCount} 集）</span>}
          <TaskTimer label="分集" timer={planTimer} />
          <TaskTimer label="批量剧本" timer={screenplayAllTimer} />
          <TaskTimer label="批量分镜" timer={storyboardAllTimer} />
        </div>
        {p.plan_status === 'failed' && <div className="error-banner">分集失败：{'\n'}{p.plan_error}</div>}

        <div className="episode-list-head"><span>分集与章节</span><span>{query || statusFilter !== 'all' ? `找到 ${filteredEps.length} 集` : `共 ${eps.length} 集`}</span></div>

        {pageEps.map(ep => {
          const firstCh = ep.source_chapters[0]
          const preview = (chapterPreview.get(firstCh) ?? ep.synopsis ?? '').trim()
          const destination = ep.status === 'done' ? 'cinema'
            : ['confirmed', 'generating', 'paused_budget'].includes(ep.status) ? 'wall'
              : ep.screenplay_status === 'ready' ? 'board' : 'script'
          const destinationLabel = destination === 'cinema' ? '查看成片' : destination === 'wall' ? '继续评审' : destination === 'board' ? '继续分镜' : '继续剧本'
          return (
          <div key={ep.id} className="episode-row">
            <div className="ep-main">
              <div className="ep-no">第{numToCn(ep.episode_no)}集</div>
              <div className="ep-body">
                <div className="ep-title">{ep.title}</div>
                <div className="ep-syn">{preview || '本章暂无正文预览'}</div>
                <button className="episode-read" type="button" onClick={() => go('reader', p.id, undefined, firstCh)}>阅读原章</button>
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
              <div className="episode-pipeline" aria-label="制作进度">
                <span className={ep.screenplay_status === 'ready' ? 'done' : ep.screenplay_status === 'running' ? 'active' : ''}>剧本</span>
                <i /><span className={['scripted','confirmed','generating','done'].includes(ep.status) ? 'done' : ep.status === 'scripting' ? 'active' : ''}>分镜</span>
                <i /><span className={['confirmed','generating','done'].includes(ep.status) ? 'done' : ''}>视频</span>
                <i /><span className={ep.status === 'done' ? 'done' : ''}>成片</span>
              </div>
              <div className="ep-actions">
                <button className="btn small primary" onClick={() => go(destination, p.id, ep.id)}>{destinationLabel} →</button>
              </div>
            </div>
          </div>
        )})}

        {pageCount > 1 && (
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', justifyContent: 'center', marginTop: 14, flexWrap: 'wrap' }}>
            <button className="btn small" disabled={curPage <= 0} onClick={() => setPage(curPage - 1)}>← 上一页</button>
            <span style={{ fontSize: 13, color: 'var(--ink-faint)' }}>第 {curPage + 1} / {pageCount} 页 · 共 {filteredEps.length} 集</span>
            <button className="btn small" disabled={curPage >= pageCount - 1} onClick={() => setPage(curPage + 1)}>下一页 →</button>
            <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 13, color: 'var(--ink-faint)' }}>
              跳至
              <input
                type="number"
                min={1}
                max={pageCount}
                inputMode="numeric"
                value={pageDraft}
                aria-label={`跳至页码，范围 1 到 ${pageCount}`}
                onFocus={() => { pageInputFocused.current = true }}
                onChange={e => setPageDraft(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    jumpToPage()
                  }
                }}
                onBlur={() => {
                  pageInputFocused.current = false
                  // 失焦只还原显示，不自动跳页，避免点「上一页/下一页/跳转」时被 blur 抢先改页。
                  setPageDraft(String(curPage + 1))
                }}
                style={{
                  width: 56,
                  padding: '4px 6px',
                  border: '1px solid var(--hairline)',
                  borderRadius: 6,
                  background: 'var(--card)',
                  color: 'var(--ink)',
                  font: '13px "SF Mono", Menlo, monospace',
                  textAlign: 'center',
                }}
              />
              / {pageCount} 页
            </label>
            <button
              className="btn small"
              type="button"
              onMouseDown={e => e.preventDefault()}
              onClick={jumpToPage}
            >
              跳转
            </button>
          </div>
        )}
      </section>
    </>
  )
}
