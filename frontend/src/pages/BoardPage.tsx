import { ReactNode, useEffect, useId, useMemo, useRef, useState } from 'react'
import { api, ApiError, Bible, Episode, Shot, StoryboardPackResources, StoryboardStatus } from '../api'
import { useEpisode, useNav, useProject } from '../App'
import EpisodeCrumb from '../components/EpisodeCrumb'
import { ItemTaskTimer, ServerTaskTimer } from '../components/TaskTimer'
import DecisionDialog from '../components/DecisionDialog'
import QueryState from '../components/QueryState'
import { useFocusTrap } from '../hooks/useFocusTrap'
import { storyboardTaskNotice } from '../lib/productionNotices'
import { compressSegmentIndexes } from '../lib/segmentIndexes'
import { findPortraitImage, findSceneReferenceImage } from '../lib/bibleAssets'
import "../styles/BoardPage.css";

// 视频生成模型选择：与生成台强绑定（app/video_providers.py 的 provider key）。
// 两个供应商的提示词方言互不兼容，这里只列这两个已接入的模型，不做自动发现——
// 新增供应商需要先接入 app/video_providers.py，再在这里补一条。
const VIDEO_MODEL_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'hiagent', label: 'Seedance 2.0' },
  { value: 'minimax_h3', label: 'MiniMax H3' },
]

function videoModelLabel(value: string): string {
  return VIDEO_MODEL_OPTIONS.find(option => option.value === value)?.label ?? value
}

// StoryboardPackSegment.target_model 用的是冻结契约自己的词表（"seedance_2" |
// "minimax_h3"，见 app/production/storyboard_pack.py _dialect_for_target_video_model），
// 与上面 VIDEO_MODEL_OPTIONS 的供应商 key（"hiagent" | "minimax_h3"）不是同一套
// 词表——两者恰好共享 "minimax_h3" 这一个值是巧合，不能假设通用，不能复用
// videoModelLabel 给段落记录查标签。
const STORYBOARD_PACK_TARGET_MODEL_LABELS: Record<string, string> = {
  seedance_2: 'Seedance 2.0',
  minimax_h3: 'MiniMax H3',
}
export function storyboardPackTargetModelLabel(targetModel: string): string {
  return STORYBOARD_PACK_TARGET_MODEL_LABELS[targetModel] ?? targetModel
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

export type StoryboardPackBeatOverviewEntry = { beat_id: string; summary: string; segment_nos: number[] }

/**
 * 节拍概览：按每段自带的 beats（自包含 beat_id/summary/segment_indexes，见
 * api.ts 的 StoryboardPackSegmentBeat 注释）反推"哪个节拍进了哪一段、在讲什么"。
 * 不再读裸 beat_ids——那是没有摘要的过渡期字段，展示一律改用 beats。
 * 同一 beat_id 在多段间重复出现时，摘要取首次遇到的非空值，不逐段覆盖。
 */
export function storyboardPackBeatOverview(shots: Shot[]): StoryboardPackBeatOverviewEntry[] {
  const byBeat = new Map<string, { summary: string; segmentNos: Set<number> }>()
  for (const shot of shots) {
    const segment = shot.storyboard_pack_segment
    if (!segment) continue
    for (const beat of segment.beats ?? []) {
      const entry = byBeat.get(beat.beat_id) ?? { summary: '', segmentNos: new Set<number>() }
      if (!entry.summary && beat.summary) entry.summary = beat.summary
      entry.segmentNos.add(segment.segment_no)
      byBeat.set(beat.beat_id, entry)
    }
  }
  return [...byBeat.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([beat_id, entry]) => ({ beat_id, summary: entry.summary, segment_nos: [...entry.segmentNos].sort((a, b) => a - b) }))
}

export type StoryboardPackResourceGapSummary = {
  charactersLinked: number
  charactersTotal: number
  scenesLinked: number
  scenesTotal: number
  propsTotal: number
}

/**
 * 要求 2：一眼看出这一集有多少素材没映射上。linked = portrait_id/scene_reference_id
 * 非空；未 link 的与全部 props 都只有文字描述（世界书没有道具素材库）。按段落
 * 出现次数计数，不去重——同一角色在不同段落可能一段有素材、另一段没有，逐段计数
 * 才能反映"这一集"整体的映射缺口，而不是掩盖某几段的缺失。
 */
export function storyboardPackResourceGapSummary(shots: Shot[]): StoryboardPackResourceGapSummary {
  let charactersLinked = 0
  let charactersTotal = 0
  let scenesLinked = 0
  let scenesTotal = 0
  let propsTotal = 0
  for (const shot of shots) {
    const resources = shot.storyboard_pack_segment?.resources
    if (!resources) continue
    for (const character of resources.characters ?? []) {
      charactersTotal += 1
      if (character.portrait_id) charactersLinked += 1
    }
    for (const scene of resources.scenes ?? []) {
      scenesTotal += 1
      if (scene.scene_reference_id) scenesLinked += 1
    }
    propsTotal += resources.props?.length ?? 0
  }
  return { charactersLinked, charactersTotal, scenesLinked, scenesTotal, propsTotal }
}

/**
 * 要求 4：degraded_capabilities 必须显示出来，不许静默吞掉，且能据此导出后期文字
 * 合成清单。把每段的降级项摊平成一行一条、带段号回指的纯文本；没有任何降级项时
 * 返回空串，调用方据此显示"本集无降级项"而不是复制出一段空文本。
 */
export function storyboardPackDegradedCapabilitiesExportText(shots: Shot[]): string {
  const lines: string[] = []
  for (const shot of shots) {
    const segment = shot.storyboard_pack_segment
    if (!segment?.degraded_capabilities?.length) continue
    for (const item of segment.degraded_capabilities) {
      lines.push(`第 ${segment.segment_no} 段：${item}`)
    }
  }
  return lines.join('\n')
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

type VideoModelSwitchConfirm = {
  requested_target_video_model: string
  current_target_video_model: string
  prompt_artifact_count: number
}

// 分镜台只剩段视图一条渲染路径（旧的逐镜编辑连同它绑定的经典字段形状已整块拆除，
// 2026-08-26 用户拍板：测试期没有需要兼容的重要数据）。一个 15 秒段 = shots 表
// 一行，段内 3-4 镜写进 prompt_text 文本、不拆成独立数据行；storyboard_pack_segment
// 非 null 是这一行有内容可展示的唯一标记，见 api.ts 的 StoryboardPackSegment 注释。
export function isStoryboardPackSegmentShot(shot: Shot): boolean {
  return shot.storyboard_pack_segment != null
}

export function isStoryboardProblemShot(shot: Shot): boolean {
  const status = shot.storyboard_evidence?.status || ''
  return shot.spoken_contract_status === 'conflict'
    || Boolean(shot.legacy_unvalidated)
    || ['candidate', 'needs_revision', 'rejected', 'stale', 'superseded'].includes(status)
    || Boolean(shot.preflight_errors?.length)
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
    .replace(/^shots\[\d+\]\(shot_no=(\d+)\)\./, '第 $1 段：')
    .replace(/^shot_no=(\d+)\./, '第 $1 段：')
    .replaceAll('action_desc', '画面动作')
    .replaceAll('primary_action', '镜头动作')
    .replaceAll('first_frame_desc', '生成起点')
    .replaceAll('last_frame_desc', '结束状态')
    .replaceAll('QA', '质检')
    .replaceAll('门禁', '必检项')
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
    go_screenplay: '先去映射台',
    generate_storyboard: '生成视频提示词',
    view_progress: '查看任务详情',
    resume_storyboard: finalizingEvidence ? '完成发布证据' : '继续生成视频提示词',
    confirm_storyboard: '确认视频提示词',
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
  const target = status.planned_shots > 0 ? `${status.planned_shots} 段` : '待确定'
  const summary = `目标 ${target} · 工作副本 ${working} 段 · 已校验 ${validated} 段`

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
      detail: `当前 ${working} 段已完成逐段校验，但整集仍有 ${gateIssueCount} 个确认门禁问题。继续任务会重开整集修复，不是从第 ${resumeFrom} 段续写；修复候选通过前不会覆盖现有段落。`,
    }
  }
  if (status.resume_mode === 'finalize_evidence') {
    return {
      summary,
      detail: '段落内容与整集硬门禁已通过；继续任务只会完成冷观众审读、校准校验和发布证据签发，不会改写现有段落。',
    }
  }
  const finalDraftNote = status.final_shot_valid
    ? '工作副本中的收尾标记不代表整集已通过。'
    : ''
  if (pending > 0) {
    return {
      summary,
      detail: `第 ${validated + 1}–${working} 段仍待校验；任务将从第 ${resumeFrom} 段继续修复。人工确认前，轨道中的内容都只是工作副本。${finalDraftNote}`,
    }
  }
  return {
    summary,
    detail: `当前 ${validated} 段已通过逐段校验；任务将从第 ${resumeFrom} 段继续。整集门禁通过并经人工确认后才会交给生成台。${finalDraftNote}`,
  }
}

export function storyboardEmptyCopy(status: StoryboardStatus): string {
  if (status.state === 'no_screenplay') return '尚无可用于生成视频提示词的映射结果，请先去映射台。'
  if (status.state === 'running') return '首批视频提示词正在生成与逐段校验，通过后会自动展示。'
  if (status.state === 'failed') return '视频提示词任务未完成，尚无通过校验的工作段落；请查看原因后继续处理。'
  if (status.state === 'paused') return '视频提示词任务已暂停，尚无通过校验的工作段落。'
  return '映射已就绪，尚未生成视频提示词。'
}

export function storyboardStartPreviewCopy(preview: StartPreview): {
  title: string
  confirmLabel: string
  summary: string
  detail: string
} {
  if (preview.action === 'create') {
    return {
      title: '生成本集视频提示词',
      confirmLabel: '开始生成',
      summary: '从空白开始生成本集分段视频提示词。',
      detail: preview.planned_shots
        ? `计划 ${preview.planned_shots} 段。`
        : '任务启动后会先规划分段数量。',
    }
  }
  if (preview.resume_mode === 'repair_existing') {
    const issueCount = preview.current_gate_issue_count
      ?? preview.current_gate_issues?.length
      ?? preview.repair?.last_issue_messages.length
      ?? 0
    return {
      title: '继续修复视频提示词',
      confirmLabel: '开始修复',
      summary: `现有 ${preview.kept_validated_shots} 段保持不变，重新校验并修复${issueCount ? ` ${issueCount} 个` : ''}确认门禁问题。`,
      detail: `这是重开整集修复，不是从第 ${preview.checkpoint.resume_from_shot} 段续写；候选通过前不会覆盖现有段落。`,
    }
  }
  if (preview.resume_mode === 'finalize_evidence') {
    return {
      title: '完成视频提示词发布证据',
      confirmLabel: '继续审读发布',
      summary: `现有 ${preview.kept_validated_shots} 段已完成结构与逐段校验。`,
      detail: '将保留全部段落，仅继续冷观众审读、校准校验与发布证据签发。',
    }
  }
  return {
    title: '继续生成视频提示词',
    confirmLabel: '继续任务',
    summary: `从第 ${preview.checkpoint.resume_from_shot} 段继续；前 ${preview.kept_validated_shots} 段已通过逐段校验。`,
    detail: preview.planned_shots
      ? `计划 ${preview.planned_shots} 段${preview.remaining_shots != null ? `，剩余 ${preview.remaining_shots} 段` : ''}。`
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
    return { label: '已校验', className: 'checkpoint-safe', title: '本轮已通过逐段校验；整集仍需通过门禁并由人工确认' }
  }
  return { label: '待校验', className: 'checkpoint-pending', title: '仍在工作副本中，继续任务时可能更新' }
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
  const mappingReady = status.screenplay_available

  return (
    <section className={`storyboard-launch card ${mappingReady ? 'is-ready' : 'needs-screenplay'}`} aria-labelledby="storyboard-launch-title">
      <header className="storyboard-launch-head">
        <div className="storyboard-launch-status">
          <span className="storyboard-launch-mark" aria-hidden="true">词</span>
          <div><b>{mappingReady ? '映射已就绪' : '等待映射'}</b><small>{mappingReady ? '视频提示词尚未生成' : '完成映射后才能生成视频提示词'}</small></div>
        </div>
      </header>
      <div className="storyboard-launch-action" aria-label="下一步">
        <h2 id="storyboard-launch-title">{mappingReady ? '生成本集视频提示词' : '请先完成本集映射'}</h2>
        <p>{mappingReady
          ? `将按照《${episode.title}》当前映射结果生成分段视频提示词，生成后可逐段复制。`
          : '当前没有可用的映射结果，完成映射后再返回这里开始。'}</p>
        <button id="storyboard-primary-action" type="button" className="btn primary storyboard-launch-button" disabled={busy}
          aria-label={busy ? '生成视频提示词，暂不可用：正在准备' : mappingReady ? '生成视频提示词' : '先去映射台'}
          onClick={onPrimary}>
          {busy ? '正在准备…' : mappingReady ? '生成视频提示词' : '先去映射台'}
        </button>
        {mappingReady && <small>只生成视频提示词，不会自动提交付费视频。</small>}
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
      <div><b>全链路叙事一致性</b><span>同一事件、动作、人物认知与观众理解合同贯穿映射和分镜</span></div>
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
  const { episodeId, go, projectId, toast } = useNav()
  const { data: ep, refresh, error, status: queryErrorStatus, loading } = useEpisode(episodeId!, 'board')
  // 分镜台 2.0.0 段落资源清单里的 portrait_id/scene_reference_id 指向项目人物谱/
  // 场景库，需要同一份 bible 才能查缩略图；口径与用法都照抄 ScriptPage.tsx（同一个
  // 项目、一次性拉取、不轮询）。
  const { data: project } = useProject(projectId!, 0, 'bible')
  const [busy, setBusy] = useState(false)
  const [selectedShotId, setSelectedShotId] = useState<string | null>(null)
  const [onlyProblems, setOnlyProblems] = useState(false)
  const [recoveredStatus, setRecoveredStatus] = useState<StoryboardStatus | null>(null)
  const [startPreview, setStartPreview] = useState<StartPreview | null>(null)
  const [confirmPreview, setConfirmPreview] = useState<ConfirmPreview | null>(null)
  const [clearPreview, setClearPreview] = useState<StoryboardClearPreview | null>(null)
  const [videoModelConfirm, setVideoModelConfirm] = useState<VideoModelSwitchConfirm | null>(null)
  const [videoModelBusy, setVideoModelBusy] = useState(false)
  const timelineRef = useRef<HTMLDivElement>(null)
  const startPreviewTriggerRef = useRef<HTMLElement | null>(null)

  const shots = ep?.shots ?? []
  const visibleShots = useMemo(() => shots.filter(shot => {
    if (onlyProblems && !isStoryboardProblemShot(shot)) return false
    return true
  }), [shots, onlyProblems])
  // 节拍概览与素材缺口统计按全量 shots（不受筛选影响），反映"这一集"整体口径，
  // 不随段落筛选变化而变化。
  const packBeatOverview = useMemo(() => storyboardPackBeatOverview(shots), [shots])
  const packResourceGap = useMemo(() => storyboardPackResourceGapSummary(shots), [shots])
  const packDegradedExportText = useMemo(() => storyboardPackDegradedCapabilitiesExportText(shots), [shots])
  const copyPackDegradedExport = async () => {
    if (!packDegradedExportText) return
    if (!navigator.clipboard) {
      toast('当前浏览器无法访问剪贴板，请检查浏览器权限后重试', true)
      return
    }
    try {
      await navigator.clipboard.writeText(packDegradedExportText)
      toast('后期文字合成清单已复制')
    } catch {
      toast('复制失败，请允许浏览器访问剪贴板后重试', true)
    }
  }
  // 先在全量 shots 里按当前选中 id 解析，避免轮询刷新使选中段因"问题态"变化离开
  // visibleShots 时被静默跳到第一条；仅当尚无选中或该段确实被删除时，才回退到 visibleShots[0]。
  const selectedShot = shots.find(shot => shot.id === selectedShotId) ?? visibleShots[0]
  const selectedIndex = visibleShots.findIndex(shot => shot.id === selectedShot?.id)
  const selectionOutsideFilters = Boolean(
    selectedShot && visibleShots.length > 0 && !visibleShots.some(shot => shot.id === selectedShot.id),
  )
  const status = ep?.storyboard_status ?? recoveredStatus ?? (ep ? statusFallback(ep) : null)
  const taskNotice = ep ? storyboardTaskNotice(ep, status?.state) : null
  const hasActiveFilters = onlyProblems

  useEffect(() => {
    if (!selectedShot || selectedShot.id === selectedShotId) return
    setSelectedShotId(selectedShot.id)
  }, [selectedShot?.id, selectedShotId])

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
    setSelectedShotId(shotId)
  }

  const selectRelative = (offset: number) => {
    if (!visibleShots.length) return
    const next = Math.max(0, Math.min(visibleShots.length - 1, selectedIndex + offset))
    requestShotSelect(visibleShots[next].id)
  }

  const clearShotFilters = () => {
    setOnlyProblems(false)
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
  }, [visibleShots, selectedIndex, selectedShot?.id])

  if (!ep || !status) {
    return (
      <QueryState
        loading={loading}
        error={error}
        status={queryErrorStatus}
        hasData={false}
        objectName="分镜台"
        loadingText="正在加载分镜、段落列表与确认状态…"
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
        ? '已开始重新校验并修复现有视频提示词'
        : preview.action === 'resume' ? '已从安全检查点继续生成' : '视频提示词生成已开始',
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
      '视频提示词已确认，可以进入生成台',
    )
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
      `已清空 ${deletedShots} 个视频提示词段落及其下游资源，映射结果已保留`,
    )
    if (!result) return
    setSelectedShotId(null)
    clearShotFilters()
  }

  const pauseStoryboard = async () => {
    const result = await run(
      () => api.post(`/episodes/${ep.id}/storyboard/cancel`, {}),
      '视频提示词任务已暂停，工作段落和安全检查点已保留',
    )
  }

  const currentVideoModel = ep.target_video_model || 'hiagent'

  // 与生成台强绑定：切换视频模型不做静默转换。首次提交不带 confirm，若本集已有
  // 视频生成产物后端会用 409 + VIDEO_MODEL_SWITCH_REQUIRES_CONFIRMATION 挡下来，
  // 这里弹二次确认；确认后带 confirm_clear_prompts=true 重新提交才真正执行清空。
  const submitVideoModel = async (target: string, confirmClearPrompts?: boolean) => {
    if (target === currentVideoModel) return
    setVideoModelBusy(true)
    try {
      const result = await api.post(`/episodes/${ep.id}/video-model`, {
        target_video_model: target,
        ...(confirmClearPrompts ? { confirm_clear_prompts: true } : {}),
      }) as { changed: boolean; cleared_videos: number; target_video_model: string }
      setVideoModelConfirm(null)
      if (result.changed) {
        toast(result.cleared_videos
          ? `已切换为 ${videoModelLabel(result.target_video_model)}，清空了 ${result.cleared_videos} 个旧方言视频`
          : `已切换为 ${videoModelLabel(result.target_video_model)}`)
      }
      await refresh({ force: true })
    } catch (caught) {
      const apiError = caught as ApiError
      if (apiError.code === 'VIDEO_MODEL_SWITCH_REQUIRES_CONFIRMATION') {
        const detail = apiError.detail as Partial<VideoModelSwitchConfirm> | undefined
        setVideoModelConfirm({
          requested_target_video_model: detail?.requested_target_video_model ?? target,
          current_target_video_model: detail?.current_target_video_model ?? currentVideoModel,
          prompt_artifact_count: detail?.prompt_artifact_count ?? 0,
        })
      } else {
        toast(apiError.message, true)
      }
    } finally {
      setVideoModelBusy(false)
    }
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
        <h1>分镜台 <span className="sub">《{ep.title}》 · 安全审阅分段视频提示词并交接下游</span></h1>
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
              <small>{progressCopy.summary}{terminalFinalShot ? ' · 收尾段有效' : ''}</small>
            </div>
          </div>
          <div className="board-action-group">
            <label title="切换本集绑定的视频生成模型；两个供应商提示词方言不兼容，与生成台强绑定">
              <span>视频模型</span>
              <select
                aria-label="切换本集视频生成模型"
                disabled={busy || videoModelBusy}
                value={currentVideoModel}
                onChange={event => void submitVideoModel(event.target.value)}
              >
                {VIDEO_MODEL_OPTIONS.map(option => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>
            {toolbarActions.pause ? <>
              <button id="storyboard-primary-action" type="button" className="btn board-primary-action danger"
                disabled={busy} aria-label={busy ? '暂停任务，正在处理' : '暂停视频提示词任务'}
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
                disabled={busy}
                title="清空全部视频提示词段落、检查点及下游资源，保留映射结果"
                onClick={() => void previewClearStoryboard()}>
                清空视频提示词
              </button>}
            </>}
          </div>
          {status.state === 'running' && (
            <ServerTaskTimer
              label="视频提示词"
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
            {gateIssueCount > 3 && <small>其余问题会在对应段落中标出。</small>}
          </div>
        )}
      </section>

      {shots.length > 0 && (
        <section className="card storyboard-pack-overview" aria-label="分镜台节拍与素材概览">
          <div className="storyboard-pack-overview-head">
            <b>节拍概览</b>
            <small>{packBeatOverview.length ? `${packBeatOverview.length} 个节拍` : '暂无数据'}</small>
          </div>
          {packBeatOverview.length ? (
            <ul className="storyboard-pack-beat-list">
              {packBeatOverview.map(entry => (
                <li key={entry.beat_id}>
                  <b>{entry.beat_id}</b>
                  <span className="storyboard-pack-beat-summary">{entry.summary || '暂无摘要'}</span>
                  <span className="storyboard-pack-beat-locator">第 {compressSegmentIndexes(entry.segment_nos)} 段</span>
                </li>
              ))}
            </ul>
          ) : <p className="storyboard-pack-empty-hint">暂无数据</p>}
          <div className="storyboard-pack-resource-gap" role="status">
            <span>角色素材命中 <b>{packResourceGap.charactersLinked}/{packResourceGap.charactersTotal}</b>（按段落引用计）</span>
            <span>场景素材命中 <b>{packResourceGap.scenesLinked}/{packResourceGap.scenesTotal}</b>（按段落引用计）</span>
            <span>道具 <b>{packResourceGap.propsTotal}</b>（世界书无道具素材库，均为文字描述）</span>
            <button type="button" className="text-action storyboard-pack-degraded-copy" disabled={!packDegradedExportText}
              title={packDegradedExportText ? '复制全集能力降级项，用于后期文字合成' : '本集暂无能力降级项'}
              onClick={() => void copyPackDegradedExport()}>
              复制后期文字合成清单{packDegradedExportText ? '' : '（暂无）'}
            </button>
          </div>
        </section>
      )}

      <div className="workspace-gap" />

      {!shots.length ? (
        <div className="empty storyboard-empty">
          <div className="big">词</div>
          {storyboardEmptyCopy(status)}
        </div>
      ) : (
        <div className="board-workspace">
          <section className="shot-navigator" aria-label="段落轨道">
            <div className="shot-filter-bar" aria-label="段落筛选">
              <label><input type="checkbox" checked={onlyProblems}
                aria-label="仅看问题段"
                onChange={event => setOnlyProblems(event.target.checked)} />仅看问题段</label>
              {hasActiveFilters && <button type="button" className="btn small ghost clear-shot-filters"
                aria-label="清除全部段落筛选"
                onClick={clearShotFilters}>清除筛选</button>}
              <span className="shot-keyboard-hint">← → 切段（输入时不会触发）</span>
            </div>
            <div className="shot-navigator-head">
              <b>段落轨道</b>
              <div className="shot-navigator-actions" aria-live="polite">
                <span>{visibleShots.length ? selectedIndex + 1 : 0} / {visibleShots.length}，问题段 {shots.filter(isStoryboardProblemShot).length}{pendingRevalidation > 0 ? ` · 待校验 ${pendingRevalidation} 段` : ''}</span>
                <button type="button"
                  aria-label={!visibleShots.length ? '上一段，暂不可用：当前筛选下没有段落' : selectedIndex <= 0 ? '上一段，暂不可用：当前已是筛选结果中的第一段' : '上一段'}
                  disabled={selectedIndex <= 0} onClick={() => selectRelative(-1)}>←</button>
                <button type="button"
                  aria-label={!visibleShots.length ? '下一段，暂不可用：当前筛选下没有段落' : selectedIndex >= visibleShots.length - 1 ? '下一段，暂不可用：当前已是筛选结果中的最后一段' : '下一段'}
                  disabled={selectedIndex >= visibleShots.length - 1} onClick={() => selectRelative(1)}>→</button>
              </div>
            </div>
            <div ref={timelineRef} className="shot-navigator-list" role="listbox" aria-label="段落列表" tabIndex={0}>
              {visibleShots.map(shot => {
                const checkpoint = storyboardShotCheckpointLabel(shot.shot_no, status)
                const segment = shot.storyboard_pack_segment
                return <button key={shot.id} type="button" role="option" aria-selected={shot.id === selectedShot?.id}
                  className={shot.id === selectedShot?.id ? 'active' : ''}
                  onClick={() => requestShotSelect(shot.id)}>
                  <span className="shot-nav-top">
                    <span className="shot-nav-no">段 {String(shot.shot_no).padStart(2, '0')}</span>
                    <span>{segment?.duration_s ?? shot.duration_s}s</span>
                  </span>
                  <span className="shot-nav-main">
                    <b>{segment ? `${segment.shot_count} 镜切换` : '暂无数据'}</b>
                    <small>{segment?.synopsis || '（无梗概）'}</small>
                  </span>
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
                    {shot.is_final && <i>{checkpoint?.className === 'checkpoint-pending' ? '草稿收尾' : '收尾段'}</i>}
                    {!!segment?.degraded_capabilities.length && <i className="problem" title="本段存在能力降级项，详见段落详情">能力降级</i>}
                  </span>
                </button>
              })}
              {!visibleShots.length && <div className="shot-filter-empty" role="status">
                <b>当前筛选下没有段落</b>
                <span>清除筛选后可恢复全部 {shots.length} 段。</span>
                <button type="button" className="btn small" onClick={clearShotFilters}>清除筛选</button>
              </div>}
            </div>
          </section>

          <section className="shot-editor-pane">
            {selectionOutsideFilters && (
              <div className="filter-selection-note" role="status">当前段落已不在筛选结果内，已保留当前对象；只有你主动切换时才会离开。</div>
            )}
            {selectedShot && (
              <StoryboardPackSegmentView key={selectedShot.id}
                shot={selectedShot} bible={project?.bible} notify={toast} />
            )}
          </section>
        </div>
      )}</>}

      {clearPreview && <DecisionDialog
        title="清空本集全部视频提示词？"
        summary={`${clearPreview.shot_count} 个段落、${clearPreview.video_version_count} 个视频版本、${clearPreview.reference_asset_count} 个参考图资源将被删除`}
        message="将把本集视频提示词工作区恢复到未生成状态；当前映射结果会完整保留，之后可以从头生成新的视频提示词。"
        details={[
          ...(clearPreview.active_task_will_stop ? ['正在运行的视频提示词或视频任务会先安全停止'] : []),
          `将终止当前任务并重置修复检查点；${clearPreview.workflow_run_count} 条历史任务记录保留用于审计，不参与下次生成`,
          clearPreview.delivery_package_count
            ? `将删除 ${clearPreview.delivery_package_count} 个下游交付包`
            : '当前没有下游交付包',
          '此操作不可撤销，已经产生的模型调用费用不会退回',
        ]}
        confirmLabel="确认清空并保留映射结果"
        cancelLabel="取消"
        danger
        onClose={() => setClearPreview(null)}
        onConfirm={() => void clearStoryboard()}
      />}

      {videoModelConfirm && <DecisionDialog
        title="切换视频生成模型？"
        summary={`本集已有 ${videoModelConfirm.prompt_artifact_count} 条视频生成产物（提示词方言绑定于 ${videoModelLabel(videoModelConfirm.current_target_video_model)}）`}
        message={`两个供应商的提示词语法互不兼容，不能混用；切换到 ${videoModelLabel(videoModelConfirm.requested_target_video_model)} 会清空本集已生成的视频提示词与产物，且不可撤销。`}
        details={[
          `${videoModelLabel(videoModelConfirm.current_target_video_model)} → ${videoModelLabel(videoModelConfirm.requested_target_video_model)}`,
          '参考图与人物谱不受影响，只清空视频生成产物',
          '已经产生的模型调用费用不会退回',
        ]}
        confirmLabel="确认切换并清空"
        cancelLabel="取消"
        danger
        onClose={() => setVideoModelConfirm(null)}
        onConfirm={() => void submitVideoModel(videoModelConfirm.requested_target_video_model, true)}
      />}

      <Modal open={!!startPreview} title={startPreviewCopy?.title ?? '视频提示词任务'} onClose={closeStartPreview}
        actions={<><button className="btn" onClick={closeStartPreview}>取消</button><button className="btn primary" disabled={startPreview?.can_start === false} onClick={() => void submitStart()}>
          {startPreviewCopy?.confirmLabel ?? '继续'}
        </button></>}>
        {startPreview && startPreviewCopy && <div className="storyboard-preview-card">
          <p><b>{startPreviewCopy.summary}</b></p>
          <p>{startPreviewCopy.detail}</p>
          {startPreview.repair && startPreview.action === 'resume'
            && startPreview.resume_mode !== 'finalize_evidence' && <p>
            历史修复 {startPreview.repair.lifetime_repair_count} 次；将开启第 {startPreview.repair.activation_no + 1} 轮，
            每轮最多 {startPreview.repair.max_attempts_per_activation} 次。候选通过校验前不会覆盖现有视频提示词。
          </p>}
          {startPreview.blocking_reason && <div className="error-banner" role="alert">{startPreview.blocking_reason}</div>}
          {startPreview.warning && <div className="warning-banner">{startPreview.warning}</div>}
          {!!startPreview.repair?.last_issue_messages.length && <div className="warning-banner">
            {startPreview.repair.last_issue_messages.map(storyboardGateIssueLabel).join('；')}
          </div>}
        </div>}
      </Modal>

      {/* 决策③：确认视频提示词不再是需要人工点头的内容质量门——必检项全部通过时已在 autoConfirmStoryboard
          里自动放行，走不到这个弹窗。这里只在必检项未通过（真实阻断，不是审美取舍）时出现，
          纯粹展示要修什么，不提供"确认"按钮。 */}
      <Modal open={!!confirmPreview} title="暂不能确认视频提示词" onClose={() => setConfirmPreview(null)}
        actions={<button className="btn" onClick={() => setConfirmPreview(null)}>返回审阅</button>}>
        {confirmPreview && <div className="storyboard-preview-card">
          <dl><div><dt>当前视频提示词</dt><dd>{confirmPreview.storyboard_artifact_id ? '已生成，等待确认' : '待定稿'}</dd></div><div><dt>段落完整性</dt><dd>{confirmPreview.shot_count}/{confirmPreview.planned_shots}</dd></div>
            <div><dt>总时长</dt><dd>{confirmPreview.total_duration_s}s</dd></div><div><dt>收尾段</dt><dd>{confirmPreview.final_shot_valid ? '有效' : '缺失'}</dd></div>
            <div><dt>必检项</dt><dd>未通过</dd></div><div><dt>预计视频成本</dt><dd>¥{confirmPreview.estimated_video_cost_cny.min}–¥{confirmPreview.estimated_video_cost_cny.max}</dd></div></dl>
          {!!confirmPreview.warnings.length && <div className="warning-banner">{confirmPreview.warnings.map(storyboardGateIssueLabel).join('；')}</div>}
          {!!confirmPreview.hard_gates.errors.length && <div className="error-banner" role="alert"><b>请先处理以下问题：</b><ul>{confirmPreview.hard_gates.errors.map((item, index) => <li key={`${index}-${item}`}>{storyboardGateIssueLabel(item)}</li>)}</ul></div>}
          <p>处理方式：{confirmPreview.recovery_action || '返回分镜台继续修复，全部必检项通过后会自动确认'}</p>
          <small>{confirmPreview.estimated_video_cost_cny.note}</small>
        </div>}
      </Modal>
    </>
  )
}

/**
 * 分镜台唯一的段落展示（docs/STORYBOARD_PROMPT_IR_DESIGN.md 冻结契约）。
 * shot_size/camera_move/first_frame_desc 等经典逐镜字段在这一行没有意义
 * （见 api.ts 的 StoryboardPackSegment 注释）；后端也没有提供段落编辑能力，
 * 这里只做展示、只保留一个动作——复制整段提示词，不做没有动作的编辑入口。
 *
 * 信息分层（用户拍板，不得自由发挥）：
 * 1. 永远可见——段号 + 时长 + 一句话梗概；右侧素材缩略图行。
 * 2. 主体——prompt_text 整块，唯一动作是整段复制；这是本页主交付物，视觉权重最高。
 * 3. 次要——shot_count/目标模型/原文段号回指/台词条数/节拍/降级角标，小字与角标，
 *    不占正文层级，用 <details> 收起可展开的长内容（台词全文、节拍摘要、素材详情）。
 */
function StoryboardPackSegmentView({ shot, bible, notify }: {
  shot: Shot
  bible: Bible | null | undefined
  notify: (message: string, error?: boolean) => void
}) {
  const segment = shot.storyboard_pack_segment
  if (!segment) {
    return (
      <article className="shot-strip storyboard-pack-segment">
        <p className="storyboard-pack-empty-hint">本段暂无数据</p>
      </article>
    )
  }
  const rangeText = compressSegmentIndexes(segment.source_segment_indexes ?? [])
  const beats = segment.beats ?? []

  const copyPromptText = async () => {
    if (!navigator.clipboard) {
      notify('当前浏览器无法访问剪贴板，请检查浏览器权限后重试', true)
      return
    }
    try {
      await navigator.clipboard.writeText(segment.prompt_text)
      notify('提示词已整块复制')
    } catch {
      notify('复制失败，请允许浏览器访问剪贴板后重试', true)
    }
  }

  return (
    <article className="shot-strip storyboard-pack-segment">
      <header className="storyboard-pack-segment-head">
        <div className="storyboard-pack-segment-head-copy">
          <div className="storyboard-pack-segment-head-top">
            <b>第 {segment.segment_no} 段</b>
            <span>{segment.duration_s}s</span>
          </div>
          <p className="storyboard-pack-synopsis">{segment.synopsis || '（本段无梗概）'}</p>
        </div>
        <StoryboardPackResourceStrip resources={segment.resources} bible={bible} />
      </header>

      <section className="storyboard-pack-prompt-block">
        <div className="storyboard-pack-prompt-head">
          <b>视频生成提示词</b>
          <button type="button" className="text-action" onClick={() => void copyPromptText()}>复制整段提示词</button>
        </div>
        {segment.prompt_text
          ? <pre className="storyboard-pack-prompt-text">{segment.prompt_text}</pre>
          : <p className="storyboard-pack-empty-hint">暂无数据</p>}
      </section>

      <section className="storyboard-pack-meta-strip" aria-label="次要信息">
        <span className="pack-meta-chip">{segment.shot_count} 镜切换</span>
        <span className="pack-meta-chip">{storyboardPackTargetModelLabel(segment.target_model)}</span>
        <span className="pack-meta-chip">对应原文{rangeText ? ` 第 ${rangeText} 段` : '暂无数据'}</span>
        {segment.dialogue.length ? (
          <details className="pack-meta-details">
            <summary className="pack-meta-chip">台词 {segment.dialogue.length} 条</summary>
            <ul className="storyboard-pack-dialogue-list">
              {segment.dialogue.map((line, index) => (
                <li key={index}>
                  <span className="storyboard-pack-dialogue-speaker">{line.speaker_identity_id || '未知说话人'}</span>
                  <span className="storyboard-pack-dialogue-line">{line.line}</span>
                  <span className="storyboard-pack-dialogue-source">原文第 {line.source_segment_index} 段</span>
                </li>
              ))}
            </ul>
          </details>
        ) : <span className="pack-meta-chip muted">无台词</span>}
        {!!beats.length && (
          <details className="pack-meta-details">
            <summary className="pack-meta-chip">节拍 {beats.length} 个</summary>
            <ul className="storyboard-pack-beat-detail-list">
              {beats.map(beat => (
                <li key={beat.beat_id}><b>{beat.beat_id}</b>{beat.summary ? `：${beat.summary}` : '（暂无摘要）'}</li>
              ))}
            </ul>
          </details>
        )}
        <details className="pack-meta-details">
          <summary className="pack-meta-chip">素材详情</summary>
          <StoryboardPackResourceRoster resources={segment.resources} bible={bible} />
        </details>
        {segment.degraded_capabilities.map((item, index) => (
          <span key={index} className="pack-meta-chip degraded">{item}</span>
        ))}
      </section>
    </article>
  )
}

/** 永远可见层的素材缩略图行：能用视觉表达的不用文字——人物/场景绑了素材显示缩略图，
 *  没绑上显示灰位占位（用户一眼数得出有多少没映射上）；道具没有世界书图像素材库，
 *  设计使然地走文字。名称与描述收进 title 悬浮提示，不占正文层级。 */
function StoryboardPackResourceStrip({ resources, bible }: {
  resources: StoryboardPackResources
  bible: Bible | null | undefined
}) {
  const characters = resources.characters ?? []
  const scenes = resources.scenes ?? []
  const props = resources.props ?? []
  if (!characters.length && !scenes.length && !props.length) {
    return <div className="storyboard-pack-resource-strip-empty">暂无数据</div>
  }
  return (
    <div className="storyboard-pack-resource-strip" aria-label="本段素材">
      {characters.map((character, index) => {
        const imageUrl = findPortraitImage(bible, character.portrait_id)
        const label = character.identity_id || '未命名角色'
        const tip = character.description ? `${label} · ${character.description}` : label
        return imageUrl
          ? <img key={`c-${index}`} className="pack-resource-chip-thumb" src={imageUrl} alt={label} title={tip} loading="lazy" decoding="async" />
          : <div key={`c-${index}`} className="pack-resource-chip-empty" title={tip} aria-label={tip}>{label.slice(0, 1) || '无'}</div>
      })}
      {scenes.map((scene, index) => {
        const imageUrl = findSceneReferenceImage(bible, scene.scene_reference_id)
        const label = scene.scene_id || '未命名场景'
        const tip = scene.description ? `${label} · ${scene.description}` : label
        return imageUrl
          ? <img key={`s-${index}`} className="pack-resource-chip-thumb pack-resource-chip-scene" src={imageUrl} alt={label} title={tip} loading="lazy" decoding="async" />
          : <div key={`s-${index}`} className="pack-resource-chip-empty pack-resource-chip-scene" title={tip} aria-label={tip}>{label.slice(0, 1) || '无'}</div>
      })}
      {props.map((prop, index) => (
        <span key={`p-${index}`} className="pack-resource-chip-text" title={prop.description || prop.label}>{prop.label || '未命名道具'}</span>
      ))}
    </div>
  )
}

/** 要求 2：区分"有素材"（人物 portrait_id / 场景 scene_reference_id 非空，显示缩略图）
 *  与"只有文字描述"（为 null，以及全部 props——世界书没有道具素材库）两种状态，
 *  视觉上一眼能分：有图用缩略图，无图用占位块 + 文字描述。 */
function StoryboardPackResourceRoster({ resources, bible }: {
  resources: StoryboardPackResources
  bible: Bible | null | undefined
}) {
  const characters = resources.characters ?? []
  const scenes = resources.scenes ?? []
  const props = resources.props ?? []
  return (
    <section className="storyboard-pack-resources">
      <div className="storyboard-pack-resource-group">
        <b>人物 · {characters.length}</b>
        <div className="pack-resource-list">
          {characters.map((character, index) => {
            const imageUrl = findPortraitImage(bible, character.portrait_id)
            return (
              <div className="pack-resource-item" key={`${character.identity_id || 'character'}-${index}`}>
                {imageUrl
                  ? <img className="pack-resource-thumb" src={imageUrl} alt={character.identity_id} loading="lazy" decoding="async" />
                  : <div className="pack-resource-thumb-empty" aria-hidden="true">无图</div>}
                <div className="pack-resource-body">
                  <span className="pack-resource-name">{character.identity_id || '未命名角色'}</span>
                  <span className="pack-resource-desc">{character.description || (imageUrl ? '' : '暂无文字描述')}</span>
                </div>
              </div>
            )
          })}
          {!characters.length && <p className="storyboard-pack-empty-hint">本段无人物资源</p>}
        </div>
      </div>

      <div className="storyboard-pack-resource-group">
        <b>场景 · {scenes.length}</b>
        <div className="pack-resource-list">
          {scenes.map((scene, index) => {
            const imageUrl = findSceneReferenceImage(bible, scene.scene_reference_id)
            return (
              <div className="pack-resource-item" key={`${scene.scene_id || 'scene'}-${index}`}>
                {imageUrl
                  ? <img className="pack-resource-thumb" src={imageUrl} alt={scene.scene_id} loading="lazy" decoding="async" />
                  : <div className="pack-resource-thumb-empty" aria-hidden="true">无图</div>}
                <div className="pack-resource-body">
                  <span className="pack-resource-name">{scene.scene_id || '未命名场景'}</span>
                  <span className="pack-resource-desc">{scene.description || (imageUrl ? '' : '暂无文字描述')}</span>
                </div>
              </div>
            )
          })}
          {!scenes.length && <p className="storyboard-pack-empty-hint">本段无场景资源</p>}
        </div>
      </div>

      <div className="storyboard-pack-resource-group">
        <b>道具 · {props.length}</b>
        <div className="pack-resource-list">
          {/* 道具没有世界书图像素材库（设计使然），一律只有文字描述，用统一占位图标。 */}
          {props.map((prop, index) => (
            <div className="pack-resource-item" key={`${prop.label || 'prop'}-${index}`}>
              <div className="pack-resource-icon" aria-hidden="true">物</div>
              <div className="pack-resource-body">
                <span className="pack-resource-name">{prop.label || '未命名道具'}</span>
                <span className="pack-resource-desc">{prop.description || '暂无文字描述'}</span>
              </div>
            </div>
          ))}
          {!props.length && <p className="storyboard-pack-empty-hint">本段无道具</p>}
        </div>
      </div>
    </section>
  )
}
