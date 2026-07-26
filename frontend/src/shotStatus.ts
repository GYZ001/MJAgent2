/** 评审墙镜头视频五态。
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
