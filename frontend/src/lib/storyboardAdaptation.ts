import type { StoryboardAdaptationSummary } from '../api'

/** 每段固定 15 秒（2.4.0 起分镜台契约），约合时长按段数直接乘——不是精确时长，
 *  只用于给用户一个数量级参照，与目标时长对比。 */
const SECONDS_PER_SEGMENT = 15

export function adaptationModeLabel(summary: Pick<StoryboardAdaptationSummary, 'recorded' | 'adaptation_mode'>): string {
  if (!summary.recorded) return '旧分镜，生成时未记录改编档位'
  if (summary.adaptation_mode === 'short_drama') return '短剧节奏'
  if (summary.adaptation_mode === 'faithful') return '忠实原著'
  return summary.adaptation_mode
}

/** 面板标题：折叠态也要能一眼看出有没有删减、删了多少（用户要求带计数）。 */
export function adaptationPanelTitle(spanCount: number, lineCount: number): string {
  return `本集删减 · ${spanCount} 处原文 / ${lineCount} 句台词`
}

/** 段数 × 15 秒 与目标时长的对比文案；segmentCount 为 null（老分集未记录）时
 *  返回空串，调用方据此不渲染这一行，而不是显示一个编造的"0 段"。 */
export function durationComparisonText(segmentCount: number | null, targetDurationS: number | null): string {
  if (segmentCount == null) return ''
  const estimated = segmentCount * SECONDS_PER_SEGMENT
  return targetDurationS == null
    ? `${segmentCount} 段 · 约 ${estimated} 秒`
    : `${segmentCount} 段 · 约 ${estimated} 秒（目标 ${targetDurationS} 秒）`
}
