import type { Episode } from './api'

export type EpisodeOption = Pick<Episode, 'id' | 'episode_no' | 'title'>

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
): EpisodeOption[] {
  const keyword = query.trim().toLowerCase()
  const filtered = keyword
    ? episodes.filter(episode => `${episode.episode_no} ${episode.title}`.toLowerCase().includes(keyword))
    : episodes
  return filtered.slice(0, limit)
}
