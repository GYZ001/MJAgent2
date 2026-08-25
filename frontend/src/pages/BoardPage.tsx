import { ReactNode, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError, Episode, Shot, StoryboardStatus } from '../api'
import { useEpisode, useNav } from '../App'
import EpisodeCrumb from '../components/EpisodeCrumb'
import { ItemTaskTimer, ServerTaskTimer } from '../components/TaskTimer'
import DecisionDialog from '../components/DecisionDialog'
import ImpactDialog, { ImpactSummary } from '../components/harness/ImpactDialog'
import QueryState from '../components/QueryState'
import { useFocusTrap } from '../hooks/useFocusTrap'
import { storyboardTaskNotice } from '../lib/productionNotices'
import "../styles/BoardPage.css";

const SIZES = ['远景', '全景', '中景', '近景', '特写']
const MOVES = ['固定', '推近', '拉远', '横摇', '跟随']
const TRANSITIONS = ['硬切', '叠化', '淡出淡入', '黑场', '闪黑', '闪白', '甩镜', '遮挡转场', '匹配剪辑', '声音延续+叠化', '声音先行+淡入']
const DURATIONS = [5, 6, 7, 8, 9, 10]
const SCENE_TIMES = ['清晨', '早晨', '上午', '中午', '午后', '傍晚', '黄昏', '夜晚', '深夜']
const CONTINUITY_MODES: Record<string, string> = {
  action_continuation: '动作延续', same_scene_cut: '同场切换', reaction_cut: '反应镜头',
  reverse_angle: '反打', insert_detail: '细节插入', scene_change: '转场换景',
}

const EDITABLE_FIELDS: Array<keyof Shot> = [
  'duration_s', 'shot_size', 'camera_angle', 'camera_move', 'scene_time', 'scene_name', 'characters', 'action_desc',
  'first_frame_desc', 'last_frame_desc', 'dialogues', 'transition', 'continuity_from_prev',
  'continuity_mode', 'state_in', 'primary_action', 'state_out', 'characters_visible',
  'audio_cast', 'new_information_ids', 'spine_beat_ids', 'key_line_ids',
  'purpose', 'resulting_change', 'readability_focus', 'camera_motivation',
  'repeat_of_shot_id', 'repeat_gain',
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

export type StartPreview = {
  preview_token: string
  action: 'create' | 'resume'
  resume_mode?: 'create' | 'continue_generation' | 'repair_existing' | 'finalize_evidence' | null
  kept_validated_shots: number
  planned_shots?: number | null
  remaining_shots?: number | null
  checkpoint: { available: boolean; phase?: string | null; resume_from_shot: number }
  can_start?: boolean
  blocking_reason?: string | null
  current_gate_issue_count?: number
  current_gate_issues?: string[]
  warning?: string | null
  repair?: {
    lifetime_repair_count: number
    activation_no: number
    activation_attempt_count: number
    max_attempts_per_activation: number
    external_calls: number
    cache_reuses: number
    candidate_preserves_official_shots: boolean
    last_issue_messages: string[]
  }
}

export function storyboardCharacterFilterOptions(shots: Shot[]): string[] {
  const internalIdentity = /^(?:character|entity|speaker|voice|passerby)[-_]/i
  const internalSlug = /^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+$/i
  return [...new Set(
    shots
      .flatMap(shot => [...(shot.characters ?? []), ...(shot.audio_cast ?? [])])
      .map(value => value.trim())
      .filter(value => value && !internalIdentity.test(value) && !internalSlug.test(value)),
  )]
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
  recovery_action?: string | null
}

type DraftItem = {
  id: string; version: number; content: Partial<Shot>; baseline_artifact_ids: string[]
  issues: string[]; created_at: number
}

type SpokenConflictPreview = ImpactSummary & {
  preview_token: string
  edit_session_token: string
  baseline_content_hash: string
  choice: 'rebuild_timeline_from_dialogues' | 'rebuild_dialogues_from_timeline'
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

type StoryboardClearPreview = {
  preview_token: string
  shot_count: number
  video_version_count: number
  reference_asset_count: number
  workflow_run_count: number
  delivery_package_count: number
  active_task_will_stop: boolean
  screenplay_preserved: true
  irreversible: true
}

function cloneShot(shot: Shot): Shot {
  return JSON.parse(JSON.stringify(shot)) as Shot
}

export type StoryboardInputStrategy = {
  kind: 'scene_library' | 'previous_video_tail' | 'reference_video'
  label: string
  detail: string
}

export function storyboardInputStrategy(shot: Shot, previous?: Shot): StoryboardInputStrategy {
  if (shot.mode_plan?.mode === 'VIDEO_INPUT_MODE') {
    return {
      kind: 'reference_video',
      label: '参考视频（原逻辑）',
      detail: '继续按既有参考视频计划执行，不改变视频来源与依赖关系。',
    }
  }
  const scene = (shot.scene_name || shot.scene_setting || '').trim()
  const previousScene = (previous?.scene_name || previous?.scene_setting || '').trim()
  const sceneTime = (shot.scene_time || '').replace(/\s+/g, '')
  const previousSceneTime = (previous?.scene_time || '').replace(/\s+/g, '')
  if (previous && scene === previousScene && sceneTime === previousSceneTime) {
    return {
      kind: 'previous_video_tail',
      label: '上一视频真实尾帧 → 本镜唯一首帧',
      detail: '同场景自动承接上一条采用视频的真实尾帧，不单独生成剧情首帧或静态尾帧。',
    }
  }
  return {
    kind: 'scene_library',
    label: '人物谱 + 场景库',
    detail: '换场起点只使用人物谱与场景库中的图片，不额外生成剧情参考图。',
  }
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

// 口播容量上限的唯一口径，对齐后端 config.max_spoken_chars_for_duration：
// clamp(duration_s, 5, 10) * 18 // 5（3.6 字/秒，纯文字不计标点）。后端在所有含镜头的视图里
// 都会注入 spoken_limit（storyboard_ops.py:210），优先采信；仅当缺失时按同一公式兜底，
// 避免列表徽标与编辑器保存判定使用两套标准而互相矛盾。
export function shotSpokenLimit(shot: Pick<Shot, 'spoken_limit' | 'duration_s'>): number {
  if (typeof shot.spoken_limit === 'number' && Number.isFinite(shot.spoken_limit)) {
    return shot.spoken_limit
  }
  const duration = Math.min(Math.max(Math.trunc(shot.duration_s || 5), 5), 10)
  return Math.floor((duration * 18) / 5)
}

export function isStoryboardProblemShot(shot: Shot): boolean {
  const status = shot.storyboard_evidence?.status || ''
  return shot.spoken_contract_status === 'conflict'
    || Boolean(shot.legacy_unvalidated)
    || ['candidate', 'needs_revision', 'rejected', 'stale', 'superseded'].includes(status)
    || storyboardSpokenChars(shot) > shotSpokenLimit(shot)
    || Boolean(shot.preflight_errors?.length)
}

export function storyboardDeleteUsesFullClear(shots: Shot[], selectedShotId: string): boolean {
  return shots.length === 1 && shots[0]?.id === selectedShotId
}

export function storyboardToolbarActions(state: StoryboardStatus['state']): {
  pause: boolean
  clear: boolean
} {
  return {
    pause: state === 'running',
    clear: ['paused', 'failed', 'ready_to_confirm', 'confirmed'].includes(state),
  }
}

export function storyboardGateIssueLabel(message: string): string {
  return message
    .replace(/^shots\[\d+\]\(shot_no=(\d+)\)\./, '第 $1 镜：')
    .replace(/^shot_no=(\d+)\./, '第 $1 镜：')
    .replaceAll('action_desc', '画面动作')
    .replaceAll('primary_action', '镜头动作')
    .replaceAll('first_frame_desc', '生成起点')
    .replaceAll('last_frame_desc', '结束状态')
    .replaceAll('QA', '质检')
    .replaceAll('门禁', '必检项')
}

export function storyboardSaveDisabledReason(
  dirty: boolean,
  overCapacity: boolean,
  characterCount: number,
): string {
  if (!dirty) return '尚未修改任何内容'
  if (overCapacity) return '口播超出本镜容量，请删减台词或调整镜头'
  if (!characterCount) return '请至少选择一个画面角色'
  return ''
}

type StoryboardProgressCopy = {
  summary: string
  detail: string | null
}

export type StoryboardPrimaryAction = {
  intent: StoryboardStatus['recommended_action'] | 'activate_ai_one_watch'
  label: string
}

export function storyboardPrimaryAction(
  status: StoryboardStatus,
  calibration: Episode['narrative_calibration_summary'],
  review: Episode['narrative_review_summary'],
): StoryboardPrimaryAction {
  const finalizingEvidence = status.recommended_action === 'resume_storyboard'
    && status.resume_mode === 'finalize_evidence'
  if (
    finalizingEvidence
    && calibration?.status === 'needs_review'
    && review?.decision === 'pass'
  ) {
    return {
      intent: 'activate_ai_one_watch',
      label: '运行 AI 一次观看模拟',
    }
  }
  const labels: Record<StoryboardStatus['recommended_action'], string> = {
    go_screenplay: '先去剧本台',
    generate_storyboard: '开始分镜任务',
    view_progress: '查看任务详情',
    resume_storyboard: finalizingEvidence ? '完成发布证据' : '继续分镜任务',
    confirm_storyboard: '确认分镜',
    go_review_wall: '进入生成台',
    refresh_status: '状态同步中',
  }
  return {
    intent: status.recommended_action,
    label: labels[status.recommended_action],
  }
}

export function storyboardProgressCopy(status: StoryboardStatus): StoryboardProgressCopy {
  const working = status.draft_shots ?? status.produced_shots
  const validated = Math.min(working, status.safe_checkpoint_shots ?? status.validated_shots)
  const resumeFrom = status.resume_from_shot ?? Math.max(1, validated + 1)
  const target = status.planned_shots > 0 ? `${status.planned_shots} 镜` : '待确定'
  const summary = `目标 ${target} · 工作副本 ${working} 镜 · 已校验 ${validated} 镜`

  if (!['running', 'paused', 'failed'].includes(status.state)) return { summary, detail: null }
  const pending = status.pending_revalidation_shots ?? Math.max(0, working - validated)
  const gateIssueCount = status.hard_gate_issue_count ?? status.hard_gate_issues?.length ?? 0
  const repairsExisting = status.resume_mode === 'repair_existing'
    || (
      status.recommended_action === 'resume_storyboard'
      && status.final_shot_valid
      && pending === 0
      && gateIssueCount > 0
    )
  if (repairsExisting) {
    return {
      summary,
      detail: `当前 ${working} 镜已完成逐镜校验，但整集仍有 ${gateIssueCount} 个确认门禁问题。继续任务会重开整集修复，不是从第 ${resumeFrom} 镜续写；修复候选通过前不会覆盖现有镜头。`,
    }
  }
  if (status.resume_mode === 'finalize_evidence') {
    return {
      summary,
      detail: '镜头内容与整集硬门禁已通过；继续任务只会完成冷观众审读、校准校验和发布证据签发，不会改写现有镜头。',
    }
  }
  const finalDraftNote = status.final_shot_valid
    ? '工作副本中的收尾标记不代表整集已通过。'
    : ''
  if (pending > 0) {
    return {
      summary,
      detail: `第 ${validated + 1}–${working} 镜仍待校验；任务将从第 ${resumeFrom} 镜继续修复。人工确认前，轨道中的内容都只是工作副本。${finalDraftNote}`,
    }
  }
  return {
    summary,
    detail: `当前 ${validated} 镜已通过逐镜校验；任务将从第 ${resumeFrom} 镜继续。整集门禁通过并经人工确认后才会交给生成台。${finalDraftNote}`,
  }
}

export function storyboardEmptyCopy(status: StoryboardStatus): string {
  if (status.state === 'no_screenplay') return '尚无可用于分镜的剧本，请先去剧本台。'
  if (status.state === 'running') return '首批分镜正在生成与逐镜校验，通过后会自动展示。'
  if (status.state === 'failed') return '分镜任务未完成，尚无通过校验的工作镜头；请查看原因后继续处理。'
  if (status.state === 'paused') return '分镜任务已暂停，尚无通过校验的工作镜头。'
  return '剧本已就绪，尚未生成分镜。'
}

export function storyboardStartPreviewCopy(preview: StartPreview): {
  title: string
  confirmLabel: string
  summary: string
  detail: string
} {
  if (preview.action === 'create') {
    return {
      title: '开始本集分镜任务',
      confirmLabel: '开始任务',
      summary: '从空白开始生成本集分镜。',
      detail: preview.planned_shots
        ? `计划 ${preview.planned_shots} 镜。`
        : '任务启动后会先规划镜头数量。',
    }
  }
  if (preview.resume_mode === 'repair_existing') {
    const issueCount = preview.current_gate_issue_count
      ?? preview.current_gate_issues?.length
      ?? preview.repair?.last_issue_messages.length
      ?? 0
    return {
      title: '继续修复分镜',
      confirmLabel: '开始修复',
      summary: `现有 ${preview.kept_validated_shots} 镜保持不变，重新校验并修复${issueCount ? ` ${issueCount} 个` : ''}确认门禁问题。`,
      detail: `这是重开整集修复，不是从第 ${preview.checkpoint.resume_from_shot} 镜续写；候选通过前不会覆盖现有镜头。`,
    }
  }
  if (preview.resume_mode === 'finalize_evidence') {
    return {
      title: '完成分镜发布证据',
      confirmLabel: '继续审读发布',
      summary: `现有 ${preview.kept_validated_shots} 镜已完成结构与逐镜校验。`,
      detail: '将保留全部镜头，仅继续冷观众审读、校准校验与发布证据签发。',
    }
  }
  return {
    title: '继续分镜任务',
    confirmLabel: '继续任务',
    summary: `从第 ${preview.checkpoint.resume_from_shot} 镜继续；前 ${preview.kept_validated_shots} 镜已通过逐镜校验。`,
    detail: preview.planned_shots
      ? `计划 ${preview.planned_shots} 镜${preview.remaining_shots != null ? `，剩余 ${preview.remaining_shots} 镜` : ''}。`
      : '继续任务后会先恢复已有计划。',
  }
}

export function storyboardShotCheckpointLabel(
  shotNo: number,
  status: StoryboardStatus,
): { label: string; className: string; title: string } | null {
  if (!['running', 'paused', 'failed'].includes(status.state)) return null
  const draft = status.draft_shots ?? status.produced_shots
  const safe = Math.min(draft, status.safe_checkpoint_shots ?? status.validated_shots)
  if (shotNo <= safe) {
    return { label: '已校验', className: 'checkpoint-safe', title: '本轮已通过逐镜校验；整集仍需通过门禁并由人工确认' }
  }
  return { label: '待校验', className: 'checkpoint-pending', title: '仍在工作副本中，继续任务时可能更新' }
}

function fieldId(shotId: string, name: string): string {
  return `shot-${shotId}-${name}`
}

function Modal({ open, title, children, onClose, actions }: {
  open: boolean; title: string; children: ReactNode; onClose: () => void; actions: ReactNode
}) {
  const trapRef = useFocusTrap(open, onClose)
  const titleId = useId()
  if (!open) return null
  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog storyboard-modal" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h3 id={titleId} tabIndex={-1}>{title}</h3>
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
  const screenplayReady = status.screenplay_available

  return (
    <section className={`storyboard-launch card ${screenplayReady ? 'is-ready' : 'needs-screenplay'}`} aria-labelledby="storyboard-launch-title">
      <header className="storyboard-launch-head">
        <div className="storyboard-launch-status">
          <span className="storyboard-launch-mark" aria-hidden="true">镜</span>
          <div><b>{screenplayReady ? '剧本已就绪' : '等待剧本'}</b><small>{screenplayReady ? '分镜尚未生成' : '完成剧本后才能拆解镜头'}</small></div>
        </div>
      </header>
      <div className="storyboard-launch-action" aria-label="下一步">
        <h2 id="storyboard-launch-title">{screenplayReady ? '开始本集分镜任务' : '请先完成本集剧本'}</h2>
        <p>{screenplayReady
          ? `将按照《${episode.title}》当前定稿剧本生成分镜，生成后可逐镜审阅。`
          : '当前没有可用剧本，完成剧本后再返回这里开始。'}</p>
        <button id="storyboard-primary-action" type="button" className="btn primary storyboard-launch-button" disabled={busy}
          aria-label={busy ? '开始分镜任务，暂不可用：正在准备' : screenplayReady ? '开始分镜任务' : '先去剧本台'}
          onClick={onPrimary}>
          {busy ? '正在准备…' : screenplayReady ? '开始分镜任务' : '先去剧本台'}
        </button>
        {screenplayReady && <small>只生成分镜，不会自动提交付费视频。</small>}
      </div>
    </section>
  )
}

type CalibrationProtocol = {
  narrative_review_artifact_id: string
  audience_priors: Array<{
    audience_prior_id: string
    audience_description: string
    existing_observation_count: number
  }>
}

type FrozenHumanWatch = {
  freeze_artifact_id: string
  observation_id: string
  audience_prior_id: string
  target_contract: Array<{
    target_delta_id: string
    dimension: string
    description: string
  }>
}

async function participantHash(value: string) {
  const bytes = new TextEncoder().encode(value.trim())
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
}

type AiOneWatchSimulationResult = {
  message?: string
}

async function activateAiOneWatchSimulation(
  episodeId: string,
): Promise<AiOneWatchSimulationResult> {
  return api.post(
    `/episodes/${episodeId}/narrative-calibration/ai-simulate`,
  ) as Promise<AiOneWatchSimulationResult>
}

function HumanCalibrationControls({
  episode,
  notify,
  onChanged,
}: {
  episode: Episode
  notify: (message: string, error?: boolean) => void
  onChanged: () => Promise<unknown> | unknown
}) {
  const [protocol, setProtocol] = useState<CalibrationProtocol | null>(null)
  const [participant, setParticipant] = useState('')
  const [priorId, setPriorId] = useState('')
  const [genre, setGenre] = useState('')
  const [form, setForm] = useState('')
  const [recall, setRecall] = useState('')
  const [protocolConfirmed, setProtocolConfirmed] = useState(false)
  const [frozen, setFrozen] = useState<FrozenHumanWatch | null>(null)
  const [scores, setScores] = useState<Record<string, number>>({})
  const [interpretations, setInterpretations] = useState<Record<string, string>>({})
  const [calibrationPreview, setCalibrationPreview] = useState<Record<string, any> | null>(null)
  const [busy, setBusy] = useState(false)

  async function loadProtocol() {
    setBusy(true)
    try {
      const next = await api.get(`/episodes/${episode.id}/narrative-calibration/protocol`) as CalibrationProtocol
      setProtocol(next)
      setPriorId(current => current || next.audience_priors[0]?.audience_prior_id || '')
    } catch (caught) {
      notify((caught as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  async function runAiOneWatchSimulation() {
    setBusy(true)
    try {
      const result = await activateAiOneWatchSimulation(episode.id)
      await onChanged()
      notify(String(result.message || 'AI 一次观看模拟权威已激活'))
    } catch (caught) {
      notify((caught as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const freezeBlockedReason = !protocolConfirmed
    ? '请先确认一次观看协议'
    : !participant.trim()
      ? '请填写匿名参与者编号'
      : !priorId
        ? '请选择目标观众前提'
        : !genre.trim() || !form.trim()
          ? '请填写题材与叙事形式标签'
          : !recall.trim()
            ? '请填写首次自由复述'
            : null

  async function freezeRecall() {
    if (freezeBlockedReason) return
    setBusy(true)
    try {
      const result = await api.post(
        `/episodes/${episode.id}/narrative-calibration/freeze`,
        {
          participant_id_hash: await participantHash(participant),
          audience_prior_id: priorId,
          watched_once: true,
          watch_count: 1,
          replay_or_seek_used: false,
          source_material_seen: false,
          target_answers_seen: false,
          director_intent_seen: false,
          spontaneous_recall_frozen: true,
          spontaneous_recall: { free_text: recall.trim() },
          content_dimensions: { genre: genre.trim(), form: form.trim() },
          collection_context: { source: 'board_calibration_ui' },
        },
      ) as FrozenHumanWatch
      setFrozen(result)
      setScores(Object.fromEntries(result.target_contract.map(item => [item.target_delta_id, 50])))
      notify('首次复述已冻结，现在可以记录中性追问后的观察')
    } catch (caught) {
      notify((caught as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  async function submitObservation() {
    if (!frozen) return
    setBusy(true)
    try {
      await api.post(`/episodes/${episode.id}/narrative-calibration/observations`, {
        freeze_artifact_id: frozen.freeze_artifact_id,
        neutral_followup_observations: [],
        target_delta_observations: frozen.target_contract.map(item => ({
          audience_prior_id: frozen.audience_prior_id,
          target_delta_id: item.target_delta_id,
          observed_score: (scores[item.target_delta_id] ?? 0) / 100,
          observed_interpretation: {
            free_text: interpretations[item.target_delta_id]?.trim() || '未补充文字说明',
          },
        })),
      })
      setFrozen(null)
      setRecall('')
      setProtocolConfirmed(false)
      setScores({})
      setInterpretations({})
      await onChanged()
      await loadProtocol()
      notify('真人一次观看观察已纳入校准样本')
    } catch (caught) {
      notify((caught as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  async function rebuildCalibration(activate: boolean) {
    setBusy(true)
    try {
      const result = await api.post('/narrative-calibration/rebuild', {
        activate,
        expected_report_fingerprint: activate
          ? calibrationPreview?.activation_fingerprint
          : undefined,
      }) as Record<string, any>
      setCalibrationPreview(result)
      if (activate) {
        await onChanged()
        notify(result.activated ? '真人校准权威已激活' : '样本仍未达到激活条件', !result.activated)
      }
    } catch (caught) {
      notify((caught as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  return <details className="narrative-calibration-controls">
    <summary>一次观看权威与真人一次观看校准</summary>
    <div className="narrative-calibration-rebuild">
      <button type="button" className="btn primary" disabled={busy}
        onClick={() => void runAiOneWatchSimulation()}>
        {busy ? '模拟运行中…' : '运行 AI 一次观看模拟'}
      </button>
      <small>由你显式启动独立 AI 多先验模拟；不会伪造真人参与者或观察记录。</small>
    </div>
    {!protocol ? <button type="button" className="btn" disabled={busy} onClick={() => void loadProtocol()}>
      {busy ? '正在读取…' : '开始记录真人样本'}
    </button> : <>
      {!frozen ? <div className="narrative-calibration-form">
        <label>匿名参与者编号<input value={participant} onChange={event => setParticipant(event.target.value)} /></label>
        <label>观看前提<select value={priorId} onChange={event => setPriorId(event.target.value)}>
          {protocol.audience_priors.map(item => <option key={item.audience_prior_id} value={item.audience_prior_id}>
            {item.audience_description}（已有 {item.existing_observation_count} 份）
          </option>)}
        </select></label>
        <label>题材标签<input value={genre} onChange={event => setGenre(event.target.value)} placeholder="例如：都市悬疑" /></label>
        <label>叙事形式<input value={form} onChange={event => setForm(event.target.value)} placeholder="例如：纯对白或追逐" /></label>
        <label className="wide">首次自由复述<textarea value={recall} onChange={event => setRecall(event.target.value)}
          placeholder="只记录第一次看完后自然记住的人物、因果、问题和下一步预期" /></label>
        <label className="wide calibration-confirm"><input type="checkbox" checked={protocolConfirmed}
          onChange={event => setProtocolConfirmed(event.target.checked)} />
          我确认参与者只连续观看一次，未回放、未看原文、目标答案或导演意图
        </label>
        <button type="button" className="btn primary" disabled={busy || Boolean(freezeBlockedReason)}
          title={freezeBlockedReason || undefined} onClick={() => void freezeRecall()}>
          冻结首次复述
        </button>
        {freezeBlockedReason && <small>{freezeBlockedReason}</small>}
      </div> : <div className="narrative-calibration-targets">
        <p>首次复述已冻结。以下评分不会反写首次理解率。</p>
        {frozen.target_contract.map(item => <fieldset key={item.target_delta_id}>
          <legend>{item.description}</legend>
          <label>实际达成度 {scores[item.target_delta_id] ?? 50}%
            <input type="range" min="0" max="100" step="5" value={scores[item.target_delta_id] ?? 50}
              onChange={event => setScores(current => ({ ...current, [item.target_delta_id]: Number(event.target.value) }))} />
          </label>
          <label>观察说明<textarea value={interpretations[item.target_delta_id] || ''}
            onChange={event => setInterpretations(current => ({ ...current, [item.target_delta_id]: event.target.value }))} /></label>
        </fieldset>)}
        <button type="button" className="btn primary" disabled={busy} onClick={() => void submitObservation()}>
          提交真人观察
        </button>
      </div>}
      <div className="narrative-calibration-rebuild">
        <button type="button" className="btn" disabled={busy} onClick={() => void rebuildCalibration(false)}>预览全局校准</button>
        {calibrationPreview?.report && <p>
          样本 {calibrationPreview.report.sample_summary?.observation_count ?? 0} 份 ·
          结论 {calibrationPreview.report.decision === 'calibrated' ? '可激活' : '仍需补样本'}
        </p>}
        {calibrationPreview?.report?.decision === 'calibrated' && !calibrationPreview.activated &&
          <button type="button" className="btn primary" disabled={busy} onClick={() => void rebuildCalibration(true)}>
            激活校准权威
          </button>}
      </div>
    </>}
  </details>
}

function NarrativeReadinessPanel({
  episode,
  notify,
  onChanged,
}: {
  episode: Episode
  notify: (message: string, error?: boolean) => void
  onChanged: () => Promise<unknown> | unknown
}) {
  const summary = episode.narrative_contract_summary
  if (!summary) return null
  const metrics = episode.narrative_metrics || {}
  const review = episode.narrative_review_summary
  const calibration = episode.narrative_calibration_summary
  const numberMetric = (key: string) => {
    const value = metrics[key]
    return typeof value === 'number' && Number.isFinite(value) ? value : null
  }
  const percentMetric = (key: string) => {
    const value = numberMetric(key)
    return value === null ? '待计算' : `${Math.round(value * 100)}%`
  }
  const duplicateActions = numberMetric('duplicate_primary_action_count')
  const stateRegressions = numberMetric('state_regression_count')
  const processingDebt = numberMetric('audience_processing_debt')
  const ready = metrics.narrative_ready === true && calibration?.ready === true
  const reviewCopy = review?.decision === 'pass'
    ? calibration?.ready
      ? '冷观众与一次观看权威通过'
      : '等待一次观看权威'
    : review?.decision === 'revise'
      ? '冷观众要求修订'
      : review?.decision === 'needs_human_review'
        ? '等待人工复核'
        : '等待冷观众审读'

  return <section className="card narrative-readiness" aria-label="全链路叙事一致性">
    <header>
      <div><b>全链路叙事一致性</b><span>同一事件、动作、人物认知与观众理解合同贯穿剧本和分镜</span></div>
      <span className={`stamp ${ready ? 'green' : 'gold'}`}>{ready ? '叙事就绪' : reviewCopy}</span>
    </header>
    <dl>
      <div><dt>命题 / 事件</dt><dd>{summary.proposition_count} / {summary.event_count}</dd></div>
      <div><dt>目标观众路径</dt><dd>{summary.audience_prior_count}</dd></div>
      <div><dt>事件交付覆盖</dt><dd>{percentMetric('event_coverage_rate')}</dd></div>
      <div><dt>重复主动作</dt><dd>{duplicateActions ?? '待计算'}</dd></div>
      <div><dt>状态回退</dt><dd>{stateRegressions ?? '待计算'}</dd></div>
      <div><dt>观众处理欠债</dt><dd>{processingDebt === null ? '待计算' : `${processingDebt.toFixed(1)}s`}</dd></div>
      <div><dt>一次观看权威</dt><dd>{
        calibration?.ready
          ? '当前版本已绑定'
          : calibration?.status === 'awaiting_republish'
            ? '已就绪，待发布绑定'
            : '待激活'
      }</dd></div>
    </dl>
    {review?.reason && <p>{review.reason}</p>}
    {calibration?.blockers?.length ? <p className="narrative-calibration-blocker">{calibration.blockers[0]}</p> : null}
    <details><summary>查看理解与结构指标</summary><p>体验意图覆盖 {percentMetric('experience_intent_coverage_rate')} · 认知任务截止通过 {percentMetric('assimilation_deadline_pass_rate')} · 镜头功能贡献 {percentMetric('shot_contribution_coverage')}</p><p>系统逐个观众先验验收，不以平均高分替代低分位失败。</p></details>
    <HumanCalibrationControls episode={episode} notify={notify} onChanged={onChanged} />
  </section>
}

function statusFallback(ep: Episode): StoryboardStatus {
  return {
    contract_version: 'storyboard-workspace.v1', snapshot_version: 0, state_fingerprint: '',
    state: 'syncing', headline: '状态同步中，暂不可执行高影响操作',
    screenplay_available: ep.screenplay_status === 'ready', planned_shots: 0,
    produced_shots: ep.shots?.length ?? 0, validated_shots: 0, final_shot_valid: false,
    hard_gates_passed: false, confirmed: false, editable: false, confirmable: false,
    recommended_action: 'refresh_status', write_block_reason: '等待服务端同步分镜状态',
  }
}

export default function BoardPage() {
  const { episodeId, go, projectId, toast, registerNavigationGuard } = useNav()
  const { data: ep, refresh, error, status: queryErrorStatus, loading } = useEpisode(episodeId!, 'board')
  const [busy, setBusy] = useState(false)
  const [selectedShotId, setSelectedShotId] = useState<string | null>(null)
  const [shotEditDirty, setShotEditDirty] = useState(false)
  const [pendingShotId, setPendingShotId] = useState<string | null>(null)
  const [onlyProblems, setOnlyProblems] = useState(false)
  const [sceneFilter, setSceneFilter] = useState('')
  const [characterFilter, setCharacterFilter] = useState('')
  const [capacityFilter, setCapacityFilter] = useState(false)
  const [riskFilter, setRiskFilter] = useState(false)
  const [recoveredStatus, setRecoveredStatus] = useState<StoryboardStatus | null>(null)
  const [startPreview, setStartPreview] = useState<StartPreview | null>(null)
  const [confirmPreview, setConfirmPreview] = useState<ConfirmPreview | null>(null)
  const [structurePreview, setStructurePreview] = useState<StructurePreview | null>(null)
  const [clearPreview, setClearPreview] = useState<StoryboardClearPreview | null>(null)
  const timelineRef = useRef<HTMLDivElement>(null)
  const startPreviewTriggerRef = useRef<HTMLElement | null>(null)

  const shots = ep?.shots ?? []
  const visibleShots = useMemo(() => shots.filter(shot => {
    if (onlyProblems && !isStoryboardProblemShot(shot)) return false
    if (sceneFilter && (shot.scene_name || shot.scene_setting) !== sceneFilter) return false
    if (characterFilter && ![...(shot.characters ?? []), ...(shot.audio_cast ?? [])].includes(characterFilter)) return false
    if (capacityFilter && storyboardSpokenChars(shot) <= shotSpokenLimit(shot)) return false
    if (riskFilter && !shot.preflight_errors?.length && !shot.continuity_degraded && !(shot.risk_tags?.length)) return false
    return true
  }), [shots, onlyProblems, sceneFilter, characterFilter, capacityFilter, riskFilter])
  // 先在全量 shots 里按当前选中 id 解析，避免轮询刷新使选中镜因“问题态”变化离开
  // visibleShots 时被静默跳到第一条；仅当尚无选中或该镜确实被删除时，才回退到 visibleShots[0]。
  const selectedShot = shots.find(shot => shot.id === selectedShotId) ?? visibleShots[0]
  const selectedIndex = visibleShots.findIndex(shot => shot.id === selectedShot?.id)
  const absoluteIndex = shots.findIndex(shot => shot.id === selectedShot?.id)
  const selectionOutsideFilters = Boolean(
    selectedShot && visibleShots.length > 0 && !visibleShots.some(shot => shot.id === selectedShot.id),
  )
  const status = ep?.storyboard_status ?? recoveredStatus ?? (ep ? statusFallback(ep) : null)
  const taskNotice = ep ? storyboardTaskNotice(ep, status?.state) : null
  const filterDisabledReason = shotEditDirty ? '请先保存或放弃当前镜头修改' : ''
  const hasActiveFilters = onlyProblems || Boolean(sceneFilter) || Boolean(characterFilter) || capacityFilter || riskFilter

  useLayoutEffect(() => {
    if (!shotEditDirty) {
      registerNavigationGuard(null, false)
      return
    }
    registerNavigationGuard({
      title: '放弃未保存的镜头修改？',
      summary: selectedShot ? `镜 ${selectedShot.shot_no} 仍在编辑` : '当前镜头仍在编辑',
      message: '逐镜字段修改尚未保存，离开分镜台会丢失当前输入；发布版、失败草稿和下游产物不会变化。',
      details: ['当前逐镜编辑不会自动保存', '返回继续编辑可保留当前输入'],
      confirmLabel: '放弃修改并离开',
      cancelLabel: '继续编辑',
      danger: true,
    }, true)
    return () => registerNavigationGuard(null, false)
  }, [registerNavigationGuard, selectedShot, shotEditDirty])

  const scenes = useMemo(() => [...new Set(shots.map(shot => shot.scene_name || shot.scene_setting).filter(Boolean))], [shots])
  const characters = useMemo(() => storyboardCharacterFilterOptions(shots), [shots])

  useEffect(() => {
    if (!selectedShot || selectedShot.id === selectedShotId) return
    if (shotEditDirty && selectedShotId) {
      setPendingShotId(selectedShot.id)
      return
    }
    setSelectedShotId(selectedShot.id)
  }, [selectedShot?.id, selectedShotId, shotEditDirty])

  useEffect(() => {
    timelineRef.current?.querySelector<HTMLElement>('[aria-selected="true"]')
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

  const requestShotSelect = (shotId: string) => {
    if (shotId === selectedShot?.id) return
    if (shotEditDirty) {
      setPendingShotId(shotId)
      return
    }
    setSelectedShotId(shotId)
  }

  const selectRelative = (offset: number) => {
    if (!visibleShots.length) return
    const next = Math.max(0, Math.min(visibleShots.length - 1, selectedIndex + offset))
    requestShotSelect(visibleShots[next].id)
  }

  const clearShotFilters = () => {
    setOnlyProblems(false)
    setSceneFilter('')
    setCharacterFilter('')
    setCapacityFilter(false)
    setRiskFilter(false)
    window.requestAnimationFrame(() => timelineRef.current?.focus())
  }

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (
        event.defaultPrevented ||
        !['ArrowLeft', 'ArrowRight'].includes(event.key)
      ) return
      const target = event.target as HTMLElement | null
      if (target?.closest(
        'input, textarea, select, button, a, [role="tab"], [role="listbox"], [role="combobox"], [contenteditable="true"]',
      )) return
      event.preventDefault()
      selectRelative(event.key === 'ArrowLeft' ? -1 : 1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [visibleShots, selectedIndex, selectedShot?.id, shotEditDirty])

  if (!ep || !status) {
    return (
      <QueryState
        loading={loading}
        error={error}
        status={queryErrorStatus}
        hasData={false}
        objectName="分镜台"
        loadingText="正在加载分镜、镜头列表与确认状态…"
        emptyText="未找到可展示的分镜数据，请稍后重新进入本页。"
        onRetry={() => void refresh()}
      >
        {null}
      </QueryState>
    )
  }
  const currentEpisodeId = ep.id
  const primaryAction = storyboardPrimaryAction(
    status,
    ep.narrative_calibration_summary,
    ep.narrative_review_summary,
  )

  const run = async <T,>(fn: () => Promise<T>, message?: string): Promise<T | undefined> => {
    setBusy(true)
    try {
      const result = await fn()
      if (message) toast(message)
      await refresh({ force: true })
      return result
    } catch (caught) {
      toast((caught as Error).message, true)
      return undefined
    } finally {
      setBusy(false)
    }
  }

  const loadStartPreview = async () => {
    startPreviewTriggerRef.current = document.activeElement as HTMLElement | null
    setBusy(true)
    try {
      const preview = await api.post(`/episodes/${ep.id}/storyboard/preflight`, {}) as StartPreview
      setStartPreview(preview)
    } catch (caught) {
      toast((caught as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const closeStartPreview = () => {
    setStartPreview(null)
    window.requestAnimationFrame(() => {
      const trigger = startPreviewTriggerRef.current
      const triggerUsable = trigger
        && trigger !== document.body
        && trigger.isConnected
        && trigger.getClientRects().length > 0
      const fallback = document.getElementById('storyboard-primary-action')
      const focusTarget = triggerUsable ? trigger : fallback
      focusTarget?.focus()
    })
  }

  const submitStart = async () => {
    if (!startPreview || startPreview.can_start === false) return
    const preview = startPreview
    setStartPreview(null)
    const result = await run(
      () => api.post(`/episodes/${ep.id}/storyboard`, {
        preflight_token: preview.preview_token,
      }),
      preview.resume_mode === 'repair_existing'
        ? '已开始重新校验并修复现有分镜'
        : preview.action === 'resume' ? '已从安全检查点继续生成' : '分镜生成已开始',
    )
  }

  const runPrimary = async () => {
    if (primaryAction.intent === 'activate_ai_one_watch') {
      const result = await run(() => activateAiOneWatchSimulation(ep.id))
      if (result) toast(result.message || 'AI 一次观看模拟权威已激活')
      return
    }
    switch (primaryAction.intent) {
      case 'go_screenplay': go('script', projectId, ep.id); break
      case 'generate_storyboard': await loadStartPreview(); break
      case 'resume_storyboard': await loadStartPreview(); break
      case 'view_progress': go('observability', projectId, null); break
      case 'confirm_storyboard': {
        // 决策③：分镜确认不再是需要人工点头的内容质量门——必检项一旦全部通过就自动放行，
        // 不再停下来等一次"批准并确认分镜"的点击。必检项没通过是真实阻断（不是审美/内容取舍），
        // 这类问题继续展示详情，因为用户需要知道具体要修什么，而不是被要求"确认"一个坏结果。
        setBusy(true)
        try {
          const preview = await api.post(`/episodes/${ep.id}/confirm-preview`) as ConfirmPreview
          await autoConfirmStoryboard(preview)
        } catch (caught) {
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
      default: break
    }
  }

  const autoConfirmStoryboard = async (preview: ConfirmPreview) => {
    if (!preview.hard_gates.passed || !preview.preview_token) {
      setConfirmPreview(preview)
      return
    }
    await run(
      () => api.post(`/episodes/${ep.id}/confirm`, {
        preview_token: preview.preview_token,
      }),
      '分镜已确认，可以进入生成台',
    )
  }

  const previewStructure = async (operation: StructurePreview['operation'], targetIndex = absoluteIndex) => {
    if (!selectedShot) return
    if (operation === 'delete' && storyboardDeleteUsesFullClear(shots, selectedShot.id)) {
      await previewClearStoryboard()
      return
    }
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

  async function previewClearStoryboard() {
    setBusy(true)
    try {
      setClearPreview(await api.post(
        `/episodes/${currentEpisodeId}/storyboard/clear-preview`,
        {},
      ) as StoryboardClearPreview)
    } catch (caught) {
      toast((caught as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const clearStoryboard = async () => {
    if (!clearPreview) return
    const previewToken = clearPreview.preview_token
    const deletedShots = clearPreview.shot_count
    setClearPreview(null)
    const result = await run(
      () => api.post(`/episodes/${ep.id}/storyboard/clear`, { preview_token: previewToken }),
      `已清空 ${deletedShots} 个镜头及其下游资源，剧本已保留`,
    )
    if (!result) return
    setSelectedShotId(null)
    setShotEditDirty(false)
    clearShotFilters()
  }

  const pauseStoryboard = async () => {
    const result = await run(
      () => api.post(`/episodes/${ep.id}/storyboard/cancel`, {}),
      '分镜任务已暂停，工作镜头和安全检查点已保留',
    )
  }

  const showLaunchPanel = !shots.length && (status.state === 'empty' || status.state === 'no_screenplay')
  const primaryBlocked = status.recommended_action === 'refresh_status'
  const gateIssueCount = status.hard_gate_issue_count ?? status.hard_gate_issues?.length ?? 0
  const progressCopy = storyboardProgressCopy(status)
  const startPreviewCopy = startPreview ? storyboardStartPreviewCopy(startPreview) : null
  const pendingRevalidation = status.pending_revalidation_shots
    ?? Math.max(0, status.produced_shots - status.validated_shots)
  const terminalFinalShot = status.final_shot_valid && ['ready_to_confirm', 'confirmed'].includes(status.state)
  const toolbarActions = storyboardToolbarActions(status.state)
  // 逐镜耗时按 shot_no 归集；从未生成过的镜头没有条目，计时器自然不显示。
  const shotTiming = (shotNo: number) => ep.shot_timings?.[String(shotNo)]

  return (
    <>
      <header className="desk-head">
        <EpisodeCrumb label="分镜台" view="board" episodeNo={ep.episode_no} />
        <h1>分镜台 <span className="sub">《{ep.title}》 · 安全审阅、修镜并交接下游</span></h1>
        <hr className="rule" />
      </header>

      <NarrativeReadinessPanel episode={ep} notify={toast} onChanged={refresh} />

      {showLaunchPanel ? (
        <StoryboardLaunchPanel episode={ep} status={status} busy={busy} onPrimary={() => { void runPrimary() }} />
      ) : <><section className={`card board-toolbar state-${status.state}`} aria-labelledby="storyboard-state-title">
        <div className="board-toolbar-row">
          <div className="board-state-copy">
            <span className={`storyboard-state-dot state-${status.state}`} aria-hidden="true" />
            <div>
              <strong id="storyboard-state-title">{status.headline}</strong>
              <small>{progressCopy.summary}{terminalFinalShot ? ' · 收尾镜有效' : ''}</small>
            </div>
          </div>
          <div className="board-action-group">
            {toolbarActions.pause ? <>
              <button id="storyboard-primary-action" type="button" className="btn board-primary-action danger"
                disabled={busy} aria-label={busy ? '暂停任务，正在处理' : '暂停分镜任务'}
                onClick={() => void pauseStoryboard()}>
                {busy ? '正在暂停…' : '暂停任务'}
              </button>
              <button type="button" className="btn" disabled={busy} onClick={() => go('observability', projectId, null)}>
                查看任务详情
              </button>
            </> : <>
              <button id="storyboard-primary-action" type="button" className="btn board-primary-action primary" disabled={busy || primaryBlocked}
                aria-label={busy ? `${primaryAction.label}，暂不可用：正在处理上一项操作` : primaryBlocked ? `${primaryAction.label}，暂不可操作` : primaryAction.label}
                onClick={() => void runPrimary()}>
                {busy ? '处理中…' : primaryAction.label}
              </button>
              {toolbarActions.clear && <button type="button" className="btn danger"
                disabled={busy || shotEditDirty}
                title={shotEditDirty ? '请先保存或放弃当前镜头修改' : '清空全部镜头、检查点及下游资源，保留剧本'}
                onClick={() => void previewClearStoryboard()}>
                清空分镜
              </button>}
            </>}
          </div>
          {status.state === 'running' && (
            <ServerTaskTimer
              label="分镜"
              startedAt={status.task_started_at}
              finishedAt={status.task_finished_at}
              running
            />
          )}
        </div>
        {progressCopy.detail && <div className="board-progress-explanation" role="status">
          <b>数字口径</b><span>{progressCopy.detail}</span>
        </div>}
        {status.write_block_reason && status.state === 'syncing' && <div className="board-sync-banner" role={status.system_error ? 'alert' : 'status'}><b>{status.system_error ? '系统校验异常' : '正在同步状态'}</b><span>{status.write_block_reason}</span></div>}
        {(taskNotice || ep.storyboard_warning) && (
          <div
            className={`storyboard-error-details open ${(taskNotice?.severity ?? 'warning') === 'warning' ? 'warning' : 'error'}`}
            role={taskNotice?.severity === 'error' ? 'alert' : 'status'}
          >
            <b>{taskNotice?.severity === 'error' ? '任务未完成' : taskNotice ? '任务已暂停' : '提示'}</b>
            <p>{taskNotice?.message || ep.storyboard_warning}</p>
          </div>
        )}
        {!!status.hard_gate_issues?.length && (
          <div className="storyboard-error-details open" role="alert">
            <b>{gateIssueCount} 个问题需要处理</b>
            <ul>{status.hard_gate_issues.slice(0, 3).map((item, index) => <li key={`${index}-${item}`}>{storyboardGateIssueLabel(item)}</li>)}</ul>
            {gateIssueCount > 3 && <small>其余问题会在对应镜头中标出。</small>}
          </div>
        )}
      </section>

      <div className="workspace-gap" />

      {!shots.length ? (
        <div className="empty storyboard-empty">
          <div className="big">镜</div>
          {storyboardEmptyCopy(status)}
        </div>
      ) : (
        <div className="board-workspace">
          <section className="shot-navigator" aria-label="镜头轨道">
            <div className="shot-filter-bar" aria-label="镜头筛选">
              <label title={filterDisabledReason || undefined}><input type="checkbox" disabled={shotEditDirty} checked={onlyProblems}
                aria-label={filterDisabledReason ? `仅看问题镜，暂不可用：${filterDisabledReason}` : '仅看问题镜'}
                onChange={event => setOnlyProblems(event.target.checked)} />仅看问题镜</label>
              <label title={filterDisabledReason || undefined}><span>场景</span><select
                aria-label={filterDisabledReason ? `按场景筛选，暂不可用：${filterDisabledReason}` : '按场景筛选'}
                disabled={shotEditDirty} value={sceneFilter} onChange={event => setSceneFilter(event.target.value)}>
                <option value="">全部</option>{scenes.map(scene => <option key={scene}>{scene}</option>)}
              </select></label>
              <label title={filterDisabledReason || undefined}><span>角色</span><select
                aria-label={filterDisabledReason ? `按角色筛选，暂不可用：${filterDisabledReason}` : '按角色筛选'}
                disabled={shotEditDirty} value={characterFilter} onChange={event => setCharacterFilter(event.target.value)}>
                <option value="">全部</option>{characters.map(name => <option key={name}>{name}</option>)}
              </select></label>
              <label title={filterDisabledReason || undefined}><input type="checkbox" disabled={shotEditDirty} checked={capacityFilter}
                aria-label={filterDisabledReason ? `仅看口播超限镜头，暂不可用：${filterDisabledReason}` : '仅看口播超限镜头'}
                onChange={event => setCapacityFilter(event.target.checked)} />口播超限</label>
              <label title={filterDisabledReason || undefined}><input type="checkbox" disabled={shotEditDirty} checked={riskFilter}
                aria-label={filterDisabledReason ? `仅看连续性风险镜头，暂不可用：${filterDisabledReason}` : '仅看连续性风险镜头'}
                onChange={event => setRiskFilter(event.target.checked)} />连续性风险</label>
              {hasActiveFilters && <button type="button" className="btn small ghost clear-shot-filters" disabled={shotEditDirty}
                aria-label={filterDisabledReason ? `清除全部镜头筛选，暂不可用：${filterDisabledReason}` : '清除全部镜头筛选'}
                onClick={clearShotFilters}>清除筛选</button>}
              <span className="shot-keyboard-hint">{shotEditDirty ? '保存或放弃后可切镜与筛选' : '← → 切镜（输入时不会触发）'}</span>
            </div>
            <div className="shot-navigator-head">
              <b>镜头轨道</b>
              <div className="shot-navigator-actions" aria-live="polite">
                <span>{visibleShots.length ? selectedIndex + 1 : 0} / {visibleShots.length}，问题镜 {shots.filter(isStoryboardProblemShot).length}{pendingRevalidation > 0 ? ` · 待校验 ${pendingRevalidation} 镜` : ''}</span>
                <button type="button"
                  aria-label={!visibleShots.length ? '上一镜，暂不可用：当前筛选下没有镜头' : selectedIndex <= 0 ? '上一镜，暂不可用：当前已是筛选结果中的第一镜' : '上一镜'}
                  disabled={selectedIndex <= 0} onClick={() => selectRelative(-1)}>←</button>
                <button type="button"
                  aria-label={!visibleShots.length ? '下一镜，暂不可用：当前筛选下没有镜头' : selectedIndex >= visibleShots.length - 1 ? '下一镜，暂不可用：当前已是筛选结果中的最后一镜' : '下一镜'}
                  disabled={selectedIndex >= visibleShots.length - 1} onClick={() => selectRelative(1)}>→</button>
              </div>
            </div>
            <div ref={timelineRef} className="shot-navigator-list" role="listbox" aria-label="镜头列表" tabIndex={0}>
              {visibleShots.map(shot => {
                const checkpoint = storyboardShotCheckpointLabel(shot.shot_no, status)
                return <button key={shot.id} type="button" role="option" aria-selected={shot.id === selectedShot?.id}
                  className={shot.id === selectedShot?.id ? 'active' : ''}
                  onClick={() => requestShotSelect(shot.id)}>
                  <span className="shot-nav-top"><span className="shot-nav-no">镜 {String(shot.shot_no).padStart(2, '0')}</span><span>{shot.duration_s}s</span></span>
                  <span className="shot-nav-main"><b>{shot.shot_size} · {shot.camera_angle || '平视'} · {shot.camera_move}</b><small>{shot.scene_time ? `${shot.scene_time} · ` : ''}{shot.scene_name || shot.scene_setting}</small></span>
                  <span className="shot-nav-badges">
                    {shotTiming(shot.shot_no) && (
                      <ItemTaskTimer
                        elapsedMs={shotTiming(shot.shot_no)!.elapsed_ms}
                        runningSince={shotTiming(shot.shot_no)!.running_since}
                        iterations={shotTiming(shot.shot_no)!.iterations}
                        compact
                      />
                    )}
                    {checkpoint && <i className={checkpoint.className} title={checkpoint.title}>{checkpoint.label}</i>}
                    {isStoryboardProblemShot(shot) && <i className="problem">需处理</i>}
                    {shot.is_final && <i>{checkpoint?.className === 'checkpoint-pending' ? '草稿收尾' : '收尾镜'}</i>}
                    {storyboardSpokenChars(shot) > shotSpokenLimit(shot) && <i className="problem">口播超限</i>}
                  </span>
                </button>
              })}
              {!visibleShots.length && <div className="shot-filter-empty" role="status">
                <b>当前筛选下没有镜头</b>
                <span>清除筛选后可恢复全部 {shots.length} 镜。</span>
                <button type="button" className="btn small" onClick={clearShotFilters}>清除筛选</button>
              </div>}
            </div>
          </section>

          <section className="shot-editor-pane">
            {selectionOutsideFilters && (
              <div className="filter-selection-note" role="status">当前镜头已不在筛选结果内，已保留当前对象；只有你主动切换时才会离开。</div>
            )}
            {selectedShot && (
              <ShotWorkspace key={`${selectedShot.id}:${selectedShot.storyboard_artifact_id ?? ''}`}
                shot={selectedShot} episode={ep} status={status}
                previous={shots[absoluteIndex - 1]} next={shots[absoluteIndex + 1]}
                onChanged={() => { void refresh({ force: true }) }} onSelect={requestShotSelect}
                onDirtyChange={setShotEditDirty}
                onStructure={previewStructure} disabled={busy} />
            )}
          </section>
        </div>
      )}</>}

      <Modal
        open={!!pendingShotId}
        title="切换镜头前放弃当前修改？"
        onClose={() => setPendingShotId(null)}
        actions={<>
          <button className="btn" onClick={() => setPendingShotId(null)}>返回继续编辑</button>
          <button className="btn danger" onClick={() => {
            const target = pendingShotId
            setPendingShotId(null)
            setShotEditDirty(false)
            if (target) setSelectedShotId(target)
          }}>放弃修改并切镜</button>
        </>}
      >
        <p>当前镜头的逐字段修改尚未保存。切换后输入无法恢复；发布版、失败草稿和下游产物不会变化。</p>
      </Modal>

      {clearPreview && <DecisionDialog
        title="清空本集全部分镜？"
        summary={`${clearPreview.shot_count} 个镜头、${clearPreview.video_version_count} 个视频版本、${clearPreview.reference_asset_count} 个参考图资源将被删除`}
        message="将把本集分镜工作区恢复到未生成状态；当前剧本会完整保留，之后可以从头生成新的分镜。"
        details={[
          ...(clearPreview.active_task_will_stop ? ['正在运行的分镜或视频任务会先安全停止'] : []),
          `将终止当前任务并重置修复检查点；${clearPreview.workflow_run_count} 条历史任务记录保留用于审计，不参与下次生成`,
          clearPreview.delivery_package_count
            ? `将删除 ${clearPreview.delivery_package_count} 个下游交付包`
            : '当前没有下游交付包',
          '此操作不可撤销，已经产生的模型调用费用不会退回',
        ]}
        confirmLabel="确认清空并保留剧本"
        cancelLabel="取消"
        danger
        onClose={() => setClearPreview(null)}
        onConfirm={() => void clearStoryboard()}
      />}

      <Modal open={!!startPreview} title={startPreviewCopy?.title ?? '分镜任务'} onClose={closeStartPreview}
        actions={<><button className="btn" onClick={closeStartPreview}>取消</button><button className="btn primary" disabled={startPreview?.can_start === false} onClick={() => void submitStart()}>
          {startPreviewCopy?.confirmLabel ?? '继续'}
        </button></>}>
        {startPreview && startPreviewCopy && <div className="storyboard-preview-card">
          <p><b>{startPreviewCopy.summary}</b></p>
          <p>{startPreviewCopy.detail}</p>
          {startPreview.repair && startPreview.action === 'resume'
            && startPreview.resume_mode !== 'finalize_evidence' && <p>
            历史修复 {startPreview.repair.lifetime_repair_count} 次；将开启第 {startPreview.repair.activation_no + 1} 轮，
            每轮最多 {startPreview.repair.max_attempts_per_activation} 次。候选通过校验前不会覆盖现有分镜。
          </p>}
          {startPreview.blocking_reason && <div className="error-banner" role="alert">{startPreview.blocking_reason}</div>}
          {startPreview.warning && <div className="warning-banner">{startPreview.warning}</div>}
          {!!startPreview.repair?.last_issue_messages.length && <div className="warning-banner">
            {startPreview.repair.last_issue_messages.map(storyboardGateIssueLabel).join('；')}
          </div>}
        </div>}
      </Modal>

      {/* 决策③：确认分镜不再是需要人工点头的内容质量门——必检项全部通过时已在 autoConfirmStoryboard
          里自动放行，走不到这个弹窗。这里只在必检项未通过（真实阻断，不是审美取舍）时出现，
          纯粹展示要修什么，不提供"确认"按钮。 */}
      <Modal open={!!confirmPreview} title="暂不能确认分镜" onClose={() => setConfirmPreview(null)}
        actions={<button className="btn" onClick={() => setConfirmPreview(null)}>返回审阅</button>}>
        {confirmPreview && <div className="storyboard-preview-card">
          <dl><div><dt>当前分镜</dt><dd>{confirmPreview.storyboard_artifact_id ? '已生成，等待确认' : '待定稿'}</dd></div><div><dt>镜头完整性</dt><dd>{confirmPreview.shot_count}/{confirmPreview.planned_shots}</dd></div>
            <div><dt>总时长</dt><dd>{confirmPreview.total_duration_s}s</dd></div><div><dt>收尾镜</dt><dd>{confirmPreview.final_shot_valid ? '有效' : '缺失'}</dd></div>
            <div><dt>必检项</dt><dd>未通过</dd></div><div><dt>预计视频成本</dt><dd>¥{confirmPreview.estimated_video_cost_cny.min}–¥{confirmPreview.estimated_video_cost_cny.max}</dd></div></dl>
          {!!confirmPreview.warnings.length && <div className="warning-banner">{confirmPreview.warnings.map(storyboardGateIssueLabel).join('；')}</div>}
          {!!confirmPreview.hard_gates.errors.length && <div className="error-banner" role="alert"><b>请先处理以下问题：</b><ul>{confirmPreview.hard_gates.errors.map((item, index) => <li key={`${index}-${item}`}>{storyboardGateIssueLabel(item)}</li>)}</ul></div>}
          <p>处理方式：{confirmPreview.recovery_action || '返回分镜台继续修复，全部必检项通过后会自动确认'}</p>
          <small>{confirmPreview.estimated_video_cost_cny.note}</small>
        </div>}
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

function ShotWorkspace({ shot, episode, status, previous, next, onChanged, onSelect, onDirtyChange, onStructure, disabled }: {
  shot: Shot; episode: Episode; status: StoryboardStatus; previous?: Shot; next?: Shot
  onChanged: () => void; onSelect: (id: string) => void
  onDirtyChange: (dirty: boolean) => void
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
  const [conflictImpact, setConflictImpact] = useState<SpokenConflictPreview | null>(null)
  const [conflictImpactLoading, setConflictImpactLoading] = useState(false)
  const [conflictImpactError, setConflictImpactError] = useState<string | null>(null)
  const [discardDraftId, setDiscardDraftId] = useState<string | null>(null)
  const [discardEditOpen, setDiscardEditOpen] = useState(false)
  const [reloadLatestOpen, setReloadLatestOpen] = useState(false)
  const sourceTextRef = useRef<HTMLTextAreaElement>(null)
  const current = edit ?? shot
  const inputStrategy = storyboardInputStrategy(current, previous)
  const effectiveFirstFrameDesc = inputStrategy.kind === 'previous_video_tail'
    ? (previous?.last_frame_desc || current.first_frame_desc)
    : current.first_frame_desc
  const firstFrameLabel = inputStrategy.kind === 'previous_video_tail'
    ? '生成起点（继承前镜视频尾帧）'
    : inputStrategy.kind === 'reference_video'
      ? '参考视频起点（原逻辑）'
      : '场景首镜起点（人物谱/场景库）'
  const changes = edit && baseline ? buildStoryboardChanges(baseline, edit, sourceBinding) : {}
  const dirty = Object.keys(changes).length > 0
  const currentChars = storyboardSpokenChars(current)
  const spokenLimit = shotSpokenLimit(current)
  const overCapacity = currentChars > spokenLimit
  const prevConflict = Boolean(edit && previous?.state_out?.trim() && edit.state_in?.trim() && previous.state_out.trim() !== edit.state_in.trim())
  const nextConflict = Boolean(edit && next?.state_in?.trim() && edit.state_out?.trim() && next.state_in.trim() !== edit.state_out.trim())
  const structureEditEnabled = status.feature_flags?.structure_edit !== false
  const writeBlockReason = status.write_block_reason || '当前状态暂不可修改，请等待状态同步'
  const editDisabledReason = disabled
    ? '正在处理上一项操作'
    : !status.editable
      ? writeBlockReason
      : ''
  const deleteDisabledReason = editDisabledReason || (!structureEditEnabled ? '镜头结构编辑已由管理员关闭' : '')
  const isOnlyShot = !previous && !next
  const deleteActionTitle = deleteDisabledReason || (isOnlyShot
    ? '删除最后一镜将清空本集分镜工作区及下游资源，剧本保留；执行前会再次确认'
    : '删除前会展示镜号重排、相邻重验和媒体失效影响')
  const saveDisabledReason = storyboardSaveDisabledReason(dirty, overCapacity, current.characters.length)
  const structureDisabledReason = disabled
    ? '正在处理上一项操作'
    : dirty
      ? '请先保存或放弃当前字段修改'
      : ''
  const detailPanelId = `shot-detail-panel-${shot.id}`

  useLayoutEffect(() => {
    onDirtyChange(dirty)
    return () => onDirtyChange(false)
  }, [dirty, onDirtyChange])
  const focusDetailTab = (nextTab: 'frames' | 'script') => {
    setDetailTab(nextTab)
    window.requestAnimationFrame(() => {
      document.getElementById(`shot-detail-tab-${shot.id}-${nextTab}`)?.focus()
    })
  }

  const characterOptions = useMemo(() => [...new Set([
    ...(episode.shots ?? []).flatMap(item => item.characters ?? []),
    ...(episode.screenplay?.scene_outline ?? []).flatMap(scene => scene.characters ?? []),
  ].filter(Boolean))], [episode.shots, episode.screenplay])
  const sceneOptions = useMemo(() => [...new Set([
    ...(episode.scene_options ?? []),
    ...(episode.shots ?? []).map(item => item.scene_name || item.scene_setting),
  ].filter(Boolean))], [episode.scene_options, episode.shots])
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
        toast('没有实际变化，无需保存')
        return
      }
      setImpact(result as ImpactSummary & { preview_token: string; normalized_changes: Record<string, unknown> })
    } catch (caught) {
      const error = caught as ApiError
      setImpactError(error.message)
      if (error.status === 409) setSaveErrors(['编辑期间分镜已被其他操作更新。可复制当前草稿后重新载入最新内容。'])
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
      toast(result.unchanged ? '内容未变化，无需保存' : `镜 ${shot.shot_no} 已保存并完成影响传播`)
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

  const requestDiscard = () => {
    if (dirty) setDiscardEditOpen(true)
    else discard()
  }

  const copyDraft = async () => {
    if (!edit || !navigator.clipboard) {
      toast('当前浏览器无法访问剪贴板，请检查浏览器权限后重试', true)
      return
    }
    try {
      await navigator.clipboard.writeText(JSON.stringify(edit, null, 2))
      toast('当前草稿已复制')
    } catch {
      toast('复制失败，请允许浏览器访问剪贴板后重试', true)
    }
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

  const previewConflictResolution = async (
    choice: SpokenConflictPreview['choice'],
  ) => {
    setConflictImpactLoading(true)
    setConflictImpactError(null)
    try {
      const preview = await api.post(
        `/shots/${shot.id}/spoken-conflict-preview`,
        { choice },
      ) as SpokenConflictPreview
      setConflictImpact(preview)
    } catch (caught) {
      setConflictImpactError((caught as Error).message)
    } finally {
      setConflictImpactLoading(false)
    }
  }

  const resolveConflict = async () => {
    if (!conflictImpact) return
    const preview = conflictImpact
    setConflictImpact(null)
    try {
      await api.post(`/shots/${shot.id}/resolve-spoken-conflict`, {
        choice: preview.choice, invalidate_media: true, preview_token: preview.preview_token,
        edit_session_token: preview.edit_session_token,
        baseline_content_hash: preview.baseline_content_hash,
      })
      toast(preview.choice === 'rebuild_timeline_from_dialogues' ? '已按台词重建时间轴' : '已按时间轴重建台词')
      setConflictOpen(false); discard(); onChanged()
    } catch (caught) { toast((caught as Error).message, true) }
  }

  return (
    <article className={`shot-strip ${edit ? 'editing' : 'reviewing'}`}>
      <header className="shot-head">
        <div className="shot-head-copy"><span className="sn">镜{String(shot.shot_no).padStart(2, '0')}</span>
          <span className="meta">{current.duration_s}s · {current.shot_size} · {current.camera_angle || '平视'} · {current.camera_move} · {current.transition}</span>
          <span className="meta shot-characters">{current.characters.join(' / ') || '缺角色（需修改）'}</span>
          <span className="shot-badge gate" title={inputStrategy.detail}>{inputStrategy.label}</span>
          {isStoryboardProblemShot(shot) && <span className="shot-badge status-needs_revision">需处理</span>}
          {shot.is_final && <span className="shot-badge gate">收尾镜</span>}
        </div>
        <div className="shot-head-actions">
          {shot.spoken_contract_status === 'conflict' && <button className="btn small ghost" onClick={() => setConflictOpen(true)}>解决口播冲突</button>}
          {!edit && !status.editable && <span className="shot-actions-locked" role="status">{writeBlockReason}</span>}
          {!edit ? <>
            <button className="btn small danger shot-delete-action" disabled={Boolean(deleteDisabledReason)}
              aria-label={deleteDisabledReason ? `删除镜头，暂不可用：${deleteDisabledReason}` : '删除镜头'}
              title={deleteActionTitle}
              onClick={() => void onStructure('delete')}>删除镜头</button>
            <button className="btn small" disabled={Boolean(editDisabledReason)}
              aria-label={editDisabledReason ? `修改镜头，暂不可用：${editDisabledReason}` : '修改镜头'}
              title={editDisabledReason || undefined} onClick={() => void beginEdit()}>修改</button>
          </> : <>
            <button className="btn small primary" disabled={Boolean(saveDisabledReason)}
              aria-label={saveDisabledReason ? `保存前预览，暂不可用：${saveDisabledReason}` : '保存前预览'}
              onClick={() => void previewSave()}>保存前预览</button>
            <button className="btn small ghost" onClick={requestDiscard}>{dirty ? '放弃修改' : '退出编辑'}</button>
          </>}
        </div>
      </header>

      {conflictOpen && <div className="shot-conflict-panel" role="region" aria-label="口播冲突修复">
        <p>台词与高级时间轴分别发生了变化。选择前会先计算视频、成片与重新确认影响。</p>
        <div className="shot-conflict-actions"><button className="btn small primary" onClick={() => void previewConflictResolution('rebuild_timeline_from_dialogues')}>以台词为准</button>
          <button className="btn small" onClick={() => void previewConflictResolution('rebuild_dialogues_from_timeline')}>以时间轴为准</button>
          <button className="btn small ghost" onClick={() => setConflictOpen(false)}>稍后处理</button></div>
      </div>}

      {!!shot.qa_warnings?.length && <details className="shot-drafts"><summary>质量优化建议（{shot.qa_warnings.length}）</summary>
        <ul>{shot.qa_warnings.map((item, index) => <li key={`${item}-${index}`}>{storyboardGateIssueLabel(item)}</li>)}</ul>
      </details>}

      {!!saveErrors.length && <div className="shot-validation-summary" role="alert"><b>修改尚未通过，共 {saveErrors.length} 个问题</b><ul>{saveErrors.map((item, index) => <li key={`${item}-${index}`}>{storyboardGateIssueLabel(item)}</li>)}</ul>
        <button className="btn small ghost" onClick={() => void copyDraft()}>复制我的草稿</button>
        <button className="btn small ghost" onClick={() => setReloadLatestOpen(true)}>重新载入最新版</button></div>}

      {!!drafts.length && edit && <details className="shot-drafts"><summary>失败草稿（{drafts.length}）</summary>{drafts.map(draft => <div key={draft.id} className="shot-draft-row">
        <div><b>{new Date(draft.created_at * 1000).toLocaleString()}</b><small>{draft.baseline_artifact_ids.length ? '基于较早分镜内容' : '未记录保存基线'} · {storyboardGateIssueLabel(draft.issues[0] || '业务校验未通过')}</small></div>
        <button className="btn small" disabled={dirty}
          aria-label={dirty ? '继续编辑此失败草稿，暂不可用：请先保存或放弃当前修改' : '继续编辑此失败草稿'}
          onClick={() => { setEdit({ ...cloneShot(shot), ...draft.content } as Shot); setSaveErrors(draft.issues) }}>继续编辑</button>
        <button className="btn small ghost" onClick={() => setDiscardDraftId(draft.id)}>丢弃</button>
      </div>)}</details>}

      {edit ? (
        <div className="shot-edit-form">
          <section aria-labelledby={fieldId(shot.id, 'basic-title')}>
            <h3 id={fieldId(shot.id, 'basic-title')}>常用修改</h3>
            <div className="shot-edit-grid">
              <label htmlFor={fieldId(shot.id, 'duration')}>时长<select id={fieldId(shot.id, 'duration')} value={edit.duration_s} onChange={event => setEdit({ ...edit, duration_s: Number(event.target.value) })}>{DURATIONS.map(value => <option key={value} value={value}>{value}s</option>)}</select><small>{edit.duration_s > 5 ? '保存后进入自动与规则审核，通过前不会发布' : '标准时长'}</small></label>
              <label htmlFor={fieldId(shot.id, 'size')}>景别<select id={fieldId(shot.id, 'size')} value={edit.shot_size} onChange={event => setEdit({ ...edit, shot_size: event.target.value })}>{SIZES.map(value => <option key={value}>{value}</option>)}</select></label>
              <label htmlFor={fieldId(shot.id, 'angle')}>角度<input id={fieldId(shot.id, 'angle')} value={edit.camera_angle ?? ''} placeholder="平视 / 过肩 / 低角度 / 俯拍" onChange={event => setEdit({ ...edit, camera_angle: event.target.value })} /></label>
              <label htmlFor={fieldId(shot.id, 'move')}>运镜<select id={fieldId(shot.id, 'move')} value={edit.camera_move} onChange={event => setEdit({ ...edit, camera_move: event.target.value })}>{MOVES.map(value => <option key={value}>{value}</option>)}</select></label>
              <label htmlFor={fieldId(shot.id, 'transition')}>转场<select id={fieldId(shot.id, 'transition')} value={edit.transition} onChange={event => setEdit({ ...edit, transition: event.target.value })}>{TRANSITIONS.map(value => <option key={value}>{value}</option>)}</select></label>
            </div>
            <div className="shot-edit-grid">
              <label htmlFor={fieldId(shot.id, 'scene-time')}>时间标签<input id={fieldId(shot.id, 'scene-time')} list={fieldId(shot.id, 'scene-time-options')} value={edit.scene_time ?? ''} placeholder="黄昏 / 18:30 / 次日清晨" onChange={event => setEdit({ ...edit, scene_time: event.target.value })} /><datalist id={fieldId(shot.id, 'scene-time-options')}>{SCENE_TIMES.map(value => <option key={value} value={value} />)}</datalist><small>可输入早中晚、黄昏或具体时刻</small></label>
              <label htmlFor={fieldId(shot.id, 'scene-name')}>场景图标签<input id={fieldId(shot.id, 'scene-name')} list={fieldId(shot.id, 'scene-name-options')} value={edit.scene_name ?? ''} required placeholder="选择或输入接近的场景名" onChange={event => setEdit({ ...edit, scene_name: event.target.value })} /><datalist id={fieldId(shot.id, 'scene-name-options')}>{sceneOptions.map(value => <option key={value} value={value} />)}</datalist><small>保存时可模糊匹配，命中后会归一成场景库规范名</small></label>
            </div>
            <label htmlFor={fieldId(shot.id, 'action')}>画面与动作<textarea id={fieldId(shot.id, 'action')} rows={3} value={edit.action_desc} required onChange={event => setEdit({ ...edit, action_desc: event.target.value })} /></label>
            <label htmlFor={fieldId(shot.id, 'purpose')}>本镜作用<textarea id={fieldId(shot.id, 'purpose')} rows={2} value={edit.purpose ?? ''} onChange={event => setEdit({ ...edit, purpose: event.target.value })} /></label>
            <label htmlFor={fieldId(shot.id, 'resulting-change')}>本镜结果变化<textarea id={fieldId(shot.id, 'resulting-change')} rows={2} value={edit.resulting_change ?? ''} onChange={event => setEdit({ ...edit, resulting_change: event.target.value })} /></label>
            <label htmlFor={fieldId(shot.id, 'camera-motivation')}>摄影动机<textarea id={fieldId(shot.id, 'camera-motivation')} rows={2} value={edit.camera_motivation ?? ''} onChange={event => setEdit({ ...edit, camera_motivation: event.target.value })} /></label>
            <div className="shot-edit-grid frames"><label htmlFor={fieldId(shot.id, 'first')}>{firstFrameLabel}<textarea id={fieldId(shot.id, 'first')} rows={2} value={effectiveFirstFrameDesc} readOnly={inputStrategy.kind === 'previous_video_tail'} onChange={event => setEdit({ ...edit, first_frame_desc: event.target.value })} /><small>{inputStrategy.detail}</small></label>
              <label htmlFor={fieldId(shot.id, 'last')}>结束状态（仅作视频目标）<textarea id={fieldId(shot.id, 'last')} rows={2} value={edit.last_frame_desc} onChange={event => setEdit({ ...edit, last_frame_desc: event.target.value })} /><small>不生成静态尾帧参考图</small></label></div>
          </section>

          <section><h3>画面角色与信息点</h3><fieldset className="token-picker"><legend>画面角色（人物谱）</legend>{characterOptions.map(name => <label key={name}><input type="checkbox" checked={edit.characters.includes(name)} onChange={() => toggleCharacter(name)} />{name}</label>)}{!characterOptions.length && <small>人物谱暂无可选角色</small>}</fieldset>
            {!edit.characters.length && <p className="field-error" role="alert">至少选择一个画面角色；“缺角色”会从这里修复。</p>}
            <fieldset className="token-picker"><legend>声音角色</legend>{characterOptions.map(name => <label key={name}><input type="checkbox" checked={(edit.audio_cast ?? []).includes(name)} onChange={() => {
              const active = (edit.audio_cast ?? []).includes(name); setEdit({ ...edit, audio_cast: active ? (edit.audio_cast ?? []).filter(item => item !== name) : [...(edit.audio_cast ?? []), name] })
            }} />{name}</label>)}</fieldset>
            <fieldset className="token-picker"><legend>本镜新信息（来自剧本清单）</legend>{informationOptions.map(item => <label key={item.id}><input type="checkbox" checked={(edit.new_information_ids ?? []).includes(item.id)} onChange={() => toggleInformation(item.id)} />{item.label}</label>)}{!informationOptions.length && <small>本集剧本没有结构化信息清单</small>}</fieldset>
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

          <section><h3>原文依据（只从本集授权原文框选，不送视频模型）</h3>
            <p className="source-bound-preview">当前依据：{edit.source_excerpt || '尚未绑定'} {shot.source_binding && !sourceBinding ? `· 已绑定第 ${shot.source_binding.chapter_idx} 章原文片段` : ''}</p>
            {sourceChapters.length ? <><label htmlFor={fieldId(shot.id, 'source-chapter')}>授权章节<select id={fieldId(shot.id, 'source-chapter')} value={sourceChapterId ?? ''} onChange={event => setSourceChapterId(Number(event.target.value))}>{sourceChapters.map(chapter => <option key={chapter.id} value={chapter.id}>第 {chapter.idx} 章 · {chapter.title}</option>)}</select></label>
              <label htmlFor={fieldId(shot.id, 'source-text')}>在原文中拖选连续片段<textarea ref={sourceTextRef} id={fieldId(shot.id, 'source-text')} rows={7} readOnly value={sourceChapters.find(item => item.id === sourceChapterId)?.content ?? ''} /></label>
              <button className="btn small" onClick={bindSelectedSource}>使用当前选区作为证据</button></> : <p className="field-error">本集原文暂不可用；现有证据只读，不能修改。</p>}
          </section>

          <details open={advancedOpen} onToggle={event => setAdvancedOpen(event.currentTarget.open)} className="shot-advanced"><summary>高级：连续性与制作约束</summary>
            <div className="neighbor-continuity" aria-label="邻镜连续性对照">
              <button type="button" disabled={!previous} aria-label={previous ? `切换到前镜 ${previous.shot_no}` : '切换到前镜，暂不可用：当前已是第一镜'}
                onClick={() => previous && onSelect(previous.id)}><b>{previous ? `前镜 ${previous.shot_no} · 离开` : '首镜边界'}</b><span>{previous?.state_out || previous?.last_frame_desc || '无前镜依赖'}</span></button>
              <div className={prevConflict || nextConflict ? 'current conflict' : 'current'}><b>本镜 {shot.shot_no}</b><span>{edit.state_in || '未设进入'} → {edit.state_out || '未设离开'}</span></div>
              <button type="button" disabled={!next} aria-label={next ? `切换到后镜 ${next.shot_no}` : '切换到后镜，暂不可用：当前已是收尾镜'}
                onClick={() => next && onSelect(next.id)}><b>{next ? `后镜 ${next.shot_no} · 进入` : '末镜边界'}</b><span>{next?.state_in || next?.first_frame_desc || '无后镜依赖'}</span></button>
            </div>
            {(prevConflict || nextConflict) && <p className="field-error" role="alert">{prevConflict ? '前镜离开与本镜进入不一致。' : ''}{nextConflict ? '本镜离开与后镜进入不一致。' : ''} 可点击邻镜定位。</p>}
            <div className="shot-edit-grid continuity"><label htmlFor={fieldId(shot.id, 'state-in')}>进入状态<textarea id={fieldId(shot.id, 'state-in')} value={edit.state_in ?? ''} onChange={event => setEdit({ ...edit, state_in: event.target.value })} /></label>
              <label htmlFor={fieldId(shot.id, 'primary-action')}>镜头动作<textarea id={fieldId(shot.id, 'primary-action')} value={edit.primary_action ?? ''} onChange={event => setEdit({ ...edit, primary_action: event.target.value })} /></label>
              <label htmlFor={fieldId(shot.id, 'state-out')}>离开状态<textarea id={fieldId(shot.id, 'state-out')} value={edit.state_out ?? ''} onChange={event => setEdit({ ...edit, state_out: event.target.value })} /></label></div>
            <label htmlFor={fieldId(shot.id, 'continuity-mode')}>与上镜关系<select id={fieldId(shot.id, 'continuity-mode')} value={edit.continuity_mode ?? ''} onChange={event => setEdit({ ...edit, continuity_mode: event.target.value })}><option value="">自动/未设定</option>{Object.entries(CONTINUITY_MODES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          </details>

          {status.feature_flags?.structure_edit !== false && <section className="shot-structure-actions"><h3>镜头结构</h3><p>{structureDisabledReason || '所有结构动作都会先展示镜号、收尾镜、相邻校验和媒体失效影响。'}</p><div>
            <button className="btn small" disabled={Boolean(structureDisabledReason)}
              aria-label={structureDisabledReason ? `在当前镜后新增，暂不可用：${structureDisabledReason}` : '在当前镜后新增'}
              onClick={() => void onStructure('add_after')}>在当前镜后新增</button>
            <button className="btn small" disabled={Boolean(structureDisabledReason)}
              aria-label={structureDisabledReason ? `复制为下一镜，暂不可用：${structureDisabledReason}` : '复制为下一镜'}
              onClick={() => void onStructure('duplicate_after')}>复制为下一镜</button>
            <button className="btn small" disabled={Boolean(structureDisabledReason) || !previous}
              aria-label={structureDisabledReason ? `前移，暂不可用：${structureDisabledReason}` : !previous ? '前移，暂不可用：当前已是第一镜' : '前移'}
              onClick={() => void onStructure('move', Math.max(0, shot.shot_no - 2))}>前移</button>
            <button className="btn small" disabled={Boolean(structureDisabledReason) || !next}
              aria-label={structureDisabledReason ? `后移，暂不可用：${structureDisabledReason}` : !next ? '后移，暂不可用：当前已是收尾镜' : '后移'}
              onClick={() => void onStructure('move', shot.shot_no)}>后移</button>
            <button className="btn small danger" disabled={Boolean(structureDisabledReason)}
              aria-label={structureDisabledReason ? `删除镜头，暂不可用：${structureDisabledReason}` : '删除镜头'}
              onClick={() => void onStructure('delete')}>删除镜头</button></div></section>}

          <footer className="shot-edit-sticky"><span aria-live="polite">{dirty ? `${Object.keys(changes).length} 个字段域有变化` : '尚无改动'}</span><button className="btn ghost" onClick={requestDiscard}>{dirty ? '放弃修改' : '退出编辑'}</button><button className="btn primary" disabled={Boolean(saveDisabledReason)}
            aria-label={saveDisabledReason ? `保存前预览，暂不可用：${saveDisabledReason}` : '保存前预览'}
            onClick={() => void previewSave()}>保存前预览</button></footer>
        </div>
      ) : (
        <div className="shot-review-body">
          <section className="shot-overview"><div className="shot-visual-brief"><span className="shot-section-label">画面设计</span><h2>{current.scene_name || current.scene_setting}</h2>{current.scene_time && <small>时间：{current.scene_time}</small>}<p>{current.action_desc}</p></div>
            <dl className="shot-specs"><div><dt>时长</dt><dd>{current.duration_s}s</dd></div><div><dt>景别</dt><dd>{current.shot_size}</dd></div><div><dt>角度</dt><dd>{current.camera_angle || '平视'}</dd></div><div><dt>运镜</dt><dd>{current.camera_move}</dd></div><div><dt>转场</dt><dd>{current.transition}</dd></div></dl></section>
          <div className="shot-frame-pair shot-continuity-chain" aria-label="镜头状态链"><div className="shot-frame-card"><b>进入状态</b><p>{current.state_in || current.first_frame_desc || '未设置'}</p></div><span aria-hidden>→</span><div className="shot-frame-card"><b>镜头动作</b><p>{current.primary_action || current.action_desc}</p></div><span aria-hidden>→</span><div className="shot-frame-card"><b>离开状态</b><p>{current.state_out || current.last_frame_desc || '未设置'}</p></div></div>
          <section className="shot-spoken-panel"><header className="shot-context-head"><b>本镜台词</b><span>{currentChars} / {spokenLimit} 字</span></header>{overCapacity && <p className="shot-spoken-warn">口播已超出本镜容量</p>}{current.dialogues.length ? current.dialogues.map((line, index) => <div key={index} className="shot-audio-line"><b>{line.speaker}<small>{line.emotion}</small></b><p>「{line.line}」</p></div>) : <p>本镜无台词</p>}</section>
          <section className="shot-context-panel"><h3>镜头要素</h3><dl className="shot-context-grid"><div><dt>本镜作用</dt><dd>{current.purpose || '旧版镜头未单列'}</dd></div><div><dt>结果变化</dt><dd>{current.resulting_change || current.state_out || '未单列'}</dd></div><div><dt>摄影动机</dt><dd>{current.camera_motivation || '旧版镜头未单列'}</dd></div><div><dt>画面角色</dt><dd>{current.characters.join('、') || '无'}</dd></div><div><dt>声音角色</dt><dd>{current.audio_cast?.join('、') || '无'}</dd></div><div><dt>本镜新信息</dt><dd>{current.new_information_items?.map(item => item.content).join('；') || '无'}</dd></div></dl></section>
          <div className="shot-detail-tabs" role="tablist" aria-label="镜头详情"><button id={`shot-detail-tab-${shot.id}-frames`} type="button" role="tab" aria-selected={detailTab === 'frames'} aria-controls={detailPanelId} tabIndex={detailTab === 'frames' ? 0 : -1} className={detailTab === 'frames' ? 'active' : ''} onClick={() => setDetailTab('frames')} onKeyDown={event => { if (event.key === 'ArrowRight' || event.key === 'End') { event.preventDefault(); focusDetailTab('script') } }}>生成起点与结束状态</button><button id={`shot-detail-tab-${shot.id}-script`} type="button" role="tab" aria-selected={detailTab === 'script'} aria-controls={detailPanelId} tabIndex={detailTab === 'script' ? 0 : -1} className={detailTab === 'script' ? 'active' : ''} onClick={() => setDetailTab('script')} onKeyDown={event => { if (event.key === 'ArrowLeft' || event.key === 'Home') { event.preventDefault(); focusDetailTab('frames') } }}>声音与原文</button></div>
          {detailTab === 'frames' ? <div id={detailPanelId} className="shot-frame-pair" role="tabpanel" aria-labelledby={`shot-detail-tab-${shot.id}-frames`}><div className="shot-frame-card"><b>01 · {inputStrategy.kind === 'previous_video_tail' ? '上一视频真实尾帧' : inputStrategy.kind === 'reference_video' ? '参考视频起点' : '图库生成起点'}</b><p>{effectiveFirstFrameDesc || '暂未描述'}</p><small>{inputStrategy.detail}</small></div><span aria-hidden>→</span><div className="shot-frame-card"><b>02 · 视频结束状态目标</b><p>{current.last_frame_desc || '暂未描述'}</p><small>不生成静态尾帧参考图</small></div></div>
            : <div id={detailPanelId} className="shot-script-grid" role="tabpanel" aria-labelledby={`shot-detail-tab-${shot.id}-script`}><div className="shot-script-copy"><b>原文依据（不送视频模型）</b><p>{current.source_excerpt || '暂无对应原文'}</p><small>{current.source_binding ? `已绑定第 ${current.source_binding.chapter_idx} 章原文片段` : '当前只保留原文内容，确认前会检查来源位置'}</small></div><div className="shot-audio-copy">{current.dialogues.map((line, index) => <div key={index} className="shot-audio-line"><b>{line.speaker}</b><p>「{line.line}」</p></div>)}</div></div>}
        </div>
      )}

      <ImpactDialog open={!!impact || impactLoading || !!impactError} title={`保存镜 ${shot.shot_no} 的精确影响`}
        impact={impact} loading={impactLoading} error={impactError} confirmLabel="批准影响并保存"
        knownEffects={impact ? [`重新校验：${((impact as unknown as { revalidation_shots?: number[] }).revalidation_shots ?? []).join('、') || '本镜与相邻镜'}`, '失败时只保留工作草稿，不覆盖发布版'] : []}
        onClose={() => { setImpact(null); setImpactError(null); setImpactLoading(false) }} onConfirm={() => void save()} />
      <ImpactDialog
        open={!!conflictImpact || conflictImpactLoading || !!conflictImpactError}
        title={`解决镜 ${shot.shot_no} 的口播冲突`}
        impact={conflictImpact}
        loading={conflictImpactLoading}
        error={conflictImpactError}
        confirmLabel="批准影响并解决冲突"
        knownEffects={[
          conflictImpact?.choice === 'rebuild_timeline_from_dialogues'
            ? '将以当前台词重建口播时间轴'
            : '将以当前口播时间轴重建台词',
          '关联媒体失效后需要重新生成，不会沿用旧视频',
        ]}
        onClose={() => {
          setConflictImpact(null)
          setConflictImpactError(null)
          setConflictImpactLoading(false)
        }}
        onConfirm={() => void resolveConflict()}
      />
      <Modal open={discardEditOpen} title={`放弃镜 ${shot.shot_no} 的未保存修改？`} onClose={() => setDiscardEditOpen(false)}
        actions={<><button className="btn" onClick={() => setDiscardEditOpen(false)}>继续编辑</button><button className="btn danger" onClick={() => {
          setDiscardEditOpen(false)
          discard()
        }}>确认放弃修改</button></>}>
        <p>将丢失当前 {Object.keys(changes).length} 个字段域的输入；发布版、失败草稿和下游产物不会变化。</p>
      </Modal>
      <Modal open={reloadLatestOpen} title={`重新载入镜 ${shot.shot_no} 的最新版？`} onClose={() => setReloadLatestOpen(false)}
        actions={<><button className="btn" onClick={() => setReloadLatestOpen(false)}>保留当前输入</button><button className="btn danger" onClick={() => {
          setReloadLatestOpen(false)
          discard()
          onChanged()
        }}>放弃输入并重新载入</button></>}>
        <p>当前输入和未提交的修改会被清除，再从已发布的最新版加载；失败草稿仍会保留。</p>
      </Modal>
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
