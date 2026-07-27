import { useEffect, useId, useRef, useState } from 'react'
import {
  api, Scene, SceneCostPrecheck, SceneGapScan, SceneRefSegment, SceneReferenceCandidate,
  SceneRefsProgress,
} from '../api'
import { useNav, useProject } from '../App'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'
import SearchField from '../components/SearchField'
import EvidenceDrawer from '../components/harness/EvidenceDrawer'
import GenerationParamsDialog from '../components/GenerationParamsDialog'
import ImageCompareModal from '../components/ImageCompareModal'
import PaymentConfirmDialog from '../components/PaymentConfirmDialog'
import PrepSubnav from '../components/PrepSubnav'
import QueryState from '../components/QueryState'
import AutoChangeQueue from '../components/AutoChangeQueue'
import { useFillPageSize } from '../hooks/useFillPageSize'
import { useFocusTrap } from '../hooks/useFocusTrap'
import { usePrepListState } from '../hooks/usePrepListState'
import { formatBookTitle } from '../lib/bookTitle'
import { sceneUsability } from '../lib/sceneUsability'
import { statusLabel, statusTitle, type PrepStepStatus } from '../lib/statusLabels'

export default function ScenesPage() {
  const { projectId, toast } = useNav()
  const { data: p, refresh, error, loading } = useProject(projectId!, undefined, 'scenes')
  const [busy, setBusy] = useState(false)
  const pageSize = useFillPageSize({ minCardWidth: 270, rows: 3, floor: 8, ceiling: 24 })
  const [listState, setListState] = usePrepListState(projectId!, 'scene-library', pageSize)
  const search = listState.search
  const page = listState.page
  const availabilityFilter = listState.filters.availability || ''
  const effectivePageSize = pageSize
  const setSearch = (value: string) => setListState(current => ({ ...current, search: value, page: 0 }))
  const setPage = (value: number) => setListState(current => ({ ...current, page: value, scrollY: window.scrollY }))
  const setFilter = (key: string, value: string) => setListState(current => ({
    ...current, filters: { ...current.filters, [key]: value }, page: 0,
  }))
  const [detailSceneName, setDetailSceneName] = useState<string | null>(null)
  const [paramsSceneName, setParamsSceneName] = useState<string | null>(null)
  const [paramsDirty, setParamsDirty] = useState({ anchor: false, prompt: false })
  const [candidatePreview, setCandidatePreview] = useState<{
    sceneName: string
    candidates: SceneReferenceCandidate[]
    adoptedArtifactId?: string | null
  } | null>(null)
  const [payOpen, setPayOpen] = useState(false)
  const [payLoading, setPayLoading] = useState(false)
  const [payError, setPayError] = useState<string | null>(null)
  const [payPrecheck, setPayPrecheck] = useState<SceneCostPrecheck | null>(null)
  const [payTitle, setPayTitle] = useState('')
  const payActionRef = useRef<null | ((scenes: string[]) => Promise<void>)>(null)
  const [gapScan, setGapScan] = useState<SceneGapScan | null>(null)
  const [progress, setProgress] = useState<SceneRefsProgress | null>(null)
  const [scenePreview, setScenePreview] = useState<Scene[] | null>(null)
  const [compareDetail, setCompareDetail] = useState<{ title: string; images: { src: string; label: string }[] } | null>(null)
  const sceneTimer = useTaskTimer(`project.${projectId}.scene_refs`, p?.scene_refs_status === 'running')

  const scenes = p?.bible?.scenes ?? []
  const generating = p?.scene_refs_status === 'running'
  const query = search.trim()
  const filtered = [...scenes].filter(s => {
    if (query && !s.name.includes(query) && !(s.scene_canonical || '').includes(query)) return false
    if (availabilityFilter && sceneUsability(s, false) !== availabilityFilter) return false
    return true
  }).sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
  const pageCount = Math.max(1, Math.ceil(filtered.length / effectivePageSize))
  const curPage = Math.min(page, pageCount - 1)

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
    if (!generating) return
    let cancelled = false
    const poll = async () => {
      try { const next = await api.sceneRefsProgress(projectId!); if (!cancelled) setProgress(next) } catch { /* refresh owns errors */ }
    }
    poll(); const id = window.setInterval(poll, 2500)
    return () => { cancelled = true; window.clearInterval(id) }
  }, [generating, projectId])

  if (error && !p) return <QueryState loading={false} error={error} hasData={false} objectName="场景库" onRetry={refresh}>{null}</QueryState>
  if (loading && !p) return <QueryState loading hasData={false} objectName="场景库" onRetry={refresh}>{null}</QueryState>
  if (!p) return <QueryState loading hasData={false} objectName="场景库" onRetry={refresh}>{null}</QueryState>

  const act = async (fn: () => Promise<unknown>, doneMsg?: string) => {
    setBusy(true)
    try { await fn(); if (doneMsg) toast(doneMsg); refresh() }
    catch (e: unknown) { toast((e as Error).message, true) }
    finally { setBusy(false) }
  }

  const showPayment = (
    title: string,
    precheck: SceneCostPrecheck,
    action: (selectedScenes: string[]) => Promise<void>,
  ) => {
    setPayTitle(title); setPayPrecheck(precheck); setPayError(null); setPayLoading(false)
    payActionRef.current = action; setPayOpen(true)
  }

  const previewInitialScenes = async () => {
    setBusy(true)
    try {
      const preview = await api.sceneBiblePreview(p.id)
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
    setPayLoading(true); setPayError(null); setPayTitle(title); setPayOpen(true)
    try {
      const quote = await api.sceneRefsPrecheck(p.id, { scenes: selectedScenes, resume })
      showPayment(title, quote, async selected => {
        await api.genSceneRefs(p.id, {
          scenes: selected.length ? selected : selectedScenes,
          resume, confirm: true, quote_id: quote.quote_id,
        })
      })
    } catch (e: unknown) { setPayError(e instanceof Error ? e.message : String(e)) }
    finally { setPayLoading(false) }
  }

  const quoteViewRedo = async (sceneName: string, sceneRefId: string, viewRole: string) => {
    const title = `重做「${sceneName}」${sceneViewPresentation(viewRole).label}`
    setPayLoading(true); setPayError(null); setPayTitle(title); setPayOpen(true)
    try {
      const quote = await api.sceneRefsPrecheck(p.id, {
        scenes: [sceneName], view_role: viewRole, scene_reference_id: sceneRefId, action: 'regenerate_view',
      })
      showPayment(title, quote, async () => {
        await api.regenerateSceneView(p.id, sceneName, sceneRefId, viewRole, {
          confirm: true, quote_id: quote.quote_id,
        })
      })
    } catch (e: unknown) { setPayError(e instanceof Error ? e.message : String(e)) }
    finally { setPayLoading(false) }
  }

  const paged = filtered.slice(curPage * effectivePageSize, curPage * effectivePageSize + effectivePageSize)
  const hasBible = !!p.bible
  const detailScene = detailSceneName ? scenes.find(scene => scene.name === detailSceneName) ?? null : null
  const paramsScene = paramsSceneName ? scenes.find(scene => scene.name === paramsSceneName) ?? null : null
  const hasUnavailable = scenes.some(scene => sceneUsability(scene, false) === 'unavailable')
  const sceneStatus: PrepStepStatus = hasUnavailable
    ? 'problem'
    : generating
      ? 'running'
      : scenes.length > 0
        ? 'done'
        : 'idle'

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
          <span className="hint">一键扫描缺图和不可用图；确需生图时才会进入费用确认</span>
        </h3>
        {!hasBible && (
          <div className="hint">请先到「人物谱」生成角色圣经；场景圣经会在人物谱定稿后自动生成。</div>
        )}
        {hasBible && (
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
            {!scenes.length && !generating && (
              <button className="btn primary" disabled={busy}
                onClick={previewInitialScenes}>
                生成场景圣经与场景图
              </button>
            )}
            {scenes.length > 0 && !generating && (
              <button className="btn primary" disabled={busy} onClick={scanGaps}>
                扫描场景图缺口（免费）
              </button>
            )}
            {generating && (
              <button className="btn ghost" disabled={busy}
                onClick={() => act(() => api.cancelSceneRefs(p.id), '已停止场景图生成')}>
                停止
              </button>
            )}
            {generating && <span className="stamp gold">生成中</span>}
            {scenes.length > 0 && <span className="stamp green">{scenes.length} 个场景</span>}
            <TaskTimer label="场景图" timer={sceneTimer} />
          </div>
        )}
        {generating && progress && (
          <div className="hint" role="status" style={{ marginTop: 8 }}>
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
          <div className="error-banner">场景图生成失败（原始错误如下，不做静默兜底）：{'\n'}{p.scene_refs_error}</div>
        )}
        {p.scene_refs_status === 'warning' && hasUnavailable && (
          <div className="warning-banner">{p.scene_refs_error}</div>
        )}
        {scenes.length > 0 && (
          <div className="hint" style={{ marginTop: 10 }}>
            分镜阶段会把镜头收敛到规范场景；新场景或永久状态变化只进入待审队列，批准后仍须单独确认费用才会出图。
          </div>
        )}
      </section>

      {scenes.length > 0 && (
        <section className="card scene-library">
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', margin: '4px 0 12px' }}>
            <SearchField value={search} onChange={value => { setSearch(value); setPage(0) }}
              placeholder="搜索场景名…" ariaLabel="搜索场景" className="library-search" />
            <select aria-label="可用状态筛选" value={availabilityFilter} onChange={e => setFilter('availability', e.target.value)}>
              <option value="">全部</option><option value="available">可用</option><option value="unavailable">不可用</option>
            </select>
            {(query || availabilityFilter) && (
              <button type="button" className="btn small ghost" onClick={() => setListState(current => ({
                ...current, search: '', filters: {}, sort: 'name', page: 0,
              }))}>清除</button>
            )}
            <span style={{ fontSize: 12.5, color: 'var(--ink-faint)' }}>
              共 {scenes.length} 个场景{query ? ` · 命中 ${filtered.length}` : ''}
            </span>
          </div>
          <div className="figure-grid">
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
          {!paged.length && (
            <div className="empty">{query ? `没有匹配「${query}」的场景` : '暂无场景'}</div>
          )}
          {pageCount > 1 && (
            <div style={{ display: 'flex', gap: 10, alignItems: 'center', justifyContent: 'center', marginTop: 14 }}>
              <button className="btn small" disabled={curPage <= 0} onClick={() => setPage(curPage - 1)}>← 上一页</button>
              <span style={{ fontSize: 13, color: 'var(--ink-faint)' }}>第 {curPage + 1} / {pageCount} 页</span>
              <button className="btn small" disabled={curPage >= pageCount - 1} onClick={() => setPage(curPage + 1)}>下一页 →</button>
            </div>
          )}
        </section>
      )}
      {hasBible && <AutoChangeQueue projectId={p.id} onChanged={refresh} scope="scene" />}
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
          title={`${paramsScene.name} · 生成参数与重绘`}
          subtitle="查看或调整场景图生成词，修改后可保存并重新出图。"
          focusActive={!payOpen && !compareDetail}
          onClose={() => {
            if ((paramsDirty.anchor || paramsDirty.prompt) && !window.confirm('有未保存的场景编辑，确认放弃并关闭？')) return
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
      <PaymentConfirmDialog
        open={payOpen}
        title={payTitle || '确认场景图片费用'}
        precheck={payPrecheck}
        loading={payLoading}
        error={payError}
        enableScopeSelection={false}
        scopeSelectionTitle="选择本次处理的场景/视角"
        onClose={() => { setPayOpen(false); setPayPrecheck(null); setPayError(null); payActionRef.current = null }}
        onConfirm={async selection => {
          if (!payActionRef.current) return
          setPayLoading(true)
          try {
            await payActionRef.current(selection.scenes ?? [])
            toast('付费任务已受理；新包通过完整 QA 前保留旧资产')
            setPayOpen(false); setGapScan(null); refresh()
          } catch (e: unknown) { setPayError(e instanceof Error ? e.message : String(e)) }
          finally { setPayLoading(false) }
        }}
      />
      {gapScan && (
        <SceneGapDialog scan={gapScan} onClose={() => setGapScan(null)} onGenerate={selected => {
          void handoffGapSelectionToPayment(
            selected,
            () => setGapScan(null),
            scenes => quoteSceneGeneration(scenes, true, '付费补齐/重试场景图'),
          )
        }} />
      )}
      {scenePreview && (
        <ScenePreviewDialog
          scenes={scenePreview}
          onClose={() => setScenePreview(null)}
          onConfirm={async confirmed => {
            setScenePreview(null)
            setPayLoading(true); setPayError(null); setPayTitle('确认场景清单与首次出图费用'); setPayOpen(true)
            try {
              const quote = await api.sceneBiblePrecheck(p.id, confirmed)
              showPayment('确认场景清单与首次出图费用', quote, async selected => {
                const selectedSet = new Set(selected.length ? selected : confirmed.map(scene => scene.name))
                const finalScenes = confirmed.filter(scene => selectedSet.has(scene.name))
                if (finalScenes.length !== confirmed.length) {
                  throw new Error('付费范围已改变，请返回场景清单重新预检')
                }
                await api.genSceneBible(p.id, { scenes: finalScenes, confirm: true, quote_id: quote.quote_id })
              })
            } catch (e: unknown) { setPayError(e instanceof Error ? e.message : String(e)) }
            finally { setPayLoading(false) }
          }}
        />
      )}
      {compareDetail && <ImageCompareModal {...compareDetail} onClose={() => setCompareDetail(null)} />}
    </>
  )
}

/** 二级扫描弹窗向费用确认弹窗交接时，必须先卸载前者，避免同层遮罩挡住确认卡。 */
export async function handoffGapSelectionToPayment(
  selectedScenes: string[],
  closeGapDialog: () => void,
  openPayment: (scenes: string[]) => Promise<void>,
) {
  closeGapDialog()
  await openPayment(selectedScenes)
}

function sceneSegmentPrimaryFailed(segment: SceneRefSegment): boolean {
  return segment.qa?.status === 'failed' || Boolean(segment.qa?.hard_failures?.length)
}

function scenePhaseLabel(value: string): string {
  const phase = value.toLowerCase()
  if (phase.includes('pack_qa')) return '整包 QA'
  if (phase.includes('single_view_qa')) return '单图 QA'
  if (phase.includes('scene_reference') || phase.includes('image')) return '生成场景图'
  if (phase === 'running') return '执行中'
  if (phase === 'ready' || phase === 'succeeded') return '已完成'
  if (phase === 'failed') return '失败'
  return '处理中'
}

function candidateGateEvaluation(candidate: SceneReferenceCandidate) {
  return [...(candidate.evidence?.evaluations ?? [])].reverse().find(item => {
    const qa = item.evidence?.qa
    return item.evaluator_name.includes('consistency_qa')
      || item.evaluator_name === 'scene_candidate_human_hard_gate_review'
      || (!!qa && typeof qa === 'object' && 'policy_version' in qa)
  })
}

function candidateQaScore(candidate: SceneReferenceCandidate): number | null {
  const evaluation = candidateGateEvaluation(candidate)
  return typeof evaluation?.score === 'number' ? evaluation.score / 100 : null
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

function candidateGate(candidate: SceneReferenceCandidate) {
  const evaluations = candidate.evidence?.evaluations ?? []
  const evaluation = candidateGateEvaluation(candidate)
  const hard: string[] = []
  const warnings: string[] = []
  const uncertainties: string[] = []
  const historicalHard: string[] = []
  for (const previous of evaluations) {
    const previousQa = previous.evidence?.qa
    if (previousQa && typeof previousQa === 'object') {
      const value = previousQa as { hard_failures?: unknown[] }
      historicalHard.push(...(value.hard_failures ?? []).map(String))
    }
  }
  if (evaluation) {
    const qa = evaluation.evidence?.qa
    if (qa && typeof qa === 'object') {
      const value = qa as {
        hard_failures?: unknown[]; warnings?: unknown[]; issues?: unknown[];
        uncertainties?: unknown[]; status?: string; policy_version?: string; qa_recovered?: boolean
      }
      hard.push(...(value.hard_failures ?? []).map(String))
      warnings.push(...((value.warnings ?? value.issues) ?? []).map(String))
      uncertainties.push(...(value.uncertainties ?? []).map(String))
    }
    hard.push(...(evaluation.issues ?? [])
      .filter(issue => issue.severity === 'blocker' && issue.code === 'SCENE_HARD_GATE')
      .map(issue => issue.message))
  }
  const qa = evaluation?.evidence?.qa as { status?: string; policy_version?: string; qa_recovered?: boolean } | undefined
  const verified = !!evaluation
    && !!qa?.policy_version
    && (evaluation.hard_gate_passed === true || evaluation.hard_gate_passed === 1)
    && !evaluation.recovered
    && !qa.qa_recovered
    && !['unverified', 'pending', 'failed'].includes(qa.status || '')
    && hard.length === 0
  const reviewState = hard.length ? 'hard_failed'
    : verified ? 'passed'
      : evaluation && (evaluation.status === 'error' || qa?.qa_recovered) ? 'qa_incomplete'
        : 'not_reviewed'
  return {
    hard: [...new Set(hard)],
    historicalHard: [...new Set(historicalHard)],
    warnings: [...new Set(warnings)],
    uncertainties: [...new Set(uncertainties)],
    verified,
    reviewState,
    manualReviewAllowed: !verified && hard.length === 0 && historicalHard.length === 0,
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
  onRequestRedo: (sceneName: string, sceneRefId: string, viewRole: string) => void
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
            <span className="eyebrow">SCENE DETAILS</span>
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
              <div><dt>空间</dt><dd>{scene.space || scene.location_kind || '历史锚点未拆分'}</dd></div>
              <div><dt>时段</dt><dd>{scene.time_of_day || '历史锚点未拆分'}</dd></div>
              <div><dt>光线</dt><dd>{scene.lighting || '历史锚点未拆分'}</dd></div>
              <div><dt>标志物</dt><dd>{scene.landmarks?.join('、') || '历史锚点未拆分'}</dd></div>
              <div><dt>禁用元素</dt><dd>{scene.forbidden_elements?.join('、') || '人物、文字、水印、Logo'}</dd></div>
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
              生成参数与重绘
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
  const [reviewingId, setReviewingId] = useState<string | null>(null)
  const [manualCandidateId, setManualCandidateId] = useState<string | null>(null)
  const [manualBusy, setManualBusy] = useState(false)
  const trapRef = useFocusTrap(focusActive && !evidenceOpen && !manualCandidateId, onClose)
  const summary = candidates.reduce((result, candidate) => {
    const state = candidateGate(candidate).reviewState as keyof typeof result
    result[state] += 1
    return result
  }, { passed: 0, hard_failed: 0, qa_incomplete: 0, not_reviewed: 0 })

  const applyAdoptedState = (artifactId: string) => candidates.map(item =>
    item.artifact_id === artifactId
      ? { ...item, status: 'approved' }
      : item.status === 'approved' ? { ...item, status: 'superseded' } : item)

  const adopt = async (artifactId: string, warnings: string[]) => {
    let reason = '候选已有图片文件，人工确认采用；QA 结果仅作评分参考'
    if (warnings.length) {
      const input = window.prompt(`此候选有 QA 提示：\n${warnings.join('；')}\n\n请填写人工采用理由：`, '')
      if (!input?.trim()) return
      reason = input.trim()
    } else if (!window.confirm(`确认将此候选采纳为「${sceneName}」的场景库主图？切换会保留历史版本。`)) return
    setAdoptingId(artifactId)
    try {
      await api.adoptSceneCandidate(projectId, sceneName, artifactId, reason)
      toast(`已采纳「${sceneName}」的候选图`)
      onAdopted(sceneName, applyAdoptedState(artifactId), artifactId)
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : String(e), true)
    } finally {
      setAdoptingId(null)
    }
  }

  const review = async (candidate: SceneReferenceCandidate) => {
    setReviewingId(candidate.artifact_id)
    try {
      const result = await api.reviewSceneCandidate(projectId, sceneName, candidate.artifact_id)
      const nextCandidates = candidates.map(item => item.artifact_id === candidate.artifact_id && item.evidence
        ? { ...item, evidence: { ...item.evidence, evaluations: [...item.evidence.evaluations, result.evaluation] } }
        : item)
      onCandidatesChanged(sceneName, nextCandidates)
      const qa = result.qa as { status?: string; hard_failures?: string[]; uncertainties?: string[] }
      if (qa.status === 'passed' || qa.status === 'warning') {
        toast('新版 QA 已完成；有图候选可由人工直接采纳')
      } else if (qa.status === 'failed') {
        toast(`QA 已完成，记录提示：${(qa.hard_failures ?? []).join('；') || '请查看证据'}`, true)
      } else {
        toast(`QA 未能给出完整结论：${(qa.uncertainties ?? []).join('；') || '可稍后重试或人工复核'}`, true)
      }
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : String(e), true)
    } finally {
      setReviewingId(null)
    }
  }

  const manualReviewAndAdopt = async (
    artifactId: string,
    confirmations: { person_free: boolean; watermark_free: boolean; forbidden_text_free: boolean; space_type_matches: boolean },
    reason: string,
  ) => {
    setManualBusy(true)
    try {
      await api.manualReviewSceneCandidate(projectId, sceneName, artifactId, { confirmations, reason })
      setManualCandidateId(null)
      toast(`已记录人工复核并采纳「${sceneName}」候选`)
      onAdopted(sceneName, applyAdoptedState(artifactId), artifactId)
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : String(e), true)
    } finally {
      setManualBusy(false)
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
            <span className="eyebrow">SCENE CANDIDATES</span>
            <h2 id="scene-candidate-title">{sceneName}</h2>
            <p>候选图是否可人工采纳只看图片文件是否存在；QA 结果仅作评分和风险提示，可重验现有图且不重新出图。</p>
            <div className="scene-candidate-summary" aria-label="候选 QA 汇总">
              <span className="passed">QA 无提示 {summary.passed}</span>
              <span>未验证 {summary.not_reviewed}</span>
              <span>QA 未完成 {summary.qa_incomplete}</span>
              <span className="failed">QA 提示 {summary.hard_failed}</span>
            </div>
            {candidates.filter(item => !!item.image_url).length > 1 && (
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
            const gate = candidateGate(candidate)
            const passed = gate.verified && gate.hard.length === 0
            const score = candidateQaScore(candidate)
            const canAdopt = !isCurrent && !!candidate.image_url && !disabled
            return (
              <article className={`scene-candidate-preview${isCurrent ? ' current' : passed ? ' passed' : ' rejected'}`}
                key={candidate.artifact_id}>
                <div className="scene-candidate-image">
                  {candidate.image_url
                    ? <CandidateImage src={candidate.image_url} alt={`${sceneName}候选 ${candidate.attempt ?? ''}`}
                        onOpen={() => onCompare([{
                          src: candidate.image_url!, label: `${sceneName} · 尝试 ${candidate.attempt ?? '—'}`,
                        }])} />
                    : <div className="scene-candidate-empty">图片不可用</div>}
                  <span>{isCurrent ? '当前采用' : gate.hard.length ? 'QA 提示' : passed ? 'QA 无提示' : '待复核/未验证'}</span>
                </div>
                <div className="scene-candidate-meta">
                  <div>
                    <b>尝试 {candidate.attempt ?? '—'}</b>
                    <small title={`${statusTitle(candidate.trust_level)} · ${statusTitle(candidate.status)}`}>
                      {statusLabel(candidate.trust_level)} · {statusLabel(candidate.status)}
                    </small>
                  </div>
                  <strong className={gate.hard.length ? 'failed' : passed ? 'passed' : ''}>
                    QA {score == null ? '—' : score.toFixed(2)}
                  </strong>
                </div>
                {!!gate.hard.length && <div className="error-banner">{gate.hard.slice(0, 3).join('；')} · QA 只评分，不自动拦截人工采纳</div>}
                {!gate.hard.length && !gate.verified && <div className="hint">
                  {gate.reviewState === 'qa_incomplete'
                    ? `上次 QA 未能完整判定${gate.uncertainties.length ? `：${gate.uncertainties.slice(0, 2).join('；')}` : ''}`
                    : '该候选尚未执行新版 QA'}
                </div>}
                {!!gate.warnings.length && <div className="hint">警告：{gate.warnings.slice(0, 3).join('；')}</div>}
                {candidate.evidence && <EvidenceDrawer evidence={candidate.evidence} label="查看 QA 证据"
                  onOpenChange={setEvidenceOpen} />}
                {canAdopt && <button className="btn small primary" type="button" disabled={!!adoptingId}
                  onClick={() => adopt(candidate.artifact_id, [...gate.hard, ...gate.warnings])}>
                  {adoptingId === candidate.artifact_id ? '采纳中…' : '采纳此图'}
                </button>}
                {!isCurrent && !passed && !!candidate.image_url && <div className="scene-candidate-actions">
                  <button className="btn small primary" type="button"
                    disabled={!!reviewingId || !!adoptingId || disabled} onClick={() => void review(candidate)}>
                    {reviewingId === candidate.artifact_id ? '验证中…' : '重新验 QA'}
                  </button>
                  <span className="hint">可直接人工采纳；重新验 QA 不会重新出图</span>
                </div>}
              </article>
            )
          })}
        </div>
        <footer>
          <span>共 {candidates.length} 个候选</span>
          <button className="btn" type="button" onClick={onClose}>完成</button>
        </footer>
        {manualCandidateId && <SceneCandidateManualReviewDialog
          sceneName={sceneName} busy={manualBusy} onClose={() => setManualCandidateId(null)}
          onConfirm={(confirmations, reason) => void manualReviewAndAdopt(manualCandidateId, confirmations, reason)}
        />}
      </section>
    </div>
  )
}

function SceneCandidateManualReviewDialog({ sceneName, busy, onClose, onConfirm }: {
  sceneName: string
  busy: boolean
  onClose: () => void
  onConfirm: (
    confirmations: { person_free: boolean; watermark_free: boolean; forbidden_text_free: boolean; space_type_matches: boolean },
    reason: string,
  ) => void
}) {
  const [confirmations, setConfirmations] = useState({
    person_free: false, watermark_free: false, forbidden_text_free: false, space_type_matches: false,
  })
  const [reason, setReason] = useState('')
  const trapRef = useFocusTrap(true, onClose)
  const allChecked = Object.values(confirmations).every(Boolean)
  return (
    <div className="scene-manual-review-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target && !busy) onClose()
    }}>
      <section ref={trapRef} className="scene-manual-review-dialog" role="dialog" aria-modal="true"
        aria-labelledby="scene-manual-review-title">
        <h3 id="scene-manual-review-title">人工复核「{sceneName}」</h3>
        <p>仅用于“无 QA 或 QA 未完成”的恢复。系统已识别的硬失败无法通过此流程覆盖；复核结果会记入审计证据。</p>
        <div className="scene-manual-review-checks">
          {([
            ['person_free', '画面是纯环境，没有人物或人影'],
            ['watermark_free', '画面没有水印、Logo 或 AI 生成标记'],
            ['forbidden_text_free', '画面没有字幕、角标或禁止的多余文字'],
            ['space_type_matches', '室内/室外与场景定义一致，空间类型匹配'],
          ] as const).map(([key, label]) => <label key={key}>
            <input type="checkbox" checked={confirmations[key]} disabled={busy}
              onChange={event => setConfirmations(current => ({ ...current, [key]: event.target.checked }))} />
            <span>{label}</span>
          </label>)}
        </div>
        <label className="scene-manual-review-reason">
          <span>复核理由（必填）</span>
          <textarea value={reason} disabled={busy} rows={3} maxLength={300}
            placeholder="例：已 1:1 放大查看，四项硬门禁均人工确认通过"
            onChange={event => setReason(event.target.value)} />
        </label>
        <footer>
          <button className="btn" type="button" disabled={busy} onClick={onClose}>取消</button>
          <button className="btn primary" type="button" disabled={busy || !allChecked || reason.trim().length < 4}
            onClick={() => onConfirm(confirmations, reason.trim())}>
            {busy ? '复核采纳中…' : '记录复核并采纳'}
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
            {!!seg.group_qa?.hard_failures?.length && sceneSegmentPrimaryFailed(seg) && (
              <div className="error-banner">主图 QA 提示：{seg.group_qa.hard_failures.join('；')} · QA 只评分，不自动拦截生产引用</div>
            )}
            {!!seg.group_qa?.hard_failures?.length && !sceneSegmentPrimaryFailed(seg) && (
              <div className="hint">附加视角 QA 提示：{seg.group_qa.hard_failures.join('；')} · 主图仍可用于视频</div>
            )}
            {!!seg.group_qa?.warnings?.length && !seg.group_qa?.hard_failures?.length && (
              <div className="hint">软警告：{seg.group_qa.warnings.join('；')}</div>
            )}
            {(seg.views && seg.views.length > 0) ? (
              <div className="scene-view-grid">
                {seg.views.map(view => view.image_url ? (
                  <figure key={view.id} className="scene-view-card">
                    <button type="button" className="scene-image-button" onClick={() => onCompare([{
                      src: view.image_url!, label: `${sceneRangeLabel(seg.ep_start, seg.ep_end)} · ${sceneViewPresentation(view.view_role).label}`,
                    }])} aria-label={`放大查看${sceneViewPresentation(view.view_role).label}`}>
                      <img src={view.image_url} alt={sceneViewPresentation(view.view_role).label} />
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
                        onClick={() => redoView(seg.id!, view.view_role!)}
                      >
                        {redoing === `${seg.id}:${view.view_role}` ? '重做中…' : '重做'}
                      </button>
                    )}
                  </figure>
                ) : null)}
              </div>
            ) : (
              <div style={{ width: 104, textAlign: 'center' }}>
                {seg.image_url
                  ? <img src={seg.image_url} alt={sceneRangeLabel(seg.ep_start, seg.ep_end)}
                      style={{ width: 104, height: 184, objectFit: 'cover', borderRadius: 6, border: '1px solid var(--hairline)' }} />
                  : <div style={{ width: 104, height: 184, borderRadius: 6, border: '1px dashed var(--hairline)',
                                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                                  fontSize: 11, color: 'var(--ink-faint)' }}>无图</div>}
              </div>
            )}
            {seg.ep_end != null && seg.id && (
              <button className="btn small" type="button" disabled={disabled || !!rollingBack}
                onClick={async () => {
                  const reason = window.prompt('回滚会影响新的下游引用，请填写切换原因：', '回滚到此历史通过包')
                  if (!reason?.trim()) return
                  setRollingBack(seg.id!)
                  try { await api.rollbackSceneReference(projectId, sceneName, seg.id!, reason.trim()); toast('场景包已原子回滚'); onChanged?.() }
                  catch (e: unknown) { toast(e instanceof Error ? e.message : String(e), true) }
                  finally { setRollingBack(null) }
                }}>{rollingBack === seg.id ? '回滚中…' : '回滚到此版本'}</button>
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
  const [draft, setDraft] = useState(() => ({
    scene_canonical: scene.scene_canonical,
    location_kind: scene.location_kind || '', space: scene.space || '',
    time_of_day: scene.time_of_day || '', lighting: scene.lighting || '',
    landmarks: (scene.landmarks || []).join('、'),
    forbidden_elements: (scene.forbidden_elements || []).join('、'),
  }))
  const dirty = JSON.stringify(draft) !== JSON.stringify({
    scene_canonical: scene.scene_canonical,
    location_kind: scene.location_kind || '', space: scene.space || '',
    time_of_day: scene.time_of_day || '', lighting: scene.lighting || '',
    landmarks: (scene.landmarks || []).join('、'),
    forbidden_elements: (scene.forbidden_elements || []).join('、'),
  })
  useEffect(() => { onDirtyChange(editing && dirty) }, [dirty, editing])
  useEffect(() => {
    if (!dirty) return
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', beforeUnload)
    return () => window.removeEventListener('beforeunload', beforeUnload)
  }, [dirty])
  const split = (value: string) => value.split(/[、,，;；]/).map(item => item.trim()).filter(Boolean)
  if (!editing) return (
    <section className="scene-anchor-editor">
      <h4>结构化场景锚点</h4>
      <p className="hint">空间：{scene.space || '未拆分'} · 时段：{scene.time_of_day || '未拆分'} · 光线：{scene.lighting || '未拆分'}</p>
      <button className="btn small" type="button" disabled={disabled} onClick={() => setEditing(true)}>逐段修改 / 查看差异</button>
    </section>
  )
  return (
    <section className="scene-anchor-editor">
      <h4>结构化场景锚点（只保存不会生成图片）</h4>
      <div className="scene-anchor-form">
        <label>室内外<select value={draft.location_kind} onChange={e => setDraft(v => ({ ...v, location_kind: e.target.value }))}>
          <option value="">待确认</option><option value="室内">室内</option><option value="室外">室外</option><option value="其他">其他</option>
        </select></label>
        <label>空间<input value={draft.space} onChange={e => setDraft(v => ({ ...v, space: e.target.value }))} /></label>
        <label>时段<input value={draft.time_of_day} onChange={e => setDraft(v => ({ ...v, time_of_day: e.target.value }))} /></label>
        <label>光线<input value={draft.lighting} onChange={e => setDraft(v => ({ ...v, lighting: e.target.value }))} /></label>
        <label>标志物<input value={draft.landmarks} onChange={e => setDraft(v => ({ ...v, landmarks: e.target.value }))} placeholder="用顿号分隔" /></label>
        <label>禁用元素<input value={draft.forbidden_elements} onChange={e => setDraft(v => ({ ...v, forbidden_elements: e.target.value }))} placeholder="用顿号分隔" /></label>
      </div>
      <label>完整锚点<textarea rows={4} value={draft.scene_canonical} onChange={e => setDraft(v => ({ ...v, scene_canonical: e.target.value }))} /></label>
      <div className={draft.scene_canonical.trim().length < 30 || draft.scene_canonical.trim().length > 80 ? 'error-banner' : 'hint'}>
        {draft.scene_canonical.trim().length}/80 字（要求 30~80）{dirty ? ' · 保存后现有图片标记“待重绘”' : ''}
      </div>
      {dirty && <details><summary>查看前后差异</summary><p>原：{scene.scene_canonical}</p><p>新：{draft.scene_canonical}</p></details>}
      <div className="dialog-actions">
        <button className="btn small" type="button" disabled={saving} onClick={() => {
          if (!dirty || window.confirm('放弃尚未保存的场景锚点修改？')) setEditing(false)
        }}>取消</button>
        <button className="btn small primary" type="button" disabled={saving || !dirty || draft.scene_canonical.trim().length < 30 || draft.scene_canonical.trim().length > 80}
          onClick={async () => {
            setSaving(true)
            try {
              await api.editSceneAnchor(projectId, scene.name, {
                expected_version: expectedVersion, ...draft,
                landmarks: split(draft.landmarks), forbidden_elements: split(draft.forbidden_elements),
              })
              toast('场景锚点已保存；现有图片已标记待重绘（未生成、未扣费）')
              setEditing(false); onChanged()
            } catch (e: unknown) { toast(e instanceof Error ? e.message : String(e), true) }
            finally { setSaving(false) }
          }}>仅保存锚点</button>
      </div>
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
  const isOverridden = !!(s.scene_prompt_override || '').trim()
  const effective = s.scene_prompt_effective || ''
  const promptParts = effective.split('。').map(part => part.trim()).filter(Boolean)
  const promptSections = [
    { label: '全局画风', value: promptParts[0] || '未提供' },
    { label: '场景锚点', value: promptParts.find(part => part.includes('场景')) || s.scene_canonical },
    { label: '视角与构图', value: promptParts.find(part => /视角|镜头|构图|竖屏/.test(part)) || '由当前视角包动态追加' },
    { label: '负面约束', value: promptParts.filter(part => /无人物|无文字|无字幕|无水印|logo|禁止/.test(part)).join('；') || '无人物、无文字、无水印、无 Logo' },
  ]
  const draftLength = (draft ?? '').trim().length
  const requestsPeople = draft !== null && /出现人物|有人物|出现人群|包含人群|有人群|出现行人|有行人|角色入镜|主体人物/.test(draft)
  const draftInvalid = draft !== null && draftLength > 0 && (draftLength < 10 || draftLength > 400 || requestsPeople)
  useEffect(() => { onDirtyChange(draft !== null) }, [draft])

  async function save(thenRegen: boolean, valueOverride?: string) {
    setSaving(true)
    try {
      const r = await api.editScenePrompt(projectId, s.name, valueOverride ?? draft ?? '')
      toast(r.reset_to_default ? `「${s.name}」场景图描述已恢复默认` : `「${s.name}」场景图描述已保存`)
      setDraft(null); onChanged()
      if (thenRegen) regenerate()
    } catch (e: unknown) { toast((e as Error).message, true) }
    finally { setSaving(false) }
  }

  return (
    <div style={{ marginTop: 10 }}>
      <label className="f">场景图描述（生成词）{isOverridden ? ' · 已自定义' : ' · 默认（由画风+锚点串合成）'}</label>
      {draft === null ? (
        <>
          <div className="f-misc" style={{ background: 'rgba(91,114,83,0.06)', borderLeft: '3px solid var(--moss)', padding: '6px 10px', borderRadius: '0 6px 6px 0', fontSize: 12.5 }}>
            {promptSections.map(section => <p key={section.label}><b>{section.label}：</b>{section.value}</p>)}
          </div>
          <div className="hint">生成约束与 QA 对照：禁人物 / 禁文字 / 无水印均为入库硬门禁，违反项会标红且总分不能抵消。</div>
          <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
            <button className="btn small" disabled={disabled || saving}
              onClick={() => setDraft(s.scene_prompt_override || s.scene_prompt_effective || '')}>改场景描述</button>
            <button className="btn small" disabled={disabled || saving} onClick={regenerate}>
              {s.ref_image_url ? '重新生成场景视角包' : '单独生成场景视角包'}
            </button>
          </div>
        </>
      ) : (
        <>
          <textarea rows={4} style={{ fontSize: 12.5 }} value={draft} onChange={e => setDraft(e.target.value)}
            placeholder="描述场景定场图：画风、地点、光线时段、陈设、氛围……（10~400 字，不要出现人物）" />
          <div className={draftInvalid ? 'error-banner' : 'hint'}>{draftLength}/400 字
            {draftLength > 0 && (draftLength < 10 || draftLength > 400) ? ' · 自定义描述要求 10~400 字' : ''}
            {requestsPeople ? ' · 纯环境场景不能要求人物/角色入镜' : ''}
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 6, flexWrap: 'wrap' }}>
            <button className="btn small primary" disabled={saving || disabled || draftInvalid} onClick={() => save(true)}>保存并重新出图</button>
            <button className="btn small" disabled={saving || draftInvalid} onClick={() => save(false)}>仅保存</button>
            {isOverridden && <button className="btn small" disabled={saving} onClick={() => {
              if (window.confirm(`确认恢复「${s.name}」的默认描述？此操作只保存描述，不生成图片、不扣费。`)) {
                setDraft(''); void save(false, '')
              }
            }}>恢复默认</button>}
            <button className="btn small ghost" disabled={saving} onClick={() => setDraft(null)}>放弃</button>
          </div>
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
        <p>扫描本身免费。系统会优先复用已有图片并补做验证；只有确需生成新图片时，下一步才会显示费用。</p>
        <div className="pay-scope-actions">
          <button className="btn small" type="button" onClick={() => setSelected(defaults)}>选择建议项</button>
          <button className="btn small ghost" type="button" onClick={() => setSelected([])}>清空</button>
          <span>已选 {selected.length}/{scan.items.length}</span>
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
          <button className="btn primary" type="button" disabled={!selected.length} onClick={() => onGenerate(selected)}>处理已选缺口</button>
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
    const name = window.prompt('合并后的规范场景名：', selectedItems[0].name)?.trim()
    if (!name) return
    const merged: Scene = {
      ...selectedItems[0], name,
      scene_canonical: [...new Set(selectedItems.map(item => item.scene_canonical))].join('；').slice(0, 80),
      discovery_sources: [...new Set(selectedItems.flatMap(item => item.discovery_sources?.length ? item.discovery_sources : [item.name]))],
    }
    setItems(current => [merged, ...current.filter(item => !selected.includes(item.name))])
    setSelected([name])
  }

  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog scene-preview-dialog" role="dialog" aria-modal="true" aria-label="确认场景提取清单">
        <h3>确认场景提取清单</h3>
        <p>先取消不需要项、合并同义场景或修订名称/锚点；下一步才显示服务端真实费用。</p>
        <div className="pay-scope-actions">
          <button className="btn small" type="button" disabled={selectedItems.length < 2} onClick={mergeSelected}>合并勾选项</button>
          <span>已选 {selectedItems.length}/{items.length}</span>
        </div>
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
              <textarea aria-label="场景锚点" rows={3} value={scene.scene_canonical}
                onChange={event => update(index, { scene_canonical: event.target.value })} />
              <small className={scene.scene_canonical.length < 30 || scene.scene_canonical.length > 80 ? 'failed' : ''}>
                锚点 {scene.scene_canonical.length}/80 字（要求 30~80）
              </small>
              {!!scene.discovery_sources?.length && <small>发现依据：{scene.discovery_sources.join('、')}</small>}
            </article>
          ))}
        </div>
        {duplicate && <div className="error-banner">场景名称不能重复；同义场景请合并</div>}
        <div className="dialog-actions">
          <button className="btn" type="button" disabled={busy} onClick={onClose}>取消</button>
          <button className="btn primary" type="button" disabled={busy || invalid} onClick={async () => {
            setBusy(true); try { await onConfirm(selectedItems) } finally { setBusy(false) }
          }}>下一步：费用预检</button>
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
      <img key={retry} src={src} alt={name} onError={() => setFailed(true)}
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
      <img key={retry} src={src} alt={alt} onError={() => setFailed(true)} />
    </button>
  )
}
