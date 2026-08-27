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
import StageTextModelPicker from '../components/StageTextModelPicker'
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
 * 节拍表的排序键：从 beat_id 里提取数字自然排序，不按字符串字典序——字典序会把
 * "b10" 排到 "b2" 前面（真实 EP1 数据验证过：16 个节拍字符串排序后是
 * b1,b10,b11..b16,b2,b3..b9，整张节拍表的推进顺序全乱）。beat_id 的具体形状
 * （"b<数字>"）不是冻结契约保证的稳定形状，所以只抓"第一段数字"，前缀是什么不重要；
 * 抓不到数字的 id 统一排到最后（Infinity），不抛错也不按字典序静默重排——多个 id
 * 并列同一个数字（或都没有数字）时，交给下面 Array.prototype.sort 的稳定排序退回
 * byBeat 的插入顺序，也就是遍历 shots/beats[] 的原始次序，不需要额外写 tie-break。
 * 没有采用"按 segment_indexes 最小值排序"：那个信号在 B01 先出现于后段、B02
 * 先出现于前段这类真实场景里可能和 beat_id 本身的编号方向相反，会引入一个新的
 * 不一致来源；beat_id 的数字就是用户在节拍表里直接看到的东西，按它自然排序最直接
 * 对应"一眼看出这一集怎么推进"的诉求。
 */
function naturalBeatOrderKey(beatId: string): number {
  const match = beatId.match(/\d+/)
  return match ? Number(match[0]) : Number.POSITIVE_INFINITY
}

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
    .sort(([a], [b]) => naturalBeatOrderKey(a) - naturalBeatOrderKey(b))
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

// 供应商付费任务尚未终态时的清空阻塞（app/completion_grant.py
// ProviderTasksNotTerminalError.detail）；recovery_action 是后端给出的下一步
// 建议，前端只做文案翻译，不臆造新含义。
export type ProviderTaskBlocker = {
  job_id: string
  shot_id: string | null
  version_id: string | null
  job_status: string
  provider_operation_id: string | null
  provider_task_id: string | null
  provider_create_state: string
  claim_status: string | null
  amount_cny: number
  recovery_status: 'waiting_provider' | 'waiting_human' | string
  recovery_action: 'review_provider_failure' | 'continue_provider_poll' | 'restore_provider_poll' | 'reconcile_provider_create' | string
}

type ProviderTaskClearance = {
  safe_to_clear: boolean
  resume_supported: boolean
  blockers: ProviderTaskBlocker[]
}

type ProviderTaskReconcileResult = {
  episode_id: string
  blockers_before: number
  provider_confirmed_terminal_job_ids: string[]
  superseded_jobs_closed_job_ids: string[]
  clearance: ProviderTaskClearance
}

const PROVIDER_RECOVERY_ACTION_LABEL: Record<string, string> = {
  review_provider_failure: '供应商任务发生技术失败，系统正在等待人工核对；点击下方按钮会去问供应商这个任务现在到底是什么状态',
  continue_provider_poll: '供应商任务仍可能在处理中；点击下方按钮会继续查询它的最新状态',
  restore_provider_poll: '本地曾记录到供应商任务号，但轮询中断了；点击下方按钮会用这个任务号重新查询',
  reconcile_provider_create: '不确定这次创建请求供应商是否收到；点击下方按钮会去核对供应商那边有没有对应任务',
}

export function providerRecoveryActionLabel(blocker: ProviderTaskBlocker): string {
  return PROVIDER_RECOVERY_ACTION_LABEL[blocker.recovery_action]
    ?? `建议：${blocker.recovery_action}`
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

/**
 * status.headline 是后端整集状态文案（app/domain/storyboard_ops.py），那边把 shots
 * 表的每一行统称"镜"；但分镜台的段落视图早就只剩"段"一种展示单位（见本文件
 * isStoryboardPackSegmentShot 的注释：一个 15 秒段 = shots 表一行，段内 3-4 镜写进
 * prompt_text 文本、不拆成独立数据行）。顶部状态条一句"10/10 镜已通过"和下方
 * "段落轨道""段 01""3 镜切换"同屏出现，会让人误以为 10 个 15 秒段还要再拆成
 * 10 个镜头。这里只在展示层收窄，不改后端措辞：只替换"数字/数字 镜已通过""第 N
 * 镜""问题镜"这几个明确指"shots 表一行"的模式，不动"分镜"这个词本身（下面两条
 * 正则都要求"镜"前面紧跟数字或"问题"二字，不会命中"分镜任务""当前分镜已确认"里的
 * "分镜"）。
 */
export function storyboardHeadlineLabel(headline: string): string {
  return headline
    // 分镜台 2.0.3（后端 app/production/storyboard_pack.py）：全部段落的视频
    // 提示词现在由一次整集模型调用联合产出，不再是逐段并行发起、也没有任何
    // 逐段落库的中间态（persist_storyboard_pack 单事务、要么整份写完、要么
    // 什么都不写）。"当前处理第 N 镜"这句话只可能来自 app.domain.storyboard_
    // ops._storyboard_status_snapshot 的 running 分支，而该分支在这条管线下
    // resume_from 永远算出 1（生成完成前 shots 表没有任何行）——这个数字从不
    // 是真进度，必须整体替换掉，不能只做"镜"->"段"的措辞替换（旧的逐镜叙事
    // 权威管线已下线，这句话不会再来自任何其它路径）。
    .replace(/当前处理第\s*\d+\s*镜/, '整集视频提示词正在联合生成')
    .replace(/(\d+\/\d+)\s*镜已通过/g, '$1 段已通过')
    .replace(/第\s*(\d+)\s*镜/g, '第 $1 段')
    .replaceAll('问题镜', '问题段')
}

type StoryboardProgressCopy = {
  summary: string
  detail: string | null
}

export type StoryboardPrimaryAction = {
  intent: StoryboardStatus['recommended_action']
  label: string
}

export function storyboardPrimaryAction(
  status: StoryboardStatus,
): StoryboardPrimaryAction {
  // 2026-08-26 用户拍板：分镜提示词全部生成完就直接可进生成台，不再有一个
  // 独立的"完成发布证据"人工仪式挡在中间（该仪式此前对应的产物签发已经在
  // 生成完成时自动落盘，见 app.domain.review_wall._review_upstream_snapshot /
  // app.domain.storyboard_ops._storyboard_status_snapshot 同一处改动）。
  // 冷观众审读/一次观看校准（AI 一次观看模拟分支）功能已整体下线（用户
  // 拍板），原本挂在 finalizingEvidence 上的那个分支一并删除。
  const labels: Record<StoryboardStatus['recommended_action'], string> = {
    go_screenplay: '先去映射台',
    generate_storyboard: '生成视频提示词',
    view_progress: '查看任务详情',
    resume_storyboard: '继续生成视频提示词',
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
  // 分镜台 2.0.3：全部段落由一次整集模型调用联合产出，落库单事务、要么整份
  // 写完、要么什么都不写（app.production.storyboard_pack.persist_storyboard_
  // pack）。working===0 意味着这一轮还没有任何段落成功产出——"从第 N 段
  // 继续""逐段校验"这类措辞会暗示一种其实不存在的、可从任意中间段续跑的
  // 能力，必须换成如实描述"整集一次生成、要么全有要么全无"的文案。
  if (working === 0) {
    return {
      summary,
      detail: status.state === 'running'
        ? '这一集的视频提示词正在生成——按整集一次调用联合产出，不是逐段推进；完成前不会有段落先出现，完成后会一次性全部展示。'
        : '这一集的视频提示词按整集一次调用联合产出；这一轮还没有成功产出任何段落，重新发起会整集重新生成，不是从某一段续写。',
    }
  }
  // 2026-08-26 用户拍板：分镜台不再有一个独立的"完成发布证据"确认步骤——
  // 产物（含叙事权威分集的冷观众审读/校准）在生成完成时已自动签发；这里不
  // 再单独描述那一步，落到下面的通用文案，不留一句只对小部分分集成立、
  // 对大多数分集已经不成立的"还差一步"话术。
  const finalDraftNote = status.final_shot_valid
    ? '工作副本中的收尾标记不代表整集已通过校验。'
    : ''
  if (pending > 0) {
    return {
      summary,
      detail: `第 ${validated + 1}–${working} 段仍待校验；任务将从第 ${resumeFrom} 段继续修复。全部段落通过校验前，轨道中的内容都只是工作副本。${finalDraftNote}`,
    }
  }
  return {
    summary,
    detail: `当前 ${validated} 段已通过逐段校验；任务将从第 ${resumeFrom} 段继续。全部段落生成并通过校验后即可进入生成台。${finalDraftNote}`,
  }
}

export function storyboardEmptyCopy(status: StoryboardStatus): string {
  if (status.state === 'no_screenplay') return '尚无可用于生成视频提示词的映射结果，请先去映射台。'
  // 分镜台 2.0.3：全部段落由一次整集模型调用联合产出，不是逐段生成/逐段
  // 校验——不会有"首批"先出现，完成前也不存在中间态。
  if (status.state === 'running') return '这一集的视频提示词正在生成，按整集一次调用联合产出全部段落，完成后会一次性展示。'
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
  // 2026-08-26 用户拍板：不再单独展示"完成发布证据"这一步——产物齐了就
  // 直接可进生成台，resume_mode==='finalize_evidence' 落回下面的通用文案。
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
    return { label: '已校验', className: 'checkpoint-safe', title: '本轮已通过逐段校验；整集全部生成完成后才算产物齐全' }
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
  const { data: project, refresh: refreshProject } = useProject(projectId!, 0, 'bible')
  const [busy, setBusy] = useState(false)
  const [selectedShotId, setSelectedShotId] = useState<string | null>(null)
  const [onlyProblems, setOnlyProblems] = useState(false)
  const [recoveredStatus, setRecoveredStatus] = useState<StoryboardStatus | null>(null)
  const [startPreview, setStartPreview] = useState<StartPreview | null>(null)
  const [confirmPreview, setConfirmPreview] = useState<ConfirmPreview | null>(null)
  const [clearPreview, setClearPreview] = useState<StoryboardClearPreview | null>(null)
  const [videoModelConfirm, setVideoModelConfirm] = useState<VideoModelSwitchConfirm | null>(null)
  const [providerClearance, setProviderClearance] = useState<ProviderTaskClearance | null>(null)
  const [providerReconcileBusy, setProviderReconcileBusy] = useState(false)
  const [providerReconcileResult, setProviderReconcileResult] = useState<ProviderTaskReconcileResult | null>(null)
  // 两个不同按钮（清空视频提示词 / 切换视频模型）撞上同一个 409
  // PROVIDER_TASKS_NOT_TERMINAL 时复用同一块恢复面板；这个状态只决定解除
  // 阻塞后该指引用户回去点哪个按钮，不影响核对逻辑本身。
  const [providerClearanceOrigin, setProviderClearanceOrigin] = useState<'clear' | 'video_model' | null>(null)
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
  const primaryAction = storyboardPrimaryAction(status)

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
    setBusy(true)
    try {
      await api.post(`/episodes/${ep.id}/storyboard/clear`, { preview_token: previewToken })
      toast(`已清空 ${deletedShots} 个视频提示词段落及其下游资源，映射结果已保留`)
      await refresh({ force: true })
      setSelectedShotId(null)
      clearShotFilters()
    } catch (caught) {
      const apiError = caught as ApiError
      if (apiError.code === 'PROVIDER_TASKS_NOT_TERMINAL') {
        // 供应商付费任务尚未确认终态时闸门本身是对的：不核实就清空会丢账。
        // 但不能只把用户挡在这里——detail.blockers 是每一条具体卡住了什么、
        // 后端建议怎么核对，接到面板上让用户能真正采取行动，而不是对着一句
        // 「请核对供应商创建结果」却无处可核对。
        const detail = apiError.detail as Partial<ProviderTaskClearance> | undefined
        setProviderReconcileResult(null)
        setProviderClearanceOrigin('clear')
        setProviderClearance({
          safe_to_clear: false,
          resume_supported: detail?.resume_supported ?? true,
          blockers: detail?.blockers ?? [],
        })
      } else {
        toast(apiError.message, true)
      }
    } finally {
      setBusy(false)
    }
  }

  const reconcileProviderTasks = async () => {
    setProviderReconcileBusy(true)
    try {
      const result = await api.post(
        `/episodes/${currentEpisodeId}/provider-tasks/reconcile`,
        {},
      ) as ProviderTaskReconcileResult
      setProviderReconcileResult(result)
      setProviderClearance(result.clearance)
      if (result.clearance.safe_to_clear) {
        const retryLabel = providerClearanceOrigin === 'video_model' ? '切换视频模型' : '清空视频提示词'
        toast(`供应商任务已核对完毕，阻塞已解除，可以重新点击「${retryLabel}」`)
      }
    } catch (caught) {
      toast((caught as Error).message, true)
    } finally {
      setProviderReconcileBusy(false)
    }
  }

  const closeProviderClearancePanel = () => {
    setProviderClearance(null)
    setProviderReconcileResult(null)
    setProviderClearanceOrigin(null)
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
      } else if (apiError.code === 'PROVIDER_TASKS_NOT_TERMINAL') {
        // 切换视频模型在本集已有视频产物时会走与「清空视频提示词」相同的
        // _require_provider_clearance 闸门；撞上同一种阻塞，复用同一块
        // 恢复面板，不能又让这条路径回到无处下手的裸错误提示。
        const detail = apiError.detail as Partial<ProviderTaskClearance> | undefined
        setProviderReconcileResult(null)
        setProviderClearanceOrigin('video_model')
        setProviderClearance({
          safe_to_clear: false,
          resume_supported: detail?.resume_supported ?? true,
          blockers: detail?.blockers ?? [],
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

      {showLaunchPanel ? (
        <StoryboardLaunchPanel episode={ep} status={status} busy={busy} onPrimary={() => { void runPrimary() }} />
      ) : <><section className={`card board-toolbar state-${status.state}`} aria-labelledby="storyboard-state-title">
        <div className="board-toolbar-row">
          <div className="board-state-copy">
            <span className={`storyboard-state-dot state-${status.state}`} aria-hidden="true" />
            <div>
              {/* .board-state-copy strong 是单行省略号截断（工具栏窄时常见，
                  尤其分集标题较长或视口较窄时）；曾经真实发生过用户看到的
                  报错文案被截断在句子中间、看不出后半句在说什么（真实回归
                  ep_3d523ff4d0a4）。title 把完整文案原样暴露给悬浮/屏幕阅读
                  器，不改变视觉截断本身——截断是空间限制下的合理取舍，但
                  不能让用户在文案被截断时无处可查完整内容。 */}
              <strong id="storyboard-state-title" title={storyboardHeadlineLabel(status.headline)}>
                {storyboardHeadlineLabel(status.headline)}
              </strong>
              <small>{progressCopy.summary}{terminalFinalShot ? ' · 收尾段有效' : ''}</small>
            </div>
          </div>
          <div className="board-action-group">
            <label title="切换本集绑定的视频生成模型；两个供应商提示词方言不兼容，与生成台强绑定。作用域：仅本集">
              <span>视频模型（本集）</span>
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
            <StageTextModelPicker
              projectId={projectId!}
              field="board_text_provider"
              label="文本模型（项目）"
              title="分镜台生成分镜内容使用的文本模型（与左边的视频模型是两回事：视频模型控制提交视频生成用哪个供应商，且只对本集生效）。作用域：整个项目，对该项目所有分集的分镜台生效，不是只对本集；不选则使用系统默认文本模型，只影响之后新发起的分镜生成，不影响已生成内容"
              value={project?.board_text_provider}
              choices={project?.text_model_choices ?? []}
              disabled={busy}
              toast={toast}
              onSaved={() => void refreshProject({ force: true })}
            />
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

      <Modal open={!!providerClearance} title="供应商付费任务尚未终态，未清空任何资源"
        onClose={closeProviderClearancePanel}
        actions={<>
          <button type="button" className="btn" onClick={closeProviderClearancePanel}>关闭</button>
          <button type="button" className="btn primary" disabled={providerReconcileBusy}
            onClick={() => void reconcileProviderTasks()}>
            {providerReconcileBusy ? '正在核对…' : '核对供应商任务状态'}
          </button>
        </>}>
        {providerClearance && <div className="storyboard-preview-card">
          <p>
            以下任务可能已被供应商接单，直接清空会让这笔费用查无对应产物；不核实清楚就清空可能丢账，所以先挡在这里。
            点击「核对供应商任务状态」会去问供应商每个任务的真实状态——只有确认结果（成功/失败）或确认从未真正提交给供应商，才会解除阻塞；
            供应商仍在处理中的任务会原样保留，不提供绕过闸门的选项。
          </p>
          <ul className="provider-blocker-list">
            {providerClearance.blockers.map(blocker => {
              const shotNo = shots.find(shot => shot.id === blocker.shot_id)?.shot_no
              return (
                <li key={blocker.job_id}>
                  <b>{shotNo != null ? `第 ${shotNo} 段` : '未知段落'} · 任务 {blocker.job_id}</b>
                  <span>费用 ¥{blocker.amount_cny.toFixed(2)} · 状态 {blocker.job_status}</span>
                  <span>{providerRecoveryActionLabel(blocker)}</span>
                </li>
              )
            })}
          </ul>
          {!providerClearance.blockers.length && (
            <p role="status">
              没有更多阻塞任务了；可以关闭本面板，
              重新点击「{providerClearanceOrigin === 'video_model' ? '切换视频模型' : '清空视频提示词'}」。
            </p>
          )}
          {providerReconcileResult && (
            <div className="board-progress-explanation" role="status">
              <b>核对结果</b>
              <span>
                {providerReconcileResult.provider_confirmed_terminal_job_ids.length
                  ? `${providerReconcileResult.provider_confirmed_terminal_job_ids.length} 个任务确认供应商终态并已结算费用责任；`
                  : ''}
                {providerReconcileResult.superseded_jobs_closed_job_ids.length
                  ? `${providerReconcileResult.superseded_jobs_closed_job_ids.length} 个任务确认从未提交给供应商（所属段落已有其他成功版本），已作为过时任务收口；`
                  : ''}
                {providerReconcileResult.clearance.blockers.length
                  ? `仍有 ${providerReconcileResult.clearance.blockers.length} 个任务在供应商侧处理中，请稍后再核对一次`
                  : `全部解除，可以重新点击「${providerClearanceOrigin === 'video_model' ? '切换视频模型' : '清空视频提示词'}」`}
              </span>
            </div>
          )}
        </div>}
      </Modal>

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
