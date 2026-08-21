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

/** 构造窗口化 picker 的查询串；空值一律不写入，URL 才稳定、可缓存。 */
export function pickerWindowParams(
  limit: number,
  cursor: string | null = null,
  options: { query?: string; production?: EpisodeProductionFilter } = {},
): string {
  const params = new URLSearchParams({ episode_limit: String(limit) })
  if (cursor) params.set('episode_cursor', cursor)
  const query = options.query?.trim()
  if (query) params.set('episode_query', query)
  if (options.production && options.production !== 'all') {
    params.set('episode_filter', options.production)
  }
  return params.toString()
}

/** 窗口化 picker 的分集解析。
 *
 * 窗口模式下 `episodes` 只是全量里的一小段，不能再用「当前 id 是否在数组里」判断有效性；
 * `episode_current` 才是服务端对「光标是否仍属于本项目」的判定。
 * 光标失效时退回窗口首条——服务端在无光标时把窗口落在第一集，故等价于取首集。
 */
export function resolveWindowedEpisodeId(
  picker: { episode_current?: { id: string } | null; episodes?: EpisodeOption[] },
  currentEpisodeId: string | null,
  requestedEpisodeId: string | null = null,
): string | null {
  if (requestedEpisodeId) return requestedEpisodeId
  if (currentEpisodeId && picker.episode_current?.id === currentEpisodeId) {
    return currentEpisodeId
  }
  return picker.episode_current?.id ?? picker.episodes?.[0]?.id ?? null
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
