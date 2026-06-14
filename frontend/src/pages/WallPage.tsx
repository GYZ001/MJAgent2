import { useCallback, useEffect, useRef, useState } from 'react'
import EpisodeCrumb from '../components/EpisodeCrumb'
import { useEpisode, useNav } from '../App'
import { api, type Shot, type ShotVersion } from '../api'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'

/* ─── 常量 ─── */
const MODE_LABEL: Record<string, string> = {
  FIRST_LAST_FRAME_MODE: '首尾帧',
  REFERENCE_IMAGE_MODE: '参考图',
}
const REF_TYPE_LABEL: Record<string, string> = {
  character: '角色定妆照',
  scene: '场景参考',
  previous_tail: '上镜尾帧复用',
  previous_head: '上镜首帧复用',
  style: '画风锚点',
  prop: '道具参考',
  plot_key_frame: '剧情关键帧',
}
const SOURCE_LABEL: Record<string, string> = {
  bible: '角色圣经',
  generated: 'AI 生成',
  previous_shot: '上镜复用',
}

/* ─── Lightbox 图片预览 ─── */
function Lightbox({ src, alt, onClose }: { src: string; alt: string; onClose: () => void }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', h)
    return () => document.removeEventListener('keydown', h)
  }, [onClose])
  return (
    <div className="lightbox-overlay" onClick={onClose}>
      <div className="lightbox-content" onClick={e => e.stopPropagation()}>
        <button className="lightbox-close" onClick={onClose}>✕</button>
        <img src={src} alt={alt} />
        {alt && <div className="lightbox-caption">{alt}</div>}
      </div>
    </div>
  )
}

/* ─── 收集单镜全部素材（参考图 / 关键帧 / 首尾帧），按类型归类 ─── */
type Material = { key: string; src: string; label: string; group: string }

function collectMaterials(shot: Shot): Material[] {
  const out: Material[] = []
  const seen = new Set<string>()
  const push = (src: string | null | undefined, label: string, group: string, key: string) => {
    if (!src || seen.has(src)) return
    seen.add(src)
    out.push({ key, src, label, group })
  }
  // 已采用 / 最新版本所用的参考图
  const adopted = shot.versions.find(v => v.id === shot.adopted_version_id) || shot.versions[0]
  const refs = adopted?.image_inputs?.reference_images || []
  refs.forEach((r, i) => push(r.image_url, REF_TYPE_LABEL[r.type] || r.type, '参考图', r.id || `ref-${i}`))
  if (adopted?.image_inputs?.first_frame_used) push(adopted.image_inputs.first_frame_src, '首帧', '首尾帧', 'ff-first')
  if (adopted?.image_inputs?.last_frame_used) push(adopted.image_inputs.last_frame_src, '尾帧', '首尾帧', 'ff-last')
  // 关键帧候选（首/尾图）
  shot.scenes
    .filter(s => s.image_url && s.status === 'succeeded')
    .forEach(s => push(s.image_url, `${s.kind === 'head' ? '首' : '尾'}关键帧 v${s.version_no}`, '关键帧', s.id))
  return out
}

/* ─── 单镜素材横向画廊：每张图独立卡片，横向滑动预览 ─── */
function ShotMaterialStrip({ shot, onOpen }: { shot: Shot; onOpen: (src: string, label?: string) => void }) {
  const materials = collectMaterials(shot)
  if (!materials.length) {
    return <div className="material-strip-empty">本镜暂无素材，准备关键帧或参考图后会在此展示</div>
  }
  return (
    <div className="material-strip">
      {materials.map(m => (
        <figure key={m.key} className="material-card" onClick={() => onOpen(m.src, m.label)} title={m.label}>
          <img src={m.src} alt={m.label} loading="lazy" />
          <figcaption>
            <span className="mc-group">{m.group}</span>
            <span className="mc-label">{m.label}</span>
          </figcaption>
        </figure>
      ))}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════
   WallPage
   ═══════════════════════════════════════════════════════════════ */
export default function WallPage() {
  const { episodeId } = useNav()
  const { data: ep, refresh, error } = useEpisode(episodeId || '', 5000)
  const shots = ep?.shots ?? []
  const [idx, setIdx] = useState(0)
  const [toast, setToast] = useState<string | null>(null)
  const [genMask, setGenMask] = useState<Set<string>>(new Set())
  const [sceneMask, setSceneMask] = useState<Set<string>>(new Set())
  const [lightbox, setLightbox] = useState<{ src: string; label?: string } | null>(null)
  const [showGallery, setShowGallery] = useState(true)
  const carouselRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (shots.length && idx >= shots.length) setIdx(shots.length - 1)
  }, [shots.length, idx])

  useEffect(() => {
    if (!carouselRef.current || !shots.length) return
    const el = carouselRef.current.children[idx] as HTMLElement | undefined
    el?.scrollIntoView({ behavior: 'smooth', inline: 'start', block: 'nearest' })
  }, [idx, shots.length])

  const keyframeActive = shots.some(s => s.scene_status === 'generating')
  const videoActive = shots.some(s => s.versions.some(v => v.status === 'queued' || v.status === 'running'))
  const keyframeTimer = useTaskTimer(`episode.${episodeId}.keyframes`, keyframeActive)
  const videoTimer = useTaskTimer(`episode.${episodeId}.videos`, videoActive)

  const openLightbox = useCallback((src: string, label?: string) => {
    setLightbox({ src, label })
  }, [])

  if (!ep) return <div className="empty">展卷中……</div>
  if (error) return <div className="empty">{error}</div>

  const shot = shots[idx]
  const keyframeReady = shots.filter(s => s.scene_status === 'approved' || s.scenes.some(sc => sc.status === 'succeeded')).length
  const videoReady = shots.filter(s => s.versions.some(v => v.status === 'succeeded')).length

  const t = async (fn: () => Promise<unknown>, msg: string) => {
    try { await fn(); setToast(`${msg} 成功`); refresh() }
    catch (e: unknown) { setToast(e instanceof Error ? e.message : String(e)) }
    setTimeout(() => setToast(null), 3200)
  }

  const canGenerate = ep.status === 'confirmed' || ep.status === 'generating' || ep.status === 'done'

  const doGenerateEpisode = async () => {
    if (!canGenerate) { setToast('请先在分镜台确认本集分镜'); return }
    if (!confirm(`即将生成全片 ${shots.length} 个镜头的视频，是否继续？`)) return
    keyframeTimer.start(); videoTimer.start()
    await t(() => api.episodeGenerate(ep.id), '全片生成已启动')
  }

  const doGenerateScene = async (shotId: string, kinds?: ('head' | 'tail')[]) => {
    setSceneMask(m => new Set(m).add(shotId)); keyframeTimer.start()
    try { await api.sceneGenerate(shotId, kinds); refresh() }
    catch (e: unknown) { setToast(e instanceof Error ? e.message : String(e)) }
    finally { setSceneMask(m => { const n = new Set(m); n.delete(shotId); return n }) }
  }

  const doGenerateVideo = async (shotId: string) => {
    setGenMask(m => new Set(m).add(shotId)); videoTimer.start()
    try {
      // 模式由模型按剧本决定（后端 mode_plan），前端不再覆盖。
      await api.shotGenerate(shotId)
      refresh()
    } catch (e: unknown) { setToast(e instanceof Error ? e.message : String(e)) }
    finally { setGenMask(m => { const n = new Set(m); n.delete(shotId); return n }) }
  }

  return (
    <main className="desk wall-page">
      {/* ── 顶栏 ── */}
      <div className="wall-topbar">
        <div className="wall-topbar-left">
          <EpisodeCrumb label="评审墙" view="wall" episodeNo={ep.episode_no} />
          <span className="wall-stats">
            {shots.length} 镜 · 关键帧 {keyframeReady}/{shots.length} · 视频 {videoReady}/{shots.length}
          </span>
        </div>
        <div className="wall-topbar-right">
          <TaskTimer label="关键帧" timer={keyframeTimer} />
          <TaskTimer label="视频" timer={videoTimer} />
          <span className={`stamp ${ep.status === 'confirmed' || ep.status === 'done' ? 'green' : ep.status === 'generating' ? 'gold' : 'grey'}`}>
            {{ confirmed: '已确认', generating: '生成中', done: '已完成', paused_budget: '预算暂停' }[ep.status] ?? ep.status}
          </span>
          {canGenerate && (
            <button className="btn primary small" onClick={doGenerateEpisode}>一键生成所有视频</button>
          )}
        </div>
      </div>

      {/* ── 镜头分页 ── */}
      {shots.length > 0 && (
        <div className="shot-pager">
          <button className="btn ghost small" disabled={idx === 0} onClick={() => setIdx(i => i - 1)}>← 上一镜</button>
          <span className="pg-no">第 {idx + 1} / {shots.length} 镜</span>
          <button className="btn ghost small" disabled={idx >= shots.length - 1} onClick={() => setIdx(i => i + 1)}>下一镜 →</button>
          <div className="shot-chips">
            {shots.map((s, i) => (
              <button
                key={s.id}
                className={`shot-chip${i === idx ? ' active' : ''}${s.scene_status === 'approved' ? ' scene-ok' : ''}${s.versions.some(v => v.status === 'succeeded') ? ' has-video' : ''}`}
                onClick={() => setIdx(i)}
              >{s.shot_no}</button>
            ))}
          </div>
        </div>
      )}

      {/* ── 轮播主体 ── */}
      <div className="shot-carousel" ref={carouselRef}>
        {shot && (
          <div className="shot-slide" key={shot.id}>
            <ShotSlide
              shot={shot}
              episodeStatus={ep.status}
              generating={genMask.has(shot.id)}
              sceneGenerating={sceneMask.has(shot.id)}
              onGenVideo={() => doGenerateVideo(shot.id)}
              onGenScene={doGenerateScene}
              onRefresh={refresh}
              onToast={setToast}
              onOpenImage={openLightbox}
            />
          </div>
        )}
      </div>

      {/* ── 当前镜素材画廊（仅展示本镜，横向可滑动） ── */}
      {shot && (
        <div className="wall-global-gallery">
          <button className="gallery-toggle" onClick={() => setShowGallery(g => !g)}>
            {showGallery ? '▾' : '▸'} 镜 {shot.shot_no} · 素材画廊
          </button>
          {showGallery && <ShotMaterialStrip shot={shot} onOpen={openLightbox} />}
        </div>
      )}

      {toast && <div className="toast">{toast}</div>}
      {lightbox && <Lightbox src={lightbox.src} alt={lightbox.label || ''} onClose={() => setLightbox(null)} />}
    </main>
  )
}

/* ═══════════════════════════════════════════════════════════════
   ShotSlide — 单镜卡片
   ═══════════════════════════════════════════════════════════════ */
function ShotSlide({ shot, episodeStatus, generating, sceneGenerating,
  onGenVideo, onGenScene, onRefresh, onToast, onOpenImage }: {
  shot: Shot; episodeStatus: string; generating: boolean; sceneGenerating: boolean
  onGenVideo: () => void; onGenScene: (id: string, kinds?: ('head' | 'tail')[]) => void
  onRefresh: () => void; onToast: (m: string) => void
  onOpenImage: (src: string, label?: string) => void
}) {
  const adopted = shot.versions.find(v => v.id === shot.adopted_version_id)
  const latest = shot.versions[0]
  const current = adopted || latest
  const hasAnyVersion = shot.versions.length > 0
  // 生成前用模型决策（mode_plan）判断模式；生成后以实际版本的模式为准
  const effectiveMode = current?.image_inputs?.mode || shot.mode_plan?.mode
  const isRefMode = effectiveMode === 'REFERENCE_IMAGE_MODE'
  const needsKf = !isRefMode && (shot.required_keyframes?.length ?? 0) > 0 && shot.scene_status !== 'approved'

  return (
    <div className="slide-card">
      {/* 头部 */}
      <div className="slide-head">
        <span className="sn">镜 {shot.shot_no}</span>
        <span className="meta">{shot.shot_size} · {shot.camera_move} · {shot.duration_s}s · {shot.transition}</span>
        <span className="meta">{shot.scene_setting}</span>
        {shot.continuity_from_prev ? <span className="stamp blue">接上镜</span> : <span className="stamp grey">新场景</span>}
        {adopted ? <span className="stamp green">已采用</span> : hasAnyVersion && <span className="stamp grey">待采用</span>}
        {shot.video_stale && <span className="stamp red">视频需重生</span>}
      </div>

      <div className="slide-body">
        {/* 左栏：分镜信息（台词等） */}
        <div className="slide-left">
          <InfoSection shot={shot} />
        </div>

        {/* 右栏：视频控制 + 模式决策 + 关键帧 */}
        <div className="slide-right">
          <VideoPhase
            shot={shot} episodeStatus={episodeStatus} current={current}
            generating={generating} needsKf={needsKf}
            onGenVideo={onGenVideo} onGenScene={onGenScene}
            onRefresh={onRefresh} onToast={onToast}
          />
          {!isRefMode && (
            <KeyframePhase
              shot={shot} sceneGenerating={sceneGenerating}
              onGenScene={onGenScene} onRefresh={onRefresh} onToast={onToast}
              onOpenImage={onOpenImage}
            />
          )}
        </div>
      </div>
    </div>
  )
}

/* ─── 分镜信息区块 ─── */
function InfoSection({ shot }: { shot: Shot }) {
  return (
    <div className="info-section">
      <div className="info-row"><b>画面</b><span>{shot.action_desc}</span></div>
      <div className="info-row"><b>首帧</b><span className="mono-text">{shot.first_frame_desc}</span></div>
      <div className="info-row"><b>尾帧</b><span className="mono-text">{shot.last_frame_desc}</span></div>
      {shot.narration && <div className="info-row"><b>旁白</b><span>{shot.narration}</span></div>}
      {shot.dialogues.length > 0 && (
        <div className="info-row">
          <b>台词</b>
          <div className="dlg-list">
            {shot.dialogues.map((d, i) => (
              <div key={i} className="dlg-line">
                <span className="dlg-speaker">{d.speaker}</span>
                <span className="dlg-text">{d.line}</span>
                {d.emotion && d.emotion !== '平静' && <span className="dlg-emotion">{d.emotion}</span>}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════
   KeyframePhase — 关键帧管理
   ═══════════════════════════════════════════════════════════════ */
function KeyframePhase({ shot, sceneGenerating, onGenScene, onRefresh, onToast, onOpenImage }: {
  shot: Shot; sceneGenerating: boolean
  onGenScene: (id: string, kinds?: ('head' | 'tail')[]) => void
  onRefresh: () => void; onToast: (m: string) => void
  onOpenImage: (src: string, label?: string) => void
}) {
  const req = shot.required_keyframes ?? []
  const heads = shot.scenes.filter(s => s.kind === 'head')
  const tails = shot.scenes.filter(s => s.kind === 'tail')

  const doApprove = async (sceneId: string, kind: 'head' | 'tail') => {
    try { await api.sceneApprove(shot.id, sceneId, kind); onRefresh() }
    catch (e: unknown) { onToast(e instanceof Error ? e.message : String(e)) }
  }
  const doDelete = async (sceneId: string) => {
    if (!confirm('删除此关键帧？')) return
    try { await api.sceneDelete(sceneId); onRefresh() }
    catch (e: unknown) { onToast(e instanceof Error ? e.message : String(e)) }
  }

  return (
    <div className="kf-section">
      <div className="kf-head">
        <span className="kf-title">首尾帧素材</span>
        {req.length > 0 && shot.scene_status !== 'approved' && (
          <button className="btn small" disabled={sceneGenerating} onClick={() => onGenScene(shot.id, req.length === 2 ? ['head', 'tail'] : req)}>
            {sceneGenerating ? '生成中…' : '准备首尾帧素材'}
          </button>
        )}
        {shot.scene_status === 'approved' && <span className="stamp green">已备齐</span>}
        {shot.scene_status === 'generating' && <span className="stamp gold">生成中</span>}
      </div>

      {(['head', 'tail'] as const).map(kind => {
        const items = kind === 'head' ? heads : tails
        if (!req.includes(kind) && !items.length) return null
        const approvedId = kind === 'head' ? shot.approved_head_scene_id : shot.approved_tail_scene_id
        return (
          <div key={kind}>
            <div className="kf-group-label">{kind === 'head' ? '首帧' : '尾帧'}{approvedId ? ' ✓' : ''}</div>
            <div className="kf-scroll">
              {items.map(s => (
                <SceneThumb key={s.id} scene={s} approved={approvedId === s.id}
                  onApprove={() => doApprove(s.id, kind)} onDelete={() => doDelete(s.id)} onOpenImage={onOpenImage} />
              ))}
              {!items.length && <span className="kf-empty">暂无</span>}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function SceneThumb({ scene, approved, onApprove, onDelete, onOpenImage }: {
  scene: { id: string; kind: string; version_no: number; image_url?: string | null; status: string; error?: string; qa?: { overall: number } | null }
  approved: boolean
  onApprove: () => void; onDelete: () => void
  onOpenImage: (src: string, label?: string) => void
}) {
  if (scene.status === 'generating') return <div className="scene-thumb"><div className="scene-placeholder">生成中…</div></div>
  if (scene.status === 'failed') return <div className="scene-thumb"><div className="scene-placeholder">失败</div></div>
  if (!scene.image_url) return null
  return (
    <div className={`scene-thumb${approved ? ' approved' : ''}`}>
      <img src={scene.image_url} alt="" onClick={() => onOpenImage(scene.image_url!, `${scene.kind === 'head' ? '首' : '尾'}帧 v${scene.version_no}`)} />
      <div className="st-body">
        v{scene.version_no} {scene.qa && <span>QA {scene.qa.overall.toFixed(2)}</span>}
        <div className="st-actions">
          {!approved && <button className="btn small" onClick={onApprove}>采用</button>}
          <button className="btn small ghost" onClick={onDelete}>删除</button>
        </div>
      </div>
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════
   VideoPhase — 视频生成控制
   ═══════════════════════════════════════════════════════════════ */
function VideoPhase({ shot, episodeStatus, current, generating, needsKf,
  onGenVideo, onGenScene, onRefresh, onToast }: {
  shot: Shot; episodeStatus: string; current?: ShotVersion
  generating: boolean; needsKf: boolean
  onGenVideo: () => void; onGenScene: (id: string, kinds?: ('head' | 'tail')[]) => void
  onRefresh: () => void; onToast: (m: string) => void
}) {
  const hasAdopted = !!shot.adopted_version_id
  const modePlan = shot.mode_plan

  const doAdopt = async (vid: string) => {
    try { await api.adoptVersion(shot.id, vid); onRefresh() }
    catch (e: unknown) { onToast(e instanceof Error ? e.message : String(e)) }
  }
  const doDelete = async (vid: string) => {
    if (!confirm('删除此版本？')) return
    try { await api.deleteVersion(vid); onRefresh() }
    catch (e: unknown) { onToast(e instanceof Error ? e.message : String(e)) }
  }
  const doWithCritique = async () => {
    try { await api.shotGenerate(shot.id, undefined, undefined, true); onRefresh() }
    catch (e: unknown) { onToast(e instanceof Error ? e.message : String(e)) }
  }

  // 当前版本的模式决策（已生成后显示实际结果）
  const decision = current?.image_inputs?.mode_decision

  return (
    <div className="video-section">
      {/* 视频播放器 */}
      <div className="video-player-area">
        {current?.video_url ? (
          <video src={current.video_url} controls className="rev-video" />
        ) : (
          <div className="vp-empty">
            <span>{current?.status === 'queued' || current?.status === 'running' ? '⏳ 生成中…' : '暂无视频'}</span>
            {current?.error && <span className="err-text">{current.error}</span>}
          </div>
        )}
      </div>

      {/* 控制面板 */}
      <div className="video-controls">
        {/* 当前版本模式信息 */}
        {decision?.mode && (
          <div className="mode-badge">
            <span className={`mode-tag ${decision.mode === 'REFERENCE_IMAGE_MODE' ? 'ref' : 'ff'}`}>
              {MODE_LABEL[decision.mode] || decision.mode}
            </span>
            {decision.confidence != null && <span className="conf">{(decision.confidence * 100).toFixed(0)}%</span>}
            {decision.llmUsed && <span className="llm-badge">AI</span>}
          </div>
        )}
        {decision?.reason && <div className="mode-reason">{decision.reason}</div>}

        {/* 模型决策（未生成时显示模型按剧本给出的模式与参考图计划，只读） */}
        {!current && modePlan && (
          <div className="plan-preview">
            <div className="plan-head">模型决策（按剧本）</div>
            <div className="plan-mode">
              模式：<b>{MODE_LABEL[modePlan.mode] || modePlan.mode}</b>
              <span className="conf"> 置信度 {(modePlan.confidence * 100).toFixed(0)}%</span>
              {modePlan.llmUsed ? <span className="llm-badge">AI 决策</span> : <span className="conf">规则回退</span>}
            </div>
            <div className="plan-reason">{modePlan.reason}</div>
            {modePlan.referenceImagePlan && modePlan.mode === 'REFERENCE_IMAGE_MODE' && (
              <div className="plan-refs">
                参考图计划：共 {modePlan.referenceImagePlan.totalCount} 张
                {modePlan.referenceImagePlan.reusePreviousSceneCount > 0 &&
                  ` · 复用上镜 ${modePlan.referenceImagePlan.reusePreviousSceneCount} 张`}
                {modePlan.referenceImagePlan.generateNewCount > 0 &&
                  ` · 新生成 ${modePlan.referenceImagePlan.generateNewCount} 张`}
                {modePlan.referenceImagePlan.types.length > 0 && (
                  <div className="plan-types">
                    {modePlan.referenceImagePlan.types.map(t => (
                      <span key={t} className="type-tag">{REF_TYPE_LABEL[t] || t}</span>
                    ))}
                  </div>
                )}
                {modePlan.referenceImagePlan.prompts && modePlan.referenceImagePlan.prompts.length > 0 && (
                  <ol className="plan-prompts">
                    {modePlan.referenceImagePlan.prompts.map((p, i) => (
                      <li key={i}>
                        <span className="type-tag">{REF_TYPE_LABEL[p.type] || p.type}</span>
                        <span className="pp-text">{p.prompt}</span>
                      </li>
                    ))}
                  </ol>
                )}
              </div>
            )}
          </div>
        )}

        {/* 当前版本参考图信息 */}
        {current?.image_inputs?.reference_images && current.image_inputs.reference_images.length > 0 && (
          <div className="ref-info">
            <div className="ref-head">使用参考图 ({current.image_inputs.reference_images.length})</div>
            <div className="ref-list">
              {current.image_inputs.reference_images.map((r, i) => (
                <span key={r.id || i} className="ref-item">
                  {REF_TYPE_LABEL[r.type] || r.type}
                  {r.qualityScore != null && ` · QA ${r.qualityScore.toFixed(2)}`}
                  <span className="ref-src">{SOURCE_LABEL[r.source] || r.source}</span>
                </span>
              ))}
            </div>
            {current.image_inputs.reference_failure_logs && current.image_inputs.reference_failure_logs.length > 0 && (
              <div className="ref-fails">
                {current.image_inputs.reference_failure_logs.map((f, i) => (
                  <span key={i} className="fail-item">{f.type}: {f.error} → {f.fallback}</span>
                ))}
              </div>
            )}
          </div>
        )}

        {/* QA 评分 */}
        {current?.qa && (
          <div className={`qa-row ${current.qa.overall >= 0.6 ? 'pass' : 'fail'}`}>
            QA {current.qa.overall.toFixed(2)}
            {current.qa.issues.length > 0 && <span> · {current.qa.issues.join('；')}</span>}
          </div>
        )}

        {/* 操作按钮 */}
        <div className="action-row">
          {(episodeStatus === 'confirmed' || episodeStatus === 'generating' || episodeStatus === 'done') && (
            <button className="btn primary small" disabled={generating || needsKf} onClick={onGenVideo}>
              {generating ? '生成中…' : needsKf ? '需先备关键帧' : hasAdopted ? '重生成视频' : '生成本镜视频'}
            </button>
          )}
          {needsKf && (
            <button className="btn small" onClick={() => onGenScene(shot.id, shot.required_keyframes)}>
              准备关键帧
            </button>
          )}
          {current?.video_url && !hasAdopted && (
            <button className="btn small" onClick={() => doAdopt(current.id)}>采用此版</button>
          )}
          {current?.video_url && (
            <a className="btn small ghost" href={current.video_url} download={`${shot.shot_no}.mp4`}>导出</a>
          )}
          {hasAdopted && current?.qa && (
            <>
              <button className="btn small" onClick={doWithCritique}>带评语重生</button>
              <button className="btn small" onClick={() => onGenVideo()}>改词重生</button>
            </>
          )}
          {shot.versions.length > 1 && (
            <button className="btn small ghost" onClick={() => onGenVideo()}>原词重抽</button>
          )}
          {current && !hasAdopted && (
            <button className="btn small ghost" onClick={() => doDelete(current.id)}>删除此版</button>
          )}
        </div>

        {/* 版本历史 */}
        {shot.versions.length > 1 && (
          <div className="version-history">
            <div className="vh-head">历史版本 ({shot.versions.length})</div>
            {shot.versions.map(v => (
              <div key={v.id} className={`vh-item${v.id === shot.adopted_version_id ? ' adopted' : ''}${v.id === current?.id ? ' current' : ''}`}>
                <span className="vh-no">v{v.version_no}</span>
                <span className={`stamp ${v.status === 'succeeded' ? 'green' : v.status === 'failed' ? 'red' : 'gold'}`}>
                  {v.status === 'succeeded' ? '成功' : v.status === 'failed' ? '失败' : '生成中'}
                </span>
                {v.image_inputs?.mode && <span className="vh-mode">{MODE_LABEL[v.image_inputs.mode] || v.image_inputs.mode}</span>}
                {v.qa && <span className="vh-qa">{v.qa.overall.toFixed(2)}</span>}
                {v.id !== shot.adopted_version_id && v.status === 'succeeded' && (
                  <button className="btn small ghost" onClick={() => doAdopt(v.id)}>采用</button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
