import type { Episode } from './api'

export type EpisodeOption = Pick<Episode, 'id' | 'episode_no' | 'title'> & Partial<Pick<Episode,
  'status' | 'screenplay_status' | 'shot_count' | 'video_count' | 'pending_adoption_count' | 'failed_count' | 'open_review_count' | 'reviewed_count'
>>

export type EpisodeProductionFilter = 'all' | 'with_video' | 'pending_adoption' | 'failed' | 'unproduced'
export type EpisodeReviewFilter = 'all' | 'problem' | 'unreviewed' | 'completed'
  & Partial<Pick<Episode, 'status' | 'screenplay_status'>>

export function resolveEpisodeId(
  episodes: EpisodeOption[],
  currentEpisodeId: string | null,
): string | null {
  if (currentEpisodeId && episodes.some(episode => episode.id === currentEpisodeId)) {
    return currentEpisodeId
  }
  return episodes[0]?.id ?? null
}

export function filterEpisodeOptions(
  episodes: EpisodeOption[],
  query: string,
  limit = 60,
  filters?: { production?: EpisodeProductionFilter; review?: EpisodeReviewFilter },
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
  const review = filters?.review || 'all'
  if (review === 'problem') filtered = filtered.filter(episode => (episode.open_review_count || 0) > 0)
  if (review === 'unreviewed') filtered = filtered.filter(episode => (episode.reviewed_count || 0) < (episode.shot_count || 0))
  if (review === 'completed') filtered = filtered.filter(episode => (episode.shot_count || 0) > 0 && (episode.reviewed_count || 0) >= (episode.shot_count || 0))
  return filtered.slice(0, limit)
}
