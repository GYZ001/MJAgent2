import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import EpisodeCrumb from '../components/EpisodeCrumb'
import { useEpisode, useNav } from '../App'
import {
  api,
  type ReferenceImage,
  type ReviewItemStatus,
  type ReviewSeverity,
  type ReviewShotSummary,
  type ReviewWallContext,
  type Shot,
  type ShotReviewItem,
  type ShotVersion,
} from '../api'
import { ServerTaskTimer, TaskTimer, useTaskTimer } from '../components/TaskTimer'
import { countAdoptedVideos, shotVideoState } from '../shotStatus'
import AsyncButton from '../components/AsyncButton'
import QueryState from '../components/QueryState'
import VideoSupervisorPanel from '../components/VideoSupervisorPanel'

type ReviewTab = 'text' | 'references' | 'videos'
type DetailState =
  | { status: 'idle' }
  | { status: 'loading'; shotId: string }
  | { status: 'ready'; shotId: string; shot: Shot; loadedAt: number }
  | { status: 'error'; shotId: string; message: string; errorId?: string }
type ShotFilter = 'problem' | 'unproduced' | 'generating' | 'pending_adoption' | 'adopted' | 'failed' | 'unreviewed' | 'grade_b' | 'continuity'

export const REVIEW_TABS: Array<{ id: ReviewTab; label: string }> = [
  { id: 'text', label: '文字内容' },
  { id: 'references', label: '参考图' },
  { id: 'videos', label: '视频预览' },
]

const reviewDraftKey = (shotId: string) => `manju:review-item-draft:${shotId}`
const generationDraftKey = (shotId: string) => `manju:video-generation-draft:${shotId}`
const EMPTY_SHOTS: Shot[] = []

const EPISODE_STATUS: Record<string, { label: string; next: string }> = {
  planned: { label: '待制作', next: '请先完成剧本和分镜' },
  scripting: { label: '剧本制作中', next: '等待剧本完成后到分镜台确认' },
  scripted: { label: '剧本已完成', next: '请到分镜台制作并确认' },
  confirmed: { label: '分镜已确认', next: '可以开始生成和评审视频' },
  generating: { label: '视频生成中', next: '可继续评审已就绪的镜头' },
  done: { label: '已完成', next: '请完成评审并进入成片台' },
}

const REJECT_REASON: Record<string, { label: string; suggestion: string; risk: 'low' | 'medium' | 'high' }> = {
  missing_quality_score: { label: '缺少质检结果', suggestion: '先重新运行质检', risk: 'high' },
  quality_below_threshold: { label: '画面质量未达标', suggestion: '调整生成词或重新生成', risk: 'medium' },
  quality_issue_blocks_reuse: { label: '硬质量门禁未通过', suggestion: '修复质量问题后重新验证', risk: 'high' },
  consistency_drift: { label: '人物或场景一致性漂移', suggestion: '换用已验证的人物/场景参考', risk: 'high' },
  consistency_drift_unfixable: { label: '一致性严重漂移', suggestion: '重建参考图后再生成', risk: 'high' },
  duplicate_character_suppressed: { label: '画面出现重复角色', suggestion: '减少冲突参考并明确人数', risk: 'medium' },
}

function newId(prefix: string) {
  return `${prefix}:${Date.now()}:${Math.random().toString(36).slice(2, 10)}`
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

function commaList(value?: string[]) {
  return (value ?? []).join('、') || '无'
}

function truncateText(value: string, max = 1000) {
  return value.length > max ? `${value.slice(0, max)}…` : value
}

function safeFilePart(value: string) {
  return value.replace(/[\\/:*?"<>|\s]+/g, '-').replace(/^-+|-+$/g, '') || 'video'
}

function videoVersionStatusLabel(version: ShotVersion, adopted: boolean): string {
  if (adopted) return '已采纳'
  if (version.status === 'succeeded' && version.video_url) return '待采纳'
  if (version.status === 'failed') return '生成失败'
  if (['queued', 'running', 'waiting_provider'].includes(version.status)) return '生成中'
  return '待生成'
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
  return REJECT_REASON[reason] || { label: '未知淘汰原因', suggestion: '查看技术码并联系管理员', risk: 'high' as const }
}

function Dialog({ title, children, onClose, wide = false }: {
  title: string; children: React.ReactNode; onClose: () => void; wide?: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  const returnFocus = useRef(document.activeElement as HTMLElement | null)
  useEffect(() => {
    const root = ref.current
    const focusables = () => Array.from(root?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex="0"]') || [])
    focusables()[0]?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
      if (event.key !== 'Tab') return
      const items = focusables()
      if (!items.length) return
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
    }
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('keydown', onKey); returnFocus.current?.focus() }
  }, [onClose])
  return (
    <div className="review-dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <div ref={ref} className={`review-dialog${wide ? ' wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby="review-dialog-title" onMouseDown={event => event.stopPropagation()}>
        <div className="review-dialog-head"><h3 id="review-dialog-title">{title}</h3><button type="button" aria-label="关闭对话框" onClick={onClose}>×</button></div>
        {children}
      </div>
    </div>
  )
}

function Lightbox({ src, alt, onClose }: { src: string; alt: string; onClose: () => void }) {
  return (
    <Dialog title={alt || '参考图预览'} onClose={onClose} wide>
      <img className="review-lightbox-image" src={src} alt={alt} />
    </Dialog>
  )
}

function stateMeta(status: string) {
  return EPISODE_STATUS[status] || { label: '未知状态', next: '请刷新或查看技术详情' }
}

function reviewSummary(context: ReviewWallContext | null, shotId: string | null): ReviewShotSummary | null {
  return context?.shots.find(item => item.shot_id === shotId) || null
}

function matchesFilter(shot: Shot, summary: ReviewShotSummary | null, filter: ShotFilter) {
  const state = shotVideoState(shot)
  if (filter === 'problem') return Boolean((summary?.open_issue_count || 0) > 0 || state.grade === 'B' || state.continuityDegraded || state.phase === 'generation_failed')
  if (filter === 'unproduced') return state.phase === 'pending_generation'
  if (filter === 'generating') return state.phase === 'generating'
  if (filter === 'pending_adoption') return state.phase === 'pending_adoption'
  if (filter === 'adopted') return state.phase === 'adopted'
  if (filter === 'failed') return state.phase === 'generation_failed'
  if (filter === 'unreviewed') return !summary || summary.review_status !== 'completed'
  if (filter === 'grade_b') return state.grade === 'B'
  return state.continuityDegraded
}

export default function WallPage() {
  const { projectId, episodeId, go } = useNav()
  const { data: ep, refresh, error, loading } = useEpisode(episodeId || '', 'wall')
  // Keep the pre-detail dependency stable. A fresh [] on every render makes
  // the detail effect re-enter and repeatedly write a new idle state.
  const shots = ep?.shots ?? EMPTY_SHOTS
  const [selectedShotId, setSelectedShotId] = useState<string | null>(null)
  const [selectionReady, setSelectionReady] = useState(false)
  const [tombstoneShotId, setTombstoneShotId] = useState<string | null>(null)
  const [reviewTab, setReviewTab] = useState<ReviewTab>('text')
  const [detail, setDetail] = useState<DetailState>({ status: 'idle' })
  const [context, setContext] = useState<ReviewWallContext | null>(null)
  const [contextError, setContextError] = useState<string | null>(null)
  const [filters, setFilters] = useState<Set<ShotFilter>>(new Set())
  const [filterSource, setFilterSource] = useState('未筛选')
  const [contentUpdate, setContentUpdate] = useState<string | null>(null)
  const [toast, setToast] = useState<{ message: string; action?: { label: string; run: () => void } } | null>(null)
  const [lightbox, setLightbox] = useState<{ src: string; label: string } | null>(null)
  const [clearMenuOpen, setClearMenuOpen] = useState(false)
  const [genMenuOpen, setGenMenuOpen] = useState(false)
  const [supervisorKickoff, setSupervisorKickoff] = useState(false)
  const [supervisorPanelDismissed, setSupervisorPanelDismissed] = useState(false)
  const [authOpen, setAuthOpen] = useState(false)
  const [authBudget, setAuthBudget] = useState('150')
  const [authHours, setAuthHours] = useState('4')
  const [authEdit, setAuthEdit] = useState(false)
  const [stalePreview, setStalePreview] = useState<Awaited<ReturnType<typeof api.staleAssetsPreview>> | null>(null)
  const [staleSelection, setStaleSelection] = useState<Set<string>>(new Set())
  const [staleBusy, setStaleBusy] = useState(false)
  const [genMask, setGenMask] = useState<Set<string>>(new Set())
  const [draftDirty, setDraftDirty] = useState(false)
  const [pendingShotId, setPendingShotId] = useState<string | null>(null)
  const [pendingEpisodeId, setPendingEpisodeId] = useState<string | null>(null)
  const toastTimer = useRef<number>()
  const detailRequest = useRef(0)
  const lastReadyDetail = useRef<Shot | null>(null)
  const contextRequest = useRef(0)
  const positionKey = `manju:review-wall:${projectId || 'project'}:${episodeId || 'episode'}`

  const showToast = useCallback((message: string, action?: { label: string; run: () => void }, persistent = false) => {
    setToast({ message, action })
    if (toastTimer.current) window.clearTimeout(toastTimer.current)
    if (!persistent) toastTimer.current = window.setTimeout(() => setToast(null), action ? 8000 : 3600)
  }, [])

  const loadContext = useCallback(async () => {
    if (!episodeId) return null
    const request = ++contextRequest.current
    try {
      const next = await api.getReviewContext(episodeId)
      if (request !== contextRequest.current) return null
      setContext(next)
      setContextError(null)
      return next
    } catch (reason) {
      if (request === contextRequest.current) setContextError(reason instanceof Error ? reason.message : String(reason))
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

  useEffect(() => { void loadContext() }, [loadContext])

  useEffect(() => {
    if (!episodeId || selectionReady || loading || !ep) return
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
    setSelectionReady(true)
  }, [ep, episodeId, loading, positionKey, selectionReady, shots])

  const selectedSummary = shots.find(shot => shot.id === selectedShotId) || null
  const selectedDetailRefreshKey = shotDetailRefreshKey(selectedSummary)

  useEffect(() => {
    if (!selectionReady || !selectedShotId) {
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
  }, [loadDetail, selectedDetailRefreshKey, selectedShotId, selectionReady])

  useEffect(() => {
    if (!selectionReady || !selectedShotId) return
    localStorage.setItem(positionKey, JSON.stringify({ shotId: selectedShotId, tab: reviewTab }))
  }, [positionKey, reviewTab, selectedShotId, selectionReady])

  const selectShot = useCallback((shotId: string) => {
    if (draftDirty) { setPendingShotId(shotId); return }
    setDraftDirty(false)
    setContentUpdate(null)
    setTombstoneShotId(null)
    setSelectedShotId(shotId)
  }, [draftDirty])

  const filteredShots = useMemo(() => {
    if (!filters.size) return shots
    return shots.filter(shot => [...filters].every(filter => matchesFilter(shot, reviewSummary(context, shot.id), filter)))
  }, [context, filters, shots])

  const selectedReview = reviewSummary(context, selectedShotId)
  const readyShot = detail.status === 'ready' && detail.shotId === selectedShotId ? detail.shot : null
  const writeFrozen = Boolean(tombstoneShotId || detail.status !== 'ready' || !context?.upstream.eligible_for_production)
  const videoReady = countAdoptedVideos(shots)
  const supervisor = ep?.video_supervisor as import('../components/VideoSupervisorPanel').VideoSupervisorSnapshot | null | undefined
  const supervisorPhase = typeof supervisor?.phase === 'string' ? supervisor.phase : ''
  const supervisorTerminal = ['SUCCEEDED_COVERED', 'COMPLETED_DEADLINE_FALLBACK', 'PARTIAL_NO_USABLE_CANDIDATE', 'FAILED_CLOSED', 'CANCELLED'].includes(supervisorPhase)
  const supervisorTaskRunning = supervisor?.task_running === true
  const supervisorLive = supervisorKickoff || (!supervisorTerminal && supervisorTaskRunning)
  const videoActive = supervisorLive || shots.some(shot => shot.versions.some(version => ['queued', 'running', 'waiting_provider'].includes(version.status)))
  const videoTimer = useTaskTimer(`episode.${episodeId}.videos`, videoActive)

  useEffect(() => { if (supervisorTerminal || supervisorTaskRunning) setSupervisorKickoff(false) }, [supervisorTaskRunning, supervisorTerminal])
  useEffect(() => { setSupervisorPanelDismissed(false) }, [episodeId])

  const refreshAll = useCallback(async () => {
    const preservedShotId = selectedShotId
    const next = await refresh()
    await loadContext()
    if (!preservedShotId) return
    if (!next?.shots?.some(shot => shot.id === preservedShotId)) {
      detailRequest.current += 1
      setTombstoneShotId(preservedShotId)
      setDetail({ status: 'idle' })
      return
    }
    await loadDetail(preservedShotId)
  }, [loadContext, loadDetail, refresh, selectedShotId])

  const mutate = useCallback(async (operation: () => Promise<unknown>, success: string) => {
    try {
      await operation()
      showToast(success)
      await refreshAll()
      return true
    } catch (reason) {
      showToast(reason instanceof Error ? reason.message : String(reason), undefined, true)
      return false
    }
  }, [refreshAll, showToast])

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
    setGenMenuOpen(false)
    if (!context?.upstream.eligible_for_production) { showToast(context?.upstream.blockers.join('；') || '分镜尚未确认', undefined, true); return }
    videoTimer.start()
    const ok = await mutate(() => api.episodeGenerate(ep!.id), '全片生成请求已接受，可在任务面板追踪')
    if (!ok) videoTimer.clear()
  }

  const startCompletion = async () => {
    if (!ep || !context) return
    const budget = Number(authBudget)
    const wall = Number(authHours) * 3600
    const budgetRule = context.authorization_constraints.budget_cap_cny
    const wallRule = context.authorization_constraints.wall_clock_cap_s
    if (!Number.isFinite(budget) || budget < budgetRule.min || budget > budgetRule.max) { showToast(`预算必须在 ${budgetRule.min}–${budgetRule.max} 元之间`); return }
    if (!Number.isFinite(wall) || wall < wallRule.min || wall > wallRule.max) { showToast(`时长必须在 ${wallRule.min / 3600}–${wallRule.max / 3600} 小时之间`); return }
    setSupervisorKickoff(true); videoTimer.start()
    try {
      await api.episodeVideoCompletion(ep.id, {
        mode: 'fresh', budget_cap_cny: budget, wall_clock_cap_s: wall,
        allow_fallback_adopt: true, allow_storyboard_edit: authEdit,
        qualification_version: context.upstream.qualification_version,
        idempotency_key: newId('completion'),
      })
      setAuthOpen(false)
      showToast('补齐请求已接受；「已接受」不等于已完成，请关注 Supervisor 终态')
      await refreshAll()
    } catch (reason) {
      setSupervisorKickoff(false); videoTimer.clear()
      showToast(reason instanceof Error ? reason.message : String(reason), undefined, true)
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
    setStaleBusy(true); videoTimer.start()
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
    } catch (reason) { videoTimer.clear(); showToast(reason instanceof Error ? reason.message : String(reason), undefined, true) }
    finally { setStaleBusy(false) }
  }

  if (error && !ep) return <QueryState loading={false} error={error} hasData={false}>{null}</QueryState>
  if (!ep) return <QueryState loading={loading !== false} error={null} hasData={false}>{null}</QueryState>
  const episodeState = stateMeta(ep.status)
  const staleCount = stalePreview?.stale_count ?? shots.filter(shot => shot.video_stale).length

  return (
    <div className="wall-page">
      <header className="wall-topbar">
        <div className="wall-topbar-left">
          <EpisodeCrumb
            label="评审墙"
            view="wall"
            episodeNo={ep.episode_no}
            showReviewFilters
            onBeforeEpisodeChange={target => {
              if (!draftDirty) return true
              setPendingEpisodeId(target)
              return false
            }}
          />
          <PipelineFilters shots={shots} active={filters} onSelect={(filter, label) => { setFilters(new Set([filter])); setFilterSource(`顶部五态 · ${label}`) }} />
          <details className="status-detail"><summary><span className={`stamp ${context?.upstream.eligible_for_production ? 'green' : 'gold'}`}>{episodeState.label}</span></summary><div>{episodeState.next}<br /><code>{ep.status}</code></div></details>
        </div>
        <div className="wall-topbar-right">
          {typeof supervisor?.started_at === 'number'
            ? <ServerTaskTimer label="视频" startedAt={supervisor.started_at} finishedAt={typeof supervisor.finished_at === 'number' ? supervisor.finished_at : null} running={supervisorTaskRunning} />
            : <TaskTimer label="视频" timer={videoTimer} />}
          {shots.length > 0 && (
            <div className="clear-menu-wrap">
              <button className="btn primary small" disabled={!context?.upstream.eligible_for_production} title={context?.upstream.blockers.join('；')} onClick={() => setGenMenuOpen(open => !open)}>生成视频 ▾</button>
              {genMenuOpen && <><button type="button" className="clear-menu-backdrop" aria-label="关闭生成菜单" onClick={() => setGenMenuOpen(false)} /><div className="clear-menu"><button className="clear-menu-item" onClick={() => { void startEpisodeGeneration() }}>快速生成全部</button><button className="clear-menu-item" onClick={() => { setGenMenuOpen(false); setAuthOpen(true) }}>补齐到全片可用</button></div></>}
            </div>
          )}
          {shots.length > 0 && (
            <div className="clear-menu-wrap">
              <button className="btn ghost small danger" onClick={() => setClearMenuOpen(open => !open)}>清空 ▾</button>
              {clearMenuOpen && <><button type="button" className="clear-menu-backdrop" aria-label="关闭清空菜单" onClick={() => setClearMenuOpen(false)} /><div className="clear-menu"><div className="clear-menu-hint">将先显示权威影响预演</div><button className="clear-menu-item" disabled={!selectedSummary || detail.status !== 'ready' || Boolean(context?.upstream.active_upstream_runs.length)} title={context?.upstream.active_upstream_runs.length ? '上游任务运行中' : ''} onClick={() => { setClearMenuOpen(false); if (selectedSummary) void mutate(() => api.clearShotArtifacts(selectedSummary.id), `镜 ${selectedSummary.shot_no} 已清空`) }}>清空本镜{selectedSummary ? `（镜 ${selectedSummary.shot_no}）` : ''}</button><button className="clear-menu-item" disabled={Boolean(context?.upstream.active_upstream_runs.length)} title={context?.upstream.active_upstream_runs.length ? '上游任务运行中' : ''} onClick={() => { setClearMenuOpen(false); void mutate(() => api.clearEpisodeArtifacts(ep.id), '本集已清空') }}>清空本集（{shots.length} 镜）</button></div></>}
            </div>
          )}
        </div>
      </header>

      {contextError && <section className="review-persistent-error" role="alert"><b>评审契约加载失败</b><span>{contextError}</span><button className="btn small" onClick={() => { void loadContext() }}>重试</button></section>}
      {context && !context.upstream.eligible_for_production && <section className="review-blocked-banner" role="status"><b>当前为只读评审</b><span>{context.upstream.blockers.join('；')}。查看、停止旧任务和废弃/隔离仍可用，生成、恢复、采用和修复已保护。</span><button className="btn small" onClick={() => go(ep.status === 'scripting' || ep.status === 'planned' ? 'script' : 'board', projectId, ep.id)}>去{ep.status === 'scripting' || ep.status === 'planned' ? '剧本台' : '分镜台'}处理</button><details><summary>技术详情</summary><code>{context.upstream.qualification_version}</code></details></section>}

      {staleCount > 0 && <section className="material-fallback-note review-stale-banner" role="status"><span>参考资产已更新：<b>{staleCount}</b> 镜采用版可能使用旧证据。</span><button className="btn small" disabled={staleBusy} onClick={() => { void loadStale() }}>{staleBusy ? '预演中…' : '查看影响与选择修复'}</button></section>}

      {!supervisorPanelDismissed && (supervisorLive || supervisor) && <VideoSupervisorPanel api={api} episodeId={ep.id} runId={ep.active_video_run_id} supervisor={supervisor} running={supervisorTaskRunning || supervisorKickoff} onChanged={refreshAll} onToast={showToast} onDismiss={() => setSupervisorPanelDismissed(true)} />}

      {shots.length === 0 ? (
        <section className="review-business-empty"><div className="big">镜</div><h2>本集尚无可评审镜头</h2><p>当前状态：{episodeState.label}。{episodeState.next}。</p><div><button className="btn" onClick={() => go('script', projectId, ep.id)}>去剧本台</button><button className="btn primary" onClick={() => go('board', projectId, ep.id)}>去分镜台</button><button className="btn ghost" onClick={() => { void refreshAll() }}>刷新</button></div></section>
      ) : (
        <>
          <ShotFilters shots={shots} context={context} filters={filters} source={filterSource} onChange={(next, source) => { setFilters(next); setFilterSource(source) }} />
          <nav className="wall-shot-rail" aria-label="镜头状态导航">
            {filteredShots.map(item => {
              const state = shotVideoState(item)
              const itemReview = reviewSummary(context, item.id)
              return <button key={item.id} type="button" data-grade={state.grade || undefined} className={`${item.id === selectedShotId ? 'active ' : ''}${state.railClass}`} onClick={() => selectShot(item.id)} aria-current={item.id === selectedShotId ? 'true' : undefined} aria-label={`镜 ${item.shot_no}，${state.label}，${itemReview?.open_issue_count || 0} 个未解决问题`}><b>{String(item.shot_no).padStart(2, '0')}</b><span>{state.label}</span>{itemReview?.open_issue_count ? <i className="rail-issue-badge">问题 {itemReview.open_issue_count}</i> : null}{itemReview?.review_status === 'completed' ? <i className="rail-reviewed-badge">已评完</i> : null}{state.continuityDegraded ? <i className="continuity-degraded-badge">衔接降级</i> : null}</button>
            })}
            {!filteredShots.length && <div className="rail-empty">当前筛选无命中镜头。<button onClick={() => { setFilters(new Set()); setFilterSource('未筛选') }}>清除筛选</button></div>}
          </nav>
          {selectedSummary && filteredShots.length > 0 && !filteredShots.some(shot => shot.id === selectedSummary.id) && <div className="filter-selection-note">当前镜头不再命中筛选，已保留当前对象；只有你主动切换时才会离开。</div>}
          {contentUpdate && <div className="filter-selection-note" role="status"><b>当前镜头内容已更新</b><span>{contentUpdate}。{draftDirty ? '本地评审草稿未被覆盖，可比较后提交。' : '当前 shotId 与标签保持不变。'}</span><button onClick={() => setContentUpdate(null)}>知道了</button></div>}

          {tombstoneShotId ? (
            <section className="review-tombstone" role="alert"><h2>原镜头已删除或无权访问</h2><p>已保存的 shotId <code>{tombstoneShotId}</code> 已不在当前列表。所有写操作已冻结，不会自动改作相邻镜头。</p><div>{shots.map(shot => <button className="btn small" key={shot.id} onClick={() => selectShot(shot.id)}>选择镜 {shot.shot_no}</button>)}</div></section>
          ) : detail.status === 'loading' ? (
            <section className="review-detail-loading" aria-busy="true"><b>正在加载镜 {selectedSummary?.shot_no} 的完整评审详情…</b><div className="review-skeleton" /><div className="review-skeleton short" /></section>
          ) : detail.status === 'error' ? (
            <section className="review-persistent-error detail" role="alert"><b>镜 {selectedSummary?.shot_no} 详情加载失败</b><span>{detail.message}</span>{detail.errorId && <code>关联 ID：{detail.errorId}</code>}<p>上方状态轨仅是摘要，不代表“无参考图/无视频”。依赖详情的操作已冻结。</p><button className="btn primary" onClick={() => { if (selectedShotId) void loadDetail(selectedShotId) }}>重试</button></section>
          ) : readyShot ? (
            <ShotWorkbench key={readyShot.id} shot={readyShot} episodeNo={ep.episode_no} episodeStatus={ep.status} tab={reviewTab} onTab={setReviewTab} review={selectedReview} context={context} writeFrozen={writeFrozen} generating={genMask.has(readyShot.id) || readyShot.versions.some(version => ['queued', 'running', 'waiting_provider'].includes(version.status))} setGenerating={busy => setGenMask(mask => { const next = new Set(mask); busy ? next.add(readyShot.id) : next.delete(readyShot.id); return next })} onOpen={(src, label) => setLightbox({ src, label })} onRefresh={refreshAll} onContext={loadContext} onToast={showToast} onDraftDirty={setDraftDirty} />
          ) : null}

          <div className="shot-pager"><button className="btn ghost small" disabled={!filteredShots.some(shot => shot.shot_no < (selectedSummary?.shot_no ?? -Infinity))} title="Alt + ←" onClick={() => navigateIn(-1)}>← 上一镜</button><span className="pg-no">当前镜 {selectedSummary?.shot_no || '—'} · 筛选 {filteredShots.length}/{shots.length}</span><button className="btn ghost small" disabled={!filteredShots.some(shot => shot.shot_no > (selectedSummary?.shot_no ?? Infinity))} title="Alt + →" onClick={() => navigateIn(1)}>下一镜 →</button></div>
        </>
      )}

      {toast && <div className="toast review-toast" role="status"><span>{toast.message}</span>{toast.action && <button onClick={() => { toast.action?.run(); setToast(null) }}>{toast.action.label}</button>}<button aria-label="关闭消息" onClick={() => setToast(null)}>×</button></div>}
      {lightbox && <Lightbox src={lightbox.src} alt={lightbox.label} onClose={() => setLightbox(null)} />}
      {pendingShotId && <Dialog title="当前评审草稿尚未提交" onClose={() => setPendingShotId(null)}><p>草稿已自动保存在本机。你可以继续编辑，或明确放弃后切换镜头。</p><div className="dialog-actions"><button className="btn ghost" onClick={() => setPendingShotId(null)}>返回继续编辑</button><button className="btn danger" onClick={() => { const target = pendingShotId; if (selectedShotId) localStorage.removeItem(reviewDraftKey(selectedShotId)); setPendingShotId(null); setDraftDirty(false); setTombstoneShotId(null); setSelectedShotId(target) }}>放弃草稿并切换</button></div></Dialog>}
      {pendingEpisodeId && <Dialog title="切换分集前处理评审草稿" onClose={() => setPendingEpisodeId(null)}><p>当前镜头有未提交批注。草稿已自动保存在本机，切回本镜可继续编辑；也可以明确放弃。</p><div className="dialog-actions"><button className="btn ghost" onClick={() => setPendingEpisodeId(null)}>返回继续编辑</button><button className="btn" onClick={() => { const target = pendingEpisodeId; setPendingEpisodeId(null); go('wall', projectId, target) }}>保存草稿并切集</button><button className="btn danger" onClick={() => { const target = pendingEpisodeId; if (selectedShotId) localStorage.removeItem(reviewDraftKey(selectedShotId)); setDraftDirty(false); setPendingEpisodeId(null); go('wall', projectId, target) }}>放弃草稿并切集</button></div></Dialog>}
      {authOpen && context && <Dialog title="补齐到全片可用·授权" onClose={() => setAuthOpen(false)}><div className="review-form-grid"><label>预算上限（元）<input type="number" value={authBudget} min={context.authorization_constraints.budget_cap_cny.min} max={context.authorization_constraints.budget_cap_cny.max} step={context.authorization_constraints.budget_cap_cny.step} onChange={event => setAuthBudget(event.target.value)} /><small>范围 {context.authorization_constraints.budget_cap_cny.min}–{context.authorization_constraints.budget_cap_cny.max} 元</small></label><label>时长墙（小时）<input type="number" value={authHours} min={context.authorization_constraints.wall_clock_cap_s.min / 3600} max={context.authorization_constraints.wall_clock_cap_s.max / 3600} step="0.5" onChange={event => setAuthHours(event.target.value)} /></label><label className="full review-check"><input type="checkbox" checked={authEdit} onChange={event => setAuthEdit(event.target.checked)} />允许 Supervisor 创建分镜修改草稿（不会绕过重新确认；一旦修改，媒体流水线将暂停）</label><div className="review-impact full"><b>影响预览</b><p>只处理尚未采用的 {Math.max(0, shots.length - videoReady)} 镜；已采用 {videoReady} 镜保留。不自动拼接成片或建交付包。提交后还会经过一次权威影响确认。</p><code>资格版本 {context.upstream.qualification_version}</code></div></div><div className="dialog-actions"><button className="btn ghost" onClick={() => setAuthOpen(false)}>取消（零写入）</button><button className="btn primary" onClick={() => { void startCompletion() }}>继续到权威确认</button></div></Dialog>}
      {stalePreview && <Dialog title="陈旧资产影响预演" onClose={() => setStalePreview(null)} wide><div className="stale-preview-summary">共 {stalePreview.stale_count} 镜，当前选择 {staleSelection.size} 镜，选中估算 ¥{stalePreview.shots.filter(shot => staleSelection.has(shot.shot_id)).reduce((sum, shot) => sum + shot.estimated_cost_cny, 0).toFixed(2)}。旧采用版保留到新版成功。</div><div className="stale-shot-list">{stalePreview.shots.map(shot => <label key={shot.shot_id}><input type="checkbox" checked={staleSelection.has(shot.shot_id)} onChange={event => setStaleSelection(selected => { const next = new Set(selected); event.target.checked ? next.add(shot.shot_id) : next.delete(shot.shot_id); return next })} /><span><b>镜 {shot.shot_no}</b>{shot.reason_labels.join('；')} · 估算 ¥{shot.estimated_cost_cny.toFixed(2)}<small>当前分镜 {shot.current_storyboard_artifact_id || '无'} · 旧版 {shot.storyboard_artifact_id || '无'}</small><small>资产资格：{shot.asset_qualification?.length ? shot.asset_qualification.map(asset => `${asset.entity_type || '资产'} ${asset.entity_name || asset.ref_id || ''} · ${asset.gate_status || '未验证'} · 版本 ${asset.asset_version || '未知'}`).join('；') : '未关联可验证人物/场景输入'}；规则版本 {shot.rule_versions?.join('、') || '未知'}</small>{shot.asset_soft_warnings?.length ? <small>软警告：{shot.asset_soft_warnings.map(item => item.warning).filter(Boolean).join('；')}</small> : null}</span></label>)}</div><div className="dialog-actions"><button className="btn ghost" onClick={() => setStalePreview(null)}>取消（零任务/零扣费）</button><button className="btn primary" disabled={!staleSelection.size || staleBusy || !stalePreview.qualification.eligible_for_production} onClick={() => { void repairStale() }}>{staleBusy ? '提交中…' : '确认选中范围并修复'}</button></div></Dialog>}
    </div>
  )
}

function PipelineFilters({ shots, active, onSelect }: { shots: Shot[]; active: Set<ShotFilter>; onSelect: (filter: ShotFilter, label: string) => void }) {
  const items: Array<{ filter: ShotFilter; label: string }> = [
    { filter: 'unproduced', label: '待生成' }, { filter: 'generating', label: '生成中' },
    { filter: 'pending_adoption', label: '待采纳' }, { filter: 'adopted', label: '已采纳' },
    { filter: 'failed', label: '生成失败' },
  ]
  return <div className="wall-stats pipeline-filter-stats" aria-label="五态镜头筛选">{items.map(item => <button key={item.filter} type="button" aria-pressed={active.size === 1 && active.has(item.filter)} onClick={() => onSelect(item.filter, item.label)}>{item.label} {shots.filter(shot => matchesFilter(shot, null, item.filter)).length}</button>)}</div>
}

function ShotFilters({ shots, context, filters, source, onChange }: { shots: Shot[]; context: ReviewWallContext | null; filters: Set<ShotFilter>; source: string; onChange: (next: Set<ShotFilter>, source: string) => void }) {
  const options: Array<{ id: ShotFilter; label: string }> = [
    { id: 'problem', label: '只看问题' }, { id: 'pending_adoption', label: '待采纳' }, { id: 'failed', label: '失败' },
    { id: 'unreviewed', label: '未评审' }, { id: 'grade_b', label: 'B 级' }, { id: 'continuity', label: '衔接降级' },
  ]
  const hitCount = filters.size ? shots.filter(shot => [...filters].every(filter => matchesFilter(shot, reviewSummary(context, shot.id), filter))).length : shots.length
  return <section className="shot-filter-bar" aria-label="镜头筛选"><b>镜头队列</b>{options.map(option => { const count = shots.filter(shot => matchesFilter(shot, reviewSummary(context, shot.id), option.id)).length; return <button type="button" key={option.id} aria-pressed={filters.has(option.id)} className={filters.has(option.id) ? 'active' : ''} onClick={() => { const next = new Set(filters); next.has(option.id) ? next.delete(option.id) : next.add(option.id); onChange(next, `镜头队列 · ${option.label}`) }}>{option.label} <span>{count}</span></button> })}{filters.size > 0 && <button className="clear" onClick={() => onChange(new Set(), '未筛选')}>清除 {filters.size} 个筛选</button>}<small className="filter-source">来源：{source} · 命中 {hitCount}/{shots.length}</small></section>
}

function ShotWorkbench({ shot, episodeNo, episodeStatus, tab, onTab, review, context, writeFrozen, generating, setGenerating, onOpen, onRefresh, onContext, onToast, onDraftDirty }: {
  shot: Shot; episodeNo: number; episodeStatus: string; tab: ReviewTab; onTab: (tab: ReviewTab) => void
  review: ReviewShotSummary | null; context: ReviewWallContext | null; writeFrozen: boolean; generating: boolean
  setGenerating: (busy: boolean) => void; onOpen: (src: string, label: string) => void
  onRefresh: () => Promise<void>; onContext: () => Promise<ReviewWallContext | null>
  onToast: (message: string, action?: { label: string; run: () => void }, persistent?: boolean) => void
  onDraftDirty: (dirty: boolean) => void
}) {
  const state = shotVideoState(shot)
  const current = state.adopted || state.latest
  return <article className="slide-card"><header className="slide-head"><span className="sn">镜 {shot.shot_no}</span><span className="meta">{shot.shot_size} · {shot.camera_move} · {shot.duration_s}s · {shot.transition}</span><span className="meta">{shot.scene_setting}</span><span className={`stamp ${state.phase === 'adopted' ? 'green' : state.phase === 'generation_failed' ? 'red' : state.phase === 'generating' ? 'gold' : 'grey'}`}>{state.label}</span>{review?.review_status === 'completed' && <span className="stamp green">本镜已评完</span>}{state.continuityDegraded && <span className="continuity-degraded-badge">衔接已降级</span>}</header><nav className="review-tabs" role="tablist" aria-label={`镜 ${shot.shot_no} 评审内容`}>{REVIEW_TABS.map(item => <button key={item.id} type="button" role="tab" aria-selected={tab === item.id} className={tab === item.id ? 'active' : ''} onClick={() => onTab(item.id)}>{item.label}</button>)}</nav><div className="review-workbench-panel" role="tabpanel">
    {tab === 'text' && <InfoSection shot={shot} current={current} />}
    {tab === 'references' && <MaterialGallery shot={shot} productionEligible={!writeFrozen} onOpen={onOpen} onRefresh={onRefresh} onToast={onToast} />}
    {tab === 'videos' && <VideoPreviewWorkspace shot={shot} episodeNo={episodeNo} episodeStatus={episodeStatus} context={context} generating={generating} setGenerating={setGenerating} writeFrozen={writeFrozen} review={review} onRefresh={onRefresh} onToast={onToast} />}
  </div></article>
}

function InfoSection({ shot, current }: { shot: Shot; current?: ShotVersion }) {
  const dialogue = shot.dialogues.map(line => `${line.speaker}：${line.line}${line.emotion && line.emotion !== '平静' ? `（${line.emotion}）` : ''}`).join('\n')
  const prompt = current?.prompt_text || shot.prompt_preview || ''
  const copy = async (text: string) => { try { await navigator.clipboard.writeText(text) } catch { /* clipboard permission */ } }
  return <div className="info-section"><section className="script-card"><div className="script-card-head">原文摘录 <button className="text-action" onClick={() => { void copy(shot.source_excerpt || '') }}>复制</button></div><div className={`script-source${shot.source_excerpt ? '' : ' empty'}`}>{shot.source_excerpt || '暂无原文摘录'}</div></section><section className="script-card"><div className="script-card-head">镜头信息</div><dl className="script-meta-grid"><Meta label="场景" value={shot.scene_setting} /><Meta label="角色" value={commaList(shot.characters)} /><Meta label="时长" value={`${shot.duration_s}s`} /><Meta label="镜头" value={`${shot.shot_size} / ${shot.camera_move}`} /><Meta label="转场" value={shot.transition} /><Meta label="衔接" value={shot.continuity_mode || (shot.continuity_from_prev ? '接上镜' : '新场景')} /></dl></section><section className="script-card continuity-card"><div className="script-card-head">Seedance 连续性</div><div className="continuity-flow"><div><b>输入状态</b><p>{shot.state_in || shot.first_frame_desc || '未设置'}</p></div><span>→</span><div><b>主要动作</b><p>{shot.primary_action || shot.action_desc || '未设置'}</p></div><span>→</span><div><b>输出状态</b><p>{shot.state_out || shot.last_frame_desc || '未设置'}</p></div></div>{current?.qa?.failure_types?.length ? <div className="continuity-risk" role="status"><b>⚠ 连续性风险</b>{current.qa.failure_types.join('、')}<p>观测输出：{current.qa.observed_state_out || '未返回'}</p></div> : <div className="continuity-ok">✓ 暂无已知高风险差异</div>}<details><summary>技术字段</summary>{prompt && <pre>{truncateText(prompt)}</pre>}</details></section><section className="script-card"><div className="script-card-head">镜头脚本 <button className="text-action" onClick={() => { void copy([shot.action_desc, shot.narration, dialogue].filter(Boolean).join('\n')) }}>复制业务文本</button></div><div className="script-block"><div className="script-paragraph"><span className="script-label">画面</span><p>{shot.action_desc}</p></div>{shot.narration && <div className="script-paragraph"><span className="script-label">旁白</span><p>{shot.narration}</p></div>}{dialogue && <div className="script-paragraph"><span className="script-label">台词</span><pre className="script-dialogues">{dialogue}</pre></div>}</div></section></div>
}

function Meta({ label, value }: { label: string; value: string }) { return <div className="script-meta-item"><dt className="script-meta-label">{label}</dt><dd className="script-meta-value">{value}</dd></div> }

function MaterialGallery({ shot, productionEligible, onOpen, onRefresh, onToast }: { shot: Shot; productionEligible: boolean; onOpen: (src: string, label: string) => void; onRefresh: () => Promise<void>; onToast: (message: string, action?: { label: string; run: () => void }, persistent?: boolean) => void }) {
  const data = currentVersionRefs(shot)
  const buckets = classifyReferenceBuckets(data?.refs || [])
  const [restore, setRestore] = useState<ReferenceImage | null>(null)
  const [reason, setReason] = useState('')
  const act = async (operation: () => Promise<unknown>, success: string) => { try { await operation(); onToast(success); await onRefresh() } catch (error) { onToast(error instanceof Error ? error.message : String(error), undefined, true) } }
  const discard = async (ref: ReferenceImage) => {
    if (!data) return
    await act(() => api.discardReferenceImage(data.versionId, ref.id), `已废弃「${refSourceLabel(ref)}」`)
    onToast(`已废弃「${refSourceLabel(ref)}」`, { label: '撤销', run: () => { void act(() => api.restoreReferenceImage(data.versionId, ref.id, '撤销刚才的人工废弃'), '已撤销废弃') } })
  }
  const render = (title: string, items: ReferenceImage[], discarded = false) => <section className={`material-group${discarded ? ' discarded' : ''}`}><header>{title} · {items.length}</header>{items.length ? <div className="material-strip">{items.map(ref => { const score = refScore(ref); const label = refSourceLabel(ref); const reject = rejectReasonInfo(ref.rejectReason); const hard = ref.qa?.hard_failures || ref.hard_failures || []; const eligible = !hard.length && !['failed', 'unverified', 'unknown', 'ineligible'].includes(String(ref.gate_status || ref.downstream_eligibility || ref.qa?.status || '').toLowerCase()); return <figure key={ref.id} className={`material-card${discarded ? ' material-card-discarded' : ''}`}><button type="button" className="mc-thumb" disabled={!ref.image_url} aria-label={`预览${label}`} onClick={() => ref.image_url && onOpen(ref.image_url, label)}>{ref.image_url ? <img src={ref.image_url} alt={label} loading="lazy" /> : <span className="mc-noimg">无图</span>}{score != null && <span className={`mc-qa-badge${score < 0.8 ? ' bad' : ''}`}>QA {score.toFixed(2)}</span>}{!eligible && <span className="mc-gate-badge">⚠ 不可作新输入</span>}</button><figcaption><b>{label}</b><small>ID {ref.id}</small>{ref.selection_reason && <span>选择：{ref.selection_reason}</span>}{discarded && <span className={`mc-reject risk-${reject.risk}`}>{reject.label}</span>}<span>来源 {ref.entity_name || ref.source} · 资产版本 {ref.library_revision_id || ref.library_view_id || '未关联'}</span><span>引用版本 {ref.referenced_by_version_ids?.join('、') || '未关联'}</span>{ref.soft_warnings?.map(warning => <span className="warn" key={warning}>软警告：{warning}</span>)}<details><summary>技术码/修复建议</summary><code>{ref.rejectReason || '无'}</code><p>{reject.suggestion}</p><p>规则版本 {ref.rule_version || '未知'}</p></details>{discarded ? <button className="mc-action restore" disabled={!productionEligible || !eligible} title={!eligible ? '硬失败或未验证不得恢复为生产输入' : !productionEligible ? '上游资格不满足' : ''} onClick={() => { setRestore(ref); setReason('') }}>恢复使用</button> : <button className="mc-action discard" onClick={() => { void discard(ref) }}>废弃/隔离</button>}</figcaption></figure> })}</div> : <div className="review-state-empty"><b>{title === '视频实际输入' ? '暂无可用视频输入' : '暂无该类参考图'}</b><p>{shot.versions.some(version => ['queued', 'running'].includes(version.status)) ? '参考图可能仍在生成，请稍后刷新。' : productionEligible ? '可通过「新建视频版本」生成或重用经验证的参考图。' : '请先完成上游确认或资产门禁。'}</p><button className="btn small" onClick={() => { void onRefresh() }}>刷新状态</button></div>}</section>
  return <div className="candidate-compare material-review"><header className="candidate-compare-head"><b>本镜参考图</b><span>分组、选择/淘汰原因、资产资格与视频血缘</span></header>{data?.isFallback && <div className="material-fallback-note">当前版本参考图未就绪，暂显示最近有图版本 v{data.versionNo}</div>}{render('视频实际输入', buckets.video)}{render('关键帧生成 / QA 依据', buckets.evidence)}{buckets.discarded.length > 0 && render('废弃候选', buckets.discarded, true)}{restore && data && <Dialog title={`恢复参考图·${refSourceLabel(restore)}`} onClose={() => setRestore(null)}><div className="review-impact"><p>原因：{rejectReasonInfo(restore.rejectReason).label}；QA {refScore(restore)?.toFixed(2) || '未评估'}；风险 {rejectReasonInfo(restore.rejectReason).risk}。</p><p>恢复后只会成为后续新视频的候选输入，不改写已有历史视频。</p></div><label className="review-field">必填理由<textarea value={reason} onChange={event => setReason(event.target.value)} rows={4} /></label><div className="dialog-actions"><button className="btn ghost" onClick={() => setRestore(null)}>取消</button><button className="btn primary" disabled={!reason.trim()} onClick={() => { void act(() => api.restoreReferenceImage(data.versionId, restore.id, reason.trim()), '参考图已恢复'); setRestore(null) }}>确认恢复并记录审计</button></div></Dialog>}</div>
}

function ReviewItems({ shot, summary, onContext, onToast, onDraftDirty }: { shot: Shot; summary: ReviewShotSummary | null; onContext: () => Promise<ReviewWallContext | null>; onToast: (message: string, action?: { label: string; run: () => void }, persistent?: boolean) => void; onDraftDirty: (dirty: boolean) => void }) {
  const stored = useMemo(() => {
    try { return JSON.parse(localStorage.getItem(reviewDraftKey(shot.id)) || '{}') as { type?: string; severity?: ReviewSeverity; comment?: string; assignee?: string } }
    catch { return {} }
  }, [shot.id])
  const [type, setType] = useState(stored.type || 'visual')
  const [severity, setSeverity] = useState<ReviewSeverity>(stored.severity || 'medium')
  const [comment, setComment] = useState(stored.comment || '')
  const [assignee, setAssignee] = useState(stored.assignee || '')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    const dirty = Boolean(comment.trim() || assignee.trim())
    onDraftDirty(dirty)
    if (dirty) localStorage.setItem(reviewDraftKey(shot.id), JSON.stringify({ type, severity, comment, assignee }))
    else localStorage.removeItem(reviewDraftKey(shot.id))
  }, [assignee, comment, onDraftDirty, severity, shot.id, type])
  const submit = async () => { if (!comment.trim() || !summary) return; setBusy(true); try { await api.createReviewItem(shot.id, { issue_type: type, severity, comment: comment.trim(), assignee: assignee.trim(), anchor: { field: type === 'dialogue' ? 'dialogues' : type === 'continuity' ? 'state_chain' : 'action_desc' }, content_version: summary.content_version, idempotency_key: newId(`review-create:${shot.id}`) }); setComment(''); setAssignee(''); localStorage.removeItem(reviewDraftKey(shot.id)); onDraftDirty(false); await onContext(); onToast('评审项已创建') } catch (error) { onToast(error instanceof Error ? error.message : String(error), undefined, true) } finally { setBusy(false) } }
  const update = async (item: ShotReviewItem, status: ReviewItemStatus) => { try { await api.updateReviewItem(item.id, { expected_revision: item.revision, status, idempotency_key: newId(`review-update:${item.id}`) }); await onContext() } catch (error) { onToast(error instanceof Error ? error.message : String(error), undefined, true) } }
  const complete = async () => { if (!summary) return; try { await api.setShotReviewState(shot.id, { review_status: summary.review_status === 'completed' ? 'in_review' : 'completed', expected_revision: summary.review_revision, idempotency_key: newId(`review-state:${shot.id}`) }); await onContext(); onToast(summary.review_status === 'completed' ? '已重新打开本镜评审' : '本镜已标记评审完成') } catch (error) { onToast(error instanceof Error ? error.message : String(error), undefined, true) } }
  return <div className="review-items-panel"><section className="review-item-compose"><h3>新建锚定评审项</h3><small>未提交内容会按 shotId 自动保存在本机，切集后可恢复。</small><div className="review-form-grid"><label>问题类型<select value={type} onChange={event => setType(event.target.value)}><option value="visual">画面</option><option value="dialogue">台词/旁白</option><option value="continuity">连续性</option><option value="reference">参考图</option><option value="video">视频质量</option><option value="other">其他</option></select></label><label>严重度<select value={severity} onChange={event => setSeverity(event.target.value as ReviewSeverity)}><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="blocker">阻断</option></select></label><label>负责人<input value={assignee} onChange={event => setAssignee(event.target.value)} placeholder="姓名或角色" /></label><label className="full">批注<textarea rows={4} value={comment} onChange={event => setComment(event.target.value)} placeholder="说明问题、期望和可验证标准" /></label></div><div className="dialog-actions"><button className="btn primary" disabled={busy || !comment.trim()} onClick={() => { void submit() }}>{busy ? '提交中…' : '创建评审项'}</button></div></section><section className="review-item-list"><header><h3>本镜评审记录</h3><button className={`btn small ${summary?.review_status === 'completed' ? 'ghost' : 'primary'}`} disabled={!summary} onClick={() => { void complete() }}>{summary?.review_status === 'completed' ? '重新打开评审' : '标记本镜评审完成'}</button></header>{summary?.review_items.length ? summary.review_items.map(item => <article key={item.id} className={`review-item severity-${item.severity}`}><div><b>{item.issue_type} · {item.severity}</b><span>{item.status}</span>{item.anchor_stale && <span className="stamp red">锚点已失效，需重新定位</span>}</div><p>{item.comment}</p><small>负责人 {item.assignee || '未指定'} · 版本 {item.revision} · {new Date(item.updated_at * 1000).toLocaleString()}</small><div>{item.status !== 'in_progress' && item.status !== 'resolved' && <button onClick={() => { void update(item, 'in_progress') }}>开始处理</button>}{item.status !== 'resolved' && <button onClick={() => { void update(item, 'resolved') }}>解决</button>}{item.status === 'resolved' && <button onClick={() => { void update(item, 'open') }}>重开</button>}</div></article>) : <div className="review-state-empty"><b>暂无评审项</b><p>可以在左侧创建批注、指定负责人和严重度。</p></div>}</section></div>
}

export function resolvePreviewVersionId(versions: ShotVersion[], currentId: string | null): string | null {
  const playable = versions.filter(version => Boolean(version.video_url))
  if (currentId && playable.some(version => version.id === currentId)) return currentId
  return [...playable].sort((left, right) => right.version_no - left.version_no)[0]?.id || null
}

function VideoPreviewWorkspace({ shot, episodeNo, episodeStatus, context, generating, setGenerating, writeFrozen, review, onRefresh, onToast }: { shot: Shot; episodeNo: number; episodeStatus: string; context: ReviewWallContext | null; generating: boolean; setGenerating: (busy: boolean) => void; writeFrozen: boolean; review: ReviewShotSummary | null; onRefresh: () => Promise<void>; onToast: (message: string, action?: { label: string; run: () => void }, persistent?: boolean) => void }) {
  const [previewId, setPreviewId] = useState<string | null>(() => resolvePreviewVersionId(shot.versions, null))
  const [sort, setSort] = useState<'time' | 'qa' | 'status'>('time')
  const [status, setStatus] = useState('all')
  const [adopt, setAdopt] = useState<ShotVersion | null>(null)
  const [adoptReason, setAdoptReason] = useState('')
  const [wizard, setWizard] = useState<'reroll' | 'rewrite' | 'critique' | null>(null)
  const [prompt, setPrompt] = useState('')
  const [stopOpen, setStopOpen] = useState(false)
  const [mediaError, setMediaError] = useState<string | null>(null)
  const playableKey = shot.versions.map(version => `${version.id}:${Boolean(version.video_url)}`).join('|')

  useEffect(() => {
    setPreviewId(current => resolvePreviewVersionId(shot.versions, current))
  }, [playableKey, shot.versions])
  useEffect(() => { setMediaError(null) }, [previewId])
  useEffect(() => {
    if (wizard !== 'rewrite') return
    const saved = localStorage.getItem(generationDraftKey(shot.id))
    if (saved) setPrompt(saved)
  }, [shot.id, wizard])
  useEffect(() => {
    if (wizard === 'rewrite' && prompt.trim()) localStorage.setItem(generationDraftKey(shot.id), prompt)
  }, [prompt, shot.id, wizard])

  const selected = shot.versions.find(version => version.id === previewId)
  const adoptedVersion = shot.versions.find(version => version.id === shot.adopted_version_id)
  const versions = useMemo(() => shot.versions
    .filter(version => status === 'all' || version.status === status)
    .sort((left, right) => sort === 'qa'
      ? (right.qa?.overall ?? -Infinity) - (left.qa?.overall ?? -Infinity) || right.version_no - left.version_no
      : sort === 'status'
        ? videoVersionStatusLabel(left, left.id === shot.adopted_version_id).localeCompare(videoVersionStatusLabel(right, right.id === shot.adopted_version_id)) || right.version_no - left.version_no
        : right.version_no - left.version_no), [shot.adopted_version_id, shot.versions, sort, status])
  const activeVersions = shot.versions.filter(version => ['queued', 'running', 'waiting_provider'].includes(version.status))

  const runGeneration = async () => {
    if (!wizard || !context) return
    const initial = adoptedVersion?.prompt_text || shot.prompt_preview || ''
    const next = prompt.trim()
    if (wizard === 'rewrite' && (!next || next === initial)) {
      onToast('修改模式需要与原词不同的有效生成词')
      return
    }
    const openItems = review?.review_items.filter(item => ['open', 'in_progress'].includes(item.status)) || []
    const selectedReviewIds = wizard === 'critique'
      ? Array.from(document.querySelectorAll<HTMLInputElement>('.critique-list input[type="checkbox"]')).flatMap((input, index) => input.checked && openItems[index] ? [openItems[index].id] : [])
      : []
    if (wizard === 'critique' && openItems.length && !selectedReviewIds.length) {
      onToast('请至少选择一条要带入的评审项')
      return
    }
    setGenerating(true)
    try {
      const result = await api.shotGenerate(shot.id, wizard === 'rewrite' ? next : undefined, wizard !== 'rewrite', wizard === 'critique' ? 'selected_review_items' : undefined, selectedReviewIds, context.upstream.qualification_version, newId(`generate:${shot.id}`)) as { reused?: boolean; version_id?: string; job_id?: string; status?: string }
      if (wizard === 'rewrite') localStorage.removeItem(generationDraftKey(shot.id))
      onToast(result.reused ? '输入未变化，已复用旧版；原词新候选会强制新建版本' : `请求已接受${result.job_id ? `，任务 ${result.job_id}` : ''}；正在排队/生成，尚未完成`)
      setWizard(null)
      await onRefresh()
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), undefined, true)
    } finally {
      setGenerating(false)
    }
  }

  const doAdopt = async () => {
    if (!adopt || !context) return
    try {
      await api.adoptVersion(shot.id, adopt.id, adoptReason.trim(), context.upstream.qualification_version, newId(`adopt:${adopt.id}`))
      setAdopt(null)
      setPreviewId(adopt.id)
      onToast(`已采用 v${adopt.version_no}，理由已写入审计`)
      await onRefresh()
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), undefined, true)
    }
  }

  const archive = async (version: ShotVersion) => {
    try {
      await api.archiveVersion(version.id, '候选版本整理')
      await onRefresh()
      onToast(`v${version.version_no} 已归档`)
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), undefined, true)
    }
  }
  const remove = async (version: ShotVersion) => {
    try {
      await api.deleteVersion(version.id)
      await onRefresh()
      onToast(`v${version.version_no} 已删除`)
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), undefined, true)
    }
  }
  const openAdopt = (version: ShotVersion) => {
    setPreviewId(version.id)
    setAdopt(version)
    setAdoptReason('')
  }

  return <div className="video-preview-workspace">
    <section className="video-toolbar">
      <div><button className="btn primary small" disabled={writeFrozen || generating || !['confirmed', 'generating', 'done'].includes(episodeStatus)} onClick={() => { setWizard('reroll'); setPrompt(shot.prompt_preview || '') }}>新建视频版本</button>{activeVersions.length > 0 && <button className="btn ghost small danger" onClick={() => setStopOpen(true)}>停止任务</button>}</div>
      <span>单次估算 ￥{shot.est_cost_cny.toFixed(2)} · 候选 {shot.versions.length} 个 · 当前评审问题 {review?.open_issue_count || 0}</span>
    </section>
    <div className="video-preview-layout">
      <section className="video-preview-player" aria-label="单视频预览">
        <header><div><span>当前预览</span><b>{selected ? `镜 ${shot.shot_no} · v${selected.version_no}` : `镜 ${shot.shot_no}`}</b></div>{selected && <span className={`stamp ${selected.status === 'failed' ? 'red' : selected.status === 'succeeded' ? 'green' : 'gold'}`}>{videoVersionStatusLabel(selected, selected.id === shot.adopted_version_id)}</span>}</header>
        {selected?.video_url ? <video key={selected.id} src={selected.video_url} controls preload="metadata" onLoadedData={() => setMediaError(null)} onError={() => setMediaError(`无法加载 v${selected.version_no} 的媒体，请检查访问权限或稍后重试`)} /> : <div className="video-preview-empty"><b>暂无可预览视频</b><span>请从右侧选择已生成的候选；排队中或失败版本仍会保留在列表中。</span></div>}
        {(mediaError || selected?.error) && <div className="review-persistent-error compact" role="alert">{mediaError || selected?.error}</div>}
        {selected && <div className="video-preview-summary"><span>QA <b>{selected.qa?.overall?.toFixed(2) ?? '未评估'}</b></span><span>费用 <b>￥{selected.cost_cny.toFixed(2)}</b></span><span>耗时 <b>{selected.latency_s.toFixed(1)}s</b></span></div>}
        {selected?.qa?.issues?.length ? <p className="video-preview-issues">{selected.qa.issues.join('；')}</p> : null}
        <div className="video-preview-actions">{selected?.video_url && <a className="btn ghost small" href={selected.video_url} download={`ep-${episodeNo}-shot-${shot.shot_no}-v${selected.version_no}-${selected.id === shot.adopted_version_id ? 'adopted' : 'candidate'}.mp4`}>导出当前视频</a>}{selected?.video_url && selected.id !== shot.adopted_version_id && !context?.archived_versions[selected.id] && <button className="btn primary small" disabled={writeFrozen} onClick={() => openAdopt(selected)}>采纳当前候选</button>}{selected?.id === shot.adopted_version_id && <span className="stamp green">当前采用版本</span>}</div>
      </section>
      <section className="video-candidate-list" aria-label="视频候选列表">
        <header><div><b>候选列表</b><span>{versions.length}/{shot.versions.length}</span></div><div><select aria-label="版本状态筛选" value={status} onChange={event => setStatus(event.target.value)}><option value="all">全部状态</option><option value="succeeded">已成功</option><option value="failed">失败</option><option value="running">运行中</option><option value="queued">排队中</option></select><select aria-label="版本排序" value={sort} onChange={event => setSort(event.target.value as typeof sort)}><option value="time">按版本</option><option value="qa">按 QA</option><option value="status">按状态</option></select></div></header>
        <div className="video-candidate-scroll">{versions.length ? versions.map(version => {
          const adopted = version.id === shot.adopted_version_id
          const archived = Boolean(context?.archived_versions[version.id])
          const selectedCandidate = version.id === previewId
          return <article className={`video-candidate-card${selectedCandidate ? ' selected' : ''}${adopted ? ' adopted' : ''}${archived ? ' archived' : ''}`} key={version.id}>
            <button type="button" className="video-candidate-select" disabled={!version.video_url} aria-pressed={selectedCandidate} onClick={() => setPreviewId(version.id)}>
              <span className="video-candidate-title"><b>v{version.version_no}</b><span className={`stamp ${version.status === 'failed' ? 'red' : version.status === 'succeeded' ? 'green' : 'gold'}`}>{videoVersionStatusLabel(version, adopted)}</span>{archived && <span className="stamp grey">已归档</span>}</span>
              <span className="video-candidate-metrics"><span>QA {version.qa?.overall?.toFixed(2) ?? '—'}</span><span>￥{version.cost_cny.toFixed(2)}</span><span>{version.latency_s.toFixed(1)}s</span></span>
              <span className="video-candidate-note">{version.qa?.issues?.join('；') || version.error || (version.video_url ? '点击切换到此候选' : '视频尚不可预览')}</span>
            </button>
            <div className="video-candidate-actions">{version.video_url && <button type="button" className="btn ghost small" disabled={selectedCandidate} onClick={() => setPreviewId(version.id)}>{selectedCandidate ? '预览中' : '预览'}</button>}{version.video_url && !adopted && !archived && <button type="button" className="btn primary small" disabled={writeFrozen} onClick={() => openAdopt(version)}>采纳</button>}{!adopted && !archived && <button type="button" className="btn ghost small" onClick={() => { void archive(version) }}>归档</button>}{!adopted && !archived && !['queued', 'running', 'waiting_provider'].includes(version.status) && <button type="button" className="btn ghost small danger" onClick={() => { void remove(version) }}>删除</button>}{archived && <button type="button" className="btn ghost small" onClick={() => { void api.unarchiveVersion(version.id).then(onRefresh) }}>恢复归档</button>}</div>
          </article>
        }) : <div className="review-state-empty"><b>当前筛选下无候选</b><p>{shot.versions.length ? '请更改状态筛选。' : writeFrozen ? '请先完成分镜确认与资产门禁。' : '可点击「新建视频版本」创建候选。'}</p></div>}</div>
      </section>
    </div>
    {wizard && <Dialog title="新建视频版本" onClose={() => setWizard(null)} wide><div className="generation-mode-tabs"><button className={wizard === 'reroll' ? 'active' : ''} onClick={() => setWizard('reroll')}>按原词新建候选</button><button className={wizard === 'rewrite' ? 'active' : ''} onClick={() => setWizard('rewrite')}>修改提示词</button><button className={wizard === 'critique' ? 'active' : ''} onClick={() => setWizard('critique')}>带评语修复</button></div><div className="review-impact"><b>{wizard === 'reroll' ? '输入不变，强制创建新候选' : wizard === 'rewrite' ? '将以新生成词创建独立版本' : `将带入 ${review?.open_issue_count || 0} 条未解决评语`}</b><p>估算 ￥{shot.est_cost_cny.toFixed(2)}；旧采用版保留，失败不覆盖。提交后会出现在右侧候选列表。</p></div>{wizard === 'rewrite' && <><div className="prompt-diff"><div><b>原词</b><pre>{truncateText(adoptedVersion?.prompt_text || shot.prompt_preview || '无')}</pre></div><div><b>新词</b><textarea rows={8} maxLength={8000} value={prompt} onChange={event => setPrompt(event.target.value)} /></div></div><small>{prompt.length}/8000 字符；切镜后草稿保留在当前对话框，未提交不产生费用。</small></>}{wizard === 'critique' && <div className="critique-list">{review?.review_items.filter(item => ['open', 'in_progress'].includes(item.status)).map(item => <label key={item.id}><input type="checkbox" defaultChecked />[{item.severity}] {item.comment}</label>)}{!review?.open_issue_count && <p>当前无未解决评语，将使用视频 QA 问题清单。</p>}</div>}<div className="dialog-actions"><button className="btn ghost" onClick={() => setWizard(null)}>取消（零任务/零扣费）</button><button className="btn primary" disabled={generating || (wizard === 'rewrite' && !prompt.trim())} onClick={() => { void runGeneration() }}>继续到影响确认</button></div></Dialog>}
    {adopt && <Dialog title={`采纳镜 ${shot.shot_no} 的 v${adopt.version_no}`} onClose={() => setAdopt(null)}><div className="review-impact"><p>当前采用：{adoptedVersion ? `v${adoptedVersion.version_no}` : '无'}；目标候选：v{adopt.version_no}。</p><p>目标 QA {adopt.qa?.overall?.toFixed(2) ?? '未评估'}，费用 ￥{adopt.cost_cny.toFixed(2)}。提交会固定镜头和版本，并写入审计。</p></div><label className="review-field">必填采纳理由<textarea rows={4} value={adoptReason} onChange={event => setAdoptReason(event.target.value)} placeholder="说明画面质量、连续性或成本判断" /></label><div className="dialog-actions"><button className="btn ghost" onClick={() => setAdopt(null)}>取消</button><button className="btn primary" disabled={adoptReason.trim().length < 4} onClick={() => { void doAdopt() }}>确认采纳此候选</button></div></Dialog>}
    {stopOpen && <Dialog title={`停止镜 ${shot.shot_no} 的视频任务`} onClose={() => setStopOpen(false)}><div className="review-impact"><b>目标：{activeVersions.map(version => `v${version.version_no}（${videoVersionStatusLabel(version, false)}）`).join('、')}</b><p>将停止本地排队、轮询和后续写入。供应商已接单的任务可能无法硬停，仍可能继续执行并计费。</p><p>已知候选费用合计 ￥{activeVersions.reduce((sum, version) => sum + version.cost_cny, 0).toFixed(2)}。</p></div><div className="dialog-actions"><button className="btn ghost" onClick={() => setStopOpen(false)}>继续运行</button><AsyncButton className="btn danger" busyLabel="停止中…" onAction={async () => { const result = await api.stopShotVideo(shot.id); setStopOpen(false); onToast(result.provider_may_continue ? `停止请求已接受；任务 ${result.jobs.map(job => job.job_id).join('、')} 的供应商部分可能继续执行和计费` : '视频任务已停止'); await onRefresh() }}>确认停止这些任务</AsyncButton></div></Dialog>}
  </div>
}

function VideoWorkspace({ shot, episodeNo, episodeStatus, context, generating, setGenerating, writeFrozen, review, onRefresh, onToast }: { shot: Shot; episodeNo: number; episodeStatus: string; context: ReviewWallContext | null; generating: boolean; setGenerating: (busy: boolean) => void; writeFrozen: boolean; review: ReviewShotSummary | null; onRefresh: () => Promise<void>; onToast: (message: string, action?: { label: string; run: () => void }, persistent?: boolean) => void }) {
  const playable = shot.versions.filter(version => Boolean(version.video_url))
  const [aId, setAId] = useState<string | null>(playable[0]?.id || null)
  const [bId, setBId] = useState<string | null>(playable[1]?.id || null)
  const [sort, setSort] = useState<'time' | 'qa' | 'status'>('time')
  const [status, setStatus] = useState('all')
  const [adopt, setAdopt] = useState<ShotVersion | null>(null)
  const [adoptReason, setAdoptReason] = useState('')
  const [wizard, setWizard] = useState<'reroll' | 'rewrite' | 'critique' | null>(null)
  const [prompt, setPrompt] = useState('')
  const [archiveReason, setArchiveReason] = useState('')
  const [stopOpen, setStopOpen] = useState(false)
  useEffect(() => { setAId(playable[0]?.id || null); setBId(playable[1]?.id || null); setWizard(null); setAdopt(null) }, [shot.id]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (wizard !== 'rewrite') return
    const saved = localStorage.getItem(generationDraftKey(shot.id))
    if (saved) setPrompt(saved)
  }, [shot.id, wizard])
  useEffect(() => {
    if (wizard === 'rewrite' && prompt.trim()) localStorage.setItem(generationDraftKey(shot.id), prompt)
  }, [prompt, shot.id, wizard])
  const a = shot.versions.find(version => version.id === aId)
  const b = shot.versions.find(version => version.id === bId)
  const versions = useMemo(() => shot.versions.filter(version => status === 'all' || version.status === status).sort((left, right) => sort === 'qa' ? (right.qa?.overall ?? -Infinity) - (left.qa?.overall ?? -Infinity) || right.version_no - left.version_no : sort === 'status' ? videoVersionStatusLabel(left, left.id === shot.adopted_version_id).localeCompare(videoVersionStatusLabel(right, right.id === shot.adopted_version_id)) || right.version_no - left.version_no : right.version_no - left.version_no), [shot.adopted_version_id, shot.versions, sort, status])
  const runGeneration = async () => { if (!wizard || !context) return; const initial = shot.versions.find(version => version.id === shot.adopted_version_id)?.prompt_text || shot.prompt_preview || ''; const next = prompt.trim(); if (wizard === 'rewrite' && (!next || next === initial)) { onToast('修改模式需要与原词不同的有效生成词'); return } const openItems = review?.review_items.filter(item => ['open', 'in_progress'].includes(item.status)) || []; const selectedReviewIds = wizard === 'critique' ? Array.from(document.querySelectorAll<HTMLInputElement>('.critique-list input[type="checkbox"]')).flatMap((input, index) => input.checked && openItems[index] ? [openItems[index].id] : []) : []; if (wizard === 'critique' && openItems.length && !selectedReviewIds.length) { onToast('请至少选择一条要带入的评审项'); return } setGenerating(true); try { const result = await api.shotGenerate(shot.id, wizard === 'rewrite' ? next : undefined, wizard !== 'rewrite', wizard === 'critique' ? 'selected_review_items' : undefined, selectedReviewIds, context.upstream.qualification_version, newId(`generate:${shot.id}`)) as { reused?: boolean; version_id?: string; job_id?: string; status?: string }; if (wizard === 'rewrite') localStorage.removeItem(generationDraftKey(shot.id)); onToast(result.reused ? '输入未变化，已复用旧版；原词新候选会强制新建版本' : `请求已接受${result.job_id ? `，任务 ${result.job_id}` : ''}；正在排队/生成，尚未完成`); setWizard(null); await onRefresh() } catch (error) { onToast(error instanceof Error ? error.message : String(error), undefined, true) } finally { setGenerating(false) } }
  const doAdopt = async () => { if (!adopt || !context) return; try { await api.adoptVersion(shot.id, adopt.id, adoptReason.trim(), context.upstream.qualification_version, newId(`adopt:${adopt.id}`)); setAdopt(null); onToast(`已采用 v${adopt.version_no}，理由已写入审计`); await onRefresh() } catch (error) { onToast(error instanceof Error ? error.message : String(error), undefined, true) } }
  const archive = async (version: ShotVersion) => { try { await api.archiveVersion(version.id, archiveReason || '候选版本整理'); await onRefresh(); onToast(`v${version.version_no} 已归档`) } catch (error) { onToast(error instanceof Error ? error.message : String(error), undefined, true) } }
  const remove = async (version: ShotVersion) => { try { await api.deleteVersion(version.id); await onRefresh(); onToast(`v${version.version_no} 已删除`) } catch (error) { onToast(error instanceof Error ? error.message : String(error), undefined, true) } }
  const activeVersions = shot.versions.filter(version => ['queued', 'running', 'waiting_provider'].includes(version.status))
  return <div className="video-workspace"><ABPlayers a={a} b={b} shotNo={shot.shot_no} /><section className="video-toolbar"><div><button className="btn primary small" disabled={writeFrozen || generating || !['confirmed', 'generating', 'done'].includes(episodeStatus)} onClick={() => { setWizard('reroll'); setPrompt(shot.prompt_preview || '') }}>新建视频版本</button>{activeVersions.length > 0 && <button className="btn ghost small danger" onClick={() => setStopOpen(true)}>停止任务</button>}</div><span>单次估算 ¥{shot.est_cost_cny.toFixed(2)} · 当前评审问题 {review?.open_issue_count || 0}</span></section><section className="version-list"><header><b>视频版本</b><select aria-label="版本状态筛选" value={status} onChange={event => setStatus(event.target.value)}><option value="all">全部状态</option><option value="succeeded">已成功</option><option value="failed">失败</option><option value="running">运行中</option><option value="queued">排队中</option></select><select aria-label="版本排序" value={sort} onChange={event => setSort(event.target.value as typeof sort)}><option value="time">按时间</option><option value="qa">按 QA</option><option value="status">按状态</option></select></header>{versions.length ? versions.map(version => { const adopted = version.id === shot.adopted_version_id; const archived = Boolean(context?.archived_versions[version.id]); const qaDiff = version.qa?.overall != null && shot.versions.find(item => item.id === shot.adopted_version_id)?.qa?.overall != null ? version.qa.overall - (shot.versions.find(item => item.id === shot.adopted_version_id)?.qa?.overall || 0) : null; return <article className={`version-card${adopted ? ' adopted' : ''}${archived ? ' archived' : ''}`} key={version.id}><div><b>v{version.version_no}</b><span className={`stamp ${version.status === 'failed' ? 'red' : version.status === 'succeeded' ? 'green' : 'gold'}`}>{videoVersionStatusLabel(version, adopted)}</span>{archived && <span className="stamp grey">已归档</span>}</div><div className="version-metrics"><span>QA {version.qa?.overall?.toFixed(2) ?? '未评估'}{qaDiff != null ? `（较采用版 ${qaDiff >= 0 ? '+' : ''}${qaDiff.toFixed(2)}）` : ''}</span><span>¥{version.cost_cny.toFixed(2)}</span><span>{version.latency_s.toFixed(1)}s</span></div>{version.qa?.issues?.length ? <p>{version.qa.issues.join('；')}</p> : version.error ? <p className="err-text">{version.error}</p> : <p>与当前采用版的输入/失败差异暂无更多数据</p>}{version.adoption_reason && <small>采用理由：{version.adoption_reason}</small>}<div className="version-actions">{version.video_url && <><button onClick={() => setAId(version.id)} aria-pressed={aId === version.id}>设为 A</button><button onClick={() => setBId(version.id)} aria-pressed={bId === version.id}>设为 B</button><a href={version.video_url} download={`ep-${episodeNo}-shot-${shot.shot_no}-v${version.version_no}-${adopted ? 'adopted' : 'candidate'}.mp4`}>导出</a></>}{version.video_url && !adopted && !archived && <button disabled={writeFrozen} onClick={() => { setAdopt(version); setAdoptReason('') }}>采用</button>}{!adopted && !archived && <button onClick={() => { void archive(version) }}>归档</button>}{!adopted && !archived && !['queued', 'running', 'waiting_provider'].includes(version.status) && <button className="danger" onClick={() => { void remove(version) }}>删除</button>}{archived && <button onClick={() => { void api.unarchiveVersion(version.id).then(onRefresh) }}>恢复归档</button>}</div></article> }) : <div className="review-state-empty"><b>当前筛选下无视频版本</b><p>{shot.versions.length ? '请清除状态筛选。' : writeFrozen ? '请先完成分镜确认与资产门禁，或等待上游任务结束。' : '可点击「新建视频版本」进入向导。'}</p></div>}</section>{wizard && <Dialog title="新建视频版本" onClose={() => setWizard(null)} wide><div className="generation-mode-tabs"><button className={wizard === 'reroll' ? 'active' : ''} onClick={() => setWizard('reroll')}>按原词新建候选</button><button className={wizard === 'rewrite' ? 'active' : ''} onClick={() => setWizard('rewrite')}>修改提示词</button><button className={wizard === 'critique' ? 'active' : ''} onClick={() => setWizard('critique')}>带评语修复</button></div><div className="review-impact"><b>{wizard === 'reroll' ? '输入不变，强制创建新候选' : wizard === 'rewrite' ? '将以新生成词创建独立版本' : `将带入 ${review?.open_issue_count || 0} 条未解决评语`}</b><p>估算 ¥{shot.est_cost_cny.toFixed(2)}；旧采用版保留，失败不覆盖。提交后显示任务标识和阶段。</p></div>{wizard === 'rewrite' && <><div className="prompt-diff"><div><b>原词</b><pre>{truncateText(shot.versions.find(version => version.id === shot.adopted_version_id)?.prompt_text || shot.prompt_preview || '无')}</pre></div><div><b>新词</b><textarea rows={8} maxLength={8000} value={prompt} onChange={event => setPrompt(event.target.value)} /></div></div><small>{prompt.length}/8000 字符；切镜后向导草稿保留在当前对话框，未提交不产生费用。</small></>}{wizard === 'critique' && <div className="critique-list">{review?.review_items.filter(item => ['open', 'in_progress'].includes(item.status)).map(item => <label key={item.id}><input type="checkbox" defaultChecked />[{item.severity}] {item.comment}</label>)}{!review?.open_issue_count && <p>当前无未解决评语，将使用视频 QA 问题清单。</p>}</div>}<div className="dialog-actions"><button className="btn ghost" onClick={() => setWizard(null)}>取消（零任务/零扣费）</button><button className="btn primary" disabled={generating || (wizard === 'rewrite' && !prompt.trim())} onClick={() => { void runGeneration() }}>继续到影响确认</button></div></Dialog>}{adopt && <Dialog title={`采用镜 ${shot.shot_no} 的 v${adopt.version_no}`} onClose={() => setAdopt(null)}><div className="review-impact"><p>当前采用：{shot.versions.find(version => version.id === shot.adopted_version_id) ? `v${shot.versions.find(version => version.id === shot.adopted_version_id)?.version_no}` : '无'}；目标：v{adopt.version_no}；A/B：{a ? `v${a.version_no}` : '未选'}/{b ? `v${b.version_no}` : '未选'}。</p><p>目标 QA {adopt.qa?.overall?.toFixed(2) ?? '未评估'}，费用 ¥{adopt.cost_cny.toFixed(2)}。提交会固定 shotId + versionId + 资格快照，并写入审计。</p></div><label className="review-field">必填有效采用理由<textarea rows={4} value={adoptReason} onChange={event => setAdoptReason(event.target.value)} placeholder="说明与 A/B 的质量、连续性或成本比较" /></label><div className="dialog-actions"><button className="btn ghost" onClick={() => setAdopt(null)}>取消</button><button className="btn primary" disabled={adoptReason.trim().length < 4} onClick={() => { void doAdopt() }}>继续到权威确认</button></div></Dialog>}{stopOpen && <Dialog title={`停止镜 ${shot.shot_no} 的视频任务`} onClose={() => setStopOpen(false)}><div className="review-impact"><b>目标：{activeVersions.map(version => `v${version.version_no}（${videoVersionStatusLabel(version, false)}）`).join('、')}</b><p>将停止本地排队、轮询和后续写入。供应商已接单的任务可能无法硬停，仍可能继续执行并计费；系统不会承诺不可得的剩余费用。</p><p>已知候选费用合计 ¥{activeVersions.reduce((sum, version) => sum + version.cost_cny, 0).toFixed(2)}；停止请求被接受不等于供应商已终止。</p></div><div className="dialog-actions"><button className="btn ghost" onClick={() => setStopOpen(false)}>继续运行</button><AsyncButton className="btn danger" busyLabel="停止中…" onAction={async () => { const result = await api.stopShotVideo(shot.id); setStopOpen(false); onToast(result.provider_may_continue ? `停止请求已接受；任务 ${result.jobs.map(job => job.job_id).join('、')} 的供应商部分可能继续执行和计费` : '视频任务已停止'); await onRefresh() }}>确认停止这些任务</AsyncButton></div></Dialog>}</div>
}

function ABPlayers({ a, b, shotNo }: { a?: ShotVersion; b?: ShotVersion; shotNo: number }) {
  const aRef = useRef<HTMLVideoElement>(null)
  const bRef = useRef<HTMLVideoElement>(null)
  const [rate, setRate] = useState(1)
  const [loopStart, setLoopStart] = useState(0)
  const [loopEnd, setLoopEnd] = useState<number | null>(null)
  const [playing, setPlaying] = useState(false)
  const [mediaErrors, setMediaErrors] = useState<Record<'A' | 'B', string | null>>({ A: null, B: null })
  useEffect(() => { setMediaErrors({ A: null, B: null }); setPlaying(false) }, [a?.id, b?.id])
  const setTime = (time: number) => { for (const video of [aRef.current, bRef.current]) if (video) video.currentTime = Math.max(0, Math.min(time, video.duration || time)) }
  const toggle = async () => { const videos = [aRef.current, bRef.current].filter(Boolean) as HTMLVideoElement[]; if (!videos.length) return; if (playing) videos.forEach(video => video.pause()); else { const time = Math.max(...videos.map(video => video.currentTime)); setTime(time); await Promise.allSettled(videos.map(video => video.play())) } setPlaying(!playing) }
  const step = (frames: number) => { [aRef.current, bRef.current].forEach(video => video?.pause()); setPlaying(false); setTime((aRef.current?.currentTime || bRef.current?.currentTime || 0) + frames / 30) }
  useEffect(() => { for (const video of [aRef.current, bRef.current]) if (video) video.playbackRate = rate }, [rate, a?.id, b?.id])
  const onTime = (source: HTMLVideoElement, other: HTMLVideoElement | null) => { const end = loopEnd; if (end != null && end > loopStart && source.currentTime >= end) setTime(loopStart); else if (other && Math.abs(source.currentTime - other.currentTime) > 0.12 && !other.seeking) other.currentTime = Math.min(source.currentTime, other.duration || source.currentTime) }
  const player = (label: 'A' | 'B', version: ShotVersion | undefined, ref: React.RefObject<HTMLVideoElement>, other: React.RefObject<HTMLVideoElement>) => <div className="ab-player"><header><b>{label} · 镜 {shotNo} {version ? `· v${version.version_no}` : ''}</b><span>QA {version?.qa?.overall?.toFixed(2) ?? '—'} · {version ? videoVersionStatusLabel(version, false) : '未选版本'}</span></header>{version?.video_url ? <video key={version.id} ref={ref} src={version.video_url} preload="metadata" onLoadedData={() => setMediaErrors(current => ({ ...current, [label]: null }))} onError={() => setMediaErrors(current => ({ ...current, [label]: `无法加载 v${version.version_no} 的媒体，请检查访问权限或稍后重试` }))} onTimeUpdate={event => onTime(event.currentTarget, other.current)} onPause={() => setPlaying(false)} onEnded={() => setPlaying(false)} /> : <div className="ab-empty">从版本列表选择 {label} 版</div>}{(mediaErrors[label] || version?.error) && <div className="review-persistent-error compact" role="alert">{mediaErrors[label] || `该侧媒体生成失败：${version?.error}`}</div>}</div>
  return <section className="ab-comparison"><div className="ab-grid">{player('A', a, aRef, bRef)}{player('B', b, bRef, aRef)}</div><div className="ab-controls" aria-label="A/B 同步控制"><button onClick={() => { void toggle() }}>{playing ? '同步暂停' : '同步播放'}</button><button onClick={() => step(-1)}>上一帧</button><button onClick={() => step(1)}>下一帧</button><label>倍速<select value={rate} onChange={event => setRate(Number(event.target.value))}><option value="0.5">0.5×</option><option value="1">1×</option><option value="1.5">1.5×</option><option value="2">2×</option></select></label><button onClick={() => setLoopStart(aRef.current?.currentTime || bRef.current?.currentTime || 0)}>设循环起点 {loopStart.toFixed(2)}s</button><button onClick={() => setLoopEnd(aRef.current?.currentTime || bRef.current?.currentTime || null)}>设循环终点 {loopEnd?.toFixed(2) ?? '—'}s</button><button onClick={() => { setLoopStart(0); setLoopEnd(null) }}>清除循环</button></div>{a && b && <div className="qa-diff"><b>QA 差异</b><span>总分 {(a.qa?.overall ?? 0).toFixed(2)} → {(b.qa?.overall ?? 0).toFixed(2)}（{((b.qa?.overall ?? 0) - (a.qa?.overall ?? 0)) >= 0 ? '+' : ''}{((b.qa?.overall ?? 0) - (a.qa?.overall ?? 0)).toFixed(2)}）</span><span>A：{a.qa?.issues?.join('；') || '无已知问题'}</span><span>B：{b.qa?.issues?.join('；') || '无已知问题'}</span><small>时长/帧率不同时按时间轴对齐；较短版到末尾后保持末帧，偏差超过 120ms 时重新对齐。</small></div>}</section>
}
