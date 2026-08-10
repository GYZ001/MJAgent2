import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import EpisodeCrumb from '../components/EpisodeCrumb'
import { episodeBusy, useEpisode, useNav, usePoll } from '../App'
import {
  api,
  ApiError,
  type Episode,
  type EpisodeVideoGenerationPlan,
  type ReferenceImage,
  type ReviewWallContext,
  type Shot,
  type ShotVersion,
} from '../api'
import { compactShotStage, shotVideoState } from '../shotStatus'
import DecisionDialog from '../components/DecisionDialog'
import QueryState from '../components/QueryState'
import {
  artifactTypeLabel,
  statusLabel,
} from '../lib/statusLabels'
import { useFocusTrap } from '../hooks/useFocusTrap'

type ReviewTab = 'text' | 'references' | 'videos'
type DetailState =
  | { status: 'idle' }
  | { status: 'loading'; shotId: string }
  | { status: 'ready'; shotId: string; shot: Shot; loadedAt: number }
  | { status: 'error'; shotId: string; message: string; errorId?: string }
type ReviewContextPollResult =
  | { ok: true; context: ReviewWallContext }
  | { ok: false; error: string; retry: boolean }
type ShotFilter = 'problem' | 'unproduced' | 'generating' | 'pending_adoption' | 'adopted' | 'failed' | 'grade_b' | 'continuity'

export const REVIEW_TABS: Array<{ id: ReviewTab; label: string }> = [
  { id: 'text', label: '文字内容' },
  { id: 'references', label: '素材库' },
  { id: 'videos', label: '视频预览' },
]

const generationDraftKey = (shotId: string) => `manju:video-generation-draft:${shotId}`
const EMPTY_SHOTS: Shot[] = []

const VIDEO_MODE_LABEL = {
  REFERENCE_IMAGE_MODE: '参考图',
  FIRST_FRAME_MODE: '上一视频尾帧首帧',
  FIRST_LAST_FRAME_MODE: '首尾帧',
  VIDEO_INPUT_MODE: '视频参考',
} as const

function videoModeLabel(mode?: string | null) {
  return VIDEO_MODE_LABEL[mode as keyof typeof VIDEO_MODE_LABEL] || mode || '待规划'
}

export function videoModeReasonText(reasonCodes?: string[]) {
  return (reasonCodes ?? []).join('、') || '由整集关系计划生成'
}

export function isVideoModelInputRejection(
  error?: string | null,
  reasonCode?: string | null,
) {
  const rejectionCodes = [
    'VIDEO_INPUT_PRIVACY_REJECTED',
    'VIDEO_TEXT_REJECTED',
    'VIDEO_COPYRIGHT_REJECTED',
  ]
  return rejectionCodes.includes(reasonCode || '')
    || rejectionCodes.some(code => error?.includes(code))
}

const EPISODE_STATUS: Record<string, { label: string; next: string }> = {
  planned: { label: '待制作', next: '请先完成剧本和分镜' },
  scripting: { label: '剧本制作中', next: '等待剧本完成后到分镜台确认' },
  scripted: { label: '剧本已完成', next: '请到分镜台制作并确认' },
  confirmed: { label: '分镜已确认', next: '可以开始生成并检查视频' },
  generating: { label: '视频生成中', next: '可继续查看已就绪的镜头' },
  done: { label: '已完成', next: '请检查视频并进入成片台' },
}

const REJECT_REASON: Record<string, { label: string; suggestion: string; risk: 'low' | 'medium' | 'high' }> = {
  missing_quality_score: { label: '缺少质检结果', suggestion: '先重新运行质检', risk: 'high' },
  quality_below_threshold: { label: '画面质量未达标', suggestion: '调整生成词或重新生成', risk: 'medium' },
  quality_issue_blocks_reuse: { label: '必检质量项未通过', suggestion: '修复质量问题后重新验证', risk: 'high' },
  consistency_drift: { label: '人物或场景一致性漂移', suggestion: '换用已验证的人物/场景参考', risk: 'high' },
  consistency_drift_unfixable: { label: '一致性严重漂移', suggestion: '重建参考图后再生成', risk: 'high' },
  duplicate_character_suppressed: { label: '画面出现重复角色', suggestion: '减少冲突参考并明确人数', risk: 'medium' },
}
const REFERENCE_RISK_LABEL: Record<'low' | 'medium' | 'high', string> = {
  low: '低',
  medium: '中',
  high: '高',
}

function newId(prefix: string) {
  return `${prefix}:${Date.now()}:${Math.random().toString(36).slice(2, 10)}`
}

export function reviewWallPositionKey(
  projectId: string | null,
  episodeId: string | null,
  storyboardArtifactId: string | null,
): string {
  return `manju:review-wall:${projectId || 'project'}:${episodeId || 'episode'}:${storyboardArtifactId || 'unconfirmed'}`
}

export function resolveStableShotSelection(
  shots: Array<Pick<Shot, 'id'>>,
  savedShotId: string | null,
  hadSavedIdentity: boolean,
): { selectedShotId: string | null; tombstoneShotId: string | null } {
  if (savedShotId && shots.some(shot => shot.id === savedShotId)) {
    return { selectedShotId: savedShotId, tombstoneShotId: null }
  }
  if (savedShotId && hadSavedIdentity) {
    return { selectedShotId: savedShotId, tombstoneShotId: savedShotId }
  }
  return { selectedShotId: shots[0]?.id || null, tombstoneShotId: null }
}

export function shouldCommitShotDetail(
  requestSequence: number,
  latestSequence: number,
  requestedShotId: string,
  selectedShotId: string | null,
) {
  return requestSequence === latestSequence && requestedShotId === selectedShotId
}

export function shouldPersistReviewWallPosition(
  initializedPositionKey: string | null,
  currentPositionKey: string,
  selectedShotId: string | null,
) {
  return initializedPositionKey === currentPositionKey && Boolean(selectedShotId)
}

export function describeShotUpdate(previous: Shot, next: Shot): string | null {
  const contentFields: Array<keyof Shot> = ['source_excerpt', 'action_desc', 'narration', 'dialogues', 'prompt_preview']
  const changedContent = contentFields.some(field => JSON.stringify(previous[field]) !== JSON.stringify(next[field]))
  const beforeVersions = previous.versions.map(version => `${version.id}:${version.status}`).join('|')
  const afterVersions = next.versions.map(version => `${version.id}:${version.status}`).join('|')
  const changedVersions = beforeVersions !== afterVersions || previous.adopted_version_id !== next.adopted_version_id
  if (!changedContent && !changedVersions) return null
  return [changedContent ? '镜头文字/连续性内容已更新' : '', changedVersions ? '视频版本或采用关系已更新' : ''].filter(Boolean).join('；')
}

/**
 * Only refresh the heavyweight shot review payload when something visible in
 * the workbench can have changed. Episode polling returns fresh object/array
 * identities every time; using those identities as effect dependencies made
 * the current workbench enter its loading state on every poll.
 */
export function shotDetailRefreshKey(shot: Shot | null): string {
  if (!shot) return 'missing'
  return JSON.stringify({
    id: shot.id,
    adoptedVersionId: shot.adopted_version_id,
    videoStatus: shot.video_status,
    videoStale: shot.video_stale,
    versions: shot.versions.map(version => ({
      id: version.id,
      status: version.status,
      hasVideo: Boolean(version.video_url),
      error: version.error || null,
      qa: version.qa?.overall ?? null,
      playbackRate: version.playback_rate ?? 1,
      references: (version.image_inputs?.reference_images ?? []).map(ref => ({
        id: ref.id,
        imageUrl: ref.image_url || null,
        qualityScore: refScore(ref),
        selected: Boolean(ref.selectedForSeedance),
        deleted: Boolean(ref.deleted),
        rejectReason: ref.rejectReason || null,
        gateStatus: ref.gate_status || ref.downstream_eligibility || ref.qa?.status || null,
      })),
    })),
  })
}

export function reviewContextRefreshKey(ep: {
  status?: string
  storyboard_artifact_id?: string | null
  active_storyboard_run_id?: string | null
  active_video_run_id?: string | null
} | null): string {
  if (!ep) return 'missing'
  return [
    ep.status || '',
    ep.storyboard_artifact_id || '',
    ep.active_storyboard_run_id || '',
    ep.active_video_run_id || '',
  ].join('|')
}

export function shouldRetryReviewContextError(error: unknown): boolean {
  const status = Number((error as { status?: number } | null)?.status)
  return !Number.isFinite(status) || status < 400 || status >= 500
}

function commaList(value?: string[]) {
  return (value ?? []).join('、') || '无'
}

function truncateText(value: string, max = 1000) {
  return value.length > max ? `${value.slice(0, max)}…` : value
}

function videoVersionStatusLabel(version: ShotVersion, adopted: boolean): string {
  if (adopted) return '已采纳'
  if (version.status === 'succeeded' && version.video_url) return '待采纳'
  if (version.status === 'failed') return '生成失败'
  if (['queued', 'running', 'waiting_provider'].includes(version.status)) return '生成中'
  return '待生成'
}

export function videoCandidateNote(version: ShotVersion): string {
  if (version.qa?.issues?.length) return version.qa.issues.join('；')
  if (version.error) return '生成未完成，点击查看错误详情'
  if (version.video_url) return '点击卡片预览此候选'
  return '视频尚未生成完成，可点击查看当前状态'
}

export type EpisodeGenerationAction = 'generate' | 'stop' | 'resume'

export const EPISODE_COMPLETION_BUDGET_CAP_CNY = 150
export const EPISODE_COMPLETION_WALL_CLOCK_CAP_S = 4 * 60 * 60
const EPISODE_COMPLETION_LATENCY_MARGIN = 1.25

export function episodeCompletionBudgetCap(
  estimatedCostCny: number,
  requiredCompletionCapCny = 0,
): number {
  const estimate = Number.isFinite(estimatedCostCny)
    ? Math.max(0, estimatedCostCny)
    : 0
  const required = Number.isFinite(requiredCompletionCapCny)
    ? Math.max(0, requiredCompletionCapCny)
    : 0
  return Math.max(
    EPISODE_COMPLETION_BUDGET_CAP_CNY,
    Math.ceil(estimate * 100) / 100,
    Math.ceil(required * 100) / 100,
  )
}

export function episodeCompletionWallClockCap(criticalPathLatencyMs = 0): number {
  const criticalPathSeconds = Number.isFinite(criticalPathLatencyMs)
    ? Math.max(0, criticalPathLatencyMs) / 1000
    : 0
  const projected = criticalPathSeconds * EPISODE_COMPLETION_LATENCY_MARGIN
  const roundedToHour = Math.ceil(projected / 3600) * 3600
  return Math.max(EPISODE_COMPLETION_WALL_CLOCK_CAP_S, roundedToHour)
}

export function episodeCompletionRequest(
  qualificationVersion?: string,
  estimatedCostCny = 0,
  requiredCompletionCapCny = 0,
  criticalPathLatencyMs = 0,
) {
  return {
    mode: 'fresh',
    budget_cap_cny: episodeCompletionBudgetCap(
      estimatedCostCny,
      requiredCompletionCapCny,
    ),
    wall_clock_cap_s: episodeCompletionWallClockCap(criticalPathLatencyMs),
    allow_fallback_adopt: true,
    allow_storyboard_edit: false,
    qualification_version: qualificationVersion,
  }
}

export function episodeGenerationAction(
  active: boolean,
  pausedCount: number,
  failedCount: number,
  resumableCompletion = true,
): EpisodeGenerationAction {
  if (active) return 'stop'
  if (resumableCompletion && (pausedCount > 0 || failedCount > 0)) return 'resume'
  return 'generate'
}

export function episodeGenerationIsActive(
  supervisorTaskRunning: boolean,
  activeVideoRunId: string | null | undefined,
  generatingCount: number,
): boolean {
  return supervisorTaskRunning || Boolean(activeVideoRunId) || generatingCount > 0
}

export function shotHasActiveGeneration(shot: Shot): boolean {
  const status = shot.pipeline?.pipeline_status
  if (['paused_budget', 'waiting_budget', 'waiting_human', 'paused_external'].includes(status || '')) {
    return false
  }
  return shotVideoState(shot).phase === 'generating'
}

export function shotHasPausedGeneration(shot: Shot): boolean {
  return ['paused_budget', 'waiting_budget', 'waiting_human', 'paused_external']
    .includes(shot.pipeline?.pipeline_status || '')
}

export type VideoGenerationMode = 'reroll' | 'rewrite' | 'critique'

export const VIDEO_PLAYBACK_RATES = [0.5, 0.75, 1, 1.25, 1.5, 2] as const

export function videoPlaybackRate(version: Pick<ShotVersion, 'playback_rate'>): number {
  const rate = Number(version.playback_rate ?? 1)
  return Number.isFinite(rate) && rate >= 0.5 && rate <= 2 ? rate : 1
}

export function videoGenerationConfirmLabel(mode: VideoGenerationMode, estimatedCost: number): string {
  const action = mode === 'reroll'
    ? '确认新建候选'
    : mode === 'rewrite'
      ? '确认让 AI 按要求重写'
      : '确认按质检问题修复'
  return `${action} · 预计 ￥${estimatedCost.toFixed(2)}`
}

function refScore(ref: ReferenceImage): number | null {
  const score = ref.qualityScore ?? ref.qa?.overall
  return typeof score === 'number' ? score : null
}

export function currentVersionRefs(shot: Shot): {
  versionId: string; versionNo: number; refs: ReferenceImage[]; isFallback: boolean
} | null {
  const adopted = shot.versions.find(version => version.id === shot.adopted_version_id)
  const hasRefs = (version: ShotVersion) => (version.image_inputs?.reference_images?.length ?? 0) > 0
  const live = shot.versions.find(version => ['queued', 'running', 'waiting_provider'].includes(version.status) && hasRefs(version))
  const preferred = live || adopted || shot.versions[0]
  const version = preferred && hasRefs(preferred) ? preferred : shot.versions.find(hasRefs) || preferred
  if (!version) return null
  return {
    versionId: version.id,
    versionNo: version.version_no,
    refs: version.image_inputs?.reference_images ?? [],
    isFallback: !!preferred && version.id !== preferred.id,
  }
}

export type MaterialLibraryKind = 'keyframes' | 'references' | 'video'

export function shotMaterialLibraryKind(shot: Shot): MaterialLibraryKind {
  const mode = shot.mode_plan?.mode
    || shot.versions.find(version => version.image_inputs?.mode)?.image_inputs?.mode
  if (mode === 'FIRST_FRAME_MODE' || mode === 'FIRST_LAST_FRAME_MODE') return 'keyframes'
  if (mode === 'VIDEO_INPUT_MODE') return 'video'
  return 'references'
}

function versionHasMaterial(
  version: ShotVersion,
  kind: MaterialLibraryKind,
): boolean {
  const inputs = version.image_inputs
  if (kind === 'keyframes') {
    return Boolean(inputs?.first_frame_image_url || inputs?.last_frame_image_url)
  }
  if (kind === 'video') return Boolean(inputs?.video_input_url)
  return Boolean(inputs?.reference_images?.length)
}

export function currentMaterialVersion(
  shot: Shot,
  kind = shotMaterialLibraryKind(shot),
): { version: ShotVersion; isFallback: boolean } | null {
  const adopted = shot.versions.find(
    version => version.id === shot.adopted_version_id,
  )
  const live = shot.versions.find(version =>
    ['queued', 'running', 'waiting_provider'].includes(version.status)
    && versionHasMaterial(version, kind),
  )
  const preferred = live || shot.versions[0] || adopted
  const version = preferred && versionHasMaterial(preferred, kind)
    ? preferred
    : shot.versions.find(candidate => versionHasMaterial(candidate, kind))
      || preferred
  return version
    ? { version, isFallback: Boolean(preferred && version.id !== preferred.id) }
    : null
}

export function materialLibraryTitle(kind: MaterialLibraryKind): string {
  if (kind === 'keyframes') return '本镜关键帧'
  if (kind === 'video') return '本镜视频输入'
  return '本镜参考图'
}

export function refSourceLabel(ref: ReferenceImage): string {
  const views: Record<string, string> = {
    front_full: '正面全身', three_quarter: '3/4 面', profile: '侧面', back_full: '背面全身',
    face_closeup: '面部特写', establishing: '建立', reverse_angle: '反打', action_zone: '动作区',
  }
  if (ref.type === 'plot_key_frame' || ref.slot_key === 'narrative_keyframe') return '关键帧'
  if (ref.type === 'previous_shot_frame' || ref.source === 'previous_shot' || ref.source === 'previous_shot_frame') return '上镜衔接帧'
  if (ref.type === 'character' || ref.entity_type === 'character') return `人物参考${ref.view_role ? ` · ${views[ref.view_role] || ref.view_role}` : ''}`
  if (ref.type === 'scene' || ref.entity_type === 'scene') return `场景参考${ref.view_role ? ` · ${views[ref.view_role] || ref.view_role}` : ''}`
  return ({ seedream_generated: '生成参考图', asset_library: '资产库参考' } as Record<string, string>)[ref.source] || ref.source || '参考图'
}

export function referenceLibraryLabel(ref: ReferenceImage): string {
  if (
    ref.type === 'plot_key_frame'
    || ref.slot_key === 'narrative_keyframe'
  ) {
    return '剧情参考图'
  }
  return refSourceLabel(ref)
}

export function refPurposeBucket(ref: ReferenceImage): 'video' | 'evidence' | 'discarded' {
  if (ref.deleted || (!ref.selectedForSeedance && ref.rejectReason)) return 'discarded'
  if (ref.selectedForSeedance && !ref.deleted) return 'video'
  if ((ref.purposes || []).some(purpose => purpose === 'qa_anchor' || purpose === 'keyframe_seed')) return 'evidence'
  return 'discarded'
}

export function classifyReferenceBuckets(refs: ReferenceImage[]) {
  return {
    video: refs.filter(ref => refPurposeBucket(ref) === 'video'),
    evidence: refs.filter(ref => refPurposeBucket(ref) === 'evidence'),
    discarded: refs.filter(ref => refPurposeBucket(ref) === 'discarded'),
  }
}

function rejectReasonInfo(reason?: string | null) {
  if (!reason) return { label: '人工废弃', suggestion: '可在确认资格后恢复', risk: 'low' as const }
  return REJECT_REASON[reason] || { label: '未知淘汰原因', suggestion: '展开技术信息并联系管理员', risk: 'high' as const }
}

function Dialog({ title, children, onClose, wide = false, closeDisabled = false }: {
  title: string
  children: React.ReactNode
  onClose: () => void
  wide?: boolean
  closeDisabled?: boolean
}) {
  const titleId = useId()
  const requestClose = () => {
    if (!closeDisabled) onClose()
  }
  const ref = useFocusTrap(true, requestClose)
  return (
    <div className="review-dialog-backdrop" role="presentation" onMouseDown={requestClose}>
      <div ref={ref as React.RefObject<HTMLDivElement>} className={`review-dialog${wide ? ' wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby={titleId} onMouseDown={event => event.stopPropagation()}>
        <div className="review-dialog-head">
          <h3 id={titleId}>{title}</h3>
          <button
            type="button"
            disabled={closeDisabled}
            aria-label={closeDisabled ? '正在提交，暂不能关闭对话框' : '关闭对话框'}
            title={closeDisabled ? '正在提交，请等待结果' : undefined}
            onClick={requestClose}
          >
            ×
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}

function Lightbox({ src, alt, onClose }: { src: string; alt: string; onClose: () => void }) {
  return (
    <Dialog title={alt || '素材预览'} onClose={onClose} wide>
      <img className="review-lightbox-image" src={src} alt={alt} />
    </Dialog>
  )
}

function stateMeta(status: string) {
  return EPISODE_STATUS[status] || { label: '未知状态', next: '请刷新或查看技术详情' }
}

export function incompleteVideoSupervisorState(
  supervisor: Episode['video_supervisor'],
): { outcome: string; runId: string | null } | null {
  if (
    !supervisor
    || supervisor.task_running === true
    || Number(supervisor.active_media_jobs || 0) > 0
    || supervisor.run_status !== 'PARTIAL'
  ) return null
  return {
    outcome: String(supervisor.outcome || 'PARTIAL_RESULT'),
    runId: supervisor.run_id || null,
  }
}

function matchesFilter(shot: Shot, filter: ShotFilter) {
  const state = shotVideoState(shot)
  if (filter === 'problem') return Boolean(state.grade === 'B' || state.continuityDegraded || state.phase === 'generation_failed')
  if (filter === 'unproduced') return state.phase === 'pending_generation'
  if (filter === 'generating') return state.phase === 'generating'
  if (filter === 'pending_adoption') return state.phase === 'pending_adoption'
  if (filter === 'adopted') return state.phase === 'adopted'
  if (filter === 'failed') return state.phase === 'generation_failed'
  if (filter === 'grade_b') return state.grade === 'B'
  return state.continuityDegraded
}

export default function WallPage() {
  const { projectId, episodeId, go } = useNav()
  const { data: ep, refresh, error, loading } = useEpisode(
    episodeId || '',
    'wall',
    current => episodeBusy(current) ? 8000 : 0,
  )
  const {
    data: contextPollResult,
    refresh: refreshContext,
  } = usePoll<ReviewContextPollResult>(
    async () => {
      try {
        return { ok: true, context: await api.getReviewContext(episodeId!) }
      } catch (reason) {
        return {
          ok: false,
          error: reason instanceof Error ? reason.message : String(reason),
          retry: shouldRetryReviewContextError(reason),
        }
      }
    },
    result => result?.ok === false && result.retry ? 3000 : 0,
    [episodeId],
  )
  // Keep the pre-detail dependency stable. A fresh [] on every render makes
  // the detail effect re-enter and repeatedly write a new idle state.
  const shots = ep?.shots ?? EMPTY_SHOTS
  const [selectedShotId, setSelectedShotId] = useState<string | null>(null)
  const [selectionReady, setSelectionReady] = useState(false)
  const [selectionPositionKey, setSelectionPositionKey] = useState<string | null>(null)
  const [tombstoneShotId, setTombstoneShotId] = useState<string | null>(null)
  const [reviewTab, setReviewTab] = useState<ReviewTab>('text')
  const [detail, setDetail] = useState<DetailState>({ status: 'idle' })
  const [context, setContext] = useState<ReviewWallContext | null>(null)
  const [videoPlan, setVideoPlan] = useState<EpisodeVideoGenerationPlan | null>(null)
  const [contextError, setContextError] = useState<string | null>(null)
  const [filters, setFilters] = useState<Set<ShotFilter>>(new Set())
  const [filterSource, setFilterSource] = useState('未筛选')
  const [contentUpdate, setContentUpdate] = useState<string | null>(null)
  const [toast, setToast] = useState<{ message: string; action?: { label: string; run: () => void } } | null>(null)
  const [lightbox, setLightbox] = useState<{ src: string; label: string } | null>(null)
  const [generationSubmitting, setGenerationSubmitting] = useState(false)
  const [generationOperation, setGenerationOperation] = useState<EpisodeGenerationAction | 'clear' | null>(null)
  const [generationDecision, setGenerationDecision] = useState<EpisodeGenerationAction | 'clear' | null>(null)
  const [stalePreview, setStalePreview] = useState<Awaited<ReturnType<typeof api.staleAssetsPreview>> | null>(null)
  const [staleSelection, setStaleSelection] = useState<Set<string>>(new Set())
  const [staleBusy, setStaleBusy] = useState(false)
  const [genMask, setGenMask] = useState<Set<string>>(new Set())
  const toastTimer = useRef<number>()
  const generationActionRef = useRef<HTMLButtonElement>(null)
  const clearEpisodeResourcesRef = useRef<HTMLButtonElement>(null)
  const detailRequest = useRef(0)
  const lastReadyDetail = useRef<Shot | null>(null)
  const positionKey = reviewWallPositionKey(
    projectId,
    episodeId,
    ep?.storyboard_artifact_id || null,
  )

  const showToast = useCallback((message: string, action?: { label: string; run: () => void }, persistent = false) => {
    setToast({ message, action })
    if (toastTimer.current) window.clearTimeout(toastTimer.current)
    if (!persistent) toastTimer.current = window.setTimeout(() => setToast(null), action ? 8000 : 3600)
  }, [])

  const closeGenerationDecision = useCallback(() => {
    const target = generationDecision === 'clear' ? clearEpisodeResourcesRef : generationActionRef
    setGenerationDecision(null)
    window.requestAnimationFrame(() => target.current?.focus())
  }, [generationDecision])

  const loadContext = useCallback(async () => {
    const result = await refreshContext()
    return result?.ok ? result.context : null
  }, [refreshContext])

  const loadVideoPlan = useCallback(async () => {
    if (!episodeId) return null
    try {
      const plan = await api.getVideoGenerationPlan(episodeId)
      setVideoPlan(plan)
      return plan
    } catch {
      setVideoPlan(null)
      return null
    }
  }, [episodeId])

  const loadDetail = useCallback(async (shotId: string) => {
    const request = ++detailRequest.current
    // Keep an already rendered workbench mounted during background sync. This
    // avoids a full-card teardown/rebuild (and the visible page flash) while
    // the episode status poll is running.
    setDetail(current => (
      (current.status === 'ready' || current.status === 'loading') && current.shotId === shotId
        ? current
        : { status: 'loading', shotId }
    ))
    try {
      const shot = await api.get(`/shots/${shotId}/review`) as Shot
      if (!shouldCommitShotDetail(request, detailRequest.current, shotId, selectedShotId)) return null
      if (lastReadyDetail.current?.id === shot.id) {
        const summary = describeShotUpdate(lastReadyDetail.current, shot)
        if (summary) setContentUpdate(summary)
      }
      lastReadyDetail.current = shot
      setDetail({ status: 'ready', shotId, shot, loadedAt: Date.now() })
      return shot
    } catch (reason) {
      if (!shouldCommitShotDetail(request, detailRequest.current, shotId, selectedShotId)) return null
      const value = reason as Error & { errorId?: string }
      setDetail({ status: 'error', shotId, message: value.message || String(reason), errorId: value.errorId })
      return null
    }
  }, [selectedShotId])

  useEffect(() => {
    if (!contextPollResult) return
    if (contextPollResult.ok) {
      setContext(contextPollResult.context)
      setContextError(null)
    } else {
      setContextError(contextPollResult.error)
    }
  }, [contextPollResult])

  const contextRefreshKey = reviewContextRefreshKey(ep)
  useEffect(() => { void loadContext() }, [contextRefreshKey, loadContext])
  useEffect(() => { void loadVideoPlan() }, [contextRefreshKey, loadVideoPlan])

  useEffect(() => {
    if (
      !episodeId
      || loading
      || !ep
      || (selectionReady && selectionPositionKey === positionKey)
    ) return
    let saved: { shotId?: string; tab?: ReviewTab } | null = null
    try { saved = JSON.parse(localStorage.getItem(positionKey) || 'null') } catch { saved = null }
    let redirected: { episodeId?: string; shotId?: string } | null = null
    try { redirected = JSON.parse(sessionStorage.getItem('manju:select_shot') || 'null') } catch { redirected = null }
    const requested = redirected?.episodeId === episodeId ? redirected.shotId : saved?.shotId
    const validTab = REVIEW_TABS.some(tab => tab.id === saved?.tab) ? saved?.tab : undefined
    if (validTab) setReviewTab(validTab)
    const resolved = resolveStableShotSelection(shots, requested || null, Boolean(requested))
    setSelectedShotId(resolved.selectedShotId)
    setTombstoneShotId(resolved.tombstoneShotId)
    if (redirected?.episodeId === episodeId) sessionStorage.removeItem('manju:select_shot')
    setSelectionPositionKey(positionKey)
    setSelectionReady(true)
  }, [ep, episodeId, loading, positionKey, selectionPositionKey, selectionReady, shots])

  const selectedSummary = shots.find(shot => shot.id === selectedShotId) || null
  const selectedDetailRefreshKey = shotDetailRefreshKey(selectedSummary)

  useEffect(() => {
    if (
      !selectionReady
      || selectionPositionKey !== positionKey
      || !selectedShotId
    ) {
      setDetail(current => current.status === 'idle' ? current : { status: 'idle' })
      return
    }
    if (selectedDetailRefreshKey === 'missing') {
      setTombstoneShotId(selectedShotId)
      setDetail(current => current.status === 'idle' ? current : { status: 'idle' })
      return
    }
    setTombstoneShotId(null)
    void loadDetail(selectedShotId)
  }, [
    loadDetail,
    positionKey,
    selectedDetailRefreshKey,
    selectedShotId,
    selectionPositionKey,
    selectionReady,
  ])

  useEffect(() => {
    if (
      !selectionReady
      || !shouldPersistReviewWallPosition(
        selectionPositionKey,
        positionKey,
        selectedShotId,
      )
    ) return
    localStorage.setItem(positionKey, JSON.stringify({ shotId: selectedShotId, tab: reviewTab }))
  }, [positionKey, reviewTab, selectedShotId, selectionPositionKey, selectionReady])

  const selectShot = useCallback((shotId: string) => {
    setContentUpdate(null)
    setTombstoneShotId(null)
    setSelectedShotId(shotId)
  }, [])

  const filteredShots = useMemo(() => {
    if (!filters.size) return shots
    return shots.filter(shot => [...filters].every(filter => matchesFilter(shot, filter)))
  }, [filters, shots])

  const readyShot = detail.status === 'ready' && detail.shotId === selectedShotId ? detail.shot : null
  const writeFrozen = Boolean(tombstoneShotId || detail.status !== 'ready' || !context?.upstream.eligible_for_production)
  const generateDisabledReason = generationSubmitting
    ? '整集生成请求正在提交，请勿重复操作'
    : !context
      ? '正在核对分镜确认和资产资格'
      : !context.upstream.eligible_for_production
        ? context.upstream.blockers.join('；') || '当前生成资格未通过'
        : ''
  const supervisorTaskRunning = ep?.video_supervisor?.task_running === true
  const generatingCount = shots.filter(shotHasActiveGeneration).length
  const pausedGenerationCount = shots.filter(shotHasPausedGeneration).length
  const hasCurrentGeneration = episodeGenerationIsActive(
    supervisorTaskRunning,
    ep?.active_video_run_id,
    generatingCount,
  )
  const generationAction = episodeGenerationAction(
    hasCurrentGeneration,
    Math.max(ep?.pipeline_summary?.paused ?? 0, pausedGenerationCount),
    ep?.pipeline_summary?.failed ?? 0,
    ep?.video_completion_mode === 'complete',
  )
  const quickGenerationEstimate = shots.reduce((sum, shot) => sum + (shot.est_cost_cny || 0), 0)
  const episodeBudgetCap = episodeCompletionBudgetCap(
    quickGenerationEstimate,
    ep?.video_budget?.required_completion_cap_cny,
  )
  const adoptedCount = shots.filter(shot => shotVideoState(shot).phase === 'adopted').length
  const episodeVideoCandidateCount = shots.reduce(
    (sum, shot) => sum + visibleVideoVersions(shot.versions).length,
    0,
  )
  const clearEpisodeResourcesDisabledReason = generationSubmitting
    ? '正在处理上一项视频操作'
    : context?.upstream.active_upstream_runs.length
      ? '上游任务运行中，暂不能清空资源'
      : ''

  const refreshAll = useCallback(async () => {
    const preservedShotId = selectedShotId
    const next = await refresh()
    await Promise.all([loadContext(), loadVideoPlan()])
    if (!preservedShotId) return
    if (!next?.shots?.some(shot => shot.id === preservedShotId)) {
      detailRequest.current += 1
      setTombstoneShotId(preservedShotId)
      setDetail({ status: 'idle' })
      return
    }
    await loadDetail(preservedShotId)
  }, [loadContext, loadDetail, loadVideoPlan, refresh, selectedShotId])

  const navigateIn = useCallback((direction: -1 | 1) => {
    if (!filteredShots.length) return
    const currentNo = selectedSummary?.shot_no ?? (direction > 0 ? -Infinity : Infinity)
    const candidate = direction > 0
      ? filteredShots.find(shot => shot.shot_no > currentNo)
      : [...filteredShots].reverse().find(shot => shot.shot_no < currentNo)
    if (candidate) selectShot(candidate.id)
  }, [filteredShots, selectShot, selectedSummary?.shot_no])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (target?.matches('input, textarea, select, video, [contenteditable="true"]')) return
      if (!event.altKey || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return
      event.preventDefault()
      navigateIn(event.key === 'ArrowLeft' ? -1 : 1)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [navigateIn])

  const startEpisodeGeneration = async () => {
    // #region debug-point A-E:episode-generation-submit
    void fetch('http://127.0.0.1:7778/event', { method: 'POST', body: JSON.stringify({ sessionId: 'video-generation-noop', runId: 'post-fix', hypothesisId: 'A', location: 'frontend/src/pages/WallPage.tsx:startEpisodeGeneration', msg: '[DEBUG] Episode generation confirmation entered', data: { episodeId: ep?.id, eligible: context?.upstream.eligible_for_production, activeVideoRunId: ep?.active_video_run_id, supervisorPhase: ep?.video_supervisor?.phase, supervisorTaskRunning, generatingCount, generationAction }, ts: Date.now() }) }).catch(() => {})
    // #endregion
    if (!context?.upstream.eligible_for_production) { showToast(context?.upstream.blockers.join('；') || '分镜尚未确认', undefined, true); return }
    setGenerationSubmitting(true)
    setGenerationOperation('generate')
    try {
      const response = await api.episodeVideoCompletion(
        ep!.id,
        episodeCompletionRequest(
          context.upstream.qualification_version,
          quickGenerationEstimate,
          ep?.video_budget?.required_completion_cap_cny,
          videoPlan?.critical_path_latency_ms,
        ),
      ) as {
        run_id?: string
        message?: string
      }
      // #region debug-point B-E:episode-generation-response
      void fetch('http://127.0.0.1:7778/event', { method: 'POST', body: JSON.stringify({ sessionId: 'video-generation-noop', runId: 'post-fix', hypothesisId: 'B', location: 'frontend/src/pages/WallPage.tsx:startEpisodeGeneration.response', msg: '[DEBUG] Episode generation API returned', data: { episodeId: ep?.id, response }, ts: Date.now() }) }).catch(() => {})
      // #endregion
      showToast(
        response.message
          || `全片补齐任务已启动${response.run_id ? ` · ${response.run_id}` : ''}；可在下方查看实时状态`,
      )
      await loadVideoPlan()
      await refreshAll()
    } catch (reason) {
      // #region debug-point B-E:episode-generation-error
      void fetch('http://127.0.0.1:7778/event', { method: 'POST', body: JSON.stringify({ sessionId: 'video-generation-noop', runId: 'post-fix', hypothesisId: 'B', location: 'frontend/src/pages/WallPage.tsx:startEpisodeGeneration.error', msg: '[DEBUG] Episode generation API failed', data: { episodeId: ep?.id, errorName: reason instanceof Error ? reason.name : typeof reason, errorMessage: reason instanceof Error ? reason.message : String(reason) }, ts: Date.now() }) }).catch(() => {})
      // #endregion
      showToast(reason instanceof Error ? reason.message : String(reason), undefined, true)
    } finally {
      setGenerationSubmitting(false)
      setGenerationOperation(null)
    }
  }

  const stopEpisodeGeneration = async () => {
    if (!ep) return
    setGenerationSubmitting(true)
    setGenerationOperation('stop')
    try {
      const result = await api.stopEpisodeVideo(ep.id)
      showToast(result.provider_may_continue
        ? `已暂停 ${result.paused_jobs} 个任务；供应商已接单的部分可能继续计费，结果会在继续任务后再同步`
        : `已暂停 ${result.paused_jobs} 个任务，可随时继续`)
      await refreshAll()
    } catch (reason) {
      showToast(reason instanceof Error ? reason.message : String(reason), undefined, true)
    } finally {
      setGenerationSubmitting(false)
      setGenerationOperation(null)
    }
  }

  const resumeEpisodeGeneration = async () => {
    if (!ep || !context?.upstream.eligible_for_production) { showToast(context?.upstream.blockers.join('；') || '当前生成资格未通过', undefined, true); return }
    setGenerationSubmitting(true)
    setGenerationOperation('resume')
    try {
      const result = await api.resumeEpisodeVideo(ep.id)
      const created = (result.enqueued || []).filter(item => item.job_id).length
      const reused = (result.enqueued || []).filter(item => item.reused).length
      const failed = (result.enqueued || []).filter(item => item.error).length
      showToast([
        result.resumed_jobs ? `恢复 ${result.resumed_jobs} 个暂停任务` : '',
        result.budget_resumed_jobs ? `恢复 ${result.budget_resumed_jobs} 个预算暂停任务` : '',
        created ? `补建 ${created} 个未完成任务` : '',
        reused ? `复用 ${reused} 个在途任务` : '',
        result.skipped_completed ? `跳过 ${result.skipped_completed} 个已完成分镜` : '',
        failed ? `${failed} 镜未能继续` : '',
      ].filter(Boolean).join('；') || '没有需要继续的任务', undefined, failed > 0)
      await refreshAll()
    } catch (reason) {
      showToast(reason instanceof Error ? reason.message : String(reason), undefined, true)
    } finally {
      setGenerationSubmitting(false)
      setGenerationOperation(null)
    }
  }

  const executeEpisodeGenerationAction = (action: EpisodeGenerationAction | 'clear') => {
    setGenerationDecision(null)
    if (action === 'stop') void stopEpisodeGeneration()
    else if (action === 'resume') void resumeEpisodeGeneration()
    else if (action === 'clear') void clearEpisodeResources()
    else void startEpisodeGeneration()
  }

  const clearEpisodeResources = async () => {
    if (!ep) return
    setGenerationSubmitting(true)
    setGenerationOperation('clear')
    try {
      await api.clearEpisodeArtifacts(ep.id)
      showToast(`本集 ${shots.length} 个分镜的视频和图像资源已清空`)
      await refreshAll()
    } catch (reason) {
      showToast(reason instanceof Error ? reason.message : String(reason), undefined, true)
    } finally {
      setGenerationSubmitting(false)
      setGenerationOperation(null)
    }
  }

  const loadStale = async () => {
    if (!ep) return
    setStaleBusy(true)
    try {
      const preview = await api.staleAssetsPreview(ep.id)
      setStalePreview(preview)
      setStaleSelection(new Set(preview.shots.map(shot => shot.shot_id)))
      if (!preview.stale_count) showToast('当前没有陈旧镜头')
    } catch (reason) { showToast(reason instanceof Error ? reason.message : String(reason), undefined, true) }
    finally { setStaleBusy(false) }
  }

  const repairStale = async () => {
    if (!ep || !stalePreview || !staleSelection.size) return
    setStaleBusy(true)
    try {
      const result = await api.repairStaleAssets(ep.id, [...staleSelection], stalePreview.preview_version, stalePreview.qualification.qualification_version)
      if (result.errors.length) {
        setStaleSelection(new Set(result.errors.map(item => item.shot_id)))
        showToast(`已提交 ${result.queued} 镜；${result.errors.map(item => `镜 ${item.shot_no}：${item.error}`).join('；')}。失败镜已保留，可直接重试。`, undefined, true)
      } else {
        setStalePreview(null)
        showToast(`已提交 ${result.queued} 个镜头的陈旧修复；旧采用版保留到新版成功`)
      }
      await refreshAll()
    } catch (reason) { showToast(reason instanceof Error ? reason.message : String(reason), undefined, true) }
    finally { setStaleBusy(false) }
  }

  if (error && !ep) return <QueryState loading={false} error={error} hasData={false}>{null}</QueryState>
  if (!ep) return <QueryState loading={loading !== false} error={null} hasData={false}>{null}</QueryState>
  const incompleteSupervisor = incompleteVideoSupervisorState(ep.video_supervisor)
  const episodeState = incompleteSupervisor
    ? {
      label: '视频任务未完成',
      next: `上次全片任务已停止：${incompleteSupervisor.outcome}`,
    }
    : stateMeta(ep.status)
  const staleCount = stalePreview?.stale_count ?? shots.filter(shot => shot.video_stale).length

  return (
    <div className="wall-page">
      <header className="wall-topbar">
        <div className="wall-topbar-left">
          <EpisodeCrumb
            label="生成台"
            view="wall"
            episodeNo={ep.episode_no}
            showProductionFilters
          />
          <PipelineFilters shots={shots} active={filters} onSelect={(filter, label) => { setFilters(new Set([filter])); setFilterSource(`顶部五态 · ${label}`) }} />
          <details className="status-detail"><summary><span className={`stamp ${context?.upstream.eligible_for_production ? 'green' : 'gold'}`}>{episodeState.label}</span></summary><div>{episodeState.next}<br /><code>{ep.status}</code></div></details>
        </div>
        <div className="wall-topbar-right">
          {shots.length > 0 && (
            <button
              ref={generationActionRef}
              type="button"
              className={`btn small episode-generation-action ${generationAction === 'stop' ? 'danger' : 'primary'}`}
              disabled={generationSubmitting || (generationAction !== 'stop' && Boolean(generateDisabledReason))}
              aria-label={generationAction === 'stop' ? '停止本集全部视频任务' : generationAction === 'resume' ? '继续本集全部未完成视频任务' : '生成本集全部视频'}
              title={generationAction !== 'stop' ? generateDisabledReason || undefined : '暂停整集任务，之后可继续'}
              onClick={() => setGenerationDecision(generationAction)}
            >
              {generationSubmitting
                ? generationOperation === 'stop' ? '停止中…' : generationOperation === 'resume' ? '继续中…' : '下发中…'
                : generationAction === 'stop' ? '停止任务' : generationAction === 'resume' ? '继续任务' : '生成视频'}
            </button>
          )}
          {shots.length > 0 && (
            <button
              ref={clearEpisodeResourcesRef}
              type="button"
              className="btn ghost small danger"
              disabled={Boolean(clearEpisodeResourcesDisabledReason)}
              aria-label={clearEpisodeResourcesDisabledReason ? `清空资源，暂不可用：${clearEpisodeResourcesDisabledReason}` : '清空本集全部视频和图像资源'}
              title={clearEpisodeResourcesDisabledReason || '清空本集所有分镜的视频、关键帧和参考图'}
              onClick={() => setGenerationDecision('clear')}
            >
              {generationOperation === 'clear' ? '清空中…' : '清空资源'}
            </button>
          )}
        </div>
      </header>

      {contextError && <section className="review-persistent-error" role="alert"><b>生成资格加载失败</b><span>当前保持只读，不会用空资格继续生成或采用。</span><details><summary>查看错误详情</summary><pre>{contextError}</pre></details><button className="btn small" onClick={() => { void loadContext() }}>重试加载</button></section>}
      {incompleteSupervisor && <section className="review-persistent-error" role="alert"><b>全片视频任务未完成</b><span>任务已停止，{shots.length} 镜仍保持当前状态；错误：{incompleteSupervisor.outcome}。</span><p>已有结果均已保留。请先查看错误详情，修复后再点击「生成视频」。</p>{incompleteSupervisor.runId && <details><summary>任务信息</summary><code>{incompleteSupervisor.runId}</code></details>}</section>}
      {context && !context.upstream.eligible_for_production && <section className="review-blocked-banner" role="status"><b>当前不可生成</b><span>{context.upstream.blockers.join('；')}。查看、停止旧任务和废弃/隔离仍可用，生成、恢复、采用和修复已保护。</span><button className="btn small" onClick={() => go(ep.status === 'scripting' || ep.status === 'planned' ? 'script' : 'board', projectId, ep.id)}>去{ep.status === 'scripting' || ep.status === 'planned' ? '剧本台' : '分镜台'}处理</button><details><summary>技术详情</summary><code>{context.upstream.qualification_version}</code></details></section>}
      {videoPlan && <VideoPlanSummary plan={videoPlan} />}

      {staleCount > 0 && <section className="material-fallback-note review-stale-banner" role="status"><span>参考资产已更新：<b>{staleCount}</b> 镜采用版可能使用旧证据。</span><button className="btn small" disabled={staleBusy} onClick={() => { void loadStale() }}>{staleBusy ? '预演中…' : '查看影响与选择修复'}</button></section>}

      {shots.length === 0 ? (
        <section className="review-business-empty"><div className="big">镜</div><h2>本集尚无可生成镜头</h2><p>当前状态：{episodeState.label}。{episodeState.next}。</p><div><button className="btn" onClick={() => go('script', projectId, ep.id)}>去剧本台</button><button className="btn primary" onClick={() => go('board', projectId, ep.id)}>去分镜台</button><button className="btn ghost" onClick={() => { void refreshAll() }}>刷新</button></div></section>
      ) : (
        <>
          <ShotFilters shots={shots} filters={filters} source={filterSource} onChange={(next, source) => { setFilters(next); setFilterSource(source) }} />
          <nav className="wall-shot-rail" aria-label="镜头状态导航">
            {filteredShots.map(item => {
              const state = shotVideoState(item)
              const stageLabel = state.phase === 'generating' ? compactShotStage(item) : state.label
              const exactStage = item.pipeline?.stage_label || stageLabel
              const reviewNotes = [
                state.grade === 'B' ? '质量需复核' : '',
                state.continuityDegraded ? '衔接需复核' : '',
              ].filter(Boolean)
              const executionDetail = item.pipeline?.reason_text || item.pipeline?.blocked_reason || exactStage
              return <button key={item.id} type="button" title={state.phase === 'generating' || state.phase === 'generation_failed' ? `${executionDetail}${item.pipeline?.task_id ? ` · 任务 ${item.pipeline.task_id}` : ''}` : reviewNotes.join('；') || undefined} data-grade={state.grade || undefined} className={`${item.id === selectedShotId ? 'active ' : ''}${state.railClass}`} onClick={() => selectShot(item.id)} aria-current={item.id === selectedShotId ? 'true' : undefined} aria-label={`镜 ${item.shot_no}，${stageLabel}${reviewNotes.length ? `，${reviewNotes.join('，')}` : ''}`}><b>{String(item.shot_no).padStart(2, '0')}</b><span>{stageLabel}</span>{state.grade === 'B' ? <i className="quality-review-badge">质量需复核</i> : null}{state.continuityDegraded ? <i className="continuity-degraded-badge">衔接需复核</i> : null}</button>
            })}
            {!filteredShots.length && <div className="rail-empty">当前筛选无命中镜头。<button onClick={() => { setFilters(new Set()); setFilterSource('未筛选') }}>清除筛选</button></div>}
          </nav>
          {selectedSummary && filteredShots.length > 0 && !filteredShots.some(shot => shot.id === selectedSummary.id) && <div className="filter-selection-note">当前镜头不再命中筛选，已保留当前对象；只有你主动切换时才会离开。</div>}
          {contentUpdate && <div className="filter-selection-note" role="status"><b>当前镜头内容已更新</b><span>{contentUpdate}。当前镜头与页签保持不变。</span><button onClick={() => setContentUpdate(null)}>知道了</button></div>}

          {tombstoneShotId ? (
            <section className="review-tombstone" role="alert"><h2>原镜头已删除、取消采纳或无权访问</h2><p>此前保存的镜头已不在当前生成列表。所有写操作已冻结，不会自动改作相邻镜头。</p><details><summary>技术标识</summary><code>{tombstoneShotId}</code></details><div>{shots.map(shot => <button className="btn small" key={shot.id} onClick={() => selectShot(shot.id)}>选择镜 {shot.shot_no}</button>)}</div></section>
          ) : detail.status === 'loading' ? (
            <section className="review-detail-loading" aria-busy="true"><b>正在加载镜 {selectedSummary?.shot_no} 的完整生成详情…</b><div className="review-skeleton" /><div className="review-skeleton short" /></section>
          ) : detail.status === 'error' ? (
            <section className="review-persistent-error detail" role="alert"><b>镜 {selectedSummary?.shot_no} 详情加载失败</b><p>上方状态轨仅是摘要，不代表“无参考图或无视频”。依赖详情的操作已冻结，不会改选其他镜头。</p><details><summary>查看错误详情</summary><pre>{detail.message}</pre>{detail.errorId && <code>关联标识：{detail.errorId}</code>}</details><button className="btn primary" onClick={() => { if (selectedShotId) void loadDetail(selectedShotId) }}>重试加载</button></section>
          ) : readyShot ? (
            <ShotWorkbench key={readyShot.id} shot={readyShot} episodeNo={ep.episode_no} episodeStatus={ep.status} tab={reviewTab} onTab={setReviewTab} context={context} writeFrozen={writeFrozen} generating={genMask.has(readyShot.id) || readyShot.versions.some(version => ['queued', 'running', 'waiting_provider'].includes(version.status))} setGenerating={busy => setGenMask(mask => { const next = new Set(mask); busy ? next.add(readyShot.id) : next.delete(readyShot.id); return next })} onOpen={(src, label) => setLightbox({ src, label })} onRefresh={refreshAll} onToast={showToast} />
          ) : null}

          <div className="shot-pager"><button className="btn ghost small" disabled={!filteredShots.some(shot => shot.shot_no < (selectedSummary?.shot_no ?? -Infinity))} title="Alt + ←" onClick={() => navigateIn(-1)}>← 上一镜</button><span className="pg-no">当前镜 {selectedSummary?.shot_no || '—'} · 筛选 {filteredShots.length}/{shots.length}</span><button className="btn ghost small" disabled={!filteredShots.some(shot => shot.shot_no > (selectedSummary?.shot_no ?? Infinity))} title="Alt + →" onClick={() => navigateIn(1)}>下一镜 →</button></div>
        </>
      )}

      {toast && <div className="toast review-toast" role="status"><span>{toast.message}</span>{toast.action && <button onClick={() => { toast.action?.run(); setToast(null) }}>{toast.action.label}</button>}<button aria-label="关闭消息" onClick={() => setToast(null)}>×</button></div>}
      {lightbox && <Lightbox src={lightbox.src} alt={lightbox.label} onClose={() => setLightbox(null)} />}
      {generationDecision && (
        <DecisionDialog
          title={generationDecision === 'generate'
            ? '生成本集全部视频？'
            : generationDecision === 'stop'
              ? '停止本集视频任务？'
              : generationDecision === 'resume'
                ? '继续本集未完成任务？'
                : '清空本集全部资源？'}
          summary={generationDecision === 'generate'
            ? `${shots.length} 镜 · 首轮预计 ¥${quickGenerationEstimate.toFixed(2)} · 累计授权上限 ¥${episodeBudgetCap.toFixed(2)}`
            : generationDecision === 'stop'
              ? `${generatingCount} 镜仍在处理`
              : generationDecision === 'resume'
                ? `${(ep.pipeline_summary?.paused ?? 0) + (ep.pipeline_summary?.failed ?? 0)} 镜待继续或重试`
                : `${shots.length} 个分镜 · ${episodeVideoCandidateCount} 个视频候选 · 已采用 ${adoptedCount} 镜 · 图像资源一并清空`}
          message={generationDecision === 'generate'
            ? '系统会先补齐缺失的人物与场景素材，再逐镜生成、质检并采用可用视频；不会自动拼接成片或创建交付包。'
            : generationDecision === 'stop'
              ? '系统会暂停本地排队和后续处理；供应商已接单的任务可能继续执行并产生费用。'
              : generationDecision === 'resume'
                ? '只有确认本次页面操作后，系统才会恢复预算暂停任务，并为仍未完成的镜头补建任务；可能产生新的模型费用。'
                : '将删除本集所有视频候选、采用关系、关键帧和参考图；分镜与剧本文本会保留。'}
          details={generationDecision === 'generate'
            ? [
              `最长运行 ${EPISODE_COMPLETION_WALL_CLOCK_CAP_S / 3600} 小时，达到累计授权上限后会暂停并保留进度`,
              '已采用视频会保留；新候选失败时不会覆盖旧采用版',
              '实际费用以供应商返回为准，不会超过本次累计授权上限',
            ]
            : generationDecision === 'stop'
              ? ['停止请求不代表供应商已经终止', '已完成候选和已发生费用会保留']
              : generationDecision === 'resume'
                ? ['巡检和服务重启不会自动恢复预算暂停任务', '已完成镜头会跳过', '正在执行或可复用的任务不会重复创建']
                : ['此操作不可撤销，已发生费用不会退回', '清空后需重新准备图像并生成视频']}
          confirmLabel={generationDecision === 'generate'
            ? `确认生成 ${shots.length} 镜`
            : generationDecision === 'stop'
              ? '确认停止这些任务'
              : generationDecision === 'resume'
                ? '确认继续任务'
                : '确认清空全部资源'}
          cancelLabel={generationDecision === 'stop' ? '继续运行' : '取消'}
          danger={generationDecision === 'stop' || generationDecision === 'clear'}
          onClose={closeGenerationDecision}
          onConfirm={() => executeEpisodeGenerationAction(generationDecision)}
        />
      )}
      {stalePreview && <Dialog title="旧资产影响预演" onClose={() => setStalePreview(null)} wide><div className="stale-preview-summary">共 {stalePreview.stale_count} 镜，当前选择 {staleSelection.size} 镜，选中估算 ¥{stalePreview.shots.filter(shot => staleSelection.has(shot.shot_id)).reduce((sum, shot) => sum + shot.estimated_cost_cny, 0).toFixed(2)}。旧采用版保留到新版成功。</div><div className="stale-shot-list">{stalePreview.shots.map(shot => <label key={shot.shot_id}><input type="checkbox" checked={staleSelection.has(shot.shot_id)} onChange={event => setStaleSelection(selected => { const next = new Set(selected); event.target.checked ? next.add(shot.shot_id) : next.delete(shot.shot_id); return next })} /><span><b>镜 {shot.shot_no}</b>{shot.reason_labels.join('；')} · 估算 ¥{shot.estimated_cost_cny.toFixed(2)}<small>当前分镜 {shot.current_storyboard_artifact_id || '无'} · 旧版 {shot.storyboard_artifact_id || '无'}</small><small>资产资格：{shot.asset_qualification?.length ? shot.asset_qualification.map(asset => `${artifactTypeLabel(asset.entity_type)} ${asset.entity_name || asset.ref_id || ''} · ${statusLabel(asset.gate_status)} · 版本 ${asset.asset_version || '未知'}`).join('；') : '未关联可验证人物或场景输入'}；规则版本 {shot.rule_versions?.join('、') || '未知'}</small>{shot.asset_soft_warnings?.length ? <small>提示：{shot.asset_soft_warnings.map(item => item.warning).filter(Boolean).join('；')}</small> : null}</span></label>)}</div><div className="dialog-actions"><button className="btn ghost" onClick={() => setStalePreview(null)}>取消（零任务/零扣费）</button><button className="btn primary" disabled={!staleSelection.size || staleBusy || !stalePreview.qualification.eligible_for_production} onClick={() => { void repairStale() }}>{staleBusy ? '提交中…' : '确认选中范围并修复'}</button></div></Dialog>}
    </div>
  )
}

function VideoPlanSummary({ plan }: { plan: EpisodeVideoGenerationPlan }) {
  const distribution = plan.shots.reduce<Record<string, number>>((counts, shot) => {
    counts[shot.mode] = (counts[shot.mode] || 0) + 1
    return counts
  }, {})
  const waiting = plan.shots.filter(shot =>
    shot.status === 'waiting_dependency' || Boolean(shot.depends_on_shot_id),
  ).length
  const shotNoById = new Map(plan.shots.map(shot => [shot.shot_id, shot.shot_no]))
  return <section className="video-plan-summary" aria-label="AI 视频生成计划">
    <header><div><b>AI 生成计划</b><span>系统已按镜间真实素材依赖安排安全并行</span></div><span className={`stamp ${plan.status === 'valid' ? 'green' : 'gold'}`}>{plan.status === 'valid' ? '可执行' : '需处理'}</span></header>
    <dl>
      <div><dt>参考图</dt><dd>{distribution.REFERENCE_IMAGE_MODE || 0} 镜</dd></div>
      <div><dt>上一视频尾帧首帧</dt><dd>{distribution.FIRST_FRAME_MODE || 0} 镜</dd></div>
      <div><dt>首尾帧</dt><dd>{distribution.FIRST_LAST_FRAME_MODE || 0} 镜</dd></div>
      <div><dt>视频参考</dt><dd>{distribution.VIDEO_INPUT_MODE || 0} 镜</dd></div>
      <div><dt>等待真实尾帧</dt><dd>{waiting} 镜</dd></div>
      <div><dt>预计费用</dt><dd>¥{plan.estimated_cost.toFixed(2)}</dd></div>
      <div><dt>关键路径</dt><dd>{Math.ceil(plan.critical_path_latency_ms / 60000)} 分钟</dd></div>
    </dl>
    {plan.blockers.length > 0 && <div className="video-plan-blockers" role="alert">计划仍有 {plan.blockers.length} 项阻塞，请返回分镜台或模型能力设置处理。</div>}
    <details><summary>查看计划依据与依赖</summary><p>每场首镜只使用人物谱与场景库现有图片；同场景后续镜头依次等待上一镜视频，并把真实尾帧作为本镜唯一首帧输入，不再生成剧情关键帧或静态尾帧。模式失败会保留原模式，不会自动切换。计划 revision {plan.plan_revision}，安全并行比例 {Math.round(plan.safe_parallelism_ratio * 100)}%。</p>{plan.shots.filter(shot => shot.depends_on_shot_id).map(shot => <p key={shot.shot_id}>镜 {shot.shot_no} 等待镜 {shotNoById.get(shot.depends_on_shot_id!) || '上游'}的真实尾帧 · {videoModeLabel(shot.mode)}</p>)}</details>
  </section>
}

function PipelineFilters({ shots, active, onSelect }: { shots: Shot[]; active: Set<ShotFilter>; onSelect: (filter: ShotFilter, label: string) => void }) {
  const items: Array<{ filter: ShotFilter; label: string }> = [
    { filter: 'unproduced', label: '待生成' }, { filter: 'generating', label: '生成中' },
    { filter: 'pending_adoption', label: '待采纳' }, { filter: 'adopted', label: '已采纳' },
    { filter: 'failed', label: '生成失败' },
  ]
  return <div className="wall-stats pipeline-filter-stats" aria-label="五态镜头筛选">{items.map(item => <button key={item.filter} type="button" aria-pressed={active.size === 1 && active.has(item.filter)} onClick={() => onSelect(item.filter, item.label)}>{item.label} {shots.filter(shot => matchesFilter(shot, item.filter)).length}</button>)}</div>
}

function ShotFilters({ shots, filters, source, onChange }: { shots: Shot[]; filters: Set<ShotFilter>; source: string; onChange: (next: Set<ShotFilter>, source: string) => void }) {
  const options: Array<{ id: ShotFilter; label: string; help: string }> = [
    { id: 'problem', label: '只看问题', help: '生成失败、质量需复核或衔接需复核的镜头' },
    { id: 'pending_adoption', label: '待采纳', help: '已有候选视频，但尚未确定采用版本' },
    { id: 'failed', label: '失败', help: '最近一次视频生成失败' },
    { id: 'grade_b', label: '质量需复核', help: '已有可播放采用版，但画面质量仍建议人工复核' },
    { id: 'continuity', label: '衔接需复核', help: '未使用完整首尾帧衔接，需要重点检查连续性' },
  ]
  const hitCount = filters.size ? shots.filter(shot => [...filters].every(filter => matchesFilter(shot, filter))).length : shots.length
  return <section className="shot-filter-bar" aria-label="镜头筛选"><b>镜头队列</b>{options.map(option => { const count = shots.filter(shot => matchesFilter(shot, option.id)).length; return <button type="button" key={option.id} title={option.help} aria-pressed={filters.has(option.id)} className={filters.has(option.id) ? 'active' : ''} onClick={() => { const next = new Set(filters); next.has(option.id) ? next.delete(option.id) : next.add(option.id); onChange(next, `镜头队列 · ${option.label}`) }}>{option.label} <span>{count}</span></button> })}{filters.size > 0 && <button type="button" className="clear" onClick={() => onChange(new Set(), '未筛选')}>清除 {filters.size} 个筛选</button>}<small className="filter-source">来源：{source} · 命中 {hitCount}/{shots.length}</small></section>
}

function ShotWorkbench({ shot, episodeNo, episodeStatus, tab, onTab, context, writeFrozen, generating, setGenerating, onOpen, onRefresh, onToast }: {
  shot: Shot; episodeNo: number; episodeStatus: string; tab: ReviewTab; onTab: (tab: ReviewTab) => void
  context: ReviewWallContext | null; writeFrozen: boolean; generating: boolean
  setGenerating: (busy: boolean) => void; onOpen: (src: string, label: string) => void
  onRefresh: () => Promise<void>
  onToast: (message: string, action?: { label: string; run: () => void }, persistent?: boolean) => void
}) {
  const state = shotVideoState(shot)
  const current = state.adopted || state.latest
  const visibleStatus = state.phase === 'generating' ? compactShotStage(shot) : state.label
  const panelId = `review-panel-${shot.id}`
  const focusTab = (next: ReviewTab) => {
    onTab(next)
    window.requestAnimationFrame(() => {
      document.getElementById(`review-tab-${shot.id}-${next}`)?.focus()
    })
  }
  const onTabKeyDown = (event: React.KeyboardEvent, index: number) => {
    let nextIndex = index
    if (event.key === 'ArrowRight') nextIndex = (index + 1) % REVIEW_TABS.length
    else if (event.key === 'ArrowLeft') nextIndex = (index - 1 + REVIEW_TABS.length) % REVIEW_TABS.length
    else if (event.key === 'Home') nextIndex = 0
    else if (event.key === 'End') nextIndex = REVIEW_TABS.length - 1
    else return
    event.preventDefault()
    focusTab(REVIEW_TABS[nextIndex].id)
  }
  return <article className="slide-card"><header className="slide-head"><span className="sn">镜 {shot.shot_no}</span><span className="meta">{shot.shot_size} · {shot.camera_move} · {shot.duration_s}s · {shot.transition}</span><span className="meta">{shot.scene_time ? `${shot.scene_time} · ` : ''}{shot.scene_name || shot.scene_setting}</span><span className={`stamp ${state.phase === 'adopted' ? 'green' : state.phase === 'generation_failed' ? 'red' : state.phase === 'generating' ? 'gold' : 'grey'}`} title={state.phase === 'generating' ? shot.pipeline?.stage_label || visibleStatus : undefined}>{visibleStatus}</span>{state.grade === 'B' && <span className="quality-review-badge" title={state.fallbackReason || '已有可播放采用版，但画面质量仍建议人工复核'}>质量需复核</span>}{state.continuityDegraded && <span className="continuity-degraded-badge">衔接需复核</span>}</header><nav className="review-tabs" role="tablist" aria-label={`镜 ${shot.shot_no} 生成内容`}>{REVIEW_TABS.map((item, index) => <button id={`review-tab-${shot.id}-${item.id}`} key={item.id} type="button" role="tab" aria-selected={tab === item.id} aria-controls={panelId} tabIndex={tab === item.id ? 0 : -1} className={tab === item.id ? 'active' : ''} onClick={() => onTab(item.id)} onKeyDown={event => onTabKeyDown(event, index)}>{item.label}</button>)}</nav><div id={panelId} className="review-workbench-panel" role="tabpanel" aria-labelledby={`review-tab-${shot.id}-${tab}`}>
    {tab === 'text' && <InfoSection shot={shot} current={current} />}
    {tab === 'references' && <MaterialGallery shot={shot} productionEligible={!writeFrozen} onOpen={onOpen} onRefresh={onRefresh} onToast={onToast} />}
    {tab === 'videos' && <VideoPreviewWorkspace shot={shot} episodeNo={episodeNo} episodeStatus={episodeStatus} context={context} generating={generating} setGenerating={setGenerating} writeFrozen={writeFrozen} onRefresh={onRefresh} onToast={onToast} />}
  </div></article>
}

export function InfoSection({ shot, current }: { shot: Shot; current?: ShotVersion }) {
  const dialogue = (shot.dialogues ?? []).map(line => `${line.speaker}：${line.line}${line.emotion && line.emotion !== '平静' ? `（${line.emotion}）` : ''}`).join('\n')
  const prompt = current?.prompt_text || shot.prompt_preview || ''
  const aiPromptReady = Boolean(current?.image_inputs?.ai_video_prompt_contract_version)
  const modePlan = shot.mode_plan
  const actualMode = current?.image_inputs?.actual_mode
  const reusesStaticTail = modePlan?.required_assets?.some(
    asset => asset.source === 'PREVIOUS_STATIC_TAIL',
  )
  const copy = async (text: string) => { try { await navigator.clipboard.writeText(text) } catch { /* clipboard permission */ } }
  return <div className="info-section">
    {modePlan && <section className="script-card video-mode-audit">
      <div className="script-card-head">视频生成方式</div>
      <div className="video-mode-route"><b>{videoModeLabel(modePlan.mode)}</b><span>→</span><b>{actualMode ? videoModeLabel(actualMode) : modePlan.depends_on_shot_id ? '等待上游素材' : '待执行'}</b></div>
      <p>{modePlan.degraded_reason || (modePlan.depends_on_shot_id ? '场景第二镜会先生成自己的静态尾帧，再等待场景首镜真实尾帧。' : reusesStaticTail ? '本镜首帧直接复用上一镜静态尾帧，不等待上一镜视频；本镜尾帧可供下一镜复用。' : '本镜无动态上游素材依赖，可安全并行。')}</p>
      <details><summary>计划依据</summary><p>{videoModeReasonText(modePlan.reason_codes)}</p><p>置信度 {Math.round(modePlan.confidence * 100)}% · {modePlan.video_input_intent ? `参考意图 ${modePlan.video_input_intent}` : '无视频参考意图'}</p></details>
    </section>}
    <section className="script-card">
      <div className="script-card-head">原文摘录 <button className="text-action" onClick={() => { void copy(shot.source_excerpt || '') }}>复制</button></div>
      <div className={`script-source${shot.source_excerpt ? '' : ' empty'}`}>{shot.source_excerpt || '暂无原文摘录'}</div>
    </section>
    <section className="script-card">
      <div className="script-card-head">镜头信息</div>
      <dl className="script-meta-grid"><Meta label="场景图" value={shot.scene_name || shot.scene_setting} /><Meta label="时间" value={shot.scene_time || '未设置'} /><Meta label="角色" value={commaList(shot.characters)} /><Meta label="时长" value={`${shot.duration_s}s`} /><Meta label="镜头" value={`${shot.shot_size} / ${shot.camera_move}`} /><Meta label="转场" value={shot.transition} /><Meta label="衔接" value={shot.continuity_mode || (shot.continuity_from_prev ? '接上镜' : '新场景')} /></dl>
    </section>
    <section className="script-card continuity-card">
      <div className="script-card-head">视频连续性</div>
      <div className="continuity-flow"><div><b>输入状态</b><p>{shot.state_in || shot.first_frame_desc || '未设置'}</p></div><span>→</span><div><b>主要动作</b><p>{shot.primary_action || shot.action_desc || '未设置'}</p></div><span>→</span><div><b>输出状态</b><p>{shot.state_out || shot.last_frame_desc || '未设置'}</p></div></div>
      {current?.qa?.failure_types?.length ? <div className="continuity-risk" role="status"><b>连续性风险</b>{current.qa.failure_types.join('、')}<p>观测输出：{current.qa.observed_state_out || '未返回'}</p></div> : <div className="continuity-ok">暂无已知高风险差异</div>}
    </section>
    <section className="script-card h3-prompt-card">
      <div className="script-card-head">
        {aiPromptReady ? 'AI 最终 H3 Prompt' : 'AI H3 Prompt'}
        {prompt && <button className="text-action" onClick={() => { void copy(prompt) }}>复制</button>}
      </div>
      {aiPromptReady && prompt
        ? <pre className="h3-prompt-output">{prompt}</pre>
        : <div className="script-source empty">等待 AI 完成 Physical Performance 编译；当前未生成可提交 H3 的提示词。</div>}
    </section>
    <section className="script-card">
      <div className="script-card-head">镜头脚本 <button className="text-action" onClick={() => { void copy([shot.action_desc, shot.narration, dialogue].filter(Boolean).join('\n')) }}>复制业务文本</button></div>
      <div className="script-block"><div className="script-paragraph"><span className="script-label">画面</span><p>{shot.action_desc}</p></div>{shot.narration && <div className="script-paragraph"><span className="script-label">旁白</span><p>{shot.narration}</p></div>}{dialogue && <div className="script-paragraph"><span className="script-label">台词</span><pre className="script-dialogues">{dialogue}</pre></div>}</div>
    </section>
  </div>
}

function Meta({ label, value }: { label: string; value: string }) { return <div className="script-meta-item"><dt className="script-meta-label">{label}</dt><dd className="script-meta-value">{value}</dd></div> }

export function boundarySourceLabel(source?: string | null): string {
  const labels: Record<string, string> = {
    PREVIOUS_ADOPTED_TAIL: '上一镜真实视频尾帧',
    PREVIOUS_STATIC_TAIL: '上一镜静态尾帧',
    STATIC_BOUNDARY_ASSET: '本镜生成关键帧',
  }
  return labels[source || ''] || source || '来源待同步'
}

export function MaterialGallery({ shot, productionEligible, onOpen, onRefresh, onToast }: { shot: Shot; productionEligible: boolean; onOpen: (src: string, label: string) => void; onRefresh: () => Promise<void>; onToast: (message: string, action?: { label: string; run: () => void }, persistent?: boolean) => void }) {
  const kind = shotMaterialLibraryKind(shot)
  const material = currentMaterialVersion(shot, kind)
  const version = material?.version
  const refs = kind === 'references'
    ? version?.image_inputs?.reference_images ?? []
    : []
  const buckets = classifyReferenceBuckets(refs)
  const [restore, setRestore] = useState<ReferenceImage | null>(null)
  const [reason, setReason] = useState('')
  const [clearing, setClearing] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [clearReferencesConfirm, setClearReferencesConfirm] = useState(false)
  const referenceTaskActive = shot.versions.some(version =>
    ['queued', 'running', 'waiting_provider', 'paused'].includes(version.status),
  )
  const clearReferencesDisabledReason = clearing
    ? '正在清空参考图'
    : !refs.length
      ? '当前镜头没有可清空的参考图'
      : referenceTaskActive
        ? '视频任务仍在处理，请先停止或等待完成'
        : ''
  const act = async (operation: () => Promise<unknown>, success: string) => { try { await operation(); onToast(success); await onRefresh() } catch (error) { onToast(error instanceof Error ? error.message : String(error), undefined, true) } }
  const discard = async (ref: ReferenceImage) => {
    if (!version) return
    await act(() => api.discardReferenceImage(version.id, ref.id), `已废弃「${referenceLibraryLabel(ref)}」`)
    onToast(`已废弃「${referenceLibraryLabel(ref)}」`, { label: '撤销', run: () => { void act(() => api.restoreReferenceImage(version.id, ref.id, '撤销刚才的人工废弃'), '已撤销废弃') } })
  }
  const clearReferences = async () => {
    setClearing(true)
    try {
      await api.clearShotReferences(shot.id)
      onToast(`镜 ${shot.shot_no} 的参考图已清空，已有视频保持不变`)
      await onRefresh()
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), undefined, true)
    } finally {
      setClearing(false)
    }
  }
  const refreshReferences = async () => {
    setRefreshing(true)
    try {
      await onRefresh()
    } finally {
      setRefreshing(false)
    }
  }
  const taskActive = shot.versions.some(candidate =>
    ['queued', 'running', 'waiting_provider'].includes(candidate.status),
  )
  const emptyHint = taskActive
    ? '素材可能仍在生成，请稍后刷新。'
    : productionEligible
      ? '可通过「新建视频版本」准备当前模式所需素材。'
      : '请先完成上游人工确认。'
  const renderReferences = (title: string, items: ReferenceImage[], discarded = false) => <section className={`material-group${discarded ? ' discarded' : ''}`}><header>{title} · {items.length}</header>{items.length ? <div className="material-strip">{items.map(ref => { const score = refScore(ref); const label = referenceLibraryLabel(ref); const reject = rejectReasonInfo(ref.rejectReason); const hard = ref.qa?.hard_failures || ref.hard_failures || []; const eligible = Boolean(ref.image_url); return <figure key={ref.id} className={`material-card${discarded ? ' material-card-discarded' : ''}`}><button type="button" className="mc-thumb" disabled={!ref.image_url} aria-label={`预览${label}`} onClick={() => ref.image_url && onOpen(ref.image_url, label)}>{ref.image_url ? <img src={ref.image_url} alt={label} loading="lazy" /> : <span className="mc-noimg">无图</span>}{score != null && <span className={`mc-qa-badge${score < 0.8 ? ' bad' : ''}`}>质检 {score.toFixed(2)}</span>}{!!hard.length && <span className="mc-gate-badge">⚠ 质检提示</span>}</button><figcaption><b>{label}</b>{ref.selection_reason && <span>选择：{ref.selection_reason}</span>}{discarded && <span className={`mc-reject risk-${reject.risk}`}>{reject.label}</span>}<span>来源 {ref.entity_name || ref.source} · 资产版本 {ref.library_revision_id || ref.library_view_id || '未关联'}</span><span>引用版本 {ref.referenced_by_version_ids?.join('、') || '未关联'}</span>{ref.soft_warnings?.map(warning => <span className="warn" key={warning}>提示：{warning}</span>)}<details><summary>技术信息与修复建议</summary><code>素材标识：{ref.id}</code><code>淘汰原因：{ref.rejectReason || '无'}</code><p>{reject.suggestion}</p><p>规则版本 {ref.rule_version || '未知'}</p></details>{discarded ? <button className="mc-action restore" disabled={!productionEligible || !eligible} title={!eligible ? '图片文件不可用' : !productionEligible ? '上游资格不满足' : '质检仅作提示，可恢复为生产输入'} onClick={() => { setRestore(ref); setReason('') }}>恢复使用</button> : <button className="mc-action discard" onClick={() => { void discard(ref) }}>废弃/隔离</button>}</figcaption></figure> })}</div> : <div className="review-state-empty"><b>暂无该类参考图</b><p>{emptyHint}</p></div>}</section>
  const inputs = version?.image_inputs
  const materialMode = shot.mode_plan?.mode || inputs?.mode
  const keyframes = [
    {
      id: 'first_frame',
      label: '首帧',
      imageUrl: inputs?.first_frame_image_url,
      source: inputs?.first_frame_source || inputs?.first_frame_src,
    },
    {
      id: 'last_frame',
      label: '尾帧',
      imageUrl: inputs?.last_frame_image_url,
      source: inputs?.last_frame_source || inputs?.last_frame_src,
    },
  ].filter(frame => materialMode !== 'FIRST_FRAME_MODE' || frame.id === 'first_frame')
  const subtitle = kind === 'keyframes'
    ? materialMode === 'FIRST_FRAME_MODE'
      ? '仅展示从上一镜视频真实尾帧抽取的本镜首帧'
      : '仅展示历史首尾帧模式实际使用的首帧与尾帧'
    : kind === 'video'
      ? '仅展示视频参考模式绑定的上游视频'
      : '仅展示参考图模式的实际参考图和质检依据'
  return <div className="candidate-compare material-review"><header className="asset-workspace-toolbar"><div><b>{materialLibraryTitle(kind)}</b><span>{subtitle}</span></div><div className="asset-workspace-actions"><button type="button" className="btn ghost small" disabled={refreshing} title="重新加载当前模式素材" onClick={() => { void refreshReferences() }}>{refreshing ? '刷新中…' : '刷新状态'}</button>{kind === 'references' && <button type="button" className="btn ghost small danger" disabled={Boolean(clearReferencesDisabledReason)} aria-label={clearReferencesDisabledReason ? `清空参考图，暂不可用：${clearReferencesDisabledReason}` : `清空镜 ${shot.shot_no} 的参考图`} title={clearReferencesDisabledReason || '只删除本镜创建的参考图，不删除已有视频'} onClick={() => setClearReferencesConfirm(true)}>{clearing ? '清空中…' : '清空参考图'}</button>}</div></header>{material?.isFallback && <div className="material-fallback-note">当前版本素材未就绪，暂显示最近有素材版本 v{version?.version_no}</div>}{kind === 'keyframes' && <section className="material-group"><header>关键帧 · {keyframes.filter(frame => frame.imageUrl).length}/{keyframes.length}</header><div className="material-strip">{keyframes.map(frame => <figure key={frame.id} className="material-card material-card-keyframe"><button type="button" className="mc-thumb" disabled={!frame.imageUrl} aria-label={`预览${frame.label}`} onClick={() => frame.imageUrl && onOpen(frame.imageUrl, frame.label)}>{frame.imageUrl ? <img src={frame.imageUrl} alt={frame.label} loading="lazy" /> : <span className="mc-noimg">待生成</span>}</button><figcaption><b>{frame.label}</b><span>来源 {boundarySourceLabel(frame.source)}</span><span>引用视频版本 v{version?.version_no ?? '—'}</span></figcaption></figure>)}</div></section>}{kind === 'video' && <section className="material-group"><header>视频输入 · {inputs?.video_input_url ? 1 : 0}</header>{inputs?.video_input_url ? <div className="material-video-input"><video controls preload="metadata" src={inputs.video_input_url}>当前浏览器不支持视频预览。</video><div><b>上游视频</b><span>来源版本 {inputs.video_input_source_revision_id || '未关联'}</span></div></div> : <div className="review-state-empty"><b>暂无可用视频输入</b><p>{emptyHint}</p></div>}</section>}{kind === 'references' && <>{renderReferences('实际提交参考图', buckets.video)}{renderReferences('质检依据参考图', buckets.evidence)}{buckets.discarded.length > 0 && renderReferences('废弃参考图', buckets.discarded, true)}</>}{restore && version && kind === 'references' && <Dialog title={`恢复参考图·${referenceLibraryLabel(restore)}`} onClose={() => setRestore(null)}><div className="review-impact"><p>原因：{rejectReasonInfo(restore.rejectReason).label}；质检分 {refScore(restore)?.toFixed(2) || '未评估'}；风险 {REFERENCE_RISK_LABEL[rejectReasonInfo(restore.rejectReason).risk]}。</p><p>恢复后只会成为后续新视频的候选输入，不改写已有历史视频。</p></div><label className="review-field">必填理由<textarea value={reason} onChange={event => setReason(event.target.value)} rows={4} /></label><div className="dialog-actions"><button className="btn ghost" onClick={() => setRestore(null)}>取消</button><button className="btn primary" disabled={!reason.trim()} onClick={() => { void act(() => api.restoreReferenceImage(version.id, restore.id, reason.trim()), '参考图已恢复'); setRestore(null) }}>确认恢复并记录审计</button></div></Dialog>}{clearReferencesConfirm && kind === 'references' && <DecisionDialog title={`清空镜 ${shot.shot_no} 的参考图？`} summary={`${refs.length} 张参考图将被删除`} message="将删除本镜创建或收集的参考图记录；已有视频、剧本和分镜不会删除。" details={['此操作不可撤销', '后续再次生成视频时需要重新准备参考图']} confirmLabel="确认清空参考图" cancelLabel="保留参考图" danger onClose={() => setClearReferencesConfirm(false)} onConfirm={() => { setClearReferencesConfirm(false); void clearReferences() }} />}</div>
}

export function resolvePreviewVersionId(versions: ShotVersion[], currentId: string | null): string | null {
  const playable = versions.filter(version => Boolean(version.video_url))
  if (currentId && playable.some(version => version.id === currentId)) return currentId
  return [...playable].sort((left, right) => right.version_no - left.version_no)[0]?.id || null
}

export function visibleVideoVersions(versions: ShotVersion[]): ShotVersion[] {
  return versions
    .filter(version => version.status !== 'references_ready')
    .sort((left, right) => right.version_no - left.version_no)
}

export function countReferenceImages(versions: ShotVersion[]): number {
  return versions.reduce(
    (sum, version) => sum + (version.image_inputs?.reference_images?.length ?? 0),
    0,
  )
}

export function isProviderCreateUnresolved(
  pipeline?: Shot['pipeline'] | null,
): boolean {
  return pipeline?.reason_code === 'VIDEO_PROVIDER_CREATE_UNRESOLVED'
}

export const PROVIDER_RESUBMISSION_WARNING =
  '系统未找到可继续查询的供应商任务编号。请先核对供应商后台；确认后会创建新的 operation ID，重新核算本集额度并建立独立预算 claim。原请求费用仍可能已经产生。'

function VideoPreviewWorkspace({ shot, episodeNo, episodeStatus, context, generating, setGenerating, writeFrozen, onRefresh, onToast }: { shot: Shot; episodeNo: number; episodeStatus: string; context: ReviewWallContext | null; generating: boolean; setGenerating: (busy: boolean) => void; writeFrozen: boolean; onRefresh: () => Promise<void>; onToast: (message: string, action?: { label: string; run: () => void }, persistent?: boolean) => void }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [previewId, setPreviewId] = useState<string | null>(() => resolvePreviewVersionId(shot.versions, null))
  const [playbackRates, setPlaybackRates] = useState<Record<string, number>>(() => Object.fromEntries(
    shot.versions.map(version => [version.id, videoPlaybackRate(version)]),
  ))
  const [adopt, setAdopt] = useState<ShotVersion | null>(null)
  const [adoptReason, setAdoptReason] = useState('')
  const [wizard, setWizard] = useState<VideoGenerationMode | null>(null)
  const [prompt, setPrompt] = useState('')
  const [mediaError, setMediaError] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<ShotVersion | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [adopting, setAdopting] = useState(false)
  const [cancellingAdoption, setCancellingAdoption] = useState(false)
  const [candidateAction, setCandidateAction] = useState<{ kind: 'archive' | 'restore'; versionId: string } | null>(null)
  const [clearingResources, setClearingResources] = useState(false)
  const [clearResourcesConfirm, setClearResourcesConfirm] = useState(false)
  const [providerRecoveryBusy, setProviderRecoveryBusy] = useState(false)
  const [confirmProviderResubmission, setConfirmProviderResubmission] = useState(false)
  const playableKey = shot.versions.map(version => `${version.id}:${Boolean(version.video_url)}:${videoPlaybackRate(version)}`).join('|')

  useEffect(() => {
    setPreviewId(current => resolvePreviewVersionId(shot.versions, current))
  }, [playableKey, shot.versions])
  useEffect(() => {
    setPlaybackRates(current => Object.fromEntries(
      shot.versions.map(version => [
        version.id,
        Object.prototype.hasOwnProperty.call(current, version.id)
          ? current[version.id]
          : videoPlaybackRate(version),
      ]),
    ))
  }, [playableKey, shot.id, shot.versions])
  useEffect(() => { setMediaError(null) }, [previewId])
  useEffect(() => {
    setDeleteTarget(null)
    setDeleting(false)
    setAdopt(null)
    setAdopting(false)
    setCancellingAdoption(false)
    setCandidateAction(null)
    setProviderRecoveryBusy(false)
    setConfirmProviderResubmission(false)
  }, [shot.id])
  useEffect(() => {
    if (wizard !== 'rewrite') return
    const saved = localStorage.getItem(generationDraftKey(shot.id))
    if (saved) setPrompt(saved)
  }, [shot.id, wizard])
  useEffect(() => {
    if (wizard === 'rewrite' && prompt.trim()) localStorage.setItem(generationDraftKey(shot.id), prompt)
  }, [prompt, shot.id, wizard])

  const selected = shot.versions.find(version => version.id === previewId)
  const selectedModelRejected = isVideoModelInputRejection(
    selected?.error,
    shot.pipeline?.reason_code,
  )
  const selectedPlaybackRate = selected ? playbackRates[selected.id] ?? videoPlaybackRate(selected) : 1
  const selectedRateChanged = Boolean(
    selected && Math.abs(selectedPlaybackRate - videoPlaybackRate(selected)) > 0.0001,
  )
  useEffect(() => {
    if (videoRef.current) videoRef.current.playbackRate = selectedPlaybackRate
  }, [previewId, selectedPlaybackRate])
  const adoptedVersion = shot.versions.find(version => version.id === shot.adopted_version_id)
  const versions = useMemo(() => visibleVideoVersions(shot.versions), [shot.versions])
  const shotReferenceImageCount = countReferenceImages(shot.versions)
  const providerCreateUnresolved = isProviderCreateUnresolved(shot.pipeline)
  const videoTaskActive = versions.some(version =>
    ['queued', 'running', 'waiting_provider', 'paused'].includes(version.status),
  )
  const createVideoDisabledReason = generating
    ? '当前视频任务正在处理，请等待完成'
    : providerCreateUnresolved
      ? '供应商可能已接收当前创建请求，请先核对并恢复原任务'
    : !['confirmed', 'generating', 'done'].includes(episodeStatus)
      ? '请先在分镜台确认本集分镜'
      : writeFrozen
        ? context?.upstream.blockers.join('；') || '正在核对生成资格，请稍后重试'
        : ''
  const clearResourcesDisabledReason = clearingResources
    ? '正在清空资源'
    : !versions.length && shotReferenceImageCount === 0
      ? '当前镜头没有可清空的视频或图像资源'
      : videoTaskActive
        ? '视频任务仍在处理，请先停止或等待完成'
        : ''
  const adoptDisabledReason = writeFrozen
    ? context?.upstream.blockers.join('；') || '当前生成资格未通过'
    : ''
  const pipelineBlocked = !providerCreateUnresolved && (
    shot.pipeline?.pipeline_status === 'waiting_human'
    || shot.pipeline?.pipeline_stage === 'preflight_blocked'
    || shot.pipeline?.pipeline_stage === 'waiting_human'
  )
  const preflightRetrying = shot.pipeline?.pipeline_stage === 'preflight_retry'
    || shot.pipeline?.pipeline_stage === 'preflight_validating'

  const runGeneration = async () => {
    if (!wizard || !context) return
    const next = prompt.trim()
    if (wizard === 'rewrite' && !next) {
      onToast('请填写给 AI 的导演要求')
      return
    }
    setGenerating(true)
    try {
      const result = await api.shotGenerate(shot.id, wizard === 'rewrite' ? next : undefined, wizard !== 'rewrite', wizard === 'critique', context.upstream.qualification_version, newId(`generate:${shot.id}`)) as { reused?: boolean; version_id?: string; job_id?: string; status?: string }
      if (wizard === 'rewrite') localStorage.removeItem(generationDraftKey(shot.id))
      onToast(result.reused ? '输入未变化，已复用旧版；原词新候选会强制新建版本' : `请求已接受${result.job_id ? `，任务 ${result.job_id}` : ''}；正在排队/生成，尚未完成`)
      setWizard(null)
      await onRefresh()
    } catch (error) {
      // #region debug-point B-C:shot-generation-error
      void fetch('http://127.0.0.1:7778/event', { method: 'POST', body: JSON.stringify({ sessionId: 'video-generation-noop', runId: 'post-fix', hypothesisId: 'B', location: 'frontend/src/pages/WallPage.tsx:runGeneration.error', msg: '[DEBUG] Shot generation API failed', data: { shotId: shot.id, shotNo: shot.shot_no, errorName: error instanceof Error ? error.name : typeof error, errorMessage: error instanceof Error ? error.message : String(error) }, ts: Date.now() }) }).catch(() => {})
      // #endregion
      onToast(error instanceof Error ? error.message : String(error), undefined, true)
    } finally {
      setGenerating(false)
    }
  }

  const recoverProviderCreate = async (allowNewSubmission = false) => {
    const taskId = shot.pipeline?.task_id
    if (!taskId) {
      onToast('当前任务缺少可恢复的任务标识，请到监制房查看详情', undefined, true)
      return
    }
    setProviderRecoveryBusy(true)
    try {
      const result = await api.post(`/system/jobs/${encodeURIComponent(taskId)}/retry`, {
        expected_version: shot.pipeline?.state_revision ?? 0,
        allow_new_submission: allowNewSubmission,
      }) as { retryability?: { action?: string } }
      setConfirmProviderResubmission(false)
      onToast(
        result.retryability?.action === 'continue_poll'
          ? '已恢复原供应商任务，正在继续查询；未重复提交 create'
          : '已确认重新提交，系统正在重新校验预算',
      )
      await onRefresh()
    } catch (error) {
      if (
        error instanceof ApiError
        && error.code === 'PROVIDER_HANDLE_UNCONFIRMED'
        && !allowNewSubmission
      ) {
        setConfirmProviderResubmission(true)
        return
      }
      onToast(error instanceof Error ? error.message : String(error), undefined, true)
    } finally {
      setProviderRecoveryBusy(false)
    }
  }

  const doAdopt = async () => {
    if (!adopt || !context) return
    const playbackRate = playbackRates[adopt.id] ?? videoPlaybackRate(adopt)
    setAdopting(true)
    try {
      await api.adoptVersion(shot.id, adopt.id, adoptReason.trim(), playbackRate, context.upstream.qualification_version, newId(`adopt:${adopt.id}`))
      setAdopt(null)
      setPreviewId(adopt.id)
      onToast(`已按 ${playbackRate}× 定稿采用 v${adopt.version_no}，合成将使用该倍速`)
      await onRefresh()
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), undefined, true)
    } finally {
      setAdopting(false)
    }
  }

  const archive = async (version: ShotVersion) => {
    setCandidateAction({ kind: 'archive', versionId: version.id })
    try {
      await api.archiveVersion(version.id, '候选版本整理')
      await onRefresh()
      onToast(`v${version.version_no} 已归档`)
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), undefined, true)
    } finally {
      setCandidateAction(null)
    }
  }
  const unarchive = async (version: ShotVersion) => {
    setCandidateAction({ kind: 'restore', versionId: version.id })
    try {
      await api.unarchiveVersion(version.id)
      await onRefresh()
      onToast(`v${version.version_no} 已恢复到候选列表`)
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), undefined, true)
    } finally {
      setCandidateAction(null)
    }
  }
  const remove = async (version: ShotVersion) => {
    setDeleting(true)
    try {
      await api.deleteVersion(version.id)
      setDeleteTarget(null)
      await onRefresh()
      onToast(`v${version.version_no} 已删除`)
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), undefined, true)
    } finally {
      setDeleting(false)
    }
  }
  const openAdopt = (version: ShotVersion) => {
    setPreviewId(version.id)
    setAdopt(version)
    setAdoptReason('')
  }
  const cancelAdoption = async () => {
    setCancellingAdoption(true)
    try {
      await api.cancelShotAdoption(shot.id)
      onToast(`已取消镜 ${shot.shot_no} 的采纳；候选仍保留，成片只会使用重新采纳或自动择优的真实模型视频`)
      await onRefresh()
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), undefined, true)
    } finally {
      setCancellingAdoption(false)
    }
  }
  const clearResources = async () => {
    setClearingResources(true)
    try {
      await api.clearShotArtifacts(shot.id)
      setPreviewId(null)
      onToast(`镜 ${shot.shot_no} 的视频和图像资源已清空`)
      await onRefresh()
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), undefined, true)
    } finally {
      setClearingResources(false)
    }
  }

  return <div className="video-preview-workspace">
    <section className="asset-workspace-toolbar video-toolbar">
      <div><b>本镜视频</b><span>单次估算 ￥{shot.est_cost_cny.toFixed(2)} · 候选 {versions.length} 个</span></div>
      <div className="asset-workspace-actions"><button className="btn primary small" disabled={Boolean(createVideoDisabledReason)} aria-label={createVideoDisabledReason ? `新建视频版本，暂不可用：${createVideoDisabledReason}` : `为镜 ${shot.shot_no} 新建视频版本`} title={createVideoDisabledReason || '提交前会先展示输入方式、范围和估算费用'} onClick={() => { setWizard('reroll'); setPrompt('') }}>新建视频版本</button><button type="button" className="btn ghost small danger" disabled={Boolean(clearResourcesDisabledReason)} aria-label={clearResourcesDisabledReason ? `清空资源，暂不可用：${clearResourcesDisabledReason}` : `清空镜 ${shot.shot_no} 的视频和图像资源`} title={clearResourcesDisabledReason || '删除本镜视频、关键帧和参考图'} onClick={() => setClearResourcesConfirm(true)}>{clearingResources ? '清空中…' : '清空资源'}</button></div>
    </section>
    {providerCreateUnresolved && <section className="review-persistent-error compact" role="alert"><b>供应商 create 结果待核对</b><span>{shot.pipeline?.reason_text || '供应商可能已接收创建请求，但系统尚未取得可查询的任务编号。'}</span><p>先尝试恢复原供应商任务，此操作不会重新提交 create。只有确认旧任务无法核对后，页面才会提供重新提交入口。</p><button type="button" className="btn primary small" disabled={providerRecoveryBusy || !shot.pipeline?.task_id} onClick={() => { void recoverProviderCreate(false) }}>{providerRecoveryBusy ? '正在核对…' : '仅恢复原任务并继续查询'}</button>{shot.pipeline?.task_id && <details><summary>任务信息</summary><code>{shot.pipeline.task_id}</code></details>}</section>}
    {pipelineBlocked && <section className="review-persistent-error compact" role="alert"><b>视频任务尚未进入生成</b><span>{shot.pipeline?.reason_text || shot.pipeline?.blocked_reason || '视频输入需要处理后才能继续。'}</span><p>请先按提示修正分镜内容；修正后点击「新建视频版本」会复用当前任务并重新校验，不会重复提交供应商。</p>{shot.pipeline?.task_id && <details><summary>任务信息</summary><code>{shot.pipeline.task_id}</code></details>}</section>}
    {preflightRetrying && <section className="material-fallback-note" role="status"><b>{shot.pipeline?.pipeline_stage === 'preflight_validating' ? '正在校验视频输入' : '校验遇到瞬时故障，等待自动重试'}</b><span>{shot.pipeline?.reason_text || '此阶段尚未提交供应商，不会产生视频费用。'}{shot.pipeline?.next_retry_at ? ` 下次检查：${new Date(shot.pipeline.next_retry_at * 1000).toLocaleTimeString()}` : ''}</span></section>}
    <div className="video-preview-layout">
      <section className="video-preview-player" aria-label="单视频预览">
        <header><div><span>当前预览</span><b>{selected ? `镜 ${shot.shot_no} · v${selected.version_no}` : `镜 ${shot.shot_no}`}</b></div>{selected && <span className={`stamp ${selected.status === 'failed' ? 'red' : selected.status === 'succeeded' ? 'green' : 'gold'}`}>{videoVersionStatusLabel(selected, selected.id === shot.adopted_version_id)}</span>}</header>
        {selected?.video_url ? <video ref={videoRef} key={selected.id} src={selected.video_url} controls preload="metadata" onLoadedMetadata={event => { event.currentTarget.playbackRate = selectedPlaybackRate }} onLoadedData={() => setMediaError(null)} onError={() => setMediaError(`无法加载 v${selected.version_no} 的媒体，请检查访问权限或稍后重试`)} /> : <div className="video-preview-empty"><b>暂无可预览视频</b><span>请从右侧选择已生成的候选；排队中或失败版本仍会保留在列表中。</span></div>}
        {mediaError && <div className="review-persistent-error compact" role="alert">{mediaError}</div>}
        {!mediaError && selected?.error && <div className="review-persistent-error compact" role="alert"><b>{selectedModelRejected ? '当前视频模型拒绝了计划输入' : '该候选生成未完成'}</b><span>{selectedModelRejected ? '系统已保留原生成模式和输入素材，没有自动降级。请更换视频模型后重新生成本镜。' : '候选记录已保留；可按错误提示修复同模式输入后新建版本。'}</span>{selectedModelRejected && <a className="btn primary small" href="/system/models">去模型中心更换视频模型</a>}<details><summary>查看错误详情</summary><pre>{selected.error}</pre></details></div>}
        {selected && <div className="video-preview-summary"><span>质检分 <b>{selected.qa?.overall?.toFixed(2) ?? '未评估'}</b></span><span>费用 <b>￥{selected.cost_cny.toFixed(2)}</b></span><span>耗时 <b>{selected.latency_s.toFixed(1)} 秒</b></span><span>定稿倍速 <b>{videoPlaybackRate(selected)}×</b></span></div>}
        {selected?.qa?.issues?.length ? <p className="video-preview-issues">{selected.qa.issues.join('；')}</p> : null}
        {selected?.video_url && <div className="video-playback-control"><label htmlFor={`video-rate-${selected.id}`}>预览 / 定稿倍速</label><select id={`video-rate-${selected.id}`} value={selectedPlaybackRate} onChange={event => setPlaybackRates(current => ({ ...current, [selected.id]: Number(event.target.value) }))}>{VIDEO_PLAYBACK_RATES.map(rate => <option key={rate} value={rate}>{rate}×</option>)}</select><small>{selectedRateChanged ? `尚未定稿：当前预览 ${selectedPlaybackRate}×` : `已定稿 ${videoPlaybackRate(selected)}×`}；采纳后成片会实际按此倍速合成。</small></div>}
        <div className="video-preview-actions">{selected?.video_url && <a className="btn ghost small" href={selected.video_url} download={`ep-${episodeNo}-shot-${shot.shot_no}-v${selected.version_no}-${selected.id === shot.adopted_version_id ? 'adopted' : 'candidate'}.mp4`}>导出当前视频</a>}{selected?.video_url && selected.id !== shot.adopted_version_id && !context?.archived_versions[selected.id] && <button className="btn primary small" disabled={Boolean(adoptDisabledReason)} title={adoptDisabledReason || '按当前预览倍速采纳；提交前需填写判断理由'} onClick={() => openAdopt(selected)}>按 {selectedPlaybackRate}× 定稿采纳</button>}{selected?.id === shot.adopted_version_id && <><span className="stamp green">当前采用版本 · {videoPlaybackRate(selected)}×</span>{selectedRateChanged && <button type="button" className="btn primary small" disabled={Boolean(adoptDisabledReason)} onClick={() => openAdopt(selected)}>改为 {selectedPlaybackRate}× 定稿</button>}<button type="button" className="btn ghost small danger" disabled={cancellingAdoption} title="保留真实模型候选，只取消当前采纳；成片不会用图片或静音片段代替本镜" onClick={() => { void cancelAdoption() }}>{cancellingAdoption ? '取消中…' : '取消采纳'}</button></>}</div>
      </section>
      <section className="video-candidate-list" aria-label="视频候选列表">
        <header><div><b>全部候选</b><span>{versions.length}</span></div><small>按版本从新到旧</small></header>
        <div className="video-candidate-scroll">{versions.length ? versions.map(version => {
          const adopted = version.id === shot.adopted_version_id
          const archived = Boolean(context?.archived_versions[version.id])
          const selectedCandidate = version.id === previewId
          return <article className={`video-candidate-card${selectedCandidate ? ' selected' : ''}${adopted ? ' adopted' : ''}${archived ? ' archived' : ''}`} key={version.id}>
            <button type="button" className="video-candidate-select" aria-pressed={selectedCandidate} title={version.video_url ? `选择 v${version.version_no} 预览` : `选择 v${version.version_no} 查看生成状态`} onClick={() => setPreviewId(version.id)}>
              <span className="video-candidate-title"><b>v{version.version_no}</b><span className={`stamp ${version.status === 'failed' ? 'red' : version.status === 'succeeded' ? 'green' : 'gold'}`}>{videoVersionStatusLabel(version, adopted)}</span>{archived && <span className="stamp grey">已归档</span>}</span>
              <span className="video-candidate-metrics"><span>质检 {version.qa?.overall?.toFixed(2) ?? '—'}</span><span>￥{version.cost_cny.toFixed(2)}</span><span>{version.latency_s.toFixed(1)} 秒</span><span>{playbackRates[version.id] ?? videoPlaybackRate(version)}×</span></span>
              <span className="video-candidate-note">{videoCandidateNote(version)}</span>
            </button>
            <div className="video-candidate-actions">{version.video_url && !adopted && !archived && <button type="button" className="btn primary small" disabled={Boolean(adoptDisabledReason || candidateAction)} title={adoptDisabledReason || '采纳前需填写理由'} onClick={() => openAdopt(version)}>采纳</button>}{!adopted && !archived && <button type="button" className="btn ghost small" disabled={Boolean(candidateAction)} title="暂时从候选列表隐藏，可随时恢复" onClick={() => { void archive(version) }}>{candidateAction?.kind === 'archive' && candidateAction.versionId === version.id ? '归档中…' : '归档'}</button>}{!adopted && !archived && !['queued', 'running', 'waiting_provider'].includes(version.status) && <button type="button" className="btn ghost small danger" disabled={Boolean(candidateAction)} title="永久删除此候选，操作前会再次确认" onClick={() => setDeleteTarget(version)}>删除候选</button>}{archived && <button type="button" className="btn ghost small" disabled={Boolean(candidateAction)} title="恢复到可操作的候选列表" onClick={() => { void unarchive(version) }}>{candidateAction?.kind === 'restore' && candidateAction.versionId === version.id ? '恢复中…' : '恢复候选'}</button>}</div>
          </article>
        }) : <div className="review-state-empty"><b>暂无视频候选</b><p>{writeFrozen ? '请先确认分镜，并完成人物或场景资产校验。' : '可点击「新建视频版本」创建候选。'}</p></div>}</div>
      </section>
    </div>
    {wizard && <Dialog title="新建视频版本" onClose={() => setWizard(null)} closeDisabled={generating} wide><div className="generation-mode-tabs"><button disabled={generating} className={wizard === 'reroll' ? 'active' : ''} onClick={() => setWizard('reroll')}>AI 重新生成</button><button disabled={generating} className={wizard === 'rewrite' ? 'active' : ''} onClick={() => setWizard('rewrite')}>给 AI 导演要求</button><button disabled={generating} className={wizard === 'critique' ? 'active' : ''} onClick={() => setWizard('critique')}>按质检问题修复</button></div><div className="review-impact"><b>{wizard === 'reroll' ? '沿用同一连续性合同，由 AI 重新编写 H3 Prompt' : wizard === 'rewrite' ? '你的文字只作为导演要求，最终 H3 Prompt 仍由 AI 完整生成并通过结构化校验' : 'AI 将读取当前采用版或最新成功版的质检问题，重新编写 Physical Performance 与 H3 Prompt'}</b><p>确认后会立即创建任务并可能产生模型费用，当前预计 ￥{shot.est_cost_cny.toFixed(2)}，实际费用以供应商返回为准。旧采用版保留，失败不会覆盖。</p></div>{wizard === 'rewrite' && <><div className="prompt-diff"><div><b>当前 AI 最终 H3 Prompt（只读）</b><pre>{adoptedVersion?.prompt_text || '暂无已采用 Prompt'}</pre></div><div><b>给 AI 的导演要求</b><textarea rows={8} maxLength={2000} disabled={generating} value={prompt} onChange={event => setPrompt(event.target.value)} placeholder="例如：双人接触必须同时入画，侧面中景看清接触点；动作贯穿对白，不要退化成静态口型。" /></div></div><small>{prompt.length}/2000 字符；AI 会结合连续性合同重新生成完整提示词，不会把这里的文字直接提交给 H3。</small></>}{wizard === 'critique' && <div className="review-impact"><p>AI 会把已有视频质检问题作为本次必须改正项；若暂无成功候选，则基于当前连续性合同重新编写。</p></div>}<div className="dialog-actions"><button className="btn ghost" disabled={generating} onClick={() => setWizard(null)}>取消（零任务/零扣费）</button><button className="btn primary" disabled={generating || (wizard === 'rewrite' && !prompt.trim())} onClick={() => { void runGeneration() }}>{generating ? '正在提交…' : videoGenerationConfirmLabel(wizard, shot.est_cost_cny)}</button></div></Dialog>}
    {adopt && <Dialog title={`${adopt.id === shot.adopted_version_id ? '更新倍速定稿' : '采纳'}镜 ${shot.shot_no} 的 v${adopt.version_no}`} onClose={() => setAdopt(null)} closeDisabled={adopting}><div className="review-impact"><p>当前采用：{adoptedVersion ? `v${adoptedVersion.version_no} · ${videoPlaybackRate(adoptedVersion)}×` : '无'}；目标候选：v{adopt.version_no} · {playbackRates[adopt.id] ?? videoPlaybackRate(adopt)}×。</p><p>目标质检分 {adopt.qa?.overall?.toFixed(2) ?? '未评估'}，费用 ￥{adopt.cost_cny.toFixed(2)}。提交会固定镜头、版本和倍速，并写入审计；成片将使用倍速处理后的片段。</p></div><label className="review-field">必填定稿理由<textarea rows={4} disabled={adopting} value={adoptReason} onChange={event => setAdoptReason(event.target.value)} placeholder="说明画面质量、节奏、连续性或成本判断" /></label><div className="dialog-actions"><button className="btn ghost" disabled={adopting} onClick={() => setAdopt(null)}>取消</button><button className="btn primary" disabled={adopting || adoptReason.trim().length < 4} onClick={() => { void doAdopt() }}>{adopting ? '定稿中…' : `确认按 ${playbackRates[adopt.id] ?? videoPlaybackRate(adopt)}× 定稿采纳`}</button></div></Dialog>}
    {deleteTarget && <Dialog title={`永久删除镜 ${shot.shot_no} 的 v${deleteTarget.version_no}？`} onClose={() => setDeleteTarget(null)} closeDisabled={deleting}><div className="review-impact"><b>此操作无法撤销</b><p>将永久移除此候选记录和关联视频入口；当前采用版与其他候选不受影响。</p><p>已记录费用 ￥{deleteTarget.cost_cny.toFixed(2)} 不会退回。若只想暂时从候选列表隐藏，请取消后使用「归档」。</p></div><div className="dialog-actions"><button type="button" className="btn ghost" disabled={deleting} onClick={() => setDeleteTarget(null)}>保留候选</button><button type="button" className="btn danger" disabled={deleting} onClick={() => { void remove(deleteTarget) }}>{deleting ? '删除中…' : '确认永久删除'}</button></div></Dialog>}
    {clearResourcesConfirm && <DecisionDialog title={`清空镜 ${shot.shot_no} 的全部资源？`} summary={`${versions.length} 个视频候选、${shotReferenceImageCount} 张图像和当前采用关系将被删除`} message="本镜已引入或生成的视频、关键帧和参考图都会清空；剧本和分镜文本保留。" details={['此操作不可撤销，已发生费用不会退回', '后续需重新准备图像并生成视频']} confirmLabel="确认清空本镜资源" cancelLabel="保留资源" danger onClose={() => setClearResourcesConfirm(false)} onConfirm={() => { setClearResourcesConfirm(false); void clearResources() }} />}
    {confirmProviderResubmission && <DecisionDialog title={`确认重新提交镜 ${shot.shot_no} 的供应商 create？`} summary="旧请求可能已被供应商接收，重新提交仍可能产生第二笔费用" message={PROVIDER_RESUBMISSION_WARNING} details={['此动作不是恢复原任务', '新提交使用独立 operation ID 与预算 claim', '原请求费用状态仍未知', '普通新建视频版本保持禁用']} confirmLabel={providerRecoveryBusy ? '正在提交…' : '已核对，确认重新提交'} cancelLabel="暂不重新提交" danger onClose={() => setConfirmProviderResubmission(false)} onConfirm={() => { void recoverProviderCreate(true) }} />}
  </div>
}
