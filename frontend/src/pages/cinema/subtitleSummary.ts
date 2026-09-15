/**
 * 成片台字幕摘要的纯函数层（U4）。输入是后端 `episode.edit-report.json` 的
 * `final_edit_report`（可能为 null、可能没有 `subtitles` 键、可能是
 * `{"enabled": false}`），一律不假设形状合法——任何缺字段或类型不对的输入
 * 都返回 null / 空数组，不抛异常（CLAUDE.md「不得兜底填充」不适用于此处的
 * 防御性解析：这里是拒绝渲染，不是编造数据）。
 *
 * `reason` 的中文文案覆盖不完整枚举：未知 reason 原样显示，不做黑名单式
 * 拒绝或兜底成空字符串。
 */

const REASON_LABELS: Record<string, string> = {
  not_found: '音轨里没有找到这句台词',
  no_audio: '该镜视频没有音轨',
  short_line_partial: '短句只念出了一部分',
}

export interface MissingSubtitleRow {
  shotNo: number
  utteranceId: string
  line: string
  ratioText: string
  reasonLabel: string
}

export interface ExtraSpeechRow {
  shotNo: number
  text: string
  range: string
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function extractSubtitles(report: unknown): Record<string, unknown> | null {
  if (!isRecord(report)) return null
  const subtitles = report.subtitles
  return isRecord(subtitles) ? subtitles : null
}

function asFiniteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function asNonNegativeInt(value: unknown): number | null {
  const n = asFiniteNumber(value)
  return n !== null && Number.isInteger(n) && n >= 0 ? n : null
}

function asNonEmptyString(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null
}

function truncateLine(line: string): string {
  return line.length > 40 ? `${line.slice(0, 40)}…` : line
}

function reasonLabel(reason: string): string {
  return REASON_LABELS[reason] ?? reason
}

/** 状态卡一行摘要；报告没有 subtitles 键（旧报告）或字段类型不对时返回 null，
 *  调用方据此决定不渲染任何东西。 */
export function subtitleSummaryLine(report: unknown): string | null {
  const subtitles = extractSubtitles(report)
  if (!subtitles) return null
  if (subtitles.enabled === false) return '字幕嵌入未开启（可在监制房系统设置中开启）'
  if (subtitles.enabled !== true) return null

  const linesTotal = asNonNegativeInt(subtitles.lines_total)
  if (linesTotal === null) return null
  if (linesTotal === 0) return '字幕：本集没有台词'

  const linesAligned = asNonNegativeInt(subtitles.lines_aligned)
  if (linesAligned === null) return null

  const linesMissing = asNonNegativeInt(subtitles.lines_missing)
  if (linesMissing === null) return null
  if (linesMissing === 0) return `字幕：${linesAligned}/${linesTotal} 句已对齐`

  const shotNos = Array.from(new Set(missingSubtitleRows(report).map(row => row.shotNo))).sort((a, b) => a - b)
  const shotsText = shotNos.length ? `（第 ${shotNos.join('、')} 镜）` : ''
  return `字幕：${linesAligned}/${linesTotal} 句已对齐，${linesMissing} 句未出声${shotsText}`
}

/** 未出声台词列表，按镜号排序；逐项校验，单条畸形数据跳过而不丢弃整份列表。 */
export function missingSubtitleRows(report: unknown): MissingSubtitleRow[] {
  const subtitles = extractSubtitles(report)
  if (!subtitles || !Array.isArray(subtitles.missing)) return []
  const rows: MissingSubtitleRow[] = []
  for (const item of subtitles.missing) {
    if (!isRecord(item)) continue
    const shotNo = asNonNegativeInt(item.shot_no)
    const utteranceId = asNonEmptyString(item.utterance_id)
    const line = typeof item.line === 'string' ? item.line : null
    const matchRatio = asFiniteNumber(item.match_ratio)
    const reason = asNonEmptyString(item.reason)
    if (shotNo === null || utteranceId === null || line === null || matchRatio === null || reason === null) {
      continue
    }
    rows.push({
      shotNo,
      utteranceId,
      line: truncateLine(line),
      ratioText: `${Math.round(matchRatio * 100)}%`,
      reasonLabel: reasonLabel(reason),
    })
  }
  return rows.sort((a, b) => a.shotNo - b.shotNo)
}

/** 「模型多念的语音」列表，按镜号排序；逐项校验，单条畸形数据跳过。 */
export function extraSpeechRows(report: unknown): ExtraSpeechRow[] {
  const subtitles = extractSubtitles(report)
  if (!subtitles || !Array.isArray(subtitles.extra_speech)) return []
  const rows: ExtraSpeechRow[] = []
  for (const item of subtitles.extra_speech) {
    if (!isRecord(item)) continue
    const shotNo = asNonNegativeInt(item.shot_no)
    const text = typeof item.text === 'string' ? item.text : null
    const startS = asFiniteNumber(item.start_s)
    const endS = asFiniteNumber(item.end_s)
    if (shotNo === null || text === null || startS === null || endS === null) continue
    rows.push({ shotNo, text, range: `${startS.toFixed(1)}s–${endS.toFixed(1)}s` })
  }
  return rows.sort((a, b) => a.shotNo - b.shotNo)
}
