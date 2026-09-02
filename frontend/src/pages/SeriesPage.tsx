import { useNav } from '../App'
import QueryState from '../components/QueryState'
import OperationError from '../components/OperationError'
import SeriesTaskPlanner from './series/SeriesTaskPlanner'
import SeriesTaskBar from './series/SeriesTaskBar'
import SeriesTaskList from './series/SeriesTaskList'
import SeriesExportPanel from './series/SeriesExportPanel'
import SeriesTaskDetail from './series/SeriesTaskDetail'
import { SERIES_PAGE_SIZE, useSeriesTaskListState } from './series/useSeriesTaskListState'
import '../styles/SeriesPage.css'

/** 连播台入口：按 useNav().taskId 分流（路由 /projects/{pid}/series/{taskId}，见
 *  App.tsx 的 Nav.taskId）。空 = 任务列表页，有值 = 任务详情页。刷新页面要停在
 *  原地，所以分流必须挂在路由上，不能只靠组件内部 state。 */
export default function SeriesPage() {
  const { projectId, taskId } = useNav()
  if (!projectId) return null
  return taskId
    ? <SeriesTaskDetail projectId={projectId} taskId={taskId} />
    : <SeriesTaskListView projectId={projectId} />
}

function SeriesTaskListView({ projectId }: { projectId: string }) {
  const { go } = useNav()
  const state = useSeriesTaskListState(projectId)
  const { list } = state

  if (!list.data) {
    return (
      <QueryState
        loading={list.loading}
        error={list.error}
        status={list.status}
        hasData={false}
        objectName="连播任务列表"
        onRetry={() => void list.refresh({ force: true })}
      >
        {null}
      </QueryState>
    )
  }

  const { totals, queue, episodes, default_group_size: defaultGroupSize, offset } = list.data
  const pageCount = Math.max(1, Math.ceil(totals.all / SERIES_PAGE_SIZE))
  const curPage = Math.floor(offset / SERIES_PAGE_SIZE)

  return (
    <>
      <header className="desk-head">
        <div className="crumb">漫剧案头 / 连播台</div>
        <h1>连播台 <span className="sub">按每 N 集切分成任务，勾选后批量串行执行</span></h1>
        <hr className="rule" />
      </header>
      <p className="series-intro">
        全项目共 {episodes.total} 集（第 {episodes.min_no}-{episodes.max_no} 集），已生成 {totals.all} 个连播任务：
        未开始 {totals.idle}、排队 {totals.queued}、执行中 {totals.running}、已完成 {totals.succeeded}、
        失败 {totals.failed}、已取消 {totals.cancelled}。
      </p>
      <SeriesTaskPlanner
        projectId={projectId}
        defaultGroupSize={defaultGroupSize}
        onCreated={() => void list.refresh({ force: true })}
      />
      <SeriesTaskBar
        queue={queue}
        selectedTasks={state.selectedTasks}
        busy={state.actionBusy}
        onEnqueueSelected={state.onEnqueueSelected}
        onCancelSelected={state.onCancelSelected}
        onExportSelected={state.onExportSelected}
        onPauseQueue={state.onPauseQueue}
        onResumeQueue={state.onResumeQueue}
        onClearSelection={state.clearSelection}
      />
      {state.actionError && <OperationError title="操作失败" guidance={state.actionError} />}
      <SeriesTaskList
        tasks={state.tasks}
        selected={state.selected}
        onToggle={state.toggle}
        onToggleAllOnPage={state.toggleAllOnPage}
        allOnPageSelected={state.allOnPageSelected}
        onStart={state.onStart}
        startBusyTaskId={state.startBusyTaskId}
        onView={taskId => go('series', projectId, undefined, undefined, 'push', taskId)}
        onDelete={state.onDelete}
      />
      {pageCount > 1 && (
        <div className="series-pagination" aria-label="连播任务分页">
          <button
            type="button"
            className="btn small"
            disabled={curPage <= 0}
            onClick={() => state.setOffset(Math.max(0, offset - SERIES_PAGE_SIZE))}
          >
            ← 上一页
          </button>
          <span>{`第 ${curPage + 1} / ${pageCount} 页 · 共 ${totals.all} 个任务`}</span>
          <button
            type="button"
            className="btn small"
            disabled={curPage >= pageCount - 1}
            onClick={() => state.setOffset(offset + SERIES_PAGE_SIZE)}
          >
            下一页 →
          </button>
        </div>
      )}
      <SeriesExportPanel exports={state.exports} loading={state.exportsLoading} error={state.exportsError} />
    </>
  )
}
