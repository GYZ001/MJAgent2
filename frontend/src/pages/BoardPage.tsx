import { ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError, Episode, Shot, StoryboardStatus } from '../api'
import { useEpisode, useNav } from '../App'
import EpisodeCrumb from '../components/EpisodeCrumb'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'
import ImpactDialog, { ImpactSummary } from '../components/harness/ImpactDialog'
import SupervisorPanel from '../components/SupervisorPanel'
import { useFocusTrap } from '../hooks/useFocusTrap'

const SIZES = ['远景', '全景', '中景', '近景', '特写']
const MOVES = ['固定', '推近', '拉远', '横摇', '跟随']
const TRANSITIONS = ['硬切', '叠化', '淡出淡入', '黑场', '闪黑', '闪白', '甩镜', '遮挡转场', '匹配剪辑', '声音延续+叠化', '声音先行+淡入']
const DURATIONS = [5, 6, 7, 8, 9, 10]
const CONTINUITY_MODES: Record<string, string> = {
  action_continuation: '动作延续', same_scene_cut: '同场切换', reaction_cut: '反应镜头',
  reverse_angle: '反打', insert_detail: '细节插入', scene_change: '转场换景',
}

const EDITABLE_FIELDS: Array<keyof Shot> = [
  'duration_s', 'shot_size', 'camera_move', 'scene_setting', 'characters', 'action_desc',
  'first_frame_desc', 'last_frame_desc', 'dialogues', 'transition', 'continuity_from_prev',
  'continuity_mode', 'state_in', 'primary_action', 'state_out', 'characters_visible',
  'audio_cast', 'new_information_ids', 'spine_beat_ids', 'key_line_ids',
]

type EditSession = {
  edit_session_token: string
  baseline_artifact_id?: string | null
  baseline_content_hash: string
  lease_expires_at: number
}

type SourceChapter = {
  id: number; idx: number; title: string; content: string; source_version_hash: string
}

type SourceBindingInput = {
  chapter_id: number; source_version_hash: string; start_offset: number; end_offset: number
}

type StartPreview = {
  preview_token: string
  action: 'create' | 'resume'
  kept_validated_shots: number
  planned_shots?: number | null
  remaining_shots?: number | null
  checkpoint: { available: boolean; phase?: string | null; resume_from_shot: number }
  impact: string
  estimated_wait_minutes: number[]
  estimated_cost_cny?: number | null
  estimate_note: string
}

type ConfirmPreview = {
  preview_token?: string
  storyboard_artifact_id?: string | null
  shot_count: number
  planned_shots: number
  total_duration_s: number
  final_shot_valid: boolean
  hard_gates: { passed: boolean; errors: string[] }
  warnings: string[]
  estimated_video_cost_cny: { min: number; max: number; note: string }
  unlocks: string[]
}

type DraftItem = {
  id: string; version: number; content: Partial<Shot>; baseline_artifact_ids: string[]
  issues: string[]; created_at: number
}

type StructurePreview = ImpactSummary & {
  preview_token: string
  operation: 'add_after' | 'duplicate_after' | 'delete' | 'move'
  shot_id: string
  target_index: number
  new_final_shot_id?: string | null
  before_count: number
  after_count: number
  renumbered_shots: number
  revalidation_shots: number[]
  final_shot_impact: string
}

function cloneShot(shot: Shot): Shot {
  return JSON.parse(JSON.stringify(shot)) as Shot
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null)
}

export function buildStoryboardChanges(original: Shot, edit: Shot, sourceBinding: SourceBindingInput | null): Record<string, unknown> {
  const changes: Record<string, unknown> = {}
  for (const key of EDITABLE_FIELDS) {
    if (!same(original[key], edit[key])) changes[key] = edit[key]
  }
  if (sourceBinding) changes.source_binding = sourceBinding
  return changes
}

export function storyboardSpokenChars(shot: Shot): number {
  const punctuation = /[\s\u2000-\u206F\u3000-\u303F\uFF00-\uFFEF!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~，。！？：；、…—·「」『』【】（）《》〈〉“”‘’]/g
  return (shot.dialogues ?? []).reduce((total, line) => total + (line.line || '').replace(punctuation, '').length, 0)
}

export function isStoryboardProblemShot(shot: Shot): boolean {
  const status = shot.storyboard_evidence?.status || ''
  return shot.spoken_contract_status === 'conflict'
    || Boolean(shot.legacy_unvalidated)
    || ['candidate', 'needs_revision', 'rejected', 'stale', 'superseded'].includes(status)
    || storyboardSpokenChars(shot) > (shot.spoken_limit ?? Number.POSITIVE_INFINITY)
    || Boolean(shot.preflight_errors?.length)
}

export function storyboardGateIssueLabel(message: string): string {
  return message
    .replace(/^shots\[\d+\]\(shot_no=(\d+)\)\./, '第 $1 镜：')
    .replace(/^shot_no=(\d+)\./, '第 $1 镜：')
    .replaceAll('action_desc', '画面动作')
    .replaceAll('primary_action', '镜头动作')
    .replaceAll('first_frame_desc', '首帧画面')
    .replaceAll('last_frame_desc', '尾帧画面')
}

function fieldId(shotId: string, name: string): string {
  return `shot-${shotId}-${name}`
}

function Modal({ open, title, children, onClose, actions }: {
  open: boolean; title: string; children: ReactNode; onClose: () => void; actions: ReactNode
}) {
  const trapRef = useFocusTrap(open, onClose)
  if (!open) return null
  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog storyboard-modal" role="dialog" aria-modal="true" aria-labelledby="storyboard-modal-title">
        <h3 id="storyboard-modal-title" tabIndex={-1}>{title}</h3>
        {children}
        <div className="dialog-actions">{actions}</div>
      </section>
    </div>
  )
}

function StoryboardLaunchPanel({ episode, status, busy, onPrimary }: {
  episode: Episode
  status: StoryboardStatus
  busy: boolean
  onPrimary: () => void
}) {
  const screenplay = episode.screenplay
  const scenes = screenplay?.scene_outline ?? []
  const characters = [...new Set(scenes.flatMap(scene => scene.characters ?? []).filter(Boolean))]
  const scriptLength = (screenplay?.full_script_text ?? '').replace(/\s/g, '').length
  const summary = screenplay?.logline
    || screenplay?.plot_spine?.episode_premise
    || screenplay?.episode_premise
    || episode.synopsis
    || '当前剧本已准备就绪，可以开始拆解镜头、动作与连续性。'
  const screenplayReady = status.screenplay_available

  return (
    <section className={`storyboard-launch card ${screenplayReady ? 'is-ready' : 'needs-screenplay'}`} aria-labelledby="storyboard-launch-title">
      <header className="storyboard-launch-head">
        <div className="storyboard-launch-status">
          <span className="storyboard-launch-mark" aria-hidden="true">镜</span>
          <div><b>{screenplayReady ? '剧本已就绪' : '等待剧本'}</b><small>{screenplayReady ? '分镜尚未生成' : '完成剧本后才能拆解镜头'}</small></div>
        </div>
        <span className="storyboard-launch-version">状态快照 v{status.snapshot_version}</span>
      </header>

      <div className="storyboard-launch-main">
        <div className="storyboard-launch-copy">
          <span className="storyboard-launch-eyebrow">第 {episode.episode_no} 集 · {screenplay?.title || episode.title}</span>
          <h2 id="storyboard-launch-title">{screenplayReady ? '把定稿剧本拆成可审阅的镜头' : '先完成剧本，再进入分镜制作'}</h2>
          <p>{summary}</p>

          <dl className="storyboard-launch-facts" aria-label="剧本概览">
            <div><dt>场次</dt><dd>{scenes.length || '待整理'}</dd></div>
            <div><dt>主要角色</dt><dd>{characters.length || '待整理'}</dd></div>
            <div><dt>目标时长</dt><dd>{episode.target_duration_s}s</dd></div>
            <div><dt>剧本文字</dt><dd>{scriptLength ? `${scriptLength} 字` : '已就绪'}</dd></div>
          </dl>

          {!!scenes.length && <div className="storyboard-launch-scenes" aria-label="场景预览">
            {scenes.slice(0, 4).map(scene => <span key={scene.scene_no}>{String(scene.scene_no).padStart(2, '0')} · {scene.scene_heading}</span>)}
            {scenes.length > 4 && <span>另有 {scenes.length - 4} 场</span>}
          </div>}
        </div>

        <aside className="storyboard-launch-action" aria-label="下一步">
          <span className="storyboard-launch-step">下一步</span>
          <h3>{screenplayReady ? '生成首版分镜' : '前往剧本台'}</h3>
          <p>{screenplayReady
            ? '系统会先锁定当前剧本版本，展示计划镜数、预计等待与影响范围，再由你确认启动。'
            : '当前没有可用于分镜的发布版剧本，请先完成剧本生成与校验。'}</p>
          <button type="button" className="btn primary storyboard-launch-button" disabled={busy} onClick={onPrimary}>
            {busy ? '正在准备预检…' : screenplayReady ? '开始生成分镜' : '先去剧本台'}
          </button>
          <small>{screenplayReady ? '不会直接生成付费视频，也不会跳过确认门禁。' : '完成后返回本页即可继续。'}</small>
        </aside>
      </div>

      {screenplayReady && <ol className="storyboard-launch-flow" aria-label="分镜生成流程">
        <li><b>01</b><span><strong>锁定剧本基线</strong><small>防止生成期间版本漂移</small></span></li>
        <li><b>02</b><span><strong>规划并生成逐镜</strong><small>建立动作、角色与连续性</small></span></li>
        <li><b>03</b><span><strong>人工审阅后确认</strong><small>通过门禁才交接评审墙</small></span></li>
      </ol>}
    </section>
  )
}

function statusFallback(ep: Episode): StoryboardStatus {
  return {
    contract_version: 'storyboard-workspace.v1', snapshot_version: 0, state_fingerprint: '',
    state: 'syncing', headline: '状态同步中，暂不可执行高影响操作',
    screenplay_available: ep.screenplay_status === 'ready', planned_shots: 0,
    produced_shots: ep.shots?.length ?? 0, validated_shots: 0, final_shot_valid: false,
    hard_gates_passed: false, confirmed: false, editable: false, confirmable: false,
    recommended_action: 'refresh_status', write_block_reason: '等待服务端状态快照',
  }
}

export default function BoardPage() {
  const { episodeId, go, projectId, toast } = useNav()
  const { data: ep, refresh, error, loading } = useEpisode(episodeId!, 'board')
  const [busy, setBusy] = useState(false)
  const [selectedShotId, setSelectedShotId] = useState<string | null>(null)
  const [onlyProblems, setOnlyProblems] = useState(false)
  const [sceneFilter, setSceneFilter] = useState('')
  const [characterFilter, setCharacterFilter] = useState('')
  const [capacityFilter, setCapacityFilter] = useState(false)
  const [riskFilter, setRiskFilter] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [progressOpen, setProgressOpen] = useState(false)
  const [recoveredStatus, setRecoveredStatus] = useState<StoryboardStatus | null>(null)
  const [startPreview, setStartPreview] = useState<StartPreview | null>(null)
  const [autoConfirm, setAutoConfirm] = useState(false)
  const [confirmPreview, setConfirmPreview] = useState<ConfirmPreview | null>(null)
  const [cancelOpen, setCancelOpen] = useState(false)
  const [structurePreview, setStructurePreview] = useState<StructurePreview | null>(null)
  const timelineRef = useRef<HTMLDivElement>(null)
  const moreActionsRef = useRef<HTMLDetailsElement>(null)
  const startPreviewTriggerRef = useRef<HTMLElement | null>(null)
  const storyboardTimer = useTaskTimer(`episode.${episodeId}.storyboard`, ep?.storyboard_status?.state === 'running')

  const shots = ep?.shots ?? []
  const visibleShots = useMemo(() => shots.filter(shot => {
    if (onlyProblems && !isStoryboardProblemShot(shot)) return false
    if (sceneFilter && shot.scene_setting !== sceneFilter) return false
    if (characterFilter && ![...(shot.characters ?? []), ...(shot.audio_cast ?? [])].includes(characterFilter)) return false
    if (capacityFilter && storyboardSpokenChars(shot) <= (shot.spoken_limit ?? Number.POSITIVE_INFINITY)) return false
    if (riskFilter && !shot.preflight_errors?.length && !shot.continuity_degraded && !(shot.risk_tags?.length)) return false
    return true
  }), [shots, onlyProblems, sceneFilter, characterFilter, capacityFilter, riskFilter])
  const selectedShot = visibleShots.find(shot => shot.id === selectedShotId) ?? visibleShots[0] ?? shots[0]
  const selectedIndex = Math.max(0, visibleShots.findIndex(shot => shot.id === selectedShot?.id))
  const absoluteIndex = shots.findIndex(shot => shot.id === selectedShot?.id)
  const status = ep?.storyboard_status ?? recoveredStatus ?? (ep ? statusFallback(ep) : null)

  const scenes = useMemo(() => [...new Set(shots.map(shot => shot.scene_setting).filter(Boolean))], [shots])
  const characters = useMemo(() => [...new Set(shots.flatMap(shot => [...(shot.characters ?? []), ...(shot.audio_cast ?? [])]).filter(Boolean))], [shots])

  useEffect(() => {
    if (selectedShot && selectedShot.id !== selectedShotId) setSelectedShotId(selectedShot.id)
  }, [selectedShot?.id, selectedShotId])

  useEffect(() => {
    timelineRef.current?.querySelector<HTMLElement>('[aria-current="true"]')
      ?.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' })
  }, [selectedShotId])

  // 兼容缺少内嵌 storyboard_status 的旧详情响应，避免页面永久停在只读占位态。
  useEffect(() => {
    if (!ep || ep.storyboard_status) {
      setRecoveredStatus(null)
      return
    }
    let active = true
    void api.get(`/episodes/${ep.id}/storyboard/status`)
      .then(value => { if (active) setRecoveredStatus(value as StoryboardStatus) })
      .catch(() => { /* 仍保留安全只读占位态，由手动刷新继续恢复。 */ })
    return () => { active = false }
  }, [ep?.id, Boolean(ep?.storyboard_status)])

  useEffect(() => {
    if (!menuOpen) return
    const closeOnOutsideClick = (event: PointerEvent) => {
      if (!moreActionsRef.current?.contains(event.target as Node)) setMenuOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setMenuOpen(false)
      moreActionsRef.current?.querySelector<HTMLElement>('summary')?.focus()
    }
    document.addEventListener('pointerdown', closeOnOutsideClick)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('pointerdown', closeOnOutsideClick)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [menuOpen])

  const selectRelative = (offset: number) => {
    if (!visibleShots.length) return
    const next = Math.max(0, Math.min(visibleShots.length - 1, selectedIndex + offset))
    setSelectedShotId(visibleShots[next].id)
  }

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return
      const target = event.target as HTMLElement | null
      if (target?.closest('input, textarea, select, [role="combobox"], [contenteditable="true"]')) return
      event.preventDefault()
      selectRelative(event.key === 'ArrowLeft' ? -1 : 1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [visibleShots, selectedIndex])

  if (error && !ep) return <div className="empty">{error}</div>
  if (loading && !ep) return <div className="empty">展卷中……</div>
  if (!ep || !status) return <div className="empty">展卷中……</div>

  const run = async <T,>(fn: () => Promise<T>, message?: string): Promise<T | undefined> => {
    setBusy(true)
    try {
      const result = await fn()
      if (message) toast(message)
      await refresh()
      return result
    } catch (caught) {
      toast((caught as Error).message, true)
      return undefined
    } finally {
      setBusy(false)
    }
  }

  const refreshBoardState = async () => {
    await refresh()
    if (ep.storyboard_status) return
    try {
      setRecoveredStatus(await api.get(`/episodes/${ep.id}/storyboard/status`) as StoryboardStatus)
    } catch {
      // 详情与轻量状态接口都不可用时继续展示安全占位态。
    }
  }

  const loadStartPreview = async (mode: 'create' | 'resume') => {
    startPreviewTriggerRef.current = document.activeElement as HTMLElement | null
    setBusy(true)
    try {
      const preview = await api.post(`/episodes/${ep.id}/storyboard/preflight`, { mode }) as StartPreview
      setAutoConfirm(false)
      setStartPreview(preview)
    } catch (caught) {
      toast((caught as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const closeStartPreview = () => {
    setStartPreview(null)
    window.requestAnimationFrame(() => startPreviewTriggerRef.current?.focus())
  }

  const submitStart = async () => {
    if (!startPreview) return
    const preview = startPreview
    setStartPreview(null)
    storyboardTimer.start()
    const path = preview.action === 'resume'
      ? `/episodes/${ep.id}/storyboard/resume`
      : `/episodes/${ep.id}/storyboard`
    const result = await run(
      () => api.post(path, {
        preflight_token: preview.preview_token,
        completion_mode: autoConfirm ? 'auto_confirm' : 'ready_for_manual_confirm',
      }),
      preview.action === 'resume' ? '已从安全检查点继续生成' : '分镜生成已开始',
    )
    if (!result) storyboardTimer.clear()
  }

  const runPrimary = async () => {
    switch (status.recommended_action) {
      case 'go_screenplay': go('script', projectId, ep.id); break
      case 'generate_storyboard': await loadStartPreview('create'); break
      case 'resume_storyboard': await loadStartPreview('resume'); break
      case 'view_progress': setProgressOpen(true); window.requestAnimationFrame(() => document.getElementById('storyboard-progress')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })); break
      case 'confirm_storyboard': {
        setBusy(true)
        try { setConfirmPreview(await api.post(`/episodes/${ep.id}/confirm-preview`) as ConfirmPreview) }
        catch (caught) {
          const apiError = caught as ApiError
          const detail = apiError.detail as Partial<ConfirmPreview> | undefined
          if (detail?.hard_gates && detail.estimated_video_cost_cny && detail.unlocks) {
            setConfirmPreview(detail as ConfirmPreview)
          } else {
            toast(apiError.message, true)
          }
        }
        finally { setBusy(false) }
        break
      }
      case 'go_review_wall': go('wall', projectId, ep.id); break
      default: await refreshBoardState()
    }
  }

  const confirmStoryboard = async () => {
    if (!confirmPreview?.preview_token || !confirmPreview.hard_gates.passed) return
    const token = confirmPreview.preview_token
    setConfirmPreview(null)
    await run(() => api.post(`/episodes/${ep.id}/confirm`, { preview_token: token }), '分镜已确认，可以进入评审墙')
  }

  const requestPauseAndEdit = async () => {
    if (!ep.active_storyboard_run_id) return
    await run(
      () => api.post(`/runs/${ep.active_storyboard_run_id}/pause`),
      '已请求在安全点暂停；服务端确认暂停后开放编辑',
    )
  }

  const previewStructure = async (operation: StructurePreview['operation'], targetIndex = absoluteIndex) => {
    if (!selectedShot) return
    const newFinalShotId = operation === 'delete' && selectedShot.is_final
      ? shots[Math.max(0, absoluteIndex - 1)]?.id
      : undefined
    setBusy(true)
    try {
      const preview = await api.post(`/episodes/${ep.id}/storyboard/structure-preview`, {
        operation, shot_id: selectedShot.id, target_index: targetIndex,
        new_final_shot_id: newFinalShotId,
      }) as StructurePreview
      setStructurePreview(preview)
    } catch (caught) {
      toast((caught as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const applyStructure = async () => {
    if (!structurePreview) return
    const preview = structurePreview
    setStructurePreview(null)
    const result = await run(() => api.post(`/episodes/${ep.id}/storyboard/structure`, {
      preview_token: preview.preview_token, operation: preview.operation,
      shot_id: preview.shot_id, target_index: preview.target_index,
      new_final_shot_id: preview.new_final_shot_id,
    }), '镜头结构已更新，相邻窗口已进入重新校验') as { created_shot_id?: string } | undefined
    if (result?.created_shot_id) setSelectedShotId(result.created_shot_id)
  }

  const totalDuration = shots.reduce((total, shot) => total + shot.duration_s, 0)
  const primaryLabel: Record<StoryboardStatus['recommended_action'], string> = {
    go_screenplay: '先去剧本台', generate_storyboard: '生成分镜', view_progress: '查看分镜进度',
    resume_storyboard: status.final_shot_valid && status.produced_shots >= status.planned_shots
      ? '修复分镜问题'
      : `继续生成分镜${status.validated_shots ? `（从第 ${status.validated_shots + 1} 镜）` : ''}`,
    confirm_storyboard: '确认分镜', go_review_wall: '进入评审墙', refresh_status: '刷新状态',
  }
  const showLaunchPanel = !shots.length && (status.state === 'empty' || status.state === 'no_screenplay')
  const syncAction = status.recommended_action === 'refresh_status'
  const canCreateRevision = status.state !== 'running' && status.screenplay_available && status.editable
  const revisionBlockReason = status.write_block_reason || '当前状态暂不可创建新的分镜修订'

  return (
    <>
      <header className="desk-head">
        <EpisodeCrumb label="分镜台" view="board" episodeNo={ep.episode_no} />
        <h1>分镜台 <span className="sub">《{ep.title}》 · 安全审阅、修镜并交接下游</span></h1>
        <hr className="rule" />
      </header>

      {showLaunchPanel ? (
        <StoryboardLaunchPanel episode={ep} status={status} busy={busy} onPrimary={() => { void runPrimary() }} />
      ) : <><section className={`card board-toolbar state-${status.state}`} aria-labelledby="storyboard-state-title">
        <div className="board-toolbar-row">
          <div className="board-state-copy">
            <span className={`storyboard-state-dot state-${status.state}`} aria-hidden="true" />
            <div>
              <strong id="storyboard-state-title">{status.headline}</strong>
              <small>
                {status.produced_shots}/{status.planned_shots || '—'} 镜 · 已通过 {status.validated_shots} 镜
                {status.final_shot_valid ? ' · 最终镜有效' : ''}
              </small>
            </div>
          </div>
          <div className="board-action-group">
            <button type="button" className={`btn board-primary-action${syncAction ? ' is-sync' : ' primary'}`} disabled={busy} onClick={() => void runPrimary()}>
              {busy ? '处理中…' : primaryLabel[status.recommended_action]}
            </button>
            <details ref={moreActionsRef} className="board-more-actions" open={menuOpen} onToggle={event => setMenuOpen(event.currentTarget.open)}>
              <summary className="btn ghost"><span>更多操作</span><i aria-hidden="true" /></summary>
              <div className="board-more-menu" role="menu">
                {status.state === 'running' && <button type="button" role="menuitem" onClick={() => { setMenuOpen(false); void requestPauseAndEdit() }}>暂停并编辑</button>}
                {status.state === 'running' && <button type="button" role="menuitem" className="danger" onClick={() => { setMenuOpen(false); setCancelOpen(true) }}>取消本次生成</button>}
                {status.state !== 'running' && status.screenplay_available && <button type="button" role="menuitem" disabled={!canCreateRevision || busy}
                  title={!canCreateRevision ? revisionBlockReason : undefined}
                  onClick={() => { setMenuOpen(false); void loadStartPreview('create') }}>创建新的分镜修订</button>}
                {(status.state === 'running' || ep.supervisor) && status.recommended_action !== 'view_progress' && <button type="button" role="menuitem" onClick={() => {
                  setProgressOpen(value => !value); setMenuOpen(false)
                }}>{progressOpen ? '收起进度详情' : '查看进度详情'}</button>}
                {!syncAction && <button type="button" role="menuitem" onClick={() => { setMenuOpen(false); void refreshBoardState() }}>刷新状态</button>}
              </div>
            </details>
          </div>
          {status.state === 'running' && <TaskTimer label="分镜" timer={storyboardTimer} />}
        </div>
        <div className="board-stat-strip" aria-label="分镜统计">
          <span><b>{shots.length}</b> 镜头</span><span><b>{totalDuration}s</b> 总时长</span>
          <span><b>v{status.snapshot_version}</b> 状态快照</span>
          <span><b>{shots.filter(isStoryboardProblemShot).length}</b> 问题镜</span>
        </div>
        {status.write_block_reason && status.state === 'syncing' && <div className="board-sync-banner" role="status"><b>正在同步状态</b><span>{status.write_block_reason}</span></div>}
        {(status.state === 'running' || ep.supervisor) && (
          <div id="storyboard-progress" className={`board-progress-drawer ${progressOpen ? 'open' : ''}`}>
            <SupervisorPanel api={api} episodeId={ep.id} runId={ep.active_storyboard_run_id}
              supervisor={ep.supervisor} scripting={status.state === 'running'} onChanged={() => { void refresh() }} />
          </div>
        )}
        {(ep.storyboard_warning || ep.script_error) && (
          <details className="storyboard-error-details">
            <summary>{ep.script_error ? '查看待处理问题' : '查看提示'}</summary>
            <p>{ep.script_error || ep.storyboard_warning}</p>
            <small>建议动作只使用页面当前可见入口；错误编号和运行信息可在进度详情中复制。</small>
          </details>
        )}
        {!!status.hard_gate_issues?.length && (
          <details className="storyboard-error-details">
            <summary>查看 {status.hard_gate_issue_count} 个待处理问题</summary>
            <ul>{status.hard_gate_issues.map((item, index) => <li key={`${index}-${item}`}>{storyboardGateIssueLabel(item)}</li>)}</ul>
            <small>可在下方问题镜修改，或使用主操作让 Agent 从安全检查点继续修复。</small>
          </details>
        )}
      </section>

      <div className="workspace-gap" />

      {!shots.length ? (
        <div className="empty storyboard-empty">
          <div className="big">镜</div>
          {status.state === 'no_screenplay' ? '尚无可用于分镜的剧本，请先去剧本台。' : '剧本已就绪，尚未生成分镜。'}
        </div>
      ) : (
        <div className="board-workspace">
          <section className="shot-navigator" aria-label="镜头轨道">
            <div className="shot-filter-bar" aria-label="镜头筛选">
              <label><input type="checkbox" checked={onlyProblems} onChange={event => setOnlyProblems(event.target.checked)} />仅看问题镜</label>
              <label><span>场景</span><select aria-label="按场景筛选" value={sceneFilter} onChange={event => setSceneFilter(event.target.value)}><option value="">全部</option>{scenes.map(scene => <option key={scene}>{scene}</option>)}</select></label>
              <label><span>角色</span><select aria-label="按角色筛选" value={characterFilter} onChange={event => setCharacterFilter(event.target.value)}><option value="">全部</option>{characters.map(name => <option key={name}>{name}</option>)}</select></label>
              <label><input type="checkbox" checked={capacityFilter} onChange={event => setCapacityFilter(event.target.checked)} />口播超限</label>
              <label><input type="checkbox" checked={riskFilter} onChange={event => setRiskFilter(event.target.checked)} />连续性风险</label>
              <span className="shot-keyboard-hint">← → 切镜（输入时不会触发）</span>
            </div>
            <div className="shot-navigator-head">
              <b>镜头轨道</b>
              <div className="shot-navigator-actions" aria-live="polite">
                <span>{visibleShots.length ? selectedIndex + 1 : 0} / {visibleShots.length}，共 {shots.filter(isStoryboardProblemShot).length} 个问题镜</span>
                <button type="button" aria-label="上一镜" disabled={selectedIndex <= 0} onClick={() => selectRelative(-1)}>←</button>
                <button type="button" aria-label="下一镜" disabled={selectedIndex >= visibleShots.length - 1} onClick={() => selectRelative(1)}>→</button>
              </div>
            </div>
            <div ref={timelineRef} className="shot-navigator-list" role="listbox" aria-label="镜头列表" tabIndex={0}>
              {visibleShots.map(shot => (
                <button key={shot.id} type="button" role="option" aria-selected={shot.id === selectedShot?.id}
                  aria-current={shot.id === selectedShot?.id} className={shot.id === selectedShot?.id ? 'active' : ''}
                  onClick={() => setSelectedShotId(shot.id)}>
                  <span className="shot-nav-top"><span className="shot-nav-no">镜 {String(shot.shot_no).padStart(2, '0')}</span><span>{shot.duration_s}s</span></span>
                  <span className="shot-nav-main"><b>{shot.shot_size} · {shot.camera_move}</b><small>{shot.scene_setting}</small></span>
                  <span className="shot-nav-badges">
                    {isStoryboardProblemShot(shot) && <i className="problem">需处理</i>}
                    {shot.is_final && <i>最终镜</i>}
                    {storyboardSpokenChars(shot) > (shot.spoken_limit ?? Infinity) && <i className="problem">口播超限</i>}
                  </span>
                </button>
              ))}
              {!visibleShots.length && <div className="shot-filter-empty">当前筛选下没有镜头</div>}
            </div>
          </section>

          <section className="shot-editor-pane">
            {selectedShot && (
              <ShotWorkspace key={`${selectedShot.id}:${selectedShot.storyboard_artifact_id ?? ''}`}
                shot={selectedShot} episode={ep} status={status}
                previous={shots[absoluteIndex - 1]} next={shots[absoluteIndex + 1]}
                onChanged={() => { void refresh() }} onSelect={setSelectedShotId}
                onStructure={previewStructure} disabled={busy} />
            )}
          </section>
        </div>
      )}</>}

      <Modal open={!!startPreview} title={startPreview?.action === 'resume' ? '继续生成预检' : '生成分镜预检'} onClose={closeStartPreview}
        actions={<><button className="btn" onClick={closeStartPreview}>取消</button><button className="btn primary" onClick={() => void submitStart()}>确认启动</button></>}>
        {startPreview && <div className="storyboard-preview-card">
          <dl><div><dt>剧本/分镜基线</dt><dd>已锁定当前版本</dd></div><div><dt>保留合格镜头</dt><dd>{startPreview.kept_validated_shots} 镜</dd></div>
            <div><dt>计划 / 剩余</dt><dd>{startPreview.planned_shots ?? '待规划'} / {startPreview.remaining_shots ?? '待规划'} 镜</dd></div>
            <div><dt>恢复起点</dt><dd>第 {startPreview.checkpoint.resume_from_shot} 镜</dd></div>
            <div><dt>预计等待</dt><dd>{startPreview.estimated_wait_minutes.join('–')} 分钟</dd></div><div><dt>费用</dt><dd>{startPreview.estimated_cost_cny == null ? '文本调用按实结算' : `¥${startPreview.estimated_cost_cny}`}</dd></div></dl>
          <p>{startPreview.impact}</p><small>{startPreview.estimate_note}</small>
          <label className="auto-confirm-setting"><input type="checkbox" checked={autoConfirm} onChange={event => setAutoConfirm(event.target.checked)} />
            <span><b>全量门禁通过后自动确认分镜</b><small>不会自动提交任何付费视频；遇到警告会停下转人工。</small></span></label>
        </div>}
      </Modal>

      <Modal open={!!confirmPreview} title={confirmPreview?.hard_gates.passed ? '确认分镜前完整预览' : '暂不能确认分镜'} onClose={() => setConfirmPreview(null)}
        actions={<><button className="btn" onClick={() => setConfirmPreview(null)}>返回审阅</button>{confirmPreview?.hard_gates.passed && confirmPreview.preview_token && <button className="btn primary" onClick={() => void confirmStoryboard()}>批准并确认分镜</button>}</>}>
        {confirmPreview && <div className="storyboard-preview-card">
          <dl><div><dt>当前版本</dt><dd>{confirmPreview.storyboard_artifact_id || '待定稿'}</dd></div><div><dt>镜头完整性</dt><dd>{confirmPreview.shot_count}/{confirmPreview.planned_shots}</dd></div>
            <div><dt>总时长</dt><dd>{confirmPreview.total_duration_s}s</dd></div><div><dt>最终镜</dt><dd>{confirmPreview.final_shot_valid ? '有效' : '缺失'}</dd></div>
            <div><dt>硬门禁</dt><dd>{confirmPreview.hard_gates.passed ? '全部通过' : '未通过'}</dd></div><div><dt>预计视频成本</dt><dd>¥{confirmPreview.estimated_video_cost_cny.min}–¥{confirmPreview.estimated_video_cost_cny.max}</dd></div></dl>
          {!!confirmPreview.warnings.length && <div className="warning-banner">{confirmPreview.warnings.join('；')}</div>}
          {!!confirmPreview.hard_gates.errors.length && <div className="error-banner" role="alert"><b>请先处理以下问题：</b><ul>{confirmPreview.hard_gates.errors.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul></div>}
          <p>确认后解锁：{confirmPreview.unlocks.join('、')}</p><small>{confirmPreview.estimated_video_cost_cny.note}</small>
        </div>}
      </Modal>

      <Modal open={cancelOpen} title="取消本次分镜生成？" onClose={() => setCancelOpen(false)}
        actions={<><button className="btn" onClick={() => setCancelOpen(false)}>继续生成</button><button className="btn danger" onClick={() => {
          setCancelOpen(false); void run(() => api.post(`/episodes/${ep.id}/storyboard/cancel`, { idempotency_key: `cancel-${ep.active_storyboard_run_id || ep.id}` }), '已取消；已通过镜头和恢复点会保留')
        }}>确认取消</button></>}>
        <p>取消会停止当前长任务；已经通过的逐镜产物会保留，可从下一安全检查点恢复。尚未落库的当前一步无法恢复。</p>
      </Modal>

      <ImpactDialog open={!!structurePreview} title="镜头结构调整影响预览" impact={structurePreview}
        knownEffects={structurePreview ? [
          `镜头数：${structurePreview.before_count} → ${structurePreview.after_count}`,
          `将重排 ${structurePreview.renumbered_shots} 个镜号`,
          `相邻重验：${structurePreview.revalidation_shots.map(no => `镜 ${no}`).join('、')}`,
          structurePreview.final_shot_impact,
        ] : []}
        confirmLabel="批准并调整结构" onClose={() => setStructurePreview(null)} onConfirm={() => void applyStructure()} />
    </>
  )
}

function ShotWorkspace({ shot, episode, status, previous, next, onChanged, onSelect, onStructure, disabled }: {
  shot: Shot; episode: Episode; status: StoryboardStatus; previous?: Shot; next?: Shot
  onChanged: () => void; onSelect: (id: string) => void
  onStructure: (operation: StructurePreview['operation'], targetIndex?: number) => Promise<void>
  disabled: boolean
}) {
  const { toast } = useNav()
  const [edit, setEdit] = useState<Shot | null>(null)
  const [baseline, setBaseline] = useState<Shot | null>(null)
  const [session, setSession] = useState<EditSession | null>(null)
  const [impact, setImpact] = useState<(ImpactSummary & { preview_token: string; normalized_changes: Record<string, unknown> }) | null>(null)
  const [impactLoading, setImpactLoading] = useState(false)
  const [impactError, setImpactError] = useState<string | null>(null)
  const [saveErrors, setSaveErrors] = useState<string[]>([])
  const [drafts, setDrafts] = useState<DraftItem[]>([])
  const [sourceChapters, setSourceChapters] = useState<SourceChapter[]>([])
  const [sourceChapterId, setSourceChapterId] = useState<number | null>(shot.source_binding?.chapter_id ?? null)
  const [sourceBinding, setSourceBinding] = useState<SourceBindingInput | null>(null)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [detailTab, setDetailTab] = useState<'frames' | 'script'>('frames')
  const [deletedDialogue, setDeletedDialogue] = useState<{ value: Shot['dialogues'][number]; index: number } | null>(null)
  const [conflictOpen, setConflictOpen] = useState(false)
  const [discardDraftId, setDiscardDraftId] = useState<string | null>(null)
  const sourceTextRef = useRef<HTMLTextAreaElement>(null)
  const current = edit ?? shot
  const changes = edit && baseline ? buildStoryboardChanges(baseline, edit, sourceBinding) : {}
  const dirty = Object.keys(changes).length > 0
  const currentChars = storyboardSpokenChars(current)
  const spokenLimit = current.spoken_limit ?? Math.max(1, current.duration_s * 5)
  const overCapacity = currentChars > spokenLimit
  const prevConflict = Boolean(edit && previous?.state_out?.trim() && edit.state_in?.trim() && previous.state_out.trim() !== edit.state_in.trim())
  const nextConflict = Boolean(edit && next?.state_in?.trim() && edit.state_out?.trim() && next.state_in.trim() !== edit.state_out.trim())
  const structureEditEnabled = status.feature_flags?.structure_edit !== false
  const actionBlockReason = !structureEditEnabled
    ? '镜头结构编辑已由管理员关闭'
    : status.write_block_reason || '当前状态暂不可修改或删除，请先刷新状态'

  const characterOptions = useMemo(() => [...new Set([
    ...(episode.shots ?? []).flatMap(item => item.characters ?? []),
    ...(episode.screenplay?.scene_outline ?? []).flatMap(scene => scene.characters ?? []),
  ].filter(Boolean))], [episode.shots, episode.screenplay])
  const informationOptions = useMemo(() => {
    const ledger = episode.screenplay?.information_ledger ?? []
    return ledger.map((item, index) => {
      const value = item as Record<string, unknown>
      return {
        id: String(value.info_id ?? value.id ?? `I${index + 1}`),
        label: String(value.content ?? value.title ?? value.summary ?? `信息点 ${index + 1}`),
      }
    })
  }, [episode.screenplay])

  const loadDrafts = async () => {
    try {
      const result = await api.get(`/shots/${shot.id}/drafts`) as { items: DraftItem[] }
      setDrafts(result.items ?? [])
    } catch { setDrafts([]) }
  }

  const beginEdit = async () => {
    try {
      const [editSession, sourceResult] = await Promise.all([
        api.post(`/shots/${shot.id}/edit-session`) as Promise<EditSession>,
        api.get(`/episodes/${episode.id}/storyboard/source`) as Promise<{ chapters: SourceChapter[] }>,
      ])
      setSession(editSession)
      setBaseline(cloneShot(shot))
      setEdit(cloneShot(shot))
      setSourceChapters(sourceResult.chapters ?? [])
      setSourceChapterId(shot.source_binding?.chapter_id ?? sourceResult.chapters?.[0]?.id ?? null)
      setSourceBinding(null)
      setSaveErrors([])
      await loadDrafts()
    } catch (caught) {
      toast((caught as Error).message, true)
    }
  }

  const previewSave = async () => {
    if (!session || !edit || !dirty) return
    setImpactLoading(true); setImpactError(null)
    try {
      const result = await api.post(`/shots/${shot.id}/impact-preview`, {
        edit_session_token: session.edit_session_token,
        changes,
      }) as ImpactSummary & { unchanged?: boolean; preview_token?: string; normalized_changes?: Record<string, unknown> }
      if (result.unchanged || !result.preview_token) {
        toast('没有实际变化，不会创建新版本')
        return
      }
      setImpact(result as ImpactSummary & { preview_token: string; normalized_changes: Record<string, unknown> })
    } catch (caught) {
      const error = caught as ApiError
      setImpactError(error.message)
      if (error.status === 409) setSaveErrors(['编辑期间出现新版本，发布已冻结。可复制当前草稿后重新载入最新版。'])
    } finally { setImpactLoading(false) }
  }

  const save = async () => {
    if (!session || !edit || !impact) return
    const approved = impact
    setImpact(null)
    try {
      const payload: Record<string, unknown> = {
        ...Object.fromEntries(Object.entries(changes).filter(([key]) => key !== 'source_binding')),
        expected_version: session.baseline_artifact_id ?? undefined,
        baseline_content_hash: session.baseline_content_hash,
        edit_session_token: session.edit_session_token,
        preview_token: approved.preview_token,
        change_source: 'standard_edit',
      }
      if (sourceBinding) payload.source_binding = sourceBinding
      const result = await api.put(`/shots/${shot.id}`, payload) as { unchanged?: boolean; impact?: ImpactSummary }
      toast(result.unchanged ? '内容未变化，未创建版本' : `镜 ${shot.shot_no} 已保存并完成影响传播`)
      setEdit(null); setBaseline(null); setSession(null); setSaveErrors([]); onChanged()
    } catch (caught) {
      const error = caught as ApiError
      const detail = error.detail as { issues?: string[]; checkpoint_preserved?: boolean } | undefined
      setSaveErrors(detail?.issues?.length ? detail.issues : [error.message])
      if (detail?.checkpoint_preserved) toast('保存未通过；输入已保留为失败草稿，发布版未改变', true)
      await loadDrafts()
    }
  }

  const discard = () => {
    setEdit(null); setBaseline(null); setSession(null); setSaveErrors([]); setSourceBinding(null); setDeletedDialogue(null)
  }

  const bindSelectedSource = () => {
    if (!edit || sourceChapterId == null) return
    const chapter = sourceChapters.find(item => item.id === sourceChapterId)
    const textarea = sourceTextRef.current
    if (!chapter || !textarea || textarea.selectionEnd <= textarea.selectionStart) {
      toast('请先在本集原文中拖选一个连续片段', true); return
    }
    const selected = chapter.content.slice(textarea.selectionStart, textarea.selectionEnd)
    setEdit({ ...edit, source_excerpt: selected })
    setSourceBinding({
      chapter_id: chapter.id, source_version_hash: chapter.source_version_hash,
      start_offset: textarea.selectionStart, end_offset: textarea.selectionEnd,
    })
  }

  const toggleCharacter = (name: string) => {
    if (!edit) return
    const active = edit.characters.includes(name)
    const characters = active ? edit.characters.filter(item => item !== name) : [...edit.characters, name]
    setEdit({ ...edit, characters, characters_visible: characters })
  }

  const toggleInformation = (id: string) => {
    if (!edit) return
    const values = edit.new_information_ids ?? []
    setEdit({ ...edit, new_information_ids: values.includes(id) ? values.filter(item => item !== id) : [...values, id] })
  }

  const resolveConflict = async (choice: 'rebuild_timeline_from_dialogues' | 'rebuild_dialogues_from_timeline') => {
    try {
      const preview = await api.post(`/shots/${shot.id}/spoken-conflict-preview`, { choice }) as {
        preview_token: string; edit_session_token: string; baseline_content_hash: string
      }
      await api.post(`/shots/${shot.id}/resolve-spoken-conflict`, {
        choice, invalidate_media: true, preview_token: preview.preview_token,
        edit_session_token: preview.edit_session_token,
        baseline_content_hash: preview.baseline_content_hash,
      })
      toast(choice === 'rebuild_timeline_from_dialogues' ? '已按台词重建时间轴' : '已按时间轴重建台词')
      setConflictOpen(false); discard(); onChanged()
    } catch (caught) { toast((caught as Error).message, true) }
  }

  return (
    <article className={`shot-strip ${edit ? 'editing' : 'reviewing'}`}>
      <header className="shot-head">
        <div className="shot-head-copy"><span className="sn">镜{String(shot.shot_no).padStart(2, '0')}</span>
          <span className="meta">{current.duration_s}s · {current.shot_size} · {current.camera_move} · {current.transition}</span>
          <span className="meta shot-characters">{current.characters.join(' / ') || '缺角色（需修改）'}</span>
          {isStoryboardProblemShot(shot) && <span className="shot-badge status-needs_revision">需处理</span>}
          {shot.is_final && <span className="shot-badge gate">最终镜</span>}
        </div>
        <div className="shot-head-actions">
          {shot.spoken_contract_status === 'conflict' && <button className="btn small ghost" onClick={() => setConflictOpen(true)}>解决口播冲突</button>}
          {!edit && !status.editable && <span className="shot-actions-locked" role="status">{actionBlockReason}</span>}
          {!edit ? <>
            <button className="btn small danger shot-delete-action" disabled={disabled || !status.editable || !structureEditEnabled}
              title={!status.editable || !structureEditEnabled ? actionBlockReason : '删除前会展示镜号重排、相邻重验和媒体失效影响'}
              onClick={() => void onStructure('delete')}>删除镜头</button>
            <button className="btn small" disabled={disabled || !status.editable} title={status.write_block_reason || undefined} onClick={() => void beginEdit()}>修改</button>
          </> : <>
            <button className="btn small primary" disabled={!dirty || overCapacity || !edit.characters.length} onClick={() => void previewSave()}>保存前预览</button>
            <button className="btn small ghost" onClick={discard}>放弃</button>
          </>}
        </div>
      </header>

      {conflictOpen && <div className="shot-conflict-panel" role="region" aria-label="口播冲突修复">
        <p>台词与高级时间轴分别发生了变化。选择前会先计算视频、成片与重新确认影响。</p>
        <div className="shot-conflict-actions"><button className="btn small primary" onClick={() => void resolveConflict('rebuild_timeline_from_dialogues')}>以台词为准</button>
          <button className="btn small" onClick={() => void resolveConflict('rebuild_dialogues_from_timeline')}>以时间轴为准</button><button className="btn small ghost" onClick={() => setConflictOpen(false)}>取消</button></div>
      </div>}

      {!!saveErrors.length && <div className="shot-validation-summary" role="alert"><b>修改尚未通过，共 {saveErrors.length} 个问题</b><ul>{saveErrors.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul>
        <button className="btn small ghost" onClick={() => void navigator.clipboard?.writeText(JSON.stringify(edit, null, 2))}>复制我的草稿</button>
        <button className="btn small ghost" onClick={() => { discard(); onChanged() }}>重新载入最新版</button></div>}

      {!!drafts.length && edit && <details className="shot-drafts"><summary>失败草稿（{drafts.length}）</summary>{drafts.map(draft => <div key={draft.id} className="shot-draft-row">
        <div><b>{new Date(draft.created_at * 1000).toLocaleString()}</b><small>基线 {draft.baseline_artifact_ids[0] || '旧版本'} · {draft.issues[0] || '业务校验未通过'}</small></div>
        <button className="btn small" onClick={() => { setEdit({ ...cloneShot(shot), ...draft.content } as Shot); setSaveErrors(draft.issues) }}>继续编辑</button>
        <button className="btn small ghost" onClick={() => setDiscardDraftId(draft.id)}>丢弃</button>
      </div>)}</details>}

      {edit ? (
        <div className="shot-edit-form">
          <section aria-labelledby={fieldId(shot.id, 'basic-title')}>
            <h3 id={fieldId(shot.id, 'basic-title')}>常用修改</h3>
            <div className="shot-edit-grid">
              <label htmlFor={fieldId(shot.id, 'duration')}>时长<select id={fieldId(shot.id, 'duration')} value={edit.duration_s} onChange={event => setEdit({ ...edit, duration_s: Number(event.target.value) })}>{DURATIONS.map(value => <option key={value} value={value}>{value}s</option>)}</select><small>{edit.duration_s > 5 ? '保存后进入 AI/确定性审核，结果通过前不会发布' : '标准时长'}</small></label>
              <label htmlFor={fieldId(shot.id, 'size')}>景别<select id={fieldId(shot.id, 'size')} value={edit.shot_size} onChange={event => setEdit({ ...edit, shot_size: event.target.value })}>{SIZES.map(value => <option key={value}>{value}</option>)}</select></label>
              <label htmlFor={fieldId(shot.id, 'move')}>运镜<select id={fieldId(shot.id, 'move')} value={edit.camera_move} onChange={event => setEdit({ ...edit, camera_move: event.target.value })}>{MOVES.map(value => <option key={value}>{value}</option>)}</select></label>
              <label htmlFor={fieldId(shot.id, 'transition')}>转场<select id={fieldId(shot.id, 'transition')} value={edit.transition} onChange={event => setEdit({ ...edit, transition: event.target.value })}>{TRANSITIONS.map(value => <option key={value}>{value}</option>)}</select></label>
            </div>
            <label htmlFor={fieldId(shot.id, 'scene')}>场景标签<input id={fieldId(shot.id, 'scene')} value={edit.scene_setting} required onChange={event => setEdit({ ...edit, scene_setting: event.target.value })} /></label>
            <label htmlFor={fieldId(shot.id, 'action')}>画面与动作<textarea id={fieldId(shot.id, 'action')} rows={3} value={edit.action_desc} required onChange={event => setEdit({ ...edit, action_desc: event.target.value })} /></label>
            <div className="shot-edit-grid frames"><label htmlFor={fieldId(shot.id, 'first')}>首帧画面<textarea id={fieldId(shot.id, 'first')} rows={2} value={edit.first_frame_desc} onChange={event => setEdit({ ...edit, first_frame_desc: event.target.value })} /></label>
              <label htmlFor={fieldId(shot.id, 'last')}>尾帧画面<textarea id={fieldId(shot.id, 'last')} rows={2} value={edit.last_frame_desc} onChange={event => setEdit({ ...edit, last_frame_desc: event.target.value })} /></label></div>
          </section>

          <section><h3>画面角色与信息点</h3><fieldset className="token-picker"><legend>画面角色（人物谱）</legend>{characterOptions.map(name => <label key={name}><input type="checkbox" checked={edit.characters.includes(name)} onChange={() => toggleCharacter(name)} />{name}</label>)}{!characterOptions.length && <small>人物谱暂无可选角色</small>}</fieldset>
            {!edit.characters.length && <p className="field-error" role="alert">至少选择一个画面角色；“缺角色”会从这里修复。</p>}
            <fieldset className="token-picker"><legend>声音角色</legend>{characterOptions.map(name => <label key={name}><input type="checkbox" checked={(edit.audio_cast ?? []).includes(name)} onChange={() => {
              const active = (edit.audio_cast ?? []).includes(name); setEdit({ ...edit, audio_cast: active ? (edit.audio_cast ?? []).filter(item => item !== name) : [...(edit.audio_cast ?? []), name] })
            }} />{name}</label>)}</fieldset>
            <fieldset className="token-picker"><legend>本镜新信息（来自剧本清单）</legend>{informationOptions.map(item => <label key={item.id} title={`存储键：${item.id}`}><input type="checkbox" checked={(edit.new_information_ids ?? []).includes(item.id)} onChange={() => toggleInformation(item.id)} />{item.label}</label>)}{!informationOptions.length && <small>本集剧本没有结构化信息清单</small>}</fieldset>
          </section>

          <section aria-labelledby={fieldId(shot.id, 'dialogue-title')}><div className="section-heading-row"><h3 id={fieldId(shot.id, 'dialogue-title')}>台词</h3><span className={overCapacity ? 'capacity-count over' : 'capacity-count'} aria-live="polite">{currentChars} / {spokenLimit} 字</span></div>
            {overCapacity && <p id={fieldId(shot.id, 'capacity-error')} className="field-error" role="alert">口播超出容量 {currentChars - spokenLimit} 字，请删减台词或调整镜头。</p>}
            {edit.dialogues.map((dialogue, index) => <div key={index} className="dlg-line">
              <label><span>说话人 {index + 1}</span><select value={dialogue.speaker} onChange={event => { const values = [...edit.dialogues]; values[index] = { ...dialogue, speaker: event.target.value }; setEdit({ ...edit, dialogues: values }) }}><option value="">请选择</option>{characterOptions.map(name => <option key={name}>{name}</option>)}</select></label>
              <label className="dialogue-copy"><span>台词 {index + 1}</span><input value={dialogue.line} aria-describedby={overCapacity ? fieldId(shot.id, 'capacity-error') : undefined} onChange={event => { const values = [...edit.dialogues]; values[index] = { ...dialogue, line: event.target.value }; setEdit({ ...edit, dialogues: values }) }} /></label>
              <label><span>情绪 {index + 1}</span><input value={dialogue.emotion} onChange={event => { const values = [...edit.dialogues]; values[index] = { ...dialogue, emotion: event.target.value }; setEdit({ ...edit, dialogues: values }) }} /></label>
              <button className="btn small ghost" aria-label={`删除第 ${index + 1} 条台词`} onClick={() => { setDeletedDialogue({ value: dialogue, index }); setEdit({ ...edit, dialogues: edit.dialogues.filter((_, itemIndex) => itemIndex !== index) }) }}>删除</button>
            </div>)}
            <div className="dialogue-actions"><button className="btn small" onClick={() => setEdit({ ...edit, dialogues: [...edit.dialogues, { speaker: edit.characters[0] || '', line: '', emotion: '平静' }] })}>+ 加一句</button>
              {deletedDialogue && <button className="btn small ghost" onClick={() => { const values = [...edit.dialogues]; values.splice(deletedDialogue.index, 0, deletedDialogue.value); setEdit({ ...edit, dialogues: values }); setDeletedDialogue(null) }}>撤销删除</button>}</div>
          </section>

          <section><h3>审计原文（只从本集授权原文框选，不送视频模型）</h3>
            <p className="source-bound-preview">当前证据：{edit.source_excerpt || '尚未绑定'} {shot.source_binding && !sourceBinding ? `· 第 ${shot.source_binding.chapter_idx} 章 ${shot.source_binding.start_offset}–${shot.source_binding.end_offset}` : ''}</p>
            {sourceChapters.length ? <><label htmlFor={fieldId(shot.id, 'source-chapter')}>授权章节<select id={fieldId(shot.id, 'source-chapter')} value={sourceChapterId ?? ''} onChange={event => setSourceChapterId(Number(event.target.value))}>{sourceChapters.map(chapter => <option key={chapter.id} value={chapter.id}>第 {chapter.idx} 章 · {chapter.title}</option>)}</select></label>
              <label htmlFor={fieldId(shot.id, 'source-text')}>在原文中拖选连续片段<textarea ref={sourceTextRef} id={fieldId(shot.id, 'source-text')} rows={7} readOnly value={sourceChapters.find(item => item.id === sourceChapterId)?.content ?? ''} /></label>
              <button className="btn small" onClick={bindSelectedSource}>使用当前选区作为证据</button></> : <p className="field-error">本集原文暂不可用；现有证据只读，不能修改。</p>}
          </section>

          <details open={advancedOpen} onToggle={event => setAdvancedOpen(event.currentTarget.open)} className="shot-advanced"><summary>高级：连续性与合同信息</summary>
            <div className="neighbor-continuity" aria-label="邻镜连续性对照">
              <button type="button" disabled={!previous} onClick={() => previous && onSelect(previous.id)}><b>{previous ? `前镜 ${previous.shot_no} · 离开` : '首镜边界'}</b><span>{previous?.state_out || previous?.last_frame_desc || '无前镜依赖'}</span></button>
              <div className={prevConflict || nextConflict ? 'current conflict' : 'current'}><b>本镜 {shot.shot_no}</b><span>{edit.state_in || '未设进入'} → {edit.state_out || '未设离开'}</span></div>
              <button type="button" disabled={!next} onClick={() => next && onSelect(next.id)}><b>{next ? `后镜 ${next.shot_no} · 进入` : '末镜边界'}</b><span>{next?.state_in || next?.first_frame_desc || '无后镜依赖'}</span></button>
            </div>
            {(prevConflict || nextConflict) && <p className="field-error" role="alert">{prevConflict ? '前镜离开与本镜进入不一致。' : ''}{nextConflict ? '本镜离开与后镜进入不一致。' : ''} 可点击邻镜定位。</p>}
            <div className="shot-edit-grid continuity"><label htmlFor={fieldId(shot.id, 'state-in')}>进入状态<textarea id={fieldId(shot.id, 'state-in')} value={edit.state_in ?? ''} onChange={event => setEdit({ ...edit, state_in: event.target.value })} /></label>
              <label htmlFor={fieldId(shot.id, 'primary-action')}>镜头动作<textarea id={fieldId(shot.id, 'primary-action')} value={edit.primary_action ?? ''} onChange={event => setEdit({ ...edit, primary_action: event.target.value })} /></label>
              <label htmlFor={fieldId(shot.id, 'state-out')}>离开状态<textarea id={fieldId(shot.id, 'state-out')} value={edit.state_out ?? ''} onChange={event => setEdit({ ...edit, state_out: event.target.value })} /></label></div>
            <label htmlFor={fieldId(shot.id, 'continuity-mode')}>与上镜关系<select id={fieldId(shot.id, 'continuity-mode')} value={edit.continuity_mode ?? ''} onChange={event => setEdit({ ...edit, continuity_mode: event.target.value })}><option value="">自动/未设定</option>{Object.entries(CONTINUITY_MODES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          </details>

          {status.feature_flags?.structure_edit !== false && <section className="shot-structure-actions"><h3>镜头结构</h3><p>所有结构动作都会先展示镜号、最终镜、相邻重验和媒体失效影响。</p><div>
            <button className="btn small" onClick={() => void onStructure('add_after')}>在当前镜后新增</button><button className="btn small" onClick={() => void onStructure('duplicate_after')}>复制为下一镜</button>
            <button className="btn small" disabled={!previous} onClick={() => void onStructure('move', Math.max(0, shot.shot_no - 2))}>前移</button><button className="btn small" disabled={!next} onClick={() => void onStructure('move', shot.shot_no)}>后移</button>
            <button className="btn small danger" onClick={() => void onStructure('delete')}>删除镜头</button></div></section>}

          <footer className="shot-edit-sticky"><span aria-live="polite">{dirty ? `${Object.keys(changes).length} 个字段域有变化` : '尚无改动'}</span><button className="btn ghost" onClick={discard}>放弃</button><button className="btn primary" disabled={!dirty || overCapacity || !edit.characters.length} onClick={() => void previewSave()}>保存前预览</button></footer>
        </div>
      ) : (
        <div className="shot-review-body">
          <section className="shot-overview"><div className="shot-visual-brief"><span className="shot-section-label">画面设计</span><h2>{current.scene_setting}</h2><p>{current.action_desc}</p></div>
            <dl className="shot-specs"><div><dt>时长</dt><dd>{current.duration_s}s</dd></div><div><dt>景别</dt><dd>{current.shot_size}</dd></div><div><dt>运镜</dt><dd>{current.camera_move}</dd></div><div><dt>转场</dt><dd>{current.transition}</dd></div></dl></section>
          <div className="shot-frame-pair shot-continuity-chain" aria-label="镜头状态链"><div className="shot-frame-card"><b>进入状态</b><p>{current.state_in || current.first_frame_desc || '未设置'}</p></div><span aria-hidden>→</span><div className="shot-frame-card"><b>镜头动作</b><p>{current.primary_action || current.action_desc}</p></div><span aria-hidden>→</span><div className="shot-frame-card"><b>离开状态</b><p>{current.state_out || current.last_frame_desc || '未设置'}</p></div></div>
          <section className="shot-spoken-panel"><header className="shot-context-head"><b>本镜台词</b><span>{currentChars} / {spokenLimit} 字</span></header>{overCapacity && <p className="shot-spoken-warn">口播已超出本镜容量</p>}{current.dialogues.length ? current.dialogues.map((line, index) => <div key={index} className="shot-audio-line"><b>{line.speaker}<small>{line.emotion}</small></b><p>「{line.line}」</p></div>) : <p>本镜无台词</p>}</section>
          <section className="shot-context-panel"><h3>镜头要素</h3><dl className="shot-context-grid"><div><dt>画面角色</dt><dd>{current.characters.join('、') || '无'}</dd></div><div><dt>声音角色</dt><dd>{current.audio_cast?.join('、') || '无'}</dd></div><div><dt>本镜新信息</dt><dd>{current.new_information_items?.map(item => item.content).join('；') || '无'}</dd></div></dl></section>
          <div className="shot-detail-tabs" role="tablist" aria-label="镜头详情"><button role="tab" aria-selected={detailTab === 'frames'} className={detailTab === 'frames' ? 'active' : ''} onClick={() => setDetailTab('frames')}>起止画面</button><button role="tab" aria-selected={detailTab === 'script'} className={detailTab === 'script' ? 'active' : ''} onClick={() => setDetailTab('script')}>声音与原文</button></div>
          {detailTab === 'frames' ? <div className="shot-frame-pair" role="tabpanel"><div className="shot-frame-card"><b>01 · 首帧</b><p>{current.first_frame_desc || '暂未描述'}</p></div><span aria-hidden>→</span><div className="shot-frame-card"><b>02 · 尾帧</b><p>{current.last_frame_desc || '暂未描述'}</p></div></div>
            : <div className="shot-script-grid" role="tabpanel"><div className="shot-script-copy"><b>审计原文（不送视频模型）</b><p>{current.source_excerpt || '暂无对应原文'}</p><small>{current.source_binding ? `已验证 · 第 ${current.source_binding.chapter_idx} 章 ${current.source_binding.start_offset}–${current.source_binding.end_offset}` : '历史证据尚未绑定位置，确认前将严格校验'}</small></div><div className="shot-audio-copy">{current.dialogues.map((line, index) => <div key={index} className="shot-audio-line"><b>{line.speaker}</b><p>「{line.line}」</p></div>)}</div></div>}
        </div>
      )}

      <ImpactDialog open={!!impact || impactLoading || !!impactError} title={`保存镜 ${shot.shot_no} 的精确影响`}
        impact={impact} loading={impactLoading} error={impactError} confirmLabel="批准影响并保存"
        knownEffects={impact ? [`重验镜头：${((impact as unknown as { revalidation_shots?: number[] }).revalidation_shots ?? []).join('、') || '本镜与相邻镜'}`, '失败时只保留工作草稿，不覆盖发布版'] : []}
        onClose={() => { setImpact(null); setImpactError(null); setImpactLoading(false) }} onConfirm={() => void save()} />
      <Modal open={!!discardDraftId} title="丢弃失败草稿？" onClose={() => setDiscardDraftId(null)}
        actions={<><button className="btn" onClick={() => setDiscardDraftId(null)}>保留</button><button className="btn danger" onClick={async () => {
          const draftId = discardDraftId; setDiscardDraftId(null)
          if (draftId) { await api.del(`/shots/${shot.id}/drafts/${draftId}`); await loadDrafts() }
        }}>确认丢弃</button></>}>
        <p>只会删除该工作草稿，当前发布版和下游不会改变。</p>
      </Modal>
    </article>
  )
}
