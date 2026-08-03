/** 生成台镜头视频五态。
 * 后端 shot.video_status / shot.pipeline.video_status 是权威状态；版本数据只用于兼容旧响应。
 */
import type { Shot, ShotVersion } from './api'

export type ShotVideoPhase =
  | 'pending_generation'
  | 'generating'
  | 'pending_adoption'
  | 'adopted'
  | 'generation_failed'

export interface ShotVideoState {
  phase: ShotVideoPhase
  label: string
  railClass: 'empty' | 'working' | 'ready' | 'failed' | 'fallback'
  adopted: ShotVersion | undefined
  latest: ShotVersion | undefined
  playing: ShotVersion | undefined
  grade?: 'A' | 'B' | 'C' | null
  fallbackReason?: string | null
  continuityDegraded?: boolean
}

const PHASE_LABEL: Record<ShotVideoPhase, string> = {
  pending_generation: '待生成',
  generating: '生成中',
  pending_adoption: '待采纳',
  adopted: '已采纳',
  generation_failed: '生成失败',
}

const VIDEO_PHASES = new Set<ShotVideoPhase>(Object.keys(PHASE_LABEL) as ShotVideoPhase[])

const ACTIVE_PIPELINE = new Set([
  'queued',
  'running',
  'waiting',
  'waiting_provider',
  'waiting_retry',
  'waiting_budget',
  'paused_budget',
  'blocked',
])

function backendPhase(shot: Shot): ShotVideoPhase | undefined {
  const value = shot.video_status ?? shot.pipeline?.video_status
  return value && VIDEO_PHASES.has(value) ? value : undefined
}

export function shotVideoState(shot: Shot): ShotVideoState {
  const versions = shot.versions ?? []
  const adopted = versions.find(version => version.id === shot.adopted_version_id)
  const latest = versions[0]
  const playableAdopted = adopted?.status === 'succeeded' && !!adopted.video_url
  const playableCandidate = versions.find(
    version => version.status === 'succeeded' && !!version.video_url,
  )
  const activeVersion = versions.find(
    version => version.status === 'queued'
      || version.status === 'running'
      || version.status === 'waiting_provider',
  )

  const grade = shot.video_grade ?? null
  const fallbackReason = shot.fallback_reason ?? null
  const continuityDegraded = !!shot.continuity_degraded

  // 防御旧响应或刷新竞争：只要采纳版可播放，就绝不能被生成/过期状态覆盖。
  let phase: ShotVideoPhase
  if (playableAdopted) {
    phase = 'adopted'
  } else {
    const authoritative = backendPhase(shot)
    if (authoritative) {
      phase = authoritative
    } else {
      const pipelineActive = shot.pipeline != null
        && ACTIVE_PIPELINE.has(shot.pipeline.pipeline_status)
      const legacyVersionWorking = !shot.pipeline && !!activeVersion
      if (pipelineActive || legacyVersionWorking) {
        phase = 'generating'
      } else if (playableCandidate) {
        phase = 'pending_adoption'
      } else if (
        shot.pipeline?.pipeline_status === 'waiting_human'
        || shot.pipeline?.pipeline_stage === 'preflight_blocked'
        || shot.pipeline?.pipeline_stage === 'waiting_human'
        ||
        shot.pipeline?.pipeline_status === 'failed'
        || latest?.status === 'failed'
        || versions.some(version => version.status === 'failed')
      ) {
        phase = 'generation_failed'
      } else {
        phase = 'pending_generation'
      }
    }
  }

  const playing = phase === 'adopted'
    ? adopted
    : phase === 'generating'
      ? activeVersion || playableCandidate || latest
      : playableCandidate || latest

  const railClass: ShotVideoState['railClass'] = phase === 'generating'
    ? 'working'
    : phase === 'generation_failed'
      ? 'failed'
      : phase === 'pending_generation'
        ? 'empty'
        : phase === 'adopted' && (grade === 'B' || !!fallbackReason)
          ? 'fallback'
          : 'ready'

  return {
    phase,
    label: PHASE_LABEL[phase],
    railClass,
    adopted,
    latest,
    playing,
    grade: phase === 'adopted' ? (grade || 'A') : grade,
    fallbackReason,
    continuityDegraded,
  }
}

export function countAdoptedVideos(shots: Shot[]): number {
  return shots.filter(shot => shotVideoState(shot).phase === 'adopted').length
}

/** 生成台镜头按钮上的简明执行阶段。 */
export function compactShotStage(shot: Shot): string {
  const pipeline = shot.pipeline
  const stage = pipeline?.pipeline_stage || pipeline?.current_stage || ''
  const progress = pipeline?.stage_progress
  const current = progress?.current
  const total = progress?.total
  const labels: Record<string, string> = {
    preflight_validating: '校验视频输入',
    preflight_retry: '校验失败，自动重试',
    preflight_blocked: '输入校验未通过',
    job_queued: '已入队',
    reference_prompt: '准备参考图词',
    reference_generate: current != null && total ? `候选图 ${current}/${total}` : '生成候选图',
    reference_qa: current != null && total ? `参考图质检 ${current}/${total}` : '参考图质检',
    reference_consistency: '参考图一致性',
    waiting_continuity_anchor: '等待上一镜',
    continuity_assembling: '装配连续性',
    video_ready: '视频输入就绪',
    waiting_video_slot: pipeline?.queue_position ? `等待名额（前方 ${pipeline.queue_position}）` : '等待视频名额',
    video_submitting: '提交供应商',
    video_generating: '供应商生成中',
    video_downloading: '下载视频',
    video_technical_check: '技术校验',
    video_qa: '内容质检',
    auto_retake_queued: '重新生成排队',
    paused_budget: '预算暂停',
    waiting_human: '等待人工',
  }
  return labels[stage] || pipeline?.stage_label || '状态同步中'
}

export function formatPipelineSummary(
  summary: import('./api').EpisodePipelineSummary | null | undefined,
  shotsTotal: number,
): string {
  const counts = summary?.video_status_counts
  const adopted = counts?.adopted ?? summary?.adopted ?? 0
  const generating = counts?.generating ?? summary?.upstream_generating ?? 0
  const pendingAdoption = counts?.pending_adoption
    ?? Math.max(0, (summary?.with_candidate ?? adopted) - adopted)
  const failed = counts?.generation_failed ?? summary?.failed ?? 0
  const pendingGeneration = counts?.pending_generation
    ?? Math.max(0, shotsTotal - adopted - generating - pendingAdoption - failed)

  return [
    `${summary?.shots_total ?? shotsTotal} 镜`,
    `待生成 ${pendingGeneration}`,
    `生成中 ${generating}`,
    `待采纳 ${pendingAdoption}`,
    `已采纳 ${adopted}`,
    `生成失败 ${failed}`,
  ].join(' · ')
}
