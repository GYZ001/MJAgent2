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

/** 老留档没有 2026-09-24 新增字段时的降级文案——与改造前逐字相同，不假装
 *  拿到了具体数字（CLAUDE.md「界面承诺必须与实际行为一致」）。 */
const DEGRADED_OVER_TARGET_TEXT = '模型多次调整后仍超出短剧上限'

/** over_target 为真时的如实说明：本集最终几段、约多少秒、超出目标/上限多少；
 *  有台词预算信息且确实是台词超预算时追加原因；planned_over_cap 为真时追加
 *  "模型规划阶段就已超上限"这句区别于"归一化拆段撑大"的根因说明。
 *  final_duration_s/target_duration_s 任一缺失（老留档）时整句降级为固定
 *  文案，不用本地公式拼一个后端没给出的数字顶替。 */
export function overTargetText(summary: Pick<StoryboardAdaptationSummary,
  'segment_count' | 'final_duration_s' | 'target_duration_s' | 'max_duration_s' |
  'kept_dialogue_chars' | 'dialogue_budget_chars' | 'planned_over_cap'>): string {
  const n = summary.segment_count
  const x = summary.final_duration_s
  if (n == null || x == null || summary.target_duration_s == null) return DEGRADED_OVER_TARGET_TEXT
  let text = `本集最终 ${n} 段约 ${x} 秒，超出短剧目标约 ${summary.target_duration_s} 秒`
  if (summary.max_duration_s != null) text += `（上限 ${summary.max_duration_s} 秒）`
  const kept = summary.kept_dialogue_chars
  const budget = summary.dialogue_budget_chars
  if (kept != null && budget != null && budget > 0 && summary.max_duration_s != null && kept > budget) {
    const seconds = Math.round((kept / budget) * summary.max_duration_s)
    text += `；保留台词 ${kept} 字需要约 ${seconds} 秒口播`
  }
  if (summary.planned_over_cap) text += '；模型多次调整后规划段数仍超上限'
  return text
}
