import type { Shot, ShotVersion } from './api'

/** 镜头视频统一状态机：轨道 / 标题 / 播放器 / 版本卡必须共用同一判定 */
export type ShotVideoPhase =
  | 'empty'
  | 'working'
  | 'failed'
  | 'ready'
  | 'adopted'
  | 'stale'

export interface ShotVideoState {
  phase: ShotVideoPhase
  label: string
  railClass: 'empty' | 'working' | 'ready' | 'failed'
  adopted: ShotVersion | undefined
  latest: ShotVersion | undefined
  playing: ShotVersion | undefined
}

const PHASE_LABEL: Record<ShotVideoPhase, string> = {
  empty: '待生成',
  working: '生成中',
  failed: '生成失败',
  ready: '待采用',
  adopted: '已采用',
  stale: '需重生',
}

export function shotVideoState(shot: Shot): ShotVideoState {
  const versions = shot.versions ?? []
  const adopted = versions.find(v => v.id === shot.adopted_version_id)
  const latest = versions[0]
  const working = versions.some(v => v.status === 'queued' || v.status === 'running')

  if (working) {
    const playing = versions.find(v => v.status === 'queued' || v.status === 'running') || adopted || latest
    return { phase: 'working', label: PHASE_LABEL.working, railClass: 'working', adopted, latest, playing }
  }

  const adoptedOk = adopted?.status === 'succeeded' && !!adopted.video_url
  if (adoptedOk) {
    if (shot.video_stale) {
      return { phase: 'stale', label: PHASE_LABEL.stale, railClass: 'failed', adopted, latest, playing: adopted }
    }
    return { phase: 'adopted', label: PHASE_LABEL.adopted, railClass: 'ready', adopted, latest, playing: adopted }
  }

  const succeeded = versions.find(v => v.status === 'succeeded' && !!v.video_url)
  if (succeeded) {
    return { phase: 'ready', label: PHASE_LABEL.ready, railClass: 'ready', adopted, latest, playing: succeeded }
  }

  if (latest?.status === 'failed' || versions.some(v => v.status === 'failed')) {
    return { phase: 'failed', label: PHASE_LABEL.failed, railClass: 'failed', adopted, latest, playing: latest }
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
