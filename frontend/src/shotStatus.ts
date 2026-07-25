/** 镜头视频统一状态机：轨道 / 标题 / 播放器 / 版本卡必须共用同一判定。
 * 执行阶段只读后端 shot.pipeline，不再用 versions 状态二次推断活动阶段。 */
import type { Shot, ShotVersion } from './api'

export type ShotVideoPhase =
  | 'empty'
  | 'working'
  | 'failed'
  | 'ready'
  | 'adopted'
  | 'stale'
  | 'waiting_human'

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
  empty: '待生成',
  working: '生成中',
  failed: '生成失败',
  ready: '待采用',
  adopted: '已采用',
  stale: '需重生',
  waiting_human: '待人工',
}

const ACTIVE_PIPELINE = new Set([
  'queued', 'running', 'waiting', 'waiting_provider', 'blocked', 'waiting_human',
])

export function shotVideoState(shot: Shot): ShotVideoState {
  const versions = shot.versions ?? []
  const adopted = versions.find(v => v.id === shot.adopted_version_id)
  const latest = versions[0]
  const pipeline = shot.pipeline

  // 权威阶段：只信后端 pipeline；versions 仅用于播放/采用
  const pipelineActive = pipeline != null && ACTIVE_PIPELINE.has(pipeline.pipeline_status)
  const legacyVersionWorking = !pipeline && versions.some(v =>
    v.status === 'queued' || v.status === 'running' || v.status === 'waiting_provider'
  )
  const working = pipelineActive || legacyVersionWorking

  if (pipeline?.stage_label && (
    working
    || pipeline.pipeline_status === 'waiting_human'
    || pipeline.pipeline_status === 'blocked'
  )) {
    const phase: ShotVideoPhase =
      pipeline.pipeline_status === 'waiting_human' || pipeline.pipeline_status === 'blocked'
        ? 'waiting_human'
        : 'working'
    const playing = versions.find(v =>
      v.status === 'queued' || v.status === 'running' || v.status === 'waiting_provider'
    ) || adopted || latest
    return {
      phase,
      label: pipeline.stage_label,
      railClass: phase === 'waiting_human' ? 'failed' : 'working',
      adopted,
      latest,
      playing,
    }
  }

  if (working) {
    const playing = versions.find(v =>
      v.status === 'queued' || v.status === 'running' || v.status === 'waiting_provider'
    ) || adopted || latest
    return {
      phase: 'working',
      label: pipeline?.stage_label || PHASE_LABEL.working,
      railClass: 'working',
      adopted,
      latest,
      playing,
    }
  }

  const adoptedOk = adopted?.status === 'succeeded' && !!adopted.video_url
  const grade = (shot as { video_grade?: 'A' | 'B' | 'C' | null }).video_grade
    ?? (shot as { grade?: 'A' | 'B' | 'C' | null }).grade
    ?? null
  const fallbackReason = (shot as { fallback_reason?: string | null }).fallback_reason ?? null
  const continuityDegraded = !!(shot as { continuity_degraded?: boolean }).continuity_degraded

  if (adoptedOk) {
    if (shot.video_stale) {
      return {
        phase: 'stale', label: PHASE_LABEL.stale, railClass: 'failed',
        adopted, latest, playing: adopted, grade: grade || 'C', fallbackReason, continuityDegraded,
      }
    }
    if (grade === 'B' || fallbackReason) {
      return {
        phase: 'adopted',
        label: continuityDegraded ? '已采用（兜底·衔接降级）' : '已采用（兜底）',
        railClass: 'fallback',
        adopted, latest, playing: adopted,
        grade: 'B',
        fallbackReason,
        continuityDegraded,
      }
    }
    return {
      phase: 'adopted',
      label: PHASE_LABEL.adopted,
      railClass: 'ready',
      adopted, latest, playing: adopted,
      grade: grade || 'A',
      fallbackReason: null,
      continuityDegraded,
    }
  }

  const succeeded = versions.find(v => v.status === 'succeeded' && !!v.video_url)
  if (succeeded) {
    return { phase: 'ready', label: PHASE_LABEL.ready, railClass: 'ready', adopted, latest, playing: succeeded }
  }

  if (pipeline?.pipeline_status === 'failed' || latest?.status === 'failed' || versions.some(v => v.status === 'failed')) {
    return {
      phase: 'failed',
      label: pipeline?.stage_label || PHASE_LABEL.failed,
      railClass: 'failed',
      adopted,
      latest,
      playing: latest,
    }
  }

  return { phase: 'empty', label: PHASE_LABEL.empty, railClass: 'empty', adopted, latest, playing: latest }
}

/** 进度统计：仅计「已采用且未过期」为完成 */
export function countAdoptedVideos(shots: Shot[]): number {
  return shots.filter(s => {
    const { phase } = shotVideoState(s)
    return phase === 'adopted'
  }).length
}

export function formatPipelineSummary(summary: import('./api').EpisodePipelineSummary | null | undefined, shotsTotal: number): string {
  if (!summary) {
    return `已采用 —/${shotsTotal}`
  }
  const parts = [
    `${summary.shots_total} 镜`,
    `已采用 ${summary.adopted}`,
    `视频生成中 ${summary.upstream_generating}`,
  ]
  if (summary.video_ready != null) parts.push(`视频就绪待槽 ${summary.video_ready}`)
  parts.push(`参考图制作 ${summary.preparing_references}`)
  if (summary.waiting_continuity != null) parts.push(`等连续性 ${summary.waiting_continuity}`)
  if (summary.video_qa != null) parts.push(`视频质检 ${summary.video_qa}`)
  parts.push(`待人工 ${summary.waiting_human}`)
  if (summary.failed != null) parts.push(`失败 ${summary.failed}`)
  return parts.join(' · ')
}
