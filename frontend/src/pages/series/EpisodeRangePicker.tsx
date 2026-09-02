import { useId } from 'react'
import type { SeriesEpisodeAvailable } from '../../api'

/** 一次连播最多串多少集——契约里的硬约束（后端同一常量，前端只是提前给出反馈，
 *  真正兜底校验仍在服务端）。 */
export const SERIES_MAX_SPAN = 10

export interface EpisodeRangeValidation {
  ok: boolean
  reason?: string
  missingEpisodeNos?: number[]
  count?: number
}

/** 纯函数，供 SeriesPage.test.ts 单测：结束集不早于起始集、跨度不超过
 *  SERIES_MAX_SPAN、区间内每一集号都必须已存在（不要求连续以外的其它前提，
 *  1 集即 from===to 合法）。 */
export function validateEpisodeRange(
  available: SeriesEpisodeAvailable[],
  from: number | null,
  to: number | null,
): EpisodeRangeValidation {
  if (from == null || to == null) return { ok: false, reason: '请选择起始集与结束集' }
  if (to < from) return { ok: false, reason: '结束集不能早于起始集' }
  const count = to - from + 1
  if (count > SERIES_MAX_SPAN) {
    return {
      ok: false,
      reason: `一次最多连续制作 ${SERIES_MAX_SPAN} 集，当前选择了 ${count} 集`,
      count,
    }
  }
  const availableNos = new Set(available.map(ep => ep.episode_no))
  const missingEpisodeNos: number[] = []
  for (let no = from; no <= to; no++) {
    if (!availableNos.has(no)) missingEpisodeNos.push(no)
  }
  if (missingEpisodeNos.length) {
    return {
      ok: false,
      reason: `第 ${missingEpisodeNos.join('、')} 集尚未创建，区间内每一集都必须已存在`,
      missingEpisodeNos,
      count,
    }
  }
  return { ok: true, count }
}

export default function EpisodeRangePicker({
  available,
  from,
  to,
  onChangeFrom,
  onChangeTo,
  disabled,
}: {
  available: SeriesEpisodeAvailable[]
  from: number | null
  to: number | null
  onChangeFrom: (episodeNo: number) => void
  onChangeTo: (episodeNo: number) => void
  disabled?: boolean
}) {
  const sorted = [...available].sort((a, b) => a.episode_no - b.episode_no)
  const validation = validateEpisodeRange(available, from, to)
  const touched = from != null && to != null
  const fromId = useId()
  const toId = useId()
  const options = sorted.map(ep => (
    <option key={ep.episode_id} value={ep.episode_no}>
      第 {ep.episode_no} 集{ep.title ? ` · ${ep.title}` : ''}
    </option>
  ))

  return (
    <section className="series-range-picker card">
      <div className="series-range-fields">
        <div className="series-range-field">
          <label className="f" htmlFor={fromId}>起始集</label>
          <select
            id={fromId}
            value={from ?? ''}
            disabled={disabled}
            onChange={event => onChangeFrom(Number(event.target.value))}
          >
            <option value="" disabled>请选择</option>
            {options}
          </select>
        </div>
        <div className="series-range-field">
          <label className="f" htmlFor={toId}>结束集</label>
          <select
            id={toId}
            value={to ?? ''}
            disabled={disabled}
            onChange={event => onChangeTo(Number(event.target.value))}
          >
            <option value="" disabled>请选择</option>
            {options}
          </select>
        </div>
      </div>
      {validation.ok ? (
        <p className="series-range-summary">
          将串行制作第 {from}–{to} 集，共 {validation.count} 集
        </p>
      ) : touched ? (
        <p className="series-range-summary series-range-invalid" role="alert">{validation.reason}</p>
      ) : null}
    </section>
  )
}
