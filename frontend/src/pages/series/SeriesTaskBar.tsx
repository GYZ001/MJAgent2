import type { SeriesQueueState, SeriesTaskSummary } from '../../api'
import { seriesBatchAvailability, seriesQueueStatusText } from './seriesTaskText'

/** 队列状态条 + 批量操作条：暂停/继续队列、当前在跑哪个任务、队列剩余数、连续
 *  失败自动停队时展示 stop_reason 原文并给「继续队列」；批量按钮按勾选集合的
 *  可用性判据禁用，不额外弹二次确认（P0 拍板：只有删除单个任务才需要确认）。 */
export default function SeriesTaskBar({
  queue,
  selectedTasks,
  busy,
  onEnqueueSelected,
  onCancelSelected,
  onExportSelected,
  onPauseQueue,
  onResumeQueue,
  onClearSelection,
}: {
  queue: SeriesQueueState
  selectedTasks: SeriesTaskSummary[]
  busy: boolean
  onEnqueueSelected: () => void
  onCancelSelected: () => void
  onExportSelected: () => void
  onPauseQueue: () => void
  onResumeQueue: () => void
  onClearSelection: () => void
}) {
  const availability = seriesBatchAvailability(selectedTasks)
  const selectedCount = selectedTasks.length
  const queueTone = queue.stop_reason ? 'red' : queue.paused ? 'grey' : 'gold'

  return (
    <section className="series-task-bar card">
      <div className="series-queue-status">
        <span className={`stamp ${queueTone}`}>{seriesQueueStatusText(queue)}</span>
        {queue.paused || queue.stop_reason ? (
          <button type="button" className="btn" disabled={busy} onClick={onResumeQueue}>继续队列</button>
        ) : (
          <button type="button" className="btn" disabled={busy || !queue.running_task_id} onClick={onPauseQueue}>
            暂停队列
          </button>
        )}
      </div>
      {selectedCount > 0 && (
        <div className="series-batch-bar">
          <span className="series-batch-count">{`已选 ${selectedCount} 个`}</span>
          <button type="button" className="btn small" onClick={onClearSelection}>清空勾选</button>
          <div className="series-batch-actions">
            <button
              type="button"
              className="btn primary"
              disabled={busy || availability.enqueueDisabled}
              onClick={onEnqueueSelected}
            >
              串行执行选中
            </button>
            <button type="button" className="btn" disabled={busy || availability.cancelDisabled} onClick={onCancelSelected}>
              取消选中
            </button>
            <button type="button" className="btn" disabled={busy || availability.exportDisabled} onClick={onExportSelected}>
              打包导出选中
            </button>
          </div>
          <p className="series-batch-hint">
            按勾选顺序串行执行，一次只跑一个任务；已完成的任务会被跳过，需要重跑请先删除或勾选重跑。
          </p>
        </div>
      )}
    </section>
  )
}
