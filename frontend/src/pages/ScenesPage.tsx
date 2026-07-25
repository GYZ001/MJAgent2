import { useEffect, useState } from 'react'
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
  const paramsScene = paramsSceneName ? scenes.find(scene => scene.name === paramsSceneName) ?? null : null

  return (
    <>
      <header className="desk-head">
        <div className="crumb">书房 / 《{p.name}》</div>
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
              const candidates = s.scene_candidates ?? []
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
                  <div className="scene-quality-row">
                    {typeof qaOverall === 'number' && (
                      <QaLine overall={qaOverall} issues={activeRef?.qa?.issues} />
                    )}
                    {candidates.length > 0 && (
                      <button className="scene-candidates-trigger" type="button" onClick={() => setCandidatePreview({
                        sceneName: s.name,
                        candidates,
                        adoptedArtifactId: activeRef?.artifact_id,
                      })}>
                        查看候选 <span>{candidates.length}</span>
                      </button>
                    )}
                  </div>
                  {approvedRefs.length > 1 && (
                    <SceneRefStrip segments={approvedRefs} />
                  )}
                  <label className="f">场景锚点串（30~60 字，定稿后锁定）</label>
                  <div className="f-anchor">{s.scene_canonical}</div>
                  <div className="asset-params-action">
                    <button className="asset-params-trigger" type="button" onClick={() => setParamsSceneName(s.name)}>
                      生成参数与重绘 <span aria-hidden="true">→</span>
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
        <SceneCandidateModal {...candidatePreview} onClose={() => setCandidatePreview(null)} />
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

function SceneCandidateModal({ sceneName, candidates, adoptedArtifactId, onClose }: {
  sceneName: string
  candidates: SceneReferenceCandidate[]
  adoptedArtifactId?: string | null
  onClose: () => void
}) {
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
            <p>候选仅供追溯；场景库主图只采用 QA 通过并已提交的版本。</p>
          </div>
          <button type="button" aria-label="关闭候选预览" onClick={onClose}>×</button>
        </header>
        <div className="scene-candidate-modal-body">
          {candidates.map(candidate => {
            const isCurrent = candidate.artifact_id === adoptedArtifactId
            const passed = candidate.status === 'approved'
            const score = candidateQaScore(candidate)
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

function SceneRefStrip({ segments }: { segments: SceneRefSegment[] }) {
  const sorted = [...segments].sort((a, b) => a.ep_start - b.ep_start)
  return (
    <div style={{ margin: '2px 0 8px' }}>
      <label className="f">场景图分段（按适用集横向预览）</label>
      <div style={{ display: 'flex', gap: 8, overflowX: 'auto', paddingBottom: 4 }}>
        {sorted.map((seg, i) => (
          <div key={i} style={{ flex: '0 0 auto', width: 104, textAlign: 'center' }}>
            {seg.image_url
              ? <img src={seg.image_url} alt={sceneRangeLabel(seg.ep_start, seg.ep_end)}
                  style={{ width: 104, height: 184, objectFit: 'cover', borderRadius: 6, border: '1px solid var(--hairline)' }} />
              : <div style={{ width: 104, height: 184, borderRadius: 6, border: '1px dashed var(--hairline)',
                              display: 'flex', alignItems: 'center', justifyContent: 'center',
                              fontSize: 11, color: 'var(--ink-faint)' }}>无图</div>}
            <div style={{ fontSize: 11, color: 'var(--ink-soft)', marginTop: 3 }}>{sceneRangeLabel(seg.ep_start, seg.ep_end)}</div>
          </div>
        ))}
      </div>
    </div>
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
              {s.ref_image_url ? '重新出图' : '单独出图'}
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
