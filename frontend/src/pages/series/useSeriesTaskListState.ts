import { useEffect, useMemo, useState } from 'react'
import { usePoll } from '../../App'
import { api } from '../../api'
import type { SeriesExport, SeriesTaskListResponse, SeriesTaskSummary } from '../../api'
import { deselectTasks, selectTasks, toggleTaskSelection } from './seriesTaskText'

export const SERIES_PAGE_SIZE = 50

const listPollInterval = (data: SeriesTaskListResponse | null) =>
  data && (data.queue.running_task_id != null || data.queue.queued_count > 0) ? 4000 : 0

/**
 * 连播任务列表页的全部状态与动作：分页、跨页勾选（Set<string> 只记 id，配合
 * taskCache 缓存曾经见过的 SeriesTaskSummary，让批量按钮的可用性判据在翻页后
 * 依然算得出——不这样做的话，选中一个任务后翻到下一页，第一页那个任务的
 * status/film 字段就无从得知，"选中含运行中/选中无成片" 这类判据只能瞎猜）。
 */
export function useSeriesTaskListState(projectId: string) {
  const [offset, setOffset] = useState(0)
  const list = usePoll<SeriesTaskListResponse>(
    () => api.getSeriesTasks(projectId, offset, SERIES_PAGE_SIZE),
    listPollInterval,
    [projectId, offset],
  )
  const exportsPoll = usePoll<{ exports: SeriesExport[] }>(
    () => api.getSeriesExports(projectId),
    0,
    [projectId],
  )
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [taskCache, setTaskCache] = useState<Record<string, SeriesTaskSummary>>({})
  const [actionBusy, setActionBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [startBusyTaskId, setStartBusyTaskId] = useState<string | null>(null)

  useEffect(() => {
    if (!list.data) return
    const page = list.data.tasks
    setTaskCache(prev => {
      const next = { ...prev }
      page.forEach(t => { next[t.task_id] = t })
      return next
    })
  }, [list.data])

  const tasks = list.data?.tasks ?? []
  const selectedTasks = useMemo(
    () => Array.from(selected).map(id => taskCache[id]).filter((t): t is SeriesTaskSummary => Boolean(t)),
    [selected, taskCache],
  )
  const allOnPageSelected = tasks.length > 0 && tasks.every(t => selected.has(t.task_id))

  const runAction = async (action: () => Promise<unknown>) => {
    setActionBusy(true)
    setActionError(null)
    try {
      await action()
      await list.refresh({ force: true })
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setActionBusy(false)
    }
  }

  const onStart = (taskId: string) => {
    setStartBusyTaskId(taskId)
    void runAction(() => api.enqueueSeriesTasks(projectId, [taskId])).finally(() => setStartBusyTaskId(null))
  }
  const onDelete = (taskId: string) => {
    setSelected(prev => deselectTasks(prev, [taskId]))
    void runAction(() => api.deleteSeriesTask(projectId, taskId))
  }
  const onEnqueueSelected = () => void runAction(() => api.enqueueSeriesTasks(projectId, Array.from(selected)))
  const onCancelSelected = () => void runAction(() => api.cancelSeriesTasks(projectId, Array.from(selected)))
  const onPauseQueue = () => void runAction(() => api.pauseSeriesQueue(projectId))
  const onResumeQueue = () => void runAction(() => api.resumeSeriesQueue(projectId))
  const onExportSelected = () => {
    void runAction(() => api.createSeriesExport(projectId, Array.from(selected))).then(() =>
      exportsPoll.refresh({ force: true }),
    )
  }

  return {
    offset,
    setOffset,
    list,
    tasks,
    selected,
    selectedTasks,
    allOnPageSelected,
    toggle: (taskId: string) => setSelected(prev => toggleTaskSelection(prev, taskId)),
    toggleAllOnPage: () => setSelected(prev => (
      allOnPageSelected
        ? deselectTasks(prev, tasks.map(t => t.task_id))
        : selectTasks(prev, tasks.map(t => t.task_id))
    )),
    clearSelection: () => setSelected(new Set()),
    actionBusy,
    actionError,
    startBusyTaskId,
    onStart,
    onDelete,
    onEnqueueSelected,
    onCancelSelected,
    onPauseQueue,
    onResumeQueue,
    onExportSelected,
    exports: exportsPoll.data?.exports ?? [],
    exportsLoading: exportsPoll.loading,
    exportsError: exportsPoll.error,
  }
}
