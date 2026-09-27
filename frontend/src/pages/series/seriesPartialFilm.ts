// 连播任务台「成片缺段」文案：纯函数，供 SeriesTaskList（任务列表跨集摘要）与
// SeriesProgressBoard（详情页单集提示）共用，如实转述后端已持久化的 concat 结果
// （app.domain.series_ops.stages.final_partial_status），不编造原因、不吞掉。
//
// 单独成一个小文件而不是并进 seriesTaskText.ts：seriesTaskText.ts 已经
// `import { SERIES_STAGE_LABEL } from './SeriesProgressBoard'`，若把这里的函数
// 也放进去，SeriesProgressBoard.tsx 再反向 import 回 seriesTaskText.ts 就会
// 形成双向模块循环；这个新文件不依赖 SeriesProgressBoard，两个消费方各自单向
// import，不产生环。

import type { EpisodeEntry, SeriesTaskPartialEpisode } from '../../api'

type SkipInfo = Pick<EpisodeEntry, 'skipped_shot_nos' | 'skip_reasons'>
type StaleAwareSkipInfo = SkipInfo & Pick<EpisodeEntry, 'final_video_stale'>

/** 「缺第 3、7 段：供应商拒收」——没有具体原因时只报段号，不杜撰。 */
export function partialFilmDetail(ep: SkipInfo): string {
  const nos = ep.skipped_shot_nos
  if (nos.length === 0) return ''
  const reasons = Array.from(
    new Set(nos.map(no => ep.skip_reasons[String(no)]).filter((r): r is string => !!r)),
  )
  return `缺第 ${nos.join('、')} 段${reasons.length ? `：${reasons.join('、')}` : ''}`
}

// 采纳新镜头只给 final/episode.mp4 打 .stale 标记，不会改这份缺段清单所在的
// episode.edit-report.json；stale 为真时清单是「合成那一刻」的旧数据，不能再
// 当作现状展示，否则用户按旧清单重拍/采纳，看到的仍是同一句过期提示——与
// CinemaPage 对 final_video_stale 优先于 final_is_partial 的展示顺序一致。
const STALE_NOTICE = '成片信息可能已过期（合成后又发生变化），请重新合成后确认是否仍缺段'

/** 详情页单集提示：「成片已合成（缺第 3、7 段：供应商拒收）」；不缺段返回 null；
 *  已过期时不复述可能不准的旧清单，改提示重新合成。 */
export function episodePartialFilmText(ep: StaleAwareSkipInfo): string | null {
  if (ep.skipped_shot_nos.length === 0) return null
  if (ep.final_video_stale) return STALE_NOTICE
  return `成片已合成（${partialFilmDetail(ep)}）`
}

/** 任务列表行用的跨集缺段摘要：给出路——提示去查看详情，到对应集的成片台重拍或采纳。 */
export function seriesTaskPartialFilmSummary(partialEpisodes: SeriesTaskPartialEpisode[]): string | null {
  if (partialEpisodes.length === 0) return null
  const parts = partialEpisodes.map(ep =>
    ep.final_video_stale ? `第 ${ep.episode_no} 集${STALE_NOTICE}` : `第 ${ep.episode_no} 集${partialFilmDetail(ep)}`,
  )
  return `${parts.join('；')}，点击"查看"到对应集的成片台重拍或采纳`
}
