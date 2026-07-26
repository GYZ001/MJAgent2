import { useCallback, useEffect, useRef, useState } from 'react'
import EpisodeCrumb from '../components/EpisodeCrumb'
import { useEpisode, useNav } from '../App'
import { api, type Shot, type ShotVersion, type ReferenceImage } from '../api'
import { ServerTaskTimer, TaskTimer, useTaskTimer } from '../components/TaskTimer'
import { countAdoptedVideos, formatPipelineSummary, shotVideoState } from '../shotStatus'
import AsyncButton from '../components/AsyncButton'
import QueryState from '../components/QueryState'
import VideoSupervisorPanel from '../components/VideoSupervisorPanel'

/* ─── 常量 ─── */
type ReviewTab = 'text' | 'references' | 'videos'

const REVIEW_TABS: { id: ReviewTab; label: string }[] = [
  { id: 'text', label: '文字内容' },
  { id: 'references', label: '参考图' },
  { id: 'videos', label: '视频对比' },
]

function videoVersionStatusLabel(version: ShotVersion, adopted: boolean): string {
  if (adopted) return '已采纳'
  if (version.status === 'succeeded' && version.video_url) return '待采纳'
  if (version.status === 'failed') return '生成失败'
  if (
    version.status === 'queued'
    || version.status === 'running'
    || version.status === 'waiting_provider'
  ) return '生成中'
  return '待生成'
}

function commaList(value?: string[]): string {
  return (value ?? []).join('、') || '无'
}

function truncateText(value: string, max = 800): string {
  return value.length > max ? `${value.slice(0, max)}…` : value
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

/* ─── 取本镜参考图：当前版本尚未写入时，回退到最近一个有图的版本 ─── */
export function currentVersionRefs(shot: Shot): {
  versionId: string
  versionNo: number
  refs: ReferenceImage[]
  isFallback: boolean
} | null {
  const adopted = shot.versions.find(x => x.id === shot.adopted_version_id)
  const hasRefs = (version: ShotVersion) => (version.image_inputs?.reference_images?.length ?? 0) > 0
  // A running version streams references into image_inputs one by one. Prefer
  // that live gallery once its first image exists; otherwise an adopted version
  // would hide all progress until the new video finished.
  const live = shot.versions.find(version =>
    ['queued', 'running', 'waiting_provider'].includes(version.status) && hasRefs(version)
  )
  const preferred = live || adopted || shot.versions[0]
  const v = preferred && hasRefs(preferred)
    ? preferred
    : shot.versions.find(hasRefs) || preferred
  if (!v) return null
  return {
    versionId: v.id,
    versionNo: v.version_no,
    refs: v.image_inputs?.reference_images ?? [],
    isFallback: !!preferred && v.id !== preferred.id,
  }
}

function refSourceLabel(ref: ReferenceImage): string {
  const viewLabels: Record<string, string> = {
    front_full: '正面全身',
    three_quarter: '3/4 面',
    profile: '侧面',
    back_full: '背面全身',
    face_closeup: '面部特写',
    establishing: '建立',
    reverse_angle: '反打',
    action_zone: '动作区',
  }
  if (ref.type === 'plot_key_frame' || ref.slot_key === 'narrative_keyframe') return '关键帧'
  if (ref.type === 'previous_shot_frame' || ref.source === 'previous_shot' || ref.source === 'previous_shot_frame') {
    return '上镜衔接帧'
  }
  if (ref.type === 'character' || ref.entity_type === 'character') {
    const view = ref.view_role ? viewLabels[ref.view_role] || ref.view_role : ''
    return view ? `人物参考 · ${view}` : '人物参考'
  }
  if (ref.type === 'scene' || ref.entity_type === 'scene') {
    const view = ref.view_role ? viewLabels[ref.view_role] || ref.view_role : ''
    return view ? `场景参考 · ${view}` : '场景参考'
  }
  return ({
    seedream_generated: '生成参考图', asset_library: '角色定妆照',
    previous_shot: '上镜衔接帧', previous_shot_frame: '上镜衔接帧',
  } as Record<string, string>)[ref.source ?? ''] ?? (ref.source || '参考图')
}

function refPurposeBucket(ref: ReferenceImage): 'video' | 'evidence' | 'discarded' {
  if (ref.deleted || (!ref.selectedForSeedance && ref.rejectReason)) return 'discarded'
  const purposes = ref.purposes || []
  if (ref.selectedForSeedance && !ref.deleted) return 'video'
  if (purposes.includes('qa_anchor') || purposes.includes('keyframe_seed')) return 'evidence'
  if (!ref.selectedForSeedance) return 'discarded'
  return 'video'
}

/** 评审墙三类分组：视频实际输入 / QA 依据 / 废弃候选 */
export function classifyReferenceBuckets(refs: ReferenceImage[]) {
  return {
    video: refs.filter(r => refPurposeBucket(r) === 'video'),
    evidence: refs.filter(r => refPurposeBucket(r) === 'evidence'),
    discarded: refs.filter(r => refPurposeBucket(r) === 'discarded'),
  }
}

export { refSourceLabel, refPurposeBucket }

function rejectReasonLabel(reason?: string | null): string {
  if (!reason) return '分数不足'
  // 用户只关心分数：内部代号一律折叠为「分数不足」；手动废弃另议
  if (reason === 'missing_quality_score') return '缺少质检分'
  if (
    reason === 'quality_below_threshold'
    || reason === 'quality_issue_blocks_reuse'
    || reason === 'consistency_drift'
    || reason === 'consistency_drift_unfixable'
    || reason === 'duplicate_character_suppressed'
  ) return '分数不足'
  return '分数不足'
}

function refScore(r: ReferenceImage): number | null {
  const s = r.qualityScore ?? r.qa?.overall
  return typeof s === 'number' ? s : null
}

const QA_KEEP_THRESHOLD = 0.8

/* ─── 单张参考图卡片：图 + QA 打分 + 来源 + 操作 ─── */
function RefCard({ r, onOpen, onAction, actionLabel, discarded }: {
  r: ReferenceImage; onOpen: (src: string, label?: string) => void
  onAction?: () => void; actionLabel: string; discarded?: boolean
}) {
  const score = refScore(r)
  const src = r.image_url || undefined
  const label = refSourceLabel(r)
  const isKeyframe = r.type === 'plot_key_frame' || r.slot_key === 'narrative_keyframe'
  const purposeHint = (r.purposes || []).includes('video_input')
    ? '视频输入'
    : (r.purposes || []).includes('qa_anchor')
      ? 'QA 依据'
      : null
  return (
    <figure className={`material-card${discarded ? ' material-card-discarded' : ''}${isKeyframe ? ' material-card-keyframe' : ''}`} title={label}>
      <div className="mc-thumb" onClick={() => src && onOpen(src, label)}>
        {src ? <img src={src} alt={label} loading="lazy" /> : <div className="mc-noimg">无图</div>}
        {isKeyframe && <span className="mc-keyframe-badge">关键帧</span>}
        {score != null && (
          <span className={`mc-qa-badge${score < QA_KEEP_THRESHOLD ? ' bad' : ''}`}>QA {score.toFixed(2)}</span>
        )}
        {r.qa?.status === 'unverified' && <span className="mc-qa-badge bad">未验证</span>}
      </div>
      <figcaption>
        <span className="mc-label">{label}</span>
        {purposeHint && <span className="mc-purpose">{purposeHint}</span>}
        {discarded
          ? <span className="mc-reject">{r.deleted ? '已手动废弃' : rejectReasonLabel(r.rejectReason)}</span>
          : (r.rejectReason
            ? <span className="mc-reject warn">兜底·{rejectReasonLabel(r.rejectReason)}</span>
            : null)}
        {onAction && (
          <button className={`mc-action ${discarded ? 'restore' : 'discard'}`} onClick={onAction}>
            {actionLabel}
          </button>
        )}
      </figcaption>
    </figure>
  )
}

/* ─── 单镜素材画廊：视频输入 / QA 依据 / 废弃候选 ─── */
function ShotMaterialGallery({ shot, onOpen, onRefresh, onToast }: {
  shot: Shot; onOpen: (src: string, label?: string) => void
  onRefresh: () => void; onToast: (m: string) => void
}) {
  const data = currentVersionRefs(shot)
  const refs = data?.refs ?? []
  const versionId = data?.versionId
  const videoInputs = refs.filter(r => refPurposeBucket(r) === 'video')
  const evidence = refs.filter(r => refPurposeBucket(r) === 'evidence')
  const discarded = refs.filter(r => refPurposeBucket(r) === 'discarded')

  const act = (fn: () => Promise<unknown>) => async () => {
    try { await fn(); onRefresh() }
    catch (e: unknown) { onToast(e instanceof Error ? e.message : String(e)) }
  }

  const renderStrip = (items: ReferenceImage[], opts: { discarded?: boolean; actionLabel: string; restore?: boolean }) => (
    items.length ? (
      <div className="material-strip">
        {items.map(r => (
          <RefCard
            key={r.id}
            r={r}
            onOpen={onOpen}
            discarded={opts.discarded}
            actionLabel={opts.actionLabel}
            onAction={versionId ? act(async () => {
              if (opts.restore) {
                const qaRejected = !!r.rejectReason
                if (qaRejected) {
                  const reason = window.prompt(
                    `该图曾因「${rejectReasonLabel(r.rejectReason)}」被淘汰。\n请填写覆盖理由（将写入审计记录），留空则取消：`,
                    '',
                  )
                  if (!reason?.trim()) return
                  await api.restoreReferenceImage(versionId, r.id, reason.trim())
                } else if (!window.confirm('确认恢复使用该参考图？')) {
                  return
                } else {
                  await api.restoreReferenceImage(versionId, r.id)
                }
              } else {
                await api.discardReferenceImage(versionId, r.id)
              }
            }) : undefined}
          />
        ))}
      </div>
    ) : null
  )

  return (
    <>
      {data?.isFallback && (
        <div className="material-fallback-note">
          当前版本的参考图尚未就绪，暂时展示最近一次有图版本 v{data.versionNo}
        </div>
      )}
      <div className="material-section-head">视频实际输入 · {videoInputs.length}</div>
      {videoInputs.length ? renderStrip(videoInputs, { actionLabel: '废弃' }) : (
        <div className="material-strip-empty" aria-label="本镜暂无参考图">
          <span className="material-empty-frame" />
          <span className="material-empty-frame" />
          <span className="material-empty-frame" />
        </div>
      )}

      {evidence.length > 0 && (
        <div className="evidence-gallery">
          <div className="discard-gallery-head">
            关键帧生成 / QA 依据 · {evidence.length} 张
            <span className="discard-gallery-hint">人物/场景多视角真值，默认不直接喂视频模型</span>
          </div>
          {renderStrip(evidence, { actionLabel: '废弃' })}
        </div>
      )}

      {discarded.length > 0 && (
        <div className="discard-gallery">
          <div className="discard-gallery-head">
            废弃候选 · {discarded.length} 张
            <span className="discard-gallery-hint">分数不足 / 已手动废弃，不会喂给视频模型</span>
          </div>
          {renderStrip(discarded, { discarded: true, actionLabel: '恢复使用', restore: true })}
        </div>
      )}
    </>
  )
}

/* ═══════════════════════════════════════════════════════════════
   WallPage
   ═══════════════════════════════════════════════════════════════ */
export default function WallPage() {
  const { episodeId } = useNav()
  const { data: ep, refresh, error, loading } = useEpisode(episodeId || '', 'wall')
  const shots = ep?.shots ?? []
  const [idx, setIdx] = useState(0)
  const [toast, setToast] = useState<string | null>(null)
  const [genMask, setGenMask] = useState<Set<string>>(new Set())
  const [reviewShot, setReviewShot] = useState<Shot | null>(null)
  const [lightbox, setLightbox] = useState<{ src: string; label?: string } | null>(null)
  const [clearMenuOpen, setClearMenuOpen] = useState(false)
  const [genMenuOpen, setGenMenuOpen] = useState(false)
  const [supervisorKickoff, setSupervisorKickoff] = useState(false)
  const [supervisorPanelDismissed, setSupervisorPanelDismissed] = useState(false)
  const toastTimerRef = useRef<number>()

  const showToast = useCallback((msg: string) => {
    setToast(msg)
    if (toastTimerRef.current) window.clearTimeout(toastTimerRef.current)
    toastTimerRef.current = window.setTimeout(() => setToast(null), 3200)
  }, [])

  const selectedSummaryShot = shots[idx]

  const loadReviewShot = useCallback(async (shotId: string) => {
    const detail = await api.get(`/shots/${shotId}/review`) as Shot
    setReviewShot(detail)
    return detail
  }, [])

  useEffect(() => {
    const shotId = selectedSummaryShot?.id
    if (!shotId) {
      setReviewShot(null)
      return
    }
    let active = true
    setReviewShot(null)
    api.get(`/shots/${shotId}/review`)
      .then((detail: Shot) => { if (active) setReviewShot(detail) })
      .catch((reason: unknown) => {
        if (active) showToast(reason instanceof Error ? reason.message : String(reason))
      })
    return () => { active = false }
  }, [selectedSummaryShot?.id, showToast])

  useEffect(() => {
    if (shots.length && idx >= shots.length) setIdx(shots.length - 1)
  }, [shots.length, idx])

  useEffect(() => {
    if (!episodeId || !shots.length) return
    try {
      const raw = sessionStorage.getItem('manju:select_shot')
      if (!raw) return
      const payload = JSON.parse(raw) as { episodeId?: string; shotId?: string }
      if (payload.episodeId && payload.episodeId !== episodeId) return
      const found = shots.findIndex(s => s.id === payload.shotId)
      if (found >= 0) {
        setIdx(found)
        sessionStorage.removeItem('manju:select_shot')
      }
    } catch { /* ignore */ }
  }, [episodeId, shots])

  const supervisorPhase = typeof ep?.video_supervisor?.phase === 'string'
    ? ep.video_supervisor.phase
    : ''
  const supervisorTerminal = [
    'SUCCEEDED_COVERED', 'COMPLETED_DEADLINE_FALLBACK',
    'PARTIAL_NO_USABLE_CANDIDATE', 'FAILED_CLOSED', 'CANCELLED',
  ].includes(supervisorPhase)
  const supervisorTaskRunning = ep?.video_supervisor?.task_running === true
  const supervisorRunFailed = ep?.video_supervisor?.run_status === 'FAILED'
  const supervisorLive = Boolean(
    supervisorKickoff
    || (!supervisorTerminal && supervisorTaskRunning),
  )
  const videoActive = supervisorLive || shots.some(s =>
    (s.pipeline != null && ['queued', 'running', 'waiting', 'waiting_provider', 'blocked'].includes(s.pipeline.pipeline_status))
    || (!s.pipeline && s.versions.some(v => v.status === 'queued' || v.status === 'running' || v.status === 'waiting_provider'))
  )
  const videoTimer = useTaskTimer(`episode.${episodeId}.videos`, videoActive)

  useEffect(() => {
    if (supervisorTerminal || supervisorTaskRunning) {
      setSupervisorKickoff(false)
    }
  }, [supervisorTerminal, supervisorTaskRunning])

  useEffect(() => {
    setSupervisorPanelDismissed(false)
  }, [episodeId])

  useEffect(() => {
    if (supervisorKickoff || supervisorTaskRunning) {
      setSupervisorPanelDismissed(false)
    }
  }, [supervisorKickoff, supervisorTaskRunning])

  const openLightbox = useCallback((src: string, label?: string) => {
    setLightbox({ src, label })
  }, [])

  if (error && !ep) return <QueryState loading={false} error={error} hasData={false}>{null}</QueryState>
  if (!ep) return <QueryState loading={loading !== false} error={null} hasData={false}>{null}</QueryState>

  const shot = reviewShot?.id === selectedSummaryShot?.id ? reviewShot : selectedSummaryShot
  const videoReady = countAdoptedVideos(shots)

  const refreshAll = async () => {
    const next = await refresh()
    const shotId = next?.shots?.[idx]?.id ?? selectedSummaryShot?.id
    if (shotId) await loadReviewShot(shotId)
  }

  const t = async (fn: () => Promise<unknown>, msg: string) => {
    try {
      await fn()
      showToast(`${msg} 成功`)
      void refreshAll()
      return true
    } catch (e: unknown) {
      showToast(e instanceof Error ? e.message : String(e))
      return false
    }
  }

  const canGenerate = ep.status === 'confirmed' || ep.status === 'generating' || ep.status === 'done'
  const adoptedCount = videoReady

  const doGenerateEpisode = async () => {
    if (!canGenerate) { showToast('请先在分镜台确认本集分镜'); return }
    const ok = confirm(
      `即将为全片 ${shots.length} 个镜头生成新视频版本（快速模式，不自动补齐）。\n\n`
      + `影响范围：\n`
      + `· 每个镜头会新建（或复用）一个视频版本并入队\n`
      + `· 当前已采用的 ${adoptedCount} 个成片在新版本成功前会保留\n`
      + `· 新版本成功并通过技术门禁后，系统会自动比较并可能切换采用版\n`
      + `· 若任务失败，原采用结果不变，可继续交付\n\n`
      + `是否继续？`,
    )
    if (!ok) return
    videoTimer.start()
    const started = await t(() => api.episodeGenerate(ep.id), '全片生成已启动')
    if (!started) videoTimer.clear()
  }

  const doCompleteEpisode = async () => {
    if (!canGenerate) { showToast('请先在分镜台确认本集分镜'); return }
    const budget = window.prompt('授权预算上限（元，默认 150）', '150')
    if (budget === null) return
    const wallH = window.prompt('授权时长墙（小时，默认 4）', '4')
    if (wallH === null) return
    const allowEdit = window.confirm('是否授权 Supervisor 微调分镜时长？（默认否，点取消=不授权）')
    const startOk = window.confirm(
      `确认启动「补齐到全片可用」？\n`
      + `预算 ¥${budget || 150} · ${wallH || 4}h · 微调分镜：${allowEdit ? '是' : '否'}\n`
      + `只处理尚未采用的 ${Math.max(0, shots.length - adoptedCount)} 镜；已有采用的 ${adoptedCount} 镜会原样保留，不重生、不换版。\n`
      + `不会自动拼接成片或创建交付包。`,
    )
    if (!startOk) return
    showToast('正在启动全片补齐 Supervisor…')
    setSupervisorKickoff(true)
    videoTimer.start()
    try {
      await api.episodeVideoCompletion(ep.id, {
        mode: 'fresh',
        budget_cap_cny: Number(budget) || 150,
        wall_clock_cap_s: (Number(wallH) || 4) * 3600,
        allow_fallback_adopt: true,
        allow_storyboard_edit: allowEdit,
      })
      showToast('全片补齐 Supervisor 已启动 — 请看顶部进度面板')
      void refreshAll()
    } catch (e: unknown) {
      setSupervisorKickoff(false)
      videoTimer.clear()
      showToast(e instanceof Error ? e.message : String(e))
    }
  }

  const doClearEpisode = async () => {
    setClearMenuOpen(false)
    if (!confirm(
      `确认清空第 ${ep.episode_no} 集全部 ${shots.length} 镜的参考图、视频与模型分析结果？\n`
      + `同时会停止全片补齐 Supervisor。\n`
      + `（操作不可恢复）`,
    )) return
    setSupervisorKickoff(false)
    videoTimer.clear()
    await t(() => api.clearEpisodeArtifacts(ep.id), '本集已清空')
  }

  const doClearShot = async () => {
    setClearMenuOpen(false)
    if (!shot) return
    if (!confirm(
      `确认清空第 ${shot.shot_no} 镜的参考图、视频与模型分析结果？\n`
      + `（操作不可恢复）`,
    )) return
    await t(() => api.clearShotArtifacts(shot.id), `镜 ${shot.shot_no} 已清空`)
  }

  const doGenerateVideo = async (
    shotId: string,
    opts?: { promptOverride?: string; reroll?: boolean; withCritique?: boolean; actionLabel?: string },
  ) => {
    setGenMask(m => new Set(m).add(shotId)); videoTimer.start()
    try {
      const actionLabel = opts?.actionLabel || '视频生成'
      showToast(`${actionLabel}已提交，正在处理…`)
      const r = await api.shotGenerate(shotId, opts?.promptOverride, opts?.reroll, opts?.withCritique) as { reused?: boolean }
      showToast(r.reused
        ? `${actionLabel}未新建任务：当前内容未变化，已复用已有版本；如需强制重出请点「原词重抽」`
        : `${actionLabel}已开始，正在生成新版本`)
      void refreshAll()
    } catch (e: unknown) {
      showToast(e instanceof Error ? e.message : String(e))
      videoTimer.clear()
    }
    finally { setGenMask(m => { const n = new Set(m); n.delete(shotId); return n }) }
  }

  return (
    <div className="wall-page">
      {/* ── 顶栏 ── */}
      <div className="wall-topbar">
        <div className="wall-topbar-left">
          <EpisodeCrumb label="评审墙" view="wall" episodeNo={ep.episode_no} />
          <span className="wall-stats">
            {formatPipelineSummary(ep.pipeline_summary, shots.length)}
          </span>
        </div>
        <div className="wall-topbar-right">
          {typeof ep.video_supervisor?.started_at === 'number'
            ? <ServerTaskTimer
                label="视频"
                startedAt={ep.video_supervisor.started_at}
                finishedAt={typeof ep.video_supervisor.finished_at === 'number' ? ep.video_supervisor.finished_at : null}
                running={supervisorTaskRunning}
              />
            : <TaskTimer label="视频" timer={videoTimer} />}
          <span className={`stamp ${ep.status === 'done' && !supervisorLive ? 'green' : (ep.status === 'confirmed' && !supervisorLive) ? 'green' : (ep.status === 'generating' || supervisorLive) ? 'gold' : 'grey'}`}>
            {supervisorRunFailed && !supervisorLive
              ? 'Supervisor失败'
              : supervisorTerminal
              ? (supervisorPhase === 'SUCCEEDED_COVERED' ? '补齐完成'
                : supervisorPhase === 'COMPLETED_DEADLINE_FALLBACK' ? '截止已收口'
                : supervisorPhase === 'PARTIAL_NO_USABLE_CANDIDATE' ? '部分收口'
                : supervisorPhase === 'FAILED_CLOSED' ? '安全停止'
                : '已取消')
              : supervisorLive
              ? (supervisorPhase === 'WAITING_AUTHORIZATION' ? '等待授权'
                : supervisorPhase === 'WAITING_HUMAN' ? '待人工'
                : supervisorPhase === 'PAUSED_EXTERNAL' || supervisorPhase === 'PAUSED_BUDGET' ? '已暂停'
                : '补齐中')
              : ({ confirmed: '已确认', generating: '生成中', done: '已完成', paused_budget: '预算暂停' }[ep.status] ?? ep.status)}
          </span>
          {canGenerate && (
            <div className="clear-menu-wrap">
              <button className="btn primary small" onClick={() => setGenMenuOpen(o => !o)}>
                一键生成所有视频 ▾
              </button>
              {genMenuOpen && (
                <>
                  <div className="clear-menu-backdrop" onClick={() => setGenMenuOpen(false)} />
                  <div className="clear-menu">
                    <div className="clear-menu-hint">选择生成模式</div>
                    <button className="clear-menu-item" onClick={() => { setGenMenuOpen(false); void doGenerateEpisode() }}>
                      快速生成全部（不自动补齐）
                    </button>
                    <button className="clear-menu-item" onClick={() => { setGenMenuOpen(false); void doCompleteEpisode() }}>
                      补齐到全片可用（Supervisor）
                    </button>
                  </div>
                </>
              )}
            </div>
          )}
          <div className="clear-menu-wrap">
            <button className="btn ghost small danger" onClick={() => setClearMenuOpen(o => !o)}>清空 ▾</button>
            {clearMenuOpen && (
              <>
                <div className="clear-menu-backdrop" onClick={() => setClearMenuOpen(false)} />
                <div className="clear-menu">
          <div className="clear-menu-hint">清空参考图 / 视频 / 模型分析</div>
                  <button className="clear-menu-item" onClick={doClearShot} disabled={!shot}>
                    清空本镜{shot ? `（镜 ${shot.shot_no}）` : ''}
                  </button>
                  <button className="clear-menu-item" onClick={doClearEpisode}>
                    清空本集（{shots.length} 镜）
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {!supervisorPanelDismissed && (supervisorLive || ep.video_supervisor) && (
        <VideoSupervisorPanel
          api={api}
          episodeId={ep.id}
          runId={ep.active_video_run_id}
          supervisor={ep.video_supervisor as import('../components/VideoSupervisorPanel').VideoSupervisorSnapshot | null}
          running={supervisorTaskRunning || supervisorKickoff}
          onChanged={refreshAll}
          onToast={showToast}
          onDismiss={() => setSupervisorPanelDismissed(true)}
        />
      )}

      {/* ── 镜头状态导航：快速定位问题镜头 ── */}
      {shots.length > 0 && (
        <nav className="wall-shot-rail" aria-label="镜头状态导航">
          {shots.map((item, itemIdx) => {
            const state = shotVideoState(item)
            return (
              <button
                key={item.id}
                type="button"
                data-grade={state.grade || undefined}
                className={`${itemIdx === idx ? 'active ' : ''}${state.railClass}`}
                onClick={() => setIdx(itemIdx)}
                aria-current={itemIdx === idx ? 'true' : undefined}
                title={
                  state.grade === 'B' && state.fallbackReason
                    ? `镜 ${item.shot_no} · ${state.label}：${state.fallbackReason}`
                    : `镜 ${item.shot_no} · ${state.label}`
                }
              >
                <b>{String(item.shot_no).padStart(2, '0')}</b>
                <span>{state.label}</span>
                {state.continuityDegraded ? <i className="continuity-degraded-badge">衔接降级</i> : null}
              </button>
            )
          })}
        </nav>
      )}

      {/* ── 轮播主体 ── */}
      <div className="shot-carousel">
        {shot && (
          <div className="shot-slide" key={shot.id}>
            <ShotSlide
              shot={shot}
              episodeStatus={ep.status}
              generating={genMask.has(shot.id) || shot.versions.some(v =>
                v.status === 'queued' || v.status === 'running' || v.status === 'waiting_provider'
              )}
              onGenVideo={(opts) => doGenerateVideo(shot.id, opts)}
              onOpen={openLightbox}
              onRefresh={() => { void refreshAll() }}
              onToast={showToast}
            />
          </div>
        )}
      </div>

      {/* ── 镜头分页 ── */}
      {shots.length > 0 && (
        <div className="shot-pager">
          <button className="btn ghost small" disabled={idx === 0} onClick={() => setIdx(i => i - 1)}>← 上一镜</button>
          <span className="pg-no">第 {idx + 1} / {shots.length} 镜</span>
          <button className="btn ghost small" disabled={idx >= shots.length - 1} onClick={() => setIdx(i => i + 1)}>下一镜 →</button>
        </div>
      )}

      {toast && <div className="toast">{toast}</div>}
      {lightbox && <Lightbox src={lightbox.src} alt={lightbox.label || ''} onClose={() => setLightbox(null)} />}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════
   ShotSlide — 单镜卡片
   ═══════════════════════════════════════════════════════════════ */
function ShotSlide({ shot, episodeStatus, generating,
  onGenVideo, onOpen, onRefresh, onToast }: {
  shot: Shot; episodeStatus: string; generating: boolean
  onGenVideo: (opts?: { promptOverride?: string; reroll?: boolean; withCritique?: boolean; actionLabel?: string }) => void
  onOpen: (src: string, label?: string) => void
  onRefresh: () => void; onToast: (m: string) => void
}) {
  const [previewVersionId, setPreviewVersionId] = useState<string | null>(null)
  const [reviewTab, setReviewTab] = useState<ReviewTab>('text')
  const videoState = shotVideoState(shot)
  const adopted = videoState.adopted
  const current = adopted || videoState.latest
  const preview = previewVersionId
    ? shot.versions.find(v => v.id === previewVersionId)
    : undefined
  const playing = preview || videoState.playing

  useEffect(() => {
    setPreviewVersionId(null)
    setReviewTab('text')
  }, [shot.id])

  useEffect(() => {
    if (previewVersionId && !shot.versions.some(v => v.id === previewVersionId)) {
      setPreviewVersionId(null)
    }
  }, [previewVersionId, shot.versions])

  return (
    <div className="slide-card">
      {/* 头部 */}
      <div className="slide-head">
        <span className="sn">镜 {shot.shot_no}</span>
        <span className="meta">{shot.shot_size} · {shot.camera_move} · {shot.duration_s}s · {shot.transition}</span>
        <span className="meta">{shot.scene_setting}</span>
        {shot.continuity_mode
          ? <span className="stamp blue">{shot.continuity_mode}</span>
          : shot.continuity_from_prev ? <span className="stamp blue">接上镜</span> : <span className="stamp grey">新场景</span>}
        <span className={`stamp ${
          videoState.grade === 'B' || videoState.railClass === 'fallback' ? 'gold'
            : videoState.phase === 'adopted' ? 'green'
              : videoState.phase === 'generating' ? 'gold'
                : videoState.phase === 'generation_failed' ? 'red'
                  : 'grey'
        }`}>{videoState.label}</span>
        {videoState.continuityDegraded ? <span className="continuity-degraded-badge">衔接已降级</span> : null}
        {videoState.grade === 'B' && videoState.fallbackReason ? (
          <span className="meta" title={videoState.fallbackReason}>兜底原因：{videoState.fallbackReason}</span>
        ) : null}
      </div>

      <div className="slide-body">
        {/* 左栏：以成片为第一视觉焦点 */}
        <div className="slide-right">
          <VideoPlayer current={playing} previewing={!!preview && preview.id !== current?.id} phase={videoState.phase} />
        </div>

        {/* 右栏：横向菜单切换评审依据，操作按钮固定在底部 */}
        <div className="slide-left">
          <nav className="review-tabs" role="tablist" aria-label={`镜 ${shot.shot_no} 评审内容`}>
            {REVIEW_TABS.map(tab => (
              <button
                key={tab.id}
                id={`review-tab-${shot.id}-${tab.id}`}
                type="button"
                role="tab"
                className={reviewTab === tab.id ? 'active' : ''}
                aria-selected={reviewTab === tab.id}
                aria-controls={`review-panel-${shot.id}-${tab.id}`}
                onClick={() => setReviewTab(tab.id)}
              >
                {tab.label}
              </button>
            ))}
          </nav>

          <div
            key={reviewTab}
            id={`review-panel-${shot.id}-${reviewTab}`}
            className="review-tab-content"
            role="tabpanel"
            aria-labelledby={`review-tab-${shot.id}-${reviewTab}`}
          >
            {reviewTab === 'text' && <InfoSection shot={shot} current={current} />}

            {reviewTab === 'references' && (
              <section className="candidate-compare">
                <div className="candidate-compare-head">
                  <b>本镜参考图</b>
                  <span>生成视频时，系统会先生成或复用参考图，再提交视频模型</span>
                </div>
                <ShotMaterialGallery
                  shot={shot}
                  onOpen={onOpen}
                  onRefresh={onRefresh}
                  onToast={onToast}
                />
              </section>
            )}

            {reviewTab === 'videos' && (
              <VideoControls
                mode="comparison"
                shot={shot} episodeStatus={episodeStatus} current={current}
                previewVersionId={preview?.id ?? null}
                generating={generating}
                onGenVideo={onGenVideo}
                onPreview={setPreviewVersionId}
                onRefresh={onRefresh} onToast={onToast}
              />
            )}
          </div>

          <VideoControls
            mode="actions"
            shot={shot} episodeStatus={episodeStatus} current={current}
            previewVersionId={preview?.id ?? null}
            generating={generating}
            onGenVideo={onGenVideo}
            onPreview={setPreviewVersionId}
            onRefresh={onRefresh} onToast={onToast}
          />
        </div>
      </div>
    </div>
  )
}

/* ─── 分镜信息区块 ─── */
function ScriptMeta({ label, value }: { label: string; value: string }) {
  return (
    <div className="script-meta-item">
      <span className="script-meta-label">{label}</span>
      <span className="script-meta-value">{value}</span>
    </div>
  )
}

function InfoSection({ shot, current }: { shot: Shot; current?: ShotVersion }) {
  const dialogueText = shot.dialogues
    .map(d => `${d.speaker}：${d.line}${d.emotion && d.emotion !== '平静' ? `（${d.emotion}）` : ''}`)
    .join('\n')
  const promptPreview = current?.prompt_text || shot.prompt_preview || ''
  const qa = current?.qa

  return (
    <div className="info-section">
      <section className="script-card">
        <div className="script-card-head">原文摘录</div>
        <div className={`script-source${shot.source_excerpt ? '' : ' empty'}`}>
          {shot.source_excerpt || '暂无原文摘录'}
        </div>
      </section>

      <section className="script-card">
        <div className="script-card-head">镜头信息</div>
        <div className="script-meta-grid">
          <ScriptMeta label="场景" value={shot.scene_setting} />
          <ScriptMeta label="角色" value={shot.characters.join('、') || '无'} />
          <ScriptMeta label="时长" value={`${shot.duration_s}s`} />
          <ScriptMeta label="镜头" value={`${shot.shot_size} / ${shot.camera_move}`} />
          <ScriptMeta label="转场" value={shot.transition} />
          <ScriptMeta label="衔接" value={shot.continuity_mode || (shot.continuity_from_prev ? '接上镜' : '新场景')} />
          <ScriptMeta label="可见角色" value={commaList(shot.characters_visible)} />
          <ScriptMeta label="声音角色" value={commaList(shot.audio_cast)} />
        </div>
      </section>

      <section className="script-card">
        <div className="script-card-head">Seedance 连续性</div>
        <div className="script-block">
          <div className="script-paragraph">
            <span className="script-label">状态链</span>
            <p>{shot.state_in || shot.first_frame_desc || '未设置'} → {shot.primary_action || shot.action_desc || '未设置'} → {shot.state_out || shot.last_frame_desc || '未设置'}</p>
          </div>
          <div className="script-paragraph">
            <span className="script-label">可见/声音</span>
            <p>画面：{commaList(shot.characters_visible)}；声音：{commaList(shot.audio_cast)}</p>
          </div>
          {promptPreview && (
            <div className="script-paragraph">
              <span className="script-label">prompt_text 预览</span>
              <pre className="script-dialogues">{truncateText(promptPreview)}</pre>
            </div>
          )}
          {(qa?.observed_state_out || qa?.failure_types?.length) && (
            <div className="script-paragraph">
              <span className="script-label">QA 连续性</span>
              <p>{qa.observed_state_out ? `observed_state_out：${qa.observed_state_out}` : ''}</p>
              {!!qa.failure_types?.length && <p>failure_types：{qa.failure_types.join('、')}</p>}
            </div>
          )}
        </div>
      </section>

      <section className="script-card">
        <div className="script-card-head">镜头脚本</div>
        <div className="script-block">
          <div className="script-paragraph">
            <span className="script-label">画面</span>
            <p>{shot.action_desc}</p>
          </div>
          {!!shot.narration && (
            <div className="script-paragraph">
              <span className="script-label">旁白</span>
              <p>{shot.narration}</p>
            </div>
          )}
          {!!shot.dialogues.length && (
            <div className="script-paragraph">
              <span className="script-label">台词</span>
              <pre className="script-dialogues">{dialogueText}</pre>
            </div>
          )}
        </div>
      </section>
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════
   VideoPlayer — 视频播放（右栏，仅播放器）
   ═══════════════════════════════════════════════════════════════ */
function VideoPlayer({ current, previewing, phase }: {
  current?: ShotVersion; previewing?: boolean; phase?: string
}) {
  const emptyLabel = phase === 'generating' || current?.status === 'queued' || current?.status === 'running'
    ? '⏳ 生成中…'
    : phase === 'generation_failed' || current?.status === 'failed'
      ? '生成失败'
      : '暂无视频'
  return (
    <div className="video-player-area">
      {previewing && current && (
        <div className="vp-preview-badge">预览 v{current.version_no}</div>
      )}
      {current?.video_url ? (
        <video key={current.id} src={current.video_url} controls preload="metadata" className="rev-video" />
      ) : (
        <div className="vp-empty">
          <span>{emptyLabel}</span>
          {current?.error && <span className="err-text">{current.error}</span>}
        </div>
      )}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════
   VideoControls — 操作按钮 + 版本历史（左栏）
   ═══════════════════════════════════════════════════════════════ */
function VideoControls({ mode, shot, episodeStatus, current, previewVersionId, generating,
  onGenVideo, onPreview, onRefresh, onToast }: {
  mode: 'actions' | 'comparison'
  shot: Shot; episodeStatus: string; current?: ShotVersion
  previewVersionId: string | null
  generating: boolean
  onGenVideo: (opts?: { promptOverride?: string; reroll?: boolean; withCritique?: boolean; actionLabel?: string }) => void
  onPreview: (versionId: string) => void
  onRefresh: () => void; onToast: (m: string) => void
}) {
  const hasAdopted = !!shot.adopted_version_id
  const disabled = generating
  const hasActiveVideoTask = shot.versions.some(
    version => version.status === 'queued' || version.status === 'running' || version.status === 'waiting_provider',
  )
  const videoState = shotVideoState(shot)

  const doAdopt = async (vid: string) => {
    const reason = window.prompt('请填写采用理由（应说明质量、成本或版本比较）', '技术门禁通过，横向比较后质量分最佳')
    if (!reason?.trim()) return
    try { await api.adoptVersion(shot.id, vid, reason.trim()); onRefresh() }
    catch (e: unknown) { onToast(e instanceof Error ? e.message : String(e)) }
  }
  const doDelete = async (vid: string) => {
    if (!confirm('删除此版本？')) return
    try { await api.deleteVersion(vid); onRefresh() }
    catch (e: unknown) { onToast(e instanceof Error ? e.message : String(e)) }
  }
  const doWithCritique = () => onGenVideo({ withCritique: true, reroll: true, actionLabel: '带评语重生' })
  const doStop = async () => {
    try {
      const result = await api.stopShotVideo(shot.id)
      if (result.stopped_count === 0) {
        onToast('视频任务已经结束，无需停止')
      } else if (result.provider_may_continue) {
        onToast('已停止本地任务；视频平台已接单，平台侧可能仍继续执行并计费')
      } else {
        onToast('视频任务已停止，可稍后重新生成')
      }
      onRefresh()
    } catch (error: unknown) {
      onToast(error instanceof Error ? error.message : String(error))
    }
  }
  const doRewrite = () => {
    const initial = (current?.prompt_text || '').trim()
    const next = window.prompt('请输入新的生成词。留空则取消。', initial)
    if (next == null) return
    const promptOverride = next.trim()
    if (!promptOverride) {
      onToast('已取消改词重生')
      return
    }
    if (promptOverride === initial) {
      onToast('生成词未修改；如需强制重出，请点「原词重抽」')
      return
    }
    onGenVideo({ promptOverride, actionLabel: '改词重生' })
  }

  if (mode === 'actions') {
    return (
      <div className="review-action-footer" aria-label="本镜操作">
        <div className="action-row">
          {(episodeStatus === 'confirmed' || episodeStatus === 'generating' || episodeStatus === 'done') && (
            <button className="btn primary small" disabled={disabled}
              onClick={() => onGenVideo({
                // 已有采用版时强制重抽，避免幂等复用旧版本形成死循环
                reroll: hasAdopted,
                actionLabel: hasAdopted ? '重生成视频' : '生成本镜视频',
              })}>
              {generating ? '生成中…' : hasAdopted ? '重生成视频' : '生成本镜视频'}
            </button>
          )}
          {hasActiveVideoTask && (
            <AsyncButton
              className="btn ghost small danger"
              busyLabel="停止中…"
              onAction={doStop}
              title="立即停止本镜当前视频任务；已被视频平台接单的任务可能无法撤回"
            >
              停止任务
            </AsyncButton>
          )}
          {current?.video_url && !hasAdopted && (
            <button className="btn small" disabled={disabled} onClick={() => doAdopt(current.id)}>采用此版</button>
          )}
          {current?.video_url && (
            <a className={`btn small ghost${disabled ? ' is-disabled' : ''}`} aria-disabled={disabled}
              onClick={e => { if (disabled) e.preventDefault() }}
              href={current.video_url} download={`${shot.shot_no}.mp4`}>导出</a>
          )}
          {hasAdopted && <button className="btn small" disabled={disabled} onClick={doRewrite}>改词重生</button>}
          {hasAdopted && current?.qa && <button className="btn small" disabled={disabled} onClick={doWithCritique}>带评语重生</button>}
          {(hasAdopted || shot.versions.length > 0) && (
            <button className="btn small ghost" disabled={disabled}
              onClick={() => onGenVideo({ reroll: true, actionLabel: '原词重抽' })}>原词重抽</button>
          )}
          {current && !hasAdopted && (
            <button className="btn small ghost" disabled={disabled} onClick={() => doDelete(current.id)}>删除此版</button>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="video-section video-comparison-panel">
      <div className="version-history candidate-compare">
        <div className="candidate-compare-head">
          <b>视频候选版本比较</b>
          <span>单次预估 ￥{shot.est_cost_cny.toFixed(2)} · 点击方框可预览 · 本镜状态：{videoState.label}</span>
        </div>
        {shot.versions.length > 0 ? (
          <div className="version-compare-grid">
            {shot.versions.map(version => {
              const adopted = version.id === shot.adopted_version_id
              const previewing = version.id === previewVersionId
              const canPreview = !!version.video_url
              const stampClass = version.status === 'succeeded' ? 'green'
                : version.status === 'queued' || version.status === 'running' || version.status === 'waiting_provider' ? 'gold'
                  : version.status === 'failed' ? 'red' : 'grey'
              return (
                <div
                  className={`version-compare-card${adopted ? ' adopted' : ''}${previewing ? ' previewing' : ''}${canPreview ? ' clickable' : ''}`}
                  key={version.id}
                  role={canPreview ? 'button' : undefined}
                  tabIndex={canPreview ? 0 : undefined}
                  onClick={() => { if (canPreview) onPreview(version.id) }}
                  onKeyDown={e => {
                    if (!canPreview) return
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      onPreview(version.id)
                    }
                  }}
                >
                  <div className="version-compare-details">
                    <div className="version-compare-heading">
                      <b>v{version.version_no}</b>
                      <span className={`stamp ${stampClass}`}>
                        {videoVersionStatusLabel(version, adopted)}
                      </span>
                    </div>
                    <span>QA {version.qa?.overall?.toFixed(2) ?? '未评估'} · ￥{version.cost_cny.toFixed(2)} · {version.latency_s.toFixed(1)}s</span>
                    {version.qa?.issues?.[0] && <small>{version.qa.issues[0]}</small>}
                    {version.error && <small>{version.error}</small>}
                    {version.adoption_reason && <small className="adoption-reason">采用理由：{version.adoption_reason}</small>}
                  </div>
                  <div className="version-compare-actions">
                    {previewing && !adopted && <span className="stamp blue">预览中</span>}
                    {adopted ? <span className="stamp green">当前采用</span> : version.video_url && (
                      <button className="btn small" onClick={e => { e.stopPropagation(); doAdopt(version.id) }}>比较后采用</button>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        ) : (
          <div className="review-empty">暂无视频版本，请使用下方按钮生成本镜视频</div>
        )}
      </div>
    </div>
  )
}
