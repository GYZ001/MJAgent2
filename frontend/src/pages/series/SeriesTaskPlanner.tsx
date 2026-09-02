import { useState } from 'react'
import { api } from '../../api'
import type { SeriesTaskPlanResponse } from '../../api'
import OperationError from '../../components/OperationError'
import { seriesPlanSummaryText, validateGroupSize } from './seriesTaskText'

/** 「按每 N 集切分生成任务清单」表单：填 group_size → 先预览（GET plan，不落库）
 *  → 确认后才真正创建（POST，补齐式、幂等）。确认按钮只在「刚预览过的 group_size
 *  与当前输入一致」时可用，避免用户改了数字却拿旧预览去确认。 */
export default function SeriesTaskPlanner({
  projectId,
  defaultGroupSize,
  onCreated,
}: {
  projectId: string
  defaultGroupSize: number
  onCreated: () => void
}) {
  const [groupSize, setGroupSize] = useState(defaultGroupSize)
  const [plan, setPlan] = useState<SeriesTaskPlanResponse | null>(null)
  const [previewedSize, setPreviewedSize] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<string | null>(null)
  const validation = validateGroupSize(groupSize)

  const changeGroupSize = (value: number) => {
    setGroupSize(value)
    setPlan(null)
    setPreviewedSize(null)
    setResult(null)
  }

  const preview = async () => {
    if (!validation.ok) return
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      setPlan(await api.getSeriesTaskPlan(projectId, groupSize))
      setPreviewedSize(groupSize)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const confirm = async () => {
    if (!plan || previewedSize !== groupSize) return
    setBusy(true)
    setError(null)
    try {
      const created = await api.createSeriesTasks(projectId, { group_size: groupSize })
      setResult(`已生成：新增 ${created.created} 个，已存在 ${created.existing} 个，共 ${created.tasks_total} 个任务`)
      setPlan(null)
      setPreviewedSize(null)
      onCreated()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="series-planner card">
      <h3>按每 N 集切分生成任务清单</h3>
      <div className="series-planner-form">
        <label className="f series-planner-field">
          每组集数（1–10）
          <input
            type="number"
            min={1}
            max={10}
            value={groupSize}
            disabled={busy}
            onChange={event => changeGroupSize(Number(event.target.value))}
          />
        </label>
        <button type="button" className="btn" disabled={busy || !validation.ok} onClick={() => void preview()}>
          预览
        </button>
        <button
          type="button"
          className="btn primary"
          disabled={busy || !plan || previewedSize !== groupSize}
          onClick={() => void confirm()}
        >
          {busy ? '处理中…' : '生成任务清单'}
        </button>
      </div>
      {!validation.ok && <p className="series-range-invalid" role="alert">{validation.reason}</p>}
      {plan && <p className="series-plan-summary">{seriesPlanSummaryText(plan)}</p>}
      {result && <p className="series-plan-result" role="status">{result}</p>}
      {error && <OperationError title="操作失败" guidance={error} />}
    </section>
  )
}
