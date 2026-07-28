import type { Episode } from './api'

export type EpisodeOption = Pick<Episode, 'id' | 'episode_no' | 'title'> & Partial<Pick<Episode,
  'status' | 'screenplay_status' | 'shot_count' | 'video_count' | 'pending_adoption_count' | 'failed_count'
>>

export type EpisodeProductionFilter = 'all' | 'with_video' | 'pending_adoption' | 'failed' | 'unproduced'

export function episodeProductionStatus(
  episode: Pick<EpisodeOption, 'status' | 'screenplay_status'>,
): string {
  if (episode.screenplay_status === 'failed' || episode.status === 'script_failed') return '需处理'
  if (episode.screenplay_status !== 'ready') {
    return episode.screenplay_status === 'running' ? '剧本中' : '待剧本'
  }
  if (episode.status === 'scripting') return '分镜中'
  if (['confirmed', 'generating', 'done'].includes(episode.status || '')) return '已确认'
  if (episode.status === 'scripted') return '待确认'
  return '待分镜'
}

export function resolveEpisodeId(
  episodes: EpisodeOption[],
  currentEpisodeId: string | null,
): string | null {
  if (currentEpisodeId && episodes.some(episode => episode.id === currentEpisodeId)) {
    return currentEpisodeId
  }
  return episodes[0]?.id ?? null
}

/** 地址栏显式指定的分集保持权威；无显式目标时才从项目分集中恢复选择。 */
export function resolveRoutedEpisodeId(
  episodes: EpisodeOption[],
  currentEpisodeId: string | null,
  requestedEpisodeId: string | null,
): string | null {
  return requestedEpisodeId ?? resolveEpisodeId(episodes, currentEpisodeId)
}

export function filterEpisodeOptions(
  episodes: EpisodeOption[],
  query: string,
  limit = 60,
  filters?: { production?: EpisodeProductionFilter },
): EpisodeOption[] {
  const keyword = query.trim().toLowerCase()
  let filtered = keyword
    ? episodes.filter(episode => `${episode.episode_no} ${episode.title}`.toLowerCase().includes(keyword))
    : episodes
  const production = filters?.production || 'all'
  if (production === 'with_video') filtered = filtered.filter(episode => (episode.video_count || 0) > 0)
  if (production === 'pending_adoption') filtered = filtered.filter(episode => (episode.pending_adoption_count || 0) > 0)
  if (production === 'failed') filtered = filtered.filter(episode => (episode.failed_count || 0) > 0)
  if (production === 'unproduced') filtered = filtered.filter(episode => (episode.shot_count || 0) === 0 || (episode.video_count || 0) === 0)
  return filtered.slice(0, limit)
}
