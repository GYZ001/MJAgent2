import { useDeferredValue, useEffect, useId, useRef, useState } from 'react'
import { api, numToCn, type Project } from '../api'
import { useNav, usePoll } from '../App'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'
import SearchField from '../components/SearchField'
import PrepSubnav from '../components/PrepSubnav'
import QueryState from '../components/QueryState'
import DecisionDialog from '../components/DecisionDialog'
import { EpisodeStatusStamp, ScreenplayStatusStamp } from '../components/ProductionStatusStamp'
import OperationError from '../components/OperationError'
import { useFocusTrap } from '../hooks/useFocusTrap'
import { formatBookTitle } from '../lib/bookTitle'

const PAGE_SIZE = 15
const TARGET_DURATION_CHOICES = [40, 50, 60, 70, 80, 90] as const
type BatchAction = 'replan' | 'screenplay' | 'storyboard'

export function resolveEpisodePage(
  value: string,
  pageCount: number,
  currentPage: number,
): { page: number; message: string } {
  const raw = Number.parseInt(value.trim(), 10)
  if (!Number.isFinite(raw)) {
    return { page: currentPage, message: `请输入 1 到 ${pageCount} 之间的页码` }
  }
  const page = Math.min(pageCount, Math.max(1, raw))
  if (page === currentPage && raw === currentPage) {
    return { page, message: `当前已是第 ${currentPage} 页` }
  }
  if (raw < 1) return { page, message: '页码不能小于 1，已跳到第一页' }
  if (raw > pageCount) return { page, message: `页码不能超过 ${pageCount}，已跳到最后一页` }
  return { page, message: `已跳到第 ${page} 页` }
}

export default function EpisodesPage() {
  const { projectId, go, toast } = useNav()
  const [busy, setBusy] = useState(false)
  const [page, setPage] = useState(0)
  const [pageDraft, setPageDraft] = useState('1')
  const [pageJumpMessage, setPageJumpMessage] = useState('')
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [focusEpisodeNo, setFocusEpisodeNo] = useState<number | null>(null)
  const [batchConfirm, setBatchConfirm] = useState<BatchAction | null>(null)
  const [batchStopConfirm, setBatchStopConfirm] = useState(false)
  const deferredSearch = useDeferredValue(search)
  const query = deferredSearch.trim().toLowerCase()
  const { data: p, refresh, error, loading } = usePoll<Project>(
    () => api.get(
      `/projects/${projectId}?view=episodes&page=${page + 1}&page_size=${PAGE_SIZE}`
      + `&query=${encodeURIComponent(query)}&status_filter=${encodeURIComponent(statusFilter)}`,
    ),
    (project) => project?.episodes_busy ? 3000 : 0,
    [projectId, page, query, statusFilter],
  )
  const pageInputFocused = useRef(false)
  const pageJumpMessageId = useId()
  const responseMatches = p?.episodes_page === page + 1
    && p?.episodes_query === query
    && p?.episodes_status_filter === statusFilter
  const eps = responseMatches ? (p?.episodes ?? []) : []
  const counts = p?.episode_counts
  const totalEpisodes = counts?.total ?? p?.episodes_total ?? eps.length
  const screenplayTodoCount = counts?.screenplay_todo ?? eps.filter(e => ['pending', 'failed', 'warning'].includes(e.screenplay_status) || !e.screenplay_mode || e.screenplay_mode === 'none').length
  const screenplayRunningCount = counts?.screenplay_running ?? eps.filter(e => e.screenplay_status === 'running').length
  const storyboardReadyCount = counts?.storyboard_ready ?? eps.filter(e => e.screenplay_status === 'ready' && ['planned', 'script_failed'].includes(e.status)).length
  const scriptingCount = counts?.scripting ?? eps.filter(e => e.status === 'scripting').length
  const planTimer = useTaskTimer(`project.${projectId}.plan`, p?.plan_status === 'running')
  const screenplayAllTimer = useTaskTimer(`project.${projectId}.screenplay-all`, screenplayRunningCount > 0)
  const storyboardAllTimer = useTaskTimer(`project.${projectId}.storyboard-all`, scriptingCount > 0)
  const [sbMetrics, setSbMetrics] = useState<{
    active_storyboard_runs: number
    scripting_episodes: number
    waiting_human: number
    paused: number
    repairing: number
    waiting_authorization?: number
    phase_counts: Record<string, number>
  } | null>(null)

  useEffect(() => {
    if (!projectId) return
    let cancelled = false
    const load = () => {
      api.get(`/projects/${projectId}/storyboard-metrics`).then((m: any) => {
        if (!cancelled) setSbMetrics(m)
      }).catch(() => { /* ignore */ })
    }
    load()
    const id = window.setInterval(load, scriptingCount > 0 ? 4000 : 15000)
    return () => { cancelled = true; window.clearInterval(id) }
  }, [projectId, scriptingCount])
  const filteredEps = eps
  const filteredTotal = p?.episodes_total ?? eps.length
  const pageCount = p?.episodes_page_count ?? 1
  const curPage = Math.min(page, pageCount - 1)
  const listUpdating = !responseMatches && !error

  const [portraitGap, setPortraitGap] = useState<{ missing_count: number; image_count: number } | null>(null)

  useEffect(() => {
    if (!projectId) return
    try {
      const raw = window.sessionStorage.getItem(`prep-episodes-focus:${projectId}`)
      if (!raw) return
      const focus = JSON.parse(raw) as { episode_no?: number }
      if (typeof focus.episode_no === 'number' && focus.episode_no > 0) {
        setFocusEpisodeNo(focus.episode_no)
        setPage(Math.max(0, Math.floor((focus.episode_no - 1) / PAGE_SIZE)))
        setSearch('')
        setStatusFilter('all')
      }
      window.sessionStorage.removeItem(`prep-episodes-focus:${projectId}`)
    } catch { /* ignore invalid focus payload */ }
  }, [projectId])

  useEffect(() => {
    if (!projectId) return
    let cancelled = false
    api.refsGaps(projectId).then(res => {
      if (!cancelled) {
        setPortraitGap(res.missing_count > 0
          ? { missing_count: res.missing_count, image_count: res.image_count }
          : null)
      }
    }).catch(() => {
      if (!cancelled) setPortraitGap(null)
    })
    return () => { cancelled = true }
  }, [projectId, p?.refs_status, p?.bible_version])

  useEffect(() => {
    // 输入框聚焦编辑时不要被轮询/钳位覆盖草稿，否则正在输入的页码会被悄悄清空。
    if (!pageInputFocused.current) {
      setPageDraft(String(curPage + 1))
    }
  }, [curPage])

  useEffect(() => {
    if (page > pageCount - 1) setPage(Math.max(0, pageCount - 1))
  }, [page, pageCount])

  useEffect(() => {
    if (!focusEpisodeNo || !responseMatches) return
    const node = document.querySelector<HTMLElement>(`[data-episode-no="${focusEpisodeNo}"]`)
    if (!node) return
    node.scrollIntoView({ block: 'center', behavior: 'smooth' })
    const id = window.setTimeout(() => setFocusEpisodeNo(null), 1800)
    return () => window.clearTimeout(id)
  }, [focusEpisodeNo, responseMatches, eps])

  if (!p) {
    return (
      <QueryState
        loading={loading}
        error={error}
        hasData={false}
        objectName="分集规划"
        loadingText="正在加载分集、剧本与分镜制作状态…"
        emptyText="未找到可展示的分集规划，请刷新后重试。"
        onRetry={() => void refresh()}
      >
        {null}
      </QueryState>
    )
  }

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

  // 分页（每页 15 集）+ 章节预览映射（按源章号取该章前 100 字）
  const chapterPreview = new Map((p.chapters ?? []).map(c => [c.idx, c.preview ?? '']))
  const pageEps = filteredEps

  const jumpToPage = () => {
    const resolved = resolveEpisodePage(pageDraft, pageCount, curPage + 1)
    if (!Number.isFinite(Number.parseInt(pageDraft.trim(), 10))) {
      setPageDraft(String(curPage + 1))
      setPageJumpMessage(resolved.message)
      return
    }
    pageInputFocused.current = false
    setPage(resolved.page - 1)
    setPageDraft(String(resolved.page))
    setPageJumpMessage(resolved.message)
  }

  const parsedPageDraft = Number.parseInt(pageDraft.trim(), 10)
  const jumpDisabledReason = !pageDraft.trim()
    ? '请先输入页码'
    : Number.isFinite(parsedPageDraft) && parsedPageDraft === curPage + 1
      ? `当前已是第 ${curPage + 1} 页`
      : ''

  const executeBatch = async () => {
    if (!batchConfirm) return
    if (batchConfirm === 'replan') {
      planTimer.start()
      await act(() => api.post(`/projects/${p.id}/plan`))
    } else if (batchConfirm === 'screenplay') {
      screenplayAllTimer.start()
      await act(async () => {
        const result = await api.post(`/projects/${p.id}/screenplay-all`) as { started: number }
        toast(`已为 ${result.started} 集发起剧本生成`)
      })
    } else {
      storyboardAllTimer.start()
      await act(async () => {
        const result = await api.post(`/projects/${p.id}/storyboard-all`) as { started: number }
        toast(`已为 ${result.started} 集发起分镜生成`)
      })
    }
    setBatchConfirm(null)
  }

  return (
    <>
      <header className="desk-head">
        <div className="crumb">书房 / {formatBookTitle(p.name)}</div>
        <PrepSubnav current="episodes" />
        <h1>分集规划 <span className="sub">{p.chapter_count ?? 0} 章 · {totalEpisodes} 集 · 追踪每集从剧本到成片的制作状态</span></h1>
        <hr className="rule" />
      </header>

      {portraitGap && (
        <section className="card" style={{ marginBottom: 12 }}>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
            <span className="stamp gold">人物定妆缺口</span>
            <span>
              仍有 {portraitGap.missing_count} 个角色/包未就绪（约 {portraitGap.image_count} 张图待补）。
              分集可继续，但下游出图可能受阻。
            </span>
            <button type="button" className="btn small primary" onClick={() => {
              window.sessionStorage.setItem(`prep-bible-focus:${p.id}`, JSON.stringify({ missing: 'yes' }))
              go('bible', p.id)
            }}>
              返回人物谱补齐缺口
            </button>
          </div>
        </section>
      )}

      <section className="episode-overview">
        <div><span>全部分集</span><b>{totalEpisodes}</b><small>每章一集</small></div>
        <div><span>待写剧本</span><b>{screenplayTodoCount}</b><small>可批量生成</small></div>
        <div><span>制作进行中</span><b>{screenplayRunningCount + scriptingCount}</b><small>剧本 / 分镜</small></div>
        <div><span>已成片</span><b>{counts?.done ?? eps.filter(ep => ep.status === 'done').length}</b><small>可进入交付</small></div>
      </section>

      <section className="card episode-workspace">
        <div className="episode-toolbar">
          <SearchField value={search} onChange={value => { setSearch(value); setPage(0); setPageJumpMessage('') }} placeholder="搜索集数、标题或章节…" ariaLabel="搜索分集" />
          <select aria-label="按制作状态筛选" value={statusFilter} onChange={event => { setStatusFilter(event.target.value); setPage(0); setPageJumpMessage('') }}>
            <option value="all">全部状态</option><option value="pending">待制作</option><option value="running">进行中</option>
            <option value="failed">需要处理</option><option value="done">已成片</option>
          </select>
          <button
            className="btn"
            disabled={!p.chapter_count}
            title={!p.chapter_count ? '当前项目没有可阅读的原文章节' : '从第一章开始阅读原著'}
            aria-label={!p.chapter_count ? '阅读原著，暂不可用：当前项目没有可阅读的原文章节' : '从第一章开始阅读原著'}
            onClick={() => go('reader', p.id, undefined, p.first_chapter_idx ?? 1)}
          >
            阅读原著
          </button>
          <details className="batch-actions">
            <summary className="btn primary">批量操作</summary>
            <div className="batch-actions-menu">
              <b>批量制作</b><span>操作前会再次确认影响范围</span>
              <button type="button" className="btn" aria-label={busy || p.plan_status === 'running'
                ? `${totalEpisodes ? '重新规划全部分集' : '开始规划分集'}，暂不可用：${p.plan_status === 'running' ? '分集规划正在运行，请等待完成' : '正在处理上一项操作'}`
                : totalEpisodes ? '重新规划全部分集' : '开始规划分集'} disabled={busy || p.plan_status === 'running'}
                title={p.plan_status === 'running' ? '分集规划正在运行，请等待完成' : busy ? '正在处理上一项操作' : ''}
                onClick={() => setBatchConfirm('replan')}>{totalEpisodes ? '重新分集' : '开始分集'}</button>
              <button type="button" className="btn" aria-label={busy || p.plan_status === 'running' || screenplayTodoCount === 0
                ? `生成待办剧本，暂不可用：${p.plan_status === 'running' ? '需等待分集规划完成' : screenplayTodoCount === 0 ? '当前没有待生成剧本的分集' : '正在处理上一项操作'}`
                : `生成待办剧本，共 ${screenplayTodoCount} 集`} disabled={busy || p.plan_status === 'running' || screenplayTodoCount === 0}
                title={p.plan_status === 'running' ? '需等待分集规划完成' : screenplayTodoCount === 0 ? '当前没有待生成剧本的分集' : busy ? '正在处理上一项操作' : ''}
                onClick={() => setBatchConfirm('screenplay')}>生成待办剧本（{screenplayTodoCount} 集）</button>
              <button type="button" className="btn" aria-label={busy || p.plan_status === 'running' || pendingCount === 0
                ? `生成待办分镜，暂不可用：${p.plan_status === 'running' ? '需等待分集规划完成' : pendingCount === 0 ? '当前没有剧本已就绪的待生成分镜' : '正在处理上一项操作'}`
                : `生成待办分镜，共 ${pendingCount} 集`} disabled={busy || p.plan_status === 'running' || pendingCount === 0}
                title={p.plan_status === 'running' ? '需等待分集规划完成' : pendingCount === 0 ? '当前没有剧本已就绪的待生成分镜' : busy ? '正在处理上一项操作' : ''}
                onClick={() => setBatchConfirm('storyboard')}>生成待办分镜（{pendingCount} 集）</button>
              {screenplayRunningCount > 0 && (
                <button className="btn ghost danger" disabled={busy}
                  aria-label={busy ? '停止批量剧本，暂不可用：正在处理上一项操作' : `停止批量剧本，共 ${screenplayRunningCount} 集正在运行`}
                  onClick={() => setBatchStopConfirm(true)}>
                  停止批量剧本
                </button>
              )}
            </div>
          </details>
        </div>
        <div className="episode-active-tasks">
          {p.plan_status === 'running' && <span className="stamp gold">分集中（依据原文规划，篇幅长时需数分钟）</span>}
          {screenplayRunningCount > 0 && <span className="stamp gold">剧本中（{screenplayRunningCount} 集）</span>}
          {scriptingCount > 0 && <span className="stamp gold">分镜中（{scriptingCount} 集）</span>}
          {sbMetrics && (sbMetrics.active_storyboard_runs > 0 || sbMetrics.waiting_human > 0 || sbMetrics.paused > 0 || sbMetrics.repairing > 0) && (
            <span className="stamp grey">
              分镜任务 {sbMetrics.active_storyboard_runs}
              {sbMetrics.repairing > 0 ? ` · 修复 ${sbMetrics.repairing}` : ''}
              {sbMetrics.paused > 0 ? ` · 暂停 ${sbMetrics.paused}` : ''}
              {sbMetrics.waiting_human > 0 ? ` · 待人工 ${sbMetrics.waiting_human}` : ''}
              {(sbMetrics.waiting_authorization || 0) > 0 ? ` · 待授权 ${sbMetrics.waiting_authorization}` : ''}
            </span>
          )}
          <TaskTimer label="分集" timer={planTimer} />
          <TaskTimer label="批量剧本" timer={screenplayAllTimer} />
          <TaskTimer label="批量分镜" timer={storyboardAllTimer} />
        </div>
        {p.plan_status === 'failed' && (
          <OperationError
            title="分集规划未完成"
            message={p.plan_error}
            guidance="当前分集和已有下游内容没有被自动覆盖。可修正问题后重新规划，确认前不会清空旧数据。"
          />
        )}

        {error && p && (
          <OperationError
            title="分集列表更新失败"
            message={error}
            guidance="当前已有分集和制作内容没有改变。可重试加载当前搜索、筛选或页码。"
          >
            <button type="button" className="btn small ghost" onClick={() => void refresh()}>重试加载</button>
          </OperationError>
        )}

        <div className="episode-list-head"><span>分集与章节</span><span>{error
          ? '列表更新失败'
          : listUpdating
            ? '正在更新列表…'
            : query || statusFilter !== 'all' ? `找到 ${filteredTotal} 集` : `共 ${totalEpisodes} 集`}</span></div>

        {listUpdating && <div className="episode-list-loading" role="status">正在更新搜索、筛选与分页结果…</div>}

        {!listUpdating && pageEps.map(ep => {
          const firstCh = ep.source_chapters[0]
          const preview = (chapterPreview.get(firstCh) ?? ep.synopsis ?? '').trim()
          const destination = ep.status === 'done' ? 'cinema'
            : ['confirmed', 'generating', 'paused_budget'].includes(ep.status) ? 'wall'
              : ep.screenplay_status === 'ready' ? 'board' : 'script'
          const destinationLabel = destination === 'cinema' ? '查看成片' : destination === 'wall' ? '进入生成台' : destination === 'board' ? '继续分镜' : '继续剧本'
          const targetDurationEditable = ['pending', 'failed'].includes(ep.screenplay_status)
            && ['planned', 'drafting'].includes(ep.status)
            && !ep.screenplay_artifact_id
            && !ep.shot_count
          const targetDurationReason = busy
            ? '正在处理上一项操作'
            : !targetDurationEditable
              ? '已有剧本或下游产物，为避免版本不一致暂不可修改'
              : ''
          return (
          <div key={ep.id} className={`episode-row${focusEpisodeNo === ep.episode_no ? ' focus-highlight' : ''}`} data-episode-no={ep.episode_no}>
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
                <span className="episode-target-inline" title={targetDurationReason || '整集节奏预算，包含对白、动作、反应和转场'}>
                  目标
                  <select
                    aria-label={`第${ep.episode_no}集目标时长${targetDurationReason ? `，暂不可修改：${targetDurationReason}` : ''}`}
                    title={targetDurationReason || '选择整集节奏预算'}
                    value={ep.target_duration_s}
                    disabled={busy || !targetDurationEditable}
                    onChange={event => {
                      const target = Number(event.target.value)
                      void act(
                        () => api.put(`/episodes/${ep.id}/target-duration`, { target_duration_s: target }),
                        `第${ep.episode_no}集目标时长已调整为 ${target} 秒`,
                      )
                    }}
                  >
                    {TARGET_DURATION_CHOICES.map(value => <option key={value} value={value}>{value}s</option>)}
                  </select>
                </span>
                <span>已耗 ¥{ep.cost_cny.toFixed(1)}</span>
              </div>
              <div className="ep-stamps">
                <ScreenplayStatusStamp status={ep.screenplay_status} />
                <EpisodeStatusStamp status={ep.status} />
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

        {!listUpdating && !error && !pageEps.length && (
          <div className="episode-list-empty" role="status">
            <strong>
              {query || statusFilter !== 'all'
                ? '当前筛选没有匹配的分集'
                : '当前项目还没有分集'}
            </strong>
            <p>
              {query || statusFilter !== 'all'
                ? '可清除搜索词和制作状态筛选，重新查看全部分集。'
                : '先根据原文章节生成分集规划，再继续批量制作剧本和分镜。'}
            </p>
            {query || statusFilter !== 'all' ? (
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setSearch('')
                  setStatusFilter('all')
                  setPage(0)
                  setPageJumpMessage('')
                }}
              >
                清除筛选并查看全部
              </button>
            ) : (
              <button
                type="button"
                className="btn primary"
                disabled={busy || p.plan_status === 'running'}
                aria-label={busy || p.plan_status === 'running'
                  ? `开始规划分集，暂不可用：${p.plan_status === 'running' ? '分集规划正在运行' : '正在处理上一项操作'}`
                  : '开始规划分集'}
                onClick={() => setBatchConfirm('replan')}
              >
                {p.plan_status === 'running' ? '分集规划中…' : '开始规划分集'}
              </button>
            )}
          </div>
        )}

        {!listUpdating && !error && pageCount > 1 && (
          <div className="episode-pagination" aria-label="分集分页">
            <button className="btn small" disabled={curPage <= 0}
              aria-label={curPage <= 0 ? '上一页，暂不可用：当前已是第一页' : '上一页'}
              onClick={() => { setPage(curPage - 1); setPageJumpMessage('') }}>← 上一页</button>
            <span className="episode-page-status">第 {curPage + 1} / {pageCount} 页 · 共 {filteredTotal} 集</span>
            <button className="btn small" disabled={curPage >= pageCount - 1}
              aria-label={curPage >= pageCount - 1 ? '下一页，暂不可用：当前已是最后一页' : '下一页'}
              onClick={() => { setPage(curPage + 1); setPageJumpMessage('') }}>下一页 →</button>
            <label className="episode-page-jump">
              跳至
              <input
                className="episode-page-input"
                type="number"
                min={1}
                max={pageCount}
                inputMode="numeric"
                value={pageDraft}
                aria-label={`跳至页码，范围 1 到 ${pageCount}`}
                aria-describedby={pageJumpMessageId}
                onFocus={() => { pageInputFocused.current = true }}
                onChange={e => { setPageDraft(e.target.value); setPageJumpMessage('') }}
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
              />
              / {pageCount} 页
            </label>
            <button
              className="btn small"
              type="button"
              disabled={Boolean(jumpDisabledReason)}
              aria-label={jumpDisabledReason ? `跳转页码，暂不可用：${jumpDisabledReason}` : '跳转到输入页码'}
              onMouseDown={e => e.preventDefault()}
              onClick={jumpToPage}
            >
              跳转
            </button>
            <span id={pageJumpMessageId} className="episode-page-message" role="status">{pageJumpMessage}</span>
          </div>
        )}
      </section>
      {batchConfirm && (
        <EpisodeBatchConfirmDialog
          action={batchConfirm}
          projectName={p.name}
          totalEpisodes={totalEpisodes}
          screenplayTodoCount={screenplayTodoCount}
          storyboardTodoCount={pendingCount}
          busy={busy}
          onClose={() => setBatchConfirm(null)}
          onConfirm={() => { void executeBatch() }}
        />
      )}
      {batchStopConfirm && (
        <DecisionDialog
          title="停止批量剧本生成？"
          summary={`当前有 ${screenplayRunningCount} 集剧本正在生成或修复`}
          message="系统会逐集取消仍在运行的任务，并把每集恢复到可重新生成或继续修复的状态；已完成剧本不会删除。"
          details={[
            '已写入的工作副本和已发布剧本会保留',
            '正在执行的模型请求可能已产生费用，停止不会退回已发生费用',
          ]}
          confirmLabel="确认停止这些剧本任务"
          cancelLabel="继续批量生成"
          danger
          onClose={() => setBatchStopConfirm(false)}
          onConfirm={() => {
            setBatchStopConfirm(false)
            void act(async () => {
              const result = await api.post(`/projects/${p.id}/screenplay-all/cancel`) as { stopped: number }
              toast(`已停止 ${result.stopped} 集剧本生成；已完成剧本和工作副本保留`)
            })
          }}
        />
      )}
    </>
  )
}

function EpisodeBatchConfirmDialog({
  action,
  projectName,
  totalEpisodes,
  screenplayTodoCount,
  storyboardTodoCount,
  busy,
  onClose,
  onConfirm,
}: {
  action: BatchAction
  projectName: string
  totalEpisodes: number
  screenplayTodoCount: number
  storyboardTodoCount: number
  busy: boolean
  onClose: () => void
  onConfirm: () => void
}) {
  const trapRef = useFocusTrap(true, onClose)
  const content = action === 'replan'
    ? {
      title: totalEpisodes ? '重新规划全部分集？' : '开始规划分集？',
      count: `${totalEpisodes || '全部'} 集`,
      impact: totalEpisodes
        ? '将清空当前全部分集及其剧本、分镜、视频和交付记录，再按原著重新建立分集。'
        : '将依据原著章节创建分集，不会启动剧本、分镜或视频生成。',
      cost: totalEpisodes
        ? '重新规划可能调用文本模型并产生费用；已发生的旧任务费用不会退回。'
        : '分集规划可能调用文本模型并产生费用，实际金额以调用日志为准。',
      confirm: totalEpisodes ? '确认清空并重新分集' : '确认开始分集',
      danger: totalEpisodes > 0,
    }
    : action === 'screenplay'
      ? {
        title: '批量生成待办剧本？',
        count: `${screenplayTodoCount} 集`,
        impact: '只处理待生成、失败或需要修订的剧本；已完成且无需重建的剧本不会重复生成。',
        cost: '每集会调用文本模型，可能产生模型费用；实际金额以调用日志为准，失败不会覆盖已完成剧本。',
        confirm: '确认生成待办剧本',
        danger: false,
      }
      : {
        title: '批量生成待办分镜？',
        count: `${storyboardTodoCount} 集`,
        impact: '只处理剧本已就绪或可从恢复点继续的分集；不会自动确认分镜，也不会启动付费视频。',
        cost: '逐集调用文本模型，可能产生模型费用；实际金额以调用日志为准。',
        confirm: '确认生成待办分镜',
        danger: false,
      }
  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target && !busy) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog episode-batch-dialog" role="dialog" aria-modal="true"
        aria-labelledby="episode-batch-title">
        <h3 id="episode-batch-title">{content.title}</h3>
        <dl>
          <div><dt>项目</dt><dd>{projectName}</dd></div>
          <div><dt>本次范围</dt><dd>{content.count}</dd></div>
          <div><dt>执行影响</dt><dd>{content.impact}</dd></div>
          <div><dt>费用说明</dt><dd>{content.cost}</dd></div>
        </dl>
        <div className="dialog-actions">
          <button className="btn" type="button" disabled={busy}
            aria-label={busy ? '取消批量操作，暂不可用：正在提交任务' : '取消，不执行批量操作'}
            onClick={onClose}>取消（不执行）</button>
          <button className={`btn ${content.danger ? 'danger' : 'primary'}`} type="button" disabled={busy}
            aria-label={busy ? `${content.confirm}，暂不可用：正在提交任务` : content.confirm}
            onClick={onConfirm}>
            {busy ? '提交中…' : content.confirm}
          </button>
        </div>
      </section>
    </div>
  )
}
