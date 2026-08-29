import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import {
  api, Scene, SceneGapScan, SceneRefSegment,
  SceneReferenceCandidate, SceneRefsProgress,
} from '../api'
import { useNav, usePoll, useProject } from '../App'
import { ServerTaskTimer } from '../components/TaskTimer'
import SearchField from '../components/SearchField'
import EvidenceDrawer from '../components/harness/EvidenceDrawer'
import GenerationParamsDialog from '../components/GenerationParamsDialog'
import ImageCompareModal from '../components/ImageCompareModal'
import PrepSubnav from '../components/PrepSubnav'
import QueryState from '../components/QueryState'
import DecisionDialog from '../components/DecisionDialog'
import OperationError from '../components/OperationError'
import VisualStyleDialog from '../components/VisualStyleDialog'
import WorldbuildingStatus, { worldbuildingRunning } from '../components/WorldbuildingStatus'
import { SINGLE_ROW_ASSET_PAGE, useFillPageSize } from '../hooks/useFillPageSize'
import { useFocusTrap } from '../hooks/useFocusTrap'
import { usePrepListState } from '../hooks/usePrepListState'
import { useVisualStyleDialog } from '../hooks/useVisualStyleDialog'
import { formatBookTitle } from '../lib/bookTitle'
import { sceneStepStatus } from '../lib/prepSteps'
import { sceneUsability } from '../lib/sceneUsability'
import { applyStyleRegen } from '../lib/styleRegen'
import { statusLabel, statusTitle } from '../lib/statusLabels'
import "../styles/ScenesPage.css";

type ScenePreviewDraft = {
  bibleVersion: number
  scenes: Scene[]
}

export function scenePreviewStorageKey(projectId: string): string {
  return `manju:scene-preview:${projectId}`
}

export function readScenePreviewDraft(
  storage: Pick<Storage, 'getItem' | 'removeItem'>,
  projectId: string,
  bibleVersion: number,
): Scene[] | null {
  const key = scenePreviewStorageKey(projectId)
  try {
    const parsed = JSON.parse(storage.getItem(key) || 'null') as ScenePreviewDraft | null
    const valid = parsed?.bibleVersion === bibleVersion
      && Array.isArray(parsed.scenes)
      && parsed.scenes.length > 0
      && parsed.scenes.every(scene =>
        typeof scene?.name === 'string' && typeof scene?.scene_canonical === 'string')
    if (valid) return parsed!.scenes
  } catch {
    // Invalid drafts are removed below.
  }
  storage.removeItem(key)
  return null
}

export function writeScenePreviewDraft(
  storage: Pick<Storage, 'setItem'>,
  projectId: string,
  bibleVersion: number,
  scenes: Scene[],
): void {
  storage.setItem(scenePreviewStorageKey(projectId), JSON.stringify({ bibleVersion, scenes }))
}

export default function ScenesPage() {
  const { projectId, toast, registerNavigationGuard } = useNav()
  const { data: p, refresh, error, status, loading } = useProject(projectId!, undefined, 'scenes')
  const [busy, setBusy] = useState(false)
  const [pageSize, sceneGridRef] = useFillPageSize(SINGLE_ROW_ASSET_PAGE)
  const [listState, setListState] = usePrepListState(projectId!, 'scene-library', pageSize)
  const search = listState.search
  const page = listState.page
  const availabilityFilter = listState.filters.availability || ''
  const setSearch = (value: string) => setListState(current => ({ ...current, search: value, page: 0 }))
  const setPage = (value: number) => setListState(current => ({ ...current, page: value, scrollY: window.scrollY }))
  const setFilter = (key: string, value: string) => setListState(current => ({
    ...current, filters: { ...current.filters, [key]: value }, page: 0,
  }))
  const [detailSceneName, setDetailSceneName] = useState<string | null>(null)
  const [paramsSceneName, setParamsSceneName] = useState<string | null>(null)
  const [paramsDirty, setParamsDirty] = useState({ anchor: false, prompt: false })
  const [paramsCloseConfirm, setParamsCloseConfirm] = useState(false)
  const [stopConfirm, setStopConfirm] = useState(false)
  const [candidatePreview, setCandidatePreview] = useState<{
    sceneName: string
    candidates: SceneReferenceCandidate[]
    adoptedArtifactId?: string | null
  } | null>(null)
  const [gapScan, setGapScan] = useState<SceneGapScan | null>(null)
  const [scenePreview, setScenePreview] = useState<Scene[] | null>(null)
  const [compareDetail, setCompareDetail] = useState<{ title: string; images: { src: string; label: string }[] } | null>(null)
  const styleDialog = useVisualStyleDialog(projectId!)
  // 生成前的费用确认弹窗已删除（2026-08-29 用户拍板）。busyRef 是同步锁，
  // 防止连点两次把「预检 -> 立即确认」这条自动化路径跑两遍——参见
  // BiblePage.tsx 同名注释，两页判据一致。
  const busyRef = useRef(false)

  const scenes = p?.bible?.scenes ?? []
  const generating = p?.scene_refs_status === 'running'
  const {
    data: progress,
    refresh: refreshProgress,
  } = usePoll<SceneRefsProgress>(
    () => api.sceneRefsProgress(projectId!),
    () => generating ? 2500 : 0,
    [p?.id ?? null],
  )
  const query = search.trim()
  const filtered = [...scenes].filter(s => {
    if (query && !s.name.includes(query) && !(s.scene_canonical || '').includes(query)) return false
    if (availabilityFilter && sceneUsability(s, false) !== availabilityFilter) return false
    return true
  }).sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
  const pageCount = pageSize > 0 ? Math.max(1, Math.ceil(filtered.length / pageSize)) : 1
  const curPage = Math.min(page, pageCount - 1)
  const dirtyParamCount = Number(paramsDirty.anchor) + Number(paramsDirty.prompt)
  const hasSceneCriteria = Boolean(query) || Boolean(availabilityFilter)

  const resetSceneList = () => {
    setListState(current => ({ ...current, search: '', filters: {}, sort: 'name', page: 0 }))
    window.requestAnimationFrame(() => {
      document.querySelector<HTMLInputElement>('input[aria-label="搜索场景"]')?.focus()
    })
  }

  useLayoutEffect(() => {
    if (!paramsSceneName || dirtyParamCount === 0) {
      registerNavigationGuard(null, false)
      return
    }
    registerNavigationGuard({
      title: '放弃未保存的场景参数？',
      summary: `${dirtyParamCount} 组场景参数仍在编辑`,
      message: '场景固定信息和场景图描述尚未保存，离开会丢失当前输入；已有场景图和下游产物不会改变。',
      details: ['场景参数不会自动保存', '返回继续编辑可保留当前输入'],
      confirmLabel: '放弃修改并离开',
      cancelLabel: '继续编辑',
      danger: true,
    }, true)
    return () => registerNavigationGuard(null, false)
  }, [dirtyParamCount, paramsSceneName, registerNavigationGuard])

  useEffect(() => {
    if (page > pageCount - 1) setPage(Math.max(0, pageCount - 1))
  }, [page, pageCount])

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      if (listState.scrollY > 0) window.scrollTo({ top: listState.scrollY, behavior: 'auto' })
    })
    const onScroll = () => setListState(current => current.scrollY === window.scrollY
      ? current : { ...current, scrollY: window.scrollY })
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => { window.cancelAnimationFrame(frame); window.removeEventListener('scroll', onScroll) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (generating) void refreshProgress()
  }, [generating, refreshProgress])

  useEffect(() => {
    if (!p || busy || scenePreview || scenes.length > 0) return
    const restored = readScenePreviewDraft(
      window.localStorage,
      p.id,
      p.bible_version ?? 0,
    )
    if (restored) setScenePreview(restored)
  }, [p, busy, scenePreview, scenes.length])

  if (error && !p) return <QueryState loading={false} error={error} status={status} hasData={false} objectName="场景库" onRetry={refresh}>{null}</QueryState>
  if (loading && !p) return <QueryState loading hasData={false} objectName="场景库" onRetry={refresh}>{null}</QueryState>
  if (!p) return <QueryState loading hasData={false} objectName="场景库" onRetry={refresh}>{null}</QueryState>

  // 同步锁：连点两次时第二次直接报错，而不是静默吞掉——静默吞掉会让用户以为
  // 第二次点击也生效了，实际什么都没发生。act() 自己吃掉这个错误转成 toast；
  // withLock 的调用方（如 SceneRefStrip.redoView）已经自带 try/catch + toast，
  // 让错误原样往上抛，不在这里重复提示。
  const withLock = async <T,>(fn: () => Promise<T>): Promise<T> => {
    if (busyRef.current) throw new Error('正在处理上一项操作，请稍候')
    busyRef.current = true
    setBusy(true)
    try { return await fn() }
    finally { busyRef.current = false; setBusy(false) }
  }

  const act = async (fn: () => Promise<unknown>, doneMsg?: string) => {
    try {
      await withLock(fn)
      if (doneMsg) toast(doneMsg)
      refresh()
    } catch (e: unknown) { toast(e instanceof Error ? e.message : String(e), true) }
  }

  const previewInitialScenes = async () => {
    setBusy(true)
    try {
      const preview = await api.sceneBiblePreview(p.id)
      writeScenePreviewDraft(
        window.localStorage,
        p.id,
        p.bible_version ?? 0,
        preview.scenes,
      )
      setScenePreview(preview.scenes)
    } catch (e: unknown) { toast(e instanceof Error ? e.message : String(e), true) }
    finally { setBusy(false) }
  }

  const scanGaps = async () => {
    setBusy(true)
    try {
      const result = await api.sceneRefsGaps(p.id)
      setGapScan(result)
      toast(`只读扫描完成：发现 ${result.total} 项；未生成图片、未扣费`)
    } catch (e: unknown) { toast(e instanceof Error ? e.message : String(e), true) }
    finally { setBusy(false) }
  }

  const quoteSceneGeneration = async (selectedScenes: string[], resume: boolean, title: string) => {
    await act(async () => {
      const quote = await api.sceneRefsPrecheck(p.id, { scenes: selectedScenes, resume })
      await api.genSceneRefs(p.id, {
        scenes: selectedScenes, resume, confirm: true, quote_id: quote.quote_id,
      })
      toast(`${title}：已提交 ${quote.image_count} 张场景图生成`)
    })
  }

  /**
   * 场景库页在项目尚无人物谱时的风格确认：与人物谱页「开始生成人物谱与场景库」
   * 走同一条后端路径（POST /projects/{id}/bible，重路径，含 LLM），不是
   * POST /bible/style——那条轻路径要求 bible_json 已存在，空项目调用会被
   * 后端 409 拒绝（见 app/domain/bible_ops.py set_bible_visual_style）。
   * 判据用产物信号 `hasBible = !!p.bible`，不是某个状态字段：人物谱一旦生成成
   * 功（即使后面失败重试）都会落 bible_json，只有真正从未成功过时才为空。
   *
   * 后端 _bible_task 在人物谱生成成功后会无条件依次触发定妆照、场景清单
   * （pending_scene_regen 票据）、场景图——本页确认一次即可拿到全部四类产物，
   * 不需要用户之后再去人物谱页点第二次。
   */
  const startBibleAndSceneLibrary = async (styleName: string) => {
    await act(async () => {
      const quote = await api.bibleGeneratePrecheck(p.id, { style_name: styleName })
      await api.post(`/projects/${p.id}/bible`, {
        confirm: true,
        quote_id: quote.quote_id,
        idempotency_key: quote.quote_id,
        style_name: styleName,
      })
      toast('人物谱生成已开始；完成后会自动接续生成定妆照、场景清单与场景图')
    })
  }

  /**
   * 场景库页的风格确认：只切换项目统一画风（不重新生成人物谱角色内容）。
   * 预检拿到（人物+场景）合并报价后立即用 quote_id 自动确认，不再弹窗等
   * 用户手动点「确认并开始」——后端在同一次请求里发起人物定妆照与场景图
   * 两条生成线，不是本页自己排队调用两个端点，那样任一步失败或页面被
   * 关掉，另一条线就发不出去了。
   */
  const submitStyleForScenes = async (styleName: string) => {
    await act(async () => {
      const outcome = await applyStyleRegen(p.id, styleName, p.bible_version ?? 0)
      if (outcome.kind === 'unchanged') {
        toast(`统一画风仍为「${styleName}」，无需变更`)
        return
      }
      if (outcome.kind === 'idempotent_replay') {
        toast('该次风格切换已经处理过，未重复触发生成')
        return
      }
      const parts: string[] = []
      if (outcome.sceneBibleReady) {
        parts.push(outcome.sceneRefsStarted ? '场景图已开始按新画风重新生成' : `场景图未能启动：${outcome.sceneRefsError || '请重试'}`)
      } else {
        parts.push('场景清单尚未生成，请先点击上方“准备场景清单”；完成后可单独按新画风生成场景图')
      }
      parts.push(outcome.refsStarted ? '定妆照已开始按新画风重新生成' : `定妆照未能启动：${outcome.refsError || '请到人物谱重试'}`)
      toast(parts.join('；'), !outcome.sceneRefsStarted && outcome.sceneBibleReady)
    })
  }

  const quoteViewRedo = (sceneName: string, sceneRefId: string, viewRole: string) =>
    withLock(async () => {
      const quote = await api.sceneRefsPrecheck(p.id, {
        scenes: [sceneName], view_role: viewRole, scene_reference_id: sceneRefId, action: 'regenerate_view',
      })
      await api.regenerateSceneView(p.id, sceneName, sceneRefId, viewRole, {
        confirm: true, quote_id: quote.quote_id,
      })
    })

  const paged = pageSize > 0
    ? filtered.slice(curPage * pageSize, curPage * pageSize + pageSize)
    : []
  const hasBible = !!p.bible
  // 「出图之前」阶段是否仍在跑：人物谱、定妆照两段都算——只要有一段在跑，
  // 场景库这一步已经在管线里排队等着接续，不能显示「未开始」（2026-08-29
  // 用户实测反馈：以人物谱页为准统一两页在这个窗口内的界面）。
  const pipelineRunning = worldbuildingRunning(p)
  const detailScene = detailSceneName ? scenes.find(scene => scene.name === detailSceneName) ?? null : null
  const paramsScene = paramsSceneName ? scenes.find(scene => scene.name === paramsSceneName) ?? null : null
  const hasUnavailable = scenes.some(scene => sceneUsability(scene, false) === 'unavailable')
  const sceneStatus = sceneStepStatus(p)

  return (
    <>
      <header className="desk-head">
        <div className="crumb">书房 / {formatBookTitle(p.name)}</div>
        <PrepSubnav current="scenes" statuses={{ scenes: sceneStatus }} />
        <h1>场景库 <span className="sub">管理视频生成所需的场景参考图</span></h1>
        <hr className="rule" />
      </header>

      <section className="card">
        <h3>场景图素材库
          <span className="hint">人物谱完成后自动准备场景设定</span>
        </h3>
        {!hasBible && (
          <div className="library-action-row">
            {!pipelineRunning && (
              <button className="btn primary" disabled={busy}
                title="确认画风后会依次自动生成人物谱、角色定妆照、场景清单与场景图；无需再到人物谱页操作。"
                aria-label={busy
                  ? '选择画风并生成人物谱与场景库，暂不可用：正在处理上一项操作'
                  : p.bible_status === 'failed' ? '重新选择画风并生成人物谱与场景库' : '选择画风并生成人物谱与场景库'}
                onClick={() => void styleDialog.openStyleDialog(p.bible_style_name)}>
                {p.bible_status === 'failed' ? '重新选择画风并生成人物谱与场景库' : '选择画风并生成人物谱与场景库'}
              </button>
            )}
            <WorldbuildingStatus project={p} running={pipelineRunning}
              busy={busy} setBusy={setBusy} toast={toast} refresh={refresh} />
          </div>
        )}
        {!hasBible && !pipelineRunning && (
          <div className="hint">
            选择统一画风后将依次自动生成人物谱、角色定妆照、场景清单与场景图。
          </div>
        )}
        {!hasBible && p.bible_status === 'failed' && (
          <OperationError
            title="人物谱生成未完成"
            message={p.bible_error}
            guidance="失败结果没有发布；原著和已生成资产保持不变。可重新选择画风并生成。"
          />
        )}
        {hasBible && (
          <div className="library-action-row">
            {!scenes.length && !generating && !pipelineRunning && (
              <button className="btn primary" disabled={busy}
                aria-label={busy
                  ? '准备场景清单，暂不可用：正在分析原文'
                  : '准备场景清单'}
                onClick={previewInitialScenes}>
                {busy ? '正在分析原文并准备场景清单…' : '准备场景清单'}
              </button>
            )}
            {scenes.length > 0 && !generating && (
              <button className="btn primary" disabled={busy}
                aria-label={busy ? '扫描场景图缺口，暂不可用：正在处理上一项操作' : '扫描场景图缺口，扫描免费且不会生成图片'}
                onClick={scanGaps}>
                扫描场景图缺口（免费）
              </button>
            )}
            {generating && (
              <button className="btn ghost" disabled={busy}
                aria-label={busy ? '停止场景图生成，暂不可用：正在处理上一项操作' : '停止场景图生成'}
                onClick={() => setStopConfirm(true)}>
                停止场景图生成
              </button>
            )}
            <WorldbuildingStatus project={p} running={pipelineRunning}
              busy={busy} setBusy={setBusy} toast={toast} refresh={refresh} />
            <button className="btn ghost" disabled={busy || generating || pipelineRunning}
              title="风格是项目级设置：确认后将直接依次重新生成场景图与定妆照两部分，已生成的都会按新画风重做"
              aria-label={busy || generating || pipelineRunning
                ? '配置统一画风，暂不可用：正在处理上一项操作'
                : '配置统一画风；确认后依次触发场景图与定妆照重新生成'}
              onClick={() => void styleDialog.openStyleDialog(p.bible_style_name)}>
              配置统一画风
            </button>
            {generating && <span className="stamp gold">生成中</span>}
            {scenes.length > 0 && <span className="stamp green">{scenes.length} 个场景</span>}
            <ServerTaskTimer
              label="场景图"
              startedAt={p.task_timings?.scene_refs?.started_at}
              finishedAt={p.task_timings?.scene_refs?.finished_at}
              running={p.scene_refs_status === 'running'}
            />
          </div>
        )}
        {pipelineRunning && (
          <div className="hint task-progress-copy" role="status">
            {p.bible_status === 'running'
              ? '人物谱正在生成；完成后会自动接续生成定妆照、场景清单与场景图，无需在本页手动触发。'
              : '定妆照正在生成；完成后会自动接续准备场景设定并生成场景图，无需在本页手动触发。'}
          </div>
        )}
        {generating && !scenes.length && (
          <div className="hint task-progress-copy" role="status">
            正在根据人物谱画风和原文准备场景设定；完成后会展示场景清单并自动开始生成场景图。
          </div>
        )}
        {generating && progress && (
          <div className="hint task-progress-copy" role="status">
            已完成 {progress.ready}/{progress.total} · 失败 {progress.failed} · 待复核 {progress.unverified} · 剩余 {progress.remaining}
            {progress.current_scene ? ` · 当前：${progress.current_scene}` : ''}
            {progress.current_view ? ` / ${sceneViewPresentation(progress.current_view).label}` : ''}
            {progress.phase ? ` · 阶段：${scenePhaseLabel(progress.phase)}` : ''}
            {progress.attempt ? ` · 第 ${progress.attempt} 次尝试` : ''}
            {typeof progress.spent_cny === 'number' ? ` · 已发生约 ¥${progress.spent_cny.toFixed(2)}` : ''}
            {Array.isArray(progress.refs_target) && progress.refs_target.length ? ` · 当前范围：${progress.refs_target.join('、')}` : ''}
          </div>
        )}
        {p.scene_refs_status === 'failed' && hasUnavailable && (
          <OperationError
            title="场景图生成未完成"
            message={p.scene_refs_error}
            guidance="已完成场景图和当前采用版本会保留。可重试失败或缺失项，不会重复覆盖可用图片。"
          />
        )}
        {p.scene_refs_status === 'warning' && hasUnavailable && (
          <OperationError
            title="部分场景图需要复核"
            message={p.scene_refs_error}
            guidance="主图可用性以每张场景卡片的状态为准；请先处理提示项，再继续下游制作。"
            variant="warning"
            detailLabel="查看复核详情"
          />
        )}
        {scenes.length > 0 && (
          <div className="hint library-note">
            分镜阶段由 AI 自动识别本集场景并等待对应场景图就绪，无需人工处理场景更新。
          </div>
        )}
      </section>

      {scenes.length > 0 && (
        <section className="card scene-library">
          <div className="library-toolbar">
            <SearchField value={search} onChange={value => { setSearch(value); setPage(0) }}
              placeholder="搜索场景名…" ariaLabel="搜索场景" className="library-search" />
            <select aria-label="可用状态筛选" value={availabilityFilter} onChange={e => setFilter('availability', e.target.value)}>
              <option value="">全部可用状态</option><option value="available">可用</option><option value="unavailable">不可用</option>
            </select>
            {(query || availabilityFilter) && (
              <button type="button" className="btn small ghost" onClick={resetSceneList}>清除搜索与筛选</button>
            )}
            <span className="library-result-count" role="status">
              共 {scenes.length} 个场景{hasSceneCriteria ? ` · 当前显示 ${filtered.length}` : ''}
            </span>
          </div>
          <div ref={sceneGridRef} className="figure-grid">
            {paged.map(s => {
              const fitting = generating && (!p.scene_refs_target || p.scene_refs_target === s.name)
              const refs = s.scene_refs ?? []
              const activeRef = refs.find(ref => ref.ep_end == null)
                ?? refs.find(ref => ref.image_url === s.ref_image_url)
                ?? [...refs].sort((a, b) => b.ep_start - a.ep_start)[0]
              const adoptedImageUrl = activeRef?.image_url
              const usability = sceneUsability(s, fitting)
              const stamp = fitting
                ? { label: '处理中', color: 'gold' }
                : usability === 'available'
                  ? { label: '可用', color: 'green' }
                  : { label: '不可用', color: 'red' }
              return (
                <article key={s.name} className="figure scene-card">
                  <div className="f-name">{s.name}
                    <span className={`stamp ${stamp.color}`}>{stamp.label}</span>
                  </div>
                  {adoptedImageUrl && (
                    <SceneCardImage src={adoptedImageUrl} name={s.name} dimmed={fitting}
                      onOpen={() => setCompareDetail({ title: `${s.name} · 当前采用图`, images: [{ src: adoptedImageUrl, label: '当前采用图' }] })} />
                  )}
                  <div className="scene-card-summary">
                    <small className="hint">首次出场：{s.first_episode ? `第 ${s.first_episode} 集` : '未分集'}</small>
                    {usability === 'unavailable' && !fitting && (
                      <small className="hint">请使用上方“扫描场景图缺口”统一处理</small>
                    )}
                    <button className="scene-detail-trigger" type="button" onClick={() => setDetailSceneName(s.name)}>
                      查看场景详情
                      <span aria-hidden="true">→</span>
                    </button>
                  </div>
                </article>
              )
            })}
          </div>
          {pageSize > 0 && !paged.length && (
            <div className="library-filter-empty" role="status">
              <b>{hasSceneCriteria ? '没有符合当前条件的场景' : '场景库暂无场景'}</b>
              <p>{hasSceneCriteria
                ? `${query ? `搜索“${query}”` : '当前可用状态筛选'}未命中；清除条件后可恢复全部 ${scenes.length} 个场景。`
                : '可先生成场景设定，确认清单后将直接开始生成场景图。'}</p>
              {hasSceneCriteria && <button type="button" className="btn small" onClick={resetSceneList}>清除搜索与筛选</button>}
            </div>
          )}
          {pageCount > 1 && (
            <div className="library-pagination" aria-label="场景分页">
              <button className="btn small" disabled={curPage <= 0}
                aria-label={curPage <= 0 ? '上一页，暂不可用：当前已是第一页' : '上一页'}
                onClick={() => setPage(curPage - 1)}>← 上一页</button>
              <span>第 {curPage + 1} / {pageCount} 页</span>
              <button className="btn small" disabled={curPage >= pageCount - 1}
                aria-label={curPage >= pageCount - 1 ? '下一页，暂不可用：当前已是最后一页' : '下一页'}
                onClick={() => setPage(curPage + 1)}>下一页 →</button>
            </div>
          )}
        </section>
      )}
      {candidatePreview && (
        <SceneCandidateModal
          projectId={p.id}
          {...candidatePreview}
          focusActive={!compareDetail}
          disabled={busy || generating}
          onCompare={images => setCompareDetail({ title: `${candidatePreview.sceneName} · 候选比较`, images })}
          onClose={() => setCandidatePreview(null)}
          onAdopted={(sceneName, candidates, adoptedArtifactId) => {
            setCandidatePreview({ sceneName, candidates, adoptedArtifactId })
            refresh()
          }}
          onCandidatesChanged={(sceneName, candidates) => {
            setCandidatePreview(current => current ? { ...current, sceneName, candidates } : current)
            refresh()
          }}
        />
      )}
      {detailScene && (
        <SceneDetailModal
          projectId={p.id}
          scene={detailScene}
          focusActive={!candidatePreview && !paramsScene && !compareDetail}
          disabled={busy || generating}
          onClose={() => setDetailSceneName(null)}
          onChanged={refresh}
          onShowCandidates={(sceneName, candidates, adoptedArtifactId) => {
            setCandidatePreview({ sceneName, candidates, adoptedArtifactId })
          }}
          onShowParams={sceneName => {
            setParamsDirty({ anchor: false, prompt: false }); setParamsSceneName(sceneName)
          }}
          onCompare={images => setCompareDetail({ title: `${detailScene.name} · 场景视角比较`, images })}
          onRequestRedo={quoteViewRedo}
        />
      )}
      {paramsScene && (
        <GenerationParamsDialog
          title={`${paramsScene.name} · 场景设定与重绘`}
          subtitle="查看或调整场景图生成描述，修改后可保存并重新出图。"
          focusActive={!compareDetail && !paramsCloseConfirm}
          onClose={() => {
            if (paramsDirty.anchor || paramsDirty.prompt) {
              setParamsCloseConfirm(true)
              return
            }
            setParamsSceneName(null); setParamsDirty({ anchor: false, prompt: false })
          }}
        >
          <SceneAnchorBlock projectId={p.id} scene={paramsScene} expectedVersion={p.bible_version ?? 0}
            disabled={busy || generating} onChanged={refresh}
            onDirtyChange={dirty => setParamsDirty(value => ({ ...value, anchor: dirty }))} />
          <ScenePromptBlock projectId={p.id} scene={paramsScene} disabled={busy || generating}
            onChanged={refresh}
            onDirtyChange={dirty => setParamsDirty(value => ({ ...value, prompt: dirty }))}
            regenerate={() => quoteSceneGeneration([paramsScene.name], false, `重新生成「${paramsScene.name}」场景视角包`)} />
        </GenerationParamsDialog>
      )}
      {paramsScene && paramsCloseConfirm && (
        <SceneParamsCloseDialog
          sceneName={paramsScene.name}
          onClose={() => setParamsCloseConfirm(false)}
          onDiscard={() => {
            setParamsCloseConfirm(false)
            setParamsSceneName(null)
            setParamsDirty({ anchor: false, prompt: false })
          }}
        />
      )}
      {stopConfirm && (
        <DecisionDialog
          title="停止场景图生成？"
          summary={progress
            ? `已完成 ${progress.ready}/${progress.total}，剩余 ${progress.remaining}`
            : '当前场景图任务仍在运行'}
          message="系统会停止本地生成队列并保留已落盘场景图；当前已提交给图片服务的请求可能仍会完成并产生费用。"
          details={[
            progress?.current_scene
              ? `当前处理：${progress.current_scene}${progress.current_view ? ` / ${sceneViewPresentation(progress.current_view).label}` : ''}`
              : '未完成场景可稍后重新扫描并补齐',
            '停止不会删除已完成图片或当前采用版本',
          ]}
          confirmLabel="确认停止生成"
          cancelLabel="继续生成"
          danger
          onClose={() => setStopConfirm(false)}
          onConfirm={() => {
            setStopConfirm(false)
            void act(() => api.cancelSceneRefs(p.id), '已停止场景图生成；已完成图片保留')
          }}
        />
      )}
      <VisualStyleDialog
        open={styleDialog.styleOpen}
        loading={styleDialog.styleLoading}
        error={styleDialog.styleError}
        options={styleDialog.styleOptions}
        selected={styleDialog.selectedStyle}
        scopeNote={hasBible
          ? '确认后将直接重新生成「场景图 + 定妆照」；人物设定本身不会重新生成。'
          : '确认后将生成人物谱与角色定妆照；完成后场景清单与场景图会自动接续生成，无需再到人物谱页操作。'}
        onSelect={styleDialog.setSelectedStyle}
        onClose={styleDialog.closeStyleDialog}
        onConfirm={() => {
          if (!styleDialog.selectedStyle) {
            styleDialog.setStyleError('请先选择统一画面风格')
            return
          }
          const chosen = styleDialog.selectedStyle
          styleDialog.closeStyleDialog()
          if (hasBible) {
            void submitStyleForScenes(chosen)
          } else {
            void startBibleAndSceneLibrary(chosen)
          }
        }}
      />
      {gapScan && (
        <SceneGapDialog scan={gapScan} onClose={() => setGapScan(null)} onGenerate={selected => {
          void handoffGapSelectionToGenerate(
            selected,
            () => setGapScan(null),
            scenes => quoteSceneGeneration(scenes, true, '补齐/重试场景图'),
          )
        }} />
      )}
      {scenePreview && (
        <ScenePreviewDialog
          scenes={scenePreview}
          onClose={() => {
            window.localStorage.removeItem(scenePreviewStorageKey(p.id))
            setScenePreview(null)
          }}
          onConfirm={async confirmed => {
            window.localStorage.removeItem(scenePreviewStorageKey(p.id))
            setScenePreview(null)
            await act(async () => {
              const quote = await api.sceneBiblePrecheck(p.id, confirmed)
              await api.genSceneBible(p.id, { scenes: confirmed, confirm: true, quote_id: quote.quote_id })
              toast(`场景清单已确认；已提交 ${quote.actual_view_count} 张场景图生成`)
            })
          }}
        />
      )}
      {compareDetail && <ImageCompareModal {...compareDetail} onClose={() => setCompareDetail(null)} />}
    </>
  )
}

function SceneParamsCloseDialog({
  sceneName,
  onClose,
  onDiscard,
}: {
  sceneName: string
  onClose: () => void
  onDiscard: () => void
}) {
  const trapRef = useFocusTrap(true, onClose)
  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog" role="dialog" aria-modal="true"
        aria-labelledby="scene-params-close-title">
        <h3 id="scene-params-close-title">「{sceneName}」有未保存修改</h3>
        <p>关闭后会放弃尚未保存的场景固定信息或场景图描述；已保存版本、图片和下游不会变化。</p>
        <div className="dialog-actions">
          <button className="btn" type="button" onClick={onClose}>返回继续编辑</button>
          <button className="btn danger" type="button" onClick={onDiscard}>放弃修改并关闭</button>
        </div>
      </section>
    </div>
  )
}

/** 二级扫描弹窗向生成动作交接时，必须先卸载前者，避免同层遮罩挡住后续状态提示。 */
export async function handoffGapSelectionToGenerate(
  selectedScenes: string[],
  closeGapDialog: () => void,
  generate: (scenes: string[]) => Promise<void>,
) {
  closeGapDialog()
  await generate(selectedScenes)
}

function sceneSegmentPrimaryFailed(segment: SceneRefSegment): boolean {
  return segment.qa?.status === 'failed' || Boolean(segment.qa?.hard_failures?.length)
}

function scenePhaseLabel(value: string): string {
  const phase = value.toLowerCase()
  if (phase.includes('pack_qa')) return '整包质检'
  if (phase.includes('single_view_qa')) return '单图质检'
  if (phase.includes('scene_reference') || phase.includes('image')) return '生成场景图'
  if (phase === 'running') return '执行中'
  if (phase === 'ready' || phase === 'succeeded') return '已完成'
  if (phase === 'failed') return '失败'
  return '处理中'
}

const SCENE_VIEW_PRESENTATION: Record<string, { label: string; description: string }> = {
  establishing: {
    label: '全景视角',
    description: '交代场景的整体布局、出入口和标志物，帮助后续镜头认清空间。',
  },
  reverse_angle: {
    label: '对向视角',
    description: '从相对方向看同一空间，常用于对话切换机位，并保持人物和方位一致。',
  },
  action_zone: {
    label: '动作区视角',
    description: '展示角色主要活动区域，为走位和动作镜头提供空间参考。',
  },
}

function sceneViewPresentation(viewRole?: string | null) {
  return SCENE_VIEW_PRESENTATION[viewRole || ''] || {
    label: '待识别视角',
    description: viewRole ? '该视角角色尚未映射，不能按已知机位说明或判定为通过。' : '视角角色尚未识别。',
  }
}

function SceneDetailModal({
  projectId, scene, focusActive, disabled, onClose, onChanged, onShowCandidates, onShowParams, onCompare, onRequestRedo,
}: {
  projectId: string
  scene: Scene
  focusActive: boolean
  disabled?: boolean
  onClose: () => void
  onChanged: () => void
  onShowCandidates: (
    sceneName: string,
    candidates: SceneReferenceCandidate[],
    adoptedArtifactId?: string | null,
  ) => void
  onShowParams: (sceneName: string) => void
  onCompare: (images: { src: string; label: string }[]) => void
  onRequestRedo: (sceneName: string, sceneRefId: string, viewRole: string) => Promise<void>
}) {
  const titleId = useId()
  const trapRef = useFocusTrap(focusActive, onClose)
  const refs = scene.scene_refs ?? []
  const activeRef = refs.find(ref => ref.ep_end == null)
    ?? refs.find(ref => ref.image_url === scene.ref_image_url)
    ?? [...refs].sort((a, b) => b.ep_start - a.ep_start)[0]
  const candidates = scene.scene_candidates ?? []
  const actualRoles = Array.from(new Set(refs.flatMap(ref => (ref.views ?? []).map(view => view.view_role || '')))).filter(Boolean)
  const guides = actualRoles.length ? actualRoles : ['establishing', 'reverse_angle']

  return (
    <div className="scene-detail-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section ref={trapRef} className="scene-detail-modal" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <header className="scene-detail-modal-head">
          <div>
            <span className="eyebrow">场景详情</span>
            <h2 id={titleId}>{scene.name}</h2>
            <p>查看这个场景的参考机位、适用版本和生成设置。</p>
          </div>
          <button type="button" aria-label="关闭场景详情" onClick={onClose}>×</button>
        </header>
        <div className="scene-detail-modal-body">
          <section className="scene-detail-anchor">
            <b>场景定位{scene.location_kind ? ` · ${scene.location_kind}` : ''}</b>
            <p>{scene.scene_canonical}</p>
            <dl className="scene-anchor-grid">
              <div><dt>空间</dt><dd>{scene.space || scene.location_kind || '旧版场景信息未拆分'}</dd></div>
              <div><dt>时段</dt><dd>{scene.time_of_day || '旧版场景信息未拆分'}</dd></div>
              <div><dt>光线</dt><dd>{scene.lighting || '旧版场景信息未拆分'}</dd></div>
              <div><dt>标志物</dt><dd>{scene.landmarks?.join('、') || '旧版场景信息未拆分'}</dd></div>
            </dl>
          </section>

          <section className="scene-view-guide" aria-label="场景视角说明">
            <h3>这些视角有什么区别？</h3>
            <div>{guides.map(role => {
              const guide = sceneViewPresentation(role)
              return <article key={role}><b>{guide.label}</b><p>{guide.description}</p></article>
            })}</div>
          </section>

          {refs.length > 0 ? (
            <SceneRefStrip
              projectId={projectId}
              sceneName={scene.name}
              segments={refs}
              disabled={disabled}
              onChanged={onChanged}
              onCompare={onCompare}
              onRequestRedo={onRequestRedo}
            />
          ) : (
            <div className="scene-detail-empty">这个场景还没有可用的参考视角文件。</div>
          )}
        </div>
        <footer>
          <div>
            {candidates.length > 0 && (
              <button className="btn" type="button" onClick={() => onShowCandidates(
                scene.name,
                candidates,
                activeRef?.artifact_id,
              )}>
                查看候选图（{candidates.length}）
              </button>
            )}
            <button className="btn" type="button" onClick={() => onShowParams(scene.name)}>
              场景设定与重绘
            </button>
            {!!refs.some(ref => (ref.views ?? []).some(view => view.image_url)) && (
              <button className="btn" type="button" onClick={() => onCompare(refs.flatMap(ref =>
                (ref.views ?? []).filter(view => !!view.image_url).map(view => ({
                  src: view.image_url!, label: `${sceneRangeLabel(ref.ep_start, ref.ep_end)} · ${sceneViewPresentation(view.view_role).label}`,
                }))))}>放大 / 1:1 / 并排比较</button>
            )}
          </div>
          <button className="btn primary" type="button" onClick={onClose}>完成</button>
        </footer>
      </section>
    </div>
  )
}

function SceneCandidateModal({
  projectId, sceneName, candidates, adoptedArtifactId, focusActive, disabled, onClose, onAdopted,
  onCandidatesChanged, onCompare,
}: {
  projectId: string
  sceneName: string
  candidates: SceneReferenceCandidate[]
  adoptedArtifactId?: string | null
  focusActive: boolean
  disabled?: boolean
  onClose: () => void
  onAdopted: (sceneName: string, candidates: SceneReferenceCandidate[], adoptedArtifactId: string) => void
  onCandidatesChanged: (sceneName: string, candidates: SceneReferenceCandidate[]) => void
  onCompare: (images: { src: string; label: string }[]) => void
}) {
  const { toast } = useNav()
  const [evidenceOpen, setEvidenceOpen] = useState(false)
  const [adoptingId, setAdoptingId] = useState<string | null>(null)
  const [adoptRequest, setAdoptRequest] = useState<{ artifactId: string; warnings: string[] } | null>(null)
  const trapRef = useFocusTrap(focusActive && !evidenceOpen && !adoptRequest, onClose)
  const availableCount = candidates.filter(item => !!item.image_url).length

  const applyAdoptedState = (artifactId: string) => candidates.map(item =>
    item.artifact_id === artifactId
      ? { ...item, status: 'approved' }
      : item.status === 'approved' ? { ...item, status: 'superseded' } : item)

  const adopt = async (artifactId: string, reason: string) => {
    setAdoptingId(artifactId)
    try {
      await api.adoptSceneCandidate(projectId, sceneName, artifactId, reason)
      setAdoptRequest(null)
      toast(`已采纳「${sceneName}」的候选图`)
      onAdopted(sceneName, applyAdoptedState(artifactId), artifactId)
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : String(e), true)
    } finally {
      setAdoptingId(null)
    }
  }

  return (
    <div className="scene-candidate-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section ref={trapRef} className={`scene-candidate-modal${candidates.length === 1 ? ' single' : candidates.length === 2 ? ' double' : ''}`}
        role="dialog" aria-modal="true" aria-labelledby="scene-candidate-title">
        <header className="scene-candidate-modal-head">
          <div>
            <span className="eyebrow">场景候选</span>
            <h2 id="scene-candidate-title">{sceneName}</h2>
            <p>候选图是否可人工采纳只看图片文件是否存在，由人工挑选决定。</p>
            {availableCount > 1 && (
              <button className="btn small" type="button" onClick={() => onCompare(
                candidates.filter(item => !!item.image_url).map(item => ({
                  src: item.image_url!, label: `尝试 ${item.attempt ?? '—'} · ${statusLabel(item.status)}`,
                })),
              )}>1:1 并排比较候选</button>
            )}
          </div>
          <button type="button" aria-label="关闭候选预览" onClick={onClose}>×</button>
        </header>
        <div className="scene-candidate-modal-body">
          {candidates.map(candidate => {
            const isCurrent = candidate.artifact_id === adoptedArtifactId
            const canOfferAdopt = !isCurrent && !!candidate.image_url
            return (
              <article className={`scene-candidate-preview${isCurrent ? ' current' : candidate.image_url ? ' passed' : ' rejected'}`}
                key={candidate.artifact_id}>
                <div className="scene-candidate-image">
                  {candidate.image_url
                    ? <CandidateImage src={candidate.image_url} alt={`${sceneName}候选 ${candidate.attempt ?? ''}`}
                        onOpen={() => onCompare([{
                          src: candidate.image_url!, label: `${sceneName} · 尝试 ${candidate.attempt ?? '—'}`,
                        }])} />
                    : <div className="scene-candidate-empty">图片不可用</div>}
                  <span>{isCurrent ? '当前采用' : candidate.image_url ? '可采纳' : '不可用'}</span>
                </div>
                <div className="scene-candidate-meta">
                  <div>
                    <b>尝试 {candidate.attempt ?? '—'}</b>
                    <small title={`${statusTitle(candidate.trust_level)} · ${statusTitle(candidate.status)}`}>
                      {statusLabel(candidate.trust_level)} · {statusLabel(candidate.status)}
                    </small>
                  </div>
                </div>
                {candidate.evidence && <EvidenceDrawer evidence={candidate.evidence} label="查看技术证据"
                  onOpenChange={setEvidenceOpen} />}
                {canOfferAdopt && <button className="btn small primary" type="button" disabled={!!adoptingId || disabled}
                  aria-label={adoptingId || disabled
                    ? `采纳此图，暂不可用：${adoptingId ? '正在处理上一项采纳操作' : '当前有其他场景任务运行'}`
                    : '采纳此图；下一步填写采纳理由并确认影响'}
                  onClick={() => setAdoptRequest({ artifactId: candidate.artifact_id, warnings: [] })}>
                  {adoptingId === candidate.artifact_id ? '采纳中…' : '采纳此图'}
                </button>}
              </article>
            )
          })}
        </div>
        <footer>
          <span>共 {candidates.length} 个候选</span>
          <button className="btn" type="button" onClick={onClose}>完成</button>
        </footer>
        {adoptRequest && <SceneCandidateAdoptDialog
          sceneName={sceneName}
          warnings={adoptRequest.warnings}
          busy={adoptingId === adoptRequest.artifactId}
          onClose={() => setAdoptRequest(null)}
          onConfirm={reason => void adopt(adoptRequest.artifactId, reason)}
        />}
      </section>
    </div>
  )
}

function SceneCandidateAdoptDialog({
  sceneName,
  warnings,
  busy,
  onClose,
  onConfirm,
}: {
  sceneName: string
  warnings: string[]
  busy: boolean
  onClose: () => void
  onConfirm: (reason: string) => void
}) {
  const [reason, setReason] = useState('')
  const trapRef = useFocusTrap(true, onClose)
  return (
    <div className="scene-manual-review-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target && !busy) onClose()
    }}>
      <section ref={trapRef} className="scene-manual-review-dialog" role="dialog" aria-modal="true"
        aria-labelledby="scene-adopt-title">
        <h3 id="scene-adopt-title">采纳「{sceneName}」候选图</h3>
        <p>采纳后将作为场景库当前主图；历史版本会保留，可在场景版本中回滚。</p>
        {!!warnings.length && (
          <div className="warning-banner" role="status">
            <b>采纳前请确认以下质检提示</b>
            <ul>{warnings.map((warning, index) => <li key={`${index}:${warning}`}>{warning}</li>)}</ul>
          </div>
        )}
        <label className="scene-manual-review-reason">
          <span>采纳理由（必填）</span>
          <textarea value={reason} disabled={busy} rows={3} maxLength={300}
            placeholder="说明画面质量、场景一致性和风险判断"
            onChange={event => setReason(event.target.value)} />
        </label>
        <footer>
          <button className="btn" type="button" disabled={busy} onClick={onClose}>取消</button>
          <button className="btn primary" type="button" disabled={busy || reason.trim().length < 4}
            aria-label={busy || reason.trim().length < 4
              ? `确认采纳此图，暂不可用：${busy ? '正在处理采纳操作' : '请填写至少 4 个字的采纳理由'}`
              : '确认采纳此图'}
            onClick={() => onConfirm(reason.trim())}>
            {busy ? '采纳中…' : '确认采纳此图'}
          </button>
        </footer>
      </section>
    </div>
  )
}

function sceneRangeLabel(start: number, end: number | null): string {
  if (end == null) return `第${start}集起`
  return start === end ? `第${start}集` : `第${start}~${end}集`
}

function SceneRefStrip({ projectId, sceneName, segments, disabled, onChanged, onCompare, onRequestRedo }: {
  projectId: string
  sceneName: string
  segments: SceneRefSegment[]
  disabled?: boolean
  onChanged?: () => void
  onCompare: (images: { src: string; label: string }[]) => void
  onRequestRedo: (sceneName: string, sceneRefId: string, viewRole: string) => void
}) {
  const { toast, go } = useNav()
  const [redoing, setRedoing] = useState<string | null>(null)
  const [rollingBack, setRollingBack] = useState<string | null>(null)
  const [rollbackDraft, setRollbackDraft] = useState<{ id: string; reason: string } | null>(null)
  const sorted = [...segments].sort((a, b) => a.ep_start - b.ep_start)
  const current = sorted.filter(seg => seg.ep_end == null).at(-1) || sorted.at(-1)

  const redoView = async (sceneRefId: string, viewRole: string) => {
    const label = sceneViewPresentation(viewRole).label
    setRedoing(`${sceneRefId}:${viewRole}`)
    try {
      await onRequestRedo(sceneName, sceneRefId, viewRole)
      toast(`${label}重做已受理`)
      onChanged?.()
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : String(e), true)
    } finally {
      setRedoing(null)
    }
  }

  return (
    <section className="scene-version-list">
      <div className="scene-version-heading">
        <div>
          <h3>参考视角</h3>
          <p>每个适用版本包含同一空间的不同机位；当前版本可单独重做某个视角。</p>
        </div>
        <button type="button" className="btn small" onClick={() => onCompare(
          sorted.flatMap(seg => (seg.views ?? []).filter(view => !!view.image_url).map(view => ({
            src: view.image_url!,
            label: `${sceneRangeLabel(seg.ep_start, seg.ep_end)} · ${sceneViewPresentation(view.view_role).label}`,
          }))),
        )}>并排比较</button>
      </div>
      <div className="scene-version-track">
        {sorted.map((seg, i) => (
          <article key={seg.id || i} className="scene-version-card">
            <div className="scene-version-meta">
              {sceneRangeLabel(seg.ep_start, seg.ep_end)} · {seg.ep_end == null ? '当前采用版本' : '历史版本'}
              {seg.pack_status ? ` · ${seg.pack_status === 'failed' && !sceneSegmentPrimaryFailed(seg)
                ? '附加视角待补（主图可用）' : statusLabel(seg.pack_status)}` : ''}
            </div>
            {seg.change?.reason && <div className="hint">切换原因：{seg.change.reason}</div>}
            {seg.reference_summary && (
              <div className="hint">
                引用：{seg.reference_summary.shot_count} 个镜头
                {(seg.reference_summary.episodes || []).map(episode => (
                  <button key={episode.id} type="button" className="link-button"
                    onClick={() => go('board', projectId, episode.id)}>第 {episode.episode_no} 集</button>
                ))}
                {!seg.reference_summary.shot_count && ' · 暂无分镜引用'}
              </div>
            )}
            {(seg.views && seg.views.length > 0) ? (
              <div className="scene-view-grid">
                {seg.views.map(view => view.image_url ? (
                  <figure key={view.id} className="scene-view-card">
                    <button type="button" className="scene-image-button" onClick={() => onCompare([{
                      src: view.image_url!, label: `${sceneRangeLabel(seg.ep_start, seg.ep_end)} · ${sceneViewPresentation(view.view_role).label}`,
                    }])} aria-label={`放大查看${sceneViewPresentation(view.view_role).label}`}>
                      <img src={view.image_url} alt={sceneViewPresentation(view.view_role).label} loading="lazy" decoding="async" />
                    </button>
                    <figcaption>
                      <b>{sceneViewPresentation(view.view_role).label}</b>
                      <span>{sceneViewPresentation(view.view_role).description}</span>
                    </figcaption>
                    {current?.id === seg.id && seg.id && view.view_role && (
                      <button
                        type="button"
                        className="btn small ghost"
                        disabled={disabled || !!redoing}
                        aria-label={disabled || !!redoing
                          ? `重做${sceneViewPresentation(view.view_role).label}，暂不可用：${disabled ? '当前有其他场景任务运行' : '正在提交上一项重做任务'}`
                          : `重做${sceneViewPresentation(view.view_role).label}；点击后立即提交生成`}
                        title={
                          disabled
                            ? '当前有其他场景任务运行，请等待完成'
                            : redoing
                              ? '正在提交重做任务'
                              : `点击后立即重新生成${sceneViewPresentation(view.view_role).label}，无需再次确认`
                        }
                        onClick={() => redoView(seg.id!, view.view_role!)}
                      >
                        {redoing === `${seg.id}:${view.view_role}`
                          ? '重做中…'
                          : `重做${sceneViewPresentation(view.view_role).label}`}
                      </button>
                    )}
                  </figure>
                ) : null)}
              </div>
            ) : (
              <div style={{ width: 104, textAlign: 'center' }}>
                {seg.image_url
                  ? <img src={seg.image_url} alt={sceneRangeLabel(seg.ep_start, seg.ep_end)} loading="lazy" decoding="async"
                      style={{ width: 104, height: 184, objectFit: 'cover', borderRadius: 6, border: '1px solid var(--hairline)' }} />
                  : <div style={{ width: 104, height: 184, borderRadius: 6, border: '1px dashed var(--hairline)',
                                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                                  fontSize: 11, color: 'var(--ink-faint)' }}>无图</div>}
              </div>
            )}
            {seg.ep_end != null && seg.id && (
              <button className="btn small" type="button" disabled={disabled || !!rollingBack}
                aria-label={disabled || !!rollingBack
                  ? `回滚到此版本，暂不可用：${disabled ? '当前有其他场景任务运行' : '正在处理上一项回滚'}`
                  : '回滚到此版本；下一步填写切换原因'}
                onClick={() => setRollbackDraft({ id: seg.id!, reason: '回滚到此历史通过包' })}>
                {rollingBack === seg.id ? '回滚中…' : '回滚到此版本'}
              </button>
            )}
            {rollbackDraft?.id === seg.id && (
              <div className="scene-version-rollback">
                <b>确认回滚到此版本</b>
                <span>新产生的下游引用将改用此场景包，现有历史记录不会删除。</span>
                <label>切换原因
                  <textarea rows={2} value={rollbackDraft?.reason ?? ''}
                    onChange={event => setRollbackDraft(current => current
                      ? { ...current, reason: event.target.value }
                      : current)} />
                </label>
                <div>
                  <button className="btn small ghost" type="button" disabled={!!rollingBack}
                    aria-label={rollingBack ? '取消版本回滚，暂不可用：正在处理回滚' : '取消版本回滚'}
                    onClick={() => setRollbackDraft(null)}>取消</button>
                  <button className="btn small primary" type="button"
                    disabled={!!rollingBack || (rollbackDraft?.reason.trim().length ?? 0) < 4}
                    aria-label={rollingBack || (rollbackDraft?.reason.trim().length ?? 0) < 4
                      ? `确认回滚，暂不可用：${rollingBack ? '正在处理回滚' : '请填写至少 4 个字的切换原因'}`
                      : '确认回滚到此场景版本'}
                    onClick={async () => {
                      const reason = rollbackDraft?.reason.trim()
                      if (!reason) return
                      setRollingBack(seg.id!)
                      try {
                        await api.rollbackSceneReference(projectId, sceneName, seg.id!, reason)
                        setRollbackDraft(null)
                        toast('已切换到所选历史场景版本')
                        onChanged?.()
                      } catch (e: unknown) {
                        toast(e instanceof Error ? e.message : String(e), true)
                      } finally {
                        setRollingBack(null)
                      }
                    }}>
                    {rollingBack === seg.id ? '回滚中…' : '确认回滚'}
                  </button>
                </div>
              </div>
            )}
          </article>
        ))}
      </div>
    </section>
  )
}

function SceneAnchorBlock({ projectId, scene, expectedVersion, disabled, onChanged, onDirtyChange }: {
  projectId: string; scene: Scene; expectedVersion: number; disabled: boolean; onChanged: () => void
  onDirtyChange: (dirty: boolean) => void
}) {
  const { toast } = useNav()
  const [editing, setEditing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [discardConfirm, setDiscardConfirm] = useState(false)
  const savedDraft = {
    scene_canonical: scene.scene_canonical,
    location_kind: scene.location_kind || '', space: scene.space || '',
    time_of_day: scene.time_of_day || '', lighting: scene.lighting || '',
    landmarks: (scene.landmarks || []).join('、'),
  }
  const [draft, setDraft] = useState(() => savedDraft)
  const dirty = JSON.stringify(draft) !== JSON.stringify(savedDraft)
  useEffect(() => { onDirtyChange(editing && dirty) }, [dirty, editing])
  useEffect(() => {
    if (!dirty) return
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault() }
    window.addEventListener('beforeunload', beforeUnload)
    return () => window.removeEventListener('beforeunload', beforeUnload)
  }, [dirty])
  const split = (value: string) => value.split(/[、,，;；]/).map(item => item.trim()).filter(Boolean)
  const descriptionLength = draft.scene_canonical.trim().length
  const descriptionInvalid = descriptionLength < 30 || descriptionLength > 80
  const saveDisabledReason = saving
    ? '正在保存场景固定信息'
    : !dirty
      ? '尚未修改任何场景信息'
      : descriptionInvalid
        ? '完整场景描述需为 30 至 80 字'
        : ''
  if (!editing) return (
    <section className="scene-anchor-editor">
      <h4>场景固定信息</h4>
      <p className="hint">空间：{scene.space || '未拆分'} · 时段：{scene.time_of_day || '未拆分'} · 光线：{scene.lighting || '未拆分'}</p>
      <button className="btn small" type="button" disabled={disabled}
        aria-label={disabled ? '逐段修改场景固定信息，暂不可用：当前有其他场景任务运行' : '逐段修改场景固定信息并查看差异'}
        onClick={() => setEditing(true)}>逐段修改 / 查看差异</button>
    </section>
  )
  return (
    <section className="scene-anchor-editor">
      <h4>场景固定信息（只保存不会生成图片）</h4>
      <div className="scene-anchor-form">
        <label>室内外<select value={draft.location_kind} onChange={e => setDraft(v => ({ ...v, location_kind: e.target.value }))}>
          <option value="">待确认</option><option value="室内">室内</option><option value="室外">室外</option><option value="其他">其他</option>
        </select></label>
        <label>空间<input value={draft.space} onChange={e => setDraft(v => ({ ...v, space: e.target.value }))} /></label>
        <label>时段<input value={draft.time_of_day} onChange={e => setDraft(v => ({ ...v, time_of_day: e.target.value }))} /></label>
        <label>光线<input value={draft.lighting} onChange={e => setDraft(v => ({ ...v, lighting: e.target.value }))} /></label>
        <label>标志物<input value={draft.landmarks} onChange={e => setDraft(v => ({ ...v, landmarks: e.target.value }))} placeholder="用顿号分隔" /></label>
      </div>
      <label>完整场景描述<textarea rows={4} value={draft.scene_canonical} onChange={e => setDraft(v => ({ ...v, scene_canonical: e.target.value }))} /></label>
      <div className={descriptionInvalid ? 'error-banner' : 'hint'}>
        {descriptionLength}/80 字（要求 30~80）{dirty ? ' · 保存后现有图片标记“待重绘”' : ''}
      </div>
      {dirty && <details><summary>查看前后差异</summary><p>原：{scene.scene_canonical}</p><p>新：{draft.scene_canonical}</p></details>}
      <div className="dialog-actions">
        <button className="btn small" type="button" disabled={saving}
          aria-label={saving ? '退出场景固定信息编辑，暂不可用：正在保存' : dirty ? '取消并检查是否放弃场景修改' : '退出场景固定信息编辑'}
          onClick={() => dirty ? setDiscardConfirm(true) : setEditing(false)}>取消</button>
        <button className="btn small primary" type="button" disabled={Boolean(saveDisabledReason)}
          aria-label={saveDisabledReason ? `仅保存场景固定信息，暂不可用：${saveDisabledReason}` : '仅保存场景固定信息，不生成图片'}
          onClick={async () => {
            setSaving(true)
            try {
              await api.editSceneAnchor(projectId, scene.name, {
                expected_version: expectedVersion, ...draft,
                landmarks: split(draft.landmarks),
              })
              toast('场景固定信息已保存；现有图片已标记待重绘（未生成、未扣费）')
              setDiscardConfirm(false); setEditing(false); onChanged()
            } catch (e: unknown) { toast(e instanceof Error ? e.message : String(e), true) }
            finally { setSaving(false) }
          }}>仅保存场景信息</button>
      </div>
      {discardConfirm && (
        <div className="inline-reset-confirm" role="status">
          <span><b>放弃尚未保存的场景固定信息？</b>当前输入会恢复为已保存版本，图片和下游不会变化。</span>
          <div>
            <button className="btn small ghost" type="button" onClick={() => setDiscardConfirm(false)}>继续编辑</button>
            <button className="btn small danger" type="button"
              onClick={() => { setDraft(savedDraft); setDiscardConfirm(false); setEditing(false) }}>放弃修改</button>
          </div>
        </div>
      )}
    </section>
  )
}

function ScenePromptBlock({ projectId, scene: s, disabled, onChanged, regenerate, onDirtyChange }: {
  projectId: string; scene: Scene; disabled: boolean
  onChanged: () => void; regenerate: () => void
  onDirtyChange: (dirty: boolean) => void
}) {
  const { toast } = useNav()
  const [draft, setDraft] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [restoreConfirm, setRestoreConfirm] = useState(false)
  const [discardConfirm, setDiscardConfirm] = useState(false)
  const isOverridden = !!(s.scene_prompt_override || '').trim()
  const effective = s.scene_prompt_effective || ''
  const savedPrompt = s.scene_prompt_override || effective
  const draftChanged = draft !== null && draft !== savedPrompt
  const promptSections = [
    { label: '场景固定描述', value: s.scene_canonical },
    { label: '实际生成合同', value: effective || '未提供' },
  ]
  const draftLength = (draft ?? '').trim().length
  const draftInvalid = draft !== null && draftLength > 0 && (draftLength < 10 || draftLength > 400)
  const baseDisabledReason = saving
    ? '正在保存上一项修改'
    : disabled
      ? '当前有其他场景任务运行，请等待完成'
      : ''
  const saveAndRegenerateDisabledReason = baseDisabledReason
    || (!draftChanged ? '尚未修改场景图描述' : '')
    || (draftInvalid ? '描述需为 10 至 400 字' : '')
  const saveOnlyDisabledReason = saving
    ? '正在保存上一项修改'
    : !draftChanged
      ? '尚未修改场景图描述'
      : draftInvalid
        ? '描述需为 10 至 400 字'
        : ''
  useEffect(() => { onDirtyChange(draftChanged) }, [draftChanged])

  async function save(thenRegen: boolean, valueOverride?: string) {
    setSaving(true)
    try {
      const r = await api.editScenePrompt(projectId, s.name, valueOverride ?? draft ?? '')
      toast(r.reset_to_default ? `「${s.name}」场景图描述已恢复默认` : `「${s.name}」场景图描述已保存`)
      setRestoreConfirm(false); setDiscardConfirm(false); setDraft(null); onChanged()
      if (thenRegen) regenerate()
    } catch (e: unknown) { toast((e as Error).message, true) }
    finally { setSaving(false) }
  }

  return (
    <div style={{ marginTop: 10 }}>
      <label className="f">场景图生成描述{isOverridden ? ' · 已自定义' : ' · 默认（由画风与场景固定信息合成）'}</label>
      {draft === null ? (
        <>
          <div className="f-misc" style={{ background: 'rgba(91,114,83,0.06)', borderLeft: '3px solid var(--moss)', padding: '6px 10px', borderRadius: '0 6px 6px 0', fontSize: 12.5 }}>
            {promptSections.map(section => <p key={section.label}><b>{section.label}：</b>{section.value}</p>)}
          </div>
          <div className="hint">生成结果是否可用只看文件是否存在；文案不会触发关键字拦截或自动改写。</div>
          <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
            <button className="btn small" disabled={disabled || saving}
              aria-label={baseDisabledReason ? `修改场景图描述，暂不可用：${baseDisabledReason}` : '修改场景图描述'}
              onClick={() => { setDiscardConfirm(false); setDraft(savedPrompt) }}>修改场景描述</button>
            <button className="btn small" disabled={disabled || saving}
              aria-label={baseDisabledReason
                ? `${s.ref_image_url ? '重新生成场景视角图' : '单独生成场景视角图'}，暂不可用：${baseDisabledReason}`
                : s.ref_image_url ? '重新生成场景视角图；点击后立即提交生成' : '单独生成场景视角图；点击后立即提交生成'}
              onClick={regenerate}>
              {s.ref_image_url ? '重新生成场景视角包' : '单独生成场景视角包'}
            </button>
          </div>
        </>
      ) : (
        <>
          <textarea aria-label={`${s.name}场景图描述`} rows={4} style={{ fontSize: 12.5 }} value={draft} onChange={e => setDraft(e.target.value)}
            placeholder="描述场景定场图：画风、地点、光线时段、陈设、氛围……（10~400 字）" />
          <div className={draftInvalid ? 'error-banner' : 'hint'}>{draftLength}/400 字
            {draftLength > 0 && (draftLength < 10 || draftLength > 400) ? ' · 自定义描述要求 10~400 字' : ''}
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 6, flexWrap: 'wrap' }}>
            <button className="btn small primary" disabled={Boolean(saveAndRegenerateDisabledReason)}
              aria-label={saveAndRegenerateDisabledReason ? `保存并重新出图，暂不可用：${saveAndRegenerateDisabledReason}` : '保存场景图描述并重新出图'}
              onClick={() => save(true)}>保存并重新出图</button>
            <button className="btn small" disabled={Boolean(saveOnlyDisabledReason)}
              aria-label={saveOnlyDisabledReason ? `仅保存场景图描述，暂不可用：${saveOnlyDisabledReason}` : '仅保存场景图描述'}
              onClick={() => save(false)}>仅保存</button>
            {isOverridden && <button className="btn small" disabled={saving}
              aria-label={saving ? '恢复默认场景图描述，暂不可用：正在保存上一项修改' : '恢复默认场景图描述'}
              onClick={() => setRestoreConfirm(true)}>恢复默认</button>}
            <button className="btn small ghost" disabled={saving}
              aria-label={saving ? '退出场景图描述编辑，暂不可用：正在保存上一项修改' : draftChanged ? '放弃场景图描述修改' : '退出场景图描述编辑'}
              onClick={() => {
                setRestoreConfirm(false)
                if (draftChanged) setDiscardConfirm(true)
                else setDraft(null)
              }}>{draftChanged ? '放弃修改' : '退出编辑'}</button>
          </div>
          {restoreConfirm && (
            <div className="inline-reset-confirm" role="status">
              <span><b>恢复「{s.name}」的默认场景描述？</b>只恢复由画风和场景固定信息合成的默认描述，不生成图片、不扣费。</span>
              <div>
                <button className="btn small ghost" type="button" disabled={saving}
                  onClick={() => setRestoreConfirm(false)}>取消</button>
                <button className="btn small primary" type="button" disabled={saving}
                  onClick={() => { setRestoreConfirm(false); setDraft(''); void save(false, '') }}>确认恢复默认</button>
              </div>
            </div>
          )}
          {discardConfirm && (
            <div className="inline-reset-confirm" role="status">
              <span><b>放弃尚未保存的场景图描述？</b>当前输入会恢复为已保存版本，场景图和下游不会变化。</span>
              <div>
                <button className="btn small ghost" type="button" onClick={() => setDiscardConfirm(false)}>继续编辑</button>
                <button className="btn small danger" type="button"
                  onClick={() => { setDiscardConfirm(false); setDraft(null) }}>放弃修改</button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function SceneGapDialog({ scan, onClose, onGenerate }: {
  scan: SceneGapScan
  onClose: () => void
  onGenerate: (scenes: string[]) => void
}) {
  const trapRef = useFocusTrap(true, onClose)
  const defaults = scan.items.map(item => item.scene)
  const [selected, setSelected] = useState(defaults)
  const labels: Record<string, string> = {
    missing: '不可用 · 缺图', hard_failure: '不可用 · 需要处理', warning: '不可用 · 待人工判断',
    interrupted: '不可用 · 任务未完成', unverified: '不可用 · 待确认',
  }
  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog scene-gap-dialog" role="dialog" aria-modal="true" aria-label="场景图缺口扫描结果">
        <h3>场景图缺口扫描结果</h3>
        <p>扫描本身免费。系统会优先复用已有图片并补做验证；只有确需生成新图片的项才会真正提交生成。</p>
        <div className="pay-scope-actions">
          <button className="btn small" type="button" disabled={!defaults.length}
            aria-label={!defaults.length ? '选择建议项，暂不可用：当前没有场景图缺口' : '选择全部建议项'}
            onClick={() => setSelected(defaults)}>选择建议项</button>
          <button className="btn small ghost" type="button" disabled={!selected.length}
            aria-label={!selected.length ? '清空已选场景，暂不可用：当前没有已选场景' : '清空已选场景'}
            onClick={() => setSelected([])}>清空</button>
          <span role="status">已选 {selected.length}/{scan.items.length}</span>
        </div>
        <ul className="scene-gap-list">
          {scan.items.map(item => (
            <li key={`${item.scene}:${item.category}`}>
              <label>
                <input type="checkbox" checked={selected.includes(item.scene)} onChange={event => setSelected(current =>
                  event.target.checked ? [...new Set([...current, item.scene])] : current.filter(name => name !== item.scene))} />
                <span><b>{item.scene}</b> · {labels[item.category] || item.category}</span>
              </label>
              <p>{item.reason}{item.views.length ? ` · 建议处理：${item.views.map(role => sceneViewPresentation(role).label).join('、')}` : ''}</p>
            </li>
          ))}
        </ul>
        {!scan.items.length && <div className="empty">所有场景图均可用，当前没有需要处理的缺口</div>}
        <div className="dialog-actions">
          <button className="btn" type="button" onClick={onClose}>关闭</button>
          <button className="btn primary" type="button" disabled={!selected.length}
            aria-label={!selected.length ? '处理已选缺口，暂不可用：请至少选择一个场景' : '处理已选缺口；点击后立即提交生成'}
            onClick={() => onGenerate(selected)}>处理已选缺口</button>
        </div>
      </section>
    </div>
  )
}

function ScenePreviewDialog({ scenes, onClose, onConfirm }: {
  scenes: Scene[]
  onClose: () => void
  onConfirm: (scenes: Scene[]) => Promise<void>
}) {
  const trapRef = useFocusTrap(true, onClose)
  const [items, setItems] = useState(() => scenes.map(scene => ({ ...scene })))
  const [selected, setSelected] = useState(() => scenes.map(scene => scene.name))
  const [busy, setBusy] = useState(false)
  const [mergeOpen, setMergeOpen] = useState(false)
  const [mergeName, setMergeName] = useState('')
  const selectedItems = items.filter(item => selected.includes(item.name))
  const duplicate = new Set(selectedItems.map(item => item.name)).size !== selectedItems.length
  const invalid = !selectedItems.length || duplicate || selectedItems.some(item =>
    !item.name.trim() || item.scene_canonical.trim().length < 30 || item.scene_canonical.trim().length > 80)

  const update = (index: number, patch: Partial<Scene>) => setItems(current => current.map((item, i) => {
    if (i !== index) return item
    const next = { ...item, ...patch }
    if (patch.name && selected.includes(item.name)) {
      setSelected(names => names.map(name => name === item.name ? patch.name! : name))
    }
    return next
  }))

  const mergeSelected = () => {
    if (selectedItems.length < 2) return
    const name = mergeName.trim()
    if (!name) return
    const merged: Scene = {
      ...selectedItems[0], name,
      scene_canonical: [...new Set(selectedItems.map(item => item.scene_canonical))].join('；').slice(0, 80),
      discovery_sources: [...new Set(selectedItems.flatMap(item => item.discovery_sources?.length ? item.discovery_sources : [item.name]))],
    }
    setItems(current => [merged, ...current.filter(item => !selected.includes(item.name))])
    setSelected([name])
    setMergeOpen(false)
    setMergeName('')
  }

  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog scene-preview-dialog" role="dialog" aria-modal="true" aria-label="确认场景提取清单">
        <h3>确认场景提取清单</h3>
        <p>先取消不需要项、合并同义场景或修订名称与固定场景描述；确认后将直接开始生成场景清单与首批场景图。</p>
        <div className="pay-scope-actions">
          <button className="btn small" type="button" aria-expanded={mergeOpen} disabled={selectedItems.length < 2}
            aria-label={selectedItems.length < 2 ? '合并勾选项，暂不可用：请至少选择两个场景' : '合并勾选的同义场景'}
            onClick={() => { setMergeName(selectedItems[0]?.name || ''); setMergeOpen(value => !value) }}>合并勾选项</button>
          <span role="status">已选 {selectedItems.length}/{items.length}</span>
        </div>
        {mergeOpen && (
          <div className="scene-merge-control">
            <label>合并后的规范场景名
              <input value={mergeName} autoFocus onChange={event => setMergeName(event.target.value)} />
            </label>
            <span>将合并当前勾选的 {selectedItems.length} 个场景，原始发现依据会保留。</span>
            <div>
              <button className="btn small ghost" type="button" onClick={() => setMergeOpen(false)}>取消合并</button>
              <button className="btn small primary" type="button" disabled={!mergeName.trim()}
                aria-label={!mergeName.trim() ? '确认合并，暂不可用：请填写合并后的场景名' : '确认合并勾选场景'}
                onClick={mergeSelected}>确认合并</button>
            </div>
          </div>
        )}
        <div className="scene-preview-list">
          {items.map((scene, index) => (
            <article key={`${index}:${scene.name}`}>
              <label className="pay-scope-option">
                <input type="checkbox" checked={selected.includes(scene.name)} onChange={event => setSelected(current =>
                  event.target.checked ? [...new Set([...current, scene.name])] : current.filter(name => name !== scene.name))} />
                <b>纳入本次范围</b>
              </label>
              <input aria-label="场景名称" value={scene.name} onChange={event => update(index, { name: event.target.value })} />
              <select aria-label="室内外" value={scene.location_kind || ''} onChange={event => update(index, { location_kind: event.target.value })}>
                <option value="">待确认</option><option value="室内">室内</option><option value="室外">室外</option><option value="其他">其他</option>
              </select>
              <textarea aria-label="场景固定描述" rows={3} value={scene.scene_canonical}
                onChange={event => update(index, { scene_canonical: event.target.value })} />
              <small className={scene.scene_canonical.length < 30 || scene.scene_canonical.length > 80 ? 'failed' : ''}>
                固定描述 {scene.scene_canonical.length}/80 字（要求 30~80）
              </small>
              {!!scene.discovery_sources?.length && <small>发现依据：{scene.discovery_sources.join('、')}</small>}
            </article>
          ))}
        </div>
        {duplicate && <div className="error-banner">场景名称不能重复；同义场景请合并</div>}
        <div className="dialog-actions">
          <button className="btn" type="button" disabled={busy}
            aria-label={busy ? '取消场景清单，暂不可用：正在提交生成请求' : '取消场景清单'}
            onClick={onClose}>取消</button>
          <button className="btn primary" type="button" disabled={busy || invalid}
            aria-label={busy || invalid
              ? `确认场景清单并开始出图，暂不可用：${busy ? '正在提交生成请求' : !selectedItems.length ? '请至少选择一个场景' : duplicate ? '场景名称不能重复，请先合并' : '场景名称不能为空，且固定描述需为 30 至 80 字'}`
              : '确认场景清单，点击后立即开始生成场景图'}
            onClick={async () => {
            setBusy(true); try { await onConfirm(selectedItems) } finally { setBusy(false) }
          }}>{busy ? '正在提交…' : '确认场景清单并开始出图'}</button>
        </div>
      </section>
    </div>
  )
}

function SceneCardImage({ src, name, dimmed, onOpen }: {
  src: string; name: string; dimmed: boolean; onOpen: () => void
}) {
  const [failed, setFailed] = useState(false)
  const [retry, setRetry] = useState(0)
  if (failed) return (
    <div className="scene-visual scene-image-error" role="status">
      <span>图片加载失败（不是尚未出图）</span>
      <button type="button" className="btn small" onClick={() => { setFailed(false); setRetry(value => value + 1) }}>
        重试加载
      </button>
    </div>
  )
  return (
    <button type="button" className="scene-visual scene-image-button" onClick={onOpen} aria-label={`放大查看${name}`}>
      <img key={retry} src={src} alt={name} onError={() => setFailed(true)} loading="lazy" decoding="async"
        style={{ opacity: dimmed ? 0.45 : 1, transition: 'opacity 0.3s' }} />
    </button>
  )
}

function CandidateImage({ src, alt, onOpen }: { src: string; alt: string; onOpen: () => void }) {
  const [failed, setFailed] = useState(false)
  const [retry, setRetry] = useState(0)
  if (failed) return (
    <div className="scene-candidate-empty">
      图片加载失败
      <button type="button" className="btn small" onClick={() => { setFailed(false); setRetry(value => value + 1) }}>重试</button>
    </div>
  )
  return (
    <button type="button" className="scene-image-button" onClick={onOpen} aria-label={`放大查看${alt}`}>
      <img key={retry} src={src} alt={alt} onError={() => setFailed(true)} loading="lazy" decoding="async" />
    </button>
  )
}
