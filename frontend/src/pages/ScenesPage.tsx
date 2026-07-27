import { useEffect, useId, useRef, useState } from 'react'
import { api, Scene, SceneRefSegment, SceneReferenceCandidate } from '../api'
import { useNav, useProject } from '../App'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'
import SearchField from '../components/SearchField'
import EvidenceDrawer from '../components/harness/EvidenceDrawer'
import GenerationParamsDialog from '../components/GenerationParamsDialog'
import PrepSubnav from '../components/PrepSubnav'
import { useFillPageSize } from '../hooks/useFillPageSize'

export default function ScenesPage() {
  const { projectId, toast } = useNav()
  const { data: p, refresh, error, loading } = useProject(projectId!, undefined, 'scenes')
  const [busy, setBusy] = useState(false)
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)
  const [detailSceneName, setDetailSceneName] = useState<string | null>(null)
  const [paramsSceneName, setParamsSceneName] = useState<string | null>(null)
  const [candidatePreview, setCandidatePreview] = useState<{
    sceneName: string
    candidates: SceneReferenceCandidate[]
    adoptedArtifactId?: string | null
  } | null>(null)
  const pageSize = useFillPageSize({ minCardWidth: 270, rows: 3, floor: 8, ceiling: 24 })
  const sceneTimer = useTaskTimer(`project.${projectId}.scene_refs`, p?.scene_refs_status === 'running')

  const scenes = p?.bible?.scenes ?? []
  const query = search.trim()
  const filtered = query ? scenes.filter(s => s.name.includes(query)) : scenes
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize))
  const curPage = Math.min(page, pageCount - 1)

  useEffect(() => {
    if (page > pageCount - 1) setPage(Math.max(0, pageCount - 1))
  }, [page, pageCount])

  if (error && !p) return <div className="empty">{error}</div>
  if (loading && !p) return <div className="empty">展卷中……</div>
  if (!p) return <div className="empty">展卷中……</div>

  const act = async (fn: () => Promise<unknown>, doneMsg?: string) => {
    setBusy(true)
    try { await fn(); if (doneMsg) toast(doneMsg); refresh() }
    catch (e: unknown) { toast((e as Error).message, true) }
    finally { setBusy(false) }
  }

  const paged = filtered.slice(curPage * pageSize, curPage * pageSize + pageSize)
  const generating = p.scene_refs_status === 'running'
  const hasBible = !!p.bible
  const detailScene = detailSceneName ? scenes.find(scene => scene.name === detailSceneName) ?? null : null
  const paramsScene = paramsSceneName ? scenes.find(scene => scene.name === paramsSceneName) ?? null : null

  return (
    <>
      <header className="desk-head">
        <div className="crumb">书房 / {(() => {
          const n = (p.name || '').trim()
          if (!n) return '《未命名》'
          return n.startsWith('《') && n.endsWith('》') ? n : `《${n}》`
        })()}</div>
        <PrepSubnav current="scenes" />
        <h1>场景库 <span className="sub">以视觉资产为中心管理场景锚点、版本与跨集一致性</span></h1>
        <hr className="rule" />
      </header>

      <section className="card">
        <h3>场景图素材库
          <span className="hint">从原文提取的规范场景 · 分镜的场景必须落在库内 · ¥0.2/张</span>
        </h3>
        {!hasBible && (
          <div className="hint">请先到「人物谱」生成角色圣经；场景圣经会在人物谱定稿后自动生成。</div>
        )}
        {hasBible && (
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
            {!scenes.length && !generating && (
              <button className="btn primary" disabled={busy}
                onClick={() => act(() => api.genSceneBible(p.id), '已开始生成场景圣经与场景图')}>
                生成场景圣经与场景图
              </button>
            )}
            {scenes.length > 0 && !generating && (
              <button className="btn" disabled={busy}
                onClick={() => act(() => api.genSceneRefs(p.id), '已开始检查并补齐场景图')}>
                检查并补齐场景图
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
        {p.scene_refs_status === 'failed' && (
          <div className="error-banner">场景图生成失败（原始错误如下，不做静默兜底）：{'\n'}{p.scene_refs_error}</div>
        )}
        {scenes.length > 0 && (
          <div className="hint" style={{ marginTop: 10 }}>
            分镜阶段会自动把每个镜头的场景收敛到这些规范场景之一；剧本里出现、库里没有且戏份足够的新场景会在分镜前自动补入库并出图。
          </div>
        )}
      </section>

      {scenes.length > 0 && (
        <section className="card scene-library">
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', margin: '4px 0 12px' }}>
            <SearchField value={search} onChange={value => { setSearch(value); setPage(0) }}
              placeholder="搜索场景名…" ariaLabel="搜索场景" className="library-search" />
            <span style={{ fontSize: 12.5, color: 'var(--ink-faint)' }}>
              共 {scenes.length} 个场景{query ? ` · 命中 ${filtered.length}` : ''}
            </span>
          </div>
          <div className="figure-grid">
            {paged.map(s => {
              const fitting = generating && (!p.scene_refs_target || p.scene_refs_target === s.name)
              const approvedRefs = (s.scene_refs ?? []).filter(sceneRefPassedQa)
              const activeRef = approvedRefs.find(ref => ref.image_url === s.ref_image_url)
                ?? [...approvedRefs].sort((a, b) => b.ep_start - a.ep_start)[0]
              const qaOverall = activeRef?.qa_overall
              const adoptedImageUrl = activeRef?.image_url
              return (
                <article key={s.name} className="figure scene-card">
                  <div className="f-name">{s.name}
                    {s.location_kind ? <span className="f-role">{s.location_kind}</span> : null}
                    {fitting ? <span className="stamp gold">生成中</span>
                      : adoptedImageUrl ? <span className="stamp green">已采纳</span>
                        : s.ref_image_url ? <span className="stamp gold">待通过 QA</span>
                          : <span className="stamp grey">未出图</span>}
                  </div>
                  {adoptedImageUrl && (
                    <div className="scene-visual"><img src={adoptedImageUrl} alt={s.name}
                      style={{ opacity: fitting ? 0.45 : 1, transition: 'opacity 0.3s' }} /></div>
                  )}
                  <div className="scene-card-summary">
                    {typeof qaOverall === 'number' && (
                      <QaLine overall={qaOverall} issues={activeRef?.qa?.issues} />
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
      {candidatePreview && (
        <SceneCandidateModal
          projectId={p.id}
          {...candidatePreview}
          disabled={busy || generating}
          onClose={() => setCandidatePreview(null)}
          onAdopted={(sceneName, candidates, adoptedArtifactId) => {
            setCandidatePreview({ sceneName, candidates, adoptedArtifactId })
            refresh()
          }}
        />
      )}
      {detailScene && (
        <SceneDetailModal
          projectId={p.id}
          scene={detailScene}
          disabled={busy || generating}
          onClose={() => setDetailSceneName(null)}
          onChanged={refresh}
          onShowCandidates={(sceneName, candidates, adoptedArtifactId) => {
            setDetailSceneName(null)
            setCandidatePreview({ sceneName, candidates, adoptedArtifactId })
          }}
          onShowParams={sceneName => {
            setDetailSceneName(null)
            setParamsSceneName(sceneName)
          }}
        />
      )}
      {paramsScene && (
        <GenerationParamsDialog
          title={`${paramsScene.name} · 生成参数与重绘`}
          subtitle="查看或调整场景图生成词，修改后可保存并重新出图。"
          onClose={() => setParamsSceneName(null)}
        >
          <ScenePromptBlock projectId={p.id} scene={paramsScene} disabled={busy || generating}
            onChanged={refresh}
            regenerate={() => act(
              () => api.genSceneRefs(p.id, paramsScene.name),
              `正在为「${paramsScene.name}」重新出图`,
            )} />
        </GenerationParamsDialog>
      )}
    </>
  )
}

const SCENE_QA_PASS_SCORE = 0.6

function sceneRefPassedQa(ref: SceneRefSegment): boolean {
  if (!ref.image_url) return false
  const evaluationPassed = ref.evidence?.evaluations.some(evaluation =>
    evaluation.evaluator_name.includes('consistency_qa') && !!evaluation.hard_gate_passed)
  const scorePassed = typeof ref.qa_overall === 'number' && ref.qa_overall >= SCENE_QA_PASS_SCORE
  const adopted = !ref.artifact_id || ref.evidence?.status === 'approved'
  return adopted && (scorePassed || !!evaluationPassed)
}

function candidateQaScore(candidate: SceneReferenceCandidate): number | null {
  const evaluation = candidate.evidence?.evaluations.find(item =>
    item.evaluator_name.includes('consistency_qa'))
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
    label: viewRole || '其他视角',
    description: '同一场景的补充参考机位。',
  }
}

function SceneDetailModal({
  projectId, scene, disabled, onClose, onChanged, onShowCandidates, onShowParams,
}: {
  projectId: string
  scene: Scene
  disabled?: boolean
  onClose: () => void
  onChanged: () => void
  onShowCandidates: (
    sceneName: string,
    candidates: SceneReferenceCandidate[],
    adoptedArtifactId?: string | null,
  ) => void
  onShowParams: (sceneName: string) => void
}) {
  const titleId = useId()
  const closeRef = useRef<HTMLButtonElement>(null)
  const approvedRefs = (scene.scene_refs ?? []).filter(sceneRefPassedQa)
  const activeRef = approvedRefs.find(ref => ref.image_url === scene.ref_image_url)
    ?? [...approvedRefs].sort((a, b) => b.ep_start - a.ep_start)[0]
  const candidates = scene.scene_candidates ?? []

  useEffect(() => {
    const previousOverflow = document.body.style.overflow
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.body.style.overflow = 'hidden'
    window.addEventListener('keydown', closeOnEscape)
    closeRef.current?.focus()
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', closeOnEscape)
    }
  }, [onClose])

  return (
    <div className="scene-detail-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section className="scene-detail-modal" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <header className="scene-detail-modal-head">
          <div>
            <span className="eyebrow">SCENE DETAILS</span>
            <h2 id={titleId}>{scene.name}</h2>
            <p>查看这个场景的参考机位、适用版本和生成设置。</p>
          </div>
          <button ref={closeRef} type="button" aria-label="关闭场景详情" onClick={onClose}>×</button>
        </header>
        <div className="scene-detail-modal-body">
          <section className="scene-detail-anchor">
            <b>场景定位{scene.location_kind ? ` · ${scene.location_kind}` : ''}</b>
            <p>{scene.scene_canonical}</p>
          </section>

          <section className="scene-view-guide" aria-label="场景视角说明">
            <h3>这些视角有什么区别？</h3>
            <div>
              <article>
                <b>全景视角 <small>原“建立”</small></b>
                <p>{SCENE_VIEW_PRESENTATION.establishing.description}</p>
              </article>
              <article>
                <b>对向视角 <small>原“反打”</small></b>
                <p>{SCENE_VIEW_PRESENTATION.reverse_angle.description}</p>
              </article>
            </div>
          </section>

          {approvedRefs.length > 0 ? (
            <SceneRefStrip
              projectId={projectId}
              sceneName={scene.name}
              segments={approvedRefs}
              disabled={disabled}
              onChanged={onChanged}
            />
          ) : (
            <div className="scene-detail-empty">这个场景还没有通过 QA 的参考视角。</div>
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
          </div>
          <button className="btn primary" type="button" onClick={onClose}>完成</button>
        </footer>
      </section>
    </div>
  )
}

function SceneCandidateModal({
  projectId, sceneName, candidates, adoptedArtifactId, disabled, onClose, onAdopted,
}: {
  projectId: string
  sceneName: string
  candidates: SceneReferenceCandidate[]
  adoptedArtifactId?: string | null
  disabled?: boolean
  onClose: () => void
  onAdopted: (sceneName: string, candidates: SceneReferenceCandidate[], adoptedArtifactId: string) => void
}) {
  const { toast } = useNav()
  const [adoptingId, setAdoptingId] = useState<string | null>(null)

  useEffect(() => {
    const previousOverflow = document.body.style.overflow
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.body.style.overflow = 'hidden'
    window.addEventListener('keydown', closeOnEscape)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', closeOnEscape)
    }
  }, [onClose])

  const adopt = async (artifactId: string) => {
    if (!window.confirm(`确认将此候选采纳为「${sceneName}」的场景库主图？将替换当前采用版本。`)) return
    setAdoptingId(artifactId)
    try {
      await api.adoptSceneCandidate(projectId, sceneName, artifactId)
      toast(`已采纳「${sceneName}」的候选图`)
      const nextCandidates = candidates.map(item =>
        item.artifact_id === artifactId
          ? { ...item, status: 'approved' }
          : item.status === 'approved'
            ? { ...item, status: 'superseded' }
            : item,
      )
      onAdopted(sceneName, nextCandidates, artifactId)
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
      <section className={`scene-candidate-modal${candidates.length === 1 ? ' single' : candidates.length === 2 ? ' double' : ''}`}
        role="dialog" aria-modal="true"
        aria-labelledby="scene-candidate-title">
        <header className="scene-candidate-modal-head">
          <div>
            <span className="eyebrow">SCENE CANDIDATES</span>
            <h2 id="scene-candidate-title">{sceneName}</h2>
            <p>可手动采纳任一候选为主图；检查并补齐时若候选超过 4 张会自动采纳最高分。</p>
          </div>
          <button type="button" aria-label="关闭候选预览" onClick={onClose}>×</button>
        </header>
        <div className="scene-candidate-modal-body">
          {candidates.map(candidate => {
            const isCurrent = candidate.artifact_id === adoptedArtifactId
            const passed = candidate.status === 'approved'
            const score = candidateQaScore(candidate)
            const canAdopt = !isCurrent && !!candidate.image_url && !disabled
            return (
              <article className={`scene-candidate-preview${isCurrent ? ' current' : passed ? ' passed' : ' rejected'}`}
                key={candidate.artifact_id}>
                <div className="scene-candidate-image">
                  {candidate.image_url
                    ? <img src={candidate.image_url} alt={`${sceneName}候选 ${candidate.attempt ?? ''}`} />
                    : <div className="scene-candidate-empty">图片不可用</div>}
                  <span>{isCurrent ? '当前采用' : passed ? 'QA 通过' : '未采用'}</span>
                </div>
                <div className="scene-candidate-meta">
                  <div>
                    <b>尝试 {candidate.attempt ?? '—'}</b>
                    <small>{candidate.trust_level} · {candidate.status}</small>
                  </div>
                  <strong className={score != null && score >= SCENE_QA_PASS_SCORE ? 'passed' : 'failed'}>
                    QA {score == null ? '—' : score.toFixed(2)}
                  </strong>
                </div>
                {candidate.evidence && <EvidenceDrawer evidence={candidate.evidence} label="查看 QA 证据" />}
                {canAdopt && (
                  <button
                    className="btn small primary"
                    type="button"
                    disabled={!!adoptingId}
                    onClick={() => adopt(candidate.artifact_id)}
                  >
                    {adoptingId === candidate.artifact_id ? '采纳中…' : '采纳此图'}
                  </button>
                )}
              </article>
            )
          })}
        </div>
        <footer>
          <span>共 {candidates.length} 个候选</span>
          <button className="btn" type="button" onClick={onClose}>完成</button>
        </footer>
      </section>
    </div>
  )
}

function QaLine({ overall, issues }: { overall: number; issues?: string[] }) {
  const color = overall >= 0.75 ? 'var(--moss)' : overall >= 0.6 ? 'var(--gold, #b8860b)' : 'var(--cinnabar)'
  return (
    <div className="scene-qa-line">
      <span>QA：<b style={{ color }}>{overall.toFixed(2)}</b></span>
      {issues?.length ? <span style={{ color: 'var(--ink-faint)' }}>　{issues.slice(0, 2).join('；')}</span> : null}
    </div>
  )
}

function sceneRangeLabel(start: number, end: number | null): string {
  if (end == null) return `第${start}集起`
  return start === end ? `第${start}集` : `第${start}~${end}集`
}

function SceneRefStrip({ projectId, sceneName, segments, disabled, onChanged }: {
  projectId: string
  sceneName: string
  segments: SceneRefSegment[]
  disabled?: boolean
  onChanged?: () => void
}) {
  const { toast } = useNav()
  const [redoing, setRedoing] = useState<string | null>(null)
  const sorted = [...segments].sort((a, b) => a.ep_start - b.ep_start)
  const current = sorted.filter(seg => seg.ep_end == null).at(-1) || sorted.at(-1)

  const redoView = async (sceneRefId: string, viewRole: string) => {
    const label = sceneViewPresentation(viewRole).label
    if (!window.confirm(`确认重做「${sceneName}」的${label}？将重新付费生成并复跑整包 QA。`)) return
    setRedoing(`${sceneRefId}:${viewRole}`)
    try {
      await api.regenerateSceneView(projectId, sceneName, sceneRefId, viewRole)
      toast(`${label}已重做`)
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
      </div>
      <div className="scene-version-track">
        {sorted.map((seg, i) => (
          <article key={seg.id || i} className="scene-version-card">
            <div className="scene-version-meta">
              {sceneRangeLabel(seg.ep_start, seg.ep_end)}
              {seg.pack_status ? ` · ${seg.pack_status}` : ''}
            </div>
            {(seg.views && seg.views.length > 0) ? (
              <div className="scene-view-grid">
                {seg.views.map(view => view.image_url ? (
                  <figure key={view.id} className="scene-view-card">
                    <img src={view.image_url} alt={view.view_role || 'view'} />
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
          </article>
        ))}
      </div>
    </section>
  )
}

function ScenePromptBlock({ projectId, scene: s, disabled, onChanged, regenerate }: {
  projectId: string; scene: Scene; disabled: boolean
  onChanged: () => void; regenerate: () => void
}) {
  const { toast } = useNav()
  const [draft, setDraft] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const isOverridden = !!(s.scene_prompt_override || '').trim()

  async function save(thenRegen: boolean) {
    setSaving(true)
    try {
      const r = await api.editScenePrompt(projectId, s.name, draft ?? '')
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
            {s.scene_prompt_effective}
          </div>
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
          <div style={{ display: 'flex', gap: 8, marginTop: 6, flexWrap: 'wrap' }}>
            <button className="btn small primary" disabled={saving || disabled} onClick={() => save(true)}>保存并重新出图</button>
            <button className="btn small" disabled={saving} onClick={() => save(false)}>仅保存</button>
            {isOverridden && <button className="btn small" disabled={saving} onClick={() => { setDraft('') }} title="清空后保存即恢复默认">清空</button>}
            <button className="btn small ghost" disabled={saving} onClick={() => setDraft(null)}>放弃</button>
          </div>
        </>
      )}
    </div>
  )
}
