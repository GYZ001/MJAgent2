/**
 * 成片合成三项增强（统一配乐/片头预告/主角内心独白）的展示纯函数层。输入是
 * 后端 `episode.edit-report.json` 的 `final_edit_report.enhancements`（可能
 * 不存在——旧报告或本次合成前的报告没有这个键），任何缺字段/类型不对都跳过
 * 渲染而不是编造，与 `subtitleSummary.ts` 同一套防御性解析约定。
 */

const FEATURE_LABELS: Record<string, string> = {
  music_bed: '统一配乐',
  teaser: '片头预告',
  monologue: '主角内心独白',
}

const FEATURE_ORDER = ['music_bed', 'teaser', 'monologue'] as const

export interface EnhancementFeatureSummary {
  key: (typeof FEATURE_ORDER)[number]
  label: string
  applied: boolean
  reason: string
  details: string[]
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function asFiniteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function extractEnhancements(report: unknown): Record<string, unknown> | null {
  if (!isRecord(report)) return null
  const enhancements = report.enhancements
  return isRecord(enhancements) ? enhancements : null
}

function musicBedDetails(fragment: Record<string, unknown>): string[] {
  const tracks = Array.isArray(fragment.tracks) ? fragment.tracks : []
  const titles = tracks
    .filter(isRecord)
    .map(track => (typeof track.title === 'string' ? track.title : null))
    .filter((title): title is string => title !== null)
  return titles.length ? [`使用曲目：${titles.join('、')}`] : []
}

function teaserDetails(fragment: Record<string, unknown>): string[] {
  const clips = Array.isArray(fragment.clips) ? fragment.clips : []
  const duration = asFiniteNumber(fragment.duration_s)
  const lines: string[] = []
  if (duration !== null) lines.push(`预告总长 ${duration.toFixed(1)} 秒`)
  for (const clip of clips) {
    if (!isRecord(clip)) continue
    const shotNo = asFiniteNumber(clip.shot_no)
    const startS = asFiniteNumber(clip.start_s)
    const endS = asFiniteNumber(clip.end_s)
    const reason = typeof clip.reason === 'string' ? clip.reason : ''
    if (shotNo === null || startS === null || endS === null) continue
    lines.push(`第 ${shotNo} 段 ${startS.toFixed(1)}–${endS.toFixed(1)}s${reason ? `：${reason}` : ''}`)
  }
  return lines
}

function monologueDetails(fragment: Record<string, unknown>): string[] {
  const items = Array.isArray(fragment.lines) ? fragment.lines : []
  const lines: string[] = []
  for (const item of items) {
    if (!isRecord(item)) continue
    const character = typeof item.character_name === 'string' ? item.character_name : ''
    const text = typeof item.text === 'string' ? item.text : ''
    if (!character || !text) continue
    lines.push(`${character}：「${text}」`)
  }
  const skipped = Array.isArray(fragment.skipped) ? fragment.skipped : []
  if (skipped.length) lines.push(`另有 ${skipped.length} 句独白候选未采用（见理由）`)
  return lines
}

const DETAIL_BUILDERS: Record<string, (fragment: Record<string, unknown>) => string[]> = {
  music_bed: musicBedDetails,
  teaser: teaserDetails,
  monologue: monologueDetails,
}

/** 模型提名过、但被代码核验判定不满足而丢弃的条目——不管该项本身是否
 *  applied，都要让用户看到"模型提过什么、为什么没被采用"，而不只是一份
 *  已采纳条目的精简清单。 */
function rejectedDetails(fragment: Record<string, unknown>): string[] {
  const rejected = Array.isArray(fragment.rejected) ? fragment.rejected : []
  const lines: string[] = []
  for (const entry of rejected) {
    if (!isRecord(entry)) continue
    const reason = typeof entry.reason === 'string' ? entry.reason : ''
    if (reason) lines.push(`模型提名未采用：${reason}`)
  }
  return lines
}

/** 逐项 applied/skipped 摘要；`enhancements` 键不存在时返回空数组（本次报告
 *  在增强上线前生成，或增强层因异常整体跳过前也会走到这里——两者对用户呈现
 *  的动作相同：不渲染这块面板）。 */
export function enhancementFeatureSummaries(report: unknown): EnhancementFeatureSummary[] {
  const enhancements = extractEnhancements(report)
  if (!enhancements) return []
  const summaries: EnhancementFeatureSummary[] = []
  for (const key of FEATURE_ORDER) {
    const fragment = enhancements[key]
    if (!isRecord(fragment)) continue
    const applied = fragment.applied === true
    const reason = typeof fragment.reason === 'string' ? fragment.reason : ''
    const details = applied ? DETAIL_BUILDERS[key](fragment) : []
    summaries.push({ key, label: FEATURE_LABELS[key], applied, reason, details: [...details, ...rejectedDetails(fragment)] })
  }
  return summaries
}
